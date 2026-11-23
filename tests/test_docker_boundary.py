"""Verify broker authorization and generated Docker operations without a daemon."""
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from backend.docker_backend import BrokerError, Config, DockerBroker, OWNER_LABEL, WORKSPACE_LABEL, RESOURCE_LABEL, ADMIN_AUTH_ID
from backend.shared_auth import AUTH_MOUNT, AUTH_HELPER, LAUNCH_WITH_SHARED_AUTH


class DockerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.broker = DockerBroker(Config(data_dir=self.temporary.name))
        self.workspace_id = "a" * 32
        self.broker.db.execute("INSERT INTO workspaces (id,name,created_at) VALUES (?,?,?)", (self.workspace_id, "合法名称; $(id)", "2026-11-15T00:00:00Z"))
        self.broker.db.commit()

    def tearDown(self):
        self.broker.close()
        self.temporary.cleanup()

    def owned(self, kind, **values):
        labels = {OWNER_LABEL: self.broker.owner, WORKSPACE_LABEL: self.workspace_id}
        return {**({"Config": {"Labels": labels}} if kind == "container" else {"Labels": labels}), **values}

    def existing_worker(self, *, running=False, attached=False, proxy_running=True):
        names = self.broker.names(self.workspace_id)
        self.broker._model_policy_ready = True
        self.broker._policy_checked_workers.add(names["container"])
        proxy_networks = {names["network"]: {"Aliases": ["egress"]}} if attached else {}
        resources = {
            ("container", names["container"]): self.owned("container", State={"Running": running},
                HostConfig={"Memory": self.broker.resource_limits()[0], "MemorySwap": self.broker.resource_limits()[0],
                            "NanoCpus": self.broker.resource_limits()[1]},
                Mounts=[{"Type": "volume", "Name": self.broker.model_policy_volume, "Destination": "/etc/claude-code", "RW": False},
                        {"Type": "volume", "Name": self.broker.shared_auth_volume, "Destination": AUTH_MOUNT, "RW": False}]),
            ("volume", self.broker.shared_auth_volume): {"Labels": {OWNER_LABEL: self.broker.owner, RESOURCE_LABEL: "shared-auth"}},
            ("network", names["network"]): self.owned(
                "network", Internal=True,
                Options={"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}),
            ("container", self.broker.config.egress_container): {
                "State": {"Running": proxy_running}, "NetworkSettings": {"Networks": proxy_networks}},
        }

        async def docker(*args, **kwargs):
            if args[1] == "inspect":
                resource = resources.get((args[0], args[2]))
                if resource is None:
                    return 1, b"", b"No such object"
                return 0, json.dumps([resource]).encode(), b""
            if args[:2] == ("network", "connect"):
                proxy_networks[args[4]] = {"Aliases": [args[3]]}
            return 0, b"", b""

        self.broker.command = AsyncMock(side_effect=docker)
        return names, resources

    async def test_unknown_or_command_like_id_never_reaches_docker(self):
        self.broker.command = AsyncMock()
        for value in ("b" * 32, "../other", "--privileged", "a;id", ""):
            with self.subTest(value=value), self.assertRaises(BrokerError):
                await self.broker.stop(value)
        self.broker.command.assert_not_called()

    async def test_resource_labels_must_match_install_and_workspace(self):
        for labels in ({}, {OWNER_LABEL: "someone-else", WORKSPACE_LABEL: self.workspace_id}, {OWNER_LABEL: self.broker.owner, WORKSPACE_LABEL: "b" * 32}):
            self.broker.command = AsyncMock(return_value=(0, json.dumps([{"Config": {"Labels": labels}}]).encode(), b""))
            with self.subTest(labels=labels), self.assertRaises(BrokerError):
                await self.broker.inspect_owned("container", "irrelevant", self.workspace_id)

    async def test_docker_failure_is_not_mistaken_for_missing_resource(self):
        self.broker.command = AsyncMock(return_value=(1, b"", b"permission denied connecting to daemon"))
        with self.assertRaises(BrokerError):
            await self.broker.inspect_owned("volume", "irrelevant", self.workspace_id)

    async def test_old_engine_is_rejected_without_network_fallback(self):
        self.broker.command = AsyncMock(return_value=(0, b"27.5.1\n", b""))
        with self.assertRaises(BrokerError):
            await self.broker.check_engine()
        self.assertFalse(self.broker._engine_checked)

    async def test_worker_has_only_own_volumes_and_isolated_network(self):
        names = self.broker.names(self.workspace_id)

        async def docker(*args, **kwargs):
            if args[0] == "exec" and args[-1] == "--version":
                return 0, b"2.1.311 (Claude Code)\n", b""
            if args[:2] == ("container", "inspect") and args[2] == self.broker.config.egress_container:
                return 0, json.dumps([{"State": {"Running": True}, "NetworkSettings": {"Networks": {}}}]).encode(), b""
            if len(args) > 1 and args[1] == "inspect" and args[0] != "image":
                return 1, b"", b"No such object"
            return 0, b"[]", b""

        self.broker.command = AsyncMock(side_effect=docker)
        await self.broker._start(self.workspace_id)
        calls = [call.args for call in self.broker.command.call_args_list]
        create = next(args for args in calls if args[:2] == ("container", "create"))
        network = next(args for args in calls if args[:2] == ("network", "create"))
        self.assertIn("--internal", network)
        self.assertIn("com.docker.network.bridge.gateway_mode_ipv4=isolated", network)
        self.assertIn("--read-only", create)
        self.assertIn("no-new-privileges=true", create)
        self.assertEqual(create[create.index("--user") + 1], "1000:1000")
        self.assertEqual(create[create.index("--cap-drop") + 1], "ALL")
        self.assertEqual(create[create.index("--network") + 1], names["network"])
        mounts = [create[i + 1] for i, arg in enumerate(create[:-1]) if arg == "--mount"]
        self.assertEqual(set(mounts), {
            f"type=volume,src={names['files']},dst=/workspace",
            f"type=volume,src={names['home']},dst=/home/claude",
            f"type=volume,src={self.broker.model_policy_volume},dst=/etc/claude-code,readonly,volume-nocopy",
            f"type=volume,src={self.broker.shared_auth_volume},dst={AUTH_MOUNT},readonly,volume-nocopy",
        })
        for forbidden in ("--privileged", "--pid", "--publish", "--device", "--volume", "-v"):
            self.assertNotIn(forbidden, create)
        self.assertFalse(any("docker.sock" in arg for arg in create))
        self.assertFalse(any("合法名称" in arg for arg in create))
        self.assertIn("HTTPS_PROXY=http://egress:3128", create)

    async def test_existing_workers_restore_recreated_proxy_before_start(self):
        for running in (False, True):
            with self.subTest(running=running):
                names, _ = self.existing_worker(running=running)
                await self.broker._start(self.workspace_id)
                calls = [call.args for call in self.broker.command.call_args_list]
                connect = ("network", "connect", "--alias", "egress", names["network"], self.broker.config.egress_container)
                self.assertEqual(calls.count(connect), 1)
                start = ("container", "start", names["container"])
                if running:
                    self.assertNotIn(start, calls)
                else:
                    self.assertLess(calls.index(connect), calls.index(start))
                self.assertFalse(any(args[1] in ("create", "rm") or args[0] == "image" for args in calls))

    async def test_existing_proxy_attachment_is_idempotent(self):
        for attached in (False, True):
            with self.subTest(initially_attached=attached):
                self.existing_worker(running=True, attached=attached)
                await self.broker._start(self.workspace_id)
                await self.broker._start(self.workspace_id)
                calls = [call.args for call in self.broker.command.call_args_list]
                self.assertEqual(sum(args[:2] == ("network", "connect") for args in calls), 0 if attached else 1)

    async def test_existing_worker_rejects_missing_or_unsafe_network_before_mutation(self):
        for condition in ("missing", "wrong-owner", "external", "not-isolated"):
            with self.subTest(condition=condition):
                names, resources = self.existing_worker()
                network = resources[("network", names["network"])]
                if condition == "missing":
                    del resources[("network", names["network"])]
                elif condition == "wrong-owner":
                    network["Labels"][OWNER_LABEL] = "another-install"
                elif condition == "external":
                    network["Internal"] = False
                else:
                    network["Options"] = {}
                with self.assertRaises(BrokerError) as raised:
                    await self.broker._start(self.workspace_id)
                self.assertEqual(raised.exception.status, 409)
                self.assertTrue(all(call.args[1] == "inspect" for call in self.broker.command.call_args_list))

    async def test_existing_worker_rejects_stopped_proxy_before_mutation(self):
        self.existing_worker(proxy_running=False)
        with self.assertRaises(BrokerError) as raised:
            await self.broker._start(self.workspace_id)
        self.assertEqual(raised.exception.status, 503)
        self.assertTrue(all(call.args[1] == "inspect" for call in self.broker.command.call_args_list))

    async def test_failed_proxy_reconnection_prevents_worker_start(self):
        self.existing_worker()
        inspect = self.broker.command.side_effect

        async def docker(*args, **kwargs):
            if args[:2] == ("network", "connect"):
                raise BrokerError("proxy connection failed", 503)
            return await inspect(*args, **kwargs)

        self.broker.command.side_effect = docker
        with self.assertRaises(BrokerError):
            await self.broker._start(self.workspace_id)
        self.assertFalse(any(call.args[:2] == ("container", "start") for call in self.broker.command.call_args_list))

    async def test_delete_preflights_all_owners_before_removing_anything(self):
        async def ownership(kind, name, workspace_id):
            if kind == "network":
                raise BrokerError("wrong owner", 409)
            return self.owned(kind)

        self.broker.inspect_owned = AsyncMock(side_effect=ownership)
        self.broker.command = AsyncMock()
        with self.assertRaises(BrokerError):
            await self.broker.delete(self.workspace_id)
        self.broker.command.assert_not_called()
        self.assertEqual(self.broker.get(self.workspace_id)["id"], self.workspace_id)

    async def test_file_paths_are_validated_before_docker(self):
        self.broker.command = AsyncMock()
        for path in ("/etc/passwd", "../home/.claude.json", "a/../../etc/passwd"):
            with self.subTest(path=path), self.assertRaises(BrokerError):
                await self.broker.files(self.workspace_id, "read", path)
        self.broker.command.assert_not_called()

    async def test_failed_delete_commit_keeps_registry_visible_for_retry_after_restart(self):
        self.broker.inspect_owned = AsyncMock(return_value=None)
        connection = self.broker.db
        self.broker.db = Mock(wraps=connection)
        self.broker.db.commit.side_effect = sqlite3.OperationalError("disk full")
        with self.assertRaises(sqlite3.OperationalError):
            await self.broker.delete(self.workspace_id)
        self.broker.db.rollback.assert_called_once()
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.broker.get(self.workspace_id)["id"], self.workspace_id)
        self.assertEqual(len(await self.broker.list()), 1)
        self.broker.close()
        self.broker = DockerBroker(Config(data_dir=self.temporary.name))
        self.broker.inspect_owned = AsyncMock(return_value=None)
        self.assertEqual(self.broker.get(self.workspace_id)["id"], self.workspace_id)
        await self.broker.delete(self.workspace_id)
        self.assertEqual(await self.broker.list(), [])

    async def test_file_payload_is_stdin_to_fixed_isolated_python_helper(self):
        self.broker.workspace = AsyncMock(return_value={"running": True})
        self.broker.command = AsyncMock(return_value=(0, b'{"ok":true}', b""))
        text = "$(touch /tmp/pwned); `id`"
        await self.broker.files(self.workspace_id, "write", "quote'file.txt", content=text)
        call = self.broker.command.call_args
        from backend.docker_backend import FILE_HELPER
        self.assertEqual(call.args[-4:], ("python3", "-I", "-c", FILE_HELPER))
        self.assertNotIn(text, call.args)
        self.assertEqual(json.loads(call.kwargs["input_data"])["content"], text)

    async def test_terminal_starts_fixed_claude_not_shell_or_arbitrary_command(self):
        self.broker.workspace = AsyncMock(return_value={"running": True})
        command = await self.broker.terminal_command(self.workspace_id)
        self.assertEqual(command[-1], "/usr/local/bin/claude")
        self.assertIn(self.broker.names(self.workspace_id)["container"], command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("sh", command)
        self.assertNotIn("bash", command)

    async def test_model_policy_writer_uses_only_owned_volume_and_managed_keys(self):
        async def docker(*args, **kwargs):
            if args[1] == "inspect":
                return 1, b"", b"No such volume"
            return 0, b"", b""

        self.broker.command = AsyncMock(side_effect=docker)
        for allowed in (False, True):
            await self.broker.apply_model_policy(allowed)
            call = self.broker.command.call_args
            self.assertEqual(call.args[:2], ("run", "--rm"))
            self.assertEqual(call.args[call.args.index("--network") + 1], "none")
            self.assertEqual(call.args[call.args.index("--workdir") + 1], "/")
            mounts = [call.args[i + 1] for i, arg in enumerate(call.args[:-1]) if arg == "--mount"]
            self.assertEqual(mounts, [f"type=volume,src={self.broker.model_policy_volume},dst=/policy,volume-nocopy"])
            self.assertIn("--read-only", call.args)
            self.assertIn("no-new-privileges=true", call.args)
            self.assertNotIn("docker.sock", " ".join(call.args))
            policy = json.loads(call.kwargs["input_data"])
            self.assertTrue(policy["enforceAvailableModels"])
            self.assertEqual("fable" in policy["availableModels"], allowed)
            self.assertEqual(policy["deniedModels"], [] if allowed else ["fable"])
            self.assertEqual(self.broker.fable_allowed, allowed)
            self.assertTrue(self.broker._model_policy_ready)

    async def test_policy_volume_wrong_owner_or_role_is_never_written(self):
        self.broker._remove_model_policy_writer = AsyncMock()
        for labels in ({}, {OWNER_LABEL: "other", RESOURCE_LABEL: "model-policy"},
                       {OWNER_LABEL: self.broker.owner, RESOURCE_LABEL: "other"}):
            self.broker.command = AsyncMock(return_value=(0, json.dumps([{"Labels": labels}]).encode(), b""))
            with self.subTest(labels=labels), self.assertRaises(BrokerError):
                await self.broker.apply_model_policy(True)
            self.assertEqual(self.broker.command.await_count, 1)
            self.assertFalse(self.broker.fable_allowed)
            self.assertFalse(self.broker._model_policy_ready)

    async def test_stale_policy_writer_is_owned_and_reaped_before_replacement(self):
        writer_name = self.broker.model_policy_volume + "-writer"
        labels = {OWNER_LABEL: self.broker.owner, RESOURCE_LABEL: "model-policy-writer"}
        self.broker.command = AsyncMock(return_value=(0, json.dumps([{"Config": {"Labels": labels}}]).encode(), b""))
        await self.broker._remove_model_policy_writer()
        self.assertEqual(self.broker.command.call_args.args, ("container", "rm", "--force", writer_name))
        labels[OWNER_LABEL] = "other"
        self.broker.command = AsyncMock(return_value=(0, json.dumps([{"Config": {"Labels": labels}}]).encode(), b""))
        with self.assertRaises(BrokerError):
            await self.broker._remove_model_policy_writer()
        self.broker.command.assert_awaited_once()

    async def test_writer_timeout_reaps_container_and_leaves_policy_not_ready(self):
        self.broker._remove_model_policy_writer = AsyncMock()
        self.broker._inspect_model_policy_volume = AsyncMock(return_value={"Labels": {}})
        self.broker.command = AsyncMock(side_effect=BrokerError("timeout", 503))
        with self.assertRaises(BrokerError):
            await self.broker.apply_model_policy(True)
        self.assertEqual(self.broker._remove_model_policy_writer.await_count, 2)
        self.assertFalse(self.broker.fable_allowed)
        self.assertFalse(self.broker._model_policy_ready)

    async def test_failed_revoke_stays_fail_closed_and_invalidates_ready_cache(self):
        self.broker.fable_allowed = True
        self.broker._model_policy_ready = True
        self.broker._write_model_policy = AsyncMock(side_effect=BrokerError("unavailable", 503))
        with self.assertRaises(BrokerError):
            await self.broker.apply_model_policy(False)
        self.assertFalse(self.broker.fable_allowed)
        self.assertFalse(self.broker._model_policy_ready)
        self.broker.workspace = AsyncMock()
        with self.assertRaises(BrokerError) as raised:
            await self.broker.chat_command(self.workspace_id, None, "unused", "fable")
        self.assertEqual(raised.exception.status, 403)
        self.broker.workspace.assert_not_awaited()

    async def test_start_reuses_policy_without_rewriting_on_every_workspace(self):
        self.broker._write_model_policy = AsyncMock()
        await self.broker._ensure_model_policy()
        await self.broker._ensure_model_policy()
        self.broker._write_model_policy.assert_awaited_once_with(False)

    def legacy_worker(self, *, running=False):
        names, resources = self.existing_worker(running=running, attached=True)
        worker = resources[("container", names["container"])]
        worker["Image"] = "sha256:" + "1" * 64
        worker["Mounts"] = [
            {"Type": "volume", "Name": names["files"], "Destination": "/workspace", "RW": True},
            {"Type": "volume", "Name": names["home"], "Destination": "/home/claude", "RW": True},
        ]
        for key in ("files", "home"):
            resources[("volume", names[key])] = self.owned("volume")
        original = self.broker.command.side_effect

        async def docker(*args, **kwargs):
            if args[0] == "exec" and args[-1] == "--version":
                return 0, b"2.1.311 (Claude Code)\n", b""
            return await original(*args, **kwargs)

        self.broker.command.side_effect = docker
        return names, resources

    async def test_legacy_upgrade_preserves_verified_data_and_exact_image(self):
        names, _ = self.legacy_worker()
        await self.broker._start(self.workspace_id)
        calls = [call.args for call in self.broker.command.call_args_list]
        remove = ("container", "rm", names["container"])
        create = next(args for args in calls if args[:2] == ("container", "create"))
        self.assertEqual(create[-1], "sha256:" + "1" * 64)
        self.assertIn(f"type=volume,src={names['files']},dst=/workspace", create)
        self.assertIn(f"type=volume,src={names['home']},dst=/home/claude", create)
        self.assertIn(f"type=volume,src={self.broker.model_policy_volume},dst=/etc/claude-code,readonly,volume-nocopy", create)
        self.assertLess(calls.index(remove), calls.index(create))
        self.assertFalse(any(args[:2] in (("volume", "rm"), ("volume", "create"), ("network", "rm")) for args in calls))

    async def test_changed_resource_limits_recreate_only_stopped_container(self):
        names, resources = self.legacy_worker()
        worker = resources[("container", names["container"])]
        worker["Mounts"].extend([
            {"Type": "volume", "Name": self.broker.model_policy_volume, "Destination": "/etc/claude-code", "RW": False},
            {"Type": "volume", "Name": self.broker.shared_auth_volume, "Destination": AUTH_MOUNT, "RW": False},
        ])
        worker["HostConfig"] = {"Memory": 4 * 1024 ** 3, "MemorySwap": 4 * 1024 ** 3, "NanoCpus": 2_000_000_000}
        await self.broker._start(self.workspace_id)
        calls = [call.args for call in self.broker.command.call_args_list]
        create = next(args for args in calls if args[:2] == ("container", "create"))
        self.assertEqual(create[create.index("--memory") + 1], "3g")
        self.assertEqual(create[create.index("--memory-swap") + 1], "3g")
        self.assertEqual(create[create.index("--cpus") + 1], "1.5")
        self.assertEqual(create[-1], worker["Image"])
        self.assertIn(f"type=volume,src={names['files']},dst=/workspace", create)
        self.assertIn(f"type=volume,src={names['home']},dst=/home/claude", create)
        self.assertFalse(any(args[:2] in (("volume", "rm"), ("volume", "create"), ("network", "rm")) for args in calls))

    async def test_changed_resource_limits_do_not_replace_running_container(self):
        names, resources = self.existing_worker(running=True)
        resources[("container", names["container"])]["HostConfig"]["Memory"] = 4 * 1024 ** 3
        with self.assertRaisesRegex(BrokerError, "请先停止"):
            await self.broker._start(self.workspace_id)
        self.assertTrue(all(call.args[1] == "inspect" for call in self.broker.command.call_args_list))

    async def test_resource_limit_comparison_accepts_equivalent_units(self):
        self.assertEqual(self.broker.resource_limits(), (3 * 1024 ** 3, 1_500_000_000))
        self.broker.config.memory = "1.5g"
        self.broker.config.cpus = ".5"
        self.assertEqual(self.broker.resource_limits(), (1536 * 1024 ** 2, 500_000_000))
        for memory, cpus in (("4", "1"), ("not-memory", "1"), ("1g", "NaN"), ("1g", "-1")):
            self.broker.config.memory, self.broker.config.cpus = memory, cpus
            with self.subTest(memory=memory, cpus=cpus), self.assertRaises(BrokerError):
                self.broker.resource_limits()

    async def test_legacy_upgrade_never_removes_running_or_untrusted_workers(self):
        for issue in ("running", "wrong-volume", "wrong-owner", "missing-volume", "bad-image"):
            names, resources = self.legacy_worker(running=issue == "running")
            worker = resources[("container", names["container"])]
            if issue == "wrong-volume":
                worker["Mounts"][0]["Name"] = "another-user-files"
            if issue == "wrong-owner":
                resources[("volume", names["home"])]["Labels"][OWNER_LABEL] = "someone-else"
            if issue == "missing-volume":
                del resources[("volume", names["files"])]
            if issue == "bad-image":
                worker["Image"] = "--privileged"
            with self.subTest(issue=issue), self.assertRaises(BrokerError):
                await self.broker._start(self.workspace_id)
            self.assertTrue(all(call.args[1] == "inspect" for call in self.broker.command.call_args_list))

    async def test_writable_or_foreign_policy_mount_never_starts_worker(self):
        for mutation in ({"RW": True}, {"Name": "foreign-policy"}, {"Type": "bind"}):
            names, resources = self.existing_worker()
            resources[("container", names["container"])]["Mounts"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(BrokerError):
                await self.broker._start(self.workspace_id)
            self.assertEqual(self.broker.command.await_count, 1)

    async def test_policy_version_gate_stops_unknown_or_older_cli(self):
        for version in (b"2.1.282", b"1.0.0", b"not-a-version"):
            self.broker.command = AsyncMock(return_value=(0, version, b""))
            with self.subTest(version=version), self.assertRaises(BrokerError):
                await self.broker._check_model_policy_support(self.workspace_id)
            self.assertEqual(self.broker.command.call_args.args[:2], ("container", "stop"))
            self.assertNotIn(self.broker.names(self.workspace_id)["container"], self.broker._policy_checked_workers)
        self.broker.command = AsyncMock(return_value=(0, b"2.1.283 (Claude Code)", b""))
        await self.broker._check_model_policy_support(self.workspace_id)
        await self.broker._check_model_policy_support(self.workspace_id)
        self.broker.command.assert_awaited_once()

    async def test_admin_auth_environment_is_not_a_user_workspace(self):
        facade = self.broker.admin_auth()
        self.assertIs(facade, self.broker.admin_auth())
        self.assertEqual(facade.workspace_id, ADMIN_AUTH_ID)
        with self.assertRaises(BrokerError):
            self.broker.get(ADMIN_AUTH_ID)
        with self.assertRaises(BrokerError):
            facade.get(self.workspace_id)
        self.assertEqual(self.broker.db.execute("SELECT count(*) FROM workspaces").fetchone()[0], 1)
        facade.workspace = AsyncMock(return_value={"running": True})
        facade.start = AsyncMock()
        command = await facade.terminal_command(ADMIN_AUTH_ID)
        facade.start.assert_awaited_once_with(ADMIN_AUTH_ID)
        self.assertEqual(command[-2:], ["/usr/local/bin/claude", "setup-token"])
        self.assertNotIn(self.broker.names(self.workspace_id)["home"], command)

    async def test_admin_auth_container_has_own_home_network_and_lower_resource_limit(self):
        async def docker(*args, **kwargs):
            if args[0] == "exec" and args[-1] == "--version":
                return 0, b"2.1.311 (Claude Code)", b""
            if args[:2] == ("container", "inspect") and args[2] == self.broker.config.egress_container:
                return 0, json.dumps([{"State": {"Running": True}, "NetworkSettings": {"Networks": {}}}]).encode(), b""
            if args[1] == "inspect" and args[0] != "image":
                return 1, b"", b"No such object"
            return 0, b"[]", b""

        self.broker.command = AsyncMock(side_effect=docker)
        self.broker._model_policy_ready = True
        facade = self.broker.admin_auth()
        await facade._start(ADMIN_AUTH_ID)
        create = next(call.args for call in self.broker.command.call_args_list if call.args[:2] == ("container", "create"))
        self.assertEqual(create[create.index("--memory") + 1], "512m")
        self.assertEqual(create[create.index("--cpus") + 1], "1")
        self.assertEqual(create[create.index("--network") + 1], facade.names(ADMIN_AUTH_ID)["network"])
        self.assertIn(f"type=volume,src={facade.names(ADMIN_AUTH_ID)['home']},dst=/home/claude", create)
        self.assertIn(f"{RESOURCE_LABEL}=admin-auth", create)
        self.assertFalse(any(self.broker.shared_auth_volume in arg for arg in create))
        self.assertFalse(any(self.broker.names(self.workspace_id)["home"] in arg for arg in create))

    async def test_admin_auth_requires_its_private_resource_role(self):
        self.broker.command = AsyncMock(return_value=(0, json.dumps([{"Config": {"Labels": {
            OWNER_LABEL: self.broker.owner, WORKSPACE_LABEL: ADMIN_AUTH_ID,
        }}}]).encode(), b""))
        facade = self.broker.admin_auth()
        with self.assertRaises(BrokerError):
            await facade.inspect_owned("container", facade.names(ADMIN_AUTH_ID)["container"], ADMIN_AUTH_ID)
        self.broker.command.assert_awaited_once()

    async def test_shared_auth_save_never_places_token_in_docker_arguments_or_env(self):
        token = "sk-ant-oat01-" + "x" * 80

        async def docker(*args, **kwargs):
            if args[1] == "inspect":
                return 1, b"", b"No such object"
            if args[0] == "run":
                return 0, b'{"authenticated":true}', b""
            return 0, b"", b""

        self.broker.command = AsyncMock(side_effect=docker)
        result = await self.broker.save_shared_auth(token)
        self.assertEqual(result, {"authenticated": True, "configured": True})
        run = next(call for call in self.broker.command.call_args_list if call.args[0] == "run")
        self.assertNotIn(token, " ".join(run.args))
        self.assertNotIn("--env", run.args)
        self.assertEqual(run.args[run.args.index("--network") + 1], "none")
        mounts = [run.args[i + 1] for i, arg in enumerate(run.args[:-1]) if arg == "--mount"]
        self.assertEqual(mounts, [f"type=volume,src={self.broker.shared_auth_volume},dst=/auth,volume-nocopy"])
        self.assertEqual(json.loads(run.kwargs["input_data"]), {"operation": "save", "token": token})
        self.assertNotIn(token, json.dumps(result))

    async def test_invalid_shared_token_never_reaches_docker(self):
        self.broker.command = AsyncMock()
        for token in (None, "", "authorization-code", "https://claude.ai/oauth/authorize",
                      "sk-ant-oat01-" + "x" * 80 + "\n", "sk-ant-oat01-" + "x" * 8001):
            with self.subTest(token_type=type(token).__name__), self.assertRaises(BrokerError):
                await self.broker.save_shared_auth(token)
        self.broker.command.assert_not_called()

    async def test_shared_auth_volume_foreign_labels_block_reads_and_writes(self):
        self.broker.command = AsyncMock(return_value=(0, json.dumps([{"Labels": {
            OWNER_LABEL: "someone-else", RESOURCE_LABEL: "shared-auth",
        }}]).encode(), b""))
        with self.assertRaises(BrokerError):
            await self.broker._ensure_shared_auth_volume()
        self.broker.command.assert_awaited_once()

    async def test_shared_auth_status_caches_only_boolean_and_clear_resets_it(self):
        self.broker._remove_owned_helper = AsyncMock()
        self.broker._ensure_shared_auth_volume = AsyncMock()

        async def docker(*args, **kwargs):
            operation = json.loads(kwargs["input_data"])["operation"]
            return 0, json.dumps({"authenticated": operation != "clear"}).encode(), b""

        self.broker.command = AsyncMock(side_effect=docker)
        self.assertTrue((await self.broker.shared_auth_status())["authenticated"])
        self.assertTrue((await self.broker.shared_auth_status())["authenticated"])
        self.broker.command.assert_awaited_once()
        self.assertFalse((await self.broker.clear_shared_auth())["authenticated"])
        self.assertFalse((await self.broker.shared_auth_status())["authenticated"])
        self.assertEqual(self.broker.command.await_count, 2)

    async def test_worker_missing_shared_authorization_cannot_launch_cli(self):
        self.broker._shared_auth_state = False
        self.broker.workspace = AsyncMock(return_value={"running": True})
        with self.assertRaises(BrokerError) as raised:
            await self.broker.chat_command(self.workspace_id, None, "c407d23c-9e9e-4c0a-a025-0824cf5dd812", "sonnet")
        self.assertEqual(raised.exception.status, 503)
        self.broker.workspace.assert_not_awaited()
        self.broker._shared_auth_state = True
        command = await self.broker.chat_command(self.workspace_id, None, "c407d23c-9e9e-4c0a-a025-0824cf5dd812", "sonnet")
        self.assertIn(LAUNCH_WITH_SHARED_AUTH, command)
        self.assertNotIn("--env", command)
        self.assertIn("--restricted", command)
        self.assertIn("--safe-mode", command)
        self.assertEqual(command[command.index("--tools") + 1], "Bash,Read,Write,Edit,Glob,Grep")

    async def test_writable_or_foreign_shared_auth_mount_is_rejected(self):
        for mutation in ({"RW": True}, {"Name": "another-account"}, {"Type": "bind"}):
            names, resources = self.existing_worker()
            resources[("container", names["container"])]["Mounts"][1].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(BrokerError):
                await self.broker._start(self.workspace_id)
            self.broker.command.assert_awaited_once()

    def test_fixed_helpers_compile_without_secrets_or_workspace_path_interpolation(self):
        compile(AUTH_HELPER, "shared-auth-helper", "exec")
        compile(LAUNCH_WITH_SHARED_AUTH, "shared-auth-launcher", "exec")


if __name__ == "__main__":
    unittest.main()

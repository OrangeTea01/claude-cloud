"""The standalone diagnostic never touches a live collector or refresh grant."""
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.docker_backend import Config, OWNER_LABEL, RESOURCE_LABEL, WORKSPACE_LABEL
from backend.official_usage import UsageUnavailable
from scripts.diagnose_official_usage import (ADMIN_USAGE_ID, NONCE_LABEL, ROLE, PROBE_HELPER,
                                             DiagnosticError, ReadOnlyBroker, main, probe, project_usage)


class ProjectionTests(unittest.TestCase):
    def test_preserves_missing_null_old_and_new_windows_without_exposing_other_fields(self):
        result = project_usage({"rate_limits_available": True, "email": "private@example.test",
                                "rate_limits": {"five_hour": None, "seven_day": {"utilization": 6,
                                "resets_at": "2026-11-20T12:00:00Z", "accessToken": "private-token"}}})
        self.assertEqual(result["five_hour"], {"shape": "null"})
        self.assertEqual(result["seven_day"]["utilization"], 6)
        self.assertEqual(result["rowsShape"], "missing")
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(project_usage({"rate_limits": {}})["five_hour"]["shape"], "missing")

    def test_sdk_rows_have_only_global_kinds_and_safe_meter_values(self):
        result = project_usage({"rate_limits": {"limits": [
            {"kind": "session", "percent": 61, "resets_at": "2026-11-17T15:20:00Z", "email": "private"},
            {"kind": "weekly_all", "percent": 6, "scope": {"private": "secret"}},
            {"kind": "weekly_scoped", "percent": 10},
        ]}})
        self.assertEqual([row["kind"] for row in result["rows"]], ["session", "weekly_all"])
        self.assertTrue(result["rows"][1]["scopePresent"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_zero_with_null_reset_remains_distinct_from_missing_or_null_meter(self):
        result = project_usage({"rate_limits_available": False, "rate_limits": {
            "five_hour": {"utilization": 0, "resets_at": None}, "seven_day": None}})
        self.assertEqual(result["five_hour"], {"shape": "object", "utilization": 0, "resets_at": None})
        self.assertEqual(result["seven_day"], {"shape": "null"})
        self.assertFalse(result["available"])
        self.assertEqual(project_usage({"rate_limits": {}})["five_hour"], {"shape": "missing"})

    def test_secret_shaped_timestamps_and_invalid_percent_are_not_reflected(self):
        result = project_usage({"rate_limits": {"five_hour": {"resetAt": "https://private/secret",
                                                               "percent": "sk-ant-private"}}})
        self.assertIsNone(result["five_hour"]["resetAt"])
        self.assertEqual(result["five_hour"]["resetAtShape"], "invalid")
        self.assertNotIn("private", json.dumps(result))

    def test_read_only_owner_lookup_does_not_migrate_existing_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "registry.sqlite3")
            database = sqlite3.connect(path)
            database.execute("CREATE TABLE meta (key TEXT,value TEXT)")
            database.execute("INSERT INTO meta VALUES ('owner','abcdef123456')")
            database.commit()
            database.close()
            before = path.read_bytes()
            reader = ReadOnlyBroker(Config(data_dir=directory))
            self.assertEqual(reader.owner, "abcdef123456")
            self.assertEqual(path.read_bytes(), before)
            database = sqlite3.connect(path)
            self.assertEqual(database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("meta",)])
            database.close()


class HelperTests(unittest.TestCase):
    def execute(self, *, expires=9999999999999, scopes=None):
        token, refresh = "sk-ant-oat01-" + "a" * 80, "must-not-copy-refresh"
        value = {"claudeAiOauth": {"accessToken": token, "refreshToken": refresh, "expiresAt": expires,
                                  "scopes": ["user:profile", "user:inference"] if scopes is None else scopes}}
        frames = b"".join((json.dumps({"type": "control_request", "request_id": kind,
                                       "request": {"subtype": kind}}) + "\n").encode()
                          for kind in ("initialize", "get_usage"))
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as source:
            json.dump(value, source)
            source.flush()
            os.lseek(source.fileno(), 0, os.SEEK_SET)
            output = io.StringIO()
            with patch("os.open", side_effect=lambda *args, **kwargs: os.dup(source.fileno())), \
                    patch.object(os, "O_NOFOLLOW", 0, create=True), patch.object(os, "O_DIRECTORY", 0, create=True), \
                    patch("os.makedirs"), patch("os.execve", side_effect=SystemExit(0)) as execute, \
                    patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(frames))), patch("sys.stdout", output):
                with self.assertRaises(SystemExit):
                    exec(PROBE_HELPER, {})
            return execute, output.getvalue(), token, refresh

    def test_real_grants_access_only_and_empty_private_home(self):
        execute, output, token, refresh = self.execute()
        environment = execute.call_args.args[2]
        self.assertEqual(environment["CLAUDE_CODE_OAUTH_TOKEN"], token)
        self.assertEqual(environment["CLAUDE_CODE_OAUTH_SCOPES"], "user:profile user:inference")
        self.assertEqual(environment["HOME"], "/tmp/usage-home")
        self.assertNotIn(refresh, json.dumps(environment))
        self.assertFalse(any("REFRESH" in key for key in environment))
        self.assertEqual(output, "")

    def test_expired_grant_never_launches_or_refreshes_and_reports_safe_code(self):
        execute, output, token, refresh = self.execute(expires=1)
        execute.assert_not_called()
        response = json.loads(output.splitlines()[-1])["response"]["response"]
        self.assertEqual(response, {"diagnosticError": "AUTH_EXPIRED", "scopePresent": True})
        self.assertNotIn(token, output)
        self.assertNotIn(refresh, output)

    def test_missing_profile_is_not_forged(self):
        execute, output, _, _ = self.execute(scopes=["user:inference"])
        execute.assert_not_called()
        self.assertIn('"diagnosticError": "AUTH_SCOPE_MISSING"', output)
        self.assertIn('"scopePresent": false', output)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = "abcdef123456"
        self.prefix = f"cc-{self.owner}-{ADMIN_USAGE_ID}"
        self.network_id, self.image_id = "a" * 64, "sha256:" + "b" * 64
        labels = {OWNER_LABEL: self.owner, WORKSPACE_LABEL: ADMIN_USAGE_ID, RESOURCE_LABEL: ADMIN_USAGE_ID}
        self.resources = {
            ("volume", self.prefix + "-home"): {"Name": self.prefix + "-home", "Labels": labels},
            ("network", self.prefix + "-net"): {"Id": self.network_id, "Labels": labels, "Internal": True,
                                                  "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}},
            ("container", self.prefix): {"Image": self.image_id, "Config": {"Labels": labels},
                                         "Mounts": [{"Type": "volume", "Name": self.prefix + "-home", "Destination": "/home/claude"}]},
            ("image", self.image_id): {"Id": self.image_id, "Config": {}},
            ("container", "claude-cloud-egress"): {"State": {"Running": True}, "NetworkSettings": {"Networks": {
                self.prefix + "-net": {"NetworkID": self.network_id, "Aliases": ["egress"]}}}},
        }
        self.calls = []
        self.tamper = False
        self.parent = SimpleNamespace(owner=self.owner, config=Config(), command=self.command)

    async def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[1] == "inspect":
            item = self.resources.get((args[0], args[2]))
            return (0, json.dumps([item]).encode(), b"") if item else (1, b"", b"no such object")
        if args[:2] == ("container", "create"):
            name = args[args.index("--name") + 1]
            labels = dict(args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "--label")
            if self.tamper:
                labels[RESOURCE_LABEL] = "another-role"
            self.resources[("container", name)] = {"Id": "c" * 64, "Name": "/" + name, "Config": {"Labels": labels}}
        return 0, b"", b""

    async def test_probe_only_creates_and_removes_unique_ephemeral_container(self):
        with patch("scripts.diagnose_official_usage.fetch_usage_command", AsyncMock(return_value={"rate_limits": {"five_hour": None}})) as fetch:
            result = await probe(self.parent)
        self.assertEqual(result["five_hour"]["shape"], "null")
        create = next(call for call in self.calls if call[:2] == ("container", "create"))
        self.assertIn("--rm", create)
        self.assertIn("--read-only", create)
        self.assertEqual(create.count("--mount"), 1)
        self.assertIn("dst=/run/usage-source,readonly,volume-nocopy", create[create.index("--mount") + 1])
        self.assertEqual(create[create.index("--network") + 1], self.network_id)
        self.assertNotIn("-p", create)
        self.assertNotIn("--publish", create)
        self.assertFalse(any(call[:2] in (("volume", "create"), ("network", "create"), ("container", "stop")) for call in self.calls))
        self.assertFalse(any(call[:2] == ("container", "start") and call[2] == self.prefix for call in self.calls))
        self.assertIn(("container", "rm", "--force", "c" * 64), self.calls)
        self.assertIn(PROBE_HELPER, fetch.call_args.args[0])

    async def test_query_failure_still_removes_only_verified_ephemeral_container(self):
        with patch("scripts.diagnose_official_usage.fetch_usage_command", AsyncMock(side_effect=RuntimeError("private"))):
            with self.assertRaises(RuntimeError):
                await probe(self.parent)
        self.assertIn(("container", "rm", "--force", "c" * 64), self.calls)

    async def test_cleanup_refuses_mismatched_role(self):
        self.tamper = True
        with patch("scripts.diagnose_official_usage.fetch_usage_command", AsyncMock(return_value={})):
            with self.assertRaises(DiagnosticError) as error:
                await probe(self.parent)
        self.assertEqual(error.exception.code, "CLEANUP_FAILED")
        self.assertFalse(any(call[:2] == ("container", "rm") for call in self.calls))

    async def test_unowned_resource_fails_before_creating_any_container(self):
        self.resources[("volume", self.prefix + "-home")]["Labels"] = {OWNER_LABEL: "someone-else"}
        with self.assertRaises(Exception):
            await probe(self.parent)
        self.assertFalse(any(call[:2] == ("container", "create") for call in self.calls))

    async def test_main_hides_exception_text(self):
        output = io.StringIO()
        with patch("scripts.diagnose_official_usage.ReadOnlyBroker", return_value=self.parent), \
                patch("scripts.diagnose_official_usage.probe", AsyncMock(side_effect=RuntimeError("private token"))), \
                patch("sys.stdout", output):
            self.assertEqual(await main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "code": "QUERY_FAILED"})

    async def test_main_preserves_safe_timeout_code_from_existing_reader(self):
        output = io.StringIO()
        with patch("scripts.diagnose_official_usage.ReadOnlyBroker", return_value=self.parent), \
                patch("scripts.diagnose_official_usage.probe", AsyncMock(side_effect=UsageUnavailable(code="query_timeout"))), \
                patch("sys.stdout", output):
            self.assertEqual(await main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "code": "QUERY_TIMEOUT"})

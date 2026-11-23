"""Automatic percentage allocation through authenticated routes and auth rotation."""
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.admin_claude import AdminClaude
from test_multiuser_api import ORIGIN, LEGACY
import test_multiuser_api as fixtures


PATH = "/api/admin/quotas/automatic"
CONFIRMATION = "CHANGE_AUTOMATIC_QUOTA"


class AutomaticUsageApiTests(unittest.TestCase):
    login = fixtures.MultiuserApiTests.login
    admin = fixtures.MultiuserApiTests.admin
    mutate = fixtures.MultiuserApiTests.mutate
    add_user = fixtures.MultiuserApiTests.add_user
    create_workspace = fixtures.MultiuserApiTests.create_workspace

    def setUp(self):
        self.maintenance = patch("backend.app.UsageControl.start")
        self.maintenance.start()
        fixtures.MultiuserApiTests.setUp(self)
        self.now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        self.five = self.now + timedelta(hours=5)
        self.week = self.now + timedelta(days=7)
        self.runtime["quotas"].clock = lambda: self.now
        self.runtime["official"].clock = lambda: self.now.timestamp()

    def tearDown(self):
        try:
            fixtures.MultiuserApiTests.tearDown(self)
        finally:
            self.maintenance.stop()

    def configure(self, ids, enabled=True):
        return self.mutate("PUT", PATH, {"enabled": enabled, "userIds": ids, "confirmation": CONFIRMATION}, admin=True)

    def settings(self):
        return self.client.get("/api/admin/quotas").json()["settings"]

    def publish(self, five=0, weekly=0, *, settle=False):
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": self.five.timestamp()},
            "seven_day": {"used_percentage": weekly, "resets_at": self.week.timestamp()},
        }}
        def apply():
            self.runtime["official"].ingest(payload)
            if settle:
                self.runtime["shares"].settle(self.runtime["official"].snapshot())
        self.client.portal.call(apply)

    def charge(self, user_id, tokens=10, event="task-one"):
        self.client.portal.call(lambda: self.runtime["quotas"].charge(user_id, event, tokens))

    def user_quota(self, user_id):
        response = self.client.get("/api/admin/quotas")
        self.assertEqual(response.status_code, 200, response.text)
        return next(user["quota"] for user in response.json()["users"] if user["id"] == user_id)

    def test_automatic_mutation_requires_admin_cookie_exact_origin_and_admin_csrf(self):
        body = {"enabled": True, "userIds": ["a" * 32], "confirmation": CONFIRMATION}
        self.assertEqual(self.client.put(PATH, json=body).status_code, 401)
        owner = self.login()["user"]
        body["userIds"] = [owner["id"]]
        self.assertEqual(self.mutate("PUT", PATH, body).status_code, 401)
        self.admin()
        for headers in ({}, {"Origin": ORIGIN}, {"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
                        {"Origin": "https://evil.example", "X-CSRF-Token": self.admin_csrf}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.put(PATH, json=body, headers=headers).status_code, 403)
        self.assertEqual(self.settings()["mode"], "tokens")
        self.assertEqual(self.settings()["userIds"], [])
        self.assertEqual(self.configure([owner["id"]]).status_code, 200)
        self.broker.chat_command.assert_not_called()

    def test_strict_request_fields_reject_coercion_unknown_fields_and_wrong_confirmation(self):
        owner = self.login()["user"]
        self.admin()
        valid = {"enabled": True, "userIds": [owner["id"]], "confirmation": CONFIRMATION}
        invalid = [
            {**valid, "enabled": "true"}, {**valid, "enabled": 1}, {**valid, "enabled": None},
            {**valid, "userIds": "all"}, {**valid, "userIds": [True]}, {**valid, "userIds": [12]},
            {**valid, "userIds": [owner["id"]] * 1001}, {**valid, "password": "private-fixture-input"},
            {key: value for key, value in valid.items() if key != "confirmation"},
        ]
        for body in invalid:
            with self.subTest(body_type=str({key: type(value).__name__ for key, value in body.items()})):
                response = self.mutate("PUT", PATH, body, admin=True)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("private-fixture-input", response.text)
        response = self.mutate("PUT", PATH, {**valid, "confirmation": "wrong"}, admin=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.settings()["mode"], "tokens")

    def test_selection_rejects_empty_duplicate_malformed_missing_and_deleting_users(self):
        owner = self.login()["user"]
        alice = self.add_user()
        for ids in ([], [owner["id"], owner["id"]], ["not-an-id"], ["../owner"]):
            with self.subTest(ids=ids):
                self.assertEqual(self.configure(ids).status_code, 400)
        self.assertEqual(self.configure(["f" * 32]).status_code, 409)
        self.client.portal.call(lambda: self.runtime["credentials"].begin_user_deletion(alice["id"]))
        self.assertEqual(self.configure([alice["id"]]).status_code, 409)
        self.assertEqual(self.settings()["mode"], "tokens")

    def test_unknown_official_windows_allow_setup_but_block_model_tasks_and_keep_workspace_start_available(self):
        owner = self.login()["user"]
        self.admin()
        response = self.configure([owner["id"]])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["settings"]["mode"], "official_share")
        quota = self.client.get("/api/account/usage").json()["quota"]
        self.assertEqual(quota["unit"], "percent")
        self.assertTrue(quota["exhausted"])
        self.assertIn("同步", quota["blockedReason"])
        self.assertIsNone(quota["fiveHour"]["limit"])
        self.assertIsNone(quota["weekly"]["limit"])
        self.assertEqual(self.mutate("POST", f"/api/workspaces/{LEGACY}/start").status_code, 200)
        response = self.mutate("POST", f"/api/workspaces/{LEGACY}/chat/message", {"text": "Do a task"})
        self.assertEqual(response.status_code, 429, response.text)
        self.broker.chat_command.assert_not_called()

    def test_explicit_members_split_remaining_pool_without_implicitly_adding_owner(self):
        owner = self.login()["user"]
        users = [self.add_user(name) for name in ("alice", "bob", "carol", "david", "erica")]
        self.publish(40, 50)
        response = self.configure([user["id"] for user in users])
        self.assertEqual(response.status_code, 200, response.text)
        for user in users:
            quota = self.user_quota(user["id"])
            self.assertEqual(quota["fiveHour"]["limit"], 12)
            self.assertEqual(quota["weekly"]["limit"], 10)
        excluded = self.user_quota(owner["id"])
        self.assertEqual(excluded["fiveHour"]["limit"], 0)
        self.assertTrue(excluded["exhausted"])
        self.assertIn("未列入", excluded["blockedReason"])
        self.assertNotIn(owner["id"], self.settings()["userIds"])

    def test_manual_quota_mutations_are_conflicts_while_automatic_mode_is_enabled(self):
        owner = self.login()["user"]
        self.admin()
        manual = {"fiveHourLimit": 100, "weeklyLimit": 500}
        path = f"/api/admin/users/{owner['id']}/quota"
        self.assertEqual(self.mutate("PUT", path, manual, admin=True).status_code, 200)
        self.assertEqual(self.configure([owner["id"]]).status_code, 200)
        self.assertEqual(self.mutate("PUT", path, {"fiveHourLimit": 1, "weeklyLimit": 1}, admin=True).status_code, 409)
        self.assertEqual(self.mutate("POST", "/api/admin/quotas/distribute", {
            "fiveHourTotal": 1, "weeklyTotal": 1, "confirmation": "DISTRIBUTE_QUOTA"}, admin=True).status_code, 409)
        saved = self.runtime["quotas"].snapshot(owner["id"])
        self.assertEqual(saved["fiveHour"]["limit"], 100)
        self.assertEqual(saved["weekly"]["limit"], 500)

    def test_running_permission_wait_and_pending_stop_tasks_prevent_policy_changes(self):
        owner = self.login()["user"]
        self.admin()
        def phase(value):
            self.runtime["chats"].get(LEGACY).phase = value
        for value in ("thinking", "starting", "awaiting_permission"):
            self.client.portal.call(phase, value)
            response = self.configure([owner["id"]])
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("等待授权", response.text)
            self.assertEqual(self.settings()["mode"], "tokens")
        self.client.portal.call(phase, "idle")
        self.runtime["chats"].quota_stop_pending.add(LEGACY)
        self.assertEqual(self.configure([owner["id"]]).status_code, 409)
        self.runtime["chats"].quota_stop_pending.clear()
        self.assertEqual(self.configure([owner["id"]]).status_code, 200)

    def test_same_member_list_disable_reenable_and_repeat_never_refund_current_usage(self):
        owner = self.login()["user"]
        alice = self.add_user()
        ids = [owner["id"], alice["id"]]
        self.publish()
        self.assertEqual(self.configure(ids).status_code, 200)
        self.charge(owner["id"])
        self.now += timedelta(seconds=1)
        self.publish(10, 20, settle=True)
        before = self.user_quota(owner["id"])
        self.assertEqual(before["fiveHour"]["used"], 10)
        self.assertEqual(before["weekly"]["used"], 20)
        # This is the actual UI contract for turning the mode off.
        response = self.configure([], enabled=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings()["mode"], "tokens")
        self.assertEqual(set(self.settings()["userIds"]), set(ids))
        self.assertEqual(self.configure(ids).status_code, 200)
        self.assertEqual(self.user_quota(owner["id"]), before)
        self.assertEqual(self.configure(ids).status_code, 200)
        self.assertEqual(self.user_quota(owner["id"]), before)

    def test_membership_changes_wait_for_both_current_windows(self):
        owner = self.login()["user"]
        alice = self.add_user()
        self.publish()
        self.assertEqual(self.configure([owner["id"], alice["id"]]).status_code, 200)
        self.assertEqual(self.configure([owner["id"]]).status_code, 409)
        self.now = self.five + timedelta(seconds=1)
        self.assertEqual(self.configure([owner["id"]]).status_code, 409)
        self.now = self.week + timedelta(seconds=1)
        response = self.configure([owner["id"]])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.user_quota(owner["id"])["fiveHour"]["limit"])

    def test_deleting_participant_does_not_refund_its_share_to_other_accounts(self):
        owner = self.login()["user"]
        alice, bob = self.add_user(), self.add_user("bob")
        self.publish()
        self.assertEqual(self.configure([owner["id"], alice["id"], bob["id"]]).status_code, 200)
        self.charge(alice["id"])
        self.now += timedelta(seconds=1)
        self.publish(6, 6, settle=True)
        before = {user_id: self.user_quota(user_id) for user_id in (owner["id"], bob["id"])}
        response = self.mutate("POST", f"/api/admin/users/{alice['id']}/delete", {"confirmation": "DELETE_USER_AND_DATA"}, admin=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])
        self.assertNotIn(alice["id"], self.settings()["userIds"])
        for user_id, quota in before.items():
            self.assertEqual(self.user_quota(user_id), quota)
        self.assertLess(sum(self.user_quota(user_id)["fiveHour"]["limit"] for user_id in before), 67)

    def test_new_shared_authorization_invalidates_previous_official_windows_and_automatic_balances(self):
        owner = self.login()["user"]
        self.admin()
        self.publish(40, 40)
        self.assertEqual(self.configure([owner["id"]]).status_code, 200)
        self.assertEqual(self.user_quota(owner["id"])["fiveHour"]["limit"], 60)
        before_generation = self.settings()["accountGeneration"]
        self.broker.save_shared_auth = AsyncMock()
        async def capture_new_authorization():
            service = object.__new__(AdminClaude)
            service.runtime = self.runtime
            service.admin_sessions = self.app.state.admin_sessions
            service.terminals = self.runtime["terminals"]
            service.owner = next(iter(self.app.state.admin_sessions.sessions))
            service.error = None
            service.importing = False
            watch = asyncio.get_running_loop().create_future()
            watch.set_result(None)
            terminal = SimpleNamespace(watch=watch, proc=SimpleNamespace(returncode=0), output_overflow=False,
                                       private_output="sk-ant-oat01-" + "fake-fixture-only-" * 8, replay=deque(), replay_size=0)
            await service._capture_token(terminal, service.owner)
            return service.error, terminal.private_output
        error, output = self.client.portal.call(capture_new_authorization)
        self.assertIsNone(error)
        self.assertEqual(output, "")
        self.broker.save_shared_auth.assert_awaited_once()
        settings = self.settings()
        self.assertEqual(settings["mode"], "official_share")
        self.assertEqual(settings["userIds"], [owner["id"]])
        self.assertEqual(settings["accountGeneration"], before_generation + 1)
        quota = self.user_quota(owner["id"])
        self.assertTrue(quota["exhausted"])
        self.assertIsNone(quota["fiveHour"]["limit"])
        self.assertIsNone(quota["weekly"]["limit"])
        self.assertIsNone(self.client.get("/api/account/usage").json()["official"]["updatedAt"])


if __name__ == "__main__":
    unittest.main()

"""Authenticated account quotas, lifecycle routes and account-level retention."""
import unittest
from unittest.mock import AsyncMock, patch

from test_multiuser_api import ORIGIN, LEGACY
import test_multiuser_api as fixtures


class UsageApiTests(unittest.TestCase):
    login = fixtures.MultiuserApiTests.login
    admin = fixtures.MultiuserApiTests.admin
    mutate = fixtures.MultiuserApiTests.mutate
    add_user = fixtures.MultiuserApiTests.add_user
    create_workspace = fixtures.MultiuserApiTests.create_workspace

    def setUp(self):
        self.maintenance = patch("backend.app.UsageControl.start")
        self.maintenance.start()
        fixtures.MultiuserApiTests.setUp(self)

    def tearDown(self):
        try:
            fixtures.MultiuserApiTests.tearDown(self)
        finally:
            self.maintenance.stop()

    def set_quota(self, user_id, five=100, weekly=1000):
        return self.mutate("PUT", f"/api/admin/users/{user_id}/quota",
                           {"fiveHourLimit": five, "weeklyLimit": weekly}, admin=True)

    def test_account_read_and_refresh_require_own_login_and_csrf(self):
        self.assertEqual(self.client.get("/api/account/usage").status_code, 401)
        self.assertEqual(self.client.post("/api/account/usage/refresh").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/account/usage").status_code, 200)
        with patch.object(self.runtime["usage"], "refresh", new_callable=AsyncMock) as refresh:
            for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://evil.example", "X-CSRF-Token": self.csrf}):
                self.assertEqual(self.client.post("/api/account/usage/refresh", headers=headers).status_code, 403)
            refresh.assert_not_awaited()
            response = self.mutate("POST", "/api/account/usage/refresh")
            self.assertEqual(response.status_code, 200)
            refresh.assert_awaited_once()
        self.broker.chat_command.assert_not_called()

    def test_user_cannot_manage_quotas_and_admin_writes_require_own_csrf(self):
        owner = self.login()["user"]
        self.assertEqual(self.client.get("/api/admin/quotas").status_code, 401)
        body = {"fiveHourLimit": 1, "weeklyLimit": 10}
        path = f"/api/admin/users/{owner['id']}/quota"
        self.assertEqual(self.mutate("PUT", path, body).status_code, 401)
        self.admin()
        for headers in ({}, {"Origin": ORIGIN}, {"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
                        {"Origin": "https://evil.example", "X-CSRF-Token": self.admin_csrf}):
            self.assertEqual(self.client.put(path, headers=headers, json=body).status_code, 403)
        self.assertIsNone(self.runtime["quotas"].snapshot(owner["id"])["fiveHour"]["limit"])
        self.assertEqual(self.set_quota(owner["id"]).status_code, 200)

    def test_admin_refresh_requires_admin_login_own_csrf_and_valid_origin(self):
        path = "/api/admin/quotas/refresh"
        self.assertEqual(self.client.post(path).status_code, 401)
        self.login()
        self.assertEqual(self.mutate("POST", path).status_code, 401)
        self.admin()
        with patch.object(self.runtime["usage"], "refresh", new_callable=AsyncMock) as refresh:
            for headers in ({}, {"Origin": ORIGIN}, {"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
                            {"Origin": "https://evil.example", "X-CSRF-Token": self.admin_csrf}):
                self.assertEqual(self.client.post(path, headers=headers).status_code, 403)
            refresh.assert_not_awaited()
            response = self.mutate("POST", path, admin=True)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("official", response.json())
            self.assertIn("users", response.json())
            refresh.assert_awaited_once()
        self.broker.chat_command.assert_not_called()

    def test_admin_refresh_rechecks_session_after_wait_and_rejects_expired_admin(self):
        self.admin()
        async def expire_while_refreshing():
            for session in self.app.state.admin_sessions.sessions.values():
                session.expires = 0
        with patch.object(self.runtime["usage"], "refresh", side_effect=expire_while_refreshing) as refresh:
            response = self.mutate("POST", "/api/admin/quotas/refresh", admin=True)
            self.assertEqual(response.status_code, 401, response.text)
            refresh.assert_awaited_once()
            refresh.reset_mock()
            self.assertEqual(self.mutate("POST", "/api/admin/quotas/refresh", admin=True).status_code, 401)
            refresh.assert_not_awaited()

    def test_account_summary_shows_only_own_usage_and_no_credentials(self):
        owner = self.login()["user"]
        alice = self.add_user()
        self.runtime["quotas"].charge(owner["id"], "owner", 10)
        self.runtime["quotas"].charge(alice["id"], "alice", 99)
        response = self.client.get("/api/account/usage")
        self.assertEqual(response.json()["quota"]["weekly"]["used"], 10)
        self.assertNotIn(alice["id"], response.text)
        self.assertNotIn("password", response.text.lower())
        self.assertNotIn("scrypt", response.text)
        self.login("alice")
        self.assertEqual(self.client.get("/api/account/usage").json()["quota"]["weekly"]["used"], 99)

    def test_individual_and_average_limits_preserve_used_amounts(self):
        owner = self.login()["user"]
        self.add_user()
        self.add_user("bob")
        self.runtime["quotas"].charge(owner["id"], "already-used", 7)
        response = self.set_quota(owner["id"], 20, 200)
        self.assertEqual(response.status_code, 200, response.text)
        response = self.mutate("POST", "/api/admin/quotas/distribute",
                               {"fiveHourTotal": 5, "weeklyTotal": 100, "confirmation": "wrong"}, admin=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.runtime["quotas"].snapshot(owner["id"])["fiveHour"]["limit"], 20)
        response = self.mutate("POST", "/api/admin/quotas/distribute",
                               {"fiveHourTotal": 5, "weeklyTotal": 100, "confirmation": "DISTRIBUTE_QUOTA"}, admin=True)
        self.assertEqual(response.status_code, 200, response.text)
        users = response.json()["users"]
        self.assertEqual(sum(user["quota"]["fiveHour"]["limit"] for user in users), 5)
        self.assertEqual(sum(user["quota"]["weekly"]["limit"] for user in users), 100)
        self.assertEqual(next(user for user in users if user["id"] == owner["id"])["quota"]["weekly"]["used"], 7)

    def test_invalid_quota_fields_are_rejected_before_modification(self):
        owner = self.login()["user"]
        self.admin()
        for value in (-1, True, 1.5, "10", 10**12 + 1):
            with self.subTest(value=value):
                self.assertEqual(self.set_quota(owner["id"], value, 10).status_code, 422)
        self.assertIsNone(self.runtime["quotas"].snapshot(owner["id"])["fiveHour"]["limit"])
        self.assertEqual(self.set_quota("f" * 32).status_code, 404)

    def test_lowering_quota_below_used_pauses_running_task_immediately(self):
        owner = self.login()["user"]
        self.admin()
        self.runtime["quotas"].charge(owner["id"], "already-used", 10)
        def running_task():
            session = self.runtime["chats"].get(LEGACY)
            session.phase = "thinking"
            session.message("user", "already executing")
            session.changed()
        self.client.portal.call(running_task)
        response = self.set_quota(owner["id"], 5, 100)
        self.assertEqual(response.status_code, 200, response.text)
        state = self.client.get(f"/api/workspaces/{LEGACY}/chat").json()
        self.assertTrue(state["quotaPaused"])
        self.assertEqual(state["phase"], "paused_quota")
        self.assertEqual(state["messages"][0]["text"], "already executing")

    def test_deleting_workspace_does_not_restore_user_allowance(self):
        owner = self.login()["user"]
        item = self.create_workspace()
        self.runtime["quotas"].charge(owner["id"], "workspace-usage", 25)
        response = self.mutate("DELETE", f"/api/workspaces/{item['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/account/usage").json()["quota"]["weekly"]["used"], 25)
        self.create_workspace()
        self.assertEqual(self.client.get("/api/account/usage").json()["quota"]["weekly"]["used"], 25)

    def test_deleting_account_removes_its_quota_ledger_only(self):
        owner = self.login()["user"]
        alice = self.add_user()
        self.runtime["quotas"].charge(owner["id"], "owner", 30)
        self.runtime["quotas"].charge(alice["id"], "alice", 20)
        response = self.mutate("POST", f"/api/admin/users/{alice['id']}/delete",
                               {"confirmation": "DELETE_USER_AND_DATA"}, admin=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])
        for table in ("quota_users", "quota_events", "usage_sessions", "usage_messages"):
            self.assertEqual(self.runtime["quotas"].db.execute(f"SELECT count(*) FROM {table} WHERE user_id=?",
                                                              (alice["id"],)).fetchone()[0], 0)
        self.assertEqual(self.runtime["quotas"].snapshot(owner["id"])["weekly"]["used"], 30)

    def test_polling_does_not_extend_idle_deadline_but_user_activity_does(self):
        self.login()
        control = self.runtime["usage"]
        clock = [1000.0]
        control.clock = lambda: clock[0]
        self.assertEqual(self.mutate("POST", f"/api/workspaces/{LEGACY}/activity").status_code, 200)
        def last_active():
            return control.db.execute("SELECT last_active FROM activity WHERE workspace=?", (LEGACY,)).fetchone()[0]
        self.assertEqual(self.client.portal.call(last_active), 1000)
        clock[0] = 1500
        for path in ("/api/workspaces", "/api/account/usage", f"/api/workspaces/{LEGACY}/chat"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.portal.call(last_active), 1000)
        self.assertEqual(self.mutate("POST", f"/api/workspaces/{LEGACY}/activity").status_code, 200)
        self.assertEqual(self.client.portal.call(last_active), 1500)

    def test_activity_and_resume_cannot_access_another_users_workspace(self):
        self.add_user()
        self.login("alice")
        with patch.object(self.runtime["chats"], "resume_quota", new_callable=AsyncMock) as resume:
            for suffix in ("/activity", "/chat/resume"):
                self.assertEqual(self.mutate("POST", f"/api/workspaces/{LEGACY}" + suffix).status_code, 404)
            resume.assert_not_awaited()

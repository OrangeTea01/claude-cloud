"""Destructive account cleanup must remain scoped, durable and retryable."""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from backend.app import COOKIE
from backend.workspace_access import DeleteAllBody
from test_multiuser_api import ADMIN_COOKIE, LEGACY, ORIGIN, USER_PASSWORD
import test_multiuser_api as fixtures


class UserDeletionTests(unittest.TestCase):
    setUp = fixtures.MultiuserApiTests.setUp
    tearDown = fixtures.MultiuserApiTests.tearDown
    login = fixtures.MultiuserApiTests.login
    admin = fixtures.MultiuserApiTests.admin
    mutate = fixtures.MultiuserApiTests.mutate
    add_user = fixtures.MultiuserApiTests.add_user
    create_workspace = fixtures.MultiuserApiTests.create_workspace

    def remove(self, user_id):
        return self.mutate("POST", f"/api/admin/users/{user_id}/delete",
                           {"confirmation": "DELETE_USER_AND_DATA"}, admin=True)

    def batch(self, ids):
        return self.mutate("POST", "/api/admin/users/delete-batch",
                           {"userIds": ids, "confirmation": "DELETE_USERS_AND_DATA"}, admin=True)

    def stored_users(self):
        return self.client.get("/api/admin/users").json()["users"]

    def test_single_user_removes_history_and_all_workspaces_only_for_target(self):
        alice = self.add_user()
        self.login("alice")
        alice_token = self.client.cookies.get(COOKIE)
        projects = [self.create_workspace()["id"] for _ in range(2)]
        self.client.cookies.delete(COOKIE)
        bob = self.add_user("bob")
        self.login("bob")
        kept = self.create_workspace()["id"]
        bob_token = self.client.cookies.get(COOKIE)

        async def seed():
            for key in [*projects, kept]:
                self.runtime["chats"].store.save(key, None, [{"role": "user", "text": "private history"}])
        self.client.portal.call(seed)
        response = self.remove(alice["id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"deleted": True, "userId": alice["id"], "username": "alice",
                                          "deletedWorkspaces": 2, "failed": [], "remaining": 0})
        self.assertEqual(set(self.broker.deleted), set(projects))
        self.assertEqual(set(self.broker.records), {LEGACY, kept})
        self.assertTrue(self.broker.raw(kept)["running"])
        self.assertIsNone(self.app.state.sessions.get(alice_token))
        self.assertEqual(self.app.state.sessions.get(bob_token).user_id, bob["id"])
        self.assertEqual(self.client.get("/api/session").json()["user"]["id"], bob["id"])
        self.assertEqual(self.client.get("/api/admin/session").json()["authenticated"], True)
        async def history():
            return [self.runtime["chats"].store.load(key)[1] for key in [*projects, kept]]
        history_rows = self.client.portal.call(history)
        self.assertEqual(history_rows[:2], [[], []])
        self.assertTrue(history_rows[2])
        self.assertNotIn(alice["id"], [user["id"] for user in self.stored_users()])
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN},
                                         json={"username": "alice", "password": USER_PASSWORD}).status_code, 401)
        replacement = self.add_user("alice")
        self.assertNotEqual(replacement["id"], alice["id"])
        self.login("alice")
        self.assertEqual(self.client.get("/api/workspaces").json(), {"workspaces": []})

    def test_batch_failure_keeps_only_failed_user_disabled_and_retry_completes(self):
        alice = self.add_user()
        self.login("alice")
        failed = self.create_workspace()["id"]
        bob = self.add_user("bob")
        self.login("bob")
        deleted = self.create_workspace()["id"]
        self.broker.fail_delete.add(failed)
        response = self.batch([alice["id"], bob["id"]])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["deletedUsers"], result["failedUsers"]), (1, 1))
        self.assertFalse(result["results"][0]["deleted"])
        self.assertEqual(result["results"][0]["remaining"], 1)
        self.assertEqual(result["results"][0]["failed"][0]["id"], failed)
        self.assertTrue(result["results"][1]["deleted"])
        self.assertNotIn(deleted, self.broker.records)
        self.assertIn(LEGACY, self.broker.records)
        self.assertFalse(self.broker.raw(failed)["running"])
        pending = next(user for user in self.stored_users() if user["id"] == alice["id"])
        self.assertTrue(pending["deleting"])
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN},
                                         json={"username": "alice", "password": USER_PASSWORD}).status_code, 401)
        self.assertEqual(self.mutate("PUT", f"/api/admin/users/{alice['id']}/password",
                                    {"newPassword": USER_PASSWORD, "confirmPassword": USER_PASSWORD}, admin=True).status_code, 409)
        self.broker.fail_delete.clear()
        self.assertTrue(self.remove(alice["id"]).json()["deleted"])

    def test_authorization_and_all_batch_ids_validated_before_any_mutation(self):
        self.login()
        owner = self.client.get("/api/session").json()["user"]
        body = {"confirmation": "DELETE_USER_AND_DATA"}
        self.admin_csrf = self.csrf
        self.assertEqual(self.remove(owner["id"]).status_code, 401)
        self.admin()
        url = f"/api/admin/users/{owner['id']}/delete"
        self.assertEqual(self.client.post(url, json=body).status_code, 403)
        self.assertEqual(self.mutate("POST", url, {"confirmation": "wrong"}, admin=True).status_code, 400)
        for ids, status in [([], 422), ([owner["id"], owner["id"]], 422), (["bad"], 422),
                            ([owner["id"], "f" * 32], 404)]:
            self.assertEqual(self.batch(ids).status_code, status)
        self.assertFalse(self.broker.deleted)
        self.assertFalse(self.stored_users()[0]["deleting"])

    def test_history_failure_and_database_finish_failure_keep_retryable_accounts(self):
        owner = self.login()["user"]
        self.admin()
        with patch.object(self.runtime["chats"], "forget", side_effect=OSError("disk full")):
            result = self.remove(owner["id"]).json()
        self.assertFalse(result["deleted"])
        self.assertIn(LEGACY, self.broker.records)
        self.assertTrue(self.stored_users()[0]["deleting"])
        self.assertEqual(self.client.get("/api/session").json(), {"authenticated": False})
        self.assertEqual(self.mutate("PUT", "/api/admin/user-password",
                                    {"newPassword": USER_PASSWORD, "confirmPassword": USER_PASSWORD}, admin=True).status_code, 409)
        with patch.object(self.runtime["credentials"], "delete_user", side_effect=OSError("disk full")):
            result = self.remove(owner["id"]).json()
        self.assertFalse(result["deleted"])
        self.assertEqual(result["remaining"], 0)
        self.assertTrue(result["failed"])
        self.assertTrue(self.stored_users()[0]["deleting"])
        self.assertTrue(self.remove(owner["id"]).json()["deleted"])
        self.assertEqual(self.stored_users(), [])
        self.assertEqual(self.mutate("PUT", "/api/admin/user-password",
                                    {"newPassword": USER_PASSWORD, "confirmPassword": USER_PASSWORD}, admin=True).status_code, 404)

    def test_failed_listing_reports_unknown_remaining_and_continues_other_users(self):
        alice, bob = self.add_user(), self.add_user("bob")
        original = self.broker.list
        async def scoped_failure(owner_id=None):
            if owner_id == alice["id"]:
                raise OSError("unavailable")
            return await original(owner_id=owner_id)
        with patch.object(self.broker, "list", side_effect=scoped_failure):
            result = self.batch([alice["id"], bob["id"]]).json()
        self.assertEqual((result["deletedUsers"], result["failedUsers"]), (1, 1))
        self.assertIsNone(result["results"][0]["remaining"])
        self.assertTrue(result["results"][0]["failed"])
        self.assertTrue(self.remove(alice["id"]).json()["deleted"])

    def test_pending_deletion_blocks_login_even_when_password_check_was_in_flight(self):
        owner = self.login()["user"]
        async def deactivate_during_verify(function, *args):
            self.runtime["credentials"].begin_user_deletion(owner["id"])
            return True
        with patch("backend.app.asyncio.to_thread", side_effect=deactivate_during_verify):
            response = self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": USER_PASSWORD})
        self.assertEqual(response.status_code, 401)
        # Persistence, not merely token revocation, rejects old in-memory sessions.
        self.assertEqual(self.client.get("/api/workspaces").status_code, 401)
        self.assertEqual(self.client.get("/api/session").json(), {"authenticated": False})

    def test_cleanup_serializes_other_deletions_but_keeps_other_users_logged_in(self):
        alice = self.add_user()
        self.login("alice")
        self.create_workspace()
        self.client.cookies.delete(COOKIE)
        self.login()
        headers = {"Origin": ORIGIN, "X-CSRF-Token": self.admin_csrf}
        cookies = dict(self.client.cookies)
        original = self.broker.delete

        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow_delete(workspace_id):
                entered.set()
                await release.wait()
                await original(workspace_id)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN, cookies=cookies) as client:
                with patch.object(self.broker, "delete", side_effect=slow_delete):
                    first = asyncio.create_task(client.post(f"/api/admin/users/{alice['id']}/delete", headers=headers,
                                                            json={"confirmation": "DELETE_USER_AND_DATA"}))
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        self.assertEqual((await client.get("/api/session")).json()["authenticated"], True)
                        for path, body in [("/api/admin/users/delete-batch", {"userIds": [alice["id"]], "confirmation": "DELETE_USERS_AND_DATA"}),
                                           ("/api/admin/workspaces/delete-all", {"confirmation": "DELETE_ALL_WORKSPACES"})]:
                            self.assertEqual((await client.post(path, headers=headers, json=body)).status_code, 409)
                    finally:
                        release.set()
                    self.assertTrue((await first).json()["deleted"])
        self.client.portal.call(scenario)

    def test_inflight_workspace_create_is_included_in_account_cleanup(self):
        alice = self.add_user()
        self.login("alice")
        cookies = dict(self.client.cookies)
        admin_headers = {"Origin": ORIGIN, "X-CSRF-Token": self.admin_csrf}
        user_headers = {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}
        create = self.broker.create
        begin = self.runtime["credentials"].begin_user_deletions

        async def scenario():
            creating, release, marked = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def slow_create(*args, **kwargs):
                creating.set()
                await release.wait()
                return await create(*args, **kwargs)
            def begin_and_signal(ids):
                result = begin(ids)
                marked.set()
                return result
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN, cookies=cookies) as client:
                with patch.object(self.broker, "create", side_effect=slow_create), patch.object(self.runtime["credentials"], "begin_user_deletions", side_effect=begin_and_signal):
                    creation = asyncio.create_task(client.post("/api/workspaces", headers=user_headers, json={"name": "racing project"}))
                    await asyncio.wait_for(creating.wait(), 2)
                    deletion = asyncio.create_task(client.post(f"/api/admin/users/{alice['id']}/delete", headers=admin_headers,
                                                               json={"confirmation": "DELETE_USER_AND_DATA"}))
                    try:
                        await asyncio.wait_for(marked.wait(), 2)
                    finally:
                        release.set()
                    self.assertEqual((await creation).status_code, 401)
                    result = (await deletion).json()
                    self.assertTrue(result["deleted"])
                    self.assertEqual(result["deletedWorkspaces"], 1)
        self.client.portal.call(scenario)
        self.assertEqual(set(self.broker.records), {LEGACY})

    def test_cancelled_admin_request_does_not_abandon_committed_cleanup(self):
        owner = self.login()["user"]
        self.admin()
        session = self.app.state.admin_sessions.get(self.client.cookies.get(ADMIN_COOKIE))
        endpoint = next(route.endpoint for route in self.app.routes if route.path == "/api/admin/users/{user_id}/delete")
        original = self.broker.delete

        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow_delete(workspace_id):
                entered.set()
                await release.wait()
                await original(workspace_id)
            with patch.object(self.broker, "delete", side_effect=slow_delete):
                request = asyncio.create_task(endpoint(owner["id"], DeleteAllBody(confirmation="DELETE_USER_AND_DATA"), session))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    request.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(self.runtime["bulk_delete_task"].cancelled())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                self.assertIsNone(self.runtime["credentials"].user(owner["id"]))
                self.assertEqual(await self.broker.list(), [])
        self.client.portal.call(scenario)

    def test_deleted_owner_does_not_return_after_service_restart(self):
        owner = self.login()["user"]
        self.admin()
        self.assertTrue(self.remove(owner["id"]).json()["deleted"])
        self.client.__exit__(None, None, None)
        self.app = fixtures.create_app(fixtures.Config(data_dir=self.directory.name, public_origin=ORIGIN,
                                       password_hash=fixtures.USER_HASH, management_password_hash=fixtures.ADMIN_HASH,
                                       cookie_secure=False), broker=self.broker, login_browsers=self.browsers)
        self.client = fixtures.TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        self.runtime = self.app.state.runtime
        self.admin()
        self.assertEqual(self.stored_users(), [])
        self.assertEqual(self.client.get("/api/health").json(), {"ok": True})
        self.assertEqual(self.client.post("/api/login", headers={"Origin": ORIGIN}, json={"username": "owner", "password": USER_PASSWORD}).status_code, 401)
        replacement = self.add_user("owner")
        self.assertNotEqual(replacement["id"], owner["id"])
        self.login()
        self.assertEqual(self.client.get("/api/workspaces").json(), {"workspaces": []})


if __name__ == "__main__":
    unittest.main()

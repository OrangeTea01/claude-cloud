"""Direct chat migration boundaries, with no cloud calls or real Docker."""
from copy import deepcopy
import unittest
from unittest.mock import ANY, AsyncMock, patch

from backend.docker_backend import BrokerError
import test_chat as chat_fixture


WORKSPACE = chat_fixture.WORKSPACE_A


class DirectChatHistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = chat_fixture.ChatTests.asyncSetUp
    asyncTearDown = chat_fixture.ChatTests.asyncTearDown
    connected = chat_fixture.ChatTests.connected

    async def test_mixed_legacy_and_direct_history_preserves_content(self):
        legacy = self.session.message("assistant", text="已有中文", original="Existing English")
        legacy["translationStatus"] = "done"
        failed = self.session.message("assistant", text="", original="Untranslated reply")
        failed["translationStatus"] = "failed"
        pending = self.session.message("assistant", text="", original="Interrupted reply")
        pending["translationStatus"] = "pending"
        direct = self.session.message("assistant", text="  **原样回复**\n")
        self.session.session_id = chat_fixture.SESSION_A
        self.session.selected_model = "haiku"
        self.session.changed()
        expected = deepcopy(self.session.messages)
        expected[2]["translationStatus"] = "failed"
        session_id, messages = self.chats.store.load(WORKSPACE)
        self.assertEqual(messages, expected)
        self.assertEqual(session_id, chat_fixture.SESSION_A)
        self.assertEqual(self.chats.store.load_model(WORKSPACE), "haiku")
        self.assertEqual(direct["translationStatus"], "not_needed")
        self.translator.translate.assert_not_awaited()

    async def test_claude_requested_other_language_is_not_rewritten(self):
        self.translator.translate.side_effect = AssertionError("Translation must not run")
        self.connected()
        text = "  # Result\n\nKeep `src/中文.py` unchanged.\n"
        await self.session.assistant_text(text)
        self.assertEqual(self.session.messages[0]["text"], text)
        self.assertEqual(self.session.messages[0]["original"], "")
        self.assertEqual(self.session.messages[0]["translationStatus"], "not_needed")
        self.translator.translate.assert_not_awaited()

    async def test_invalid_direct_inputs_leave_history_and_process_untouched(self):
        before = deepcopy(self.session.snapshot())
        for value in (None, "", " \n\t", "x" * 16001, 12):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(BrokerError):
                await self.chats.send(WORKSPACE, value)
        self.assertEqual(self.session.snapshot(), before)
        self.broker.chat_command.assert_not_awaited()
        self.broker.workspace.assert_not_awaited()
        self.translator.translate.assert_not_awaited()


class DirectChatApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chat_fixture.ChatApiTests.setUpClass.__func__(cls)

    setUp = chat_fixture.ChatApiTests.setUp
    tearDown = chat_fixture.ChatApiTests.tearDown
    login = chat_fixture.ChatApiTests.login

    def test_removed_translation_endpoints_require_authentication_and_csrf(self):
        paths = [f"/api/workspaces/{WORKSPACE}/chat/{suffix}"
                 for suffix in ("preview", "back-translate", "messages/old/translate")]
        for path in paths:
            self.assertEqual(self.client.post(path, json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/translation/status").status_code, 401)
        headers = self.login()
        for path in paths:
            for bad_headers in ({}, {"Origin": self.origin},
                                {**headers, "Origin": "https://evil.example"}):
                with self.subTest(path=path, headers=bad_headers):
                    response = self.client.post(path, headers=bad_headers, json={})
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.app.state.runtime["chats"].items, {})
        self.translator.translate.assert_not_awaited()

    def test_removed_translation_endpoints_return_gone_without_processing_text(self):
        headers = self.login()
        for suffix in ("preview", "back-translate", "messages/old/translate"):
            response = self.client.post(f"/api/workspaces/{WORKSPACE}/chat/{suffix}",
                                        headers=headers, json={"text": "中文", "translatedText": "English"})
            self.assertEqual(response.status_code, 410)
            self.assertIn("翻译功能已移除", response.json()["detail"])
        self.assertEqual(self.client.get("/api/translation/status").status_code, 410)
        self.assertEqual(self.app.state.runtime["chats"].items, {})
        self.translator.translate.assert_not_awaited()
        self.translator.status.assert_not_awaited()

    def test_message_api_rejects_old_reviewed_translation_and_invalid_input(self):
        headers = self.login()
        path = f"/api/workspaces/{WORKSPACE}/chat/message"
        bodies = [{"text": value} for value in (None, "", " \n\t", "x" * 16001, 12)]
        bodies += [{"text": "中文", "translatedText": value} for value in (None, "", "English")]
        bodies += [{"text": "中文", "command": "arbitrary"}]
        chats = self.app.state.runtime["chats"]
        with patch.object(chats, "send", AsyncMock()) as send:
            for body in bodies:
                with self.subTest(keys=list(body)):
                    self.assertEqual(self.client.post(path, headers=headers, json=body).status_code, 422)
            send.assert_not_awaited()

    def test_message_api_forwards_exact_raw_text_without_translation(self):
        headers = self.login()
        raw = "  请检查路径 `src/中文.py`。\n\n```python\nprint('中文')\n```\n"
        chats = self.app.state.runtime["chats"]
        with patch.object(chats, "send", AsyncMock(return_value={"phase": "starting"})) as send:
            response = self.client.post(f"/api/workspaces/{WORKSPACE}/chat/message",
                                        headers=headers, json={"text": raw})
            self.assertEqual(response.status_code, 202)
            send.assert_awaited_once_with(WORKSPACE, raw, [], authorize=ANY)
            self.assertTrue(callable(send.await_args.kwargs["authorize"]))
        self.translator.translate.assert_not_awaited()

    def test_app_can_start_without_constructing_translation_client(self):
        from multiuser_fixture import legacy_app as create_app
        from fastapi.testclient import TestClient
        from types import SimpleNamespace
        from pathlib import Path
        from backend.docker_backend import Config
        with patch("backend.translator_client.LocalTranslator", side_effect=AssertionError("must not construct")) as translator:
            app = create_app(Config(public_origin=self.origin, password_hash=self.password_hash,
                                    cookie_secure=False, data_dir=Path(self.temp.name)),
                             broker=self.broker,
                             login_browsers=SimpleNamespace(reconcile=AsyncMock(), close=AsyncMock(), stop=AsyncMock()))
            with TestClient(app, base_url=self.origin) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
            translator.assert_not_called()


if __name__ == "__main__":
    unittest.main()

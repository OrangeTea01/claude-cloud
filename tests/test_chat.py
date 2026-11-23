"""Bilingual chat boundary and lifecycle tests; no cloud calls or real Docker."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend.docker_backend import BrokerError, Config, DockerBroker
from backend.chat import Chats, ChatStore
from backend.terminal import Terminals
from backend.translator_client import LocalTranslator


WORKSPACE_A = "a" * 32
WORKSPACE_B = "b" * 32
SESSION_A = "11111111-1111-4111-8111-111111111111"


class RecordingInput:
    def __init__(self):
        self.messages = []
        self.closed = False

    def write(self, raw):
        if self.closed:
            raise BrokenPipeError("test stdin closed")
        self.messages.extend(json.loads(line) for line in raw.decode().splitlines() if line)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    """Models newline-framed CLI stdout with independently controlled lifetime."""
    def __init__(self):
        self.stdin = RecordingInput()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.pid = 12345
        self.exited = asyncio.Event()

    def emit(self, message):
        self.stdout.feed_data((json.dumps(message) + "\n").encode())

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def finish(self, code=0):
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


async def until(predicate, timeout=2):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


def permission_frame(request_id="permission-1", tool="Bash", tool_input=None):
    return {"type": "control_request", "request_id": request_id, "request": {
        "subtype": "can_use_tool", "tool_name": tool,
        "input": tool_input if tool_input is not None else {"command": "pwd"},
    }}


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.broker = SimpleNamespace(
            apply_model_policy=AsyncMock(), reconcile_admin_auth=AsyncMock(), shared_auth_status=AsyncMock(return_value={"authenticated": True}),
            get=Mock(return_value={"id": WORKSPACE_A}),
            workspace=AsyncMock(return_value={"id": WORKSPACE_A, "running": True}),
            stop=AsyncMock(), start=AsyncMock(),
            chat_command=AsyncMock(return_value=["fake-claude"]),
            terminal_command=AsyncMock(return_value=["fake-claude"]),
        )
        self.terminals = Terminals(self.broker)
        self.translator = SimpleNamespace(translate=AsyncMock(return_value="Translated text"))
        self.chats = Chats(self.broker, self.terminals, self.translator, Path(self.temp.name))
        self.session = self.chats.get(WORKSPACE_A)
        self.processes = []

    async def asyncTearDown(self):
        for proc in self.processes:
            proc.finish()
        await self.chats.shutdown()
        self.temp.cleanup()

    def connected(self, session=None):
        session = session or self.session
        process = FakeProcess()
        self.processes.append(process)
        session.proc = process
        session.phase = "thinking"
        return process

    async def test_unavailable_translator_does_not_block_direct_input(self):
        process = self.connected()
        self.session.phase = "idle"
        self.translator.translate.side_effect = BrokerError("local translation unavailable", 503)
        await self.chats.send(WORKSPACE_A, "私密中文输入")
        await self.session.input_task
        self.translator.translate.assert_not_awaited()
        self.assertEqual(process.stdin.messages[0]["message"]["content"], "私密中文输入")
        message = self.session.snapshot()["messages"][0]
        self.assertTrue(message["sent"])
        self.assertEqual(message["original"], "")
        self.assertEqual(message["translationStatus"], "not_needed")
        self.assertEqual(self.session.phase, "thinking")

    async def test_verbatim_input_reaches_cli_and_history(self):
        process = self.connected()
        self.session.phase = "idle"
        raw = "  请检查这个项目。\n\n```python\nprint('中文')\n```\n"
        await self.chats.send(WORKSPACE_A, raw)
        await self.session.input_task
        self.translator.translate.assert_not_awaited()
        sent = process.stdin.messages
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "user")
        self.assertEqual(sent[0]["message"]["content"], raw)
        self.assertTrue(sent[0]["client_composed"])
        self.assertTrue(self.session.messages[0]["sent"])
        self.assertEqual(self.session.messages[0]["text"], raw)
        self.assertEqual(self.chats.store.load(WORKSPACE_A)[1][0]["text"], raw)

    async def test_permission_uses_saved_input_once_and_snapshot_cannot_mutate_it(self):
        process = self.connected()
        frame = permission_frame(tool_input={"command": "printf harmless", "nested": {"value": [1]}})
        expected = deepcopy(frame["request"]["input"])
        await self.session.handle(frame)
        frame["request"]["input"]["nested"]["value"].append(2)
        snapshot = self.session.snapshot()
        snapshot["permissions"][0]["input"]["command"] = "substituted command"
        await self.chats.permission(WORKSPACE_A, "permission-1", True)
        self.assertEqual(process.stdin.messages[-1]["response"]["response"],
                         {"behavior": "allow", "updatedInput": expected})
        self.assertEqual(self.session.pending, {})
        with self.assertRaises(BrokerError):
            await self.chats.permission(WORKSPACE_A, "permission-1", True)
        self.assertEqual(len(process.stdin.messages), 1)

    async def test_permission_denial_never_includes_updated_input(self):
        process = self.connected()
        await self.session.handle(permission_frame())
        await self.chats.permission(WORKSPACE_A, "permission-1", False)
        result = process.stdin.messages[-1]["response"]["response"]
        self.assertEqual(result["behavior"], "deny")
        self.assertNotIn("updatedInput", result)

    async def test_cancelled_permission_cannot_later_be_allowed(self):
        process = self.connected()
        await self.session.handle(permission_frame())
        await self.session.handle({"type": "control_cancel_request", "request_id": "permission-1"})
        with self.assertRaises(BrokerError):
            await self.chats.permission(WORKSPACE_A, "permission-1", True)
        self.assertEqual(self.session.pending, {})
        self.assertEqual(process.stdin.messages, [])

    async def test_expired_permission_is_denied_without_user_action(self):
        process = self.connected()
        with patch("backend.chat.PERMISSION_TIMEOUT", 0.01):
            await self.session.handle(permission_frame())
            await until(lambda: bool(process.stdin.messages))
        self.assertEqual(process.stdin.messages[-1]["response"]["response"]["behavior"], "deny")
        with self.assertRaises(BrokerError):
            await self.chats.permission(WORKSPACE_A, "permission-1", True)

    async def test_permission_delivery_failure_reaps_container_without_lock_deadlock(self):
        process = self.connected()
        self.broker.stop.side_effect = lambda workspace: process.finish()
        await self.session.handle(permission_frame())
        process.stdin.close()
        with self.assertRaises(BrokerError):
            await asyncio.wait_for(self.chats.permission(WORKSPACE_A, "permission-1", True), 1)
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.phase, "error")
        self.assertEqual(self.session.pending, {})
        self.broker.stop.assert_awaited_once_with(WORKSPACE_A)

    async def test_expiry_delivery_failure_reaps_container(self):
        process = self.connected()
        self.broker.stop.side_effect = lambda workspace: process.finish()
        with patch("backend.chat.PERMISSION_TIMEOUT", 0.01):
            await self.session.handle(permission_frame())
            process.stdin.close()
            await until(lambda: self.session.phase == "error")
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.pending, {})
        self.broker.stop.assert_awaited_once_with(WORKSPACE_A)

    async def test_out_of_scope_tools_and_background_jobs_are_denied(self):
        process = self.connected()
        frames = [permission_frame(tool="Agent"),
                  permission_frame(tool_input={"command": "sleep 100", "run_in_background": True}),
                  permission_frame(tool="mcp__outside__execute")]
        for frame in frames:
            await self.session.handle(frame)
        self.assertEqual(self.session.pending, {})
        self.assertEqual(len(process.stdin.messages), 3)
        self.assertTrue(all(m["response"]["response"]["behavior"] == "deny" for m in process.stdin.messages))

    async def test_direct_reply_preserves_markdown_without_translator(self):
        process = self.connected()
        self.translator.translate.side_effect = BrokerError("unavailable", 503)
        reply = "## 检查结果\n\n| 文件 | 状态 |\n| --- | --- |\n| `a.py` | 正常 |\n\n```python\nprint('中文')\n```"
        await self.session.handle({"type": "assistant", "message": {"content": [
            {"type": "text", "text": reply}]}})
        item = self.session.messages[-1]
        self.assertEqual(item["text"], reply)
        self.assertEqual(item["original"], "")
        self.assertEqual(item["translationStatus"], "not_needed")
        self.assertIsNone(self.session.error)
        self.assertEqual(process.stdin.messages, [])
        self.broker.chat_command.assert_not_awaited()
        self.translator.translate.assert_not_awaited()

    async def test_only_assistant_text_is_saved_and_result_is_not_duplicated(self):
        self.connected()
        await self.session.handle({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "private chain"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/workspace/a"}},
            {"type": "text", "text": "Finished."}]}})
        await self.session.handle({"type": "result", "result": "Finished.", "is_error": False, "session_id": SESSION_A})
        self.translator.translate.assert_not_awaited()
        self.assertEqual(self.session.messages[0]["text"], "Finished.")
        self.assertEqual(len(self.session.messages), 1)
        self.assertEqual(self.session.phase, "idle")
        self.assertEqual(self.session.session_id, SESSION_A)
        self.assertTrue(self.session.active)

    async def test_error_result_is_error_even_with_success_subtype(self):
        self.connected()
        await self.session.handle({"type": "result", "subtype": "success", "is_error": True})
        self.assertEqual(self.session.phase, "error")
        self.assertIsNotNone(self.session.error)

    async def test_raw_terminal_and_chat_exclude_each_other(self):
        self.terminals.items[WORKSPACE_A] = SimpleNamespace(finished=False)
        with self.assertRaises(BrokerError) as caught:
            await self.chats.send(WORKSPACE_A, "你好")
        self.assertEqual(caught.exception.status, 409)
        self.terminals.items.clear()
        self.connected()
        with self.assertRaises(BrokerError) as caught:
            await self.terminals.attach(WORKSPACE_A)
        self.assertEqual(caught.exception.status, 409)
        self.broker.terminal_command.assert_not_awaited()

    async def test_starting_input_excludes_raw_terminal_and_second_turn(self):
        gate = asyncio.Event()
        with patch.object(self.session, "launch", AsyncMock(side_effect=gate.wait)):
            await self.chats.send(WORKSPACE_A, "你好")
            with self.assertRaises(BrokerError):
                await self.terminals.attach(WORKSPACE_A)
            with self.assertRaises(BrokerError):
                await self.chats.send(WORKSPACE_A, "第二条")
            self.broker.terminal_command.assert_not_awaited()
            await self.chats.stop(WORKSPACE_A)
        self.broker.chat_command.assert_not_awaited()

    async def test_stop_cancels_input_during_launch_before_it_can_be_sent(self):
        entered, gate = asyncio.Event(), asyncio.Event()
        async def launch():
            entered.set()
            await gate.wait()
        with patch.object(self.session, "launch", AsyncMock(side_effect=launch)):
            await self.chats.send(WORKSPACE_A, "你好")
            await entered.wait()
            task = self.session.input_task
            await self.chats.stop(WORKSPACE_A)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.session.messages[0]["sent"])
        self.assertEqual(self.session.messages[0]["translationStatus"], "not_needed")
        self.assertEqual(self.session.phase, "stopped")
        self.broker.chat_command.assert_not_awaited()

    async def test_real_reader_initializes_then_submits_direct_prompt(self):
        process = FakeProcess()
        self.processes.append(process)
        await self.chats.set_model(WORKSPACE_A, "haiku")
        with patch("backend.chat.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            await self.chats.send(WORKSPACE_A, "中文提示")
            await until(lambda: bool(process.stdin.messages))
            init = process.stdin.messages[0]
            self.assertEqual(init["request"]["subtype"], "initialize")
            self.assertFalse(any(m["type"] == "user" for m in process.stdin.messages))
            process.emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": init["request_id"], "response": {}}})
            await self.session.input_task
            self.assertEqual(self.broker.chat_command.await_args.args[-1], "haiku")
            self.assertEqual(process.stdin.messages[-1]["message"]["content"], "中文提示")
            process.emit({"type": "result", "is_error": False, "session_id": SESSION_A})
            await until(lambda: self.session.phase == "idle")

    async def test_process_exit_during_initialization_fails_without_deadlock(self):
        process = FakeProcess()
        self.processes.append(process)
        self.broker.stop.side_effect = lambda workspace: process.finish()
        with patch("backend.chat.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            await self.chats.send(WORKSPACE_A, "中文")
            await until(lambda: bool(process.stdin.messages))
            process.finish(1)
            await until(lambda: self.session.phase == "error")
            self.assertFalse(any(m["type"] == "user" for m in process.stdin.messages))
            self.assertFalse(self.session.active)

    async def test_session_ids_and_history_are_workspace_scoped_and_invalid_ids_ignored(self):
        self.session.session_id = SESSION_A
        self.session.message("user", text="中文")
        self.session.changed()
        self.assertEqual(self.chats.get(WORKSPACE_B).messages, [])
        self.assertIsNone(self.chats.get(WORKSPACE_B).session_id)
        persisted = ChatStore(self.temp.name)
        try:
            session_id, messages = persisted.load(WORKSPACE_A)
            self.assertEqual(session_id, SESSION_A)
            self.assertEqual(messages[0]["translationStatus"], "not_needed")
            persisted.save(WORKSPACE_B, "--dangerously-skip-permissions", [])
            self.assertEqual(persisted.load(WORKSPACE_B), (None, []))
        finally:
            persisted.close()
        await self.session.handle({"type": "system", "subtype": "init", "session_id": "../../outside"})
        self.assertEqual(self.session.session_id, SESSION_A)

    async def test_chat_command_keeps_permissions_and_validates_resumed_session(self):
        broker = SimpleNamespace(workspace=self.broker.workspace, shared_auth_status=AsyncMock(return_value={"authenticated": True}),
                                 names=Mock(return_value={"container": "owned-container"}))
        command = await DockerBroker.chat_command(broker, WORKSPACE_A, SESSION_A, "not-used")
        self.assertIn("-i", command)
        self.assertNotIn("-it", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "default")
        self.assertEqual(command[command.index("--permission-prompt-tool") + 1], "stdio")
        self.assertIn("--safe-mode", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--bare", command)
        self.assertNotIn("--continue", command)
        self.assertEqual(command[command.index("--resume") + 1], SESSION_A)
        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("unless the user requests another language", prompt)
        self.assertNotIn("replies in English", prompt)
        for invalid in ("--dangerously-skip-permissions", "../../other", "A" * 36):
            with self.subTest(session=invalid), self.assertRaises(BrokerError):
                await DockerBroker.chat_command(broker, WORKSPACE_A, invalid, SESSION_A)

    async def test_saved_model_is_workspace_scoped_and_does_not_launch_claude(self):
        self.session.session_id = SESSION_A
        self.session.message("assistant", original="Preserved reply")
        self.session.messages[-1]["translationStatus"] = "done"
        before = deepcopy(self.session.messages)
        state = await self.chats.set_model(WORKSPACE_A, "opus")
        self.assertEqual(state["selectedModel"], "opus")
        self.assertIsNone(state["activeModel"])
        self.assertEqual(self.chats.get(WORKSPACE_B).selected_model, "default")
        self.assertEqual(self.chats.store.load(WORKSPACE_A), (SESSION_A, before))
        self.assertEqual(self.chats.store.load_model(WORKSPACE_A), "opus")
        state["modelOptions"][0]["value"] = "arbitrary"
        self.assertEqual(self.session.snapshot()["modelOptions"][0]["value"], "default")
        self.broker.chat_command.assert_not_awaited()
        self.broker.start.assert_not_awaited()

    async def start_model_switch(self, model="opus", selected="default"):
        process = self.connected()
        self.session.phase = "idle"
        self.session.selected_model = selected
        self.session.active_model = "claude-old-model"
        self.session.session_id = SESSION_A
        self.session.changed()
        self.session.reader = asyncio.create_task(self.session.read())
        task = asyncio.create_task(self.chats.set_model(WORKSPACE_A, model))
        await until(lambda: bool(process.stdin.messages))
        return process, task, process.stdin.messages[-1]["request_id"]

    async def test_live_model_switch_commits_only_matching_ack_and_blocks_other_work(self):
        process, task, request_id = await self.start_model_switch()
        self.assertEqual(process.stdin.messages[-1]["request"], {"subtype": "set_model", "model": "opus"})
        self.assertEqual(self.session.snapshot()["phase"], "switching_model")
        self.assertTrue(self.session.busy)
        self.assertEqual(self.chats.store.load_model(WORKSPACE_A), "default")
        for operation in (self.chats.send(WORKSPACE_A, "中文"), self.chats.set_model(WORKSPACE_A, "haiku"),
                          self.terminals.attach(WORKSPACE_A)):
            with self.assertRaises(BrokerError) as caught:
                await operation
            self.assertEqual(caught.exception.status, 409)
        process.emit({"type": "control_response", "response": {"request_id": "unrelated", "subtype": "success"}})
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        self.assertEqual(self.session.selected_model, "default")
        process.emit({"type": "control_response", "response": {"request_id": request_id, "subtype": "success", "response": {}}})
        state = await asyncio.wait_for(task, 1)
        self.assertEqual(state["selectedModel"], "opus")
        self.assertIsNone(state["activeModel"])
        self.assertEqual(state["phase"], "idle")
        self.assertEqual(self.session.session_id, SESSION_A)
        self.assertEqual(self.chats.store.load_model(WORKSPACE_A), "opus")
        self.assertIs(self.session.proc, process)
        self.broker.stop.assert_not_awaited()

    async def test_default_reset_uses_null_control_value(self):
        process, task, request_id = await self.start_model_switch("default", "opus")
        self.assertIsNone(process.stdin.messages[-1]["request"]["model"])
        process.emit({"type": "control_response", "response": {"request_id": request_id, "subtype": "success"}})
        self.assertEqual((await task)["selectedModel"], "default")

    async def test_rejected_model_preserves_running_model_and_saved_preference(self):
        process, task, request_id = await self.start_model_switch()
        process.emit({"type": "control_response", "response": {"request_id": request_id, "subtype": "error",
                                                                 "error": "sensitive provider diagnostics"}})
        with self.assertRaises(BrokerError) as caught:
            await task
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(self.session.selected_model, "default")
        self.assertEqual(self.session.active_model, "claude-old-model")
        self.assertEqual(self.session.phase, "idle")
        self.assertFalse(self.session.busy)
        self.assertEqual(self.chats.store.load_model(WORKSPACE_A), "default")
        self.broker.stop.assert_not_awaited()

    async def test_model_timeout_reaps_process_and_keeps_saved_selection(self):
        with patch("backend.chat.MODEL_SWITCH_TIMEOUT", 0.02):
            process, task, _ = await self.start_model_switch()
            self.broker.stop.side_effect = lambda workspace: process.finish()
            with self.assertRaises(BrokerError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(self.session.phase, "error")
        self.assertFalse(self.session.active)
        self.assertEqual(self.chats.store.load_model(WORKSPACE_A), "default")
        self.assertIsNone(self.session.active_model)
        self.broker.stop.assert_awaited_once_with(WORKSPACE_A)

    async def test_exit_during_model_switch_does_not_deadlock(self):
        process, task, _ = await self.start_model_switch()
        process.finish(1)
        with self.assertRaises(BrokerError):
            await asyncio.wait_for(task, 1)
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.phase, "error")
        self.assertEqual(self.session.selected_model, "default")

    async def test_exited_process_cannot_admit_terminal_or_commit_success_ack(self):
        process, task, request_id = await self.start_model_switch()
        self.session.reader.cancel()
        await asyncio.gather(self.session.reader, return_exceptions=True)
        process.finish()
        with self.assertRaises(BrokerError):
            await self.terminals.attach(WORKSPACE_A)
        await self.session.handle({"type": "control_response", "response": {"request_id": request_id, "subtype": "success"}})
        with self.assertRaises(BrokerError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.session.selected_model, "default")
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.phase, "error")

    async def test_stop_during_model_switch_cannot_commit_late_ack(self):
        process, task, request_id = await self.start_model_switch()
        process.finish()
        await asyncio.wait_for(self.chats.stop(WORKSPACE_A), 1)
        with self.assertRaises(BrokerError):
            await asyncio.wait_for(task, 1)
        await self.session.handle({"type": "control_response", "response": {"request_id": request_id, "subtype": "success"}})
        self.assertEqual(self.session.selected_model, "default")
        self.assertEqual(self.session.phase, "stopped")
        self.assertEqual(self.session.session_id, SESSION_A)

    async def test_cancelled_model_switch_fails_closed(self):
        process, task, _ = await self.start_model_switch()
        self.broker.stop.side_effect = lambda workspace: process.finish()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.selected_model, "default")

    async def test_switch_write_never_reaches_replacement_process(self):
        process = self.connected()
        self.session.phase = "idle"
        await self.session.write_lock.acquire()
        task = asyncio.create_task(self.chats.set_model(WORKSPACE_A, "opus"))
        await until(lambda: self.session.phase == "switching_model")
        process.finish()
        await self.chats.stop(WORKSPACE_A)
        replacement = self.connected()
        self.session.closing = False
        self.session.phase = "idle"
        self.session.write_lock.release()
        with self.assertRaises(BrokerError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(replacement.stdin.messages, [])
        self.assertEqual(self.session.phase, "idle")
        self.assertIs(self.session.proc, replacement)
        self.broker.stop.assert_awaited_once_with(WORKSPACE_A)

    async def test_model_change_rejects_all_busy_phases_terminal_and_permissions(self):
        for phase in ("thinking", "starting", "translating_input", "translating_output", "awaiting_permission"):
            self.session.phase = phase
            with self.subTest(phase=phase), self.assertRaises(BrokerError) as caught:
                await self.chats.set_model(WORKSPACE_A, "opus")
            self.assertEqual(caught.exception.status, 409)
        self.session.phase = "idle"
        self.terminals.items[WORKSPACE_A] = SimpleNamespace(finished=False)
        with self.assertRaises(BrokerError):
            await self.chats.set_model(WORKSPACE_A, "opus")
        self.terminals.items.clear()
        self.session.pending["pending"] = {}
        with self.assertRaises(BrokerError):
            await self.chats.set_model(WORKSPACE_A, "opus")
        self.session.pending.clear()
        self.assertEqual(self.session.selected_model, "default")

    async def test_model_reports_are_bounded_and_cleared_on_close(self):
        await self.session.handle({"type": "system", "subtype": "init", "model": "claude-sonnet-test", "session_id": SESSION_A})
        self.assertEqual(self.session.active_model, "claude-sonnet-test")
        await self.session.handle({"type": "assistant", "message": {"model": "claude-opus-test", "content": []}})
        self.assertEqual(self.session.active_model, "claude-opus-test")
        for value in (None, {}, "x" * 201, "bad\nmodel"):
            await self.session.handle({"type": "system", "subtype": "init", "model": value})
            self.assertEqual(self.session.active_model, "claude-opus-test")
        await self.session.close()
        self.assertIsNone(self.session.active_model)

    async def test_invalid_models_cannot_reach_cli_argv(self):
        broker = SimpleNamespace(workspace=self.broker.workspace, shared_auth_status=AsyncMock(return_value={"authenticated": True}), names=Mock(return_value={"container": "owned"}))
        for model in ("default", "sonnet", "opus", "haiku"):
            command = await DockerBroker.chat_command(broker, WORKSPACE_A, SESSION_A, "unused", model)
            self.assertEqual(command[command.index("--model") + 1], model)
        for model in ("--dangerously-skip-permissions", "opus --foo", "$(command)", "Opus", "", None, ["opus"]):
            with self.subTest(model=model), self.assertRaises(BrokerError):
                await DockerBroker.chat_command(broker, WORKSPACE_A, SESSION_A, "unused", model)
            with self.subTest(manager=model), self.assertRaises(BrokerError):
                await self.chats.set_model(WORKSPACE_A, model)

    async def test_reservation_save_failure_never_leaves_switch_busy(self):
        process = self.connected()
        self.session.phase = "idle"
        with patch.object(self.chats.store, "save", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(BrokerError):
                await self.chats.set_model(WORKSPACE_A, "opus")
        self.assertEqual(self.session.phase, "idle")
        self.assertFalse(self.session.busy)
        self.assertEqual(process.stdin.messages, [])
        self.assertEqual(self.session.selected_model, "default")

    async def test_ack_save_failure_rolls_back_choice_and_reaps_cli(self):
        process, task, request_id = await self.start_model_switch()
        self.broker.stop.side_effect = lambda workspace: process.finish()
        real_save = self.chats.store.save
        def fail_new_model(workspace, session_id, messages, model=None, quota_paused=None):
            if model == "opus":
                raise sqlite3.OperationalError("disk full")
            return real_save(workspace, session_id, messages, model, quota_paused)
        with patch.object(self.chats.store, "save", side_effect=fail_new_model):
            process.emit({"type": "control_response", "response": {"request_id": request_id, "subtype": "success"}})
            with self.assertRaises(BrokerError):
                await asyncio.wait_for(task, 1)
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.selected_model, "default")
        self.assertEqual(self.chats.store.load_model(WORKSPACE_A), "default")


class ChatStoreDeletionTests(unittest.TestCase):
    def test_failed_delete_commit_preserves_history_without_implicit_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ChatStore(directory)
            history = [{"role": "user", "text": "retain until committed"}]
            store.save(WORKSPACE_A, SESSION_A, history)
            connection = store.db
            store.db = Mock(wraps=connection)
            store.db.commit.side_effect = sqlite3.OperationalError("disk full")
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    store.delete(WORKSPACE_A)
                store.db.rollback.assert_called_once()
                self.assertFalse(connection.in_transaction)
                self.assertEqual(store.load(WORKSPACE_A), (SESSION_A, history))
            finally:
                store.close()
            reopened = ChatStore(directory)
            try:
                self.assertEqual(reopened.load(WORKSPACE_A), (SESSION_A, history))
                reopened.delete(WORKSPACE_A)
                self.assertEqual(reopened.load(WORKSPACE_A), (None, []))
            finally:
                reopened.close()


class ChatModelMigrationTests(unittest.TestCase):
    def test_old_database_migrates_without_losing_session_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "chat.sqlite3")
            db.execute("CREATE TABLE chats (workspace TEXT PRIMARY KEY, session TEXT, messages TEXT NOT NULL)")
            history = [{"id": "saved", "translationStatus": "done", "text": "保留的历史"}]
            db.execute("INSERT INTO chats VALUES (?,?,?)", (WORKSPACE_A, SESSION_A, json.dumps(history)))
            db.commit()
            db.close()
            store = ChatStore(directory)
            self.assertEqual(store.load(WORKSPACE_A), (SESSION_A, history))
            self.assertEqual(store.load_model(WORKSPACE_A), "default")
            store.save(WORKSPACE_A, SESSION_A, history, "sonnet")
            store.save(WORKSPACE_A, SESSION_A, history)
            store.close()
            store = ChatStore(directory)
            try:
                self.assertEqual(store.load_model(WORKSPACE_A), "sonnet")
                self.assertEqual(store.load(WORKSPACE_A), (SESSION_A, history))
                self.assertEqual(store.load_model(WORKSPACE_B), "default")
            finally:
                store.close()


class TranslationClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_translations_never_fall_back_to_plaintext(self):
        client = LocalTranslator("http://translator:8080")
        for output in ({}, {"text": ""}, {"text": "   "}, {"text": 42}, {"text": "x" * 128001}):
            with self.subTest(output_type=type(output.get("text"))), patch.object(client, "_request", return_value=output):
                with self.assertRaises(BrokerError):
                    await client.translate("中文", "zh", "en")

    async def test_health_failure_returns_unavailable_without_leaking_details(self):
        client = LocalTranslator("http://translator:8080")
        with patch.object(client, "_request", side_effect=BrokerError("local unavailable", 503)):
            result = await client.status()
        self.assertFalse(result["ready"])
        self.assertEqual(result["languages"], ["zh", "en"])


class ChatBodyTests(unittest.TestCase):
    def test_approval_body_rejects_substituted_input_and_non_boolean_consent(self):
        from backend.app import PermissionBody
        from pydantic import ValidationError
        for body in ({"allow": True, "input": {"command": "substituted"}},
                     {"allow": "true"}, {"allow": 1}, {"allow": None}):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                PermissionBody(**body)
        self.assertTrue(PermissionBody(allow=True).allow)


class ChatApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from backend.auth import hash_password
        cls.password = "local-chat-test-password"
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        from multiuser_fixture import legacy_app as create_app
        from fastapi.testclient import TestClient
        from multiuser_fixture import attach_ownership
        self.temp = tempfile.TemporaryDirectory()
        self.origin = "http://localhost:8000"
        def registered(workspace):
            if workspace != WORKSPACE_A:
                raise BrokerError("工作区不存在", 404)
            return {"id": WORKSPACE_A}
        self.broker = SimpleNamespace(
            get=Mock(side_effect=registered), list=AsyncMock(return_value=[]),
            apply_model_policy=AsyncMock(), reconcile_admin_auth=AsyncMock(), shared_auth_status=AsyncMock(return_value={"authenticated": True}),
            stop=AsyncMock(), start=AsyncMock(), close=Mock(),
            workspace=AsyncMock(return_value={"id": WORKSPACE_A, "running": True}),
        )
        attach_ownership(self.broker)
        self.translator = SimpleNamespace(
            translate=AsyncMock(return_value="English"),
            status=AsyncMock(return_value={"ready": True, "engine": "local-test", "languages": ["zh", "en"]}),
        )
        self.app = create_app(Config(public_origin=self.origin, password_hash=self.password_hash,
                                     cookie_secure=False, data_dir=Path(self.temp.name)),
                              broker=self.broker, translator=self.translator,
                              login_browsers=SimpleNamespace(reconcile=AsyncMock(), close=AsyncMock(), stop=AsyncMock()))
        self.client = TestClient(self.app, base_url=self.origin)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self):
        response = self.client.post("/api/login", headers={"Origin": self.origin}, json={"username": "owner", "password": self.password})
        self.assertEqual(response.status_code, 200)
        return {"Origin": self.origin, "X-CSRF-Token": response.json()["csrfToken"]}

    def test_translation_and_chat_read_endpoints_require_session(self):
        for path in ("/api/translation/status", f"/api/workspaces/{WORKSPACE_A}/chat"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
        self.translator.status.assert_not_awaited()
        self.broker.get.assert_not_called()
        self.login()
        self.assertEqual(self.client.get("/api/translation/status").status_code, 410)
        self.translator.status.assert_not_awaited()
        self.assertEqual(self.client.get(f"/api/workspaces/{WORKSPACE_A}/chat").json()["messages"], [])

    def test_chat_mutations_require_csrf_and_exact_origin(self):
        headers = self.login()
        chats = self.app.state.runtime["chats"]
        routes = [("message", {"text": "中文"}), ("stop", {}),
                  ("permissions/pending", {"allow": True}), ("messages/example/translate", {})]
        for suffix, body in routes:
            for bad in ({}, {"Origin": self.origin},
                        {**headers, "Origin": "https://evil.example"}):
                with self.subTest(suffix=suffix, headers=bad):
                    response = self.client.post(f"/api/workspaces/{WORKSPACE_A}/chat/{suffix}",
                                                headers=bad, json=body)
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(chats.items, {})
        self.translator.translate.assert_not_awaited()

    def test_model_endpoint_requires_login_csrf_origin_and_registered_workspace(self):
        path = f"/api/workspaces/{WORKSPACE_A}/chat/model"
        self.assertEqual(self.client.put(path, json={"model": "opus"}).status_code, 401)
        headers = self.login()
        chats = self.app.state.runtime["chats"]
        for bad in ({}, {"Origin": self.origin}, {**headers, "Origin": "https://evil.example"}):
            self.assertEqual(self.client.put(path, headers=bad, json={"model": "opus"}).status_code, 403)
        self.assertEqual(chats.items, {})
        self.assertEqual(self.client.put(f"/api/workspaces/{WORKSPACE_B}/chat/model", headers=headers,
                                         json={"model": "opus"}).status_code, 404)
        self.assertEqual(chats.items, {})
        for body in ({"model": "opus", "command": "arbitrary"}, {"model": []}, {"model": 1}):
            self.assertEqual(self.client.put(path, headers=headers, json=body).status_code, 422)
        self.assertEqual(self.client.put(path, headers=headers, json={"model": "unsupported"}).status_code, 400)
        response = self.client.put(path, headers=headers, json={"model": "opus"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selectedModel"], "opus")
        self.assertIsNone(response.json()["activeModel"])
        self.assertEqual(self.client.get(f"/api/workspaces/{WORKSPACE_A}/chat").json()["selectedModel"], "opus")
        self.translator.translate.assert_not_awaited()

    def test_api_rejects_modified_permission_input_before_manager(self):
        headers = self.login()
        chats = self.app.state.runtime["chats"]
        with patch.object(chats, "permission", AsyncMock()) as permission:
            for body in ({"allow": True, "input": {"command": "substitute"}},
                         {"allow": "true"}, {"allow": 1}):
                response = self.client.post(f"/api/workspaces/{WORKSPACE_A}/chat/permissions/request",
                                            headers=headers, json=body)
                self.assertEqual(response.status_code, 422)
            permission.assert_not_awaited()

    def test_unregistered_workspace_does_not_create_chat_storage_entry(self):
        self.login()
        response = self.client.get(f"/api/workspaces/{WORKSPACE_B}/chat")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.app.state.runtime["chats"].items, {})

    def test_restart_reaps_stale_container_and_marks_unfinished_translation_failed(self):
        from multiuser_fixture import legacy_app as create_app
        from fastapi.testclient import TestClient
        from multiuser_fixture import attach_ownership
        with tempfile.TemporaryDirectory() as restart_dir:
            store = ChatStore(restart_dir)
            store.save(WORKSPACE_A, SESSION_A, [{"id": "previous", "role": "user", "text": "中文",
                                               "original": "", "translationStatus": "pending",
                                               "createdAt": "yesterday", "sent": False}])
            store.close()
            stale_broker = SimpleNamespace(
                apply_model_policy=AsyncMock(), reconcile_admin_auth=AsyncMock(), shared_auth_status=AsyncMock(return_value={"authenticated": True}),
                get=self.broker.get, list=AsyncMock(return_value=[{"id": WORKSPACE_A, "running": True}]),
                stop=AsyncMock(), start=AsyncMock(), close=Mock(),
            )
            attach_ownership(stale_broker)
            app = create_app(Config(public_origin=self.origin, password_hash=self.password_hash,
                                    cookie_secure=False, data_dir=Path(restart_dir)),
                             broker=stale_broker, translator=self.translator,
                             login_browsers=SimpleNamespace(reconcile=AsyncMock(), close=AsyncMock(), stop=AsyncMock()))
            with TestClient(app, base_url=self.origin) as client:
                stale_broker.stop.assert_awaited_once_with(WORKSPACE_A)
                client.post("/api/login", headers={"Origin": self.origin}, json={"username": "owner", "password": self.password})
                state = client.get(f"/api/workspaces/{WORKSPACE_A}/chat").json()
                self.assertFalse(state["active"])
                self.assertFalse(state["busy"])
                self.assertEqual(state["messages"][0]["translationStatus"], "failed")
                self.assertFalse(state["messages"][0]["sent"])
                self.translator.translate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

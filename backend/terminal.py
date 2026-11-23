"""Persistent PTY sessions. Websocket disconnect does not kill Claude."""
import asyncio
import codecs
import os
import signal
from collections import deque

from .docker_backend import BrokerError

MAX_REPLAY = 256 * 1024


class Terminal:
    def __init__(self, command: list[str]):
        self.command = command
        self.proc = None
        self.master = None
        self.queue = None
        self.replay = deque()
        self.replay_size = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.watch = None
        self.finished = False

    async def start(self):
        if os.name != "posix":
            raise BrokerError("交互式 Claude 终端需要 Linux Docker 部署", 503)
        import pty
        master, slave = pty.openpty()
        self.master = master
        try:
            self.resize(100, 30)
            self.proc = await asyncio.create_subprocess_exec(*self.command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except BaseException:
            os.close(master)
            self.master = None
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self.read)
        self.watch = asyncio.create_task(self.wait())

    def publish(self, message):
        if self.queue is not None:
            try:
                self.queue.put_nowait(message)
            except asyncio.QueueFull:
                # Disconnect a slow browser without blocking or killing the process.
                while not self.queue.empty():
                    self.queue.get_nowait()
                self.queue.put_nowait({"type": "error", "message": "终端输出过快，请重新连接"})
                self.queue.put_nowait({"type": "disconnect"})
                self.queue = None

    def read(self):
        if self.master is None:
            return
        try:
            data = os.read(self.master, 32768)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.master)
            return
        output = self.filter_output(self.decoder.decode(data))
        if output:
            self.replay.append(output)
            self.replay_size += len(output)
            while self.replay_size > MAX_REPLAY:
                self.replay_size -= len(self.replay.popleft())
            self.publish({"type": "output", "data": output})

    def filter_output(self, output):
        return output

    async def wait(self):
        code = await self.proc.wait()
        self.read()
        self.finished = True
        if self.master is not None:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            self.master = None
        self.publish({"type": "exit", "code": code})

    def attach(self):
        if self.queue is not None:
            raise BrokerError("此工作区已有网页正在控制，请先断开另一个页面", 409)
        queue = asyncio.Queue(maxsize=128)
        self.queue = queue
        queue.put_nowait({"type": "ready"})
        if self.replay:
            queue.put_nowait({"type": "output", "data": "".join(self.replay)})
        return queue

    def detach(self, queue):
        if self.queue is queue:
            self.queue = None

    def resize(self, cols: int, rows: int):
        if self.master is None:
            return
        import fcntl
        import struct
        import termios
        cols, rows = max(20, min(cols, 400)), max(5, min(rows, 150))
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGWINCH)
            except ProcessLookupError:
                pass

    async def input(self, data: str):
        if self.finished or self.master is None:
            return
        raw = data.encode()
        if len(raw) > 65536:
            raise BrokerError("单次终端输入过长")
        while raw and self.master is not None:
            try:
                count = os.write(self.master, raw)
                raw = raw[count:]
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError:
                break

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self.watch:
            await self.watch
        self.publish({"type": "disconnect"})


class Terminals:
    def __init__(self, broker, terminal_factory=Terminal):
        self.broker = broker
        self.items: dict[str, Terminal] = {}
        self.lock = asyncio.Lock()
        self.chat_active = lambda workspace_id: False
        self.require_policy = lambda: None
        self.terminal_factory = terminal_factory

    def active(self, workspace_id: str):
        term = self.items.get(workspace_id)
        return bool(term and not term.finished)

    async def attach(self, workspace_id: str):
        async with self.lock:
            self.require_policy()
            if self.chat_active(workspace_id):
                raise BrokerError("请先停止中文聊天，再连接原始终端；文件、登录和聊天记录会保留", 409)
            term = self.items.get(workspace_id)
            if not term or term.finished:
                # Recycle the container after CLI exit so child processes cannot linger.
                if term:
                    await self.broker.stop(workspace_id)
                    await self.broker.start(workspace_id)
                command = await self.broker.terminal_command(workspace_id)
                term = self.terminal_factory(command)
                await term.start()
                self.items[workspace_id] = term
            return term, term.attach()

    async def stop(self, workspace_id: str, restart: bool = False):
        async with self.lock:
            await self.stop_locked(workspace_id, restart)

    async def stop_locked(self, workspace_id: str, restart: bool = False):
        await self.broker.stop(workspace_id)
        term = self.items.pop(workspace_id, None)
        if term:
            await term.close()
        if restart:
            await self.broker.start(workspace_id)

    async def shutdown(self):
        for workspace_id in list(self.items):
            try:
                await self.stop(workspace_id)
            except BrokerError:
                await self.items[workspace_id].close()

    async def delete(self, workspace_id: str):
        async with self.lock:
            await self.broker.stop(workspace_id)
            term = self.items.pop(workspace_id, None)
            if term:
                await term.close()
            await self.broker.delete(workspace_id)

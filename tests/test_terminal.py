"""Real Linux PTY lifecycle coverage with a harmless local Python child."""
import asyncio
import os
import sys
import unittest

from backend.docker_backend import BrokerError
from backend.terminal import Terminal


class TerminalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_controller_and_stale_detach_cannot_detach_new_one(self):
        terminal = Terminal([])
        first = terminal.attach()
        with self.assertRaises(BrokerError):
            terminal.attach()
        terminal.detach(first)
        second = terminal.attach()
        terminal.detach(first)
        self.assertIs(terminal.queue, second)

    async def test_slow_browser_is_disconnected_without_blocking_output(self):
        terminal = Terminal([])
        queue = terminal.attach()
        for _ in range(200):
            terminal.publish({"type": "output", "data": "x"})
        self.assertIsNone(terminal.queue)
        self.assertEqual((await queue.get())["type"], "error")
        self.assertEqual((await queue.get())["type"], "disconnect")


@unittest.skipUnless(sys.platform.startswith("linux"), "Actual PTY tests require Linux; run CI or Docker test target")
class LinuxPtyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        script = "import sys\nprint('CHILD_READY', flush=True)\nfor line in sys.stdin:\n print('ACK:' + line.rstrip(), flush=True)\n"
        self.terminal = Terminal([sys.executable, "-u", "-c", script])
        await self.terminal.start()

    async def asyncTearDown(self):
        await self.terminal.close()

    async def output_until(self, queue, expected):
        output = ""
        async def collect():
            nonlocal output
            while expected not in output:
                message = await queue.get()
                if message["type"] == "output":
                    output += message["data"]
                elif message["type"] == "exit":
                    self.fail(f"Child exited before expected output: {output!r}")
            return output
        return await asyncio.wait_for(collect(), timeout=5)

    async def test_browser_disconnect_preserves_child_and_reconnect_replays(self):
        first = self.terminal.attach()
        await self.output_until(first, "CHILD_READY")
        pid = self.terminal.proc.pid
        self.terminal.detach(first)
        self.assertIsNone(self.terminal.proc.returncode)
        await self.terminal.input("hello-after-disconnect\n")
        second = self.terminal.attach()
        replay = await self.output_until(second, "ACK:hello-after-disconnect")
        self.assertIn("CHILD_READY", replay)
        self.assertEqual(self.terminal.proc.pid, pid)
        with self.assertRaises(BrokerError):
            self.terminal.attach()

    async def test_resize_updates_pty_dimensions_and_clamps_large_values(self):
        import fcntl
        import struct
        import termios

        self.terminal.resize(120, 40)
        size = fcntl.ioctl(self.terminal.master, termios.TIOCGWINSZ, b"\0" * 8)
        self.assertEqual(struct.unpack("HHHH", size)[:2], (40, 120))
        self.terminal.resize(100000, -100)
        size = fcntl.ioctl(self.terminal.master, termios.TIOCGWINSZ, b"\0" * 8)
        self.assertEqual(struct.unpack("HHHH", size)[:2], (5, 400))

    async def test_close_reaps_process_and_releases_descriptor(self):
        queue = self.terminal.attach()
        await self.output_until(queue, "CHILD_READY")
        master = self.terminal.master
        await asyncio.wait_for(self.terminal.close(), timeout=5)
        self.assertIsNotNone(self.terminal.proc.returncode)
        self.assertTrue(self.terminal.finished)
        self.assertIsNone(self.terminal.master)
        with self.assertRaises(OSError):
            os.fstat(master)


if __name__ == "__main__":
    unittest.main()

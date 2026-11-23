"""Linux file-boundary tests plus portable input validation tests.

Run the full suite in Linux/Docker: Windows cannot exercise dir_fd/O_NOFOLLOW.
"""
import base64
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend import file_helper as files


class PathValidationTests(unittest.TestCase):
    def test_relative_names_and_unicode_are_supported(self):
        self.assertEqual(files.parts("src/你好.py"), ["src", "你好.py"])
        self.assertEqual(files.parts("", allow_root=True), [])

    def test_escape_ambiguous_and_non_string_paths_are_rejected(self):
        for path in ("", "/", "/etc/passwd", "../secret", "a/../secret", "./a", "a//b", "a/", "C:/Windows", "a\\b", "a\x00b", ".", "..", "a" * 256, "中" * 86, None, 123, ["a"]):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.parts(path)

    def test_special_files_and_hardlinks_are_rejected(self):
        for mode, links in ((stat.S_IFLNK, 1), (stat.S_IFIFO, 1), (stat.S_IFDIR, 1), (stat.S_IFREG, 2)):
            with self.subTest(mode=mode, links=links), self.assertRaises(ValueError):
                files.check_regular(SimpleNamespace(st_mode=mode, st_nlink=links))
        files.check_regular(SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1))

    def test_non_object_protocol_input_is_rejected_before_filesystem(self):
        for value in (None, [], "hello", 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                files.execute(value)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "Linux dir_fd/O_NOFOLLOW required; run this suite in the Docker test image")
class FileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_text("private", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def call(self, op, path="", **kwargs):
        return files.execute({"op": op, "path": path, **kwargs}, str(self.root))

    def test_create_read_edit_and_delete_own_files(self):
        self.call("mkdir", "src")
        self.call("write", "src/你好.txt", content="hello 世界")
        result = self.call("read", "src/你好.txt")
        self.assertEqual(result["content"], "hello 世界")
        self.assertEqual(result["encoding"], "utf-8")
        self.call("write", "src/你好.txt", content="updated")
        self.assertEqual(self.call("read", "src/你好.txt")["content"], "updated")
        self.assertEqual(self.call("list")["entries"][0]["name"], "src")
        self.call("delete", "src")
        self.assertFalse((self.root / "src").exists())

    def test_binary_round_trip(self):
        data = b"\x00\xff\x80\x01"
        self.call("write", "image.bin", encoding="base64", content=base64.b64encode(data).decode())
        result = self.call("read", "image.bin")
        self.assertEqual(result["encoding"], "base64")
        self.assertEqual(base64.b64decode(result["content"]), data)

    def test_edit_preserves_executable_bits_but_not_privileged_bits(self):
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o6755)
        self.call("write", "run.sh", content="#!/bin/sh\nexit 1\n")
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)

    def test_symlink_file_and_parent_never_followed(self):
        (self.root / "shortcut").symlink_to(self.outside, target_is_directory=True)
        (self.root / "secret-link").symlink_to(self.outside / "secret")
        for op, path in (("read", "secret-link"), ("write", "secret-link"), ("read", "shortcut/secret"), ("write", "shortcut/secret"), ("mkdir", "shortcut/new"), ("list", "shortcut")):
            with self.subTest(op=op, path=path), self.assertRaises((OSError, ValueError)):
                self.call(op, path, content="changed")
        self.assertEqual((self.outside / "secret").read_text(), "private")
        self.assertEqual({e["type"] for e in self.call("list")["entries"]}, {"blocked"})

    def test_deleting_symlink_removes_link_not_target(self):
        self.call("mkdir", "tree")
        (self.root / "tree" / "shortcut").symlink_to(self.outside, target_is_directory=True)
        self.call("delete", "tree")
        self.assertEqual((self.outside / "secret").read_text(), "private")

    def test_hardlink_cannot_be_read_or_overwritten(self):
        os.link(self.outside / "secret", self.root / "hardlink")
        for op in ("read", "write"):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.call(op, "hardlink", content="changed")
        self.assertEqual((self.outside / "secret").read_text(), "private")

    def test_fifo_does_not_block_read_or_allow_write(self):
        os.mkfifo(self.root / "pipe")
        for op in ("read", "write"):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.call(op, "pipe", content="changed")

    def test_workspace_root_cannot_be_deleted(self):
        for path in ("", ".", "..", "/"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call("delete", path)
        self.assertTrue(self.root.is_dir())

    def test_oversized_read_and_write_are_refused(self):
        with patch.object(files, "MAX_FILE_SIZE", 10):
            with self.assertRaises(ValueError):
                self.call("write", "large", content="x" * 11)
            (self.root / "large").write_bytes(b"x" * 11)
            with self.assertRaises(ValueError):
                self.call("read", "large")

    def test_invalid_base64_does_not_create_file(self):
        with self.assertRaises(ValueError):
            self.call("write", "invalid", encoding="base64", content="???")
        self.assertFalse((self.root / "invalid").exists())

    def test_failed_atomic_save_keeps_original_and_cleans_temporary(self):
        (self.root / "original").write_text("before")
        with patch.object(files.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.call("write", "original", content="after")
        self.assertEqual((self.root / "original").read_text(), "before")
        self.assertEqual([p.name for p in self.root.iterdir()], ["original"])

    def test_replaced_target_symlink_is_not_followed_at_atomic_commit(self):
        target = self.root / "original"
        target.write_text("before")
        real_replace = os.replace

        def race_replace(*args, **kwargs):
            target.unlink()
            target.symlink_to(self.outside / "secret")
            return real_replace(*args, **kwargs)

        with patch.object(files.os, "replace", side_effect=race_replace):
            self.call("write", "original", content="after")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), "after")
        self.assertEqual((self.outside / "secret").read_text(), "private")


if __name__ == "__main__":
    unittest.main()

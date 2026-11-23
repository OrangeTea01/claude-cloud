"""ZIP protocol and Linux descriptor-boundary tests, without Docker or cloud."""
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from backend import archive_helper as archive


class ArchivePortableTests(unittest.TestCase):
    def test_safe_names_include_unicode_and_hidden_files(self):
        for name in ("README.md", ".env", ".git", "截图 中文.png", "package-lock.json"):
            archive.safe_component(name)
        for name in ("", ".", "..", "/etc", "a/b", "..\\secret", "C:secret", "a\x00b",
                     "a\nb", "a\x7fb", "space ", "dot.", "NUL", "con.txt", "COM1.log",
                     "lpt².txt", "CONOUT$", "a?b", 'a"b', "a|b", "a<b", "a" * 256, "中" * 86, "\udcff", None):
            with self.subTest(name=repr(name)), self.assertRaises(archive.ArchiveError):
                archive.safe_component(name)

    def test_size_count_depth_and_metadata_limits_are_independent(self):
        cases = [("MAX_RAW_BYTES", 3, [("a", 4, 1)]),
                 ("MAX_ENTRIES", 1, [("a", 0, 1), ("b", 0, 1)]),
                 ("MAX_DEPTH", 1, [("a/b", 0, 2)]),
                 ("MAX_NAME_BYTES", 2, [("中文", 0, 1)])]
        for constant, limit, entries in cases:
            with self.subTest(constant=constant), patch.object(archive, constant, limit):
                budget = archive.Budget()
                with self.assertRaises(archive.ArchiveError):
                    for entry in entries:
                        budget.add(*entry)
        budget = archive.Budget()
        budget.add("exact", archive.MAX_RAW_BYTES, archive.MAX_DEPTH)

    def test_output_bound_and_partial_output_writes(self):
        class ShortWriter(io.BytesIO):
            def write(self, value):
                return super().write(value[:2])
        output = ShortWriter()
        writer = archive.LimitedWriter(output)
        with patch.object(archive, "MAX_ZIP_BYTES", 4):
            self.assertEqual(writer.write(b"abcd"), 4)
            with self.assertRaises(archive.ArchiveError):
                writer.write(b"e")
        self.assertEqual(output.getvalue(), b"abcd")
        self.assertEqual(writer.tell(), 4)
        with self.assertRaises(OSError):
            archive.LimitedWriter(SimpleNamespace(write=lambda value: 0)).write(b"x")

    def test_unseekable_writer_produces_complete_zip_and_preserves_link_mode(self):
        output = io.BytesIO()
        info = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_mtime=0)
        with zipfile.ZipFile(archive.LimitedWriter(output), "w") as zipped:
            zipped.writestr(archive.zip_info("link", info, "link", 9), b"../secret")
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as zipped:
            self.assertEqual(zipped.read("link"), b"../secret")
            self.assertTrue(stat.S_ISLNK(zipped.getinfo("link").external_attr >> 16))
            self.assertIsNone(zipped.testzip())

    def test_hardlinks_special_files_and_other_devices_are_rejected(self):
        for mode, links, device in ((stat.S_IFREG, 2, 1), (stat.S_IFLNK, 2, 1),
                                    (stat.S_IFIFO, 1, 1), (stat.S_IFSOCK, 1, 1),
                                    (stat.S_IFDIR, 2, 2)):
            with self.subTest(mode=mode, links=links, device=device), self.assertRaises(archive.ArchiveError):
                archive.safe_type(SimpleNamespace(st_mode=mode, st_nlink=links, st_dev=device), 1)

    def run_main(self, error=None):
        stdout, stderr = io.BytesIO(), io.StringIO()
        signals = SimpleNamespace(SIGALRM=14, signal=Mock(), alarm=Mock())
        stream = SimpleNamespace(buffer=stdout, fileno=Mock(side_effect=OSError("closed")))
        with patch.object(archive, "signal", signals), patch.object(archive, "export", side_effect=error), \
                patch.object(archive.sys, "stdout", stream), patch.object(archive.sys, "stderr", stderr):
            result = archive.main()
        self.assertEqual(signals.alarm.call_args_list[0].args, (180,))
        self.assertEqual(signals.alarm.call_args_list[-1].args, (0,))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_protocol_errors_are_allowlisted_without_private_exception_text(self):
        for error in (PermissionError("secret/path/password"), zipfile.LargeZipFile("private"),
                      archive.ArchiveError("工作区打包超时，请缩小工作区后重试"), BrokenPipeError("secret")):
            with self.subTest(error=type(error).__name__):
                result, stdout, stderr = self.run_main(error)
                self.assertEqual(result, 1)
                self.assertEqual(stdout, b"")
                self.assertIn(stderr.strip(), archive.ERROR_MESSAGES)
                self.assertNotIn("secret", stderr)
                self.assertNotIn("private", stderr)

    def test_success_and_timeout_handler(self):
        self.assertEqual(self.run_main(), (0, b"", ""))
        with self.assertRaises(archive.ArchiveError) as raised:
            archive.timed_out(None, None)
        self.assertIn(str(raised.exception), archive.ERROR_MESSAGES)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
                     "Linux dir_fd/O_NOFOLLOW required; run in the Docker test image")
class ArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_bytes(b"OUTSIDE_PRIVATE_CONTENT")

    def tearDown(self):
        self.temporary.cleanup()

    def export(self):
        output = io.BytesIO()
        archive.export(output, str(self.root))
        return output.getvalue()

    def test_empty_workspace_produces_valid_empty_archive(self):
        with zipfile.ZipFile(io.BytesIO(self.export())) as zipped:
            self.assertEqual(zipped.namelist(), [])

    def test_all_files_unicode_binary_hidden_and_empty_directories_are_preserved(self):
        (self.root / "empty").mkdir()
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_bytes(b"hidden")
        data = bytes(range(256)) * 1000
        (self.root / "截图 中文.bin").write_bytes(data)
        (self.root / "run.sh").write_bytes(b"#!/bin/sh\n")
        (self.root / "run.sh").chmod(0o6755)
        before = set(self.root.iterdir())
        with zipfile.ZipFile(io.BytesIO(self.export())) as zipped:
            self.assertEqual(set(zipped.namelist()), {"empty/", ".git/", ".git/config", "截图 中文.bin", "run.sh"})
            self.assertEqual(zipped.read("截图 中文.bin"), data)
            self.assertEqual(zipped.read(".git/config"), b"hidden")
            self.assertEqual(stat.S_IMODE(zipped.getinfo("run.sh").external_attr >> 16), 0o755)
            self.assertIsNone(zipped.testzip())
        self.assertEqual(set(self.root.iterdir()), before)

    def test_symlinks_preserve_only_link_text_and_never_follow_targets(self):
        links = {"absolute-link": str(self.outside / "secret"), "directory-link": str(self.outside),
                 "relative-link": "../outside/secret", "dangling": "missing"}
        for name, target in links.items():
            (self.root / name).symlink_to(target)
        with zipfile.ZipFile(io.BytesIO(self.export())) as zipped:
            self.assertEqual(set(zipped.namelist()), set(links))
            for name, target in links.items():
                self.assertEqual(zipped.read(name), os.fsencode(target))
                self.assertTrue(stat.S_ISLNK(zipped.getinfo(name).external_attr >> 16))

    def test_hardlink_to_outside_is_rejected_before_reading(self):
        os.link(self.outside / "secret", self.root / "hardlink")
        with patch.object(archive.os, "read", wraps=os.read) as read:
            with self.assertRaises(archive.ArchiveError):
                self.export()
            read.assert_not_called()

    def test_special_file_is_rejected_without_blocking(self):
        os.mkfifo(self.root / "fifo")
        with self.assertRaises(archive.ArchiveError):
            self.export()

    def test_symlink_workspace_root_is_rejected(self):
        link = self.base / "root-link"
        link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError):
            archive.export(io.BytesIO(), str(link))

    def test_regular_file_swap_to_symlink_cannot_read_outside(self):
        target = self.root / "file"
        target.write_bytes(b"inside")
        original = archive.open_child
        def swap(parent, name, directory=False):
            target.unlink()
            target.symlink_to(self.outside / "secret")
            return original(parent, name, directory)
        with patch.object(archive, "open_child", side_effect=swap), patch.object(archive.os, "read", wraps=os.read) as read:
            with self.assertRaises(OSError):
                self.export()
            read.assert_not_called()

    def test_directory_swap_to_symlink_cannot_traverse_outside(self):
        target = self.root / "directory"
        target.mkdir()
        original = archive.open_child
        def swap(parent, name, directory=False):
            target.rmdir()
            target.symlink_to(self.outside, target_is_directory=True)
            return original(parent, name, directory)
        with patch.object(archive, "open_child", side_effect=swap), self.assertRaises(OSError):
            self.export()

    def test_file_mutation_during_read_is_rejected(self):
        target = self.root / "changing"
        target.write_bytes(b"before")
        original, changed = os.read, False
        def mutate(fd, size):
            nonlocal changed
            result = original(fd, size)
            if result and not changed:
                changed = True
                target.write_bytes(b"different content")
            return result
        with patch.object(archive.os, "read", side_effect=mutate), self.assertRaises(archive.ArchiveError):
            self.export()

    def test_final_verification_rejects_changed_previously_read_file(self):
        target = self.root / "changing"
        target.write_bytes(b"before")
        class MutatingOutput(io.BytesIO):
            def flush(self):
                target.write_bytes(b"after!")
                super().flush()
        with self.assertRaises(archive.ArchiveError):
            archive.export(MutatingOutput(), str(self.root))

    def test_unreadable_file_and_unsafe_filename_fail_whole_export(self):
        (self.root / "private").write_bytes(b"private")
        with patch.object(archive, "open_child", side_effect=PermissionError("do not echo")), self.assertRaises(OSError):
            self.export()
        (self.root / "private").unlink()
        (self.root / "..\\outside").write_bytes(b"unsafe name")
        with self.assertRaises(archive.ArchiveError):
            self.export()

    def test_actual_traversal_limits(self):
        (self.root / "one").write_bytes(b"1234")
        for name, limit in (("MAX_RAW_BYTES", 3), ("MAX_ZIP_BYTES", 20), ("MAX_ENTRIES", 0)):
            with self.subTest(name=name), patch.object(archive, name, limit), self.assertRaises(archive.ArchiveError):
                self.export()
        (self.root / "nested").mkdir()
        (self.root / "nested" / "file").write_bytes(b"x")
        with patch.object(archive, "MAX_DEPTH", 1), self.assertRaises(archive.ArchiveError):
            self.export()

    def test_streaming_reads_are_bounded_for_large_binary(self):
        data = os.urandom(archive.CHUNK_SIZE * 3 + 19)
        (self.root / "binary").write_bytes(data)
        with patch.object(archive.os, "read", wraps=os.read) as read:
            result = self.export()
            self.assertLessEqual(max(call.args[1] for call in read.call_args_list), archive.CHUNK_SIZE)
        with zipfile.ZipFile(io.BytesIO(result)) as zipped:
            self.assertEqual(zipped.read("binary"), data)

    def test_injected_protocol_stdout_is_only_zip_and_errors_are_nonzero(self):
        source = Path(archive.__file__).read_text(encoding="utf-8")
        source = source.replace('ROOT = "/workspace"', "ROOT = " + repr(str(self.root)), 1)
        (self.root / "普通.txt").write_bytes(b"complete")
        result = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as zipped:
            self.assertEqual(zipped.read("普通.txt"), b"complete")
        os.link(self.outside / "secret", self.root / "hardlink")
        failed = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, timeout=10)
        self.assertEqual(failed.returncode, 1)
        self.assertIn(failed.stderr.decode().strip(), archive.ERROR_MESSAGES)


if __name__ == "__main__":
    unittest.main()

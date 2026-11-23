"""Resource profiles must never reset credentials or overwrite concurrent edits."""
import os
import io
from contextlib import redirect_stdout
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.configure_multiuser import ConfigError, DEFAULT_PROFILE, PROFILES, configure_multiuser, main, render_env
from scripts.enable_https import read_regular


HASH = b"ADMIN_PASSWORD_HASH='scrypt$16384$8$1$KEEP_SALT$KEEP_HASH' # retain exactly\r\n"
ADMIN = b"MANAGEMENT_PASSWORD_HASH='fixed$private$hash'\r\n"
BASE = (b"# existing configuration\r\n" + HASH + ADMIN + b"PUBLIC_ORIGIN=https://example.org\r\n"
        b"COOKIE_SECURE=true\r\nCOMPOSE_PROFILES=https\r\nCUSTOM='keep $AS_IS'\r\n"
        b"WORKSPACE_MEMORY=4g\r\nWORKSPACE_CPUS=2\r\nMAX_WORKSPACES=3\r\nMAX_RUNNING_WORKSPACES=1\r\n")


class ResourceProfileTests(unittest.TestCase):
    def test_default_24gb_profile_and_explicit_smaller_server_profiles(self):
        expected = {
            "24g": {"WORKSPACE_MEMORY": "3g", "WORKSPACE_CPUS": "1.5", "MAX_WORKSPACES": "30", "MAX_RUNNING_WORKSPACES": "5"},
            "8g": {"WORKSPACE_MEMORY": "1536m", "WORKSPACE_CPUS": "1", "MAX_WORKSPACES": "20", "MAX_RUNNING_WORKSPACES": "3"},
            "12g": {"WORKSPACE_MEMORY": "2g", "WORKSPACE_CPUS": "1", "MAX_WORKSPACES": "20", "MAX_RUNNING_WORKSPACES": "3"},
        }
        self.assertEqual(PROFILES, expected)
        self.assertEqual(DEFAULT_PROFILE, "24g")
        self.assertEqual(render_env(BASE), render_env(BASE, "24g"))
        for arguments, profile in (([], "24g"), (["--profile", "8g"], "8g"), (["--profile", "12g"], "12g")):
            with patch("scripts.configure_multiuser.configure_multiuser", return_value=(profile, False)) as configure, redirect_stdout(io.StringIO()):
                main(arguments)
            self.assertEqual(configure.call_args.args[1], profile)

    def test_backend_defaults_match_profile_without_overriding_explicit_environment(self):
        from backend.docker_backend import Config

        with patch.dict(os.environ, {}, clear=True):
            initial = Config()
            from_env = Config.from_env()
        for config in (initial, from_env):
            self.assertEqual((config.memory, config.cpus, config.max_workspaces, config.max_running), ("3g", "1.5", 30, 5))
        with patch.dict(os.environ, PROFILES["8g"], clear=True):
            smaller = Config.from_env()
        self.assertEqual((smaller.memory, smaller.cpus, smaller.max_workspaces, smaller.max_running), ("1536m", "1", 20, 3))

    def test_changes_only_resource_lines_and_preserves_secret_quoting(self):
        original_unowned = [line for line in BASE.splitlines(keepends=True) if not line.startswith(tuple(key.encode() + b"=" for key in PROFILES["8g"]))]
        for profile in PROFILES:
            with self.subTest(profile=profile):
                updated = render_env(BASE, profile)
                unowned = [line for line in updated.splitlines(keepends=True) if not line.startswith(tuple(key.encode() + b"=" for key in PROFILES[profile]))]
                self.assertEqual(unowned, original_unowned)
                for key, value in PROFILES[profile].items():
                    self.assertIn(f"{key}={value}\r\n".encode(), updated)
                self.assertEqual(render_env(updated, profile), updated)

    def test_adds_missing_keys_without_corrupting_bom_or_missing_final_newline(self):
        original = b"\xef\xbb\xbf" + HASH + ADMIN.rstrip(b"\r\n")
        updated = render_env(original)
        self.assertTrue(updated.startswith(b"\xef\xbb\xbf" + HASH + ADMIN))
        self.assertIn(b"MAX_RUNNING_WORKSPACES=5\r\n", updated)
        self.assertEqual(render_env(updated), updated)

    def test_rejects_ambiguous_duplicate_multiline_and_missing_credentials(self):
        bad = [BASE + b"MAX_WORKSPACES=50\r\n", BASE + ADMIN, BASE + HASH,
               BASE + b"CUSTOM='multiline\nMAX_WORKSPACES=999\n'\n",
               BASE + b"export WORKSPACE_CPUS\n", b"WORKSPACE_MEMORY=4g\n", BASE + b"\x00"]
        for original in bad:
            with self.subTest(original=original), self.assertRaises(ConfigError):
                render_env(original)
        with self.assertRaises(ConfigError):
            render_env(BASE, "24g\nSECRET=changed")

    def test_first_backup_survives_repeated_profile_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(BASE)
            self.assertEqual(configure_multiuser(path), ("24g", True))
            backup = path.with_name(".env.multiuser-backup")
            self.assertEqual(backup.read_bytes(), BASE)
            self.assertEqual(configure_multiuser(path), ("24g", False))
            self.assertEqual(configure_multiuser(path, "8g"), ("8g", True))
            self.assertEqual(backup.read_bytes(), BASE)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_concurrent_edit_is_preserved_and_temporary_file_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(BASE)
            count = 0

            def racing_read(target):
                nonlocal count
                count += 1
                if count == 2:
                    path.write_bytes(BASE + b"CHANGED_BY_OWNER=yes\n")
                return read_regular(target)

            with patch("scripts.configure_multiuser.read_regular", side_effect=racing_read):
                with self.assertRaisesRegex(ConfigError, "concurrently"):
                    configure_multiuser(path)
            self.assertIn(b"CHANGED_BY_OWNER=yes", path.read_bytes())
            self.assertEqual({item.name for item in path.parent.iterdir()}, {".env", ".env.multiuser-backup"})

    def test_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(BASE)
            with patch("scripts.configure_multiuser.os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    configure_multiuser(path)
            self.assertEqual(path.read_bytes(), BASE)
            self.assertEqual({item.name for item in path.parent.iterdir()}, {".env", ".env.multiuser-backup"})

    def test_rejects_directory_or_hardlinked_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(ConfigError):
                configure_multiuser(path)
            original = path / "source"
            original.write_bytes(BASE)
            linked = path / ".env"
            os.link(original, linked)
            with self.assertRaises(ConfigError):
                configure_multiuser(linked)
            self.assertEqual(original.read_bytes(), BASE)

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic links")
    def test_rejects_symbolic_configuration_or_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "source"
            original.write_bytes(BASE)
            path = base / ".env"
            path.symlink_to(original)
            with self.assertRaises(ConfigError):
                configure_multiuser(path)
            path.unlink()
            path.write_bytes(BASE)
            path.with_name(".env.multiuser-backup").symlink_to(original)
            with self.assertRaises(ConfigError):
                configure_multiuser(path)
            self.assertEqual(path.read_bytes(), BASE)
            self.assertEqual(original.read_bytes(), BASE)


if __name__ == "__main__":
    unittest.main()

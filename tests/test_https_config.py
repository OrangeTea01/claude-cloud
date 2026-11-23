"""Public HTTPS config updates must preserve login secrets and existing data."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.enable_https import ConfigError, enable_https, public_target, render_env


HASH_LINE = b"ADMIN_PASSWORD_HASH='scrypt$16384$8$1$KEEP_THIS_SALT$KEEP_THIS_HASH' # untouched\r\n"
ORIGINAL = b"# personal deployment\r\n" + HASH_LINE + b"PUBLIC_ORIGIN=http://localhost:8000\r\nCOOKIE_SECURE=false\r\nDOMAIN=localhost\r\nWORKSPACE_MEMORY=4g\r\nCUSTOM_VALUE='keep $VALUE exactly'\r\n"


class PublicTargetTests(unittest.TestCase):
    def test_normalizes_public_ipv4_ipv6_and_domain(self):
        self.assertEqual(public_target("1.1.1.1"), ("1.1.1.1", "https://1.1.1.1"))
        self.assertEqual(public_target("[2606:4700:4700:0:0:0:0:1111]"), ("2606:4700:4700::1111", "https://[2606:4700:4700::1111]"))
        self.assertEqual(public_target("Claude.Example.COM."), ("claude.example.com", "https://claude.example.com"))

    def test_rejects_local_addresses_and_config_injection(self):
        for target in ("", "127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.1.1", "224.0.0.1", "::1", "fc00::1", "fe80::1", "::ffff:8.8.8.8", "fe80::1%eth0", "localhost", "server.local", "https://example.com", "user@example.com", "example.com:8443", "example.com/x", "example.com\nCOOKIE_SECURE=false", "${HOST}", "a\".com", "-a.com", "a..com", "999.1.1.1", "[8.8.8.8]"):
            with self.subTest(target=target), self.assertRaises(ConfigError):
                public_target(target)


class EnvUpdateTests(unittest.TestCase):
    def test_preserves_hash_and_unknown_lines_exactly(self):
        updated, origin = render_env(ORIGINAL, "1.1.1.1")
        self.assertEqual(origin, "https://1.1.1.1")
        self.assertIn(HASH_LINE, updated)
        self.assertIn(b"CUSTOM_VALUE='keep $VALUE exactly'\r\n", updated)
        self.assertIn(b"WORKSPACE_MEMORY=4g\r\n", updated)
        self.assertIn(b"COMPOSE_PROFILES=https\r\n", updated)
        self.assertIn(b"COOKIE_SECURE=true\r\n", updated)
        self.assertEqual(render_env(updated, "1.1.1.1")[0], updated)

    def test_merges_existing_profiles_and_preserves_bom(self):
        original = b"\xef\xbb\xbf" + ORIGINAL + b'COMPOSE_PROFILES="metrics,archive" # user profiles\r\n'
        updated, _ = render_env(original, "claude.example.com")
        self.assertTrue(updated.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"COMPOSE_PROFILES=metrics,archive,https\r\n", updated)

    def test_rejects_ambiguous_or_missing_values(self):
        for original in (b"PUBLIC_ORIGIN=http://localhost:8000\n", ORIGINAL + b"DOMAIN=other.example.com\n", ORIGINAL + b"COMPOSE_PROFILES=${OTHER}\n", ORIGINAL + b"CUSTOM='multiline\nDOMAIN=inside\n'\n"):
            with self.subTest(original=original), self.assertRaises(ConfigError):
                render_env(original, "1.1.1.1")

    def test_atomic_update_is_idempotent_and_keeps_first_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(ORIGINAL)
            _, changed = enable_https(path, "1.1.1.1")
            self.assertTrue(changed)
            first = path.read_bytes()
            backup = path.with_name(".env.https-backup")
            self.assertEqual(backup.read_bytes(), ORIGINAL)
            self.assertFalse(enable_https(path, "1.1.1.1")[1])
            self.assertEqual(path.read_bytes(), first)
            enable_https(path, "claude.example.com")
            self.assertEqual(backup.read_bytes(), ORIGINAL)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_replace_failure_leaves_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(ORIGINAL)
            with patch("scripts.enable_https.os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    enable_https(path, "1.1.1.1")
            self.assertEqual(path.read_bytes(), ORIGINAL)
            self.assertEqual(set(p.name for p in path.parent.iterdir()), {".env", ".env.https-backup"})

    def test_refuses_directory_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                enable_https(Path(directory), "1.1.1.1")

    @unittest.skipUnless(os.name == "posix", "POSIX link permissions")
    def test_refuses_symlink_config_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "original"
            original.write_bytes(ORIGINAL)
            path = base / ".env"
            path.symlink_to(original)
            with self.assertRaises(ConfigError):
                enable_https(path, "1.1.1.1")
            path.unlink()
            path.write_bytes(ORIGINAL)
            path.with_name(".env.https-backup").symlink_to(original)
            with self.assertRaises(ConfigError):
                enable_https(path, "1.1.1.1")
            self.assertEqual(original.read_bytes(), ORIGINAL)
            self.assertEqual(path.read_bytes(), ORIGINAL)


if __name__ == "__main__":
    unittest.main()

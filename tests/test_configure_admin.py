"""Administrator setup must preserve a deployed site's existing configuration."""
import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings

from backend.auth import verify_password
from scripts import configure_admin as setup


PASSWORD = "Local test admin passphrase 123"
ORIGINAL = (
    b"# deployed site\r\n"
    b"ADMIN_PASSWORD_HASH='scrypt$16384$8$1$KEEP_SALT$KEEP_HASH' # unchanged\r\n"
    b"PUBLIC_ORIGIN=https://claude.example.com\r\n"
    b"COOKIE_SECURE=true\r\nCOMPOSE_PROFILES=https,metrics\r\n"
    b"WORKSPACE_MEMORY=4g\r\nCUSTOM_VALUE='keep $VALUE literally'\r\n"
)


class AdministratorSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"
        self.path.write_bytes(ORIGINAL)

    def configure(self, values=(PASSWORD, PASSWORD)):
        with patch.object(setup.getpass, "getpass", side_effect=values):
            setup.configure_admin(self.path)

    def test_preserves_existing_config_and_writes_only_quoted_hash(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.configure()
        updated = self.path.read_bytes()
        self.assertTrue(updated.startswith(ORIGINAL))
        self.assertNotIn(PASSWORD.encode(), updated)
        addition = updated[len(ORIGINAL):].decode()
        self.assertTrue(addition.startswith("MANAGEMENT_PASSWORD_HASH='scrypt$"))
        self.assertTrue(addition.endswith("'\r\n"))
        encoded = addition.split("=", 1)[1].strip()[1:-1]
        self.assertTrue(verify_password(PASSWORD, encoded))
        self.assertFalse(verify_password(PASSWORD + "!", encoded))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {".env"})

    def test_existing_blank_setting_and_bom_keep_other_bytes(self):
        for empty in (b"MANAGEMENT_PASSWORD_HASH=\r\n", b" export MANAGEMENT_PASSWORD_HASH = '' # disabled\r\n",
                      b"MANAGEMENT_PASSWORD_HASH= # disabled\r\n"):
            with self.subTest(empty=empty):
                before = b"\xef\xbb\xbf" + ORIGINAL + empty + b"TAIL=keep"
                self.path.write_bytes(before)
                self.configure()
                updated = self.path.read_bytes()
                self.assertTrue(updated.startswith(b"\xef\xbb\xbf" + ORIGINAL))
                self.assertTrue(updated.endswith(b"TAIL=keep"))
                self.assertEqual(updated.count(b"MANAGEMENT_PASSWORD_HASH="), 1)

    def test_existing_nonempty_admin_setting_is_never_overwritten_or_prompted(self):
        self.configure()
        configured = self.path.read_bytes()
        with patch.object(setup.getpass, "getpass") as prompt:
            with self.assertRaisesRegex(setup.ConfigError, "already configured"):
                setup.configure_admin(self.path)
            prompt.assert_not_called()
        self.assertEqual(self.path.read_bytes(), configured)

    def test_invalid_lengths_and_mismatch_do_not_modify_configuration(self):
        for values in (("short",), ("x" * 1025,), (PASSWORD, PASSWORD + "different")):
            with self.subTest(length=len(values[0])):
                with self.assertRaises(setup.ConfigError):
                    self.configure(values)
                self.assertEqual(self.path.read_bytes(), ORIGINAL)
                self.assertEqual({p.name for p in self.path.parent.iterdir()}, {".env"})

    def test_cancelled_prompt_and_missing_private_terminal_leave_original(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), patch.object(setup.getpass, "getpass", side_effect=error):
                with self.assertRaises(type(error)):
                    setup.configure_admin(self.path)
                self.assertEqual(self.path.read_bytes(), ORIGINAL)
        def no_terminal(_):
            warnings.warn("fallback is unsafe", setup.getpass.GetPassWarning)
            return PASSWORD
        with patch.object(setup.getpass, "getpass", side_effect=no_terminal):
            with self.assertRaises(setup.getpass.GetPassWarning):
                setup.configure_admin(self.path)
        self.assertEqual(self.path.read_bytes(), ORIGINAL)

    def test_invalid_and_ambiguous_env_is_rejected_before_password_prompt(self):
        for contents in (
            b"PUBLIC_ORIGIN=https://claude.example.com\n",
            ORIGINAL + b"MANAGEMENT_PASSWORD_HASH=\nMANAGEMENT_PASSWORD_HASH=''\n",
            ORIGINAL + b"ADMIN_PASSWORD_HASH='duplicate'\n",
            ORIGINAL + b"MANAGEMENT_PASSWORD_HASH 'missing equals'\n",
            ORIGINAL + b"CUSTOM='multiline\nMANAGEMENT_PASSWORD_HASH=inside\n'\n",
            ORIGINAL + b"MANAGEMENT_PASSWORD_HASH='unterminated\n",
            ORIGINAL + b"NULL=\x00\n",
            ORIGINAL + b"INVALID=\xff\n",
        ):
            with self.subTest(contents=contents[-50:]):
                self.path.write_bytes(contents)
                with patch.object(setup.getpass, "getpass") as prompt:
                    with self.assertRaises(setup.ConfigError):
                        setup.configure_admin(self.path)
                    prompt.assert_not_called()
                self.assertEqual(self.path.read_bytes(), contents)

    def test_missing_and_non_regular_env_do_not_prompt(self):
        with patch.object(setup.getpass, "getpass") as prompt:
            for path in (self.path.with_name("missing"), self.path.parent):
                with self.subTest(path=path.name), self.assertRaises(setup.ConfigError):
                    setup.configure_admin(path)
            prompt.assert_not_called()

    def test_replace_failure_leaves_original_and_removes_temporary(self):
        with patch.object(setup.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self.configure()
        self.assertEqual(self.path.read_bytes(), ORIGINAL)
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {".env"})

    def test_concurrent_edit_is_not_overwritten(self):
        updated = ORIGINAL + b"CONCURRENT=preserve\r\n"
        calls = 0
        def prompt(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.path.write_bytes(updated)
            return PASSWORD
        with patch.object(setup.getpass, "getpass", side_effect=prompt):
            with self.assertRaisesRegex(setup.ConfigError, "concurrently"):
                setup.configure_admin(self.path)
        self.assertEqual(self.path.read_bytes(), updated)
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {".env"})

    def test_hash_renderer_rejects_injected_or_untrusted_values(self):
        for value in ("plaintext", "'\nCOOKIE_SECURE=false", "scrypt$1048576$8$1$aa$bb"):
            with self.subTest(value=value), self.assertRaises(setup.ConfigError):
                setup.render_env(ORIGINAL, value)

    def test_direct_script_imports_and_help_work_without_configuration(self):
        script = Path(setup.__file__).resolve()
        result = subprocess.run([sys.executable, str(script), "--help"], cwd=self.directory.name,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("fixed administrator password", result.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions and links")
    def test_owner_only_permissions_and_link_refusal(self):
        self.configure()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        for link_type in ("symlink", "hardlink"):
            target = self.path.with_name(link_type)
            if link_type == "symlink":
                target.symlink_to(self.path)
            else:
                os.link(self.path, target)
            with self.subTest(link_type=link_type), self.assertRaises(setup.ConfigError):
                setup.configure_admin(target)
            target.unlink()


if __name__ == "__main__":
    unittest.main()

"""Fresh setup creates private configuration, not a default dashboard account."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.auth import valid_password_hash, verify_password
from scripts import configure
from scripts.configure_admin import configure_admin


class NoDefaultUserSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = self.root / '.env'
        self.script = self.root / 'scripts' / 'configure.py'

    def run_setup(self, origin=''):
        output = io.StringIO()
        with patch.object(configure, '__file__', str(self.script)), \
                patch('builtins.input', return_value=origin) as prompt, \
                patch('getpass.getpass', side_effect=AssertionError('Must not ask for a user password')), \
                contextlib.redirect_stdout(output):
            configure.main()
        self.assertEqual(prompt.call_count, 1)
        return self.env.read_text(encoding='utf-8'), output.getvalue()

    @staticmethod
    def seed_hash(content):
        return next(line.split('=', 1)[1].strip("'") for line in content.splitlines()
                    if line.startswith('ADMIN_PASSWORD_HASH='))

    def test_fresh_setup_asks_only_for_origin_and_keeps_seed_private(self):
        known_random_value = 'private-random-compatibility-seed-abcdefghijklmnopqrstuvwxyz'
        with patch.object(configure.secrets, 'token_urlsafe', return_value=known_random_value):
            content, output = self.run_setup()
        encoded = self.seed_hash(content)
        self.assertTrue(valid_password_hash(encoded))
        self.assertTrue(verify_password(known_random_value, encoded))
        self.assertNotIn(known_random_value, content + output)
        self.assertNotIn(encoded, output)
        self.assertIn('PUBLIC_ORIGIN=http://localhost:8000', content)
        self.assertIn('COOKIE_SECURE=false', content)
        self.assertIn('No default user was created', output)
        self.assertIn('configure_admin.py', output)
        self.assertEqual({path.name for path in self.root.iterdir()}, {'.env'})

    def test_valid_https_and_existing_admin_configuration_workflow(self):
        content, _ = self.run_setup('https://claude.example.com')
        self.assertIn('COOKIE_SECURE=true', content)
        self.assertIn('DOMAIN=claude.example.com', content)
        administrator_password = 'administrator-test-password-123'
        with patch('getpass.getpass', side_effect=[administrator_password, administrator_password]):
            configure_admin(self.env)
        updated = self.env.read_text(encoding='utf-8')
        self.assertEqual(self.seed_hash(content), self.seed_hash(updated))
        management_hash = next(line.split('=', 1)[1].strip("'") for line in updated.splitlines()
                               if line.startswith('MANAGEMENT_PASSWORD_HASH='))
        self.assertTrue(verify_password(administrator_password, management_hash))
        self.assertNotIn(administrator_password, updated)

    def test_existing_configuration_is_never_overwritten(self):
        original = b'PRIVATE_EXISTING_CONFIGURATION=keep\n'
        self.env.write_bytes(original)
        with patch.object(configure, '__file__', str(self.script)), \
                patch('builtins.input', side_effect=AssertionError('No prompt for existing config')):
            with self.assertRaisesRegex(SystemExit, 'already exists'):
                configure.main()
        self.assertEqual(self.env.read_bytes(), original)

    def test_invalid_public_origin_does_not_create_configuration(self):
        for origin in ['http://public.example.com', 'https://example.com/path', 'not-an-origin']:
            with self.subTest(origin=origin), patch.object(configure, '__file__', str(self.script)), \
                    patch('builtins.input', return_value=origin), \
                    patch('getpass.getpass', side_effect=AssertionError('No password prompt')):
                with self.assertRaises(SystemExit):
                    configure.main()
                self.assertFalse(self.env.exists())


if __name__ == '__main__':
    unittest.main()

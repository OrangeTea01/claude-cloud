import tempfile
import unittest
from pathlib import Path

from backend.auth import verify_password
from scripts.configure_solo import automatic_profile, configure_solo
from scripts.enable_https import ConfigError, literal_value


class SoloConfigurationTests(unittest.TestCase):
    def test_fresh_configuration_has_one_hashed_password_and_solo_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            password = 'Private site passphrase 2026'
            origin, profile = configure_solo(path, 'cloud.example.com', password, '8g')
            text = path.read_text(encoding='utf-8')
            values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
            self.assertEqual((origin, profile), ('https://cloud.example.com', '8g'))
            self.assertEqual(values['APP_MODE'], 'solo')
            self.assertEqual(values['MANAGEMENT_PASSWORD_HASH'], '')
            self.assertEqual(values['MAX_RUNNING_WORKSPACES'], '3')
            self.assertTrue(verify_password(password, literal_value(values['ADMIN_PASSWORD_HASH'])))
            self.assertNotIn(password, text)

    def test_existing_configuration_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('existing private config', encoding='utf-8')
            with self.assertRaises(ConfigError):
                configure_solo(path, 'cloud.example.com', 'new site passphrase', '8g')
            self.assertEqual(path.read_text(), 'existing private config')

    def test_invalid_input_does_not_create_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            for address, password, profile in [('https://cloud.example.com', 'valid passphrase here', '8g'),
                    ('cloud.example.com', 'short', '8g'), ('cloud.example.com', 'valid passphrase here', 'bad')]:
                with self.assertRaises(ConfigError):
                    configure_solo(path, address, password, profile)
                self.assertFalse(path.exists())

    def test_profile_detection_handles_real_available_memory_and_small_servers(self):
        for gib, expected in [(7.7, '8g'), (11.6, '12g'), (23.3, '24g')]:
            self.assertEqual(automatic_profile(f'MemTotal: {int(gib * 1024 * 1024)} kB\n'), expected)
        for text in ['', 'MemTotal: unknown kB', 'MemTotal: 2048000 kB']:
            with self.assertRaises(ConfigError):
                automatic_profile(text)


if __name__ == '__main__':
    unittest.main()

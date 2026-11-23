"""Regression coverage for password checking and single-owner sessions."""
import unittest
from unittest.mock import patch

from backend.auth import Sessions, hash_password, verify_password


class PasswordTests(unittest.TestCase):
    def test_passwords_are_salted_and_verified(self):
        password = "correct horse 中文 battery staple"
        first = hash_password(password)
        second = hash_password(password)
        self.assertNotEqual(first, second)
        self.assertNotIn(password, first)
        self.assertTrue(verify_password(password, first))
        self.assertFalse(verify_password(password + "!", first))

    def test_invalid_hash_and_untrusted_work_factor_are_rejected(self):
        for value in ("", "plain$password", "scrypt$1073741824$8$1$00$00", "scrypt$16384$8$1$zz$00", "argon2$16384$8$1$00$00"):
            with self.subTest(value=value):
                self.assertFalse(verify_password("password", value))


class SessionTests(unittest.TestCase):
    def test_tokens_are_independent_and_logout_revokes(self):
        sessions = Sessions()
        first, second = sessions.create(), sessions.create()
        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(first.csrf, first.token)
        self.assertNotEqual(first.csrf, second.csrf)
        self.assertIs(sessions.get(first.token), first)
        self.assertIsNone(sessions.get("forged"))
        sessions.remove(first.token)
        self.assertIsNone(sessions.get(first.token))
        self.assertIs(sessions.get(second.token), second)

    def test_expiry_is_enforced_on_read(self):
        sessions = Sessions(ttl=10)
        with patch("backend.auth.time.time", return_value=100):
            token = sessions.create().token
        with patch("backend.auth.time.time", return_value=110):
            self.assertIsNone(sessions.get(token))

    def test_login_rate_limit_expires_and_has_global_bound(self):
        sessions = Sessions()
        with patch("backend.auth.time.time", return_value=1000):
            self.assertTrue(all(sessions.allow_login("a") for _ in range(8)))
            self.assertFalse(sessions.allow_login("a"))
            for i in range(52):
                self.assertTrue(sessions.allow_login(str(i)))
            self.assertFalse(sessions.allow_login("fresh-address"))
        with patch("backend.auth.time.time", return_value=1301):
            self.assertTrue(sessions.allow_login("a"))

    def test_session_memory_is_bounded(self):
        sessions = Sessions()
        first = sessions.create().token
        for _ in range(100):
            sessions.create()
        self.assertLessEqual(len(sessions.sessions), 32)
        self.assertIsNone(sessions.get(first))


if __name__ == "__main__":
    unittest.main()

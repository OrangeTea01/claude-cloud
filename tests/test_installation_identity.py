"""Public instance proof is opt-in and cannot expose arbitrary environment text."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.docker_backend import Config


IDENTITY = "3983405b-6e78-40d6-8097-5bd5b856c827"


class InstallationIdentityTests(unittest.TestCase):
    def health(self, config):
        # Health is public and independent from Docker/storage startup.
        client = TestClient(create_app(config))
        try:
            response = client.get("/api/health")
            self.assertEqual(response.status_code, 200)
            return response.json()
        finally:
            client.close()

    def test_missing_or_empty_identity_preserves_existing_health_contract(self):
        for environment in ({}, {"INSTALLATION_ID": ""}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                config = Config.from_env()
                self.assertEqual(config.installation_id, "")
                self.assertEqual(self.health(config), {"ok": True})

    def test_valid_public_identity_is_the_only_extra_health_field(self):
        with patch.dict(os.environ, {"INSTALLATION_ID": IDENTITY,
                                    "ADMIN_PASSWORD_HASH": "private-password-hash",
                                    "MANAGEMENT_PASSWORD_HASH": "private-management-hash"}, clear=True):
            config = Config.from_env()
        self.assertEqual(self.health(config), {"ok": True, "installationId": IDENTITY})

    def test_invalid_environment_identity_is_rejected_without_echoing_value(self):
        for invalid in ("arbitrary-secret", " " + IDENTITY, IDENTITY.upper(), IDENTITY.replace("-", ""),
                        "urn:uuid:" + IDENTITY, "{" + IDENTITY + "}", "not-a-uuid\nsecret"):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {"INSTALLATION_ID": invalid}, clear=True):
                with self.assertRaises(ValueError) as caught:
                    Config.from_env()
                self.assertEqual(str(caught.exception), "INSTALLATION_ID must be empty or a canonical UUID")

    def test_programmatic_invalid_identity_is_also_rejected(self):
        for invalid in (None, False, 123, [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Config(installation_id=invalid)


if __name__ == "__main__":
    unittest.main()

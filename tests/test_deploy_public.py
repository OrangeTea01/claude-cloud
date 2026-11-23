"""HTTPS deployment diagnostics stay useful without weakening verification."""
import contextlib
import errno
import io
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

from scripts.deploy_public import NoRedirect, https_failure_reason, probe_https, wait_for_https


ORIGIN = "https://1.1.1.1"


class DeploymentHTTPSChecks(unittest.TestCase):
    def test_failure_categories_never_echo_sensitive_exception_details(self):
        secret = "secret-marker-never-print?token=private"
        errors = [
            (ssl.SSLCertVerificationError(1, secret), "certificate verification"),
            (ssl.SSLError(1, secret), "handshake failed"),
            (TimeoutError(secret), "timed out"),
            (ConnectionRefusedError(errno.ECONNREFUSED, secret), "connection refused"),
            (socket.gaierror(socket.EAI_AGAIN, secret), "could not be resolved"),
            (urllib.error.HTTPError(secret, 502, secret, {}, None), "HTTP status 502"),
            (urllib.error.HTTPError(secret, 302, secret, {}, None), "redirect refused"),
            (ValueError(secret), "not valid application health JSON"),
            (urllib.error.URLError(secret), "network connection failed"),
        ]
        for error, expected in errors:
            with self.subTest(category=expected):
                result = https_failure_reason(error)
                self.assertIn(expected, result)
                self.assertNotIn(secret, result)
                if not isinstance(error, urllib.error.HTTPError):
                    self.assertEqual(https_failure_reason(urllib.error.URLError(error)), result)

    def test_only_application_health_is_success(self):
        for status, body, success in [
            (200, b'{"ok":true}', True),
            (200, b'{"ok":false}', False),
            (200, b'{"ok":1}', False),
            (200, b'[]', False),
            (200, b'<html>secret-body</html>', False),
            (503, b'{"ok":true}', False),
        ]:
            with self.subTest(status=status, body=body):
                response = MagicMock(status=status)
                response.read.return_value = body
                opener = MagicMock()
                opener.open.return_value.__enter__.return_value = response
                result = probe_https(opener, ORIGIN)
                self.assertEqual(result is None, success)
                self.assertNotIn("secret-body", result or "")
                opener.open.assert_called_once_with(ORIGIN + "/api/health", timeout=5)

    def test_retry_is_bounded_and_reports_target_attempt_and_reason(self):
        output = io.StringIO()
        with patch("scripts.deploy_public.probe_https", return_value="connection refused") as probe, \
                patch("scripts.deploy_public.time.sleep") as sleep, contextlib.redirect_stdout(output):
            self.assertEqual(wait_for_https(ORIGIN), "connection refused")
        self.assertEqual(probe.call_count, 12)
        self.assertEqual(sleep.call_count, 11)
        self.assertIn("HTTPS check 1/12 " + ORIGIN + ": connection refused", output.getvalue())
        self.assertIn("HTTPS check 12/12", output.getvalue())

    def test_recovery_stops_waiting_without_final_failure(self):
        with patch("scripts.deploy_public.probe_https", side_effect=["TLS handshake failed", None]) as probe, \
                patch("scripts.deploy_public.time.sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(wait_for_https(ORIGIN))
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_verification_proxy_and_redirect_controls_remain_enabled(self):
        with patch("scripts.deploy_public.urllib.request.build_opener") as build, \
                patch("scripts.deploy_public.probe_https", return_value=None):
            wait_for_https(ORIGIN)
        handlers = build.call_args.args
        proxy = next(handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler))
        https = next(handler for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        redirect = next(handler for handler in handlers if isinstance(handler, NoRedirect))
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://other.example/"))


if __name__ == "__main__":
    unittest.main()

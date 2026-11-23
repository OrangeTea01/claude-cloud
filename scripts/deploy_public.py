"""Deploy public HTTPS and the server login browser on the Linux server."""
import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from .enable_https import ConfigError, enable_https, public_target
else:
    from enable_https import ConfigError, enable_https, public_target


def run(*args):
    subprocess.run(args, check=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_failure_reason(error):
    """Report fixed categories, never exception text containing URLs or secrets."""
    if isinstance(error, urllib.error.HTTPError):
        if 300 <= error.code < 400:
            return "HTTP redirect refused (status %s)" % error.code
        return "HTTP status %s from HTTPS endpoint" % error.code
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed (untrusted, expired or wrong address)"
    if isinstance(error, ssl.SSLError):
        return "TLS handshake failed; check Caddy certificate issuance logs"
    if isinstance(error, TimeoutError) or getattr(error, "errno", None) == errno.ETIMEDOUT:
        return "connection or response timed out; check the target address and network access"
    if isinstance(error, ConnectionRefusedError) or getattr(error, "errno", None) == errno.ECONNREFUSED:
        return "connection refused; check that Caddy is listening on TCP 443"
    if isinstance(error, socket.gaierror):
        return "target hostname could not be resolved"
    if isinstance(error, (ValueError, UnicodeError)):
        return "HTTPS response is not valid application health JSON"
    if isinstance(error, http.client.HTTPException):
        return "invalid or incomplete HTTP response"
    return "network connection failed; check Caddy logs and the target address"


def probe_https(opener, origin):
    try:
        with opener.open(origin + "/api/health", timeout=5) as response:
            if response.status != 200:
                return "HTTP status %s from HTTPS endpoint" % response.status
            health = json.loads(response.read(4096))
            if isinstance(health, dict) and health.get("ok") is True:
                return None
            return "HTTPS response is not the expected application health result"
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
        return https_failure_reason(error)


def wait_for_https(origin):
    # Keep TLS trust and hostname verification on. Ignore environment proxies and
    # reject redirects rather than mistaking another site's health for success.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    reason = "HTTPS has not been checked"
    for attempt in range(1, 13):
        reason = probe_https(opener, origin)
        if reason is None:
            return None
        print("HTTPS check %s/12 %s: %s" % (attempt, origin, reason), flush=True)
        if attempt < 12:
            time.sleep(5)
    return reason


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="Public IP or domain, without https://")
    args = parser.parse_args()
    public_target(args.address)
    if sys.platform != "linux":
        raise SystemExit("Run this command on your Linux server, not on the Windows computer.")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    if not (root / ".env").is_file():
        raise SystemExit("Run python3 scripts/configure.py once to set your dashboard password first.")

    print("Building worker and temporary login browser...", flush=True)
    run("docker", "compose", "--profile", "tools", "build", "worker", "login-browser")
    run(sys.executable, "deploy/login_browser/smoke_runtime.py")
    origin, changed = enable_https(root / ".env", args.address)
    print("HTTPS target: " + origin + "; existing password and workspaces retained.", flush=True)
    run("docker", "compose", "pull", "caddy")
    run("docker", "compose", "run", "--rm", "--no-deps", "caddy", "caddy", "validate",
        "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
    run("docker", "compose", "up", "-d", "--build", "--wait", "--wait-timeout", "180", "web", "egress", "caddy")
    # Upgrading an existing deployment must also retire its idle translation
    # container. Explicit profile selection here stops it without building it.
    run("docker", "compose", "--profile", "legacy-translation", "stop", "translator")

    reason = wait_for_https(origin)
    if reason is None:
        print("PASS trusted HTTPS and application health: " + origin, flush=True)
        print("Open this URL on your phone or other device and use your existing dashboard password.")
        return
    run("docker", "compose", "logs", "--tail=60", "caddy")
    raise SystemExit("HTTPS is not verified yet: %s. Check the target IP/domain, Caddy log above and inbound TCP 80/443. No data volumes were deleted." % reason)


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    except subprocess.CalledProcessError as exc:
        raise SystemExit("Deployment stopped at a failed step (exit %s). See the command output above; existing data volumes were kept." % exc.returncode) from None
    except FileNotFoundError:
        raise SystemExit("A required command or project file is missing. Check Docker and upload the complete source package.") from None

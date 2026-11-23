"""Create a private .env; dashboard users are created later by the administrator."""
import hashlib
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64)
    return "scrypt$16384$8$1$%s$%s" % (salt.hex(), digest.hex())


def main():
    target = Path(__file__).resolve().parents[1] / ".env"
    if target.exists():
        raise SystemExit(".env already exists. Move it aside explicitly before generating a replacement.")
    origin = input("Public origin [http://localhost:8000; use https://your-domain for public access]: ").strip()
    origin = origin or "http://localhost:8000"
    parsed = urlsplit(origin)
    try:
        parsed.port
    except ValueError:
        raise SystemExit("Origin port must be a number between 1 and 65535.")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise SystemExit("Enter a single http(s) origin without credentials, path, query, or fragment.")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("Public access requires HTTPS. Use localhost with an SSH tunnel for HTTP.")
    if any(char in origin for char in "\r\n'\"$#\\"):
        raise SystemExit("Unsupported origin characters.")
    origin = origin.rstrip("/")
    # Keep the historical field for configuration and timing-safe login checks.
    # The generated seed is not a dashboard password and creates no user.
    bootstrap_hash = password_hash(secrets.token_urlsafe(64))
    content = "\n".join([
        "# Generated locally; never commit or share this file.",
        "# Compatibility seed only; no default dashboard user is created.",
        "ADMIN_PASSWORD_HASH='%s'" % bootstrap_hash,
        "PUBLIC_ORIGIN=%s" % origin,
        "COOKIE_SECURE=%s" % ("true" if parsed.scheme == "https" else "false"),
        "DOMAIN=%s" % parsed.hostname,
        "WORKER_IMAGE=claude-cloud-worker:local",
        "CLAUDE_VERSION=stable",
        "WORKSPACE_MEMORY=3g",
        "WORKSPACE_CPUS=1.5",
        "MAX_WORKSPACES=30",
        "MAX_RUNNING_WORKSPACES=5",
        "",
    ])
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    print("Created .env. No default user was created. Keep this file private.")
    print("Run python3 scripts/configure_admin.py next, then create users at /admin/ after deployment.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("Cancelled; no config was written.")

"""Enable public HTTPS in an existing deployment without changing its password.

Run on the server: python3 scripts/enable_https.py PUBLIC_IP_OR_DOMAIN
Only updates .env; Docker startup and certificate issuance remain explicit.
"""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import stat
import tempfile


MANAGED_KEYS = {"PUBLIC_ORIGIN", "COOKIE_SECURE", "DOMAIN", "COMPOSE_PROFILES"}
ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class ConfigError(ValueError):
    pass


def public_target(value):
    """Return canonical (certificate name, HTTPS origin) without accepting code."""
    if not value or len(value) > 1024 or any(c.isspace() for c in value) or any(c in value for c in '/@?#$\'"\\%'):
        raise ConfigError("Enter only a public IP address or domain, without a scheme, path, port, or credentials.")
    bracketed = value.startswith("[") and value.endswith("]")
    host = value[1:-1] if bracketed else value
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if bracketed or ":" in host or re.fullmatch(r"[0-9.]+", host):
            raise ConfigError("Invalid public IP address; do not include a port.") from None
        try:
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ConfigError("Invalid domain name.") from None
        labels = host.split(".")
        if len(host) > 253 or len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            raise ConfigError("Use a complete public domain name.")
        if labels[-1].isdigit() or labels[-1] in {"localhost", "local", "internal", "invalid", "test", "example", "onion"}:
            raise ConfigError("Use a public domain, not a local or reserved hostname.")
        return host, "https://" + host
    if not address.is_global or address.is_multicast or address.is_reserved or (address.version == 6 and address.ipv4_mapped is not None):
        raise ConfigError("HTTPS publication requires a publicly routable IP address.")
    if bracketed and address.version != 6:
        raise ConfigError("Only IPv6 addresses may use square brackets.")
    host = address.compressed
    authority = "[" + host + "]" if address.version == 6 else host
    return host, "https://" + authority


def literal_value(value):
    """Read a single dotenv literal without evaluating variable interpolation."""
    value = value.strip()
    if not value:
        return ""
    if value[0] in "\"'":
        match = re.fullmatch(r"(['\"])(.*?)\1(?:[ \t]+#.*)?[ \t]*", value)
        if not match:
            raise ConfigError("Multiline or unsupported quoted .env values require a manual edit; nothing was changed.")
        return match.group(2)
    return re.split(r"[ \t]+#", value, maxsplit=1)[0].rstrip()


def render_env(original, target):
    """Keep all unowned lines byte-for-byte, including the scrypt hash quoting."""
    host, origin = public_target(target)
    bom = original.startswith(b"\xef\xbb\xbf")
    try:
        text = original.decode("utf-8-sig")
    except UnicodeError:
        raise ConfigError("The existing .env must use UTF-8 encoding; nothing was changed.") from None
    if "\x00" in text:
        raise ConfigError("The existing .env contains invalid data; nothing was changed.")
    lines = text.splitlines(keepends=True)
    assignments = {}
    for line in lines:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if not match:
            continue
        key, value = match.groups()
        # Do not mistake lines inside a multiline value for managed assignments.
        literal_value(value)
        if key in MANAGED_KEYS or key == "ADMIN_PASSWORD_HASH":
            if key in assignments:
                raise ConfigError("The .env contains duplicate managed settings; resolve them before enabling HTTPS.")
            assignments[key] = value
    if not literal_value(assignments.get("ADMIN_PASSWORD_HASH", "")):
        raise ConfigError("Existing ADMIN_PASSWORD_HASH is missing. Run scripts/configure.py for initial setup first.")
    profiles = [p.strip() for p in literal_value(assignments.get("COMPOSE_PROFILES", "")).split(",") if p.strip()]
    if any(not PROFILE.fullmatch(profile) for profile in profiles):
        raise ConfigError("COMPOSE_PROFILES must contain literal comma-separated profile names; nothing was changed.")
    if "https" not in profiles:
        profiles.append("https")
    replacements = {
        "PUBLIC_ORIGIN": origin,
        "COOKIE_SECURE": "true",
        "DOMAIN": host,
        "COMPOSE_PROFILES": ",".join(profiles),
    }
    newline = "\r\n" if "\r\n" in text else "\n"
    output = []
    seen = set()
    for line in lines:
        match = ASSIGNMENT.fullmatch(line.rstrip("\r\n"))
        if match and match.group(1) in replacements:
            key = match.group(1)
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            output.append(key + "=" + replacements[key] + ending)
            seen.add(key)
        else:
            output.append(line)
    for key, value in replacements.items():
        if key not in seen:
            if output and not output[-1].endswith(("\r", "\n")):
                output.append(newline)
            output.append(key + "=" + value + newline)
    encoded = "".join(output).encode("utf-8")
    return (b"\xef\xbb\xbf" if bom else b"") + encoded, origin


def read_regular(path):
    """Reject links and special files before opening secrets, then check identity."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise ConfigError("Existing .env not found. Run scripts/configure.py for initial setup first.") from None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ConfigError("Configuration must be a regular file, not a symlink, hard link, or special file.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ConfigError("Configuration changed while being opened; retry after other edits finish.")
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ConfigError("Configuration file is unexpectedly large; nothing was changed.")
    return data, before


def enable_https(path, target):
    path = Path(path)
    original, before = read_regular(path)
    replacement, origin = render_env(original, target)
    if replacement == original:
        os.chmod(path, 0o600)
        return origin, False
    backup = path.with_name(".env.https-backup")
    try:
        backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        # Keep the very first backup, even on later changes of public address.
        read_regular(backup)
        os.chmod(backup, 0o600)
    else:
        with os.fdopen(backup_fd, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    temp_fd, temp_name = tempfile.mkstemp(prefix=".env.https-", dir=path.parent)
    try:
        with os.fdopen(temp_fd, "wb") as stream:
            os.chmod(temp_name, 0o600)
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        current, info = read_regular(path)
        if current != original or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise ConfigError("Configuration was edited concurrently; no replacement was made.")
        os.replace(temp_name, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.lexists(temp_name):
            os.unlink(temp_name)
    return origin, True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="Public IPv4/IPv6 address or domain, without https://")
    args = parser.parse_args(argv)
    try:
        origin, changed = enable_https(Path(__file__).resolve().parents[1] / ".env", args.address)
    except ConfigError as exc:
        # These messages never include file contents, values, or password hashes.
        raise SystemExit(str(exc)) from None
    except OSError:
        raise SystemExit("Could not update .env. Check file permissions and available disk space; no password was reset.") from None
    print("Updated .env." if changed else "HTTPS settings are already current.")
    print("Public URL: " + origin)
    print("Password and workspace data were preserved.")
    if changed:
        print("The initial .env backup is .env.https-backup; keep it private.")
    print("Allow inbound TCP 80 and 443 in the server and cloud firewalls.")
    print("Next: docker compose pull caddy")
    print("Then: docker compose up -d --build")
    print("Check: docker compose logs --tail=60 caddy")
    print("The URL is ready only after the HTTPS certificate is issued successfully.")


if __name__ == "__main__":
    main()

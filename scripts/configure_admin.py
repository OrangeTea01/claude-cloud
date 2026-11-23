"""Set the separate, fixed administrator password in an existing deployment.

Run locally on the server: python3 scripts/configure_admin.py
Passwords are read from the terminal only, never from command arguments.
"""
import argparse
import getpass
import os
from pathlib import Path
import re
import tempfile
import warnings

if __package__:
    from .configure import password_hash
    from .enable_https import ASSIGNMENT, ConfigError, literal_value, read_regular
else:
    from configure import password_hash
    from enable_https import ASSIGNMENT, ConfigError, literal_value, read_regular


ADMIN_KEY = "MANAGEMENT_PASSWORD_HASH"
HASH_PATTERN = re.compile(r"scrypt\$16384\$8\$1\$[0-9a-f]{32}\$[0-9a-f]{128}\Z")


def config_parts(original):
    """Reject ambiguous settings before asking the owner for a password."""
    try:
        text = original.decode("utf-8-sig")
    except UnicodeError:
        raise ConfigError("The existing .env must use UTF-8 encoding; nothing was changed.") from None
    if "\x00" in text:
        raise ConfigError("The existing .env contains invalid data; nothing was changed.")
    lines = text.splitlines(keepends=True)
    managed = {}
    target_index = None
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        match = ASSIGNMENT.fullmatch(content)
        if not match:
            if re.match(r"^[ \t]*(?:export[ \t]+)?(?:MANAGEMENT_PASSWORD_HASH|ADMIN_PASSWORD_HASH)\b", content):
                raise ConfigError("A password setting is malformed; nothing was changed.")
            continue
        key, value = match.groups()
        # Refuse multiline values anywhere, so a line inside one cannot be
        # mistaken for the administrator setting. Do not evaluate interpolation.
        parsed = "" if value.lstrip().startswith("#") else literal_value(value)
        if key in (ADMIN_KEY, "ADMIN_PASSWORD_HASH"):
            if key in managed:
                raise ConfigError("The .env contains duplicate password settings; nothing was changed.")
            managed[key] = parsed
            if key == ADMIN_KEY:
                target_index = index
    if not managed.get("ADMIN_PASSWORD_HASH"):
        raise ConfigError("Existing dashboard password is missing. Run scripts/configure.py for initial setup first.")
    if managed.get(ADMIN_KEY):
        raise ConfigError("The administrator password is already configured. It was not changed; use it to sign in at /admin/.")
    return lines, target_index, "\r\n" if "\r\n" in text else "\n", original.startswith(b"\xef\xbb\xbf")


def render_env(original, encoded_hash):
    if not HASH_PATTERN.fullmatch(encoded_hash):
        raise ConfigError("Invalid generated password hash; nothing was changed.")
    lines, index, newline, bom = config_parts(original)
    assignment = ADMIN_KEY + "='" + encoded_hash + "'"
    if index is None:
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines.append(newline)
        lines.append(assignment + newline)
    else:
        old_line = lines[index]
        ending = "\r\n" if old_line.endswith("\r\n") else "\n" if old_line.endswith("\n") else ""
        lines[index] = assignment + ending
    encoded = "".join(lines).encode("utf-8")
    return (b"\xef\xbb\xbf" if bom else b"") + encoded


def configure_admin(path):
    path = Path(path)
    original, before = read_regular(path)
    config_parts(original)
    with warnings.catch_warnings():
        # getpass otherwise falls back to an echoed stdin prompt without a TTY.
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("Administrator password (12-1024 characters): ")
        if not 12 <= len(password) <= 1024:
            raise ConfigError("Use a password between 12 and 1024 characters; nothing was changed.")
        if password != getpass.getpass("Repeat administrator password: "):
            raise ConfigError("Passwords do not match; nothing was changed.")
    replacement = render_env(original, password_hash(password))
    descriptor, temporary = tempfile.mkstemp(prefix=".env.admin-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        current, info = read_regular(path)
        if current != original or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise ConfigError("Configuration was edited concurrently; no replacement was made.")
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        configure_admin(Path(__file__).resolve().parents[1] / ".env")
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    except getpass.GetPassWarning:
        raise SystemExit("A private interactive terminal is required; no configuration was changed.") from None
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("Cancelled; no configuration was changed.") from None
    except OSError:
        raise SystemExit("Could not save administrator settings. Check file permissions and disk space before retrying.") from None
    print("Administrator password configured. Password and hash are not printed.")
    print("The dashboard password, HTTPS settings and workspace data were preserved.")
    print("Next: docker compose up -d --build --no-deps --wait web")
    print("Then open /admin/ on your existing website and sign in with the administrator password.")


if __name__ == "__main__":
    main()

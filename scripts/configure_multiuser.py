"""Set bounded multi-user workspace resources in an existing deployment.

Run on the server: python3 scripts/configure_multiuser.py --profile 24g
Only four resource settings are changed; credentials and data are preserved.
"""
import argparse
import os
from pathlib import Path
import re
import tempfile

if __package__:
    from .enable_https import ASSIGNMENT, ConfigError, literal_value, read_regular
else:
    from enable_https import ASSIGNMENT, ConfigError, literal_value, read_regular


PROFILES = {
    "8g": {"WORKSPACE_MEMORY": "1536m", "WORKSPACE_CPUS": "1", "MAX_WORKSPACES": "20", "MAX_RUNNING_WORKSPACES": "3"},
    "12g": {"WORKSPACE_MEMORY": "2g", "WORKSPACE_CPUS": "1", "MAX_WORKSPACES": "20", "MAX_RUNNING_WORKSPACES": "3"},
    "24g": {"WORKSPACE_MEMORY": "3g", "WORKSPACE_CPUS": "1.5", "MAX_WORKSPACES": "30", "MAX_RUNNING_WORKSPACES": "5"},
}
DEFAULT_PROFILE = "24g"
RESOURCE_KEYS = frozenset(PROFILES[DEFAULT_PROFILE])
CHECKED_KEYS = RESOURCE_KEYS | {"ADMIN_PASSWORD_HASH", "MANAGEMENT_PASSWORD_HASH"}


def render_env(original, profile=DEFAULT_PROFILE):
    if profile not in PROFILES:
        raise ConfigError("Choose resource profile 8g, 12g, or 24g; nothing was changed.")
    try:
        text = original.decode("utf-8-sig")
    except UnicodeError:
        raise ConfigError("The existing .env must use UTF-8 encoding; nothing was changed.") from None
    if "\x00" in text:
        raise ConfigError("The existing .env contains invalid data; nothing was changed.")
    lines = text.splitlines(keepends=True)
    assignments = {}
    for line in lines:
        content = line.rstrip("\r\n")
        match = ASSIGNMENT.fullmatch(content)
        if not match:
            if re.match(r"^[ \t]*(?:export[ \t]+)?(?:" + "|".join(CHECKED_KEYS) + r")\b", content):
                raise ConfigError("A managed .env setting is malformed; nothing was changed.")
            continue
        key, value = match.groups()
        # Parse all literals to reject ambiguous multiline values without ever
        # evaluating interpolation or printing a secret.
        parsed = "" if value.lstrip().startswith("#") else literal_value(value)
        if key in CHECKED_KEYS:
            if key in assignments:
                raise ConfigError("The .env contains duplicate resource or password settings; nothing was changed.")
            assignments[key] = parsed
    if not assignments.get("ADMIN_PASSWORD_HASH"):
        raise ConfigError("Existing dashboard password is missing. Run scripts/configure.py for initial setup first.")
    replacements = PROFILES[profile]
    newline = "\r\n" if "\r\n" in text else "\n"
    output, seen = [], set()
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
    return (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + "".join(output).encode("utf-8")


def configure_multiuser(path, profile=DEFAULT_PROFILE):
    path = Path(path)
    original, before = read_regular(path)
    replacement = render_env(original, profile)
    if replacement == original:
        os.chmod(path, 0o600)
        return profile, False
    backup = path.with_name(".env.multiuser-backup")
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        read_regular(backup)
        os.chmod(backup, 0o600)
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix=".env.multiuser-", dir=path.parent)
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
    return profile, True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default=DEFAULT_PROFILE, help="Server memory profile (default: 24g; use 8g or 12g on smaller servers)")
    args = parser.parse_args(argv)
    try:
        profile, changed = configure_multiuser(Path(__file__).resolve().parents[1] / ".env", args.profile)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    except OSError:
        raise SystemExit("Could not update .env. Check permissions and disk space; no passwords or workspace data were reset.") from None
    values = PROFILES[profile]
    print("Updated resource settings." if changed else "Resource settings are already current.")
    print("Profile: %s; workspace memory: %s; CPU limit: %s; saved workspaces: %s; running workspaces: %s." %
          (profile, values["WORKSPACE_MEMORY"], values["WORKSPACE_CPUS"], values["MAX_WORKSPACES"], values["MAX_RUNNING_WORKSPACES"]))
    print("Passwords, HTTPS settings, accounts and workspace data were preserved.")
    if changed:
        print("The initial private configuration backup is .env.multiuser-backup.")
    print("Stop active workspace tasks before the upgrade, then run:")
    print("docker compose up -d --build --no-deps --wait web")


if __name__ == "__main__":
    main()

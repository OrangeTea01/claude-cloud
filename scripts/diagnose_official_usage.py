"""One read-only official-usage probe; run from /root/claude-cloud:

docker compose exec -T web python - < diagnose_official_usage.py

Uses an isolated, temporary container and a read-only credential mount. It never
starts/stops the existing collector, refreshes credentials, or submits a prompt.
Only allowlisted meter shapes/numbers/timestamps are printed. AUTH_EXPIRED means
the saved access token is expired or too close to expiry for this short probe.
"""
import asyncio
from contextlib import closing
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import uuid

from backend.admin_usage_broker import ADMIN_USAGE_ID, AdminUsageBroker
from backend.docker_backend import Config, DockerBroker, OWNER_LABEL, RESOURCE_LABEL
from backend.official_usage import UsageUnavailable, fetch_usage_command


ROLE = "official-usage-diagnostic"
NONCE_LABEL = "io.claude-cloud.diagnostic-id"
SAFE_CODES = {"AUTH_EXPIRED", "AUTH_SCOPE_MISSING", "AUTH_UNAVAILABLE", "RESOURCE_UNAVAILABLE",
              "RESOURCE_MISMATCH", "QUERY_FAILED", "QUERY_TIMEOUT", "CLEANUP_FAILED"}

# No refresh credential is placed in the environment or copied into HOME. CLI
# receives the access token's actual, whitespace-separated grants, never invented
# scopes. A failed preflight answers only the two diagnostic control frames.
PROBE_HELPER = r'''import json, math, os, re, stat, sys, time
scope_present = False
def fail(code):
    for expected in ('initialize', 'get_usage'):
        try:
            line = sys.stdin.buffer.readline(65537)
            if len(line) > 65536:
                break
            frame = json.loads(line)
            request = frame.get('request', {})
            request_id = frame.get('request_id')
            if (frame.get('type') != 'control_request' or request.get('subtype') != expected
                    or not isinstance(request_id, str) or len(request_id) > 128):
                break
            response = {} if expected == 'initialize' else {'diagnosticError': code, 'scopePresent': scope_present}
            print(json.dumps({'type': 'control_response', 'response': {'subtype': 'success',
                             'request_id': request_id, 'response': response}}), flush=True)
        except Exception:
            break
    sys.exit(0)
try:
    source = os.open('/run/usage-source', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        config = os.open('.claude', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source)
        try:
            descriptor = os.open('.credentials.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=config)
        finally:
            os.close(config)
    finally:
        os.close(source)
    with os.fdopen(descriptor, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
            raise ValueError()
        oauth = json.loads(stream.read(1048577))['claudeAiOauth']
    scopes = oauth.get('scopes')
    if (not isinstance(scopes, list) or len(scopes) > 128
            or not all(isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9:._/-]{1,128}', item) for item in scopes)):
        raise ValueError()
    scope_present = 'user:profile' in scopes
    if not scope_present or 'user:inference' not in scopes:
        fail('AUTH_SCOPE_MISSING')
    expires = oauth.get('expiresAt')
    if type(expires) not in (int, float) or not math.isfinite(expires):
        raise ValueError()
    if expires <= (time.time() + 60) * 1000:
        fail('AUTH_EXPIRED')
    token = oauth.get('accessToken')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._-]{32,32768}', token):
        raise ValueError()
    environment = {
        'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp/usage-home',
        'CLAUDE_CONFIG_DIR': '/tmp/usage-home/.claude', 'LANG': 'C.UTF-8', 'TERM': 'dumb',
        'DISABLE_AUTOUPDATER': '1', 'DISABLE_UPDATES': '1',
        'HTTPS_PROXY': 'http://egress:3128', 'HTTP_PROXY': 'http://egress:3128',
        'https_proxy': 'http://egress:3128', 'http_proxy': 'http://egress:3128',
        'NO_PROXY': '', 'no_proxy': '', 'CLAUDE_CODE_OAUTH_TOKEN': token,
        'CLAUDE_CODE_OAUTH_SCOPES': ' '.join(scopes),
    }
    os.makedirs('/tmp/usage-home/.claude', mode=0o700, exist_ok=True)
except Exception:
    fail('AUTH_UNAVAILABLE')
arguments = ['/usr/local/bin/claude', '-p', '--input-format', 'stream-json', '--output-format',
             'stream-json', '--verbose', '--safe-mode', '--restricted', '--tools', '',
             '--strict-mcp-config', '--no-session-persistence']
try:
    os.execve(arguments[0], arguments, environment)
except Exception:
    fail('AUTH_UNAVAILABLE')
'''


class DiagnosticError(Exception):
    def __init__(self, code):
        self.code = code if code in SAFE_CODES else "QUERY_FAILED"


class ReadOnlyBroker(DockerBroker):
    def __init__(self, config):
        # Do not invoke DockerBroker's constructor: it can migrate/write the DB.
        self.config = config
        path = Path(config.data_dir) / "registry.sqlite3"
        if not path.is_file() or path.is_symlink():
            raise DiagnosticError("RESOURCE_UNAVAILABLE")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
            row = database.execute("SELECT value FROM meta WHERE key='owner'").fetchone()
        if not row or not isinstance(row[0], str) or not re.fullmatch(r"[a-f0-9]{12,32}", row[0]):
            raise DiagnosticError("RESOURCE_MISMATCH")
        self.owner = row[0]
        self.commands = asyncio.Semaphore(2)


def shape(value, present=True):
    if not present:
        return "missing"
    if value is None:
        return "null"
    return "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "boolean" if type(value) is bool else "other"


def meter(value, present=True):
    result = {"shape": shape(value, present)}
    if not isinstance(value, dict):
        return result
    for key in ("utilization", "percent", "used_percentage", "usedPercent"):
        if key in value:
            number = value[key]
            valid = type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 100
            result[key] = number if valid else None
            if number is not None and not valid:
                result[key + "Shape"] = "invalid"
    for key in ("resets_at", "reset_at", "resetAt", "resetsAt", "reset"):
        if key not in value:
            continue
        timestamp = value[key]
        valid = type(timestamp) in (int, float) and math.isfinite(timestamp) and 0 < timestamp < 253402300800000
        if isinstance(timestamp, str) and len(timestamp) <= 80:
            try:
                valid = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is not None
            except ValueError:
                valid = False
        result[key] = timestamp if valid else None
        if timestamp is not None and not valid:
            result[key + "Shape"] = "invalid"
    return result


def project_usage(reply):
    if not isinstance(reply, dict):
        raise DiagnosticError("QUERY_FAILED")
    if reply.get("diagnosticError") in SAFE_CODES:
        return {"ok": False, "code": reply["diagnosticError"], "scopePresent": reply.get("scopePresent") is True}
    report = reply.get("usage_report", reply)
    if not isinstance(report, dict):
        raise DiagnosticError("QUERY_FAILED")
    available = report.get("rate_limits_available")
    rates = report.get("rate_limits")
    result = {"ok": True, "scopePresent": True,
              "available": available if type(available) is bool else None,
              "availableShape": shape(available, "rate_limits_available" in report),
              "rateLimitsShape": shape(rates, "rate_limits" in report)}
    rates = rates if isinstance(rates, dict) else {}
    for key in ("five_hour", "seven_day"):
        result[key] = meter(rates.get(key), key in rates)
    rows = rates.get("limits")
    result["rowsShape"] = shape(rows, "limits" in rates)
    result["rows"] = []
    if isinstance(rows, list):
        for row in rows[:64]:
            if isinstance(row, dict) and row.get("kind") in ("session", "weekly_all"):
                result["rows"].append({"kind": row["kind"], "scopePresent": bool(row.get("scope")), **meter(row)})
    return result


async def inspected(parent, kind, name):
    code, output, _ = await parent.command(kind, "inspect", name, check=False, timeout=10)
    if code:
        raise DiagnosticError("RESOURCE_UNAVAILABLE")
    try:
        items = json.loads(output)
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError()
        return items[0]
    except (ValueError, TypeError):
        raise DiagnosticError("RESOURCE_MISMATCH") from None


async def cleanup(parent, name, nonce):
    code, output, error = await parent.command("container", "inspect", name, check=False, timeout=10)
    if code:
        if any(part in error.decode(errors="replace").lower() for part in ("no such", "not found")):
            return
        raise DiagnosticError("CLEANUP_FAILED")
    try:
        item = json.loads(output)[0]
        labels = item.get("Config", {}).get("Labels", {})
        if (item.get("Name") != "/" + name or labels.get(OWNER_LABEL) != parent.owner
                or labels.get(RESOURCE_LABEL) != ROLE or labels.get(NONCE_LABEL) != nonce):
            raise ValueError()
        identity = item["Id"]
        if not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise DiagnosticError("CLEANUP_FAILED") from None
    await parent.command("container", "rm", "--force", identity, timeout=10)


async def probe(parent):
    private = AdminUsageBroker(parent)
    names = private.names(ADMIN_USAGE_ID)
    home = await private.inspect_owned("volume", names["home"], ADMIN_USAGE_ID)
    network = await private.inspect_owned("network", names["network"], ADMIN_USAGE_ID)
    collector = await private.inspect_owned("container", names["container"], ADMIN_USAGE_ID)
    if not home or not network or not collector:
        raise DiagnosticError("RESOURCE_UNAVAILABLE")
    network_id = network.get("Id", "")
    if (home.get("Name") != names["home"] or not network.get("Internal")
            or network.get("Options", {}).get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
            or not re.fullmatch(r"[a-f0-9]{64}", network_id)):
        raise DiagnosticError("RESOURCE_MISMATCH")
    mounts = [item for item in collector.get("Mounts", []) if item.get("Destination") == "/home/claude"]
    if len(mounts) != 1 or mounts[0].get("Type") != "volume" or mounts[0].get("Name") != names["home"]:
        raise DiagnosticError("RESOURCE_MISMATCH")
    image_id = collector.get("Image", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise DiagnosticError("RESOURCE_MISMATCH")
    image = await inspected(parent, "image", image_id)
    if image.get("Id") != image_id or image.get("Config", {}).get("Volumes"):
        raise DiagnosticError("RESOURCE_MISMATCH")
    proxy = await inspected(parent, "container", parent.config.egress_container)
    attached = proxy.get("NetworkSettings", {}).get("Networks", {}).get(names["network"], {})
    if (not proxy.get("State", {}).get("Running") or attached.get("NetworkID") != network_id
            or "egress" not in (attached.get("Aliases") or [])):
        raise DiagnosticError("RESOURCE_UNAVAILABLE")
    nonce = uuid.uuid4().hex
    name = f"cc-{parent.owner}-usage-diag-{nonce}"
    attempted = False
    try:
        attempted = True
        await parent.command(
            "container", "create", "--rm", "--name", name,
            "--label", f"{OWNER_LABEL}={parent.owner}", "--label", f"{RESOURCE_LABEL}={ROLE}",
            "--label", f"{NONCE_LABEL}={nonce}", "--init", "--user", "1000:1000", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true", "--pids-limit", "64",
            "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--network", network_id,
            "--mount", f"type=volume,src={names['home']},dst=/run/usage-source,readonly,volume-nocopy",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777", "--workdir", "/tmp",
            "--log-driver", "none", "--entrypoint", "/usr/bin/timeout", image_id,
            "--signal=TERM", "--kill-after=3s", "35s", "/bin/sleep", "30s", timeout=10)
        await parent.command("container", "start", name, timeout=10)
        command = ["docker", "exec", "-i", "--user", "1000:1000", "--workdir", "/tmp", name,
                   "/usr/bin/timeout", "--signal=TERM", "--kill-after=3s", "25s",
                   "/usr/bin/python3", "-I", "-c", PROBE_HELPER]
        reply = await fetch_usage_command(command, timeout=20)
        return project_usage(reply)
    finally:
        if attempted:
            await cleanup(parent, name, nonce)


async def main():
    try:
        result = await probe(ReadOnlyBroker(Config.from_env()))
    except BaseException as error:
        timed_out = isinstance(error, TimeoutError) or (isinstance(error, UsageUnavailable) and error.code == "query_timeout")
        code = error.code if isinstance(error, DiagnosticError) else "QUERY_TIMEOUT" if timed_out else "QUERY_FAILED"
        result = {"ok": False, "code": code}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

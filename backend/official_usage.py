"""Read-only Claude plan meters, never inferred from tokens or local budgets.

The official Agent SDK 0.3.311 uses percent (0..100) in get_usage's
rate_limits.*.utilization, unlike rate_limit_info/unifiedWindows (0..1).
See https://code.claude.com/docs/en/statusline and the published SDK types at
https://registry.npmjs.org/@anthropic-ai/claude-agent-sdk/0.3.311 .
unifiedWindows is an observed, optional CLI extension, not a promised SDK field.
"""
import asyncio
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


WINDOWS = {"five_hour": "fiveHour", "seven_day": "weekly"}
RESET_FIELDS = ("resetsAt", "resets_at", "resetAt", "reset_at")
STATUSES = {"allowed", "allowed_warning", "rejected", "normal", "warning", "critical", "unknown"}
ERRORS = {
    "unavailable": "Claude 暂未返回官方额度；当前授权或客户端可能不提供这项数据。",
    "refresh_failed": "官方额度暂时无法刷新，保留上次记录。",
    "query_failed": "官方额度暂时无法刷新，保留上次记录。",
    "query_timeout": "查询官方额度超时，请在稍后允许重试时再次刷新。",
    "no_running_workspace": "请先启动一个工作区，再刷新官方额度。",
    "usage_authorization_required": "请管理员完成官方额度查询授权，并确认使用的是当前共享 Claude 账号。",
    "usage_authorization_busy": "管理员正在完成额度查询授权，请稍后刷新。",
    "storage_failed": "官方额度记录无法保存，请检查服务器存储。",
}
MAX_FRAME_BYTES = 1024 * 1024
MAX_REPLY_BYTES = 4 * MAX_FRAME_BYTES


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _timestamp(value):
    number = _number(value)
    if number is not None:
        # The wire protocol uses seconds. Accept clearly millisecond-sized
        # timestamps as well, without treating a usage percentage as a clock.
        if number >= 100_000_000_000:
            number /= 1000
        return number if 0 < number < 253_402_300_800 else None
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        number = parsed.timestamp()
        return number if 0 < number < 253_402_300_800 else None
    except (ValueError, OverflowError, OSError):
        return None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if value is not None else None


def _first_time(data, names):
    for name in names:
        if name in data:
            value = _timestamp(data[name])
            if value is not None:
                return value
    return None


def _percentage(data, fraction):
    for field in ("used_percentage", "usedPercent", "percent", "utilization"):
        if field not in data:
            continue
        value = _number(data[field])
        scale = 100 if field == "utilization" and fraction else 1
        if value is not None and 0 <= value <= 100 / scale:
            return round(value * scale, 6)
        return None
    return None


def _confirmed_idle(key, data, fraction, parent, percent, status):
    """Only a profile-authorized explicit zero/null report confirms an idle cycle.

    An absent window/reset, an invalid value, or a passive status-only frame
    cannot issue new quota. The weekly window has no such observed contract.
    """
    resets = [data[field] for field in RESET_FIELDS if field in data]
    amounts = [data[field] for field in ("used_percentage", "usedPercent", "percent", "utilization") if field in data]
    return (key == "fiveHour" and not fraction and parent.get("rate_limits_available") is True
            and percent == 0 and bool(amounts) and all(_number(value) == 0 for value in amounts)
            and bool(resets) and all(value is None for value in resets)
            and status not in ("rejected", "critical"))


def _fresh_full_meter(data, fraction, parent, root, percent, reset, now):
    """A complete profile report can supersede a previous transient rejection.

    Missing status alone is not recovery: require explicitly available meters,
    consistent numeric usage and a real future deadline. Passive rate headers
    and partial/invalid reports cannot clear the rejection.
    """
    amounts = [_number(data[field]) for field in ("used_percentage", "usedPercent", "percent", "utilization")
               if field in data]
    resets = [_timestamp(data[field]) for field in RESET_FIELDS if field in data]
    return (not fraction and root.get("type") != "rate_limit_event"
            and parent.get("rate_limits_available") is True
            and percent is not None and 0 <= percent < 100 and reset is not None and reset > now
            and bool(amounts) and all(value is not None and 0 <= value <= 100 and round(value, 6) == percent for value in amounts)
            and bool(resets) and all(value == reset for value in resets)
            and data.get("status") is None and data.get("severity") is None)


def _extract(payload):
    """Return only recognized, bounded meter objects; never retain raw replies."""
    if not isinstance(payload, dict):
        return [], {}
    body = payload
    if body.get("type") == "control_response":
        envelope = body.get("response")
        if not isinstance(envelope, dict) or envelope.get("subtype") != "success":
            return [], body
        body = envelope.get("response")
        if not isinstance(body, dict):
            return [], payload
    report = body.get("usage_report")
    if isinstance(report, dict):
        body = report
    if body.get("rate_limits_available") is False:
        return [], body
    result = []
    info = body.get("rate_limit_info", body)
    if isinstance(info, dict):
        active = info.get("rateLimitType", info.get("rate_limit_type"))
        active = active if isinstance(active, str) else None
        unified = info.get("unifiedWindows", info.get("unified_windows"))
        if isinstance(unified, dict):
            for name, key in WINDOWS.items():
                value = unified.get(name)
                if isinstance(value, dict):
                    result.append((key, value, True, info.get("status") if active == name else None, info))
        if active in WINDOWS and not any(item[0] == WINDOWS[active] for item in result):
            result.append((WINDOWS[active], info, True, None, info))
    rates = body.get("rate_limits", body)
    if isinstance(rates, dict):
        rows = rates.get("limits")
        if isinstance(rows, list):
            grouped = {"fiveHour": [], "weekly": []}
            for row in rows[:64]:
                if not isinstance(row, dict) or row.get("scope"):
                    continue
                kind = row.get("kind")
                key = {"session": "fiveHour", "weekly_all": "weekly"}.get(kind) if isinstance(kind, str) else None
                if key:
                    grouped[key].append(row)
            for key, matches in grouped.items():
                if len(matches) == 1:
                    result.append((key, matches[0], False, None, body))
        for name, key in WINDOWS.items():
            value = rates.get(name)
            if isinstance(value, dict) and not any(item[0] == key for item in result):
                result.append((key, value, False, None, body))
    return result, payload


class OfficialUsage:
    """One shared Claude account's latest observations, safe for UI projection.

    ingest() is synchronous and never interrupts a Claude turn for a cache error.
    refresh() accepts an async, token-free callback returning a get_usage reply.
    clear() must be called when shared authorization changes; it also invalidates
    refreshes already in flight. No request launches a model prompt here.
    """
    def __init__(self, data_dir, *, clock=time.time, stale_after=300, refresh_interval=60):
        self.clock, self.stale_after, self.refresh_interval = clock, stale_after, refresh_interval
        self._windows = {}
        self._error = None
        self._last_refresh = None
        self._refresh_lock = asyncio.Lock()
        self._generation = 0
        self._ingest_revision = 0
        self.db = None
        try:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(Path(data_dir) / "official_usage.sqlite3")
            self.db.execute("CREATE TABLE IF NOT EXISTS official_usage (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            row = self.db.execute("SELECT substr(data,1,8193) FROM official_usage WHERE id=1").fetchone()
            if row:
                saved = json.loads(row[0]) if len(row[0]) <= 8192 else {}
                if isinstance(saved, dict):
                    raw = saved.get("windows", {})
                    for key in WINDOWS.values():
                        item = raw.get(key) if isinstance(raw, dict) else None
                        if self._valid_saved(item, key):
                            self._windows[key] = item
                    self._error = saved.get("error") if saved.get("error") in ERRORS else None
            self.db.commit()
        except (sqlite3.Error, OSError, ValueError, TypeError):
            if self.db is not None:
                self.db.close()
            self.db = None
            self._error = "storage_failed"

    @staticmethod
    def _valid_saved(item, key=None):
        base = {"usedPercent", "reset", "status", "observed", "updated"}
        if not isinstance(item, dict) or set(item) not in (base, base | {"state", "idleSince"}):
            return False
        percent = item["usedPercent"]
        idle_valid = ("state" not in item or (key == "fiveHour" and item["state"] == "idle"
                      and type(percent) in (int, float) and percent == 0 and item["reset"] is None
                      and _timestamp(item["idleSince"]) == item["idleSince"]
                      and item["idleSince"] is not None and _number(item["updated"]) is not None
                      and item["idleSince"] <= item["updated"]
                      and item["status"] not in ("rejected", "critical")))
        return (idle_valid and (percent is None or (_number(percent) is not None and 0 <= percent <= 100))
                and (item["reset"] is None or _timestamp(item["reset"]) == item["reset"])
                and isinstance(item["status"], str) and item["status"] in STATUSES
                and _number(item["observed"]) is not None and _timestamp(item["observed"]) == item["observed"]
                and _number(item["updated"]) is not None and _timestamp(item["updated"]) == item["updated"])

    def _save(self, windows, error):
        if self.db is None:
            self._error = "storage_failed"
            return False
        try:
            data = json.dumps({"windows": windows, "error": error}, separators=(",", ":"), allow_nan=False)
            self.db.execute("INSERT OR REPLACE INTO official_usage VALUES (1,?)", (data,))
            self.db.commit()
        except (sqlite3.Error, OSError, ValueError):
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            self._error = "storage_failed"
            return False
        self._windows, self._error = windows, error
        return True

    def ingest(self, payload, *, observed_at=None):
        entries, root = _extract(payload)
        received = self.clock() if observed_at is None else _timestamp(observed_at)
        if received is None:
            return False
        root_time = _first_time(root, ("timestamp", "updatedAt", "updated_at", "fetchedAt", "fetchedAtMs"))
        windows = copy.deepcopy(self._windows)
        changed = False
        confirmed = False
        for key, data, fraction, inherited_status, parent in entries:
            observed = (_first_time(data, ("updatedAt", "updated_at", "timestamp", "fetchedAt", "fetchedAtMs"))
                        or _first_time(parent, ("updatedAt", "updated_at", "timestamp", "fetchedAt", "fetchedAtMs"))
                        or root_time or received)
            if observed > self.clock() + 5:
                continue
            percent = _percentage(data, fraction)
            reset = _first_time(data, RESET_FIELDS)
            status = data.get("status", data.get("severity", inherited_status))
            status = status if isinstance(status, str) and status in STATUSES else "unknown"
            if percent is None and reset is None and status == "unknown":
                continue
            idle = _confirmed_idle(key, data, fraction, parent, percent, status)
            old = windows.get(key)
            if old:
                if observed <= old["observed"]:
                    # A live get_usage reply may have been ingested by the
                    # stream reader already. An identical full observation is
                    # a valid confirmation, not missing data. Keep its original
                    # measurement time, including when that time is stale.
                    if (observed == old["observed"] and percent is not None
                            and (reset is not None or (idle and old.get("state") == "idle"))
                            and percent == old["usedPercent"] and reset == old["reset"]):
                        confirmed = True
                    continue
            if idle:
                # An idle response may arrive late from another process/cache.
                # It cannot erase a live known cycle or its consumed allowance.
                if old and old["reset"] is not None and (old["reset"] > observed or old["reset"] > self.clock()):
                    continue
                since = old["idleSince"] if old and old.get("state") == "idle" else observed
                windows[key] = {"usedPercent": 0.0, "reset": None, "status": status,
                                "observed": observed, "updated": observed, "state": "idle", "idleSince": since}
                changed = True
                continue
            if old:
                if old.get("state") == "idle":
                    if reset is not None and reset <= old["idleSince"]:
                        continue
                    if reset is None and percent in (None, 0) and status not in ("rejected", "critical"):
                        # Initialization/status-only frames do not revoke an
                        # explicit idle report or extend its freshness.
                        continue
                # A late frame from another CLI session must never roll a window
                # back. Untagged partial data cannot replace a known window.
                if old["reset"] is not None and (reset is None or reset < old["reset"]):
                    continue
                same_window = reset == old["reset"]
                if same_window and percent is None:
                    percent = old["usedPercent"]
                updated = old["updated"] if same_window and _percentage(data, fraction) is None else observed
                if (old.get("state") == "idle" and reset is not None and reset > observed
                        and percent is None and status in ("allowed", "allowed_warning")):
                    # First request can supply the new deadline before its
                    # usage. Keep the last measured zero at its ORIGINAL time;
                    # later numeric data settles the first debit in this cycle.
                    percent, updated = 0.0, old["updated"]
                if status == "unknown" and same_window:
                    if not (old["status"] == "rejected"
                            and _fresh_full_meter(data, fraction, parent, root, percent, reset, self.clock())):
                        status = old["status"]
            else:
                updated = observed
            windows[key] = {"usedPercent": percent, "reset": reset, "status": status,
                            "observed": observed, "updated": updated}
            changed = True
        if (changed or confirmed) and self._save(windows, None):
            self._ingest_revision += 1
            return True
        return False

    def snapshot(self):
        now = self.clock()
        result = {}
        updated = []
        for key in WINDOWS.values():
            item = self._windows.get(key)
            if item is None:
                result[key] = {"usedPercent": None, "resetAt": None, "status": "unknown", "updatedAt": None, "stale": True}
                continue
            idle = item.get("state") == "idle"
            stale = (self._error is not None or item["usedPercent"] is None
                     or (not idle and (item["reset"] is None or item["reset"] <= now))
                     or now - item["updated"] > self.stale_after)
            result[key] = {"usedPercent": item["usedPercent"], "resetAt": _iso(item["reset"]),
                           "status": item["status"], "updatedAt": _iso(item["updated"]), "stale": stale}
            if idle:
                result[key].update(state="idle", idleSince=_iso(item["idleSince"]))
            updated.append(item["updated"])
        code = "query_failed" if self._error == "refresh_failed" else self._error
        if code is None and not updated:
            code = "unavailable"
        retry = self._last_refresh + self.refresh_interval if self._last_refresh is not None else None
        result.update(updatedAt=_iso(max(updated)) if updated else None,
                      stale=any(result[key]["stale"] for key in WINDOWS.values()),
                      error=ERRORS.get(code), errorCode=code,
                      retryAt=_iso(retry) if retry is not None and retry > now else None)
        return result

    async def refresh(self, fetch_callback):
        async with self._refresh_lock:
            started = self.clock()
            if self._last_refresh is not None and started - self._last_refresh < self.refresh_interval:
                return self.snapshot()
            self._last_refresh = started
            generation = self._generation
            revision = self._ingest_revision
            try:
                reply = await asyncio.wait_for(fetch_callback(), 25)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if generation == self._generation and self._ingest_revision == revision:
                    code = (exc.code if isinstance(exc, UsageUnavailable) else
                            "query_timeout" if isinstance(exc, TimeoutError) else "query_failed")
                    self._save(copy.deepcopy(self._windows), code)
                return self.snapshot()
            if generation != self._generation:
                return self.snapshot()
            if not self.ingest(reply, observed_at=started):
                # Success is measured locally. Official timestamps may be
                # rounded, equal to, or earlier than the request's start time.
                if self._ingest_revision == revision:
                    self._save(copy.deepcopy(self._windows), "unavailable")
            return self.snapshot()

    def invalidate(self, code="usage_authorization_required"):
        """Keep historical meters, but cancel trust in an earlier query source."""
        self._generation += 1
        self._last_refresh = None
        code = code if code in ERRORS else "query_failed"
        if not self._save(copy.deepcopy(self._windows), code):
            self._error = "storage_failed"

    def clear(self):
        self._generation += 1
        self._last_refresh = None
        # Drop memory too if persistence fails: never show a former account's
        # meters as the newly authorized account's current usage.
        if not self._save({}, None):
            self._windows = {}

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None


class UsageUnavailable(Exception):
    def __init__(self, message="官方额度读取不可用", *, code="query_failed"):
        super().__init__(message)
        self.code = code if code in ERRORS and code != "refresh_failed" else "query_failed"


async def fetch_usage_command(command, *, timeout=20):
    """Query an already-authorized CLI with control frames only.

    Caller supplies a fixed broker-generated argv, with a short in-container
    timeout so killing docker exec cannot leave a remote child running forever.
    Stderr is discarded. Nothing here reads, returns or logs OAuth credentials.
    """
    if not isinstance(command, (list, tuple)) or not command or not all(isinstance(arg, str) for arg in command):
        raise UsageUnavailable("官方额度读取不可用")
    proc = None
    try:
        async with asyncio.timeout(timeout):
            proc = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                       limit=MAX_FRAME_BYTES)
            count, size = 0, 0
            async def request(subtype, **fields):
                nonlocal count, size
                request_id = "usage-" + uuid.uuid4().hex
                frame = {"type": "control_request", "request_id": request_id, "request": {"subtype": subtype, **fields}}
                proc.stdin.write((json.dumps(frame) + "\n").encode())
                await proc.stdin.drain()
                while True:
                    line = await proc.stdout.readline()
                    count, size = count + 1, size + len(line)
                    if not line or count > 128 or size > MAX_REPLY_BYTES or len(line) > MAX_FRAME_BYTES:
                        raise UsageUnavailable("官方额度读取不可用")
                    item = json.loads(line)
                    if not isinstance(item, dict) or item.get("type") == "control_request":
                        raise UsageUnavailable("官方额度读取不可用")
                    response = item.get("response")
                    if item.get("type") != "control_response" or not isinstance(response, dict) or response.get("request_id") != request_id:
                        continue
                    if response.get("subtype") != "success" or not isinstance(response.get("response"), dict):
                        raise UsageUnavailable("官方额度读取不可用")
                    return response["response"]
            await request("initialize", hooks=None)
            return await request("get_usage", skip_behaviors=True)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise UsageUnavailable(code="query_timeout") from None
    except UsageUnavailable:
        raise
    except Exception:
        raise UsageUnavailable("官方额度读取不可用") from None
    finally:
        if proc is not None:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except (asyncio.TimeoutError, ProcessLookupError):
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.kill()
                await asyncio.shield(proc.wait())
                raise

"""Account-wide token quotas and replay-safe usage cursors.

Limits are local admission policy, not an estimate of subscription entitlement.
Known official reset times are consumed once; unknown windows are never guessed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading


MAX_INTEGER = (1 << 63) - 1
USER_ID = re.compile(r"[0-9a-f]{32}\Z")
BUCKETS = (("fiveHour", "five_hour"), ("weekly", "weekly"))
ASSISTANT_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
MODEL_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


def integer(value, optional=False):
    if optional and value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise ValueError("额度和用量必须是非负整数")
    return value


def identity(user_id):
    if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
        raise ValueError("无效的用户标识")
    return user_id


def key(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("无效的用量事件标识")
    return value


def iso_time(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("无效的额度重置时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError("无效的额度重置时间") from None


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def usage_fields(usage, fields):
    if not isinstance(usage, dict):
        raise ValueError("无效的 Claude 用量数据")
    values = tuple(integer(usage.get(field, 0)) for field in fields)
    integer(sum(values))
    return values


def assistant_tokens(usage):
    return sum(usage_fields(usage, ASSISTANT_FIELDS))


def model_usage_tokens(model_usage):
    if not isinstance(model_usage, dict) or len(model_usage) > 100:
        raise ValueError("无效的 Claude 模型用量数据")
    total = 0
    for model, usage in model_usage.items():
        if not isinstance(model, str) or not model or len(model) > 200:
            raise ValueError("无效的 Claude 模型用量数据")
        total += sum(usage_fields(usage, MODEL_FIELDS))
    return integer(total)


class QuotaStore:
    def __init__(self, data_dir, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "quota.sqlite3"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("额度存储必须是普通文件")
            os.chmod(path, 0o600)
        finally:
            os.close(descriptor)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS quota_users (
                    user_id TEXT PRIMARY KEY,
                    five_hour_limit INTEGER CHECK (five_hour_limit IS NULL OR five_hour_limit>=0),
                    weekly_limit INTEGER CHECK (weekly_limit IS NULL OR weekly_limit>=0),
                    five_hour_used INTEGER NOT NULL DEFAULT 0 CHECK (five_hour_used>=0),
                    weekly_used INTEGER NOT NULL DEFAULT 0 CHECK (weekly_used>=0)
                );
                CREATE TABLE IF NOT EXISTS quota_windows (
                    bucket TEXT PRIMARY KEY,
                    reset_at TEXT,
                    observed_at TEXT,
                    last_reset TEXT
                );
                CREATE TABLE IF NOT EXISTS quota_events (
                    user_id TEXT NOT NULL REFERENCES quota_users(user_id) ON DELETE CASCADE,
                    event_key TEXT NOT NULL,
                    tokens INTEGER NOT NULL CHECK (tokens>=0),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id,event_key)
                );
                CREATE TABLE IF NOT EXISTS usage_sessions (
                    user_id TEXT NOT NULL REFERENCES quota_users(user_id) ON DELETE CASCADE,
                    session_key TEXT NOT NULL,
                    cumulative INTEGER NOT NULL DEFAULT 0,
                    pending INTEGER NOT NULL DEFAULT 0,
                    baseline_known INTEGER NOT NULL DEFAULT 0,
                    incomplete INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id,session_key)
                );
                CREATE TABLE IF NOT EXISTS usage_messages (
                    user_id TEXT NOT NULL REFERENCES quota_users(user_id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cache_read INTEGER NOT NULL,
                    cache_creation INTEGER NOT NULL,
                    PRIMARY KEY (user_id,message_id)
                );
            """)
            if "last_reset" not in {row[1] for row in self.db.execute("PRAGMA table_info(quota_windows)")}:
                self.db.execute("ALTER TABLE quota_windows ADD COLUMN last_reset TEXT")
            if "incomplete" not in {row[1] for row in self.db.execute("PRAGMA table_info(usage_sessions)")}:
                self.db.execute("ALTER TABLE usage_sessions ADD COLUMN incomplete INTEGER NOT NULL DEFAULT 0")
            for bucket, _ in BUCKETS:
                self.db.execute("INSERT OR IGNORE INTO quota_windows (bucket) VALUES (?)", (bucket,))
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self, *, roll_due=True):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if roll_due:
                    self._roll_due()
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _ensure_user(self, user_id):
        self.db.execute("INSERT OR IGNORE INTO quota_users (user_id) VALUES (?)", (identity(user_id),))

    def _snapshot(self, user_id):
        row = self.db.execute("SELECT five_hour_limit,weekly_limit,five_hour_used,weekly_used "
                              "FROM quota_users WHERE user_id=?", (user_id,)).fetchone()
        windows = dict(self.db.execute("SELECT bucket,reset_at FROM quota_windows"))
        result = {"exhausted": False, "concurrencyLimit": 1}
        for index, (bucket, _) in enumerate(BUCKETS):
            limit, used = integer(row[index], optional=True), integer(row[index + 2])
            result[bucket] = {"limit": limit, "used": used,
                              "remaining": None if limit is None else max(0, limit - used),
                              "resetAt": windows[bucket]}
            result["exhausted"] |= limit is not None and used >= limit
        return result

    def snapshot(self, user_id):
        with self.transaction():
            self._ensure_user(user_id)
            return self._snapshot(user_id)

    def set_limits(self, user_id, five_hour_limit, weekly_limit):
        integer(five_hour_limit, optional=True)
        integer(weekly_limit, optional=True)
        with self.transaction():
            self._ensure_user(user_id)
            self.db.execute("UPDATE quota_users SET five_hour_limit=?,weekly_limit=? WHERE user_id=?",
                            (five_hour_limit, weekly_limit, user_id))
            return self._snapshot(user_id)

    def distribute(self, user_ids, five_hour_total, weekly_total):
        if not isinstance(user_ids, (tuple, list)) or not user_ids or len(user_ids) > 1000:
            raise ValueError("请选择要分配额度的用户")
        for user_id in user_ids:
            identity(user_id)
        if len(set(user_ids)) != len(user_ids):
            raise ValueError("用户列表不能重复")
        integer(five_hour_total, optional=True)
        integer(weekly_total, optional=True)
        ordered = sorted(user_ids)
        def share(total, index):
            if total is None:
                return None
            quotient, remainder = divmod(total, len(ordered))
            return quotient + (index < remainder)
        with self.transaction():
            for index, user_id in enumerate(ordered):
                self._ensure_user(user_id)
                self.db.execute("UPDATE quota_users SET five_hour_limit=?,weekly_limit=? WHERE user_id=?",
                                (share(five_hour_total, index), share(weekly_total, index), user_id))
            return {user_id: self._snapshot(user_id) for user_id in ordered}

    def _charge(self, user_id, event_key, tokens):
        """Caller owns the transaction; cursor and debit commit together."""
        key(event_key)
        integer(tokens)
        previous = self.db.execute("SELECT tokens FROM quota_events WHERE user_id=? AND event_key=?",
                                   (user_id, event_key)).fetchone()
        if previous is not None:
            if previous[0] != tokens:
                raise ValueError("同一用量事件不能使用不同的数值")
            return False
        row = self.db.execute("SELECT five_hour_used,weekly_used FROM quota_users WHERE user_id=?", (user_id,)).fetchone()
        updated = tuple(integer(value + tokens) for value in row)
        self.db.execute("INSERT INTO quota_events VALUES (?,?,?,?)", (user_id, event_key, tokens, iso(self.clock())))
        self.db.execute("UPDATE quota_users SET five_hour_used=?,weekly_used=? WHERE user_id=?", (*updated, user_id))
        return True

    def charge(self, user_id, event_key, tokens):
        key(event_key)
        integer(tokens)
        with self.transaction():
            self._ensure_user(user_id)
            self._charge(user_id, event_key, tokens)
            return self._snapshot(user_id)

    def sync_windows(self, official_snapshot):
        if not isinstance(official_snapshot, dict):
            raise ValueError("无效的官方额度状态")
        now = self.clock().astimezone(timezone.utc)
        parsed = {}
        for bucket, _ in BUCKETS:
            item = official_snapshot.get(bucket)
            if item is None:
                continue
            if not isinstance(item, dict):
                raise ValueError("无效的官方额度状态")
            # One unknown weekly meter must not invalidate a fresh five-hour
            # deadline. Older callers without bucket metadata use the root gate.
            observation = item if "stale" in item or "updatedAt" in item else official_snapshot
            if observation.get("stale") is not False or observation.get("updatedAt") is None:
                continue
            observed = iso_time(observation["updatedAt"])
            if observed > now:
                continue
            if item.get("resetAt") is not None:
                parsed[bucket] = (iso_time(item["resetAt"]), observed)
        with self.transaction():
            for bucket, prefix in BUCKETS:
                values = parsed.get(bucket)
                if values is None:
                    continue
                reset, observed = values
                if reset <= now:
                    continue
                old_raw, seen_raw, consumed_raw = self.db.execute(
                    "SELECT reset_at,observed_at,last_reset FROM quota_windows WHERE bucket=?", (bucket,)).fetchone()
                old = iso_time(old_raw) if old_raw else None
                seen = iso_time(seen_raw) if seen_raw else None
                consumed = iso_time(consumed_raw) if consumed_raw else None
                if seen is not None and observed <= seen:
                    continue
                if consumed is not None and (reset <= consumed or observed < consumed):
                    continue
                if old is None or reset > old:
                    # Adoption or an official extension is not a reset:
                    # existing usage remains charged until the new deadline.
                    self.db.execute("UPDATE quota_windows SET reset_at=?,observed_at=? WHERE bucket=?",
                                    (iso(reset), iso(observed), bucket))
                elif reset == old:
                    self.db.execute("UPDATE quota_windows SET observed_at=? WHERE bucket=?", (iso(observed), bucket))
            return self._windows()

    def _roll_due(self):
        now = self.clock().astimezone(timezone.utc)
        for bucket, prefix in BUCKETS:
            row = self.db.execute("SELECT reset_at,last_reset FROM quota_windows WHERE bucket=?", (bucket,)).fetchone()
            if row is None or row[0] is None:
                continue
            reset = iso_time(row[0])
            if reset > now:
                continue
            consumed = iso_time(row[1]) if row[1] else None
            if consumed is None or reset > consumed:
                self.db.execute(f"UPDATE quota_users SET {prefix}_used=0")
                self.db.execute("UPDATE quota_windows SET reset_at=NULL,last_reset=? WHERE bucket=?", (iso(reset), bucket))
            else:
                self.db.execute("UPDATE quota_windows SET reset_at=NULL WHERE bucket=?", (bucket,))

    def _windows(self):
        return {bucket: {"resetAt": reset, "updatedAt": observed, "lastResetAt": consumed}
                for bucket, reset, observed, consumed in self.db.execute(
                    "SELECT bucket,reset_at,observed_at,last_reset FROM quota_windows")}

    def windows(self):
        with self.transaction():
            return self._windows()

    def clear_windows(self):
        """Forget a replaced Claude account's timing without refunding usage."""
        # The old account's deadline may already be due. Do not consume it on
        # this transaction: replacing account metadata never restores allowance.
        with self.transaction(roll_due=False):
            self.db.execute("UPDATE quota_windows SET reset_at=NULL,observed_at=NULL,last_reset=NULL")
            return self._windows()

    def delete_user(self, user_id):
        identity(user_id)
        with self.transaction():
            cursor = self.db.execute("DELETE FROM quota_users WHERE user_id=?", (user_id,))
            return bool(cursor.rowcount)

    def close(self):
        with self.lock:
            self.db.close()


class UsageTracker:
    """Reconcile assistant API messages with cumulative CLI modelUsage.

The caller passes the real Claude session UUID, including after resume. A new
session must be explicitly registered as fresh before its first assistant
frame. Unknown resumed sessions adopt their first result as a baseline instead
of charging historical usage again, and report incomplete initial metering.
"""
    def __init__(self, store):
        self.store = store

    def _ensure_session(self, user_id, session_key, fresh=False):
        key(session_key)
        self.store._ensure_user(user_id)
        self.store.db.execute("INSERT OR IGNORE INTO usage_sessions "
                              "(user_id,session_key,baseline_known,incomplete) VALUES (?,?,?,?)",
                              (user_id, session_key, int(fresh), int(not fresh)))

    def _snapshot(self, user_id, session_key, reason=None):
        known, incomplete = self.store.db.execute(
            "SELECT baseline_known,incomplete FROM usage_sessions WHERE user_id=? AND session_key=?",
            (user_id, session_key)).fetchone()
        metadata = {"complete": bool(known and not incomplete and reason is None),
                    "reason": reason or ("legacy_session_baseline" if incomplete else None)}
        return {**self.store._snapshot(user_id), "metering": metadata}

    def register_session(self, user_id, session_key, fresh=True):
        if type(fresh) is not bool:
            raise ValueError("无效的会话用量状态")
        with self.store.transaction():
            self._ensure_session(user_id, session_key, fresh)
            return self._snapshot(user_id, session_key)

    def assistant(self, user_id, payload, session_key):
        if not isinstance(payload, dict):
            raise ValueError("无效的 Claude 消息")
        message = payload.get("message")
        if not isinstance(message, dict) or not message.get("id") or message.get("usage") is None:
            with self.store.transaction():
                self._ensure_session(user_id, session_key)
                return self._snapshot(user_id, session_key, "assistant_usage_missing")
        message_id = key(message["id"])
        values = usage_fields(message["usage"], ASSISTANT_FIELDS)
        with self.store.transaction():
            self._ensure_session(user_id, session_key)
            previous = self.store.db.execute("SELECT input_tokens,output_tokens,cache_read,cache_creation "
                                              "FROM usage_messages WHERE user_id=? AND message_id=?",
                                              (user_id, message_id)).fetchone()
            merged = tuple(max(old, new) for old, new in zip(previous or (0, 0, 0, 0), values))
            total = integer(sum(merged))
            delta = total - sum(previous or (0, 0, 0, 0))
            self.store.db.execute("INSERT INTO usage_messages VALUES (?,?,?,?,?,?,?) "
                                  "ON CONFLICT(user_id,message_id) DO UPDATE SET "
                                  "input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,"
                                  "cache_read=excluded.cache_read,cache_creation=excluded.cache_creation",
                                  (user_id, message_id, session_key, *merged))
            if delta:
                pending = self.store.db.execute("SELECT pending FROM usage_sessions WHERE user_id=? AND session_key=?",
                                                (user_id, session_key)).fetchone()[0]
                pending = integer(pending + delta)
                event = "assistant:" + hashlib.sha256(message_id.encode()).hexdigest() + ":" + str(total)
                self.store._charge(user_id, event, delta)
                self.store.db.execute("UPDATE usage_sessions SET pending=? WHERE user_id=? AND session_key=?",
                                      (pending, user_id, session_key))
            return self._snapshot(user_id, session_key)

    def result(self, user_id, payload, session_key):
        if not isinstance(payload, dict):
            raise ValueError("无效的 Claude 结果")
        if payload.get("modelUsage") is None:
            with self.store.transaction():
                self._ensure_session(user_id, session_key)
                return self._snapshot(user_id, session_key, "result_model_usage_missing")
        cumulative = model_usage_tokens(payload["modelUsage"])
        with self.store.transaction():
            self._ensure_session(user_id, session_key)
            old, pending, known = self.store.db.execute(
                "SELECT cumulative,pending,baseline_known FROM usage_sessions WHERE user_id=? AND session_key=?",
                (user_id, session_key)).fetchone()
            if not known:
                # The initial result of an unknown resumed session includes its
                # old spend. Keep this turn's assistant charges, never bill the
                # unknown history as if it belonged to the current window.
                self.store.db.execute("UPDATE usage_sessions SET cumulative=?,pending=0,baseline_known=1,incomplete=1 "
                                      "WHERE user_id=? AND session_key=?", (cumulative, user_id, session_key))
            elif cumulative > old:
                advance = cumulative - old
                extra = max(0, advance - pending)
                event = "result:" + hashlib.sha256(session_key.encode()).hexdigest() + ":" + str(cumulative)
                self.store._charge(user_id, event, extra)
                self.store.db.execute("UPDATE usage_sessions SET cumulative=?,pending=? WHERE user_id=? AND session_key=?",
                                      (cumulative, max(0, pending - advance), user_id, session_key))
            # Equal or lower totals are old/replayed results. They cannot clear
            # pending charges from a newer turn or grant any quota refund.
            return self._snapshot(user_id, session_key)

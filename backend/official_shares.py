"""Local percentage shares derived from a shared official account's meters.

Official meters identify an account, never an individual website user. Positive
changes are apportioned using newly recorded task tokens. All state is durable
and shares use integer millionths of a percentage point internally.
"""
from datetime import timezone
import json
import math
import uuid

from .quotas import BUCKETS, identity, integer, iso, iso_time


SCALE = 1_000_000
FULL = 100 * SCALE
WAITING = "等待官方额度更新"
NOT_MEMBER = "未列入站内配额分配名单"
EXHAUSTED = "站内配额已用完，请等待官方额度周期恢复"


def split(total, weights):
    """Exact proportional apportionment; deterministic largest remainders."""
    integer(total)
    weights = {key: integer(value) for key, value in weights.items() if value > 0}
    denominator = sum(weights.values())
    if not denominator:
        return {}
    result = {key: total * value // denominator for key, value in weights.items()}
    remainder = total - sum(result.values())
    order = sorted(weights, key=lambda key: (-(total * weights[key] % denominator), key))
    for key in order[:remainder]:
        result[key] += 1
    return result


class OfficialShares:
    """Shares using the QuotaStore connection and its transaction lock.

    configure(user_ids, enabled=True, official_snapshot=None) retains allocations
    across disable/re-enable. settle() is called only after assistant/result
    accounting or an explicit/background refresh, never for a bare rate event.
    snapshot() does not refresh official data or mutate allocation state.
    """
    def __init__(self, quota_store):
        self.store = quota_store
        with self.store.transaction(roll_due=False):
            statements = (
                "CREATE TABLE IF NOT EXISTS share_config (id INTEGER PRIMARY KEY CHECK(id=1), "
                "enabled INTEGER NOT NULL DEFAULT 0, user_ids TEXT NOT NULL DEFAULT '[]', "
                "account_generation INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS share_windows (bucket TEXT PRIMARY KEY, "
                "generation INTEGER NOT NULL, cycle INTEGER NOT NULL, reset_at TEXT, "
                "observed_at TEXT NOT NULL, high_water INTEGER NOT NULL, pool INTEGER NOT NULL, "
                "unassigned INTEGER NOT NULL DEFAULT 0, window_state TEXT NOT NULL DEFAULT 'active', "
                "idle_since TEXT)",
                "CREATE TABLE IF NOT EXISTS share_members (bucket TEXT NOT NULL, user_id TEXT NOT NULL, "
                "allowance INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, "
                "overhead INTEGER NOT NULL DEFAULT 0, pending_weight INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(bucket,user_id))",
                "CREATE TABLE IF NOT EXISTS share_seen (user_id TEXT NOT NULL, event_key TEXT NOT NULL, "
                "PRIMARY KEY(user_id,event_key))",
            )
            for statement in statements:
                self.store.db.execute(statement)
            columns = {row[1]: row for row in self.db.execute("PRAGMA table_info(share_windows)")}
            if columns["reset_at"][3] or "window_state" not in columns or "idle_since" not in columns:
                # An idle window has no real deadline. Make the original column
                # nullable instead of persisting an invented reset timestamp.
                self.db.execute("CREATE TABLE share_windows_upgrade (bucket TEXT PRIMARY KEY, "
                                "generation INTEGER NOT NULL, cycle INTEGER NOT NULL, reset_at TEXT, "
                                "observed_at TEXT NOT NULL, high_water INTEGER NOT NULL, pool INTEGER NOT NULL, "
                                "unassigned INTEGER NOT NULL DEFAULT 0, "
                                "window_state TEXT NOT NULL DEFAULT 'active', idle_since TEXT)")
                state = "window_state" if "window_state" in columns else "'active'"
                idle = "idle_since" if "idle_since" in columns else "NULL"
                self.db.execute("INSERT INTO share_windows_upgrade SELECT "
                                "bucket,generation,cycle,reset_at,observed_at,high_water,pool,unassigned,"
                                + state + "," + idle + " FROM share_windows")
                self.db.execute("DROP TABLE share_windows")
                self.db.execute("ALTER TABLE share_windows_upgrade RENAME TO share_windows")
            self.store.db.execute("INSERT OR IGNORE INTO share_config (id) VALUES (1)")

    @property
    def db(self):
        return self.store.db

    def _now(self):
        return self.store.clock().astimezone(timezone.utc)

    def _config(self):
        enabled, raw, generation = self.db.execute(
            "SELECT enabled,user_ids,account_generation FROM share_config WHERE id=1").fetchone()
        return {"mode": "official_share" if enabled else "tokens", "userIds": json.loads(raw),
                "accountGeneration": generation}

    def config(self):
        with self.store.transaction(roll_due=False):
            return self._config()

    def _baseline_events(self):
        self.db.execute("INSERT OR IGNORE INTO share_seen SELECT user_id,event_key FROM quota_events")

    def _members_change_allowed(self):
        now = self._now()
        return all(state == "active" and reset is not None and iso_time(reset) <= now
                   for reset, state in self.db.execute("SELECT reset_at,window_state FROM share_windows"))

    def configure(self, user_ids=None, enabled=True, official_snapshot=None):
        if type(enabled) is not bool:
            raise ValueError("配额模式设置无效")
        if user_ids is not None:
            if not isinstance(user_ids, (list, tuple)) or len(user_ids) > 1000:
                raise ValueError("请选择要分配站内配额的用户")
            for user_id in user_ids:
                identity(user_id)
            if len(set(user_ids)) != len(user_ids):
                raise ValueError("用户列表不能重复")
            user_ids = sorted(user_ids)
        with self.store.transaction(roll_due=False):
            old = self._config()
            selected = old["userIds"] if user_ids is None else user_ids
            if enabled and not selected:
                raise ValueError("请至少选择一位用户分配站内配额")
            changed = selected != old["userIds"]
            if changed and not self._members_change_allowed():
                raise ValueError("五小时和每周的当前配额周期全部结束后，才能更改分配名单")
            if old["mode"] == "official_share" and not changed and official_snapshot is not None:
                self._settle(official_snapshot)
            if changed:
                self.db.execute("DELETE FROM share_members")
                self.db.execute("DELETE FROM share_windows")
            if changed or old["mode"] != "official_share" or not enabled:
                # Historical token events, including time spent in token mode,
                # cannot acquire a second allocation on enabling this policy.
                self._baseline_events()
                self.db.execute("UPDATE share_members SET pending_weight=0")
            self.db.execute("UPDATE share_config SET enabled=?,user_ids=? WHERE id=1",
                            (int(enabled), json.dumps(selected)))
            if enabled and official_snapshot is not None:
                self._settle(official_snapshot)
            return self._config()

    def _parse(self, snapshot):
        if not isinstance(snapshot, dict):
            return {}
        now = self._now()
        parsed = {}
        for bucket, _ in BUCKETS:
            item = snapshot.get(bucket)
            if not isinstance(item, dict):
                continue
            metadata = item if "stale" in item or "updatedAt" in item else snapshot
            if metadata.get("stale") is not False:
                continue
            percent = item.get("usedPercent")
            if type(percent) not in (int, float) or not 0 <= percent <= 100 or not math.isfinite(percent):
                continue
            try:
                observed = iso_time(metadata.get("updatedAt"))
            except ValueError:
                continue
            if observed > now:
                continue
            state, idle_since = item.get("state", "active"), None
            if state == "idle":
                # Only an explicit, freshly observed zero session meter is an
                # idle period. Missing rows and unknown deadlines are not zero.
                if bucket != "fiveHour" or percent != 0 or "resetAt" not in item or item["resetAt"] is not None:
                    continue
                try:
                    idle_since = iso_time(item.get("idleSince"))
                except ValueError:
                    continue
                if idle_since > observed:
                    continue
                reset = None
            elif state == "active":
                try:
                    reset = iso_time(item.get("resetAt"))
                except ValueError:
                    continue
                if reset <= now:
                    continue
            else:
                continue
            parsed[bucket] = (min(FULL, round(percent * SCALE)), observed, reset, state, idle_since)
        return parsed

    def _import_events(self, users):
        now = self._now()
        active = {bucket for bucket, reset, state in self.db.execute(
            "SELECT bucket,reset_at,window_state FROM share_windows")
            if state == "idle" or (reset is not None and iso_time(reset) > now)}
        rows = self.db.execute(
            "SELECT q.user_id,q.event_key,q.tokens FROM quota_events q LEFT JOIN share_seen s "
            "ON q.user_id=s.user_id AND q.event_key=s.event_key WHERE s.user_id IS NULL").fetchall()
        for user_id, event_key, tokens in rows:
            self.db.execute("INSERT INTO share_seen VALUES (?,?)", (user_id, event_key))
            if user_id not in users:
                continue
            for bucket in active:
                row = self.db.execute("SELECT pending_weight FROM share_members WHERE bucket=? AND user_id=?",
                                      (bucket, user_id)).fetchone()
                if row is not None:
                    pending = integer(row[0] + tokens)
                    self.db.execute("UPDATE share_members SET pending_weight=? WHERE bucket=? AND user_id=?",
                                    (pending, bucket, user_id))

    def _start_window(self, bucket, percent, observed, reset, config, cycle, state="active", idle_since=None):
        self.db.execute("DELETE FROM share_members WHERE bucket=?", (bucket,))
        pool = FULL - percent
        shares = split(pool, {user_id: 1 for user_id in config["userIds"]})
        for user_id in config["userIds"]:
            self.db.execute("INSERT INTO share_members (bucket,user_id,allowance) VALUES (?,?,?)",
                            (bucket, user_id, shares.get(user_id, 0)))
        self.db.execute("INSERT OR REPLACE INTO share_windows "
                        "(bucket,generation,cycle,reset_at,observed_at,high_water,pool,unassigned,window_state,idle_since) "
                        "VALUES (?,?,?,?,?,?,?,0,?,?)",
                        (bucket, config["accountGeneration"], cycle, iso(reset) if reset else None,
                         iso(observed), percent, pool, state, iso(idle_since) if idle_since else None))

    def _allocate_delta(self, bucket, delta):
        rows = self.db.execute("SELECT user_id,pending_weight,allowance,used,overhead "
                               "FROM share_members WHERE bucket=?", (bucket,)).fetchall()
        weights = {user_id: weight for user_id, weight, *_ in rows if weight > 0}
        if weights:
            amounts = split(delta, weights)
            available = {user_id: max(0, allowance - used - overhead)
                         for user_id, _, allowance, used, overhead in rows}
            overflow = sum(max(0, amount - available[user_id]) for user_id, amount in amounts.items())
            for user_id, amount in amounts.items():
                self.db.execute("UPDATE share_members SET used=used+? WHERE bucket=? AND user_id=?",
                                (amount, bucket, user_id))
            self.db.execute("UPDATE share_members SET pending_weight=0 WHERE bucket=?", (bucket,))
            # A late report may exceed the attributed user's whole share. Keep
            # that real debit, but reduce other available shares by the overflow
            # so aggregate remaining allocation cannot exceed the account pool.
            self._reduce_available(bucket, overflow)
        else:
            # An account-level increase with no task evidence is not a personal
            # charge. Reduce remaining allocations proportionately and expose
            # this adjustment separately from the user's attributed consumption.
            self._reduce_available(bucket, delta)
            self.db.execute("UPDATE share_windows SET unassigned=unassigned+? WHERE bucket=?", (delta, bucket))

    def _reduce_available(self, bucket, amount):
        if not amount:
            return
        available = {user_id: max(0, allowance - used - overhead)
                     for user_id, allowance, used, overhead in self.db.execute(
                         "SELECT user_id,allowance,used,overhead FROM share_members WHERE bucket=?", (bucket,))}
        reduction = min(amount, sum(available.values()))
        for user_id, adjustment in split(reduction, available).items():
            self.db.execute("UPDATE share_members SET overhead=overhead+? WHERE bucket=? AND user_id=?",
                            (adjustment, bucket, user_id))

    def _settle(self, official_snapshot):
        config = self._config()
        if config["mode"] != "official_share":
            return
        self._import_events(set(config["userIds"]))
        now = self._now()
        for bucket, (percent, observed, reset, state, idle_since) in self._parse(official_snapshot).items():
            previous = self.db.execute("SELECT cycle,reset_at,observed_at,high_water,window_state,idle_since "
                                       "FROM share_windows WHERE bucket=?", (bucket,)).fetchone()
            if previous is None:
                self._start_window(bucket, percent, observed, reset, config, 1, state, idle_since)
                continue
            cycle, old_reset_raw, old_observed_raw, high_water, old_state, old_idle_raw = previous
            old_reset = iso_time(old_reset_raw) if old_reset_raw else None
            old_observed = iso_time(old_observed_raw)
            if observed <= old_observed:
                continue
            if old_state == "idle":
                if state == "idle":
                    if idle_since == iso_time(old_idle_raw):
                        self.db.execute("UPDATE share_windows SET observed_at=? WHERE bucket=?",
                                        (iso(observed), bucket))
                    continue
                # The first task starts the official clock. Keep its existing
                # allocation and pending token weights, then charge its usage.
            else:
                if state == "idle":
                    if old_reset is not None and old_reset <= now and idle_since >= old_reset:
                        self._start_window(bucket, percent, observed, reset, config, cycle + 1, state, idle_since)
                    continue
                if old_reset is None or reset < old_reset:
                    continue
                if old_reset <= now:
                    if reset > old_reset and observed >= old_reset:
                        self._start_window(bucket, percent, observed, reset, config, cycle + 1)
                    continue
            # A later deadline observed before expiry extends this window. A
            # lower percentage never refunds users or resets their allocations.
            delta = max(0, percent - high_water)
            if delta:
                self._allocate_delta(bucket, delta)
            self.db.execute("UPDATE share_windows SET reset_at=?,observed_at=?,high_water=?,window_state='active' "
                            "WHERE bucket=?", (iso(reset), iso(observed), max(percent, high_water), bucket))

    def settle(self, official_snapshot):
        with self.store.transaction(roll_due=False):
            self._settle(official_snapshot)
            return self._config()

    def snapshot(self, user_id):
        identity(user_id)
        with self.store.transaction(roll_due=False):
            config = self._config()
            member = user_id in config["userIds"]
            result = {"mode": "official_share", "unit": "percent", "exhausted": False,
                      "blockedReason": None, "concurrencyLimit": 1}
            pending = False
            for bucket, _ in BUCKETS:
                window = self.db.execute("SELECT reset_at,high_water,unassigned,window_state,idle_since "
                                         "FROM share_windows WHERE bucket=?",
                                         (bucket,)).fetchone()
                row = self.db.execute("SELECT allowance,used,overhead FROM share_members WHERE bucket=? AND user_id=?",
                                      (bucket, user_id)).fetchone() if member else None
                reset = window[0] if window else None
                if not member:
                    item = {"limit": 0.0, "used": 0.0, "remaining": 0.0, "resetAt": reset}
                elif row is None or window is None:
                    item = {"limit": None, "used": None, "remaining": None, "resetAt": reset}
                    pending = True
                else:
                    allowance, used, overhead = row
                    remaining = min(max(0, allowance - used - overhead), max(0, FULL - window[1]))
                    item = {"limit": allowance / SCALE, "used": used / SCALE,
                            "remaining": remaining / SCALE, "resetAt": reset,
                            "sharedAdjustment": overhead / SCALE,
                            "unassignedAccountUsage": window[2] / SCALE}
                    if window[3] != "idle" and (reset is None or iso_time(reset) <= self._now()):
                        pending = True
                        item["remaining"] = None
                item["state"] = window[3] if window else "unknown"
                if window and window[3] == "idle":
                    item["idleSince"] = window[4]
                result[bucket] = item
                result["exhausted"] |= item["remaining"] is None or item["remaining"] <= 0
            if not member:
                result["blockedReason"] = NOT_MEMBER
            elif pending:
                result["blockedReason"] = WAITING
            elif result["exhausted"]:
                result["blockedReason"] = EXHAUSTED
            return result

    def account_changed(self):
        with self.store.transaction(roll_due=False):
            self.db.execute("UPDATE share_config SET account_generation=account_generation+1 WHERE id=1")
            self.db.execute("DELETE FROM share_windows")
            self.db.execute("DELETE FROM share_members")
            self._baseline_events()
            return self._config()

    def delete_user(self, user_id):
        identity(user_id)
        with self.store.transaction(roll_due=False):
            config = self._config()
            selected = [item for item in config["userIds"] if item != user_id]
            self.db.execute("UPDATE share_config SET user_ids=? WHERE id=1", (json.dumps(selected),))
            # Keep the old slot's balances and pending attribution under an
            # unrelated tombstone. Deletion never redistributes its allowance.
            self.db.execute("UPDATE share_members SET user_id=? WHERE user_id=?",
                            ("deleted-" + uuid.uuid4().hex, user_id))
            self.db.execute("DELETE FROM share_seen WHERE user_id=?", (user_id,))
            return user_id in config["userIds"]

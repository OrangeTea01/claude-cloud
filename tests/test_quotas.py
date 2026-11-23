"""Durable quota tests; Claude usage frames are synthetic documented examples.

No live Claude account response is used by these tests.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from backend.quotas import (QuotaStore, UsageTracker, MAX_INTEGER,
                           assistant_tokens, model_usage_tokens, iso)


ALICE, BOB, CAROL = "a" * 32, "b" * 32, "c" * 32
SESSION = "11111111-1111-4111-8111-111111111111"
SECOND_SESSION = "22222222-2222-4222-8222-222222222222"


class QuotaFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        self.store = QuotaStore(self.temporary.name, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def official(self, five=None, weekly=None, **extra):
        return {"stale": False, "updatedAt": iso(self.now),
                "fiveHour": {"resetAt": iso(five) if five else None},
                "weekly": {"resetAt": iso(weekly) if weekly else None}, **extra}

    def reopen(self):
        self.store.close()
        self.store = QuotaStore(self.temporary.name, clock=lambda: self.now)


class QuotaTests(QuotaFixture):
    def test_defaults_unlimited_and_zero_means_no_admission(self):
        state = self.store.snapshot(ALICE)
        self.assertFalse(state["exhausted"])
        self.assertEqual(state["concurrencyLimit"], 1)
        self.assertEqual(state["fiveHour"], {"limit": None, "used": 0, "remaining": None, "resetAt": None})
        self.assertTrue(self.store.set_limits(ALICE, 0, None)["exhausted"])
        self.assertTrue(self.store.set_limits(ALICE, None, 0)["exhausted"])

    def test_set_limits_preserves_use_and_can_restore_admission(self):
        self.store.charge(ALICE, "first", 90)
        state = self.store.set_limits(ALICE, 50, 80)
        self.assertTrue(state["exhausted"])
        self.assertEqual(state["fiveHour"]["used"], 90)
        self.assertEqual(state["weekly"]["remaining"], 0)
        state = self.store.set_limits(ALICE, 100, 1000)
        self.assertFalse(state["exhausted"])
        self.assertEqual(state["fiveHour"]["remaining"], 10)

    def test_event_replay_is_idempotent_and_conflicting_value_is_refused(self):
        first = self.store.charge(ALICE, "shared-request", 10)
        self.assertEqual(self.store.charge(ALICE, "shared-request", 10), first)
        with self.assertRaises(ValueError):
            self.store.charge(ALICE, "shared-request", 11)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 10)
        self.assertEqual(self.store.charge(BOB, "shared-request", 20)["weekly"]["used"], 20)

    def test_average_distribution_has_exact_stable_remainder_and_keeps_used(self):
        self.store.charge(ALICE, "already-used", 8)
        assigned = self.store.distribute([CAROL, ALICE, BOB], 10, 11)
        self.assertEqual(list(assigned), [ALICE, BOB, CAROL])
        self.assertEqual([state["fiveHour"]["limit"] for state in assigned.values()], [4, 3, 3])
        self.assertEqual([state["weekly"]["limit"] for state in assigned.values()], [4, 4, 3])
        self.assertEqual(assigned[ALICE]["fiveHour"]["used"], 8)
        self.assertTrue(assigned[ALICE]["exhausted"])
        self.assertEqual(self.store.distribute([BOB, CAROL, ALICE], 10, 11), assigned)
        for state in self.store.distribute([ALICE, BOB], None, 0).values():
            self.assertIsNone(state["fiveHour"]["limit"])
            self.assertEqual(state["weekly"]["limit"], 0)

    def test_invalid_values_and_overflow_never_modify_balances(self):
        for value in (-1, 1.0, float("nan"), float("inf"), True, "12", MAX_INTEGER + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.set_limits(ALICE, value, None)
            with self.subTest(charge=value), self.assertRaises(ValueError):
                self.store.charge(ALICE, "bad", value)
        for users in ([], [ALICE, ALICE], ["invalid"], "alice"):
            with self.subTest(users=users), self.assertRaises(ValueError):
                self.store.distribute(users, 10, 20)
        self.store.charge(ALICE, "largest", MAX_INTEGER)
        with self.assertRaises(ValueError):
            self.store.charge(ALICE, "overflow", 1)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], MAX_INTEGER)

    def test_same_user_concurrent_events_and_replays_across_connections(self):
        another = QuotaStore(self.temporary.name, clock=lambda: self.now)
        try:
            calls = [(index % 20, self.store if index % 2 else another) for index in range(100)]
            def charge(item):
                event, store = item
                return store.charge(ALICE, "event-" + str(event), 7)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(charge, calls))
            self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 140)
            self.assertEqual(another.snapshot(ALICE)["weekly"]["used"], 140)
        finally:
            another.close()

    def test_restart_keeps_limits_use_and_replay_keys(self):
        self.store.set_limits(ALICE, 50, 500)
        self.store.charge(ALICE, "before-restart", 40)
        self.reopen()
        self.assertEqual(self.store.charge(ALICE, "before-restart", 40)["fiveHour"]["remaining"], 10)

    def test_first_official_window_binding_preserves_existing_use(self):
        self.store.charge(ALICE, "existing", 10)
        reset = self.now + timedelta(hours=5)
        self.store.sync_windows(self.official(reset, self.now + timedelta(days=7)))
        state = self.store.snapshot(ALICE)
        self.assertEqual(state["fiveHour"]["used"], 10)
        self.assertEqual(state["fiveHour"]["resetAt"], iso(reset))

    def test_known_deadline_restores_once_without_waiting_for_another_request(self):
        reset = self.now + timedelta(hours=5)
        self.store.sync_windows(self.official(reset, self.now + timedelta(days=7)))
        self.store.set_limits(ALICE, 10, 100)
        self.store.charge(ALICE, "used-up", 10)
        self.assertTrue(self.store.snapshot(ALICE)["exhausted"])
        self.now = reset
        state = self.store.snapshot(ALICE)
        self.assertFalse(state["exhausted"])
        self.assertEqual(state["fiveHour"]["used"], 0)
        self.assertIsNone(state["fiveHour"]["resetAt"])
        self.assertEqual(state["weekly"]["used"], 10)
        self.store.charge(ALICE, "new-period", 4)
        self.assertEqual(self.store.charge(ALICE, "used-up", 10)["fiveHour"]["used"], 4)
        self.store.sync_windows(self.official(reset))
        self.reopen()
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 4)

    def test_unknown_windows_and_long_offline_periods_do_not_get_invented_resets(self):
        self.store.charge(ALICE, "initial", 10)
        self.now += timedelta(days=100)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 10)
        reset = self.now + timedelta(hours=5)
        self.store.sync_windows(self.official(reset))
        self.now += timedelta(days=100)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 0)
        self.store.charge(ALICE, "offline-after-known-reset", 7)
        self.now += timedelta(days=100)
        self.reopen()
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 7)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 17)

    def test_new_official_window_binds_after_consumed_deadline_without_clearing_again(self):
        old = self.now + timedelta(hours=5)
        self.store.sync_windows(self.official(old))
        self.store.charge(ALICE, "old", 10)
        self.now = old + timedelta(minutes=1)
        self.store.snapshot(ALICE)
        self.store.charge(ALICE, "new", 6)
        new = self.now + timedelta(hours=4)
        self.store.sync_windows(self.official(new))
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 6)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["resetAt"], iso(new))
        self.now = new
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 0)

    def test_fresh_official_extension_defers_reset_without_refunding_used_tokens(self):
        old = self.now + timedelta(hours=2)
        later = old + timedelta(hours=1)
        self.store.set_limits(ALICE, 10, 100)
        self.store.charge(ALICE, "existing", 10)
        self.store.sync_windows(self.official(old))
        self.now += timedelta(minutes=1)
        self.store.sync_windows(self.official(later))
        state = self.store.snapshot(ALICE)
        self.assertEqual(state["fiveHour"]["resetAt"], iso(later))
        self.assertEqual(state["fiveHour"]["used"], 10)
        self.assertTrue(state["exhausted"])
        # A newer observation of an earlier deadline cannot undo the extension.
        self.now += timedelta(minutes=1)
        self.store.sync_windows(self.official(old))
        self.reopen()
        self.now = old
        state = self.store.snapshot(ALICE)
        self.assertEqual(state["fiveHour"]["resetAt"], iso(later))
        self.assertEqual(state["fiveHour"]["used"], 10)
        self.assertTrue(state["exhausted"])
        self.now = later
        state = self.store.snapshot(ALICE)
        self.assertEqual(state["fiveHour"]["used"], 0)
        self.assertEqual(state["weekly"]["used"], 10)
        self.assertFalse(state["exhausted"])

    def test_five_hour_and_weekly_reset_independently(self):
        five, week = self.now + timedelta(hours=5), self.now + timedelta(days=7)
        self.store.sync_windows(self.official(five, week))
        self.store.charge(ALICE, "before", 10)
        self.now = five
        self.store.charge(ALICE, "after-five", 4)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 4)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 14)
        self.now = week
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 4)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 0)

    def test_switching_account_forgets_old_deadlines_without_refunding_any_user(self):
        self.store.set_limits(ALICE, 10, 100)
        self.store.set_limits(BOB, 50, 500)
        tracker = UsageTracker(self.store)
        tracker.register_session(ALICE, SESSION)
        tracker.assistant(ALICE, {"message": {"id": "msg-a", "usage": {"input_tokens": 10}}}, SESSION)
        self.store.charge(BOB, "bob-used", 25)
        old_reset = self.now + timedelta(hours=1)
        self.store.sync_windows(self.official(old_reset, old_reset))
        tables = ("quota_users", "quota_events", "usage_sessions", "usage_messages")
        before = {table: self.store.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        # No quota read happens between reaching the old deadline and switching
        # the shared official account: clear itself must never trigger a reset.
        self.now = old_reset + timedelta(minutes=1)
        cleared = self.store.clear_windows()
        for value in cleared.values():
            self.assertEqual(value, {"resetAt": None, "updatedAt": None, "lastResetAt": None})
        for table, rows in before.items():
            self.assertEqual(self.store.db.execute(f"SELECT * FROM {table}").fetchall(), rows)
        self.reopen()
        self.now += timedelta(days=10)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 10)
        self.assertEqual(self.store.snapshot(BOB)["weekly"]["used"], 25)
        self.assertTrue(self.store.snapshot(ALICE)["exhausted"])
        new_reset = self.now + timedelta(hours=2)
        self.store.sync_windows(self.official(new_reset, self.now + timedelta(days=7)))
        state = self.store.snapshot(ALICE)
        self.assertEqual(state["fiveHour"]["used"], 10)
        self.assertEqual(state["fiveHour"]["resetAt"], iso(new_reset))
        self.now = new_reset
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 0)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 10)

    def test_clear_windows_removes_consumed_reset_metadata_and_future_deadlines(self):
        first = self.now + timedelta(hours=1)
        self.store.sync_windows(self.official(first, self.now + timedelta(days=7)))
        self.store.charge(ALICE, "before-reset", 10)
        self.now = first
        self.store.charge(ALICE, "after-reset", 4)
        self.assertEqual(self.store.windows()["fiveHour"]["lastResetAt"], iso(first))
        self.store.clear_windows()
        self.reopen()
        self.now += timedelta(days=8)
        self.assertEqual(self.store.windows(), {
            bucket: {"resetAt": None, "updatedAt": None, "lastResetAt": None}
            for bucket in ("fiveHour", "weekly")})
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 4)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 14)

    def test_failed_clear_windows_rolls_back_metadata_without_touching_allowance(self):
        reset = self.now + timedelta(hours=1)
        self.store.sync_windows(self.official(reset, self.now + timedelta(days=7)))
        self.store.charge(ALICE, "existing", 10)
        before = self.store.windows()
        connection = self.store.db
        self.store.db = Mock(wraps=connection)
        self.store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.clear_windows()
        finally:
            self.store.db = connection
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.store.windows(), before)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 10)

    def test_stale_missing_future_or_old_observation_cannot_supply_another_reset(self):
        initial = self.now
        future = self.now + timedelta(hours=5)
        for observation in (self.official(future, stale=True), self.official(future, stale=None),
                            self.official(future, updatedAt=None),
                            self.official(future, updatedAt=iso(self.now + timedelta(minutes=1)))):
            self.store.sync_windows(observation)
            self.assertIsNone(self.store.snapshot(ALICE)["fiveHour"]["resetAt"])
        self.store.sync_windows(self.official(future))
        self.now = future
        self.store.charge(ALICE, "after-reset", 3)
        self.store.sync_windows(self.official(self.now + timedelta(hours=5), updatedAt=iso(initial)))
        self.assertIsNone(self.store.snapshot(ALICE)["fiveHour"]["resetAt"])
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 3)

    def test_past_first_observation_does_not_retroactively_reset_usage(self):
        self.store.charge(ALICE, "current", 9)
        self.store.sync_windows(self.official(self.now - timedelta(hours=1)))
        state = self.store.snapshot(ALICE)
        self.assertEqual(state["fiveHour"]["used"], 9)
        self.assertIsNone(state["fiveHour"]["resetAt"])

    def test_fresh_partial_bucket_can_bind_while_other_official_meter_is_unknown(self):
        reset = self.now + timedelta(hours=5)
        self.store.charge(ALICE, "existing", 10)
        observation = self.official(stale=True)
        observation["fiveHour"] = {"resetAt": iso(reset), "updatedAt": iso(self.now), "stale": False}
        observation["weekly"] = {"resetAt": None, "updatedAt": None, "stale": True}
        self.store.sync_windows(observation)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["resetAt"], iso(reset))
        self.now = reset
        state = self.store.snapshot(ALICE)
        self.assertEqual(state["fiveHour"]["used"], 0)
        self.assertEqual(state["weekly"]["used"], 10)

    def test_invalid_or_ambiguous_reset_time_is_rejected_atomically(self):
        for value in ("not-a-date", "2026-11-17T17:00:00", 123, ""):
            observation = self.official()
            observation["fiveHour"]["resetAt"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.sync_windows(observation)
        observation = self.official()
        observation["fiveHour"]["resetAt"] = "2026-11-18T01:00:00+08:00"
        self.store.sync_windows(observation)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["resetAt"], "2026-11-17T17:00:00Z")

    def test_failed_distribution_or_charge_commit_rolls_back_every_change(self):
        self.store.set_limits(ALICE, 10, 100)
        connection = self.store.db
        self.store.db = Mock(wraps=connection)
        self.store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        with self.assertRaises(sqlite3.OperationalError):
            self.store.distribute([ALICE, BOB], 100, 1000)
        self.assertFalse(connection.in_transaction)
        with self.assertRaises(sqlite3.OperationalError):
            self.store.charge(ALICE, "failed", 7)
        self.store.db = connection
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["limit"], 10)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 0)
        self.assertIsNone(connection.execute("SELECT 1 FROM quota_users WHERE user_id=?", (BOB,)).fetchone())
        self.assertEqual(self.store.charge(ALICE, "failed", 7)["fiveHour"]["used"], 7)

    def test_user_deletion_cascades_usage_but_preserves_other_accounts(self):
        tracker = UsageTracker(self.store)
        tracker.register_session(ALICE, SESSION)
        tracker.assistant(ALICE, {"message": {"id": "msg-a", "usage": {"input_tokens": 10}}}, SESSION)
        self.store.charge(BOB, "bob", 9)
        self.assertTrue(self.store.delete_user(ALICE))
        self.assertFalse(self.store.delete_user(ALICE))
        for table in ("quota_users", "quota_events", "usage_sessions", "usage_messages"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table} WHERE user_id=?", (ALICE,)).fetchone()[0], 0)
        self.assertEqual(self.store.snapshot(BOB)["weekly"]["used"], 9)


class UsageTrackerTests(QuotaFixture):
    """Frames follow official field names but are not captured live responses."""
    def setUp(self):
        super().setUp()
        self.tracker = UsageTracker(self.store)

    @staticmethod
    def assistant(message_id, input=0, output=0, read=0, create=0):
        return {"type": "assistant", "message": {"id": message_id,
                "usage": {"input_tokens": input, "output_tokens": output,
                          "cache_read_input_tokens": read, "cache_creation_input_tokens": create}}}

    @staticmethod
    def result(total, child=0):
        models = {"main": {"inputTokens": total, "outputTokens": 0,
                           "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}
        if child:
            models["child"] = {"inputTokens": child}
        return {"type": "result", "modelUsage": models, "usage": {"input_tokens": 999999}}

    def test_token_parsers_include_cache_and_reject_noninteger_counts(self):
        self.assertEqual(assistant_tokens(self.assistant("x", 1, 2, 3, 4)["message"]["usage"]), 10)
        self.assertEqual(model_usage_tokens(self.result(10, 5)["modelUsage"]), 15)
        for value in (-1, True, 1.0, float("nan"), float("inf"), "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                assistant_tokens({"output_tokens": value})
            with self.subTest(model=value), self.assertRaises(ValueError):
                model_usage_tokens({"main": {"inputTokens": value}})

    def test_assistant_message_replay_and_partial_field_updates_charge_only_growth(self):
        self.tracker.register_session(ALICE, SESSION)
        frame = self.assistant("msg-1", 10, 2, 3, 4)
        self.assertEqual(self.tracker.assistant(ALICE, frame, SESSION)["weekly"]["used"], 19)
        self.assertEqual(self.tracker.assistant(ALICE, frame, SESSION)["weekly"]["used"], 19)
        partial = {"message": {"id": "msg-1", "usage": {"output_tokens": 8}}}
        self.assertEqual(self.tracker.assistant(ALICE, partial, SESSION)["weekly"]["used"], 25)
        self.assertEqual(self.tracker.assistant(ALICE, frame, SESSION)["weekly"]["used"], 25)

    def test_cumulative_result_adds_only_unmetered_subagent_usage(self):
        self.tracker.register_session(ALICE, SESSION)
        self.tracker.assistant(ALICE, self.assistant("msg-1", 100), SESSION)
        result = self.result(100, child=50)
        state = self.tracker.result(ALICE, result, SESSION)
        self.assertEqual(state["weekly"]["used"], 150)
        self.assertTrue(state["metering"]["complete"])
        self.assertEqual(self.tracker.result(ALICE, result, SESSION)["weekly"]["used"], 150)

    def test_replayed_old_result_does_not_erase_new_turn_pending_credit(self):
        self.tracker.register_session(ALICE, SESSION)
        self.tracker.assistant(ALICE, self.assistant("old", 100), SESSION)
        self.tracker.result(ALICE, self.result(100), SESSION)
        self.tracker.assistant(ALICE, self.assistant("new", 40), SESSION)
        self.tracker.result(ALICE, self.result(100), SESSION)
        self.tracker.result(ALICE, self.result(50), SESSION)
        self.assertEqual(self.tracker.result(ALICE, self.result(150), SESSION)["weekly"]["used"], 150)

    def test_restart_resume_deduplicates_message_and_model_cumulative_totals(self):
        self.tracker.register_session(ALICE, SESSION)
        frame = self.assistant("msg-before", 100)
        self.tracker.assistant(ALICE, frame, SESSION)
        self.tracker.result(ALICE, self.result(120), SESSION)
        self.reopen()
        self.tracker = UsageTracker(self.store)
        self.tracker.register_session(ALICE, SESSION, fresh=True)  # Cannot overwrite a saved cursor.
        self.tracker.assistant(ALICE, frame, SESSION)
        self.assertEqual(self.tracker.result(ALICE, self.result(120), SESSION)["weekly"]["used"], 120)
        self.tracker.assistant(ALICE, self.assistant("msg-after", 30), SESSION)
        self.assertEqual(self.tracker.result(ALICE, self.result(160), SESSION)["weekly"]["used"], 160)

    def test_unknown_old_resume_adopts_baseline_without_billing_historical_usage(self):
        self.tracker.register_session(ALICE, SESSION, fresh=False)
        self.tracker.assistant(ALICE, self.assistant("current", 20), SESSION)
        state = self.tracker.result(ALICE, self.result(1000), SESSION)
        self.assertEqual(state["weekly"]["used"], 20)
        self.assertFalse(state["metering"]["complete"])
        self.assertEqual(state["metering"]["reason"], "legacy_session_baseline")
        self.tracker.assistant(ALICE, self.assistant("next", 15), SESSION)
        self.assertEqual(self.tracker.result(ALICE, self.result(1030), SESSION)["weekly"]["used"], 50)

    def test_multiple_workspace_sessions_share_one_account_balance(self):
        for session, amount in ((SESSION, 100), (SECOND_SESSION, 60)):
            self.tracker.register_session(ALICE, session)
            self.tracker.assistant(ALICE, self.assistant("message-" + session, amount), session)
            self.tracker.result(ALICE, self.result(amount), session)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 160)

    def test_concurrent_identical_assistant_frames_are_not_double_charged(self):
        self.tracker.register_session(ALICE, SESSION)
        frame = self.assistant("same", 10)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: self.tracker.assistant(ALICE, frame, SESSION), range(20)))
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 10)

    def test_invalid_usage_and_failed_commit_leave_no_cursor_or_debit(self):
        self.tracker.register_session(ALICE, SESSION)
        frame = self.assistant("msg-fail", 10)
        connection = self.store.db
        self.store.db = Mock(wraps=connection)
        self.store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        with self.assertRaises(sqlite3.OperationalError):
            self.tracker.assistant(ALICE, frame, SESSION)
        self.store.db = connection
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 0)
        self.assertEqual(connection.execute("SELECT count(*) FROM usage_messages").fetchone()[0], 0)
        self.assertEqual(self.tracker.assistant(ALICE, frame, SESSION)["weekly"]["used"], 10)
        invalid = self.assistant("invalid", -1)
        with self.assertRaises(ValueError):
            self.tracker.assistant(ALICE, invalid, SESSION)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 10)

    def test_missing_usage_is_explicitly_incomplete_and_never_fabricated(self):
        self.tracker.register_session(ALICE, SESSION)
        state = self.tracker.assistant(ALICE, {"message": {"content": []}}, SESSION)
        self.assertFalse(state["metering"]["complete"])
        self.assertEqual(state["weekly"]["used"], 0)
        state = self.tracker.result(ALICE, {"usage": {"input_tokens": 999}}, SESSION)
        self.assertFalse(state["metering"]["complete"])
        self.assertEqual(state["weekly"]["used"], 0)
        self.assertEqual(self.tracker.result(ALICE, self.result(50), SESSION)["weekly"]["used"], 50)


if __name__ == "__main__":
    unittest.main()

"""Local allocation accounting using synthetic official percentage reports."""
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from backend.official_shares import OfficialShares, FULL, SCALE, WAITING, NOT_MEMBER, split
from backend.quotas import QuotaStore, iso


USERS = [character * 32 for character in "abcde"]
ALICE, BOB, CAROL, DAVE, ERIN = USERS
OTHER = "f" * 32


class OfficialSharesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        self.store = QuotaStore(self.directory.name, clock=lambda: self.now)
        self.shares = OfficialShares(self.store)
        self.five = self.now + timedelta(hours=5)
        self.week = self.now + timedelta(days=7)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def official(self, five=0, weekly=0, *, stale=False):
        return {"stale": stale, "updatedAt": iso(self.now),
                "fiveHour": {"usedPercent": five, "resetAt": iso(self.five),
                             "updatedAt": iso(self.now), "stale": stale},
                "weekly": {"usedPercent": weekly, "resetAt": iso(self.week),
                           "updatedAt": iso(self.now), "stale": stale}}

    def enable(self, five=0, weekly=0):
        return self.shares.configure(USERS, official_snapshot=self.official(five, weekly))

    def idle(self, *, since=None, weekly=0):
        report = self.official(weekly=weekly)
        report["fiveHour"] = {"state": "idle", "usedPercent": 0, "resetAt": None,
                              "updatedAt": iso(self.now), "stale": False,
                              "idleSince": iso(since or self.now)}
        return report

    def tick(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def settle(self, five, weekly):
        self.tick()
        return self.shares.settle(self.official(five, weekly))

    def quota(self, user=ALICE, bucket="fiveHour"):
        return self.shares.snapshot(user)[bucket]

    def reopen(self):
        self.store.close()
        self.store = QuotaStore(self.directory.name, clock=lambda: self.now)
        self.shares = OfficialShares(self.store)

    def test_disabled_by_default_and_initial_remaining_pool_is_split_five_ways(self):
        self.assertEqual(self.shares.config()["mode"], "tokens")
        self.enable(40, 50)
        for user in USERS:
            state = self.shares.snapshot(user)
            self.assertEqual((state["mode"], state["unit"]), ("official_share", "percent"))
            self.assertEqual(state["fiveHour"]["limit"], 12)
            self.assertEqual(state["fiveHour"]["remaining"], 12)
            self.assertEqual(state["weekly"]["limit"], 10)
            self.assertFalse(state["exhausted"])
            self.assertIsNone(state["blockedReason"])
            self.assertEqual(state["concurrencyLimit"], 1)

    def test_initial_historical_tokens_are_not_attributed_to_future_official_changes(self):
        self.store.charge(ALICE, "historical", 10000)
        self.enable(40, 40)
        self.store.charge(BOB, "new", 1)
        self.settle(45, 45)
        self.assertEqual(self.quota(ALICE)["used"], 0)
        self.assertEqual(self.quota(BOB)["used"], 5)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 10000)

    def test_parallel_task_tokens_share_each_official_delta_proportionally(self):
        self.enable()
        self.store.charge(ALICE, "a1", 10)
        self.store.charge(BOB, "b1", 30)
        self.settle(8, 4)
        self.assertEqual((self.quota(ALICE)["used"], self.quota(BOB)["used"]), (2, 6))
        self.assertEqual(self.quota(ALICE, "weekly")["used"], 1)
        self.assertEqual(self.quota(BOB, "weekly")["used"], 3)
        self.assertEqual(sum(self.quota(user)["used"] for user in USERS), 8)

    def test_zero_official_delta_keeps_pending_work_until_delayed_meter_updates(self):
        self.enable()
        self.store.charge(ALICE, "a1", 10)
        self.settle(0, 0)
        self.store.charge(BOB, "b1", 30)
        self.settle(8, 0)
        self.assertEqual(self.quota(ALICE)["used"], 2)
        self.assertEqual(self.quota(BOB)["used"], 6)
        # Weekly lag keeps its own pending weights after the five-hour debit.
        self.settle(8, 4)
        self.assertEqual(self.quota(ALICE, "weekly")["used"], 1)
        self.assertEqual(self.quota(BOB, "weekly")["used"], 3)

    def test_repeated_reports_event_replay_and_restart_never_double_debit(self):
        self.enable()
        self.store.charge(ALICE, "same-event", 10)
        self.settle(4, 2)
        report = self.official(4, 2)
        before = self.shares.snapshot(ALICE)
        self.shares.settle(report)
        self.reopen()
        self.store.charge(ALICE, "same-event", 10)
        self.shares.settle(report)
        self.assertEqual(self.shares.snapshot(ALICE), before)
        self.settle(8, 4)
        self.assertEqual(self.quota()["used"], 4)
        self.assertEqual(self.quota()["sharedAdjustment"], 0.666667)
        self.assertEqual(sum(round(self.quota(user)["sharedAdjustment"] * SCALE) for user in USERS), 4 * SCALE)

    def test_no_weight_account_increase_reduces_available_without_fabricating_personal_use(self):
        self.enable()
        self.settle(10, 20)
        for user in USERS:
            self.assertEqual(self.quota(user)["used"], 0)
            self.assertEqual(self.quota(user)["remaining"], 18)
            self.assertEqual(self.quota(user)["sharedAdjustment"], 2)
            self.assertEqual(self.quota(user)["unassignedAccountUsage"], 10)

    def test_unassigned_decrease_uses_available_balances_and_preserves_total_pool(self):
        self.enable()
        self.store.charge(ALICE, "uses-full-slot", 10)
        self.settle(20, 20)
        self.assertEqual(self.quota()["remaining"], 0)
        self.settle(40, 40)
        self.assertEqual(sum(self.quota(user)["remaining"] for user in USERS), 60)
        self.assertEqual(self.quota(ALICE)["used"], 20)
        for user in USERS[1:]:
            self.assertEqual(self.quota(user)["remaining"], 15)

    def test_late_personal_overrun_preserves_its_debit_and_reduces_other_shares(self):
        self.enable()
        self.store.charge(ALICE, "late-large-task", 100)
        self.settle(40, 40)
        self.assertEqual(self.quota(ALICE)["used"], 40)
        self.assertEqual(self.quota(ALICE)["remaining"], 0)
        for user in USERS[1:]:
            self.assertEqual(self.quota(user)["used"], 0)
            self.assertEqual(self.quota(user)["remaining"], 15)
            self.assertEqual(self.quota(user)["sharedAdjustment"], 5)
            self.assertEqual(self.quota(user)["unassignedAccountUsage"], 0)
        self.assertEqual(sum(self.quota(user)["remaining"] for user in USERS), 60)

    def test_three_exhausted_slots_and_deleted_slot_cannot_overallocate_account_remainder(self):
        self.enable()
        self.shares.delete_user(ERIN)
        self.store.delete_user(ERIN)
        for user, percentage in ((ALICE, 30), (BOB, 60), (CAROL, 90)):
            self.store.charge(user, "overshoots-personal-share", 100)
            self.settle(percentage, percentage)
            rows = self.store.db.execute(
                "SELECT allowance,used,overhead FROM share_members WHERE bucket='fiveHour'").fetchall()
            all_remaining = sum(max(0, allowance - used - overhead) for allowance, used, overhead in rows)
            self.assertEqual(all_remaining, (100 - percentage) * SCALE)
            self.assertLessEqual(sum(round(self.quota(member)["remaining"] * SCALE) for member in USERS[:-1]),
                                 (100 - percentage) * SCALE)
        for user in (ALICE, BOB, CAROL):
            self.assertEqual(self.quota(user)["used"], 30)
            self.assertEqual(self.quota(user)["remaining"], 0)
        self.assertEqual(self.quota(DAVE)["remaining"], 5)
        self.settle(100, 100)
        self.assertEqual(self.quota(DAVE)["remaining"], 0)
        self.assertEqual(self.quota(DAVE)["used"], 0)

    def test_percent_drop_and_deadline_extension_do_not_refund_or_start_new_cycle(self):
        self.enable()
        self.store.charge(ALICE, "usage", 10)
        self.settle(10, 10)
        old = self.five
        self.five += timedelta(hours=2)
        self.settle(5, 5)
        self.assertEqual(self.quota()["limit"], 20)
        self.assertEqual(self.quota()["used"], 10)
        self.assertEqual(self.quota()["resetAt"], iso(self.five))
        self.now = old
        self.assertEqual(self.quota()["remaining"], 10)
        self.assertFalse(self.shares.snapshot(ALICE)["exhausted"])

    def test_expired_window_waits_for_new_official_window_instead_of_guessing_full_pool(self):
        self.enable()
        self.store.charge(ALICE, "usage", 10)
        self.settle(20, 20)
        self.now = self.five
        state = self.shares.snapshot(ALICE)
        self.assertTrue(state["exhausted"])
        self.assertEqual(state["blockedReason"], WAITING)
        self.assertEqual(state["fiveHour"]["used"], 20)
        self.assertEqual(state["fiveHour"]["limit"], 20)
        self.assertIsNone(state["fiveHour"]["remaining"])
        self.shares.settle(self.official())  # Expired old deadline is unusable.
        self.assertEqual(self.quota()["used"], 20)
        self.five = self.now + timedelta(hours=5)
        self.tick()
        self.shares.settle(self.official(0, 20))
        self.assertEqual(self.quota()["limit"], 20)
        self.assertEqual(self.quota()["used"], 0)
        self.assertEqual(self.quota(ALICE, "weekly")["used"], 20)

    def test_new_window_first_seen_partly_used_splits_only_actual_remaining(self):
        self.enable(40, 0)
        self.now = self.five + timedelta(minutes=1)
        self.five = self.now + timedelta(hours=5)
        self.shares.settle(self.official(25, 0))
        self.assertEqual(self.quota()["limit"], 15)
        self.assertEqual(self.quota()["remaining"], 15)

    def test_unknown_stale_future_and_invalid_data_cannot_create_allocations(self):
        self.shares.configure(USERS)
        bad = [self.official(stale=True), self.official(five=float("nan"), weekly=float("inf")),
               self.official(five=True, weekly=-1), self.official(five=101, weekly="20")]
        future = self.official()
        for bucket in ("fiveHour", "weekly"):
            future[bucket]["updatedAt"] = iso(self.now + timedelta(seconds=10))
        bad.append(future)
        for report in bad:
            self.shares.settle(report)
            for field in ("limit", "used", "remaining"):
                self.assertIsNone(self.quota()[field])
            self.assertEqual(self.shares.snapshot(ALICE)["blockedReason"], WAITING)
            self.assertTrue(self.shares.snapshot(ALICE)["exhausted"])
        self.shares.settle(self.official())
        self.assertEqual(self.quota()["limit"], 20)

    def test_partial_bucket_is_independent_but_missing_other_bucket_blocks_admission(self):
        self.shares.configure(USERS)
        report = self.official()
        report["stale"] = True
        report["weekly"]["stale"] = True
        self.shares.settle(report)
        self.assertEqual(self.quota()["limit"], 20)
        self.assertIsNone(self.quota(ALICE, "weekly")["limit"])
        self.assertEqual(self.shares.snapshot(ALICE)["blockedReason"], WAITING)

    def test_non_member_cannot_use_another_users_share(self):
        self.enable()
        state = self.shares.snapshot(OTHER)
        self.assertTrue(state["exhausted"])
        self.assertEqual(state["blockedReason"], NOT_MEMBER)
        for bucket in ("fiveHour", "weekly"):
            self.assertEqual(state[bucket]["limit"], 0)
            self.assertEqual(state[bucket]["remaining"], 0)

    def test_disable_and_reenable_same_window_keeps_debits_and_does_not_mint_new_pool(self):
        self.enable(40, 40)
        self.store.charge(ALICE, "a1", 10)
        self.settle(50, 50)
        self.shares.configure(enabled=False)
        self.assertEqual(self.shares.config()["mode"], "tokens")
        self.store.charge(BOB, "during-token-mode", 10000)
        self.tick()
        self.shares.configure(USERS, official_snapshot=self.official(55, 55))
        self.assertEqual(self.quota()["limit"], 12)
        self.assertEqual(self.quota()["used"], 10)
        self.assertEqual(self.quota(BOB)["used"], 0)
        self.assertEqual(sum(self.quota(user)["remaining"] for user in USERS), 45)
        before = self.shares.snapshot(ALICE)
        self.shares.configure(USERS, official_snapshot=self.official(55, 55))
        self.assertEqual(self.shares.snapshot(ALICE), before)

    def test_membership_changes_require_both_old_windows_to_finish(self):
        self.enable()
        with self.assertRaisesRegex(ValueError, "周期全部结束"):
            self.shares.configure([ALICE, BOB])
        self.now = self.five
        with self.assertRaises(ValueError):
            self.shares.configure([ALICE, BOB])
        self.now = self.week
        self.five, self.week = self.now + timedelta(hours=5), self.now + timedelta(days=7)
        self.shares.configure([ALICE, BOB], official_snapshot=self.official())
        self.assertEqual(self.quota()["limit"], 50)
        self.assertEqual(self.shares.snapshot(CAROL)["blockedReason"], NOT_MEMBER)

    def test_account_change_preserves_mode_roster_and_tokens_but_discards_old_share_weights(self):
        self.enable()
        self.store.charge(ALICE, "old-account-pending", 10)
        self.settle(0, 0)
        self.shares.account_changed()
        self.assertEqual(self.shares.config()["mode"], "official_share")
        self.assertEqual(self.shares.config()["userIds"], USERS)
        self.assertEqual(self.shares.snapshot(ALICE)["blockedReason"], WAITING)
        self.assertEqual(self.store.snapshot(ALICE)["weekly"]["used"], 10)
        self.tick()
        self.shares.settle(self.official(40, 40))
        self.assertEqual(self.quota()["limit"], 12)
        self.settle(45, 45)
        self.assertEqual(self.quota()["used"], 0)
        self.assertEqual(self.quota()["remaining"], 11)

    def test_deleting_user_does_not_refund_its_current_slot_or_reuse_sqlite_rowid(self):
        self.enable()
        self.store.charge(ALICE, "first-row", 10)
        self.settle(5, 5)
        before = self.quota(BOB).copy()
        self.store.delete_user(ALICE)
        self.assertTrue(self.shares.delete_user(ALICE))
        self.assertEqual(self.quota(BOB), before)
        self.assertNotIn(ALICE, self.shares.config()["userIds"])
        self.assertEqual(self.shares.snapshot(ALICE)["blockedReason"], NOT_MEMBER)
        self.store.charge(BOB, "replacement-row", 10)
        self.settle(10, 10)
        self.assertEqual(self.quota(BOB)["used"], 5)
        self.assertEqual(self.quota(BOB)["limit"], 20)
        self.assertEqual(sum(self.quota(user)["remaining"] for user in USERS[1:]), 75)
        for table in ("share_members", "share_seen"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table} WHERE user_id=?", (ALICE,)).fetchone()[0], 0)

    def test_deleted_pending_work_retains_anonymous_slot_attribution(self):
        self.enable()
        self.store.charge(ALICE, "pending", 10)
        self.settle(0, 0)
        self.store.delete_user(ALICE)
        self.shares.delete_user(ALICE)
        self.settle(10, 10)
        self.assertEqual(self.quota(BOB)["used"], 0)
        self.assertEqual(self.quota(BOB)["remaining"], 20)

    def test_exact_integer_apportionment_and_smallest_units(self):
        self.assertEqual(split(10, {CAROL: 1, BOB: 1, ALICE: 1}), {CAROL: 3, BOB: 3, ALICE: 4})
        self.assertEqual(sum(split(FULL - 1, {user: 1 for user in USERS}).values()), FULL - 1)
        self.enable()
        for user in USERS:
            self.store.charge(user, "equal", 1)
        self.settle(0.000003, 0.000003)
        micros = [round(self.quota(user)["used"] * SCALE) for user in USERS]
        self.assertEqual(micros, [1, 1, 1, 0, 0])

    def test_failed_settlement_rolls_back_event_seen_weights_and_debits_atomically(self):
        self.enable()
        self.store.charge(ALICE, "retry-event", 10)
        self.tick()
        report = self.official(10, 10)
        connection = self.store.db
        self.store.db = Mock(wraps=connection)
        self.store.db.commit.side_effect = sqlite3.OperationalError("disk full")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.shares.settle(report)
        finally:
            self.store.db = connection
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.quota()["used"], 0)
        self.assertEqual(connection.execute("SELECT count(*) FROM share_seen WHERE event_key='retry-event'").fetchone()[0], 0)
        self.shares.settle(report)
        self.assertEqual(self.quota()["used"], 10)

    def test_invalid_configuration_cannot_modify_enabled_roster(self):
        self.enable()
        before = self.shares.config()
        for users in ([], [ALICE, ALICE], ["not-a-user"], "alice"):
            with self.subTest(users=users), self.assertRaises(ValueError):
                self.shares.configure(users)
            self.assertEqual(self.shares.config(), before)
        with self.assertRaises(ValueError):
            self.shares.configure(USERS, enabled=1)

    def test_disabled_settlement_cannot_change_token_mode_usage(self):
        self.store.set_limits(ALICE, 100, 1000)
        self.store.charge(ALICE, "token-mode", 25)
        self.shares.settle(self.official(90, 90))
        self.assertEqual(self.shares.config()["mode"], "tokens")
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["used"], 25)
        self.assertEqual(self.store.snapshot(ALICE)["fiveHour"]["limit"], 100)

    def test_explicit_idle_allocates_full_pool_without_inventing_deadline(self):
        self.shares.configure(USERS, official_snapshot=self.idle(weekly=6))
        for user in USERS:
            result = self.shares.snapshot(user)
            self.assertEqual(result["fiveHour"]["state"], "idle")
            self.assertEqual(result["fiveHour"]["idleSince"], iso(self.now))
            self.assertIsNone(result["fiveHour"]["resetAt"])
            self.assertEqual(result["fiveHour"]["limit"], 20)
            self.assertEqual(result["fiveHour"]["remaining"], 20)
            self.assertEqual(result["weekly"]["limit"], 18.8)
            self.assertFalse(result["exhausted"])
        self.assertIsNone(self.store.db.execute(
            "SELECT reset_at FROM share_windows WHERE bucket='fiveHour'").fetchone()[0])

    def test_idle_activation_debits_first_task_and_preserves_delayed_pending_weights(self):
        since = self.now
        self.shares.configure(USERS, official_snapshot=self.idle(since=since))
        self.store.charge(ALICE, "first-idle-task", 10)
        self.tick()
        self.shares.settle(self.idle(since=since))
        self.reopen()
        self.store.charge(BOB, "parallel-idle-task", 30)
        self.five = self.now + timedelta(hours=5)
        self.settle(0, 0)  # The first clock can arrive before its usage reading.
        pending = dict(self.store.db.execute(
            "SELECT user_id,pending_weight FROM share_members WHERE bucket='fiveHour'"))
        self.assertEqual((pending[ALICE], pending[BOB]), (10, 30))
        self.assertEqual(self.quota()["state"], "active")
        self.assertEqual(self.quota()["resetAt"], iso(self.five))
        self.assertEqual(self.store.db.execute(
            "SELECT cycle FROM share_windows WHERE bucket='fiveHour'").fetchone(), (1,))
        self.settle(8, 4)
        self.assertEqual((self.quota(ALICE)["used"], self.quota(BOB)["used"]), (2, 6))
        self.assertEqual(self.quota()["limit"], 20)
        self.assertEqual(sum(self.quota(user)["remaining"] for user in USERS), 92)
        self.assertEqual(self.quota(ALICE, "weekly")["used"], 1)

    def test_idle_directly_activating_with_usage_keeps_full_allocation_then_charges(self):
        self.shares.configure(USERS, official_snapshot=self.idle())
        self.store.charge(ALICE, "first-real-response", 100)
        self.settle(10, 5)
        self.assertEqual(self.quota()["limit"], 20)
        self.assertEqual(self.quota()["used"], 10)
        self.assertEqual(self.quota()["remaining"], 10)
        self.assertEqual(self.quota(BOB)["remaining"], 20)

    def test_repeated_idle_restart_and_mode_toggle_do_not_reallocate_or_replay_tokens(self):
        since = self.now
        self.shares.configure(USERS, official_snapshot=self.idle(since=since))
        self.shares.delete_user(ERIN)
        self.store.charge(ALICE, "before-toggle", 100)
        self.tick()
        self.shares.settle(self.idle(since=since))
        before = self.quota().copy()
        self.shares.configure(enabled=False)
        self.store.charge(BOB, "during-disabled", 100)
        self.reopen()
        self.tick()
        self.shares.configure(enabled=True, official_snapshot=self.idle(since=since))
        self.assertEqual(self.quota(), before)
        self.assertEqual(self.store.db.execute(
            "SELECT cycle FROM share_windows WHERE bucket='fiveHour'").fetchone(), (1,))
        self.settle(10, 10)
        self.assertEqual(self.quota(ALICE)["used"], 0)
        self.assertEqual(self.quota(BOB)["used"], 0)
        self.assertEqual(self.quota(ALICE)["limit"], 20)
        rows = self.store.db.execute(
            "SELECT allowance,used,overhead FROM share_members WHERE bucket='fiveHour'").fetchall()
        self.assertEqual(sum(max(0, a-u-o) for a, u, o in rows), 90*SCALE)
        self.assertEqual(len(rows), 5)  # The deleted seat is still reserved.

    def test_expired_active_to_idle_starts_once_and_does_not_reset_weekly(self):
        self.enable()
        self.store.charge(ALICE, "old-cycle", 100)
        self.settle(20, 20)
        self.now = self.five + timedelta(seconds=1)
        since = self.now
        self.shares.settle(self.idle(since=since, weekly=20))
        self.assertEqual(self.quota()["used"], 0)
        self.assertEqual(self.quota()["remaining"], 20)
        self.assertEqual(self.quota(ALICE, "weekly")["used"], 20)
        self.store.charge(ALICE, "new-cycle", 100)
        self.tick()
        self.shares.settle(self.idle(since=since, weekly=20))
        self.reopen()
        self.tick()
        self.shares.settle(self.idle(since=since, weekly=20))
        self.five = self.now + timedelta(hours=5)
        self.settle(5, 25)
        self.assertEqual(self.quota()["used"], 5)
        self.assertEqual(self.quota(ALICE, "weekly")["used"], 25)
        self.assertEqual(self.store.db.execute(
            "SELECT cycle FROM share_windows WHERE bucket='fiveHour'").fetchone(), (2,))

    def test_idle_cannot_refund_unexpired_window_or_replay_old_cycle_after_activation(self):
        self.enable()
        self.store.charge(ALICE, "already-used", 100)
        self.settle(10, 10)
        self.tick()
        self.shares.settle(self.idle(weekly=10))
        self.assertEqual(self.quota()["used"], 10)
        self.assertEqual(self.quota()["state"], "active")
        old_idle = self.now
        self.now = self.five + timedelta(seconds=1)
        self.shares.settle(self.idle(since=old_idle, weekly=10))
        self.assertIsNone(self.quota()["remaining"])
        self.assertEqual(self.quota()["used"], 10)

    def test_idle_rejects_missing_identity_bad_zero_weekly_idle_and_stale_observation(self):
        self.shares.configure(USERS)
        for mutation in ({"idleSince": None}, {"idleSince": iso(self.now+timedelta(seconds=1))},
                         {"usedPercent": 1}, {"usedPercent": False}, {"stale": True},
                         {"state": "unknown"}, {"resetAt": iso(self.five)}):
            report = self.idle()
            report["fiveHour"].update(mutation)
            self.shares.settle(report)
            self.assertIsNone(self.quota()["remaining"])
        report = self.idle()
        report["weekly"] = dict(report["fiveHour"])
        self.shares.account_changed()
        self.shares.settle(report)
        self.assertIsNone(self.quota(ALICE, "weekly")["remaining"])
        self.assertEqual(self.quota()["remaining"], 20)

    def test_idle_stable_identity_ignores_later_different_idle_and_older_active_report(self):
        since = self.now
        self.shares.configure(USERS, official_snapshot=self.idle(since=since))
        self.store.charge(ALICE, "pending", 100)
        self.tick(5)
        self.shares.settle(self.idle(since=since))
        self.tick()
        self.shares.settle(self.idle())  # A different identity cannot mint a pool.
        self.assertEqual(self.quota()["idleSince"], iso(since))
        report = self.official(10, 0)
        report["fiveHour"]["updatedAt"] = iso(since)
        self.shares.settle(report)
        self.assertEqual(self.quota()["state"], "idle")
        self.assertEqual(self.quota()["used"], 0)
        self.settle(10, 0)
        self.assertEqual(self.quota()["used"], 10)
        self.assertEqual(self.store.db.execute(
            "SELECT cycle FROM share_windows WHERE bucket='fiveHour'").fetchone(), (1,))

    def test_idle_membership_changes_block_even_after_weekly_expiry(self):
        self.shares.configure(USERS, official_snapshot=self.idle())
        self.now = self.week + timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "周期全部结束"):
            self.shares.configure([ALICE, BOB])
        self.shares.configure(enabled=False)
        with self.assertRaises(ValueError):
            self.shares.configure([ALICE, BOB], enabled=True)
        self.assertEqual(self.shares.config()["userIds"], USERS)

    def test_idle_deleted_pending_work_stays_attributed_to_its_anonymous_seat(self):
        since = self.now
        self.shares.configure(USERS, official_snapshot=self.idle(since=since))
        self.store.charge(ALICE, "deleted-pending", 100)
        self.tick()
        self.shares.settle(self.idle(since=since))
        self.store.delete_user(ALICE)
        self.shares.delete_user(ALICE)
        self.settle(40, 0)
        self.assertEqual(self.quota(BOB)["used"], 0)
        self.assertEqual(self.quota(BOB)["remaining"], 15)
        self.assertEqual(sum(self.quota(user)["remaining"] for user in USERS[1:]), 60)

    def test_account_change_discards_idle_identity_and_old_pending_work(self):
        since = self.now
        self.shares.configure(USERS, official_snapshot=self.idle(since=since))
        self.store.charge(ALICE, "former-account", 100)
        self.tick()
        self.shares.settle(self.idle(since=since))
        generation = self.shares.config()["accountGeneration"]
        self.shares.account_changed()
        self.assertEqual(self.shares.config()["accountGeneration"], generation+1)
        self.assertIsNone(self.quota()["remaining"])
        self.tick()
        self.shares.settle(self.idle())
        self.assertNotEqual(self.quota()["idleSince"], iso(since))
        self.settle(10, 0)
        self.assertEqual(self.quota()["used"], 0)
        self.assertEqual(self.quota()["remaining"], 18)

    def test_legacy_not_null_deadline_migration_preserves_all_existing_accounting(self):
        self.enable()
        self.store.charge(ALICE, "legacy-use", 100)
        self.settle(5, 5)
        before = self.shares.snapshot(ALICE)
        rows = self.store.db.execute("SELECT bucket,generation,cycle,reset_at,observed_at,high_water,pool,unassigned "
                                     "FROM share_windows").fetchall()
        with self.store.transaction(roll_due=False):
            self.store.db.execute("DROP TABLE share_windows")
            self.store.db.execute("CREATE TABLE share_windows (bucket TEXT PRIMARY KEY,generation INTEGER NOT NULL,"
                                  "cycle INTEGER NOT NULL,reset_at TEXT NOT NULL,observed_at TEXT NOT NULL,"
                                  "high_water INTEGER NOT NULL,pool INTEGER NOT NULL,unassigned INTEGER NOT NULL DEFAULT 0)")
            self.store.db.executemany("INSERT INTO share_windows VALUES (?,?,?,?,?,?,?,?)", rows)
        self.reopen()
        self.assertEqual(self.shares.snapshot(ALICE), before)
        columns = {row[1]: row for row in self.store.db.execute("PRAGMA table_info(share_windows)")}
        self.assertEqual(columns["reset_at"][3], 0)
        self.assertIn("window_state", columns)
        self.assertIn("idle_since", columns)
        self.now = self.five + timedelta(seconds=1)
        self.shares.settle(self.idle(weekly=5))
        self.assertEqual(self.quota()["state"], "idle")
        self.assertEqual(self.quota(ALICE, "weekly")["used"], 5)

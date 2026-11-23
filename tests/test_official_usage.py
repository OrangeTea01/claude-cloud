"""Official percentages stay distinct from local budget estimates."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock

from backend.official_usage import OfficialUsage, UsageUnavailable, fetch_usage_command


NOW = 1_800_000_000


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def event(five=.24, weekly=.13, *, reset=NOW + 10000, weekly_reset=NOW + 100000, **fields):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", **fields,
        "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": reset},
                           "seven_day": {"utilization": weekly, "resetsAt": weekly_reset}}}}


def idle_report():
    # Exact native/SDK shape from the deployment diagnostic, without account data.
    return {"rate_limits_available": True, "rate_limits": {
        "five_hour": {"utilization": 0, "resets_at": None},
        "seven_day": {"utilization": 6, "resets_at": iso(NOW + 100000)},
        "limits": [{"kind": "session", "percent": 0, "resets_at": None},
                   {"kind": "weekly_all", "percent": 6, "resets_at": iso(NOW + 100000)}]}}


class OfficialUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.usage = OfficialUsage(self.temp.name, clock=lambda: self.now)

    def tearDown(self):
        self.usage.close()
        self.temp.cleanup()

    def test_unknown_is_not_reported_as_zero_or_given_a_reset(self):
        snapshot = self.usage.snapshot()
        self.assertIsNone(snapshot["fiveHour"]["usedPercent"])
        self.assertIsNone(snapshot["weekly"]["resetAt"])
        self.assertIsNone(snapshot["updatedAt"])
        self.assertTrue(snapshot["stale"])
        self.assertFalse(self.usage.ingest({"type": "result", "usage": {"input_tokens": 50000}, "total_cost_usd": 30}))
        self.assertEqual(self.usage.snapshot(), snapshot)

    def test_authorization_invalidation_preserves_history_but_removes_freshness(self):
        self.usage.ingest(event())
        self.usage._last_refresh = self.now
        generation = self.usage._generation
        self.usage.invalidate()
        snapshot = self.usage.snapshot()
        self.assertEqual(snapshot['fiveHour']['usedPercent'], 24)
        self.assertEqual(snapshot['weekly']['usedPercent'], 13)
        self.assertTrue(snapshot['stale'])
        self.assertEqual(snapshot['errorCode'], 'usage_authorization_required')
        self.assertIsNone(snapshot['retryAt'])
        self.assertGreater(self.usage._generation, generation)

    def test_unified_windows_fraction_units_and_account_scope(self):
        self.assertTrue(self.usage.ingest(event()))
        result = self.usage.snapshot()
        self.assertEqual(result["fiveHour"]["usedPercent"], 24)
        self.assertEqual(result["weekly"]["usedPercent"], 13)
        self.assertEqual(result["fiveHour"]["status"], "allowed")
        self.assertEqual(result["weekly"]["status"], "unknown")
        self.assertEqual(result["weekly"]["resetAt"], iso(NOW + 100000))
        self.assertFalse(result["stale"])

    def test_get_usage_utilization_is_percent_even_below_one_percent(self):
        self.assertTrue(self.usage.ingest({"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": .24, "resets_at": iso(NOW + 10000)},
            "seven_day": {"utilization": 13, "resets_at": iso(NOW + 100000)}}}))
        result = self.usage.snapshot()
        self.assertEqual(result["fiveHour"]["usedPercent"], .24)
        self.assertEqual(result["weekly"]["usedPercent"], 13)

    def test_statusline_and_usage_report_rows_are_supported_without_label_guessing(self):
        self.assertTrue(self.usage.ingest({"rate_limits": {
            "five_hour": {"used_percentage": 0, "resets_at": NOW + 2000}}}))
        self.assertEqual(self.usage.snapshot()["fiveHour"]["usedPercent"], 0)
        self.now += 1
        self.assertTrue(self.usage.ingest({"type": "assistant", "usage_report": {"rate_limits": {"limits": [
            {"kind": "session", "percent": 31, "resets_at": iso(NOW + 2000), "severity": "warning"},
            {"kind": "weekly_scoped", "percent": 99, "label": "Current week", "resets_at": iso(NOW + 9000)},
            {"kind": "weekly_all", "percent": 42, "resets_at": iso(NOW + 10000)}]}}}))
        result = self.usage.snapshot()
        self.assertEqual(result["fiveHour"]["usedPercent"], 31)
        self.assertEqual(result["fiveHour"]["status"], "warning")
        self.assertEqual(result["weekly"]["usedPercent"], 42)

    def test_unknown_or_model_specific_rate_limit_type_does_not_fill_global_buckets(self):
        for kind in (None, "seven_day_opus", "seven_day_sonnet", "overage", {}, []):
            self.assertFalse(self.usage.ingest({"rate_limit_info": {
                "rateLimitType": kind, "status": "rejected", "utilization": 1, "resetsAt": NOW + 100}}))
        self.assertIsNone(self.usage.snapshot()["weekly"]["usedPercent"])
        self.assertTrue(self.usage.ingest({"rate_limit_info": {
            "rateLimitType": "five_hour", "status": "rejected", "resetsAt": NOW + 100}}))
        self.assertIsNone(self.usage.snapshot()["fiveHour"]["usedPercent"])
        self.assertEqual(self.usage.snapshot()["fiveHour"]["status"], "rejected")

    def test_expired_window_remains_historical_not_zero_or_advanced(self):
        self.usage.ingest(event(reset=NOW + 10))
        self.now += 11
        result = self.usage.snapshot()
        self.assertTrue(result["fiveHour"]["stale"])
        self.assertEqual(result["fiveHour"]["usedPercent"], 24)
        self.assertEqual(result["fiveHour"]["resetAt"], iso(NOW + 10))
        self.assertEqual(result["updatedAt"], iso(NOW))

    def test_confirmed_idle_replaces_expired_61_percent_without_inventing_deadline(self):
        self.usage.ingest(event(five=.61, weekly=.06, reset=NOW + 10))
        self.now += 11
        self.assertTrue(self.usage.ingest(idle_report()))
        snapshot = self.usage.snapshot()
        five = snapshot['fiveHour']
        self.assertEqual(five['usedPercent'], 0)
        self.assertIsNone(five['resetAt'])
        self.assertEqual(five['state'], 'idle')
        self.assertEqual(five['idleSince'], iso(self.now))
        self.assertEqual(five['updatedAt'], iso(self.now))
        self.assertFalse(snapshot['stale'])
        self.assertEqual(snapshot['weekly']['usedPercent'], 6)

    def test_repeated_idle_and_restart_keep_cycle_identity_and_freshness_deadline(self):
        self.usage.ingest(idle_report())
        self.now += 60
        self.usage.ingest(idle_report())
        self.assertEqual(self.usage.snapshot()['fiveHour']['idleSince'], iso(NOW))
        self.assertEqual(self.usage.snapshot()['fiveHour']['updatedAt'], iso(self.now))
        self.usage.close()
        self.usage = OfficialUsage(self.temp.name, clock=lambda: self.now)
        self.assertEqual(self.usage.snapshot()['fiveHour']['idleSince'], iso(NOW))
        self.now += 301
        self.assertTrue(self.usage.snapshot()['fiveHour']['stale'])
        self.usage.ingest(idle_report())
        self.assertFalse(self.usage.snapshot()['fiveHour']['stale'])
        self.assertEqual(self.usage.snapshot()['fiveHour']['idleSince'], iso(NOW))

    def test_whole_null_missing_reset_invalid_zero_and_passive_frames_do_not_confirm_idle(self):
        cases = [None, {}, {'utilization': 0}, {'utilization': None, 'resets_at': None},
                 {'utilization': '0', 'resets_at': None}, {'utilization': False, 'resets_at': None},
                 {'utilization': 0.00000001, 'resets_at': None},
                 {'utilization': 0, 'resets_at': 'invalid'},
                 {'utilization': 0, 'resets_at': None, 'resetAt': 'invalid'},
                 {'utilization': 0, 'resets_at': None, 'status': 'rejected'}]
        for value in cases:
            with self.subTest(value=value):
                self.usage.clear()
                self.usage.ingest({'rate_limits_available': True, 'rate_limits': {'five_hour': value}})
                item = self.usage.snapshot()['fiveHour']
                self.assertNotEqual(item.get('state'), 'idle')
                self.assertTrue(item['stale'])
        for available in (False, None):
            self.usage.clear()
            self.usage.ingest({'rate_limits_available': available, 'rate_limits': {
                'five_hour': {'utilization': 0, 'resets_at': None}}})
            self.assertTrue(self.usage.snapshot()['fiveHour']['stale'])
        self.usage.clear()
        self.usage.ingest({'rate_limit_info': {'rateLimitType': 'five_hour', 'utilization': 0, 'resetsAt': None}})
        self.assertTrue(self.usage.snapshot()['fiveHour']['stale'])

    def test_idle_report_cannot_erase_known_active_window_before_expiry(self):
        self.usage.ingest(event(five=.61))
        self.now += 60
        self.usage.ingest(idle_report())
        five = self.usage.snapshot()['fiveHour']
        self.assertEqual(five['usedPercent'], 61)
        self.assertEqual(five['resetAt'], iso(NOW + 10000))
        self.assertNotEqual(five.get('state'), 'idle')

    def test_idle_rejects_late_old_cycle_and_handles_first_deadline_without_freshening_zero(self):
        self.usage.ingest(idle_report())
        self.now += 10
        self.usage.ingest(event(five=.61, reset=NOW - 1))
        self.assertEqual(self.usage.snapshot()['fiveHour']['state'], 'idle')
        self.usage.ingest({'rate_limit_info': {'rateLimitType': 'five_hour', 'status': 'allowed'}})
        self.assertEqual(self.usage.snapshot()['fiveHour']['updatedAt'], iso(NOW))
        self.now += 1
        self.usage.ingest({'rate_limit_info': {
            'rateLimitType': 'five_hour', 'status': 'allowed', 'resetsAt': NOW + 20000}})
        five = self.usage.snapshot()['fiveHour']
        self.assertEqual(five['usedPercent'], 0)
        self.assertEqual(five['resetAt'], iso(NOW + 20000))
        self.assertEqual(five['updatedAt'], iso(NOW))
        self.assertNotEqual(five.get('state'), 'idle')
        self.now += 1
        self.usage.ingest(event(five=.03, reset=NOW + 20000))
        self.assertEqual(self.usage.snapshot()['fiveHour']['usedPercent'], 3)

    def test_rejection_without_deadline_overrides_idle_permission(self):
        self.usage.ingest(idle_report())
        self.now += 1
        self.usage.ingest({'rate_limit_info': {'rateLimitType': 'five_hour', 'status': 'rejected'}})
        self.assertTrue(self.usage.snapshot()['fiveHour']['stale'])
        self.assertNotEqual(self.usage.snapshot()['fiveHour'].get('state'), 'idle')

    def test_same_observation_idle_can_confirm_cache_but_cannot_freshen_expired_record(self):
        payload = {**idle_report(), 'timestamp': iso(NOW)}
        self.usage.ingest(payload)
        self.usage.invalidate()
        self.assertTrue(self.usage.ingest(payload))
        self.assertFalse(self.usage.snapshot()['fiveHour']['stale'])
        self.now += 301
        self.usage.ingest(payload)
        self.assertTrue(self.usage.snapshot()['fiveHour']['stale'])

    def test_weekly_null_deadline_is_not_implicitly_an_idle_cycle(self):
        self.usage.ingest({'rate_limits_available': True, 'rate_limits': {
            'seven_day': {'utilization': 0, 'resets_at': None}}})
        self.assertTrue(self.usage.snapshot()['weekly']['stale'])
        self.assertNotEqual(self.usage.snapshot()['weekly'].get('state'), 'idle')

    def test_stale_events_cannot_roll_back_windows_but_same_window_can_decrease(self):
        self.usage.ingest(event())
        self.now += 10
        self.usage.ingest(event(five=.1, reset=NOW + 9000))
        self.assertEqual(self.usage.snapshot()["fiveHour"]["usedPercent"], 24)
        self.now += 10
        older = event(five=.1)
        older["timestamp"] = iso(NOW - 1)
        self.assertFalse(self.usage.ingest(older))
        self.assertEqual(self.usage.snapshot()["fiveHour"]["usedPercent"], 24)
        self.assertTrue(self.usage.ingest(event(five=.1)))
        self.assertEqual(self.usage.snapshot()["fiveHour"]["usedPercent"], 10)
        self.assertEqual(self.usage.snapshot()["fiveHour"]["updatedAt"], iso(self.now))

    def test_new_window_without_percent_does_not_reuse_previous_window_percent(self):
        self.usage.ingest(event())
        self.now += 20
        self.usage.ingest(event(five=None, reset=NOW + 20000))
        result = self.usage.snapshot()["fiveHour"]
        self.assertIsNone(result["usedPercent"])
        self.assertEqual(result["resetAt"], iso(NOW + 20000))
        self.assertTrue(result["stale"])

    def test_status_only_same_window_does_not_make_old_number_fresh(self):
        self.usage.ingest(event())
        self.now += 301
        self.usage.ingest({"rate_limit_info": {"rateLimitType": "five_hour", "status": "allowed_warning", "resetsAt": NOW + 10000}})
        result = self.usage.snapshot()["fiveHour"]
        self.assertEqual(result["usedPercent"], 24)
        self.assertEqual(result["updatedAt"], iso(NOW))
        self.assertTrue(result["stale"])

    def test_missing_scope_or_malformed_numbers_never_fabricate_percent(self):
        self.assertFalse(self.usage.ingest({"rate_limits_available": False, "rate_limits": {
            "five_hour": {"utilization": 30, "resets_at": iso(NOW + 2000)}}}))
        for value in (True, "50", -1, float("nan"), float("inf"), 2, {}, 10 ** 1000):
            self.now += 1
            self.usage.ingest(event(five=value, weekly=value))
            self.assertIsNone(self.usage.snapshot()["fiveHour"]["usedPercent"])
        for value in (None, [], {"rate_limits": {"limits": [{"kind": []}]}}, {"rate_limit_info": []}):
            self.assertFalse(self.usage.ingest(value))

    def test_restart_preserves_only_normalized_data_and_actual_update_time(self):
        payload = event(secret="must-not-persist", access_token="must-not-persist")
        self.usage.ingest(payload)
        original = self.usage.snapshot()
        self.usage.close()
        self.usage = OfficialUsage(self.temp.name, clock=lambda: self.now)
        self.assertEqual(self.usage.snapshot(), original)
        self.assertNotIn(b"must-not-persist", (Path(self.temp.name) / "official_usage.sqlite3").read_bytes())

    def test_commit_failure_cannot_change_in_memory_measurement(self):
        self.usage.ingest(event())
        db = self.usage.db
        self.usage.db = Mock(wraps=db)
        self.usage.db.commit.side_effect = sqlite3.OperationalError("private database details")
        self.now += 1
        self.assertFalse(self.usage.ingest(event(five=.9)))
        snapshot = self.usage.snapshot()
        self.assertEqual(snapshot["fiveHour"]["usedPercent"], 24)
        self.assertTrue(snapshot["stale"])
        self.assertNotIn("private", snapshot["error"])
        self.usage.db = db


class OfficialUsageRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = NOW
        self.usage = OfficialUsage(self.temp.name, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.usage.close()
        self.temp.cleanup()

    async def test_concurrent_refreshes_share_one_callback_and_failures_keep_timestamp(self):
        calls = []
        async def fetch():
            calls.append(1)
            await asyncio.sleep(.01)
            return event()
        results = await asyncio.gather(self.usage.refresh(fetch), self.usage.refresh(fetch))
        self.assertEqual(len(calls), 1)
        self.assertEqual(results[0], results[1])
        self.now += 61
        async def broken():
            raise RuntimeError("secret-token-never-echo")
        failed = await self.usage.refresh(broken)
        self.assertEqual(failed["updatedAt"], iso(NOW))
        self.assertEqual(failed["fiveHour"]["usedPercent"], 24)
        self.assertTrue(failed["stale"])
        self.assertNotIn("secret", failed["error"])

    async def test_no_data_and_account_change_do_not_restore_old_meters(self):
        self.usage.ingest(event())
        self.now += 1
        async def unavailable():
            return {"rate_limits": None, "rate_limits_available": False}
        result = await self.usage.refresh(unavailable)
        self.assertTrue(result["stale"])
        self.assertEqual(result["updatedAt"], iso(NOW))
        gate = asyncio.Event()
        self.now += 61
        async def delayed():
            await gate.wait()
            return event()
        pending = asyncio.create_task(self.usage.refresh(delayed))
        await asyncio.sleep(0)
        self.usage.clear()
        gate.set()
        result = await pending
        self.assertIsNone(result["updatedAt"])
        self.assertIsNone(result["fiveHour"]["usedPercent"])

    async def test_reauthorization_discards_inflight_query_without_erasing_history(self):
        self.usage.ingest(event())
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed():
            started.set()
            await release.wait()
            return event(five=.9)
        pending = asyncio.create_task(self.usage.refresh(delayed))
        await started.wait()
        self.usage.invalidate()
        release.set()
        result = await pending
        self.assertEqual(result['fiveHour']['usedPercent'], 24)
        self.assertEqual(result['errorCode'], 'usage_authorization_required')
        self.assertTrue(result['stale'])

    async def test_older_refresh_cannot_overwrite_newer_passive_event(self):
        gate = asyncio.Event()
        async def delayed():
            await gate.wait()
            return event(five=.1)
        pending = asyncio.create_task(self.usage.refresh(delayed))
        await asyncio.sleep(0)
        self.now += 1
        self.usage.ingest(event(five=.4))
        gate.set()
        result = await pending
        self.assertEqual(result["fiveHour"]["usedPercent"], 40)
        self.assertIsNone(result["error"])

    async def test_failed_older_refresh_does_not_stale_newer_passive_measurement(self):
        gate = asyncio.Event()
        async def delayed_failure():
            await gate.wait()
            raise RuntimeError("private upstream details")
        pending = asyncio.create_task(self.usage.refresh(delayed_failure))
        await asyncio.sleep(0)
        self.now += 1
        self.usage.ingest(event(five=.5))
        gate.set()
        result = await pending
        self.assertEqual(result["fiveHour"]["usedPercent"], 50)
        self.assertFalse(result["stale"])
        self.assertIsNone(result["error"])

    async def test_live_reply_ingested_twice_with_rounded_timestamp_remains_successful(self):
        self.now = NOW + .9
        reply = {"fetchedAt": iso(NOW), "rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 61, "resets_at": iso(NOW + 10000)},
            "seven_day": {"utilization": 6, "resets_at": iso(NOW + 100000)}}}
        async def fetch():
            # ChatSession.handle receives and ingests the live control frame
            # before fetch_official returns its unwrapped response to refresh.
            envelope = {"type": "control_response", "response": {
                "subtype": "success", "request_id": "usage-1", "response": reply}}
            self.assertTrue(self.usage.ingest(envelope))
            return reply
        result = await self.usage.refresh(fetch)
        self.assertIsNone(result["errorCode"])
        self.assertIsNone(result["error"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["updatedAt"], iso(NOW))
        self.assertEqual(result["fiveHour"]["usedPercent"], 61)

    async def test_failed_fetch_preserves_successful_seconds_precision_observation_received_during_request(self):
        for offset in (0, .9):
            with self.subTest(offset=offset):
                self.usage.clear()
                self.now = NOW + offset
                payload = event()
                payload["timestamp"] = iso(NOW)
                async def fetch():
                    self.assertTrue(self.usage.ingest(payload))
                    raise RuntimeError("private-transport-failure")
                result = await self.usage.refresh(fetch)
                self.assertIsNone(result["errorCode"])
                self.assertFalse(result["stale"])
                self.assertEqual(result["updatedAt"], iso(NOW))

    async def test_valid_duplicate_clears_refresh_error_without_freshening_stale_measurement(self):
        payload = event()
        payload["timestamp"] = iso(NOW)
        self.usage.ingest(payload)
        self.now += 301
        async def broken():
            raise RuntimeError("temporary failure")
        self.assertEqual((await self.usage.refresh(broken))["errorCode"], "query_failed")
        self.now += 61
        async def duplicate():
            return payload
        result = await self.usage.refresh(duplicate)
        self.assertIsNone(result["errorCode"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["updatedAt"], iso(NOW))
        self.assertEqual(result["fiveHour"]["usedPercent"], 24)

    async def test_unknown_reply_cannot_confirm_or_make_existing_cache_fresh(self):
        self.usage.ingest(event())
        self.now += 1
        async def unknown():
            return {"unknown": "private-value", "rate_limits_available": False}
        result = await self.usage.refresh(unknown)
        self.assertEqual(result["errorCode"], "unavailable")
        self.assertTrue(result["stale"])
        self.assertEqual(result["updatedAt"], iso(NOW))
        self.assertNotIn("private", json.dumps(result))

    async def test_same_timestamp_different_or_older_reply_is_not_a_valid_confirmation(self):
        self.usage.ingest(event())
        for timestamp in (NOW, NOW - 1):
            with self.subTest(timestamp=timestamp):
                self.now += 61
                reply = event(five=.9, weekly=.9)
                reply["timestamp"] = iso(timestamp)
                async def inconsistent():
                    return reply
                result = await self.usage.refresh(inconsistent)
                self.assertEqual(result["errorCode"], "unavailable")
                self.assertEqual(result["fiveHour"]["usedPercent"], 24)
                self.assertEqual(result["updatedAt"], iso(NOW))

    async def test_safe_failure_codes_and_retry_deadline_do_not_expose_exception_text(self):
        cases = ((UsageUnavailable("private-path", code="no_running_workspace"), "no_running_workspace"),
                 (UsageUnavailable("private-path", code="query_timeout"), "query_timeout"),
                 (TimeoutError("private-timeout"), "query_timeout"),
                 (UsageUnavailable("private-token", code="private-code"), "query_failed"),
                 (RuntimeError("private-debug"), "query_failed"))
        for error, code in cases:
            with self.subTest(code=code, error=type(error).__name__):
                self.usage.clear()
                calls = []
                async def failing():
                    calls.append(True)
                    raise error
                result = await self.usage.refresh(failing)
                self.assertEqual(result["errorCode"], code)
                self.assertEqual(result["retryAt"], iso(self.now + 60))
                self.assertNotIn("private", json.dumps(result))
                self.now += 1
                await self.usage.refresh(failing)
                self.assertEqual(len(calls), 1)
                self.now += 60
                self.assertIsNone(self.usage.snapshot()["retryAt"])


class UsageCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_control_requests_are_sent_and_matching_usage_reply_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "frames.jsonl"
            script = """import json,sys
for line in sys.stdin:
    item=json.loads(line)
    with open(sys.argv[1],'a') as stream: stream.write(line)
    print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':'wrong-id','response':{'bad':True}}}),flush=True)
    result={} if item['request']['subtype']=='initialize' else {'rate_limits':{'five_hour':{'utilization':12,'resets_at':'2027-01-15T18:40:00Z'}}}
    print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':item['request_id'],'response':result}}),flush=True)
"""
            result = await fetch_usage_command([sys.executable, "-u", "-c", script, str(capture)])
            self.assertEqual(result["rate_limits"]["five_hour"]["utilization"], 12)
            frames = [json.loads(line) for line in capture.read_text().splitlines()]
            self.assertEqual([frame["type"] for frame in frames], ["control_request", "control_request"])
            self.assertEqual(frames[1]["request"], {"subtype": "get_usage", "skip_behaviors": True})
            self.assertEqual(frames[0]["request"]["subtype"], "initialize")

    async def test_protocol_errors_are_redacted_and_permissions_never_approved(self):
        for reply in ({"type": "control_request", "request_id": "approval", "request": {"subtype": "can_use_tool"}},
                      {"type": "control_response", "response": {"subtype": "error", "error": "secret-token"}}):
            script = "import json,sys; item=json.loads(input()); reply=" + repr(reply) + "; reply.get('response',{})['request_id']=item['request_id']; print(json.dumps(reply),flush=True)"
            with self.assertRaises(UsageUnavailable) as caught:
                await fetch_usage_command([sys.executable, "-u", "-c", script])
            self.assertNotIn("secret", str(caught.exception))

    async def test_nonresponsive_process_is_bounded_and_reaped(self):
        with self.assertRaises(UsageUnavailable) as caught:
            await asyncio.wait_for(fetch_usage_command([sys.executable, "-u", "-c", "import time; time.sleep(60)"], timeout=.05), 4)
        self.assertEqual(caught.exception.code, "query_timeout")

    async def test_oversized_output_is_rejected_without_reflecting_it(self):
        script = "import sys; input(); sys.stdout.write('private-output' * 100000 + '\\n'); sys.stdout.flush()"
        with self.assertRaises(UsageUnavailable) as caught:
            await asyncio.wait_for(fetch_usage_command([sys.executable, "-u", "-c", script]), 4)
        self.assertNotIn("private-output", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

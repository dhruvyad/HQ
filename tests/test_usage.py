"""Subscription usage: only what a tool actually said, and null otherwise.

Nothing here needs codex, claude or a socket, and nothing may touch a real one, so every
test that could reach a real source has that route patched to fail loudly.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from hq import cli, connections, usage
from hq import server as server_module
from hq.server import Server
from hq.store import Store
from hq.stream import StateFeed

# What Codex 0.149.1 really returned, with its identifiers replaced.
CODEX_RESPONSE = {
    "rateLimits": {"limitId": "codex", "limitName": None,
                   "primary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1794382420},
                   "secondary": None, "credits": {"hasCredits": True, "unlimited": False, "balance": "123.45"},
                   "individualLimit": None, "spendControlReached": False, "planType": "pro", "rateLimitReachedType": None},
    "rateLimitsByLimitId": {"codex": {"limitId": "codex", "limitName": None,
                                      "primary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1794382420},
                                      "secondary": None, "credits": {"hasCredits": True, "unlimited": False, "balance": "123.45"},
                                      "individualLimit": None, "spendControlReached": False, "planType": "pro",
                                      "rateLimitReachedType": None}},
    "rateLimitResetCredits": {"availableCount": 1, "credits": [{"id": "RateLimitResetCredit_x", "status": "available"}]},
}
# Claude Code's documented status line payload, trimmed.
CLAUDE_PAYLOAD = {
    "session_id": "secret-session", "transcript_path": "/secret/transcript.jsonl", "cwd": "/secret/project",
    "model": {"display_name": "Opus"}, "workspace": {"current_dir": "/secret/project"},
    "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                    "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
                    "spend_limit": {"used_percentage": 62.8, "resets_at": 1740787200, "used_usd": 88.5}},
}
FAR_FUTURE = 4102444800
NOW = 1_800_000_000


def must_not_run(*args, **kwargs):
    raise AssertionError("A real usage source was touched")


class ConversionTests(unittest.TestCase):
    def test_used_becomes_whole_percent_left_rounded_down(self):
        for used, left in [(0, 100), (2, 98), (23.5, 76), (41.2, 58), (99.9, 0), (100, 0), (140, 0)]:
            self.assertEqual(usage.left_from_used(used), left, used)

    def test_anything_that_is_not_a_percentage_is_rejected(self):
        for used in [None, True, False, "5", -1, float("nan"), float("inf"), [], {}]:
            self.assertIsNone(usage.left_from_used(used), used)

    def test_window_names_come_from_their_length(self):
        for minutes, name in [(10080, "weekly"), (300, "5-hour"), (1440, "daily"), (90, "90-minute"), (None, "current"), (True, "current"), (0, "current")]:
            self.assertEqual(usage.codex_window_name(minutes), name, minutes)


class CodexParsingTests(unittest.TestCase):
    def test_the_real_response(self):
        reading = usage.parse_codex(CODEX_RESPONSE)
        self.assertEqual(reading["windows"], [{"name": "weekly", "left": 98, "resets_at": 1794382420}])
        self.assertLessEqual(abs(reading["as_of"] - time.time()), 5)

    def test_only_the_windows_leave_the_response(self):
        text = json.dumps(usage.parse_codex(CODEX_RESPONSE))
        for private in ("planType", "pro", "credits", "balance", "123.45", "RateLimitResetCredit_x", "limitId"):
            self.assertNotIn(private, text)

    def test_both_windows(self):
        reading = usage.parse_codex({"rateLimits": {"primary": {"usedPercent": 38, "windowDurationMins": 300, "resetsAt": None},
                                                    "secondary": {"usedPercent": 19, "windowDurationMins": 10080, "resetsAt": 5}}})
        self.assertEqual(reading["windows"], [{"name": "5-hour", "left": 62, "resets_at": None},
                                              {"name": "weekly", "left": 81, "resets_at": 5}])

    def test_the_codex_bucket_wins_over_the_older_single_view(self):
        reading = usage.parse_codex({"rateLimits": {"primary": {"usedPercent": 90}},
                                     "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 10}}}})
        self.assertEqual(reading["windows"][0]["left"], 90)

    def test_malformed_means_unavailable(self):
        for bad in [None, [], "x", {}, {"rateLimits": None}, {"rateLimits": "x"}, {"rateLimits": {}},
                    {"rateLimits": {"primary": None, "secondary": None}}, {"rateLimits": {"primary": {"usedPercent": "2"}}},
                    {"rateLimits": {"primary": {}}}, {"rateLimitsByLimitId": {"codex": 4}}]:
            self.assertIsNone(usage.parse_codex(bad), bad)


class ClaudeParsingTests(unittest.TestCase):
    def test_the_documented_payload(self):
        self.assertEqual(usage.parse_claude(CLAUDE_PAYLOAD)["windows"],
                         [{"name": "5-hour", "left": 76, "resets_at": 1738425600}, {"name": "weekly", "left": 58, "resets_at": 1738857600}])

    def test_each_window_may_be_absent(self):
        reading = usage.parse_claude({"rate_limits": {"seven_day": {"used_percentage": 10, "resets_at": 9}}})
        self.assertEqual(reading["windows"], [{"name": "weekly", "left": 90, "resets_at": 9}])

    def test_no_rate_limits_means_unavailable(self):
        for bad in [None, [], {}, {"rate_limits": None}, {"rate_limits": {}}, {"rate_limits": {"five_hour": "x"}},
                    {"rate_limits": {"five_hour": {"used_percentage": None}}}, {"rate_limits": {"spend_limit": {"used_percentage": 5}}}]:
            self.assertIsNone(usage.parse_claude(bad), bad)


class TempHome(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = patch.dict(os.environ, {"HQ_HOME": str(self.root / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)


class StatusLineTests(TempHome):
    def capture(self, text):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            usage.statusline(io.StringIO(text))
        self.assertEqual(out.getvalue(), "")

    def test_it_records_only_rate_limits_and_prints_nothing(self):
        self.capture(json.dumps(CLAUDE_PAYLOAD))
        text = usage.claude_path().read_text()
        for private in ("secret", "transcript", "Opus", "spend", "88.5", "session", "cwd"):
            self.assertNotIn(private, text)
        value = json.loads(text)
        self.assertEqual(set(value), {"windows", "as_of"})
        self.assertEqual([item["left"] for item in value["windows"]], [76, 58])

    def test_the_file_is_private(self):
        self.capture(json.dumps(CLAUDE_PAYLOAD))
        self.assertEqual(stat.S_IMODE(usage.claude_path().stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in usage.claude_path().parent.iterdir()), ["claude-usage.json"])

    def test_malformed_input_writes_nothing(self):
        for text in ["", "not json", "[]", "null", '{"rate_limits": 4}', '{"rate_limits": {"five_hour": {"used_percentage": "x"}}}']:
            self.capture(text)
            self.assertFalse(usage.claude_path().exists(), text)

    def test_a_payload_without_limits_leaves_the_last_reading(self):
        self.capture(json.dumps(CLAUDE_PAYLOAD))
        before = usage.claude_path().read_text()
        self.capture(json.dumps({"session_id": "a session that has not had a response yet"}))
        self.assertEqual(usage.claude_path().read_text(), before)

    def test_a_failure_never_reaches_the_terminal(self):
        broken = Mock()
        broken.read.side_effect = OSError("gone")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            usage.statusline(broken)
        self.assertEqual(out.getvalue(), "")

    def test_an_unwritable_home_never_reaches_the_terminal(self):
        with patch("hq.usage.tempfile.NamedTemporaryFile", side_effect=OSError("disk full")):
            self.capture(json.dumps(CLAUDE_PAYLOAD))
        self.assertFalse(usage.claude_path().exists())

    def test_the_command(self):
        out = io.StringIO()
        with patch("sys.argv", ["hq", "statusline"]), patch("sys.stdin", io.StringIO(json.dumps(CLAUDE_PAYLOAD))), contextlib.redirect_stdout(out):
            cli.main()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(usage.read_claude()["windows"][0]["left"], 76)


class ReadingFileTests(TempHome):
    def write(self, value):
        usage.claude_path().parent.mkdir(parents=True, exist_ok=True)
        usage.claude_path().write_text(value if isinstance(value, str) else json.dumps(value))

    def test_absent_and_damaged_mean_unavailable(self):
        self.assertIsNone(usage.read_claude())
        good = {"name": "weekly", "left": 5, "resets_at": None}
        for bad in ["not json", "null", [], {}, {"windows": [], "as_of": NOW}, {"windows": [good]}, {"windows": [good], "as_of": "soon"},
                    {"windows": [good], "as_of": True}, {"windows": "x", "as_of": NOW},
                    {"windows": [{**good, "left": 900}], "as_of": NOW}, {"windows": [{**good, "left": -1}], "as_of": NOW},
                    {"windows": [{**good, "left": True}], "as_of": NOW}, {"windows": [{**good, "left": 5.5}], "as_of": NOW},
                    {"windows": [{**good, "name": ""}], "as_of": NOW}, {"windows": [{**good, "name": 4}], "as_of": NOW},
                    {"windows": [{**good, "resets_at": "x"}], "as_of": NOW}, {"windows": ["x"], "as_of": NOW}]:
            self.write(bad)
            self.assertIsNone(usage.read_claude(), bad)

    def test_a_good_file_loses_anything_extra(self):
        self.write({"windows": [{"name": "weekly", "left": 5, "resets_at": 7, "extra": "x"}], "as_of": NOW, "other": "y"})
        self.assertEqual(usage.read_claude(), {"windows": [{"name": "weekly", "left": 5, "resets_at": 7}], "as_of": NOW})


class FreshnessTests(unittest.TestCase):
    def reading(self, *resets, as_of=NOW):
        return {"windows": [{"name": str(n), "left": 50, "resets_at": r} for n, r in enumerate(resets)], "as_of": as_of}

    def test_a_window_that_has_reset_is_dropped_not_guessed(self):
        kept = usage.fresh(self.reading(NOW + 100, NOW - 1, None), NOW)
        self.assertEqual([item["name"] for item in kept["windows"]], ["0", "2"])

    def test_a_window_resetting_right_now_is_dropped(self):
        self.assertIsNone(usage.fresh(self.reading(NOW), NOW))

    def test_all_windows_reset_means_unavailable(self):
        self.assertIsNone(usage.fresh(self.reading(NOW - 5, NOW - 1), NOW))

    def test_a_day_old_reading_is_unavailable(self):
        self.assertIsNotNone(usage.fresh(self.reading(None, as_of=NOW - usage.MAX_AGE), NOW))
        self.assertIsNone(usage.fresh(self.reading(None, as_of=NOW - usage.MAX_AGE - 1), NOW))

    def test_a_reading_from_the_future_is_unavailable(self):
        self.assertIsNotNone(usage.fresh(self.reading(None, as_of=NOW + 30), NOW))
        self.assertIsNone(usage.fresh(self.reading(None, as_of=NOW + 3600), NOW))

    def test_nothing_stays_nothing(self):
        self.assertIsNone(usage.fresh(None, NOW))

    def test_as_of_is_never_changed(self):
        self.assertEqual(usage.fresh(self.reading(None, as_of=NOW - 900), NOW)["as_of"], NOW - 900)


class CodexReadTests(unittest.TestCase):
    """A stand-in for `codex app-server`, so the real subprocess path runs anywhere."""

    def fake(self, behaviour):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "codex"
        path.write_text(f"#!{sys.executable}\nimport json, sys, time\nassert sys.argv[1:] == ['app-server']\n"
                        "def reply(value):\n    print(json.dumps(value), flush=True)\n" + behaviour)
        path.chmod(0o755)
        return str(path)

    SERVER = ("for line in sys.stdin:\n    message = json.loads(line)\n"
              "    if message.get('method') == 'initialize':\n        reply({'id': message['id'], 'result': {'userAgent': 'x'}})\n"
              "    elif message.get('method') == 'account/rateLimits/read':\n        print('not json', flush=True)\n"
              "        reply({'method': 'account/rateLimits/updated', 'params': {}})\n"
              "        reply({'id': message['id'], 'result': " + repr(CODEX_RESPONSE) + "})\n")

    def test_it_speaks_the_protocol_and_skips_what_is_not_its_answer(self):
        self.assertEqual(usage.read_over_stdio(self.fake(self.SERVER)), CODEX_RESPONSE)

    def test_the_server_it_started_is_gone_afterwards(self):
        real, processes = subprocess.Popen, []

        def spawn(*args, **kwargs):
            processes.append(real(*args, **kwargs))
            return processes[-1]
        with patch("hq.usage.subprocess.Popen", spawn):
            usage.read_over_stdio(self.fake(self.SERVER))
        self.assertIsNotNone(processes[0].poll())

    def test_a_refusal_is_an_error(self):
        binary = self.fake("for line in sys.stdin:\n    message = json.loads(line)\n    if 'id' in message:\n"
                           "        reply({'id': message['id'], 'error': {'message': 'not logged in'}})\n")
        with self.assertRaises(RuntimeError):
            usage.read_over_stdio(binary)

    def test_a_server_that_exits_is_an_error(self):
        with self.assertRaises((EOFError, OSError)):
            usage.read_over_stdio(self.fake("sys.exit(0)\n"))

    def test_a_hung_server_is_killed_on_time(self):
        began = time.monotonic()
        with self.assertRaises((EOFError, OSError)):
            usage.read_over_stdio(self.fake("time.sleep(60)\n"), timeout=0.5)
        self.assertLess(time.monotonic() - began, 10)

    def test_a_running_server_is_asked_first(self):
        with patch("hq.usage.read_over_socket", return_value=CODEX_RESPONSE), patch("hq.usage.read_over_stdio", must_not_run):
            self.assertEqual(usage.read_codex()["windows"][0]["left"], 98)

    def test_without_one_it_starts_its_own(self):
        with patch("hq.usage.read_over_socket", side_effect=ConnectionRefusedError()), patch("hq.usage.codex_binary", return_value="/x/codex"), \
                patch("hq.usage.read_over_stdio", return_value=CODEX_RESPONSE) as stdio:
            self.assertEqual(usage.read_codex()["windows"][0]["left"], 98)
        stdio.assert_called_once_with("/x/codex")

    def test_no_codex_means_unavailable(self):
        with patch("hq.usage.read_over_socket", side_effect=FileNotFoundError()), patch("hq.usage.codex_binary", return_value=None), \
                patch("hq.usage.read_over_stdio", must_not_run):
            self.assertIsNone(usage.read_codex())

    def test_every_failure_means_unavailable(self):
        for error in (OSError("x"), RuntimeError("not logged in"), ValueError("x"), EOFError(), subprocess.TimeoutExpired("codex", 1)):
            with patch("hq.usage.read_over_socket", side_effect=OSError()), patch("hq.usage.codex_binary", return_value="/x/codex"), \
                    patch("hq.usage.read_over_stdio", side_effect=error):
                self.assertIsNone(usage.read_codex(), error)

    def test_a_missing_executable_means_unavailable(self):
        with patch("hq.usage.read_over_socket", side_effect=OSError()), patch("hq.usage.codex_binary", return_value="/nonexistent/codex"):
            self.assertIsNone(usage.read_codex())

    def test_the_binary_search_finds_nothing_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as temp, patch("hq.usage.shutil.which", return_value=None), patch.object(Path, "home", return_value=Path(temp)):
            self.assertIsNone(usage.codex_binary())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.usage = usage.Usage()
        self.now = 1000.0
        self.claude = Mock(return_value=None)
        self.codex = Mock(return_value={"windows": [{"name": "weekly", "left": 40, "resets_at": None}], "as_of": int(time.time())})
        self.worker = usage.UsageWorker(self.usage, self.claude, self.codex, clock=lambda: self.now)

    def test_nothing_starts_codex_while_nobody_is_looking(self):
        self.worker.step()
        self.codex.assert_not_called()
        self.assertIsNone(self.usage.current()["codex"])

    def test_interest_fades(self):
        self.usage.watched()
        self.usage.last_watched = time.monotonic() - usage.WATCHED_FOR - 1
        self.worker.step()
        self.codex.assert_not_called()

    def test_codex_is_read_at_most_once_a_minute(self):
        self.usage.watched()
        self.worker.step()
        self.now += 59
        self.worker.step()
        self.assertEqual(self.codex.call_count, 1)
        self.now += 1
        self.worker.step()
        self.assertEqual(self.codex.call_count, 2)
        self.assertEqual(self.usage.current()["codex"]["windows"][0]["left"], 40)

    def test_a_failed_read_keeps_the_last_number_and_backs_off(self):
        self.usage.watched()
        self.worker.step()
        self.now += usage.CODEX_POLL
        self.codex.return_value = None
        self.worker.step()
        self.assertEqual(self.usage.current()["codex"]["windows"][0]["left"], 40)
        self.now += usage.CODEX_POLL
        self.worker.step()
        self.assertEqual(self.codex.call_count, 2)
        self.now += usage.CODEX_RETRY - usage.CODEX_POLL
        self.worker.step()
        self.assertEqual(self.codex.call_count, 3)

    def test_claude_follows_its_file(self):
        reading = {"windows": [{"name": "5-hour", "left": 7, "resets_at": None}], "as_of": int(time.time())}
        self.claude.return_value = reading
        self.worker.step()
        self.assertEqual(self.usage.current()["claude"], reading)
        self.claude.return_value = None
        self.worker.step()
        self.assertIsNone(self.usage.current()["claude"])

    def test_a_stopped_worker_reads_nothing(self):
        self.usage.watched()
        self.worker.stop.set()
        self.worker.run()
        self.claude.assert_not_called()
        self.codex.assert_not_called()

    def test_an_error_does_not_end_the_worker(self):
        self.claude.side_effect = [OSError("x")]
        self.worker.stop = Mock()
        self.worker.stop.wait.side_effect = [False, True]
        self.worker.run()
        self.claude.assert_called_once()


class FeedTests(TempHome):
    def setUp(self):
        super().setUp()
        self.usage = usage.Usage()
        self.feed = StateFeed(Store(self.root / "hq.sqlite3"), self.usage)
        self.addCleanup(self.feed.close)

    def state(self):
        return json.loads(self.feed.current())

    def numbers(self, left, as_of=None):
        return {"windows": [{"name": "weekly", "left": left, "resets_at": None}], "as_of": as_of or int(time.time())}

    def test_unavailable_is_null_in_the_payload(self):
        self.assertEqual(self.state()["usage"], {"claude": None, "codex": None})

    def test_a_feed_with_no_source_still_says_null(self):
        feed = StateFeed(Store(self.root / "other.sqlite3"))
        self.addCleanup(feed.close)
        self.assertEqual(json.loads(feed.current())["usage"], {"claude": None, "codex": None})

    def test_the_payload_matches_the_contract(self):
        self.usage.record("codex", self.numbers(81, 1800000000 - 5))
        with patch("hq.usage.time.time", return_value=1800000000):
            self.assertEqual(self.state()["usage"], {
                "claude": None,
                "codex": {"windows": [{"name": "weekly", "left": 81, "resets_at": None}], "as_of": 1800000000 - 5}})

    def test_a_new_number_is_a_new_revision(self):
        first = self.state()["revision"]
        self.usage.record("codex", self.numbers(80))
        second = self.state()["revision"]
        self.assertGreater(second, first)
        self.usage.record("codex", self.numbers(79))
        self.assertGreater(self.state()["revision"], second)

    def test_a_newer_reading_of_the_same_number_is_not_a_new_revision(self):
        now = int(time.time())
        self.usage.record("codex", self.numbers(80, now - 100))
        first = self.state()
        self.usage.record("codex", self.numbers(80, now - 10))
        second = self.state()
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(second["usage"]["codex"]["as_of"], now - 10)  # Still honest for anyone reading.

    def test_going_unavailable_is_a_new_revision(self):
        self.usage.record("codex", self.numbers(80))
        first = self.state()["revision"]
        self.usage.record("codex", None)
        self.assertGreater(self.state()["revision"], first)

    def test_a_window_reaching_its_reset_is_a_new_revision(self):
        self.usage.record("codex", {"windows": [{"name": "weekly", "left": 80, "resets_at": FAR_FUTURE}], "as_of": int(time.time())})
        first = self.state()
        self.assertIsNotNone(first["usage"]["codex"])
        with patch("hq.usage.time.time", return_value=FAR_FUTURE + 1):
            self.usage.readings["codex"]["as_of"] = FAR_FUTURE
            second = self.state()
        self.assertIsNone(second["usage"]["codex"])
        self.assertGreater(second["revision"], first["revision"])

    def test_publishing_reads_nothing_real(self):
        with patch("hq.usage.read_claude", must_not_run), patch("hq.usage.read_codex", must_not_run), patch("hq.usage.subprocess.Popen", must_not_run):
            for _ in range(5):
                self.feed.publish()

    def test_only_a_real_reader_counts_as_watching(self):
        self.feed.publish()
        self.assertFalse(self.usage.is_watched())
        self.feed.current()
        self.assertTrue(self.usage.is_watched())
        self.usage.last_watched = float("-inf")
        self.feed.wait(self.feed.version, timeout=0)
        self.assertTrue(self.usage.is_watched())


class LabTests(TempHome):
    def serve(self, lab):
        environment = {key: value for key, value in os.environ.items() if key != "HQ_FLOAT_LAB"}
        if lab:
            environment["HQ_FLOAT_LAB"] = "1"
        with patch.dict(os.environ, environment, clear=True), patch("hq.server.Dispatcher"), patch("hq.server.ClaudePresence"), \
                patch("hq.server.UsageWorker") as worker, patch.object(Server, "serve_forever"), contextlib.redirect_stdout(io.StringIO()):
            # Off the main thread, so serve() leaves this process's signal handlers alone.
            thread = threading.Thread(target=server_module.serve, args=(0,))
            thread.start()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        return worker.call_count

    def test_the_lab_starts_no_usage_worker(self):
        self.assertEqual(self.serve(lab=True), 0)

    def test_the_real_server_starts_one(self):
        self.assertEqual(self.serve(lab=False), 1)

    def test_the_lab_shows_fixed_numbers_and_touches_nothing_real(self):
        with patch.dict(os.environ, {"HQ_FLOAT_LAB": "1"}), patch("hq.usage.read_claude", must_not_run), patch("hq.usage.read_codex", must_not_run), \
                patch("hq.usage.read_over_socket", must_not_run), patch("hq.usage.read_over_stdio", must_not_run), patch("hq.usage.subprocess.Popen", must_not_run), \
                patch("hq.usage.CodexClient", must_not_run):
            server = Server(0, Store(self.root / "lab.sqlite3"))
            self.addCleanup(server.server_close)
            state = json.loads(server.feed.current())
        self.assertEqual(min(item["left"] for item in state["usage"]["claude"]["windows"]), 62)
        self.assertEqual(min(item["left"] for item in state["usage"]["codex"]["windows"]), 18)

    def test_a_real_server_starts_with_nothing_until_a_tool_has_said(self):
        with patch.dict(os.environ, {key: value for key, value in os.environ.items() if key != "HQ_FLOAT_LAB"}, clear=True):
            server = Server(0, Store(self.root / "real.sqlite3"))
            self.addCleanup(server.server_close)
            self.assertEqual(json.loads(server.feed.current())["usage"], {"claude": None, "codex": None})


class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = self.home / ".claude/settings.json"

    def write(self, value):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(value if isinstance(value, str) else json.dumps(value))

    def backups(self):
        return list(self.settings.parent.glob("settings.json.hq-backup-*")) if self.settings.parent.exists() else []

    def test_it_sets_the_status_line_when_there_is_none(self):
        result = connections.install_statusline()
        self.assertEqual(result["status_line"], "configured")
        line = json.loads(self.settings.read_text())["statusLine"]
        self.assertEqual(line["type"], "command")
        self.assertTrue(line["command"].endswith(" statusline"))
        self.assertEqual(connections.statusline_state(), "hq")
        self.assertEqual(self.backups(), [])

    def test_it_says_what_it_costs(self):
        self.assertIn("footer hints", connections.install_statusline()["note"])

    def test_other_settings_survive_and_a_backup_is_kept(self):
        self.write({"hooks": {"Stop": []}, "model": "opus"})
        connections.install_statusline()
        after = json.loads(self.settings.read_text())
        self.assertEqual(after["hooks"], {"Stop": []})
        self.assertEqual(after["model"], "opus")
        self.assertEqual([json.loads(path.read_text()) for path in self.backups()], [{"hooks": {"Stop": []}, "model": "opus"}])

    def test_an_existing_status_line_is_left_exactly_as_it_is(self):
        original = '{\n  "statusLine":   {"type": "command", "command": "~/my-line.sh"},\n  "model": "opus"\n}\n'
        self.write(original)
        result = connections.install_statusline()
        self.assertEqual(result["status_line"], "kept yours")
        self.assertIn("statusline", result["next_step"])
        self.assertEqual(self.settings.read_text(), original)
        self.assertEqual(self.backups(), [])
        self.assertEqual(connections.statusline_state(), "other")

    def test_installing_twice_changes_nothing(self):
        connections.install_statusline()
        before = self.settings.read_text()
        self.assertEqual(connections.install_statusline()["status_line"], "already configured")
        self.assertEqual(self.settings.read_text(), before)
        self.assertEqual(self.backups(), [])

    def test_a_broken_settings_file_is_refused_and_untouched(self):
        for broken in ["{not json", "[]"]:
            self.write(broken)
            with self.assertRaises(ValueError):
                connections.install_statusline()
            self.assertEqual(self.settings.read_text(), broken)
        self.assertEqual(self.backups(), [])

    def test_uninstall_removes_only_ours(self):
        self.write({"model": "opus"})
        connections.install_statusline()
        self.assertEqual(connections.uninstall_statusline()["status_line"], "removed")
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "opus"})
        self.assertEqual(connections.statusline_state(), "none")
        self.assertEqual(connections.uninstall_statusline()["status_line"], "not configured")

    def test_uninstall_leaves_someone_elses(self):
        original = '{"statusLine": {"type": "command", "command": "~/my-line.sh"}}'
        self.write(original)
        self.assertEqual(connections.uninstall_statusline()["status_line"], "kept yours")
        self.assertEqual(self.settings.read_text(), original)

    def test_a_non_command_status_line_counts_as_someone_elses(self):
        self.write({"statusLine": "x"})
        self.assertEqual(connections.statusline_state(), "other")


class CommandTests(TempHome):
    def run_usage(self, *arguments):
        out = io.StringIO()
        with patch("sys.argv", ["hq", "usage", *arguments]), contextlib.redirect_stdout(out):
            cli.main()
        return json.loads(out.getvalue())

    def test_status_reports_what_is_known(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "home", return_value=Path(temp)), \
                patch("hq.usage.read_codex", return_value=None):
            self.assertEqual(self.run_usage(), {"claude": None, "codex": None, "claude_login": "off", "claude_status_line": "none"})

    def test_install_goes_through_the_command(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "home", return_value=Path(temp)):
            self.assertEqual(self.run_usage("install")["status_line"], "configured")
            self.assertEqual(self.run_usage("uninstall")["status_line"], "removed")


class ClaudeLoginTests(unittest.TestCase):
    """The opt-in route: Claude Code's own login, read only when turned on, never kept or refreshed."""
    PAYLOAD = {"five_hour": None, "seven_day": {"utilization": 20.0, "resets_at": "2026-11-09T22:00:00.148647+00:00"},
               "limits": [{"kind": "weekly_all", "percent": 20}], "extra_usage": {"is_enabled": False}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {"HQ_HOME": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls, self.requests, self.answers = [], [], []
        self.clock = 1_000_000.0

    def keychain(self, token="secret-token", expires_in=3600, code=0):
        def run(command, **kwargs):
            self.calls.append(command)
            body = json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "never-used",
                                                 "expiresAt": (self.clock + expires_in) * 1000}})
            return subprocess.CompletedProcess(command, code, stdout=body if code == 0 else "", stderr="")
        return run

    def url(self, request, timeout):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else self.PAYLOAD
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "no", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps(answer).encode())

    def login(self, enabled=True, **keychain):
        return usage.ClaudeLogin(enabled=lambda: enabled, run=self.keychain(**keychain), open_url=self.url, now=lambda: self.clock)

    def test_parses_the_usage_windows(self):
        value = usage.parse_claude_login_usage(self.PAYLOAD)
        self.assertEqual(value["windows"], [{"name": "weekly", "left": 80, "resets_at": 1794261600}])
        both = usage.parse_claude_login_usage({"five_hour": {"utilization": 37.6, "resets_at": "2026-11-06T04:00:00Z"},
                                               "seven_day": {"utilization": 20, "resets_at": None}})
        self.assertEqual([w["left"] for w in both["windows"]], [62, 80])  # rounded down, never overstated
        self.assertIsNone(usage.parse_claude_login_usage({"five_hour": None, "seven_day": None}))
        self.assertIsNone(usage.parse_claude_login_usage({"seven_day": {"utilization": "lots"}}))
        self.assertIsNone(usage.parse_claude_login_usage([]))

    def test_off_reads_nothing(self):
        login = self.login(enabled=False)
        self.assertIsNone(login.read())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.requests, [])

    def test_reads_once_and_keeps_the_token_in_memory_only(self):
        login = self.login()
        self.assertEqual(login.read()["windows"][0]["left"], 80)
        self.assertEqual(login.read()["windows"][0]["left"], 80)
        self.assertEqual(len(self.calls), 1)  # the Keychain is asked once, not every poll
        self.assertEqual(self.calls[0], ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"])
        request = self.requests[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        # Nothing on disk ever holds it.
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn("secret-token", path.read_text(errors="ignore"))

    def test_an_expired_login_waits_for_claude_code_to_renew_it(self):
        login = self.login(expires_in=-5)
        self.assertIsNone(login.read())
        self.assertEqual(self.requests, [])  # no request with a dead token, and no refresh attempt
        self.assertTrue(all("refresh" not in " ".join(call) for call in self.calls))

    def test_a_rejected_token_is_read_again_once(self):
        login = self.login()
        self.answers = [401, self.PAYLOAD]
        self.assertEqual(login.read()["windows"][0]["left"], 80)
        self.assertEqual(len(self.calls), 2)
        self.answers = [401, 401]
        self.assertIsNone(login.read())

    def test_failures_say_nothing_rather_than_guess(self):
        self.assertIsNone(self.login(code=44).read())  # no login in the Keychain, or the user said no
        login = self.login()
        self.answers = [500]
        self.assertIsNone(login.read())
        self.answers = [{"unexpected": True}]
        self.assertIsNone(login.read())

    def test_the_setting_is_private_and_off_by_default(self):
        self.assertFalse(usage.claude_login_enabled())
        self.assertEqual(usage.set_claude_login(True)["claude_login"], "on")
        self.assertTrue(usage.claude_login_enabled())
        self.assertEqual(stat.S_IMODE(usage.login_path().stat().st_mode), 0o600)
        usage.set_claude_login(False)
        self.assertFalse(usage.claude_login_enabled())

    def test_the_worker_prefers_the_login_and_keeps_the_status_line_as_fallback(self):
        recorded = usage.Usage()
        recorded.watched()
        login = Mock()
        login.enabled.return_value = True
        login.read.return_value = {"windows": [{"name": "weekly", "left": 80, "resets_at": None}], "as_of": int(time.time())}
        line = {"windows": [{"name": "5-hour", "left": 40, "resets_at": None}], "as_of": int(time.time())}
        worker = usage.UsageWorker(recorded, claude=lambda: line, codex=lambda: None, login=login, clock=lambda: self.clock)
        worker.step()
        self.assertEqual(recorded.current()["claude"]["windows"][0]["left"], 80)
        login.read.return_value = None
        self.clock += usage.CLAUDE_POLL + 1
        worker.step()
        self.assertEqual(recorded.current()["claude"]["windows"][0]["left"], 80)  # a failed read keeps the last number
        login.enabled.return_value = False
        self.clock += usage.CLAUDE_RETRY + 1
        worker.step()
        self.assertEqual(recorded.current()["claude"]["windows"][0]["left"], 40)  # off: back to the status line


if __name__ == "__main__":
    unittest.main()

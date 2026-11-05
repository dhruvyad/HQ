"""The floating panel's lifecycle: build, lock, launchd and the throwaway lab.

No test needs macOS or swiftc. The lock and SIGTERM paths use a real child process,
because a mocked one would not show that the lock outlives the launching command.
"""
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch

from hq import cli, floating
from hq import server as server_module
from hq.server import Server
from hq.store import Store

ROOT = Path(__file__).resolve().parents[1]
REAL_POPEN = subprocess.Popen  # Tests patch subprocess.Popen and still need real children.


class FloatingCase(unittest.TestCase):
    platform = "darwin"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        for patcher in (patch.dict(os.environ, {"HQ_HOME": str(self.root / "data")}),
                        patch.object(Path, "home", return_value=self.home),
                        patch("sys.platform", self.platform)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ensure = Mock(return_value={"port": 4310})


class PlatformTests(FloatingCase):
    platform = "linux"

    def test_every_command_that_needs_a_window_refuses_before_doing_anything(self):
        for action in (lambda: floating.start(4310, self.ensure), lambda: floating.run(4310, self.ensure),
                       lambda: floating.install(4310), floating.uninstall, floating.lab, floating.build):
            with self.assertRaisesRegex(ValueError, "needs macOS"):
                action()
        self.ensure.assert_not_called()

    def test_status_and_stop_still_answer(self):
        self.assertEqual(floating.status(), {"running": False, "pid": None, "dock": "top-left", "installed_at_login": False})
        self.assertEqual(floating.stop(), {"status": "not running"})


class BuildTests(FloatingCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "FloatPanel.swift"
        self.source.write_text("// one")
        which = patch("hq.floating.shutil.which", return_value="/usr/bin/swiftc")
        self.which = which.start()
        self.addCleanup(which.stop)

    @staticmethod
    def compiled(command, **_):
        Path(command[command.index("-o") + 1]).write_text("binary")
        return Mock(returncode=0, stderr="")

    def test_missing_swiftc_names_the_fix(self):
        self.which.return_value = None
        with self.assertRaisesRegex(ValueError, r"xcode-select --install"):
            floating.build(self.source)

    def test_missing_source_is_clear(self):
        with self.assertRaisesRegex(ValueError, "source is missing"):
            floating.build(self.root / "gone.swift")

    def test_compiles_once_per_source_and_removes_older_builds(self):
        with patch("hq.floating.subprocess.run", side_effect=self.compiled) as compile_it:
            first = floating.build(self.source)
            self.assertEqual(floating.build(self.source), first)
            self.assertEqual(compile_it.call_count, 1)
            self.assertEqual(first.name, "hq-float")  # WebKit keys its storage on the file name.
            self.assertEqual(compile_it.call_args.args[0][:2], ["swiftc", "-O"])
            self.source.write_text("// two")
            second = floating.build(self.source)
        self.assertEqual(compile_it.call_count, 2)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(second.name, "hq-float")
        self.assertTrue(second.exists())
        self.assertFalse(first.parent.exists())

    def test_cached_build_needs_no_compiler(self):
        with patch("hq.floating.subprocess.run", side_effect=self.compiled):
            binary = floating.build(self.source)
        self.which.return_value = None
        self.assertEqual(floating.build(self.source), binary)

    def test_failed_compile_reports_why_and_leaves_nothing_to_run(self):
        def fail(command, **_):
            Path(command[command.index("-o") + 1]).write_text("half a binary")
            return Mock(returncode=1, stderr="error: boom")
        with patch("hq.floating.subprocess.run", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "boom"):
                floating.build(self.source)
        self.assertEqual([p.name for p in (self.root / "data/float").rglob("*") if p.is_file()], [])


class LifecycleTests(FloatingCase):
    def setUp(self):
        super().setUp()
        patches = {"require_route": Mock(), "build": Mock(return_value=self.fake_binary())}
        for name, value in patches.items():
            patcher = patch(f"hq.floating.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.children = []

    def fake_binary(self):
        script = self.root / "hq-float"
        script.write_text("#!/bin/sh\nexec sleep 30\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def spawn(self, *args, **kwargs):
        child = REAL_POPEN(*args, **kwargs)
        self.children.append(child)
        self.addCleanup(self.reap, child)
        return child

    @staticmethod
    def reap(child):
        if child.poll() is None:
            child.kill()
        child.wait()

    def start_real_panel(self):
        with patch("hq.floating.subprocess.Popen", side_effect=self.spawn):
            return floating.start(4310, self.ensure)

    def test_lock_outlives_the_command_and_a_second_start_only_wakes_it(self):
        result = self.start_real_panel()
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["url"], "http://127.0.0.1:4310/float")
        self.assertEqual(floating.read_pid(), result["pid"])
        self.assertEqual(self.children[0].pid, result["pid"])
        # This process closed its descriptor; only the child can be holding the lock now.
        self.assertTrue(floating.panel_running())
        with patch("hq.floating.os.kill") as kill, patch("hq.floating.subprocess.Popen") as spawn:
            again = floating.start(4310, self.ensure)
        self.assertEqual(again["status"], "already running")
        self.assertEqual(again["pid"], result["pid"])
        kill.assert_called_once_with(result["pid"], signal.SIGUSR1)
        spawn.assert_not_called()

    def test_panel_gets_its_url_state_file_and_log(self):
        with patch("hq.floating.subprocess.Popen", side_effect=self.spawn) as spawn:
            floating.start(4310, self.ensure)
        command = spawn.call_args.args[0]
        self.assertEqual(command[1:], ["http://127.0.0.1:4310/float", str(floating.state_path())])
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(len(spawn.call_args.kwargs["pass_fds"]), 1)
        self.assertEqual(stat.S_IMODE(floating.log_path().stat().st_mode), 0o600)

    def test_uses_the_port_the_running_server_reports(self):
        self.ensure.return_value = {"port": 4999}
        self.assertTrue(self.start_real_panel()["url"].endswith(":4999/float"))
        self.ensure.assert_called_once_with(4310)

    def test_stop_terminates_and_frees_the_lock(self):
        result = self.start_real_panel()
        self.assertEqual(floating.stop(), {"status": "stopped", "pid": result["pid"]})
        self.assertFalse(floating.panel_running())
        self.assertIsNone(floating.read_pid())
        self.assertEqual(floating.stop(), {"status": "not running"})

    def test_stop_reports_a_panel_that_will_not_exit(self):
        lock = floating.take_lock()
        self.addCleanup(lock.close)
        floating.write_pid(424242)
        with patch("hq.floating.os.kill") as kill, patch("hq.floating.time.sleep"):
            with self.assertRaisesRegex(ValueError, "still shutting down"):
                floating.stop()
        kill.assert_called_once_with(424242, signal.SIGTERM)

    def test_stop_without_a_pid_says_so_rather_than_guessing(self):
        lock = floating.take_lock()
        self.addCleanup(lock.close)
        with self.assertRaisesRegex(ValueError, "pid file is missing"):
            floating.stop()

    def test_panel_that_dies_at_once_is_an_error_and_frees_the_lock(self):
        floating.build.return_value = Path("/usr/bin/false")
        with patch("hq.floating.subprocess.Popen", side_effect=self.spawn), patch("hq.floating.time.sleep") as nap:
            nap.side_effect = lambda _: self.children[0].wait()
            with self.assertRaisesRegex(ValueError, "exited right away"):
                floating.start(4310, self.ensure)
        self.assertFalse(floating.panel_running())

    def test_status_reports_running_pid_dock_and_login(self):
        result = self.start_real_panel()
        floating.state_path().write_text(json.dumps({"dock": "bottom-right"}))
        floating.plist_path().parent.mkdir(parents=True)
        floating.plist_path().write_bytes(b"x")
        self.assertEqual(floating.status(), {"running": True, "pid": result["pid"], "dock": "bottom-right", "installed_at_login": True})
        floating.state_path().write_text("not json")
        self.assertEqual(floating.status()["dock"], "top-left")
        floating.state_path().write_text("[1]")
        self.assertEqual(floating.status()["dock"], "top-left")

    def test_run_execs_in_place_with_the_lock_inherited(self):
        held = []
        def at_exec(*_):
            held.append(floating.panel_running())
            raise SystemExit
        with patch("hq.floating.os.execv", side_effect=at_exec) as execv, \
                patch("hq.floating.os.set_inheritable", wraps=os.set_inheritable) as inheritable:
            with self.assertRaises(SystemExit):
                floating.run(4310, self.ensure)
        binary = str(floating.build.return_value)
        execv.assert_called_once_with(binary, [binary, "http://127.0.0.1:4310/float", str(floating.state_path())])
        self.assertEqual(held, [True])
        self.assertIs(inheritable.call_args.args[1], True)
        self.assertEqual(floating.read_pid(), os.getpid())  # exec keeps the pid, so stop can find it.

    def test_lock_and_pid_really_survive_exec(self):
        program = ("import sys\nsys.platform = 'darwin'\nfrom pathlib import Path\nfrom hq import floating\n"
                   "floating.require_route = lambda url: None\nfloating.build = lambda: Path(sys.argv[1])\n"
                   "floating.run(4310, lambda port: {'port': port})\n")
        child = self.spawn([sys.executable, "-c", program, str(floating.build.return_value)], cwd=ROOT)
        # Wait on the pid file, not the lock: probing the lock could make the child think one is held.
        for _ in range(100):
            if floating.read_pid() == child.pid:
                break
            floating.time.sleep(0.05)
        self.assertEqual(floating.read_pid(), child.pid)
        floating.time.sleep(0.2)  # Long enough for the interpreter to have become the fake panel.
        self.assertIsNone(child.poll())
        self.assertTrue(floating.panel_running())
        self.assertEqual(floating.stop(), {"status": "stopped", "pid": child.pid})
        self.assertFalse(floating.panel_running())

    def test_lab_end_to_end_with_real_processes(self):
        """A real throwaway server and a stand-in panel that talks to it, then reports picks."""
        panel = self.root / "stand-in-panel"
        panel.write_text(f"#!{sys.executable}\n"
                         "import json, sys, urllib.request\n"
                         "origin = sys.argv[1].split('/float')[0]\n"
                         "health = json.load(urllib.request.urlopen(origin + '/api/health'))\n"
                         "print('a stray line')\n"
                         "print(json.dumps({'picks': health['app'] + ' ' + sys.argv[-1]}), flush=True)\n")
        panel.chmod(panel.stat().st_mode | stat.S_IXUSR)
        floating.build.return_value = panel
        made = []
        real_mkdtemp = tempfile.mkdtemp
        def mkdtemp(**kwargs):
            made.append(Path(real_mkdtemp(**kwargs)))
            return str(made[-1])
        with patch("hq.floating.subprocess.Popen", side_effect=self.spawn), patch("hq.floating.tempfile.mkdtemp", side_effect=mkdtemp):
            result = floating.lab()
        self.assertEqual(result["status"], "saved")
        self.assertRegex(result["picks"], r"^hq http://127\.0\.0\.1:\d+/float-lab$")
        self.assertIsNotNone(self.children[0].poll())  # The server is stopped.
        self.assertFalse(made[0].exists())
        self.assertFalse((self.root / "data/hq.sqlite3").exists())  # The real database was never opened.

    def test_run_exits_quietly_when_a_panel_is_up(self):
        lock = floating.take_lock()
        self.addCleanup(lock.close)
        with patch("hq.floating.os.execv") as execv:
            self.assertEqual(floating.run(4310, self.ensure)["status"], "already running")
        execv.assert_not_called()


class RouteCheckTests(FloatingCase):
    def test_an_older_server_gets_a_clear_way_forward(self):
        error = urllib.error.HTTPError("http://x/float", 404, "Not Found", {}, io.BytesIO())
        with patch("hq.floating.urllib.request.urlopen", side_effect=error), patch("hq.floating.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "Run hq stop, then hq float"):
                floating.start(4310, self.ensure)
        spawn.assert_not_called()

    def test_other_failures_are_reported_too(self):
        error = urllib.error.HTTPError("http://x/float", 500, "Broken", {}, io.BytesIO())
        with patch("hq.floating.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "answered 500"):
                floating.require_route("http://127.0.0.1:1/float")
        with patch("hq.floating.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaisesRegex(ValueError, "not answering"):
                floating.require_route("http://127.0.0.1:1/float")


class LaunchAgentTests(FloatingCase):
    def setUp(self):
        super().setUp()
        build = patch("hq.floating.build", return_value=Path("/bin/true"))
        build.start()
        self.addCleanup(build.stop)
        run = patch("hq.floating.subprocess.run", return_value=Mock(returncode=0, stdout="", stderr=""))
        self.run_command = run.start()
        self.addCleanup(run.stop)

    def test_agent_restarts_after_a_crash_but_not_after_a_clean_stop(self):
        agent = floating.launch_agent(4311)
        self.assertEqual(agent["Label"], "local.hq.float")
        self.assertEqual(agent["ProgramArguments"][0], sys.executable)
        self.assertEqual(agent["ProgramArguments"][-4:], ["float", "run", "--port", "4311"])
        self.assertTrue(agent["ProgramArguments"][1].endswith("bin/hq"))
        self.assertIs(agent["RunAtLoad"], True)
        self.assertEqual(agent["KeepAlive"], {"SuccessfulExit": False})
        self.assertIs(agent["AbandonProcessGroup"], True)
        self.assertEqual(agent["StandardOutPath"], str(floating.log_path()))
        self.assertEqual(agent["StandardErrorPath"], str(floating.log_path()))
        self.assertEqual(agent["EnvironmentVariables"], {"HQ_HOME": os.environ["HQ_HOME"]})

    def test_default_data_directory_adds_no_environment(self):
        del os.environ["HQ_HOME"]
        self.assertNotIn("EnvironmentVariables", floating.launch_agent(4310))

    def test_install_writes_a_valid_plist_and_loads_it(self):
        result = floating.install(4310)
        path = self.home / "Library/LaunchAgents/local.hq.float.plist"
        self.assertEqual(result["plist"], str(path))
        self.assertEqual(plistlib.loads(path.read_bytes())["Label"], "local.hq.float")
        floating.build.assert_called_once()
        domain = f"gui/{os.getuid()}"
        self.assertEqual([call.args[0] for call in self.run_command.call_args_list],
                         [["launchctl", "bootout", f"{domain}/local.hq.float"],
                          ["launchctl", "bootstrap", domain, str(path)]])
        self.assertTrue(floating.status()["installed_at_login"])

    def test_failed_load_is_reported_and_removes_the_plist(self):
        self.run_command.side_effect = [Mock(returncode=3, stdout="", stderr="not loaded"),
                                        Mock(returncode=5, stdout="", stderr="Input/output error")]
        with self.assertRaisesRegex(ValueError, "Input/output error"):
            floating.install(4310)
        self.assertFalse(floating.plist_path().exists())

    def test_uninstall_unloads_and_removes(self):
        floating.install(4310)
        self.run_command.reset_mock()
        self.assertEqual(floating.uninstall()["status"], "removed")
        self.assertFalse(floating.plist_path().exists())
        self.run_command.assert_called_once()
        self.assertEqual(self.run_command.call_args.args[0], ["launchctl", "bootout", f"gui/{os.getuid()}/local.hq.float"])
        self.assertEqual(floating.uninstall(), {"status": "not installed"})


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "lab.sqlite3")
        self.ids = floating.seed_lab(self.store)
        self.items = self.store.snapshot()["items"]
        self.needs = self.store.snapshot(bucket="needs")["items"]

    def test_agent_work_does_not_count_as_needing_the_human(self):
        self.assertEqual(len(self.ids), 13)
        self.assertEqual(len(self.needs), 12)
        pending = self.store.snapshot(bucket="pending")["items"]
        self.assertEqual([i["title"] for i in pending], ["Retry the failed import"])
        self.assertEqual(pending[0]["owner"], "agent")
        self.assertNotIn(pending[0]["id"], [i["id"] for i in self.needs])

    def test_every_shape_the_panel_draws_is_present(self):
        decisions = [i for i in self.needs if i["kind"] == "question"]
        self.assertTrue(any(o.get("recommended") and o.get("note") for i in decisions for o in i["options"] if isinstance(o, dict)))
        steps = [s for i in self.needs for s in i["steps"] if isinstance(s, dict)]
        for field in ("link", "copy", "checks"):
            self.assertTrue(any(s.get(field) for s in steps), field)
        self.assertTrue(any(i["kind"] == "blocker" for i in self.needs))
        self.assertTrue(any(i["pinned"] for i in self.needs))
        today = floating.date.today().isoformat()
        self.assertTrue(any(i["due"] == today for i in self.needs))
        self.assertTrue(any(i["due"] and i["due"] < today for i in self.needs))
        self.assertTrue(any(len(i["title"]) > 150 for i in self.needs))
        self.assertTrue(any(b.get("mono") for i in self.needs for b in i["paste"]))
        self.assertTrue(any(i["matters"] and i["needs_you"] for i in self.needs))
        self.assertTrue(any(not i["matters"] and i["detail"] for i in self.needs))  # An older, unreadable item.

    def test_two_projects_three_workstreams_and_a_spread_of_ages(self):
        self.assertEqual({i["project"] for i in self.items}, {"harbor", "lantern"})
        self.assertGreaterEqual(len({i["workstream"] for i in self.items}), 3)
        ages = [floating.time.time() - i["created"] for i in self.needs]
        self.assertLess(min(ages), 15 * 60)
        self.assertGreater(max(ages), 2 * 86400)

    def test_nothing_personal_or_real_in_any_field(self):
        text = json.dumps(self.store.snapshot()).lower()
        user = getpass.getuser().lower()
        banned = ["dhruv", "gmail", "/users/", "/home/", "noclick", "wahooks", "ultron", "appsumo", str(Path.home()).lower()]
        for word in banned + ([user] if len(user) > 3 else []):
            self.assertNotIn(word, text)
        urls = [s["link"]["url"] for i in self.needs for s in i["steps"] if isinstance(s, dict) and s.get("link")]
        self.assertTrue(urls)
        self.assertTrue(all(url.startswith("https://example.com/") for url in urls))
        self.assertIsNone(self.store.session("lab:fixtures")["cwd"])


class FakeProcess:
    def __init__(self, lines=(), code=0, poll=None):
        self.lines, self.code, self.exited = lines, code, poll
        self.pid = 4242
        self.terminated = False
        self.stdout = self

    def __iter__(self):
        for line in self.lines:
            if isinstance(line, BaseException):
                raise line
            yield line

    def close(self):
        pass

    def poll(self):
        return self.exited

    def wait(self, timeout=None):
        self.exited = self.code if self.exited is None else self.exited
        return self.exited

    def terminate(self):
        self.terminated = True
        self.exited = -15


class LabTests(FloatingCase):
    PICKS = "Pill: count\nGlide: 300 ms"

    def setUp(self):
        super().setUp()
        build = patch("hq.floating.build", return_value=Path("/bin/true"))
        build.start()
        self.addCleanup(build.stop)
        self.server = FakeProcess()
        self.panel = FakeProcess([json.dumps({"picks": self.PICKS}) + "\n"])
        spawn = patch("hq.floating.subprocess.Popen", side_effect=[self.server, self.panel])
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)
        health = patch("hq.floating.wait_for_health")
        self.health = health.start()
        self.addCleanup(health.stop)

    def lab_home(self):
        return Path(self.spawn.call_args_list[0].kwargs["env"]["HQ_HOME"])

    def assert_cleaned_up(self):
        self.assertFalse(self.lab_home().exists())
        self.assertTrue(self.server.terminated)

    def real_item(self):
        store = Store()
        store.register("a", "Agent")
        return store.add("a", "Try the lab", kind="question", owner="human")["id"]

    def test_normal_exit_saves_picks_and_removes_the_throwaway_data(self):
        with patch.object(Store, "reply") as reply:
            result = floating.lab()
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["picks"], self.PICKS)
        self.assertIsNone(result["item"])
        saved = json.loads(floating.picks_path().read_text())
        self.assertEqual(saved["picks"], self.PICKS)
        self.assertEqual(stat.S_IMODE(floating.picks_path().stat().st_mode), 0o600)
        reply.assert_not_called()
        self.assert_cleaned_up()

    def test_runs_only_against_the_throwaway_hq(self):
        real = os.environ["HQ_HOME"]
        floating.lab()
        server, panel = self.spawn.call_args_list
        env = server.kwargs["env"]
        self.assertEqual(env["HQ_FLOAT_LAB"], "1")
        self.assertNotEqual(env["HQ_HOME"], real)
        self.assertEqual(os.environ["HQ_HOME"], real)
        self.assertNotIn("HQ_FLOAT_LAB", os.environ)
        self.assertIn("serve", server.args[0])
        port = server.args[0][server.args[0].index("--port") + 1]
        self.assertNotEqual(port, "4310")
        command = panel.args[0]
        self.assertEqual(command[1], f"http://127.0.0.1:{port}/float?lab=1")
        self.assertEqual(command[-2:], ["--bench", f"http://127.0.0.1:{port}/float-lab"])
        self.assertTrue(command[2].startswith(env["HQ_HOME"]))
        self.assertEqual(panel.kwargs["stdout"], subprocess.PIPE)
        self.assertFalse(Store().snapshot()["items"])  # The real database gained nothing.

    def test_ctrl_c_during_the_sitting_cleans_up(self):
        self.panel.lines = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            floating.lab()
        self.assertTrue(self.panel.terminated)
        self.assert_cleaned_up()
        self.assertFalse(floating.picks_path().exists())

    def test_ctrl_c_while_the_server_starts_cleans_up(self):
        self.health.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            floating.lab()
        self.assertEqual(self.spawn.call_count, 1)
        self.assert_cleaned_up()

    def test_failed_start_reports_and_cleans_up(self):
        self.health.side_effect = ValueError("The lab's HQ did not start")
        with self.assertRaisesRegex(ValueError, "did not start"):
            floating.lab()
        self.assertEqual(self.spawn.call_count, 1)
        self.assert_cleaned_up()

    def test_panel_that_fails_is_reported_and_cleans_up(self):
        self.panel = FakeProcess(code=3)
        self.spawn.side_effect = [self.server, self.panel]
        with self.assertRaisesRegex(ValueError, "exited with status 3"):
            floating.lab()
        self.assert_cleaned_up()

    def test_closing_the_bench_without_picks_saves_nothing(self):
        self.panel.lines = []
        with patch.object(Store, "reply") as reply:
            self.assertEqual(floating.lab(), {"status": "closed without picks"})
        reply.assert_not_called()
        self.assertFalse(floating.picks_path().exists())
        self.assert_cleaned_up()

    def test_stray_output_does_not_lose_the_picks(self):
        self.panel.lines = ["warning: something\n", "[1,2]\n", json.dumps({"picks": self.PICKS}) + "\n", "\n"]
        self.assertEqual(floating.lab()["picks"], self.PICKS)

    def test_item_receives_the_picks_exactly_once_in_the_real_store(self):
        item = self.real_item()
        real_reply = Store.reply
        with patch.object(Store, "reply", autospec=True, side_effect=real_reply) as reply:
            result = floating.lab(item)
        self.assertEqual(reply.call_count, 1)
        self.assertEqual(reply.call_args.args[1:], (item, self.PICKS))
        self.assertEqual(Store().item(item)["answer"], self.PICKS)
        self.assertEqual(result["item"], item)
        # Saved, not received: only the agent's acknowledgement may say that.
        self.assertEqual(result["delivery"], "pending")
        self.assertEqual(result["status"], "saved")
        self.assert_cleaned_up()

    def test_closed_item_is_refused_before_the_sitting_starts(self):
        item = self.real_item()
        Store().reply(item, "already answered")
        with self.assertRaisesRegex(ValueError, "not open"):
            floating.lab(item)
        self.spawn.assert_not_called()
        floating.build.assert_not_called()
        with self.assertRaisesRegex(ValueError, "not found"):
            floating.lab("missing")

    def test_picks_survive_an_item_closed_during_the_sitting(self):
        item = self.real_item()
        def answered_meanwhile():
            Store().reply(item, "answered elsewhere")
            yield json.dumps({"picks": self.PICKS}) + "\n"
        self.panel.lines = answered_meanwhile()
        with self.assertRaisesRegex(ValueError, "picks are saved in"):
            floating.lab(item)
        self.assertEqual(json.loads(floating.picks_path().read_text())["picks"], self.PICKS)


class HealthTests(FloatingCase):
    def test_server_that_exits_is_noticed_rather_than_waited_on(self):
        log = self.root / "server.log"
        log.write_text("port in use")
        with patch("hq.floating.urllib.request.urlopen") as get:
            with self.assertRaisesRegex(ValueError, "port in use"):
                floating.wait_for_health("http://127.0.0.1:1", FakeProcess(poll=1), log)
        get.assert_not_called()

    def test_a_real_server_counts_as_healthy(self):
        server = Server(0, Store(self.root / "health.sqlite3"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        floating.wait_for_health(server.origin, FakeProcess(), self.root / "unused.log")


class CommandTests(FloatingCase):
    def run_cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["hq", *arguments]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
                code = 0
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_status_prints_json_without_a_server_or_database(self):
        code, out, _ = self.run_cli("float", "status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["running"], False)
        self.assertFalse((self.root / "data/hq.sqlite3").exists())

    def test_default_action_is_start_with_the_server_ensured(self):
        with patch("hq.floating.start", return_value={"status": "started"}) as start:
            code, out, _ = self.run_cli("float", "--port", "4322")
        self.assertEqual(code, 0)
        start.assert_called_once_with(4322, cli.ensure_server)
        self.assertEqual(json.loads(out), {"status": "started"})

    def test_item_is_only_for_the_lab(self):
        code, _, err = self.run_cli("float", "stop", "--item", "x")
        self.assertEqual(code, 1)
        self.assertIn("--item is for hq float lab", err)
        with patch("hq.floating.lab", return_value={"status": "closed without picks"}) as lab:
            self.assertEqual(self.run_cli("float", "lab", "--item", "x")[0], 0)
        lab.assert_called_once_with("x")

    def test_errors_exit_cleanly_with_the_message(self):
        with patch("sys.platform", "linux"):
            code, _, err = self.run_cli("float")
        self.assertEqual(code, 1)
        self.assertIn("needs macOS", err)

    def test_ensuring_the_server_neither_opens_a_browser_nor_prints(self):
        state = {"port": 4310, "pid": 1, "instance": "a"}
        out = io.StringIO()
        with patch("hq.cli.running", return_value=state), patch("hq.cli.webbrowser.open") as browser, contextlib.redirect_stdout(out):
            self.assertEqual(cli.ensure_server(4310), state)
        browser.assert_not_called()
        self.assertEqual(out.getvalue(), "")

    def test_hq_start_still_prints_and_opens(self):
        state = {"port": 4310, "pid": 1, "instance": "a"}
        out = io.StringIO()
        with patch("hq.cli.running", return_value=state), patch("hq.cli.webbrowser.open") as browser, contextlib.redirect_stdout(out):
            cli.launch(4310)
        browser.assert_called_once_with("http://127.0.0.1:4310")
        self.assertEqual(out.getvalue().strip(), "http://127.0.0.1:4310")


class LabIsolationTests(unittest.TestCase):
    """The lab's server holds fixtures only: it never learns about or delivers to a real agent."""

    def workers_started(self, lab):
        environment = {"HQ_HOME": tempfile.mkdtemp()}
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", environment["HQ_HOME"]]))
        if lab:
            environment["HQ_FLOAT_LAB"] = "1"
        cleared = {key: value for key, value in os.environ.items() if key != "HQ_FLOAT_LAB"}
        with patch.dict(os.environ, {**cleared, **environment}, clear=True), \
                patch("hq.server.Dispatcher") as dispatcher, patch("hq.server.ClaudePresence") as presence, \
                patch.object(Server, "serve_forever"), contextlib.redirect_stdout(io.StringIO()):
            # Off the main thread, so serve() leaves this process's signal handlers alone.
            thread = threading.Thread(target=server_module.serve, args=(0,))
            thread.start()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        return dispatcher.call_count, presence.call_count

    def test_the_lab_server_runs_neither_background_worker(self):
        self.assertEqual(self.workers_started(lab=True), (0, 0))

    def test_the_real_server_still_runs_both(self):
        self.assertEqual(self.workers_started(lab=False), (1, 1))

    def test_the_bench_adds_items_only_to_the_fixture_session(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "lab.sqlite3")
            floating.seed_lab(store)
            sessions = [session["id"] for session in store.snapshot()["sessions"]]
        bench = (ROOT / "hq/static/float-lab.js").read_text()
        self.assertEqual(sessions, ["lab:fixtures"])
        self.assertIn(f"const LAB_SESSION = '{sessions[0]}'", bench)
        self.assertNotIn("sessions[0]", bench)


if __name__ == "__main__":
    unittest.main()

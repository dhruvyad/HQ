"""hq doctor reads a temporary home and data folder, with fake agents on PATH and its
own server on a free port. It must change nothing and never print a secret."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from hq import doctor, version
from hq.connections import hq_command, install_hooks
from hq.server import Server
from hq.store import Store, data_dir

A, B = "a" * 40, "b" * 40
SECRET = "sk-very-secret-123"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class DoctorCase(unittest.TestCase):
    build = {"from": "install", "commit": A, "ref": "main", "installed_at": "2026-11-06T12:00:00Z"}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.data = self.root / "data"
        self.bin = self.root / "bin"
        for folder in (self.home, self.bin):
            folder.mkdir()
        for name in ("claude", "codex"):
            (self.bin / name).write_text("#!/bin/sh\n")
            (self.bin / name).chmod(0o755)
        self.build = {**self.build, "hq": str(self.bin / "hq")}
        (self.bin / "hq").write_text("#!/bin/sh\n")
        (self.bin / "hq").chmod(0o755)
        environment = {"HQ_HOME": str(self.data), "PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "en_US.UTF-8",
                       "ANTHROPIC_API_KEY": SECRET}
        for patcher in (patch.dict(os.environ, environment), patch.object(Path, "home", return_value=self.home),
                        patch("sys.platform", "linux"), patch.object(version, "build", side_effect=lambda: dict(self.build)),
                        # Never the real HQ on 4310.
                        patch.object(doctor, "DEFAULT_PORT", free_port())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def report(self):
        return doctor.report()

    def checks(self):
        return {c["check"]: c for c in self.report()["checks"]}

    def text(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.run()
        return code, out.getvalue()

    def connect(self, command=None):
        """What hq setup leaves behind, for this hq or the command given."""
        command = command or hq_command()
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"hq": {
            "type": "stdio", "command": command[0], "args": [*command[1:], "mcp", "--provider", "claude-code"],
            "env": {"TOKEN": SECRET}}}, "oauthAccount": {"emailAddress": "someone@example.com", "accessToken": SECRET}}))
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text(
            f'model = "x"\n\n[mcp_servers.hq]\ncommand = {json.dumps(command[0])}\n'
            f'args = {json.dumps([*command[1:], "mcp", "--provider", "codex"])}\ntool_timeout_sec = 3600\n'
            f'env = {{ TOKEN = "{SECRET}" }}\n')
        install_hooks("claude-code")
        install_hooks("codex")

    def serve(self, commit=A):
        server = Server(0, Store(self.data / "hq.sqlite3"))
        server.commit = commit
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.data / "server.json").write_text(json.dumps({"pid": os.getpid(), "port": server.server_port, "instance": server.instance}))
        return server


class FreshMachineTests(DoctorCase):
    def test_a_fresh_machine_says_what_to_run_and_changes_nothing(self):
        checks = self.checks()
        self.assertEqual(checks["Data folder"]["status"], "warn")
        self.assertEqual(checks["Inbox"]["fix"], "Run hq once; it makes the inbox")
        self.assertEqual(checks["Server"]["status"], "warn")
        for name in ("Claude Code tools", "Claude Code hooks", "Codex tools", "Codex hooks"):
            self.assertEqual((checks[name]["status"], checks[name]["fix"]), ("fail", "hq setup"), name)
        self.assertFalse(self.data.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_an_agent_that_is_not_installed_is_only_a_warning(self):
        (self.bin / "codex").unlink()
        checks = self.checks()
        self.assertEqual(checks["Codex"]["status"], "warn")
        self.assertNotIn("Codex tools", checks)

    def test_exit_code_and_json(self):
        code, text = self.text()
        self.assertEqual(code, 1)
        self.assertIn("Fix the problems first", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.run(as_json=True)
        result = json.loads(out.getvalue())
        self.assertEqual(result["hq"], "hq 0.1.0 (aaaaaaa, installed 2026-11-06)")
        self.assertEqual(sum(result["counts"].values()), len(result["checks"]))
        self.assertTrue(all(c["status"] in {"ok", "warn", "fail"} and c["summary"] for c in result["checks"]))
        self.assertTrue(all("fix" in c for c in result["checks"] if c["status"] != "ok"))


class HealthyTests(DoctorCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.serve()

    def test_a_connected_setup_is_all_ok(self):
        checks = self.report()["checks"]
        self.assertEqual([c["check"] for c in checks if c["status"] != "ok"], [])
        named = {c["check"]: c["summary"] for c in checks}
        self.assertIn("every table and column present", named["Inbox"])
        self.assertIn("running aaaaaaa", named["Server"])
        self.assertIn("trust them in /hooks", named["Codex hooks"])
        code, text = self.text()
        self.assertEqual(code, 0)
        self.assertIn("Everything looks right.", text)

    def test_never_prints_a_secret_or_an_email(self):
        with Store(self.data / "hq.sqlite3").db() as db:
            db.execute("INSERT INTO accounts (tool,key,identity,email,added,stale) VALUES ('claude','k','i','someone@example.com',1,1)")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.run()
            doctor.run(as_json=True)
        self.assertNotIn(SECRET, out.getvalue())
        self.assertNotIn("someone@example.com", out.getvalue())

    def test_reads_the_inbox_without_writing_it(self):
        path = self.data / "hq.sqlite3"
        with Store(path).db() as db:
            db.execute("ALTER TABLE accounts DROP COLUMN stale")
        before = path.read_bytes()
        checks = self.checks()
        self.assertEqual(checks["Inbox"]["status"], "fail")
        self.assertIn("accounts.stale", checks["Inbox"]["summary"])
        self.assertEqual(checks["Claude"]["status"], "ok")  # Accounts are still counted without it.
        self.assertEqual(path.read_bytes(), before)
        columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(accounts)")}
        self.assertNotIn("stale", columns)

    def test_a_server_left_on_an_older_build_is_named(self):
        self.build["commit"] = B
        server = self.checks()["Server"]
        self.assertEqual(server["status"], "warn")
        self.assertIn("runs aaaaaaa while bbbbbbb is installed", server["summary"])

    def test_counts_accounts_that_need_signing_in_again(self):
        with Store(self.data / "hq.sqlite3").db() as db:
            for key, stale in (("one", 0), ("two", 1)):
                db.execute("INSERT INTO accounts (tool,key,identity,email,added,stale) VALUES ('codex',?,?,'',1,?)", (key, key, stale))
        codex = self.checks()["Codex"]
        self.assertEqual(codex["status"], "warn")
        self.assertIn("2 saved; 1 marked needs sign-in", codex["summary"])


class ProblemTests(DoctorCase):
    def test_tools_and_hooks_that_point_at_a_moved_hq(self):
        self.connect()
        gone = "/old/HQ/bin/hq"
        settings = self.home / ".claude/settings.json"
        settings.write_text(settings.read_text().replace(hq_command()[-1], gone))
        claude = json.loads((self.home / ".claude.json").read_text())
        claude["mcpServers"]["hq"]["args"][0] = gone
        (self.home / ".claude.json").write_text(json.dumps(claude))
        checks = self.checks()
        self.assertEqual(checks["Claude Code tools"]["status"], "fail")
        self.assertIn("claude mcp remove --scope user hq", checks["Claude Code tools"]["fix"])
        self.assertIn("/old/HQ/bin/hq, which is gone", checks["Claude Code hooks"]["summary"])

    def test_python_and_python3_in_one_environment_are_the_same_hq(self):
        # Setup registered bin/python; after uv's reinstall, hq runs as bin/python3.
        env = self.root / "env/bin"
        env.mkdir(parents=True)
        (env / "python").symlink_to(os.path.realpath(sys.executable))
        (env / "python3").symlink_to("python")
        (env / "other").symlink_to(os.path.realpath(sys.executable))
        registered = [str(env / "python"), "-m", "hq"]
        with patch("hq.connections.hq_command", return_value=registered):
            self.connect(registered)
        with patch.object(doctor, "hq_command", return_value=[str(env / "python3"), "-m", "hq"]):
            checks = self.checks()
            self.assertEqual((checks["Claude Code tools"]["status"], checks["Codex hooks"]["status"]), ("ok", "ok"))
        with patch.object(doctor, "hq_command", return_value=[str(self.root / "other/bin/python"), "-m", "hq"]):
            self.assertEqual(self.checks()["Claude Code tools"]["status"], "warn")

    def test_codex_without_a_long_tool_timeout(self):
        self.connect()
        config = self.home / ".codex/config.toml"
        config.write_text(config.read_text().replace("tool_timeout_sec = 3600\n", ""))
        self.assertIn("tool_timeout_sec is not set", self.checks()["Codex tools"]["summary"])

    def test_claude_tools_for_one_project_only(self):
        (self.home / ".claude.json").write_text(json.dumps({"projects": {"/src/app": {"mcpServers": {"hq": {"command": "x"}}}}}))
        tools = self.checks()["Claude Code tools"]
        self.assertEqual(tools["status"], "warn")
        self.assertIn("1 project only", tools["summary"])

    def test_a_broken_config_is_named_not_fatal(self):
        (self.home / ".claude.json").write_text("{nope")
        self.assertEqual(self.checks()["Claude Code tools"]["status"], "fail")

    def test_some_hooks_missing_or_all_turned_off(self):
        install_hooks("claude-code")
        settings = self.home / ".claude/settings.json"
        data = json.loads(settings.read_text())
        del data["hooks"]["Stop"]
        settings.write_text(json.dumps(data))
        self.assertIn("missing for Stop", self.checks()["Claude Code hooks"]["summary"])
        install_hooks("claude-code")
        settings.write_text(json.dumps({**json.loads(settings.read_text()), "disableAllHooks": True}))
        self.assertIn("disableAllHooks", self.checks()["Claude Code hooks"]["summary"])

    def test_a_server_that_crashed_or_stopped_answering(self):
        self.data.mkdir(mode=0o700)
        (self.data / "server.json").write_text(json.dumps({"pid": os.getpid(), "port": free_port(), "instance": "x"}))
        self.assertEqual(self.checks()["Server"]["status"], "fail")
        (self.data / "server.json").write_text(json.dumps({"pid": 2 ** 22 + 7, "port": free_port(), "instance": "x"}))
        self.assertIn("stopped without tidying up", self.checks()["Server"]["summary"])

    def test_a_data_folder_others_can_read(self):
        self.data.mkdir(mode=0o755)
        os.chmod(self.data, 0o755)
        folder = self.checks()["Data folder"]
        self.assertEqual(folder["status"], "warn")
        self.assertEqual(folder["fix"], f"chmod 700 {data_dir()}")

    def test_path_without_the_hq_folder(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            path = self.checks()["PATH"]
        self.assertEqual(path["status"], "fail")
        self.assertIn("uv tool update-shell", path["fix"])
        self.assertEqual(self.checks()["PATH"]["status"], "ok")

    def test_a_locale_that_is_not_utf8(self):
        with patch.dict(os.environ, {"LANG": "C", "LC_ALL": "", "LC_CTYPE": ""}):
            locale = self.checks()["Locale"]
        self.assertEqual(locale["status"], "warn")
        self.assertIn("LANG=C", locale["summary"])

    def test_a_check_that_breaks_does_not_hide_the_rest(self):
        with patch.object(doctor, "agent_checks", side_effect=RuntimeError("boom")):
            checks = self.report()["checks"]
        self.assertIn("boom", next(c for c in checks if c["section"] == "Agents")["summary"])
        self.assertIn("Locale", {c["check"] for c in checks})


class PillTests(DoctorCase):
    displays = [{"main": True, "builtin": True, "mirrors": False, "x": 0, "y": 0, "width": 1512, "height": 982, "scale": 2.0},
                {"main": False, "builtin": False, "mirrors": False, "x": 1512, "y": -300, "width": 2560, "height": 1440, "scale": 1},
                {"main": False, "builtin": False, "mirrors": False, "x": 0, "y": -1080, "width": 1920, "height": 1080, "scale": 1}]

    def test_reports_tools_build_and_displays_without_making_a_lock(self):
        tools = Mock(returncode=0, stdout="/Library/Developer/CommandLineTools\n")
        with patch("sys.platform", "darwin"), patch.object(doctor.subprocess, "run", return_value=tools), \
                patch.object(doctor, "displays", return_value=self.displays):
            checks = {c["check"]: c for c in doctor.pill_checks()}
        self.assertEqual(checks["Command line tools"]["status"], "ok")
        self.assertEqual(checks["Running"]["status"], "warn")
        self.assertEqual(checks["Displays"]["summary"],
                         "3: built-in 1512x982 @2x, main; external 2560x1440, right at (1512, -300); "
                         "external 1920x1080, above at (0, -1080)")
        self.assertFalse(self.data.exists())

    def test_missing_tools_fail_until_the_pill_is_built(self):
        with patch("sys.platform", "darwin"), patch.object(doctor.subprocess, "run", return_value=Mock(returncode=2, stdout="")), \
                patch.object(doctor, "displays", return_value=[]):
            checks = {c["check"]: c for c in doctor.pill_checks()}
        self.assertEqual((checks["Command line tools"]["status"], checks["Command line tools"]["fix"]),
                         ("fail", "xcode-select --install, then hq float"))
        self.assertEqual(checks["Displays"]["status"], "warn")

    def test_a_running_pill_on_an_older_build(self):
        from hq import floating
        binary = floating.binary_path()
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        with patch("sys.platform", "darwin"), patch.object(doctor, "pill_running", return_value=True), \
                patch.object(floating, "read_pid", return_value=42), patch.object(doctor, "executable", return_value="/old/hq-float"), \
                patch.object(doctor.subprocess, "run", return_value=Mock(returncode=0, stdout="/x")), \
                patch.object(doctor, "displays", return_value=self.displays[:1]):
            checks = {c["check"]: c for c in doctor.pill_checks()}
        self.assertIn("older build", checks["Running"]["summary"])
        self.assertEqual(checks["Built"]["status"], "ok")

    def test_where_names_each_side(self):
        main = self.displays[0]
        for frame, words in (({"x": -2560, "y": 0}, "left"), ({"x": 0, "y": 982}, "below"),
                             ({"x": 1512, "y": -1440}, "above-right")):
            display = {**self.displays[1], **frame}
            self.assertTrue(doctor.where(display, main).startswith(words), (frame, doctor.where(display, main)))
        self.assertEqual(doctor.where({**self.displays[1], "mirrors": True}, main), "mirrors another")


if __name__ == "__main__":
    unittest.main()

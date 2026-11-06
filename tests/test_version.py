"""Which build is installed, and hq update.

The installer runs for real, piped to sh as curl does, but against fake uv, git and curl
in an empty home, so nothing is downloaded or installed. hq update runs a fake installer
and a fake hq, and never sees a real server, pill or inbox.
"""
import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from hq import cli, version

ROOT = Path(__file__).resolve().parents[1]
A, B = "a" * 40, "b" * 40


def executable(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.calls = self.root / "calls.log"
        tools = self.root / "tools"
        executable(self.bin / "uv", f'echo "uv $*" >> "{self.calls}"\n'
                                    f'if [ "$1 $2" = "tool dir" ]; then if [ "${{3:-}}" = --bin ]; then echo "{self.bin}"; '
                                    f'else echo "{tools}"; fi; fi')
        self.git()
        self.curl()

    def git(self, ls_remote=None, fails=False, head=A, branch="main", changes=""):
        if fails:
            return executable(self.bin / "git", f'echo "git $*" >> "{self.calls}"; exit 1')
        executable(self.bin / "git", f'echo "git $*" >> "{self.calls}"\ncase "$*" in\n'
                                     f'  *ls-remote*) printf "{ls_remote or ""}" ;;\n'
                                     f'  *abbrev-ref*) echo "{branch}" ;;\n  *rev-parse*) echo "{head}" ;;\n'
                                     f'  *status*) printf "{changes}" ;;\nesac')

    def curl(self, answer=None):
        executable(self.bin / "curl", f'echo "curl $*" >> "{self.calls}"\n' +
                   (f'case "$*" in *api.github.com*) echo "{answer}" ;; *) exit 22 ;; esac' if answer else "exit 22"))

    def install(self, **env):
        environment = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "en_US.UTF-8", "HQ_NO_SETUP": "1", **env}
        with (ROOT / "install.sh").open() as script:
            done = subprocess.run(["sh"], stdin=script, env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        return done.stdout

    def stamp(self, home=None):
        return json.loads(((home or self.home / ".local/share/hq") / "install.json").read_text())

    def installed_from(self):
        line = next(line for line in self.calls.read_text().splitlines() if line.startswith("uv tool install"))
        return line.split("--from ", 1)[1].split()[0]

    def test_installs_the_commit_main_names_now_and_records_it(self):
        self.git(ls_remote=f"{A}\\trefs/heads/main\\n")
        self.install()
        self.assertEqual(self.installed_from(), f"https://github.com/dhruvyad/HQ/archive/{A}.tar.gz")
        stamp = self.stamp()
        self.assertEqual((stamp["commit"], stamp["ref"], stamp["dirty"]), (A, "main", False))
        self.assertEqual(stamp["source"], self.installed_from())
        self.assertEqual(stamp["hq"], f"{self.bin}/hq")
        self.assertEqual(stamp["env"], f"{self.root}/tools/hq-local")
        self.assertLess(abs(datetime.fromisoformat(stamp["installed_at"]).timestamp() - datetime.now().timestamp()), 120)
        self.assertEqual(stat.S_IMODE((self.home / ".local/share/hq/install.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.home / ".local/share/hq").stat().st_mode), 0o700)

    def test_without_git_it_asks_the_github_api(self):
        self.git(fails=True)
        self.curl(answer=B)
        self.install()
        self.assertEqual(self.stamp()["commit"], B)
        self.assertIn("Accept: application/vnd.github.sha", self.calls.read_text())

    def test_when_nothing_answers_it_installs_the_branch_and_records_no_commit(self):
        self.git(fails=True)
        self.install(HQ_REF="beta")
        self.assertEqual(self.installed_from(), "https://github.com/dhruvyad/HQ/archive/beta.tar.gz")
        self.assertEqual((self.stamp()["commit"], self.stamp()["ref"]), ("", "beta"))

    def test_a_tag_resolves_to_the_commit_it_points_at(self):
        self.git(ls_remote=f"{A}\\trefs/tags/v1\\n{B}\\trefs/tags/v1^{{}}\\n")
        self.install(HQ_REF="v1")
        self.assertEqual(self.stamp()["commit"], B)
        self.assertEqual(self.installed_from(), f"https://github.com/dhruvyad/HQ/archive/{B}.tar.gz")

    def test_a_commit_is_used_as_it_is(self):
        self.install(HQ_REF=B)
        self.assertEqual(self.stamp()["commit"], B)
        self.assertNotIn("ls-remote", self.calls.read_text())

    def test_a_clone_records_its_commit_branch_and_changes_where_hq_home_says(self):
        clone = self.root / 'my "odd" \\ clone'
        (clone / ".git").mkdir(parents=True)
        self.git(head=B, branch="feature/x", changes=" M hq/cli.py\\n")
        data = self.root / "data dir"
        self.install(HQ_SOURCE=str(clone), HQ_HOME=str(data))
        stamp = self.stamp(data)
        self.assertEqual((stamp["commit"], stamp["ref"], stamp["dirty"], stamp["source"]), (B, "feature/x", True, str(clone)))
        self.assertIn(f"--from {clone} hq-local", self.calls.read_text())
        self.assertFalse((self.home / ".local/share/hq").exists())

    def test_it_still_installs_when_the_build_cannot_be_recorded(self):
        blocked = self.root / "not a folder"
        blocked.write_text("")
        out = self.install(HQ_HOME=str(blocked))
        self.assertIn("could not record", out)
        self.assertIn("Installed.", out)


class BuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        for patcher in (patch.dict(os.environ, {"HQ_HOME": str(self.data)}), patch.object(version, "checkout", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, **stamp):
        (self.data / "install.json").write_text(json.dumps(stamp))

    def test_an_install_names_its_commit_and_day(self):
        self.write(commit=A, ref="main", dirty=False, installed_at="2026-11-06T12:00:00Z", env=sys.prefix)
        self.assertEqual(version.describe(), "hq 0.1.0 (aaaaaaa, installed 2026-11-06)")

    def test_another_branch_and_local_changes_are_named(self):
        self.write(commit=A, ref="beta", dirty=True, installed_at="2026-11-06T12:00:00Z")
        self.assertEqual(version.describe(), "hq 0.1.0 (aaaaaaa on beta with local changes, installed 2026-11-06)")

    def test_an_install_without_a_commit_names_its_branch(self):
        self.write(commit="", ref="main", installed_at="2026-11-06T12:00:00Z")
        self.assertEqual(version.describe(), "hq 0.1.0 (from main, installed 2026-11-06)")

    def test_a_stamp_for_another_environment_is_not_this_hq(self):
        self.write(commit=A, ref="main", env=str(self.data / "elsewhere"))
        self.assertEqual(version.describe(), "hq 0.1.0 (build not recorded)")

    def test_no_stamp_or_a_broken_one_degrades_quietly(self):
        self.assertEqual(version.describe(), "hq 0.1.0 (build not recorded)")
        (self.data / "install.json").write_text("{not json")
        self.assertEqual(version.describe(), "hq 0.1.0 (build not recorded)")

    def test_a_clone_answers_from_git_or_names_its_folder(self):
        self.assertEqual(version.describe({"from": "clone", "source": "/src/HQ", "commit": A, "ref": "main", "dirty": True}),
                         "hq 0.1.0 (aaaaaaa with local changes, from the clone at /src/HQ)")
        clone = self.data / "clone"
        (clone / "bin").mkdir(parents=True)
        (clone / "bin/hq").write_text("")
        with patch.object(version, "ROOT", clone), patch.object(version, "checkout", return_value=clone):
            self.assertEqual(version.build(), {"from": "clone", "source": str(clone)})  # No .git: no git call.

    def test_this_checkout_reports_its_head(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        with patch.object(version, "checkout", return_value=ROOT):
            info = version.build()
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(info["commit"], head)

    def test_version_is_worked_out_only_when_asked(self):
        with patch.object(version, "build", return_value={"from": "unknown"}) as build:
            parser = cli.parser()
            parser.parse_args(["list"])
            build.assert_not_called()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                parser.parse_args(["--version"])
        self.assertEqual(out.getvalue(), "hq 0.1.0 (build not recorded)\n")


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.log"
        self.hq = self.bin / "hq"
        executable(self.hq, f'echo "hq $*" >> "{self.calls}"')
        environment = {key: value for key, value in os.environ.items() if key not in {"HQ_REF", "HQ_SOURCE"}}
        for patcher in (patch.dict(os.environ, {**environment, "HQ_HOME": str(self.data), "PATH": f"{self.bin}:/usr/bin:/bin"}, clear=True),
                        patch.object(version, "checkout", return_value=None), patch("sys.platform", "linux")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = Mock(return_value={"port": 4399, "pid": 1, "instance": "x"})
        self.stop = Mock(return_value={"status": "stopped"})

    def stamp(self, **fields):
        (self.data / "install.json").write_text(json.dumps({"installed_at": "2026-11-01T00:00:00Z", "hq": str(self.hq), **fields}))

    def installer(self, commit, code=0, record=True):
        """An installer that records what it was asked and, like the real one, a new stamp."""
        stamp = json.dumps({"commit": commit, "ref": "main", "installed_at": "2026-11-06T00:00:00Z", "hq": str(self.hq)})
        return (f'echo "installer HQ_NO_SETUP=$HQ_NO_SETUP HQ_REF=${{HQ_REF:-}}" >> "{self.calls}"\n'
                + (f"printf '%s' '{stamp}' > \"$HQ_HOME/install.json\"\n" if record else "")
                + (f"echo boom; exit {code}\n" if code else ""))

    def update(self, script):
        download = Mock(return_value=script)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = version.update(self.server, self.stop, download)
            except ValueError as error:
                code = error
        return code, out.getvalue(), download

    def log(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_installs_the_recorded_branch_without_setup_and_restarts_what_ran(self):
        self.stamp(commit=A, ref="beta", source=f"https://github.com/dhruvyad/HQ/archive/{A}.tar.gz")
        with patch("sys.platform", "darwin"), patch("hq.floating.panel_running", return_value=True), \
                patch("hq.floating.stop") as stop_pill:
            code, out, download = self.update(self.installer(B))
        self.assertEqual(code, 0)
        download.assert_called_once_with("https://raw.githubusercontent.com/dhruvyad/HQ/beta/install.sh")
        self.assertIn("Updated HQ: aaaaaaa -> bbbbbbb (beta)", out)
        self.assertIn(f"compare/{A[:12]}...{B[:12]}", out)
        self.assertEqual(self.log(), ["installer HQ_NO_SETUP=1 HQ_REF=beta", "hq start --no-open --port 4399",
                                      "hq float start --port 4399"])
        stop_pill.assert_called_once()
        self.stop.assert_called_once()
        self.assertFalse((self.data / "hq.sqlite3").exists())

    def test_already_up_to_date_restarts_nothing(self):
        self.stamp(commit=A, ref="main")
        code, out, _ = self.update(self.installer(A))
        self.assertEqual(code, 0)
        self.assertIn("Already up to date: aaaaaaa (main)", out)
        self.stop.assert_not_called()
        self.assertEqual(self.log(), ["installer HQ_NO_SETUP=1 HQ_REF=main"])

    def test_hq_ref_picks_another_branch(self):
        self.stamp(commit=A, ref="main")
        with patch.dict(os.environ, {"HQ_REF": "beta"}):
            _, _, download = self.update(self.installer(B))
        download.assert_called_once_with("https://raw.githubusercontent.com/dhruvyad/HQ/beta/install.sh")
        self.assertEqual(self.log()[0], "installer HQ_NO_SETUP=1 HQ_REF=beta")

    def test_a_failed_install_restarts_nothing_and_shows_why(self):
        self.stamp(commit=A, ref="main")
        code, out, _ = self.update(self.installer(B, code=3, record=False))
        self.assertIsInstance(code, ValueError)
        self.assertIn("nothing was restarted", str(code))
        self.assertIn("boom", out)
        self.stop.assert_not_called()

    def test_an_installer_from_before_stamps_still_updates_and_restarts(self):
        code, out, _ = self.update(self.installer(B, record=False))
        self.assertEqual(code, 0)
        self.assertIn("Updated HQ: an unrecorded build -> a build it did not record (main)", out)
        self.assertIn("hq start --no-open --port 4399", self.log())

    def test_nothing_running_needs_no_restart(self):
        self.server.return_value = None
        self.stamp(commit=A, ref="main")
        code, out, _ = self.update(self.installer(B))
        self.assertEqual(code, 0)
        self.assertIn("Nothing was running", out)
        self.stop.assert_not_called()

    def test_a_clone_install_reruns_that_clones_installer(self):
        clone = self.root / "clone"
        clone.mkdir()
        (clone / "install.sh").write_text(self.installer(B))
        self.stamp(commit=A, ref="main", source=str(clone))
        code, out, download = self.update(None)
        self.assertEqual(code, 0)
        download.assert_not_called()
        self.assertIn("from your clone", out)
        self.assertIn("Updated HQ: aaaaaaa -> bbbbbbb", out)

    def test_a_clone_that_is_gone_says_how_to_install_again(self):
        self.stamp(commit=A, source=str(self.root / "gone"))
        code, _, _ = self.update(None)
        self.assertIn(version.INSTALL_LINE, str(code))

    def test_from_a_checkout_it_points_at_git_pull(self):
        with patch.object(version, "checkout", return_value=ROOT):
            code, _, download = self.update(None)
        self.assertIn("git pull", str(code))
        download.assert_not_called()

    def test_cli_update_alone_updates_hq_and_with_an_id_updates_an_item(self):
        def main(*args):
            err = io.StringIO()
            with patch("sys.argv", ["hq", *args]), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit:
                cli.main()
            return exit.exception.code, err.getvalue()
        with patch.object(version, "update", return_value=0) as update:
            self.assertEqual(main("update")[0], 0)
            update.assert_called_once_with(cli.running, cli.stop_service)
        self.assertIn("needs --session", main("update", "item-1")[1])
        self.assertIn("Say which item", main("update", "--owner", "human")[1])
        self.assertFalse((self.data / "hq.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()

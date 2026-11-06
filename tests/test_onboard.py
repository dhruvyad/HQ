import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hq import onboard
from hq.cli import parser
from hq.connections import install_hooks, is_hq_hook

CHECKOUT = "/Users/someone/code/HQ/bin/hq"


def hook(command):
    return {"type": "command", "command": command, "timeout": 5}


class InstallScriptTests(unittest.TestCase):
    def test_no_variable_runs_into_a_non_ascii_character(self):
        # macOS's sh, in a UTF-8 locale, reads "$source…" as one name and stops the install.
        import re
        script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
        self.assertEqual(re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", script), [])


class HookOwnershipTests(unittest.TestCase):
    def test_recognizes_hq_wherever_it_was_installed_from(self):
        self.assertTrue(is_hq_hook(hook(f"/usr/bin/python3 {CHECKOUT} hook claude-code"), "claude-code"))
        self.assertTrue(is_hq_hook(hook("/home/x/.local/share/uv/tools/hq-local/bin/python -m hq hook codex"), "codex"))
        self.assertTrue(is_hq_hook(hook("/usr/bin/python3 '/Users/some one/HQ/bin/hq' hook claude-code"), "claude-code"))

    def test_leaves_everything_else_alone(self):
        for command in ["existing-hook", "hq hook claude-code", "/opt/bin/notify hook claude-code",
                        f"/usr/bin/python3 {CHECKOUT} statusline", f"/usr/bin/python3 {CHECKOUT} hook codex", "'unbalanced"]:
            self.assertFalse(is_hq_hook(hook(command), "claude-code"), command)
        self.assertFalse(is_hq_hook({"type": "prompt", "prompt": "x"}, "claude-code"))
        self.assertFalse(is_hq_hook("not a hook", "claude-code"))


class HomeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        for target in ["hq.connections.Path.home", "hq.onboard.Path.home"]:
            patcher = patch(target, return_value=self.home)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings = self.home / ".claude/settings.json"
        self.settings.parent.mkdir()

    def write(self, data):
        self.settings.write_text(json.dumps(data))

    def read(self):
        return json.loads(self.settings.read_text())


class HookRemovalTests(HomeTest):
    def test_install_repoints_a_moved_hq_instead_of_running_both(self):
        self.write({"hooks": {"Stop": [{"hooks": [hook("mine"), hook(f"/usr/bin/python3 {CHECKOUT} hook claude-code")]}]}})
        self.assertEqual(install_hooks("claude-code")["hooks"], "configured")
        stop = self.read()["hooks"]["Stop"]
        commands = [h["command"] for group in stop for h in group["hooks"]]
        self.assertEqual(len([c for c in commands if c.endswith("hook claude-code")]), 1)
        self.assertIn("mine", commands)
        self.assertNotIn(CHECKOUT, " ".join(commands))

    def test_undo_removes_only_hq_and_keeps_yours(self):
        self.write({"permissions": {"allow": ["Read"]},
                    "hooks": {"Stop": [{"hooks": [hook("mine")]}],
                              "PreToolUse": [{"matcher": "Bash", "hooks": [hook("guard")]}]}})
        install_hooks("claude-code")
        result = onboard.remove_hooks("claude-code")
        self.assertEqual(result["hooks"], "removed 6")
        after = self.read()
        self.assertEqual(after["permissions"], {"allow": ["Read"]})
        self.assertEqual(after["hooks"], {"Stop": [{"hooks": [hook("mine")]}],
                                          "PreToolUse": [{"matcher": "Bash", "hooks": [hook("guard")]}]})
        self.assertEqual(onboard.remove_hooks("claude-code")["hooks"], "none")

    def test_undo_keeps_a_group_that_also_holds_your_hook(self):
        ours = f"/usr/bin/python3 {CHECKOUT} hook claude-code"
        self.write({"hooks": {"Stop": [{"matcher": "", "hooks": [hook("mine"), hook(ours)]}]}})
        onboard.remove_hooks("claude-code")
        self.assertEqual(self.read(), {"hooks": {"Stop": [{"matcher": "", "hooks": [hook("mine")]}]}})

    def test_undo_drops_hooks_it_emptied(self):
        install_hooks("claude-code")
        onboard.remove_hooks("claude-code")
        self.assertEqual(self.read(), {})

    def test_undo_without_a_settings_file_changes_nothing(self):
        self.assertEqual(onboard.remove_hooks("codex")["hooks"], "none")
        self.assertFalse((self.home / ".codex").exists())


class SetupTests(HomeTest):
    def setUp(self):
        super().setUp()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.log = self.home / "calls.log"
        path = patch.dict(os.environ, {"PATH": str(self.bin)})
        path.start()
        self.addCleanup(path.stop)
        # Not on a Mac, so nothing here can touch launchd or build the pill.
        platform = patch.object(onboard, "sys", SimpleNamespace(platform="linux"))
        platform.start()
        self.addCleanup(platform.stop)

    def agent(self, name):
        script = self.bin / name
        script.write_text(f"#!/bin/sh\necho {name} \"$@\" >> {self.log}\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def run_setup(self, yes=True, answer=None, trust=lambda: None):
        installed = {"claude-code": "configured", "codex": "unchanged"}
        install = Mock(side_effect=lambda agent: {"clients": [{
            "client": agent, "status": installed[agent],
            "message": "" if agent == "claude-code" else "MCP server hq already exists", "hooks": "configured"}]})
        launch = Mock()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(onboard, "ask", return_value=answer) as ask:
            code = onboard.setup(4399, yes=yes, install=install, launch=launch, trust=trust)
        return code, out.getvalue(), install, launch, ask

    def test_shows_the_plan_for_the_agents_you_have_before_changing_anything(self):
        self.agent("claude")
        code, out, install, launch, ask = self.run_setup(yes=False, answer=False)
        self.assertEqual(code, 1)
        self.assertIn("Connect Claude Code", out)
        self.assertNotIn("Codex", out)
        self.assertIn("http://127.0.0.1:4399", out)
        self.assertIn("Nothing changed", out)
        ask.assert_called_once()
        install.assert_not_called()
        launch.assert_not_called()

    def test_connects_each_agent_and_opens_the_dashboard(self):
        self.agent("claude")
        self.agent("codex")
        code, out, install, launch, ask = self.run_setup()
        self.assertEqual(code, 0)
        ask.assert_not_called()
        self.assertEqual([c.args[0] for c in install.call_args_list], ["claude-code", "codex"])
        launch.assert_called_once_with(4399)
        self.assertIn("Claude Code: tools added", out)
        self.assertIn("Codex: tools already there", out)
        self.assertIn("type /hooks, and trust the hooks that run hq hook codex", out)
        self.assertIn("hq setup --undo", out)

    def test_trusts_codex_hooks_as_its_own_step(self):
        self.agent("codex")
        trust = Mock(return_value={"trusted": ["SessionStart", "Stop"], "off": [], "listed": 6})
        _, out, install, *_ = self.run_setup(yes=False, answer=True, trust=trust)
        self.assertIn("Trust HQ's hooks in Codex: save the approval Codex's /hooks saves, for HQ's hooks only", out)
        trust.assert_called_once_with()
        self.assertIn("✓ Codex: trusts HQ's hooks\n", out)
        self.assertIn("If the ChatGPT or Codex app is open, quit and reopen it", out)
        self.assertNotIn("type /hooks", out)
        # Nothing is trusted when you say no.
        trust.reset_mock()
        self.run_setup(yes=False, answer=False, trust=trust)
        trust.assert_not_called()

    def test_codex_hooks_already_trusted_turned_off_or_unanswered(self):
        self.agent("codex")
        _, out, *_ = self.run_setup(trust=lambda: {"trusted": [], "off": [], "listed": 6})
        self.assertIn("✓ Codex: trusts HQ's hooks (it already did)", out)
        self.assertNotIn("reopen it", out)
        _, out, *_ = self.run_setup(trust=lambda: {"trusted": [], "off": ["Stop"], "listed": 6})
        self.assertIn("HQ's hooks for Stop are turned off in /hooks", out)
        _, out, *_ = self.run_setup(trust=lambda: None)
        self.assertIn("✗ Codex: could not trust HQ's hooks", out)
        self.assertIn("Until you do, Codex skips them and its conversations do not show in HQ", out)

    def test_with_no_agents_it_says_so_and_still_opens_the_dashboard(self):
        code, out, install, launch, _ = self.run_setup()
        self.assertEqual(code, 0)
        self.assertIn("No Claude Code or Codex", out)
        install.assert_not_called()
        launch.assert_called_once()

    def test_asks_on_the_terminal_even_when_piped(self):
        with patch("builtins.open", side_effect=OSError("no tty")):
            with self.assertRaisesRegex(ValueError, "--yes"):
                onboard.ask("Go ahead? ")

    def test_undo_removes_tools_hooks_and_stops_hq(self):
        self.agent("claude")
        install_hooks("claude-code")
        stop = Mock(return_value={"status": "stopped"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("hq.usage.claude_login_enabled", return_value=False):
            self.assertEqual(onboard.undo(stop), 0)
        self.assertEqual(self.log.read_text().split(), ["claude", "mcp", "remove", "hq", "--scope", "user"])
        self.assertNotIn("hooks", self.read())
        stop.assert_called_once()
        self.assertIn("Your inbox is kept", out.getvalue())
        self.assertIn("uv tool uninstall hq-local", out.getvalue())

    def test_undo_removes_codex_trust_while_the_hooks_are_still_there(self):
        self.agent("codex")
        install_hooks("codex")
        seen = []
        untrust = Mock(side_effect=lambda: seen.append((self.home / ".codex/hooks.json").exists()) or 6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("hq.usage.claude_login_enabled", return_value=False):
            onboard.undo(Mock(return_value={"status": "stopped"}), untrust=untrust)
        self.assertEqual(seen, [True])
        self.assertIn("✓ Codex: no longer trusts HQ's hooks", out.getvalue())
        hooks = self.home / ".codex/hooks.json"
        self.assertNotIn("hook codex", hooks.read_text() if hooks.exists() else "")

    def test_cli_has_setup_and_undo(self):
        args = parser().parse_args(["setup", "--undo"])
        self.assertTrue(args.undo)
        args = parser().parse_args(["setup", "--yes", "--no-pill", "--port", "4399"])
        self.assertEqual((args.yes, args.no_pill, args.port), (True, True, 4399))


if __name__ == "__main__":
    unittest.main()

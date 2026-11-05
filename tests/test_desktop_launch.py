import json
import os
from pathlib import Path
import plistlib
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

from hq.desktop_launch import (desktop_status, install_launcher, launch_desktop,
                              primary_arguments, proxy_main, uninstall_launcher)
from hq.store import Store


PRIMARY = ["-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled",
           "-c", "plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true"]


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "Codex.app"
        self.binary = self.app / "Contents/MacOS/Codex"
        self.cli = self.app / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        self.binary.parent.mkdir(parents=True)
        self.cli.parent.mkdir(parents=True)
        self.binary.touch()
        self.cli.touch()
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Codex"}))
        self.env = {"HQ_HOME": str(self.root / "hq"), "CODEX_HOME": str(self.root / "codex"),
                    "HQ_DESKTOP_APP": str(self.app), "CODEX_CLI_PATH": "wrapper"}

    def tearDown(self):
        self.temp.cleanup()

    def test_helper_sandboxes_and_unknown_startup_shapes_stay_native(self):
        self.assertTrue(primary_arguments(PRIMARY))
        self.assertTrue(primary_arguments(PRIMARY + ["-c", 'chatgpt_base_url="https://example.test"']))
        for arguments in [["app-server", "--listen", "stdio://"],
                          PRIMARY + ["-c", "sandbox_mode=read-only"],
                          PRIMARY + ["-c", "approval_policy=never"],
                          PRIMARY + ["-c", "features.code_mode_host=false"],
                          PRIMARY + ["--listen", "ws://localhost:1234"],
                          PRIMARY + ["--new-option"], ["--version"]]:
            self.assertFalse(primary_arguments(arguments), arguments)
        with patch.dict(os.environ, self.env, clear=True), patch("sys.argv", ["wrapper", "app-server", "--listen", "stdio://"]), \
                patch("hq.desktop_launch.os.execve", side_effect=SystemExit) as execute, \
                patch("hq.desktop_launch.Store") as store:
            with self.assertRaises(SystemExit):
                proxy_main()
            self.assertEqual(execute.call_args.args[1], [str(self.cli), "app-server", "--listen", "stdio://"])
            self.assertEqual(execute.call_args.args[2]["CODEX_CLI_PATH"], str(self.cli))
            store.assert_not_called()

    def test_primary_keeps_native_arguments_and_reaps_owned_runtime_on_disconnect(self):
        directory = self.root / "runtime"
        directory.mkdir()
        endpoint = directory / "runtime.sock"
        child = Mock(pid=123456)
        def start(*args, **kwargs):
            endpoint.touch()
            return child
        with patch.dict(os.environ, self.env, clear=True), patch("sys.argv", ["wrapper", *PRIMARY]), \
                patch("hq.desktop_launch.runtime_directory", return_value=directory), \
                patch("hq.desktop_launch.subprocess.Popen", side_effect=start) as popen, \
                patch("hq.desktop_launch.bridge", side_effect=EOFError) as bridge, \
                patch("hq.desktop_launch.os.killpg") as kill:
            before = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(EOFError):
                proxy_main()
            command = popen.call_args.args[0]
            self.assertEqual(command[:-2], [str(self.cli), *PRIMARY])
            self.assertEqual(command[-2:], ["--listen", "unix://" + str(endpoint)])
            self.assertEqual(popen.call_args.kwargs["env"]["CODEX_CLI_PATH"], str(self.cli))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            bridge.assert_called_once_with(endpoint, control_path=directory / "desktop.sock")
            kill.assert_called_once_with(child.pid, signal.SIGTERM)
            child.wait.assert_called_once_with(timeout=5)
            self.assertEqual(signal.getsignal(signal.SIGTERM), before)
            self.assertFalse(endpoint.exists())
            self.assertFalse((directory / "runtime.json").exists())
            self.assertEqual(Store().codex_endpoints(), [str(endpoint)])

    def test_duplicate_primary_never_starts_second_owner(self):
        with patch.dict(os.environ, self.env, clear=True), patch("sys.argv", ["wrapper", *PRIMARY]), \
                patch("hq.desktop_launch.fcntl.flock", side_effect=BlockingIOError), \
                patch("hq.desktop_launch.subprocess.Popen") as start:
            with self.assertRaisesRegex(RuntimeError, "second owner"):
                proxy_main()
            start.assert_not_called()

    def test_launcher_refuses_open_app_and_custom_runtime_overrides(self):
        env = {k: v for k, v in self.env.items() if k != "CODEX_CLI_PATH"}
        with patch("hq.desktop_launch.desktop_running", return_value=True), \
                patch("hq.desktop_launch.subprocess.Popen") as start:
            with self.assertRaisesRegex(ValueError, "already open"):
                launch_desktop(self.app, environment=env, start_hq=False)
            start.assert_not_called()
        with self.assertRaisesRegex(ValueError, "overrides"):
            launch_desktop(self.app, environment=self.env, start_hq=False)

    def test_normal_launch_preserves_history_home_and_scopes_wrapper_to_app(self):
        env = {k: v for k, v in self.env.items() if k != "CODEX_CLI_PATH"}
        with patch.dict(os.environ, env, clear=True), \
                patch("hq.desktop_launch.desktop_running", return_value=False), \
                patch("hq.desktop_launch.subprocess.Popen", return_value=Mock(pid=123)) as start:
            result = launch_desktop(self.app, environment=env, start_hq=False)
            self.assertTrue(result["started"])
            self.assertEqual(start.call_args.args[0], [str(self.binary)])
            native_env = start.call_args.kwargs["env"]
            self.assertEqual(native_env["CODEX_HOME"], env["CODEX_HOME"])
            self.assertNotIn("CODEX_ELECTRON_USER_DATA_PATH", native_env)
            self.assertTrue(Path(native_env["CODEX_CLI_PATH"]).is_file())
            self.assertNotIn("CODEX_CLI_PATH", os.environ)

    def test_install_is_reversible_and_never_replaces_unmanaged_app(self):
        target = self.root / "Codex + HQ.app"
        result = install_launcher(self.app, target)
        self.assertFalse(result["connected"])
        script = target / "Contents/MacOS/codex-hq"
        self.assertIn(repr(str(self.app.resolve())), script.read_text())
        self.assertTrue(os.access(script, os.X_OK))
        before = script.read_bytes()
        install_launcher(self.app, target)
        self.assertEqual(before, script.read_bytes())
        uninstall_launcher(target)
        self.assertFalse(target.exists())
        target.mkdir()
        with self.assertRaises(ValueError):
            install_launcher(self.app, target)
        with self.assertRaises(ValueError):
            uninstall_launcher(target)

    def test_status_requires_live_handshake_and_exact_runtime_inventory(self):
        directory = self.root / "runtime"
        directory.mkdir()
        with patch.dict(os.environ, self.env, clear=True), \
                patch("hq.desktop_launch.runtime_directory", return_value=directory):
            self.assertFalse(desktop_status(self.app)["runtime_connected"])
            (directory / "runtime.json").write_text(json.dumps({"socket": str(directory / "runtime.sock")}))
            with patch("hq.adapters.CodexClient", side_effect=OSError):
                self.assertFalse(desktop_status(self.app)["runtime_connected"])
            client = Mock()
            with patch("hq.adapters.CodexClient", return_value=client), \
                    patch("hq.adapters.sync_codex", return_value={"connected": 1, "session_ids": ["codex:exact"]}):
                result = desktop_status(self.app)
                self.assertTrue(result["runtime_connected"])
                self.assertEqual(result["session_ids"], ["codex:exact"])
                client.close.assert_called_once()

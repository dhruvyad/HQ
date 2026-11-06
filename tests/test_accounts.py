"""Several Claude and Codex accounts, switched the way /login does. Every test runs against a
fake `security`, fake processes and a temporary home: nothing real is read or written."""
import base64
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from hq import accounts as acc
from hq.accounts import AccountError, Accounts, Keychain, Places, Vault, claude_item, key_for
from hq.store import Store

USER = "tester"
SECRETS = []  # every token a test invents, for the leak checks


def secret(text):
    SECRETS.append(text)
    return text


def done(code=0, out=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr="")


class FakeSecurity:
    """`/usr/bin/security` with an in-memory Keychain."""

    def __init__(self):
        self.items, self.calls, self.stdin = {}, [], []
        self.fail, self.corrupt = set(), set()

    def __call__(self, argv, input=None, **_):
        self.calls.append(list(argv))
        assert argv[0] == "/usr/bin/security"
        args = argv[1:]
        if args == ["-i"]:
            self.stdin.append(input)
            assert len(input) <= 4096, "security -i would split this line"
            words = shlex.split(input)
            assert words[:2] == ["add-generic-password", "-U"]
            account, service = words[words.index("-a") + 1], words[words.index("-s") + 1]
            if service in self.fail:
                return done(1)
            value = bytes.fromhex(words[words.index("-X") + 1]).decode()
            self.items[(account, service)] = value + ("x" if service in self.corrupt else "")
            return done()
        account, service = args[args.index("-a") + 1], args[args.index("-s") + 1]
        if args[0] == "find-generic-password":
            return done(0, self.items[(account, service)] + "\n") if (account, service) in self.items else done(44)
        if args[0] == "delete-generic-password":
            return done(0) if self.items.pop((account, service), None) is not None else done(44)
        return done(1)


class FakeProcess:
    def __init__(self, code=None):
        self.code, self.pid, self.killed = code, 99999, False

    def poll(self):
        return self.code

    def kill(self):
        self.killed, self.code = True, -9


def jwt(claims):
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.{secret('sig' + str(len(SECRETS)))}"


def claude_login(name, expires_in=3600):
    return {"accessToken": secret(f"sk-ant-oat01-{name}-ACCESS"), "refreshToken": secret(f"sk-ant-ort01-{name}-REFRESH"),
            "expiresAt": (time.time() + expires_in) * 1000, "refreshTokenExpiresAt": (time.time() + 30 * 86400) * 1000,
            "scopes": ["user:inference"], "subscriptionType": "max"}


def profile(name):
    return {"accountUuid": f"uuid-{name}", "organizationUuid": f"org-{name}", "emailAddress": f"{name}@example.com",
            "organizationName": f"{name.title()} Org", "displayName": name.title()}


def codex_auth(name, expires_in=3600, last_refresh=None):
    id_token = jwt({"email": f"{name}@example.com", "sub": f"sub-{name}",
                    "https://api.openai.com/auth": {"chatgpt_user_id": f"user-{name}", "chatgpt_account_id": f"acct-{name}",
                                                    "chatgpt_plan_type": "plus"}})
    access = jwt({"exp": int(time.time() + expires_in)})
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": id_token, "access_token": access, "refresh_token": secret(f"codex-{name}-REFRESH"),
                       "account_id": f"acct-{name}"},
            "last_refresh": last_refresh or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


class Home(unittest.TestCase):
    """A temporary home with Alice signed in to Claude Code and Work signed in to Codex."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.places = Places(home=self.home, env={})
        self.sec = FakeSecurity()
        self.keychain = Keychain(run=self.sec, user=USER)
        self.store = Store(self.home / "hq" / "hq.sqlite3")
        self.now = 1000.0
        self.logs, self.ran, self.spawned = [], [], []
        self.ps, self.status = "", lambda: None
        self.spawn_effect = lambda argv, env: FakeProcess()
        self.responses = {}
        self.requests = []
        (self.home / ".claude").mkdir()
        self.item = {"claudeAiOauth": claude_login("alice"), "organizationUuid": "org-alice",
                     "mcpOAuth": {"github": {"accessToken": secret("MCP-SERVER-SECRET")}},
                     "pluginSecrets": {"weather": secret("PLUGIN-SECRET")}}
        self.sec.items[(USER, acc.CLAUDE_ITEM)] = json.dumps(self.item)
        config = {"oauthAccount": profile("alice"), "numStartups": 12, "projects": {"/x": {"allowedTools": []}},
                  "modelAccessCache": {"a": 1}, "cachedUsageUtilization": {"b": 2}}
        self.places.claude_json.write_text(json.dumps(config))
        os.chmod(self.places.claude_json, 0o644)
        self.places.codex_home.mkdir()
        self.work = codex_auth("work")
        (self.places.codex_home / "auth.json").write_text(json.dumps(self.work))
        self.accounts = self.make()

    def make(self, **overrides):
        def run(argv, **_):
            self.ran.append(list(argv))
            if argv[0] == "/bin/ps":
                return done(0, self.ps)
            if argv[1:] == ["auth", "status", "--json"]:
                out = self.status()
                return done(0, out) if out is not None else done(1)
            if argv[0] == "/usr/bin/osascript":
                self.on_terminal()
                return done(0)
            return done(1)

        def spawn(argv, env=None, **_):
            self.spawned.append((list(argv), dict(env or {})))
            return self.spawn_effect(argv, env)

        def open_url(request, timeout=None):
            self.requests.append(request)
            answer = self.responses.get(request.full_url)
            if isinstance(answer, Exception):
                raise answer
            return io.BytesIO(json.dumps(answer).encode())

        def sleep(seconds):
            self.now += seconds
            time.sleep(0.0005)

        options = dict(places=self.places, keychain=self.keychain, run=run, spawn=spawn, open_url=open_url,
                       clock=lambda: self.now, sleep=sleep, rand=lambda low, high: low,
                       which=lambda name: f"/fake/bin/{name}", temp_root=str(self.home), log=self.logs.append,
                       stop=lambda process: process.kill())
        options.update(overrides)
        return Accounts(self.store, **options)

    def on_terminal(self):
        pass

    def item_now(self):
        return json.loads(self.sec.items[(USER, acc.CLAUDE_ITEM)])

    def config_now(self):
        return json.loads(self.places.claude_json.read_text())

    def save_bob(self, expires_in=3600):
        bob = claude_login("bob", expires_in)
        return self.accounts.save_claude({"claudeAiOauth": bob, "organizationUuid": "org-bob"}, profile("bob")), bob

    def save_side(self, **kw):
        side = codex_auth("side", **kw)
        return self.accounts.save_codex(json.dumps(side), side), side


class KeychainTests(Home):
    def test_secrets_go_on_stdin_never_in_argv(self):
        vault = Vault(self.keychain)
        vault.put("claude", "k1", json.dumps({"token": secret("ONLY-ON-STDIN")}))
        self.assertEqual(json.loads(vault.get("claude", "k1"))["token"], "ONLY-ON-STDIN")
        argv = json.dumps(self.sec.calls)
        self.assertNotIn("ONLY-ON-STDIN", argv)
        self.assertNotIn("ONLY-ON-STDIN".encode().hex(), argv)
        self.assertTrue(all(len(line) <= acc.LINE_LIMIT for line in self.sec.stdin))

    def test_a_long_login_is_saved_in_chunks_and_read_back_whole(self):
        vault = Vault(self.keychain)
        big = json.dumps({"blob": "z" * 5000})
        vault.put("codex", "k2", big)
        chunks = [s for (_, s) in self.sec.items if s.startswith("HQ account codex k2 #1.")]
        self.assertGreater(len(chunks), 3)
        self.assertEqual(vault.get("codex", "k2"), big)
        vault.put("codex", "k2", json.dumps({"blob": "y"}))  # a new generation replaces the old chunks
        self.assertEqual([s for (_, s) in self.sec.items if " #1." in s and "k2" in s], [])
        vault.remove("codex", "k2")
        self.assertEqual([s for (_, s) in self.sec.items if "k2" in s], [])

    def test_a_line_security_would_split_is_never_sent(self):
        before = len(self.sec.stdin)
        with self.assertRaisesRegex(AccountError, "too large"):
            self.keychain.write("svc", "hq", "q" * 3000)
        self.assertEqual(len(self.sec.stdin), before)

    def test_a_write_that_reads_back_wrong_is_an_error_that_shows_no_secret(self):
        self.sec.corrupt.add("svc")
        with self.assertRaises(AccountError) as raised:
            self.keychain.write("svc", "hq", secret("READ-BACK-SECRET"))
        self.assertNotIn("READ-BACK-SECRET", str(raised.exception))

    def test_a_damaged_saved_login_asks_for_a_fresh_sign_in(self):
        vault = Vault(self.keychain)
        vault.put("claude", "k3", "{}")
        self.sec.items[("hq", "HQ account claude k3 #1.0")] = "00"
        with self.assertRaisesRegex(AccountError, "add the account again"):
            vault.get("claude", "k3")

    def test_claude_code_names_its_item_after_a_custom_config_folder(self):
        self.assertEqual(claude_item(None), "Claude Code-credentials")
        self.assertRegex(claude_item("/tmp/x"), r"^Claude Code-credentials-[0-9a-f]{8}$")
        custom = Places(home=self.home, env={"CLAUDE_CONFIG_DIR": "/tmp/x", "CODEX_HOME": "/tmp/c"})
        self.assertEqual((custom.claude_item, custom.claude_json, custom.codex_home),
                         (claude_item("/tmp/x"), Path("/tmp/x/.claude.json"), Path("/tmp/c")))


class ClaudeSwitchTests(Home):
    def setUp(self):
        super().setUp()
        self.bob_key, self.bob = self.save_bob()
        self.status = lambda: json.dumps({"loggedIn": True, "account": {"email": self.config_now()["oauthAccount"]["emailAddress"]}})

    def test_switching_replaces_only_the_accounts_own_keys(self):
        result = self.accounts.use("claude", self.bob_key)
        item, config = self.item_now(), self.config_now()
        self.assertEqual(item["claudeAiOauth"]["accessToken"], self.bob["accessToken"])
        self.assertEqual(item["organizationUuid"], "org-bob")
        self.assertEqual(item["mcpOAuth"], self.item["mcpOAuth"])  # MCP logins untouched
        self.assertEqual(item["pluginSecrets"], self.item["pluginSecrets"])
        self.assertEqual(config["oauthAccount"]["emailAddress"], "bob@example.com")
        self.assertEqual((config["numStartups"], config["projects"]), (12, {"/x": {"allowedTools": []}}))
        self.assertNotIn("modelAccessCache", config)
        self.assertNotIn("cachedUsageUtilization", config)
        self.assertEqual(stat.S_IMODE(self.places.claude_json.stat().st_mode), 0o644)  # its own mode kept
        self.assertEqual((result["verified"], result["note"]), (True, acc.CLAUDE_NOTE))
        self.assertTrue(result["account"]["active"])
        # Alice's freshest login is kept, so switching back works.
        alice = key_for("claude", "uuid-alice:org-alice")
        kept = json.loads(Vault(self.keychain).get("claude", alice))
        self.assertEqual(kept["login"]["claudeAiOauth"]["refreshToken"], self.item["claudeAiOauth"]["refreshToken"])
        self.assertNotIn("mcpOAuth", kept["login"])
        back = self.accounts.use("claude", alice)
        self.assertEqual(self.item_now()["claudeAiOauth"]["accessToken"], self.item["claudeAiOauth"]["accessToken"])
        self.assertEqual(self.item_now()["mcpOAuth"], self.item["mcpOAuth"])
        self.assertTrue(back["verified"])
        self.assertFalse(any(self.home.glob(".claude/*.lock")) or Path(str(self.home / ".claude") + ".lock").exists())

    def test_a_held_lock_never_blocks_the_switch(self):
        held = self.home / ".claude" / ".storage-write.lock"
        held.mkdir()
        self.accounts.use("claude", self.bob_key)
        self.assertEqual(self.item_now()["claudeAiOauth"]["accessToken"], self.bob["accessToken"])
        self.assertTrue(held.exists())  # someone else's lock: waited on briefly, never taken away
        self.assertTrue(any(".storage-write.lock" in line for line in self.logs))

    def test_a_refresh_under_way_still_switches_and_marks_the_outgoing_copy(self):
        (self.home / ".claude" / ".oauth_refresh.lock").mkdir()
        self.accounts.use("claude", self.bob_key)
        alice = key_for("claude", "uuid-alice:org-alice")
        listed = {a["key"]: a for a in self.accounts.list()["claude"]["accounts"]}
        self.assertEqual(listed[alice]["state"], "needs sign-in")
        self.assertTrue(listed[self.bob_key]["active"])

    def test_a_login_about_to_renew_still_switches_and_marks_it(self):
        self.item["claudeAiOauth"]["expiresAt"] = (time.time() + 60) * 1000
        self.sec.items[(USER, acc.CLAUDE_ITEM)] = json.dumps(self.item)
        self.accounts.use("claude", self.bob_key)
        alice = key_for("claude", "uuid-alice:org-alice")
        self.assertEqual(self.accounts.row("claude", alice)["stale"], 1)
        self.accounts.use("claude", alice)  # switching to it again clears the mark
        self.assertEqual(self.accounts.row("claude", alice)["stale"], 0)

    def test_a_failed_write_leaves_the_login_unchanged(self):
        before_item, before_config = dict(self.item_now()), self.places.claude_json.read_text()
        self.sec.fail.add(acc.CLAUDE_ITEM)
        with self.assertRaisesRegex(AccountError, "unchanged"):
            self.accounts.use("claude", self.bob_key)
        self.sec.fail.clear()
        self.assertEqual(self.item_now(), before_item)
        self.assertEqual(self.places.claude_json.read_text(), before_config)

    def test_a_failed_config_write_puts_the_login_back(self):
        before_item, before_config = dict(self.item_now()), self.places.claude_json.read_text()
        real = acc.write_atomic
        calls = []

        def failing(path, text, mode=0o600):
            calls.append(path)
            if len(calls) == 1:
                raise OSError("disk full")
            return real(path, text, mode)

        with patch.object(acc, "write_atomic", failing):
            with self.assertRaisesRegex(AccountError, "unchanged"):
                self.accounts.use("claude", self.bob_key)
        self.assertEqual(self.item_now(), before_item)
        self.assertEqual(self.places.claude_json.read_text(), before_config)

    def test_claude_code_saying_otherwise_puts_the_old_login_back(self):
        before_item = dict(self.item_now())
        self.status = lambda: json.dumps({"email": "alice@example.com"})
        with self.assertRaisesRegex(AccountError, "put the old one back"):
            self.accounts.use("claude", self.bob_key)
        self.assertEqual(self.item_now(), before_item)
        self.assertEqual(self.config_now()["oauthAccount"]["emailAddress"], "alice@example.com")

    def test_without_claude_code_to_ask_the_switch_stands_unverified(self):
        self.accounts = self.make(which=lambda name: None)
        result = self.accounts.use("claude", self.bob_key)
        self.assertIsNone(result["verified"])
        self.assertEqual(self.config_now()["oauthAccount"]["emailAddress"], "bob@example.com")

    def test_switching_to_the_signed_in_account_does_nothing(self):
        alice = self.accounts.save_current("claude")
        writes = len(self.sec.stdin)
        self.assertTrue(self.accounts.use("claude", alice)["already"])
        self.assertEqual(len(self.sec.stdin), writes)

    def test_an_unknown_account_is_refused(self):
        with self.assertRaisesRegex(AccountError, "isn't saved"):
            self.accounts.use("claude", "000000000000")

    def test_open_sessions_are_told_through_the_usage_reader(self):
        told = []
        self.accounts.on_switch = told.append
        self.accounts.use("claude", self.bob_key)
        self.assertIn("claude", told)


class CodexSwitchTests(Home):
    def setUp(self):
        super().setUp()
        self.side_key, self.side = self.save_side()

    def test_switching_replaces_auth_json_atomically_and_keeps_the_old_one(self):
        result = self.accounts.use("codex", self.side_key)
        path = self.places.codex_home / "auth.json"
        self.assertEqual(json.loads(path.read_text())["tokens"]["refresh_token"], self.side["tokens"]["refresh_token"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(list(self.places.codex_home.glob("*.hq-tmp")))
        self.assertIsNone(result["warning"])
        work = key_for("codex", "user-work:acct-work")
        self.assertEqual(json.loads(Vault(self.keychain).get("codex", work)), self.work)

    def test_open_codex_sessions_get_a_restart_notice_with_their_count(self):
        self.ps = "  101   1 /Applications/Codex.app/Contents/MacOS/Codex\n  102 101 codex\n  103   1 /bin/zsh\n"
        result = self.accounts.use("codex", self.side_key)
        self.assertEqual(result["warning"], "Switched to side@example.com. Restart the 2 open Codex sessions to use it.")
        self.ps = "  102   1 codex\n"
        work = key_for("codex", "user-work:acct-work")
        self.assertEqual(self.accounts.use("codex", work)["warning"],
                         "Switched to work@example.com. Restart the open Codex session to use it.")

    def test_a_login_about_to_renew_still_switches_and_is_marked(self):
        self.work = codex_auth("work", expires_in=60)
        (self.places.codex_home / "auth.json").write_text(json.dumps(self.work))
        self.accounts.use("codex", self.side_key)
        self.assertEqual(self.accounts.row("codex", key_for("codex", "user-work:acct-work"))["stale"], 1)

    def test_a_long_unrefreshed_login_still_switches_and_is_marked(self):
        self.work = codex_auth("work", last_refresh="2020-01-01T00:00:00Z")
        (self.places.codex_home / "auth.json").write_text(json.dumps(self.work))
        self.accounts.use("codex", self.side_key)
        self.assertEqual(self.accounts.row("codex", key_for("codex", "user-work:acct-work"))["stale"], 1)

    def test_a_held_auth_lock_never_blocks_the_switch(self):
        lock = self.places.codex_home / "auth.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.accounts.use("codex", self.side_key)
        self.assertEqual(json.loads((self.places.codex_home / "auth.json").read_text())["tokens"]["account_id"], "acct-side")
        self.assertTrue(any("auth.lock" in line for line in self.logs))

    def test_keychain_mode_codex_is_refused_because_its_login_is_not_in_auth_json(self):
        (self.places.codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        with self.assertRaisesRegex(AccountError, "Keychain"):
            self.accounts.use("codex", self.side_key)


class AddTests(Home):
    def finish(self, job):
        for _ in range(4000):
            current = self.accounts.job(job["id"])
            if current["state"] not in {"starting", "waiting"}:
                return current
            time.sleep(0.001)
        self.fail("the sign-in never finished")

    def claude_signs_in(self, argv, env):
        folder = Path(env["CLAUDE_CONFIG_DIR"])
        self.sec.items[(USER, claude_item(str(folder)))] = json.dumps({"claudeAiOauth": claude_login("carol")})
        (folder / ".claude.json").write_text(json.dumps({"oauthAccount": profile("carol")}))
        return FakeProcess()

    def test_adding_claude_signs_in_aside_saves_both_and_tidies_up(self):
        self.spawn_effect = self.claude_signs_in
        job = self.finish(self.accounts.add("claude"))
        self.assertEqual((job["state"], job["account"]["email"]), ("added", "carol@example.com"))
        argv, env = self.spawned[0]
        self.assertEqual(argv, ["/fake/bin/claude", "auth", "login", "--claudeai"])
        folder = Path(env["CLAUDE_CONFIG_DIR"])
        self.assertNotEqual(folder, self.home / ".claude")
        self.assertFalse(folder.exists())
        self.assertNotIn((USER, claude_item(str(folder))), self.sec.items)  # the temporary login is gone
        emails = {a["email"] for a in self.accounts.list()["claude"]["accounts"]}
        self.assertEqual(emails, {"alice@example.com", "carol@example.com"})  # the current one saved too
        self.assertEqual(self.config_now()["oauthAccount"]["emailAddress"], "alice@example.com")  # adding never switches
        self.assertFalse(any("logout" in " ".join(call) for call in self.ran + [a for a, _ in self.spawned]))

    def test_a_sign_in_that_wants_a_terminal_finishes_in_one(self):
        def quits(argv, env):
            self.folder = Path(env["CLAUDE_CONFIG_DIR"])
            return FakeProcess(code=1)
        self.spawn_effect = quits
        self.on_terminal = lambda: self.claude_signs_in(None, {"CLAUDE_CONFIG_DIR": str(self.folder)})
        job = self.finish(self.accounts.add("claude"))
        self.assertEqual((job["state"], job["terminal"]), ("added", True))
        script = next(call for call in self.ran if call[0] == "/usr/bin/osascript")
        self.assertIn("CLAUDE_CONFIG_DIR=", " ".join(script))

    def test_adding_codex_reads_the_new_auth_json_and_removes_it(self):
        def signs_in(argv, env):
            (Path(env["CODEX_HOME"]) / "auth.json").write_text(json.dumps(codex_auth("extra")))
            return FakeProcess()
        self.spawn_effect = signs_in
        job = self.finish(self.accounts.add("codex"))
        self.assertEqual((job["state"], job["account"]["email"]), ("added", "extra@example.com"))
        argv, env = self.spawned[0]
        self.assertEqual(argv, ["/fake/bin/codex", "login"])
        self.assertFalse(Path(env["CODEX_HOME"]).exists())
        self.assertEqual(json.loads((self.places.codex_home / "auth.json").read_text()), self.work)

    def test_cancel_stops_the_sign_in_and_tidies_up(self):
        process = FakeProcess()
        self.spawn_effect = lambda argv, env: process
        job = self.accounts.add("codex")
        self.accounts.cancel(job["id"])
        finished = self.finish(job)
        self.assertEqual(finished["state"], "cancelled")
        self.assertTrue(process.killed)
        self.assertFalse(Path(self.spawned[0][1]["CODEX_HOME"]).exists())

    def test_a_sign_in_that_stops_early_fails_plainly(self):
        self.spawn_effect = lambda argv, env: FakeProcess(code=1)
        job = self.finish(self.accounts.add("codex"))
        self.assertEqual((job["state"], job["message"]), ("failed", "The sign-in stopped before it finished"))

    def test_an_inbox_from_an_early_build_of_accounts_still_saves_a_sign_in(self):
        # The first build made the table without stale and with a profile column.
        early = self.home / "early" / "hq.sqlite3"
        early.parent.mkdir()
        with sqlite3.connect(early) as db:
            db.execute("""CREATE TABLE accounts (tool TEXT NOT NULL, key TEXT NOT NULL, identity TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT '', org TEXT NOT NULL DEFAULT '',
                profile TEXT NOT NULL DEFAULT '{}', added REAL NOT NULL, last_used REAL, expires REAL,
                refresh_expires REAL, usage TEXT, usage_as_of REAL, PRIMARY KEY(tool, key))""")
            db.execute("PRAGMA user_version=9")
        self.store = Store(early)
        self.accounts = self.make()
        self.spawn_effect = self.claude_signs_in
        job = self.finish(self.accounts.add("claude"))
        self.assertEqual((job["state"], job["account"]["email"]), ("added", "carol@example.com"))

    def test_a_sign_in_that_cannot_be_saved_says_so_and_leaves_no_login_behind(self):
        with self.store.db() as db:
            db.execute("ALTER TABLE accounts DROP COLUMN stale")
        before = set(self.sec.items)
        self.spawn_effect = self.claude_signs_in
        job = self.finish(self.accounts.add("claude"))
        self.assertEqual((job["state"], job["message"]),
                         ("failed", "HQ couldn't save that account. Restart HQ and add it again"))
        self.assertEqual(set(self.sec.items), before)  # neither the new login nor the temporary one is left

    def test_only_one_sign_in_per_tool_at_a_time(self):
        self.spawn_effect = lambda argv, env: FakeProcess()
        first = self.accounts.add("codex")
        self.assertEqual(self.accounts.add("codex")["id"], first["id"])
        self.accounts.cancel(first["id"])
        self.finish(first)


class UsageTests(Home):
    def test_a_saved_login_still_valid_reads_its_own_usage(self):
        bob_key, bob = self.save_bob()
        self.responses[acc.CLAUDE_USAGE_URL] = {"five_hour": {"utilization": 30, "resets_at": "2030-01-01T00:00:00Z"},
                                               "seven_day": {"utilization": 60, "resets_at": None}}
        self.assertEqual(self.accounts.poll(), ("claude", bob_key))
        self.assertEqual(self.requests[0].get_header("Authorization"), f"Bearer {bob['accessToken']}")
        listed = {a["key"]: a for a in self.accounts.list()["claude"]["accounts"]}
        self.assertEqual([w["left"] for w in listed[bob_key]["usage"]["windows"]], [70, 40])
        self.assertEqual(self.accounts.next_at[("claude", bob_key)], self.now + 1800)  # the idle rhythm

    def test_an_expired_saved_login_is_never_sent_anywhere(self):
        bob_key, _ = self.save_bob(expires_in=-10)
        self.accounts.poll()
        self.assertEqual(self.requests, [])
        listed = {a["key"]: a for a in self.accounts.list()["claude"]["accounts"]}
        self.assertEqual(listed[bob_key]["state"], "expired")

    def test_codex_usage_for_a_saved_login_reads_both_windows(self):
        side_key, side = self.save_side()
        self.responses[acc.CODEX_USAGE_URL] = {"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 25, "limit_window_seconds": 18000, "reset_at": 2000000000},
            "secondary_window": {"used_percent": 50, "limit_window_seconds": 604800, "reset_at": 2000000000}}}
        self.assertEqual(self.accounts.poll(), ("codex", side_key))
        self.assertEqual(self.requests[0].get_header("Chatgpt-account-id"), "acct-side")
        reading = self.accounts.public(self.accounts.row("codex", side_key), None)["usage"]
        self.assertEqual([(w["name"], w["left"]) for w in reading["windows"]], [("5-hour", 75), ("weekly", 50)])

    def test_saved_accounts_are_asked_one_at_a_time_and_spread_out(self):
        self.save_bob()
        self.save_side()
        self.responses[acc.CLAUDE_USAGE_URL] = {"five_hour": {"utilization": 1, "resets_at": None}}
        self.responses[acc.CODEX_USAGE_URL] = {"rate_limit": {"primary_window": {"used_percent": 1, "limit_window_seconds": 18000, "reset_at": 2000000000}}}
        first = self.accounts.poll()
        self.assertIsNone(self.accounts.poll())  # never two in a row
        self.now += acc.SPREAD[0]
        second = self.accounts.poll()
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.requests), 2)

    def test_a_refusal_backs_off_and_a_turned_down_login_needs_a_fresh_sign_in(self):
        bob_key, _ = self.save_bob()
        self.responses[acc.CLAUDE_USAGE_URL] = urllib.error.HTTPError(acc.CLAUDE_USAGE_URL, 429, "Too Many", {"Retry-After": "900"}, io.BytesIO())
        self.accounts.poll()
        self.assertEqual(self.accounts.next_at[("claude", bob_key)], self.now + 900)
        self.now = self.accounts.next_at[("claude", bob_key)]
        self.responses[acc.CLAUDE_USAGE_URL] = urllib.error.HTTPError(acc.CLAUDE_USAGE_URL, 401, "No", {}, io.BytesIO())
        self.accounts.poll()
        self.assertEqual(self.accounts.row("claude", bob_key)["stale"], 1)

    def test_the_usage_reader_forgets_the_last_accounts_numbers(self):
        from hq import usage
        recorded = usage.Usage()
        recorded.record("claude", {"windows": [{"name": "weekly", "left": 5, "resets_at": None}], "as_of": int(time.time())})
        cache = self.home / "claude-usage.json"
        cache.write_text("{}")
        worker = usage.UsageWorker(recorded, claude=lambda: None, codex=lambda: None, clock=lambda: 42.0, cache=cache)
        worker.login.token = "old"
        worker.forget("claude")
        self.assertEqual((worker.login.token, worker.from_login, worker.next_login), (None, None, 42.0))
        self.assertFalse(cache.exists())
        self.assertIsNone(recorded.current()["claude"])


class ManageTests(Home):
    def test_removing_drops_the_saved_login_but_never_the_signed_in_one(self):
        bob_key, _ = self.save_bob()
        alice = self.accounts.save_current("claude")
        with self.assertRaisesRegex(AccountError, "Switch to another account"):
            self.accounts.remove("claude", alice)
        self.accounts.remove("claude", bob_key)
        self.assertFalse([s for (_, s) in self.sec.items if bob_key in s])
        self.assertEqual([r["key"] for r in self.accounts.rows("claude")], [alice])

    def test_the_list_shows_the_signed_in_account_even_before_it_is_saved(self):
        listed = self.accounts.list({"claude": {"windows": [{"name": "weekly", "left": 50, "resets_at": None}], "as_of": 1}})
        claude = listed["claude"]["accounts"]
        self.assertEqual((len(claude), claude[0]["email"], claude[0]["saved"], claude[0]["active"]), (1, "alice@example.com", False, True))
        self.assertEqual(claude[0]["usage"]["windows"][0]["left"], 50)
        self.assertEqual(listed["codex"]["accounts"][0]["email"], "work@example.com")
        self.assertEqual(listed["codex"]["running"], 0)

    def test_a_switch_made_anywhere_else_tells_the_usage_reader(self):
        told = []
        self.accounts.on_switch = told.append
        self.accounts.refresh()
        config = self.config_now()
        config["oauthAccount"] = profile("dave")  # as /login in Claude Code would
        self.places.claude_json.write_text(json.dumps(config))
        summary = self.accounts.refresh()
        self.assertEqual(told, ["claude"])
        self.assertEqual(summary["claude"]["active"]["email"], "dave@example.com")


class LeakTests(Home):
    def test_no_secret_reaches_argv_logs_the_database_errors_or_answers(self):
        outputs = []
        self.status = lambda: json.dumps({"email": self.config_now()["oauthAccount"]["emailAddress"]})
        bob_key, _ = self.save_bob()
        side_key, _ = self.save_side()
        self.ps = "  1 1 codex\n"
        outputs += [self.accounts.use("claude", bob_key), self.accounts.use("codex", side_key), self.accounts.list()]
        self.sec.fail.add(acc.CLAUDE_ITEM)
        try:
            self.accounts.use("claude", key_for("claude", "uuid-alice:org-alice"))
        except AccountError as error:
            outputs.append(str(error))
        self.sec.fail.clear()

        def signs_in(argv, env):
            (Path(env["CODEX_HOME"]) / "auth.json").write_text(json.dumps(codex_auth("extra")))
            return FakeProcess()
        self.spawn_effect = signs_in
        job = self.accounts.add("codex")
        for _ in range(4000):
            if self.accounts.job(job["id"])["state"] not in {"starting", "waiting"}:
                break
            time.sleep(0.001)
        outputs.append(self.accounts.job(job["id"]))
        visible = json.dumps([outputs, self.sec.calls, self.ran, [a for a, _ in self.spawned], self.logs], default=str)
        stored = self.store.path.read_bytes()
        for token in SECRETS:
            self.assertNotIn(token, visible)
            self.assertNotIn(token.encode(), stored)
        self.assertTrue(self.sec.stdin)  # the only way any login travelled: stdin to security


class HTTPAccountTests(Home):
    def setUp(self):
        super().setUp()
        from hq.server import Server
        self.server = Server(0, self.store, accounts=self.accounts)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join()))
        self.bob_key, _ = self.save_bob()
        self.status = lambda: json.dumps({"email": self.config_now()["oauthAccount"]["emailAddress"]})

    def call(self, path, body=None, token=True):
        headers = {"X-HQ-Token": self.server.token} if token else {}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        request = urllib.request.Request(self.server.origin + path, data=data, headers=headers, method="POST" if data else "GET")
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def test_the_accounts_api(self):
        listed = self.call("/api/accounts")
        self.assertEqual({a["email"] for a in listed["claude"]["accounts"]}, {"alice@example.com", "bob@example.com"})
        used = self.call(f"/api/accounts/claude/{self.bob_key}/use", {})
        self.assertEqual((used["account"]["email"], used["verified"]), ("bob@example.com", True))
        alice = key_for("claude", "uuid-alice:org-alice")
        self.assertEqual(self.call(f"/api/accounts/claude/{alice}/remove", {})["removed"], alice)
        self.spawn_effect = lambda argv, env: FakeProcess()
        job = self.call("/api/accounts/codex/add", {})
        self.assertEqual(self.call(f"/api/accounts/jobs/{job['id']}")["id"], job["id"])
        self.assertEqual(self.call(f"/api/accounts/jobs/{job['id']}/cancel", {})["id"], job["id"])
        state = self.call("/api/state")
        self.assertIn("accounts", state)
        for path in ("/api/accounts", f"/api/accounts/jobs/{job['id']}"):
            with self.assertRaises(urllib.error.HTTPError) as refused:
                self.call(path, token=False)
            self.assertEqual(refused.exception.code, 403)
            refused.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.call("/api/accounts/claude/000000000000/use", {})
        self.assertEqual(bad.exception.code, 400)
        bad.exception.close()


class CLITests(Home):
    def test_hq_accounts_finds_an_account_by_email(self):
        from hq import cli
        bob_key, _ = self.save_bob()
        self.status = lambda: json.dumps({"email": self.config_now()["oauthAccount"]["emailAddress"]})
        args = cli.parser().parse_args(["accounts", "use", "claude", "BOB@example.com"])
        with patch("hq.accounts.Accounts", return_value=self.accounts), patch("hq.cli.Store"):
            result = cli.accounts_command(args)
        self.assertEqual(result["account"]["key"], bob_key)
        args = cli.parser().parse_args(["accounts", "remove", "codex", "nobody@example.com"])
        with patch("hq.accounts.Accounts", return_value=self.accounts), patch("hq.cli.Store"):
            with self.assertRaisesRegex(AccountError, "No single saved"):
                cli.accounts_command(args)


if __name__ == "__main__":
    unittest.main()

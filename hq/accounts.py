"""Several Claude and Codex accounts, and switching between them the way /login does.

What HQ reads and writes, and nothing more:

- Claude Code: the account-bound part of its Keychain login ("Claude Code-credentials":
  `claudeAiOauth` and the other keys /login itself replaces), and `oauthAccount` in
  ~/.claude.json together with the per-account caches /login clears. MCP-server logins,
  plugin secrets and every other key in that item are left exactly as they were. Open
  sessions re-read the item before their next request, so they follow the switch, as they
  do after /login.
- Codex: ~/.codex/auth.json, in Codex's default file mode. Open Codex sessions keep the
  account they started with until they restart, so a switch says so.
- HQ's own Keychain items, one set per saved account ("HQ account <tool> <key>").

A switch always goes through. Locks keep two writers from corrupting a login, but HQ only
waits a moment for them, as /login does not wait on other sessions. A write that fails is
put back, so nothing is left half switched.

Secrets never reach SQLite, logs, exceptions, argv, HQ items or what the API returns. HQ
never refreshes a token: each tool renews its own login once it is active again. Usage for
an account that is not signed in is read with its saved access token only while that token
is still valid, and otherwise shown as of when it was last read.
"""
from contextlib import contextmanager
import base64
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from .usage import (CLAUDE_USAGE_URL, IDLE, READ_ERRORS, open_token_url, parse_claude_login_usage, parse_codex_usage,
                    valid_reading)

SECURITY = "/usr/bin/security"
LINE_LIMIT = 4032          # `security -i` splits longer lines and would keep half a secret
CHUNK = 900                # bytes per saved chunk: hex, then hex again, still well under the line limit
TOOLS = ("claude", "codex")
NAMES = {"claude": "Claude", "codex": "Codex"}
CLAUDE_ITEM = "Claude Code-credentials"
# The keys /login removes when it signs in as someone else: these are the account's own.
CLAUDE_BOUND = ("claudeAiOauth", "organizationUuid", "trustedDeviceToken", "trustedDeviceEnrolledAfterRefusalAtMs",
                "enterpriseGateway", "designOauth", "cloudSessionsOauth")
# What /login clears in ~/.claude.json, because it belonged to the previous account.
CLAUDE_CACHES = ("modelAccessCache", "orgModelDefaultCache", "cachedUsageUtilization", "additionalModelOptionsCache",
                 "clientDataCache", "clientDataCacheSlots", "autoCompactWindowsCache")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
LOCK_WAIT = 2              # a moment, then switch anyway: /login does not wait on other sessions
RENEWING = 600             # a token this close to expiry may be renewed under us
CODEX_RENEW_AGE = 7.5 * 86400
ADD_TIMEOUT = 600
TERMINAL_FALLBACK = 15     # a sign-in that quits this fast wanted a terminal
SPREAD = (30, 90)          # never two saved accounts asked in a row
CLAUDE_NOTE = "Every open Claude session moves to it at its next message."


class AccountError(ValueError):
    """Safe to show: never carries a secret."""


def now():
    return time.time()


def claude_user():
    user = os.environ.get("USER") or ""
    if not user:
        try:
            user = getpass.getuser()
        except (OSError, KeyError):
            user = ""
    return user if re.fullmatch(r"[A-Za-z0-9._-]+", user) else "claude-code-user"


def claude_item(config_dir=None):
    """Claude Code's Keychain item: suffixed when CLAUDE_CONFIG_DIR points elsewhere."""
    if not config_dir:
        return CLAUDE_ITEM
    return f"{CLAUDE_ITEM}-{hashlib.sha256(unicodedata.normalize('NFC', config_dir).encode()).hexdigest()[:8]}"


def key_for(tool, identity):
    return hashlib.sha256(f"{tool}:{identity}".encode()).hexdigest()[:12]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def dumps(value):
    # ASCII only, so the Keychain prints it back as text, byte for byte.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def jwt_claims(token):
    """A token's claims, decoded here and never checked against anyone: only for reading
    whose login it is and when it expires."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return claims if isinstance(claims, dict) else {}
    except (AttributeError, IndexError, ValueError, TypeError):
        return {}


def iso_seconds(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def claude_plan(profile, login=None):
    """The plan's name as Claude shows it ("Ultra"), which Claude Code keeps in
    `oauthAccount`; failing that, the login's own type and tier ("Max 20x")."""
    named = profile.get("planDisplayName") if isinstance(profile, dict) else None
    if isinstance(named, str) and named.strip():
        return named.strip()
    oauth = (login or {}).get("claudeAiOauth") if isinstance(login, dict) else None
    oauth = oauth if isinstance(oauth, dict) else {}
    kind = oauth.get("subscriptionType")
    if not isinstance(kind, str) or not kind:
        return ""
    tier = re.search(r"_(\d+x)$", str(oauth.get("rateLimitTier") or ""))
    return kind.replace("_", " ").title() + (f" {tier.group(1)}" if tier else "")


def claude_who(profile, login=None):
    """Who a Claude login belongs to, from `oauthAccount`."""
    if not isinstance(profile, dict) or not isinstance(profile.get("accountUuid"), str) or not profile["accountUuid"]:
        return None
    return {"identity": f"{profile['accountUuid']}:{profile.get('organizationUuid') or ''}",
            "email": str(profile.get("emailAddress") or ""), "org": str(profile.get("organizationName") or ""),
            "plan": claude_plan(profile, login)}


def claude_times(login):
    oauth = login.get("claudeAiOauth") if isinstance(login, dict) else None
    if not isinstance(oauth, dict):
        return None, None
    ms = lambda v: v / 1000 if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
    return ms(oauth.get("expiresAt")), ms(oauth.get("refreshTokenExpiresAt"))


def codex_who(auth):
    """Who a Codex login belongs to, from its id_token, read locally."""
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if not isinstance(tokens, dict):
        return None
    claims = jwt_claims(tokens.get("id_token"))
    info = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    user = info.get("chatgpt_user_id") or info.get("user_id") or claims.get("sub") or ""
    account = tokens.get("account_id") or info.get("chatgpt_account_id") or ""
    if not user and not account:
        return None
    return {"identity": f"{user}:{account}", "email": str(claims.get("email") or ""), "org": "",
            "plan": str(info.get("chatgpt_plan_type") or "").replace("_", " ").title(), "account_id": str(account)}


def codex_times(auth):
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    exp = jwt_claims(tokens.get("access_token")).get("exp") if isinstance(tokens, dict) else None
    return (exp if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None), None


class Keychain:
    """`/usr/bin/security`, with every secret on stdin. The one exception is Claude Code's own
    login when it outgrows what stdin takes: that goes in argv, as Claude Code itself writes it."""

    def __init__(self, run=subprocess.run, user=None):
        self.run, self.user = run, user or claude_user()

    def call(self, args, stdin=None):
        try:
            return self.run([SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise AccountError("The Keychain could not be reached") from None

    def read(self, service, account):
        done = self.call(["find-generic-password", "-a", account, "-s", service, "-w"])
        if done.returncode == 44:
            return None
        if done.returncode == 36:
            raise AccountError("The Keychain is locked; unlock it and try again")
        if done.returncode:
            raise AccountError("The Keychain would not hand over a saved login")
        text = done.stdout
        return text[:-1] if text.endswith("\n") else text

    @staticmethod
    def line(service, account, secret):
        if re.search(r'["\\\n]', service + account):
            raise AccountError("That Keychain name can't be written safely")
        return f'add-generic-password -U -a "{account}" -s "{service}" -X "{secret.encode().hex()}"\n'

    def write(self, service, account, secret, large=False):
        """`large` lets a secret past stdin's limit go in argv, where programs running as you
        can see it for the moment `security` runs. Claude Code does the same with its own
        login once MCP and plugin logins make it that big ("exceeds security -i stdin limit;
        using argv"), so HQ allows it for that item only; its own items are chunked instead."""
        line = self.line(service, account, secret)
        if len(line) <= LINE_LIMIT:
            done = self.call(["-i"], stdin=line)
        elif large:
            done = self.call(["add-generic-password", "-U", "-a", account, "-s", service, "-X", secret.encode().hex()])
        else:
            raise AccountError("That login is too large to save safely")
        if done.returncode:
            raise AccountError("The Keychain refused to save a login")
        back = self.read(service, account)
        if back is None or digest(back) != digest(secret):
            raise AccountError("The Keychain did not keep a login intact")

    def delete(self, service, account):
        done = self.call(["delete-generic-password", "-a", account, "-s", service])
        if done.returncode not in (0, 44):
            raise AccountError("The Keychain would not remove a saved login")


class Vault:
    """HQ's own items: a secret in hex chunks, then a manifest written last that names them."""
    ACCOUNT = "hq"

    def __init__(self, keychain):
        self.keychain = keychain

    @staticmethod
    def service(tool, key, part=None):
        return f"HQ account {tool} {key}" + ("" if part is None else f" #{part}")

    def manifest(self, tool, key):
        text = self.keychain.read(self.service(tool, key), self.ACCOUNT)
        try:
            value = json.loads(text) if text else None
        except ValueError:
            return None
        ok = isinstance(value, dict) and all(isinstance(value.get(k), int) for k in ("gen", "count"))
        return value if ok else None

    def put(self, tool, key, secret):
        data = secret.encode()
        old = self.manifest(tool, key)
        gen = old["gen"] + 1 if old else 1
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        for n, chunk in enumerate(chunks):
            self.keychain.write(self.service(tool, key, f"{gen}.{n}"), self.ACCOUNT, chunk.hex())
        self.keychain.write(self.service(tool, key), self.ACCOUNT,
                            dumps({"gen": gen, "count": len(chunks), "sha256": digest(secret)}))
        if self.get(tool, key) != secret:
            raise AccountError("HQ could not save that login intact")
        if old:
            self.drop(tool, key, old)

    def get(self, tool, key):
        manifest = self.manifest(tool, key)
        if not manifest:
            return None
        parts = [self.keychain.read(self.service(tool, key, f"{manifest['gen']}.{n}"), self.ACCOUNT)
                 for n in range(manifest["count"])]
        try:
            text = b"".join(bytes.fromhex(part) for part in parts).decode()
        except (TypeError, ValueError):
            text = None
        if text is None or digest(text) != manifest.get("sha256"):
            raise AccountError("HQ's saved login for this account is damaged; add the account again")
        return text

    def drop(self, tool, key, manifest):
        for n in range(manifest["count"]):
            try:
                self.keychain.delete(self.service(tool, key, f"{manifest['gen']}.{n}"), self.ACCOUNT)
            except AccountError:
                pass

    def remove(self, tool, key):
        manifest = self.manifest(tool, key)
        self.keychain.delete(self.service(tool, key), self.ACCOUNT)
        if manifest:
            self.drop(tool, key, manifest)


class Places:
    """Where each tool keeps its login, honouring CLAUDE_CONFIG_DIR and CODEX_HOME."""

    def __init__(self, home=None, env=None):
        env = os.environ if env is None else env
        home = Path(home or Path.home())
        config = env.get("CLAUDE_CONFIG_DIR")
        self.claude_dir = Path(config) if config else home / ".claude"
        self.claude_json = self.claude_dir / ".claude.json" if config else home / ".claude.json"
        self.claude_item = claude_item(config)
        self.codex_home = Path(env.get("CODEX_HOME") or home / ".codex")

    def claude_locks(self):
        # Claude Code's own order: refresh, legacy, storage, config.
        return [self.claude_dir / ".oauth_refresh.lock", Path(os.path.realpath(self.claude_dir) + ".lock"),
                self.claude_dir / ".storage-write.lock", Path(str(self.claude_json) + ".lock")]


@contextmanager
def taken(paths, wait=LOCK_WAIT, sleep=time.sleep, clock=time.monotonic):
    """Claude Code's lock directories, taken in its order. Each gets a moment; one still
    held after that is skipped, and the switch goes ahead, as /login's would."""
    held, missed = [], []
    try:
        for path in paths:
            deadline = clock() + wait
            while True:
                try:
                    os.mkdir(path)
                    held.append(path)
                    break
                except FileNotFoundError:
                    path.parent.mkdir(parents=True, exist_ok=True)
                except FileExistsError:
                    if clock() >= deadline:
                        missed.append(path.name)
                        break
                    sleep(0.05)
        yield missed
    finally:
        for path in reversed(held):
            try:
                os.rmdir(path)
            except OSError:
                pass


@contextmanager
def flocked(path, wait=LOCK_WAIT, sleep=time.sleep, clock=time.monotonic):
    """Codex's auth.lock, the same way: a moment, then on anyway. Yields whether it was held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline, held = clock() + wait, False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if clock() >= deadline:
                    break
                sleep(0.05)
        yield held
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_atomic(path, text, mode=0o600):
    """Temp file, flushed, renamed over, and the folder flushed, so a crash leaves old or new.
    A symlinked file (a dotfiles setup) is written where it points, and stays a link."""
    path = Path(os.path.realpath(path))
    temp = path.with_name(path.name + ".hq-tmp")
    try:
        os.unlink(temp)  # a leftover from a crash, or anything planted there
    except FileNotFoundError:
        pass
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def file_mode(path, default=0o600):
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return default


def stop_group(process):
    """A sign-in runs in its own session; stop it and anything it started."""
    try:
        os.killpg(process.pid, 15)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


class Job:
    """Adding an account: started, then waiting for you to sign in, then added or failed."""

    def __init__(self, tool):
        self.id, self.tool, self.started = uuid.uuid4().hex[:12], tool, now()
        self.state, self.message, self.account, self.terminal = "starting", "Starting the sign-in…", None, False
        self.cancel = threading.Event()

    def public(self):
        return {"id": self.id, "tool": self.tool, "state": self.state, "message": self.message,
                "account": self.account, "terminal": self.terminal, "started": self.started}


class Accounts:
    def __init__(self, store, places=None, keychain=None, run=subprocess.run, spawn=subprocess.Popen,
                 open_url=open_token_url, clock=time.monotonic, sleep=time.sleep, rand=random.uniform,
                 which=shutil.which, temp_root=None, on_switch=None, log=None, stop=stop_group):
        self.store = store
        self.places = places or Places()
        self.keychain = keychain or Keychain()
        self.vault = Vault(self.keychain)
        self.run, self.spawn, self.open_url = run, spawn, open_url
        self.clock, self.sleep, self.rand, self.which = clock, sleep, rand, which
        self.temp_root, self.on_switch, self.stop = temp_root, on_switch, stop
        self.log = log or (lambda message: None)
        self.lock = threading.RLock()           # small shared state: jobs, the cached summary
        self.switching = threading.Lock()       # one switch or removal at a time
        self.jobs = {}
        self.cached = {tool: {"active": None, "count": 0} for tool in TOOLS}
        self.seen_active = {}
        self.next_at, self.backoff, self.next_any = {}, {}, -math.inf
        self.sign_ins = Path(store.path).parent / "sign-ins.json"

    # -- What is signed in now, read from each tool's own files ------------------------

    def claude_login(self):
        text = self.keychain.read(self.places.claude_item, self.keychain.user)
        try:
            return json.loads(text) if text else {}
        except ValueError:
            raise AccountError("Claude Code's saved login can't be read") from None

    def claude_config(self):
        try:
            text = self.places.claude_json.read_text()
        except FileNotFoundError:
            return "", {}
        try:
            value = json.loads(text)
        except ValueError:
            raise AccountError("~/.claude.json can't be read, so HQ won't touch it") from None
        return text, value if isinstance(value, dict) else {}

    def codex_auth(self):
        path = self.places.codex_home / "auth.json"
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None, None
        try:
            return text, json.loads(text)
        except ValueError:
            raise AccountError("Codex's auth.json can't be read, so HQ won't touch it") from None

    def current(self, tool):
        """Who is signed in, from local files only: no Keychain, no network. ~/.claude.json can
        run to megabytes, so each file is read again only once it has changed."""
        path = self.places.claude_json if tool == "claude" else self.places.codex_home / "auth.json"
        try:
            info = path.stat()
            stamp = (str(path), info.st_mtime_ns, info.st_size)
        except OSError:
            return None
        with self.lock:
            memo = getattr(self, "memo", {}).get(tool)
        if memo and memo[0] == stamp:
            return memo[1]
        try:
            who = (claude_who(self.claude_config()[1].get("oauthAccount")) if tool == "claude"
                   else codex_who(self.codex_auth()[1]))
        except (AccountError, OSError):
            who = None
        with self.lock:
            self.memo = {**getattr(self, "memo", {}), tool: (stamp, who)}
        return who

    # -- Saved accounts --------------------------------------------------------------

    def rows(self, tool):
        with self.store.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM accounts WHERE tool=? ORDER BY added", (tool,))]

    def row(self, tool, key):
        with self.store.db() as db:
            found = db.execute("SELECT * FROM accounts WHERE tool=? AND key=?", (tool, key)).fetchone()
        if not found:
            raise AccountError(f"That {NAMES.get(tool, tool)} account isn't saved in HQ")
        return dict(found)

    def remember(self, tool, who, secret, expires, refresh_expires, stale=False):
        key = key_for(tool, who["identity"])
        with self.store.db() as db:
            known = db.execute("SELECT 1 FROM accounts WHERE tool=? AND key=?", (tool, key)).fetchone()
        self.vault.put(tool, key, secret)
        try:
            with self.store.db() as db:
                db.execute("""INSERT INTO accounts (tool,key,identity,email,plan,org,added,expires,refresh_expires,stale)
                    VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tool,key) DO UPDATE SET email=excluded.email,plan=excluded.plan,
                    org=excluded.org,expires=excluded.expires,refresh_expires=excluded.refresh_expires,stale=excluded.stale""",
                           (tool, key, who["identity"], who["email"], who["plan"], who["org"], now(), expires, refresh_expires,
                            int(stale)))
        except sqlite3.Error as error:
            self.log(f"Could not save the {tool} account: {error}")
            if not known:  # never leave a login in the Keychain that no account points to
                try:
                    self.vault.remove(tool, key)
                except AccountError:
                    pass
            raise AccountError("HQ couldn't save that account. Restart HQ and add it again") from None
        return key

    def mark(self, tool, key, **fields):
        allowed = {"last_used", "stale", "usage", "usage_as_of"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        with self.store.db() as db:
            db.execute(f"UPDATE accounts SET {','.join(f'{k}=?' for k in fields)} WHERE tool=? AND key=?",
                       (*fields.values(), tool, key))

    def relabel(self, tool, key, who):
        """Keep a saved account's plan, email and organisation as its login now says."""
        fields = {k: who[k] for k in ("plan", "email", "org") if who and who.get(k)}
        with self.store.db() as db:
            row = db.execute("SELECT plan,email,org FROM accounts WHERE tool=? AND key=?", (tool, key)).fetchone()
            fields = {k: v for k, v in fields.items() if row and row[k] != v}
            if fields:
                db.execute(f"UPDATE accounts SET {','.join(f'{k}=?' for k in fields)} WHERE tool=? AND key=?",
                           (*fields.values(), tool, key))

    def keep_reading(self, tool, key, value):
        if value:
            self.mark(tool, key, usage=json.dumps(value), usage_as_of=value["as_of"])

    def save_claude(self, login, profile, stale=False):
        who = claude_who(profile, login)
        part = {k: login[k] for k in CLAUDE_BOUND if k in login}
        if not who or not isinstance(part.get("claudeAiOauth"), dict):
            return None
        expires, refresh = claude_times(part)
        return self.remember("claude", who, dumps({"login": part, "profile": profile}), expires, refresh, stale)

    def save_codex(self, text, auth, stale=False):
        who = codex_who(auth)
        if not who:
            return None
        expires, refresh = codex_times(auth)
        return self.remember("codex", who, text, expires, refresh, stale)

    def save_current(self, tool):
        """Keep the account signed in now, so you can always switch back to it."""
        if tool == "claude":
            _, config = self.claude_config()
            return self.save_claude(self.claude_login(), config.get("oauthAccount"))
        text, auth = self.codex_auth()
        return self.save_codex(text, auth) if auth else None

    def state(self, row, active):
        if active:
            return "ok"
        if row["stale"] or (row["refresh_expires"] and row["refresh_expires"] <= now()):
            return "needs sign-in"
        if row["expires"] and row["expires"] <= now():
            return "expired"
        return "ok"

    def public(self, row, active_key, reading=None):
        active = row["key"] == active_key
        usage = reading if active else None
        if usage is None and row.get("usage"):
            try:
                usage = valid_reading(json.loads(row["usage"]))
            except ValueError:
                usage = None
        return {"key": row["key"], "email": row["email"], "plan": row["plan"], "org": row["org"], "active": active,
                "saved": True, "state": self.state(row, active), "usage": usage, "added": row["added"],
                "last_used": row["last_used"]}

    def codex_running(self):
        """How many Codex processes are open, from the process list: names only."""
        try:
            done = self.run(["/bin/ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return 0
        count = 0
        for line in (done.stdout or "").splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] != str(os.getpid()) and Path(parts[2]).name in {"codex", "Codex"}:
                count += 1
        return count

    def list(self, readings=None):
        readings = readings or {}
        result = {}
        for tool in TOOLS:
            who = self.current(tool)
            active_key = key_for(tool, who["identity"]) if who else None
            rows = self.rows(tool)
            accounts = [self.public(row, active_key, readings.get(tool)) for row in rows]
            if who and active_key not in {row["key"] for row in rows}:
                accounts.append({"key": active_key, "email": who["email"], "plan": who["plan"], "org": who["org"],
                                 "active": True, "saved": False, "state": "ok", "usage": readings.get(tool),
                                 "added": None, "last_used": None})
            accounts.sort(key=lambda a: (not a["active"], a["email"].lower()))
            result[tool] = {"accounts": accounts, "active": active_key, "adding": self.adding(tool)}
        result["codex"]["running"] = self.codex_running()
        return result

    def summary(self):
        with self.lock:
            return {tool: dict(value) for tool, value in self.cached.items()}

    def refresh(self):
        """What /api/state carries, kept in memory so publishing never reads a login. A
        change of account from anywhere, /login included, tells the usage readers."""
        changed = []
        for tool in TOOLS:
            who = self.current(tool)
            key = key_for(tool, who["identity"]) if who else None
            if who:
                self.relabel(tool, key, who)
            entry = {"active": {"key": key, "email": who["email"], "plan": who["plan"]} if who else None,
                     "count": len(self.rows(tool)), "adding": (self.adding(tool) or {}).get("state")}
            with self.lock:
                self.cached[tool] = entry
                if tool in self.seen_active and self.seen_active[tool] != key:
                    changed.append(tool)
                self.seen_active[tool] = key
        for tool in changed:
            self.switched(tool)
        return self.summary()

    def switched(self, tool):
        if self.on_switch:
            try:
                self.on_switch(tool)
            except Exception:
                pass

    # -- Switching ---------------------------------------------------------------------

    def use(self, tool, key):
        if tool not in TOOLS:
            raise AccountError("Choose claude or codex")
        with self.switching:
            target = self.row(tool, key)
            result = self.use_claude(target) if tool == "claude" else self.use_codex(target)
        with self.lock:
            self.seen_active[tool] = key
        self.mark(tool, key, last_used=now(), stale=0)
        self.switched(tool)
        self.refresh()
        result["account"] = self.public(self.row(tool, key), key)
        return result

    def use_claude(self, target):
        try:
            saved = json.loads(self.vault.get("claude", target["key"]) or "null")
        except ValueError:
            saved = None
        if not isinstance(saved, dict) or not isinstance(saved.get("login"), dict):
            raise AccountError("HQ has no saved login for that account; sign in to it again")
        item, user = self.places.claude_item, self.keychain.user
        with taken(self.places.claude_locks(), sleep=self.sleep, clock=self.clock) as missed:
            if missed:
                self.log(f"Switched Claude without waiting on {', '.join(missed)}")
            before = self.keychain.read(item, user)
            try:
                login = json.loads(before) if before else {}
            except ValueError:
                raise AccountError("Claude Code's saved login can't be read, so HQ won't touch it") from None
            config_text, config = self.claude_config()
            outgoing = claude_who(config.get("oauthAccount"), login)
            if outgoing and key_for("claude", outgoing["identity"]) == target["key"]:
                return {"tool": "claude", "already": True, "note": None, "warning": None, "verified": True}
            # Everything that isn't the account's own stays exactly as it was.
            after = {k: v for k, v in login.items() if k not in CLAUDE_BOUND}
            after.update(saved["login"])
            text = dumps(after)
            # Keep the outgoing account's freshest login. A refresh may have been under way,
            # so it is kept anyway and marked in case it needs a fresh sign-in later.
            if outgoing:
                expires, _ = claude_times(login)
                renewing = ".oauth_refresh.lock" in missed or (expires is not None and expires - now() < RENEWING)
                self.save_claude(login, config.get("oauthAccount"), stale=renewing)
            try:
                self.keychain.write(item, user, text, large=True)
            except AccountError:
                self.restore_item(before)
                raise AccountError("Claude Code's login could not be switched; it is unchanged") from None
            try:
                config = dict(config, oauthAccount=saved["profile"])
                for cache in CLAUDE_CACHES:
                    config.pop(cache, None)
                write_atomic(self.places.claude_json, json.dumps(config, indent=2), file_mode(self.places.claude_json))
            except (OSError, TypeError, ValueError):
                self.restore_item(before)
                self.restore_config(config_text)
                raise AccountError("Claude Code's login could not be switched; it is unchanged") from None
        verified = self.verify_claude(target["email"])
        if verified is False:
            with taken(self.places.claude_locks(), sleep=self.sleep, clock=self.clock):
                self.restore_item(before)
                self.restore_config(config_text)
            raise AccountError("Claude Code didn't take the new account, so HQ put the old one back")
        return {"tool": "claude", "already": False, "note": CLAUDE_NOTE, "warning": None, "verified": verified}

    def restore_item(self, before):
        item, user = self.places.claude_item, self.keychain.user
        try:
            if before:
                self.keychain.write(item, user, before, large=True)
            else:
                self.keychain.delete(item, user)
        except AccountError:
            self.log("Could not put Claude Code's login back")

    def restore_config(self, text):
        try:
            if text:
                write_atomic(self.places.claude_json, text, file_mode(self.places.claude_json))
        except OSError:
            self.log("Could not put ~/.claude.json back")

    def verify_claude(self, email):
        """What Claude Code itself now says. None when it can't say."""
        binary = self.which("claude")
        if not binary or not email:
            return None
        try:
            done = self.run([binary, "auth", "status", "--json"], capture_output=True, text=True, timeout=30)
            found = json.loads(done.stdout) if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

        def emails(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in {"email", "emailAddress"} and isinstance(v, str):
                        yield v
                    else:
                        yield from emails(v)
            elif isinstance(value, list):
                for v in value:
                    yield from emails(v)

        seen = [e.lower() for e in emails(found)]
        if not seen:
            return None
        return email.lower() in seen

    def use_codex(self, target):
        text = self.vault.get("codex", target["key"])
        try:
            incoming = json.loads(text) if text else None
        except ValueError:
            incoming = None
        if not codex_who(incoming):
            raise AccountError("HQ has no saved login for that account; sign in to it again")
        if not self.codex_file_mode():
            raise AccountError("Codex keeps its login in the Keychain on this Mac, which HQ can't switch yet")
        path = self.places.codex_home / "auth.json"
        with flocked(self.places.codex_home / "auth.lock", sleep=self.sleep, clock=self.clock) as held:
            if not held:
                self.log("Switched Codex without waiting on auth.lock")
            before, current = self.codex_auth()
            outgoing = codex_who(current) if current else None
            if outgoing and key_for("codex", outgoing["identity"]) == target["key"]:
                return {"tool": "codex", "already": True, "note": None, "warning": None, "verified": True}
            if outgoing:
                expires, _ = codex_times(current)
                last = iso_seconds(current.get("last_refresh"))
                renewing = (not held or (expires is not None and expires - now() < RENEWING)
                            or (last is not None and now() - last > CODEX_RENEW_AGE))
                self.save_codex(before, current, stale=renewing)
            try:
                write_atomic(path, text, 0o600)
                back = codex_who(json.loads(path.read_text()))
                if not back or key_for("codex", back["identity"]) != target["key"]:
                    raise ValueError("not switched")
            except (OSError, ValueError):
                try:
                    if before is not None:
                        write_atomic(path, before, 0o600)
                except OSError:
                    self.log("Could not put Codex's auth.json back")
                raise AccountError("Codex's login could not be switched; it is unchanged") from None
        running = self.codex_running()
        warning = None
        if running:
            open_ = f"the {running} open Codex session{'s' if running != 1 else ''}" if running > 1 else "the open Codex session"
            warning = f"Switched to {target['email'] or 'that account'}. Restart {open_} to use it."
        return {"tool": "codex", "already": False, "note": None, "warning": warning, "verified": True, "running": running}

    def codex_file_mode(self):
        try:
            text = (self.places.codex_home / "config.toml").read_text()
        except OSError:
            return True
        found = re.search(r'(?m)^\s*cli_auth_credentials_store\s*=\s*"([^"]*)"', text)
        return not found or found.group(1) == "file"

    def remove(self, tool, key):
        if tool not in TOOLS:
            raise AccountError("Choose claude or codex")
        with self.switching:
            row = self.row(tool, key)
            who = self.current(tool)
            if who and key_for(tool, who["identity"]) == key:
                raise AccountError("Switch to another account before removing this one")
            self.vault.remove(tool, key)
            with self.store.db() as db:
                db.execute("DELETE FROM accounts WHERE tool=? AND key=?", (tool, key))
        self.next_at.pop((tool, key), None)
        self.refresh()
        return {"removed": key, "email": row["email"]}

    # -- Adding ------------------------------------------------------------------------

    # -- Temporary sign-ins -------------------------------------------------------------
    # A Claude sign-in finished in Terminal can land after HQ gave up on it (cancelled, timed out,
    # HQ restarted), leaving a login in a temporary Keychain item nothing would remove. Every
    # temporary folder is written down until it is gone, and what is left is swept: when HQ starts
    # and before each new sign-in.

    def recorded(self):
        try:
            folders = json.loads(self.sign_ins.read_text())
            return [f for f in folders if isinstance(f, str)] if isinstance(folders, list) else []
        except (OSError, ValueError):
            return []

    def record(self, folders):
        try:
            write_atomic(self.sign_ins, json.dumps(sorted(set(folders))))
        except OSError:
            self.log("Could not note the temporary sign-in folders")

    def sweep_sign_ins(self, keep=()):
        left = []
        for folder in self.recorded():
            if folder in keep:
                left.append(folder)
                continue
            try:
                self.keychain.delete(claude_item(folder), self.keychain.user)
            except AccountError:
                left.append(folder)  # try again next time
                continue
            shutil.rmtree(folder, ignore_errors=True)
        self.record(left)

    def adding(self, tool):
        with self.lock:
            for job in self.jobs.values():
                if job.tool == tool and job.state in {"starting", "waiting"}:
                    return job.public()
        return None

    def add(self, tool, wait=False):
        if tool not in TOOLS:
            raise AccountError("Choose claude or codex")
        running = self.adding(tool)
        if running:
            return running
        job = Job(tool)
        with self.lock:
            busy = [j.folder for j in self.jobs.values() if j.state in {"starting", "waiting"} and getattr(j, "folder", None)]
            self.jobs[job.id] = job
        self.sweep_sign_ins(keep=busy)
        thread = threading.Thread(target=self.sign_in, args=(job,), daemon=True)
        thread.start()
        if wait:
            thread.join()
        return job.public()

    def job(self, id):
        with self.lock:
            job = self.jobs.get(id)
        if not job:
            raise AccountError("That sign-in isn't running")
        return job.public()

    def cancel(self, id):
        with self.lock:
            job = self.jobs.get(id)
        if not job:
            raise AccountError("That sign-in isn't running")
        job.cancel.set()
        return job.public()

    def captured(self, tool, folder):
        """A finished sign-in in the temporary folder: its login, and whose it is."""
        if tool == "claude":
            text = self.keychain.read(claude_item(str(folder)), self.keychain.user)
            try:
                login = json.loads(text) if text else None
                config = json.loads((folder / ".claude.json").read_text())
            except (OSError, ValueError):
                return None
            if not isinstance(login, dict) or not isinstance(login.get("claudeAiOauth"), dict):
                return None
            return (login, config.get("oauthAccount")) if claude_who(config.get("oauthAccount")) else None
        try:
            text = (folder / "auth.json").read_text()
            auth = json.loads(text)
        except (OSError, ValueError):
            return None
        return (text, auth) if codex_who(auth) else None

    def sign_in(self, job):
        tool = job.tool
        folder = Path(tempfile.mkdtemp(prefix=f"hq-{tool}-sign-in-", dir=self.temp_root))
        job.folder = str(folder)
        if tool == "claude":
            self.record(self.recorded() + [str(folder)])
        process, outcome = None, None
        try:
            binary = self.which("claude" if tool == "claude" else "codex")
            if not binary:
                raise AccountError(f"{'Claude Code' if tool == 'claude' else 'Codex'} isn't installed")
            env = dict(os.environ, **({"CLAUDE_CONFIG_DIR": str(folder)} if tool == "claude" else {"CODEX_HOME": str(folder)}))
            # Never logout, and never the real home: a login there would revoke the current one.
            command = [binary, "auth", "login", "--claudeai"] if tool == "claude" else [binary, "login"]
            process = self.spawn(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            job.state, job.message = "waiting", "Waiting for you to sign in…"
            start = self.clock()
            found = None
            while not job.cancel.is_set() and self.clock() - start < ADD_TIMEOUT:
                found = self.captured(tool, folder)
                if found:
                    break
                code = process.poll() if process else None
                if code is not None:
                    if tool == "claude" and not job.terminal and code != 0 and self.clock() - start < TERMINAL_FALLBACK:
                        # It quit at once: it wants a terminal, so finish the sign-in in one.
                        self.open_terminal(command, env_name="CLAUDE_CONFIG_DIR", folder=folder)
                        job.terminal, process = True, None
                        job.message = "Finish signing in in the Terminal window HQ opened"
                    elif not job.terminal:
                        raise AccountError("The sign-in stopped before it finished")
                self.sleep(1)
            if job.cancel.is_set():
                outcome = ("cancelled", "Cancelled")
                return
            if not found:
                raise AccountError("The sign-in timed out")
            if tool == "claude":
                key = self.save_claude(found[0], found[1])
            else:
                key = self.save_codex(*found)
            if not key:
                raise AccountError("That sign-in didn't produce a login HQ can use")
            # The account signed in now is saved too, so you can always switch back.
            try:
                self.save_current(tool)
            except AccountError as error:
                self.log(f"Could not save the current {tool} account: {error}")
            job.account = self.public(self.row(tool, key), None)
            outcome = ("added", f"Added {job.account['email'] or 'the account'}")
        except AccountError as error:
            outcome = ("failed", str(error))
        except (OSError, subprocess.SubprocessError):
            outcome = ("failed", "The sign-in could not start")
        finally:
            if process and process.poll() is None:
                self.stop(process)
            # A sign-in left running in Terminal can still land later: the sweep takes that one.
            lingering = job.terminal and (outcome or ("failed",))[0] != "added"
            if tool == "claude":
                try:
                    self.keychain.delete(claude_item(str(folder)), self.keychain.user)
                    if not lingering:
                        self.record([f for f in self.recorded() if f != str(folder)])
                except AccountError:
                    self.log("Could not remove the temporary sign-in from the Keychain")
            if not lingering:
                shutil.rmtree(folder, ignore_errors=True)
            if lingering and outcome:
                outcome = (outcome[0], outcome[1] + ". You can close the Terminal window HQ opened")
            try:
                self.refresh()
            except Exception:
                pass
            # Only now, with everything tidied, does the sign-in say it has finished.
            job.state, job.message = outcome or ("failed", "The sign-in stopped")

    def open_terminal(self, command, env_name, folder):
        line = f"{env_name}={shlex.quote(str(folder))} {shlex.join(command)}; exit"
        script = line.replace("\\", "\\\\").replace('"', '\\"')
        self.run(["/usr/bin/osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                  "-e", 'tell application "Terminal" to activate'], capture_output=True, text=True, timeout=30)

    # -- Usage of accounts that aren't signed in ---------------------------------------

    def read_saved(self, tool, row):
        """(reading, retry_after). No request at all once the saved token has expired."""
        text = self.vault.get(tool, row["key"])
        if not text:
            return None, None
        saved = json.loads(text)
        self.relabel(tool, row["key"], claude_who(saved.get("profile"), saved.get("login")) if tool == "claude"
                     else codex_who(saved))
        if tool == "claude":
            oauth = saved.get("login", {}).get("claudeAiOauth") or {}
            token, (expires, _) = oauth.get("accessToken"), claude_times(saved.get("login", {}))
            headers = {"anthropic-beta": "oauth-2025-04-20"}
            url, parse = CLAUDE_USAGE_URL, parse_claude_login_usage
        else:
            tokens = saved.get("tokens") or {}
            token, (expires, _) = tokens.get("access_token"), codex_times(saved)
            headers = {"ChatGPT-Account-Id": str(tokens.get("account_id") or "")}
            url, parse = CODEX_USAGE_URL, parse_codex_usage
        if not isinstance(token, str) or not token or not expires or expires <= now() + 60:
            return None, None
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                       "User-Agent": "hq", **headers})
        try:
            with self.open_url(request, timeout=15) as response:
                return parse(json.load(response)), None
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 401:
                return None, "refused"
            try:
                hint = max(0, int(error.headers.get("Retry-After", ""))) if error.code == 429 else None
            except (TypeError, ValueError, AttributeError):
                hint = None
            return None, hint if hint is not None else (-1 if error.code == 429 else None)
        except (urllib.error.URLError, OSError, ValueError):
            return None, -1

    def poll(self):
        """At most one saved account per call, never two close together, each on the idle
        rhythm (it is by definition not the one at work), with back-off when refused."""
        if self.clock() < self.next_any:
            return None
        due = []
        for tool in TOOLS:
            who = self.current(tool)
            active = key_for(tool, who["identity"]) if who else None
            due += [(tool, row) for row in self.rows(tool) if row["key"] != active
                    and self.clock() >= self.next_at.get((tool, row["key"]), -math.inf)]
        if not due:
            return None
        tool, row = min(due, key=lambda pair: self.next_at.get((pair[0], pair[1]["key"]), -math.inf))
        slot = (tool, row["key"])
        try:
            value, hint = self.read_saved(tool, row)
        except (AccountError, ValueError, *READ_ERRORS):
            value, hint = None, None
        if value:
            self.keep_reading(tool, row["key"], value)
            self.backoff.pop(slot, None)
            wait = self.rand(*IDLE)
        elif hint is None or hint == "refused":
            if hint == "refused":  # the saved login was turned down: it will need a fresh sign-in
                self.mark(tool, row["key"], stale=1)
            wait = self.rand(*IDLE)  # expired, or nothing to ask: look again later
        else:
            backoff = self.backoff.get(slot, 600)
            wait = max(hint if hint > 0 else 0, backoff) * self.rand(1, 1.25)
            self.backoff[slot] = min(backoff * 2, 3600)
        self.next_at[slot] = self.clock() + wait
        self.next_any = self.clock() + self.rand(*SPREAD)
        return slot


class AccountsWorker:
    """Keeps the summary current, keeps the signed-in account's latest numbers with it, and
    reads saved accounts' usage now and then, only while someone is looking."""

    def __init__(self, accounts, usage, every=15):
        self.accounts, self.usage, self.every = accounts, usage, every
        self.stop = threading.Event()
        self.last = {}

    def step(self):
        self.accounts.refresh()
        readings = self.usage.current() if self.usage else {}
        for tool in TOOLS:
            active = (self.accounts.summary()[tool].get("active") or {}).get("key")
            value = readings.get(tool)
            if active and value and self.last.get(tool) != (active, value["as_of"]):
                try:
                    self.accounts.row(tool, active)
                    self.accounts.keep_reading(tool, active, value)
                    self.last[tool] = (active, value["as_of"])
                except AccountError:
                    pass  # not saved yet
        if self.usage is not None and getattr(self.usage, "is_watched", lambda: False)():
            self.accounts.poll()

    def run(self):
        delay = 2
        while not self.stop.wait(delay):
            try:
                self.step()
            except (AccountError, ValueError, *READ_ERRORS):
                pass
            delay = self.every

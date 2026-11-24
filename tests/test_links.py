"""Item links: what an agent writes when it mentions an item, and what clicking one does.

A click opens the item in the pill and closes the tab it came through, or opens the dashboard
on it when no pill is listening. A page that loads a link by itself never pops the pill open.
"""
import json
import os
import plistlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from hq import links, opener
from hq.hooks import handle
from hq.mcp import Tools
from hq.server import Server
from hq.store import Store


class Home(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patcher = patch.dict(os.environ, {"HQ_HOME": temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(self.home / "hq.sqlite3")
        self.store.register("a", "Agent")


class LinkTests(Home):
    def test_a_link_uses_the_port_hq_runs_on_and_eight_digits_of_the_id(self):
        self.assertEqual(links.item_link("ce9430d3-11a2-4769-8d33-071564773da4"), "http://127.0.0.1:4310/i/ce9430d3")
        (self.home / "server.json").write_text(json.dumps({"pid": 1, "port": 5001}))
        self.assertEqual(links.item_link("ce9430d3-11a2"), "http://127.0.0.1:5001/i/ce9430d3")
        for broken in ('{"port": "5001"}', '{"port": true}', '{"port": 70000}', "[]", "not json"):
            (self.home / "server.json").write_text(broken)
            self.assertEqual(links.port(), 4310, broken)

    def install_links(self):
        contents = self.home / opener.NAME / "Contents"
        contents.mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as file:
            plistlib.dump({"CFBundleVersion": "abc"}, file)

    def test_a_terminal_on_a_mac_with_hq_links_gets_links_that_skip_the_browser(self):
        with patch("sys.platform", "darwin"), patch.dict(os.environ, {"TERM_PROGRAM": "WarpTerminal"}):
            self.assertEqual(links.item_link("ce9430d3-11a2"), "http://127.0.0.1:4310/i/ce9430d3")  # no HQ Links yet
            self.install_links()
            self.assertEqual(links.item_link("ce9430d3-11a2"), "hq://i/ce9430d3")
            self.assertEqual(links.link_base(), "hq://i/")
            self.assertEqual(links.with_links({"item_id": "ce9430d3-11a2"})["hq_link"], "hq://i/ce9430d3")
            # Terminal.app may not make them clickable, and a desktop chat app may refuse them.
            for terminal in ("Apple_Terminal", ""):
                with patch.dict(os.environ, {"TERM_PROGRAM": terminal}):
                    self.assertEqual(links.item_link("ce9430d3-11a2"), "http://127.0.0.1:4310/i/ce9430d3", terminal)
        with patch("sys.platform", "linux"), patch.dict(os.environ, {"TERM_PROGRAM": "WarpTerminal"}):
            self.assertEqual(links.item_link("ce9430d3-11a2"), "http://127.0.0.1:4310/i/ce9430d3")

    def test_a_reference_is_an_id_the_start_of_one_or_a_link(self):
        for text in ("ce9430d3", " CE9430D3 ", "http://127.0.0.1:4310/i/ce9430d3", "http://localhost:4310/i/ce9430d3/?x=1", "/i/ce9430d3",
                     "hq://i/ce9430d3", "HQ://I/CE9430D3/"):
            self.assertEqual(links.reference(text), "ce9430d3", text)
        self.assertEqual(links.reference("ce9430d3-11a2-4769-8d33-071564773da4"), "ce9430d3-11a2-4769-8d33-071564773da4")
        for text in ("", "ce94", "ce94%", "../store.py", "http://127.0.0.1:4310/", "hq://", "x" * 8, "ce9430d3" * 6):
            with self.assertRaises(ValueError, msg=text):
                links.reference(text)

    def test_markdown_keeps_a_title_with_brackets_in_one_label(self):
        self.assertEqual(links.markdown("Pick [one]\nof\\these", "ab12cd34ef", 4310),
                         r"[Pick \[one\] of\\these](http://127.0.0.1:4310/i/ab12cd34)")

    def test_the_outermost_item_or_thing_about_one_carries_the_link(self):
        result = links.with_links({
            "items": [{"id": "aaaaaaaa1", "title": "T", "session_id": "a", "comments": [{"id": "c", "item_id": "aaaaaaaa1"}],
                       "link": {"label": "Its own step link", "url": "https://example.com"}}],
            "sessions": [{"id": "a", "name": "Agent"}],
            "replies": [{"item_id": "bbbbbbbb2", "text": "Yes"}]}, 4310)
        item = result["items"][0]
        self.assertEqual(item["hq_link"], "http://127.0.0.1:4310/i/aaaaaaaa")
        self.assertEqual(item["link"], {"label": "Its own step link", "url": "https://example.com"})  # untouched
        self.assertNotIn("hq_link", item["comments"][0])
        self.assertNotIn("hq_link", result["sessions"][0])
        self.assertEqual(result["replies"][0]["hq_link"], "http://127.0.0.1:4310/i/bbbbbbbb")
        self.assertEqual(links.with_links([1, "x", None], 4310), [1, "x", None])

    def test_find_items_takes_the_whole_id_or_a_start_no_wildcards(self):
        one, two = self.store.add("a", "One")["id"], self.store.add("a", "Two")["id"]
        self.assertEqual(self.store.find_items(one), [one])
        self.assertEqual(self.store.find_items(one[:8]), [one] if one[:8] != two[:8] else self.store.find_items(one[:8]))
        self.assertEqual(self.store.find_items("%"), [])
        self.assertEqual(self.store.find_items("_" * 8), [])
        with self.store.db() as db:  # two items that share a start
            db.execute("UPDATE items SET id=? WHERE id=?", ("abcdef12-0001", one))
            db.execute("UPDATE items SET id=? WHERE id=?", ("abcdef12-0002", two))
        self.assertEqual(sorted(self.store.find_items("abcdef12")), ["abcdef12-0001", "abcdef12-0002"])
        self.assertEqual(self.store.find_items("abcdef12-0002"), ["abcdef12-0002"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HTTPLinkTests(Home):
    def setUp(self):
        super().setUp()
        self.server = Server(0, self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(stop)
        self.item = self.store.add("a", "Restart HQ <now>")

    def get(self, path, headers=None):
        """(status, headers, body) without following a redirect."""
        request = urllib.request.Request(self.server.origin + path, headers=headers or {})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, error.read().decode()

    def stream(self, surface=None):
        """An open event stream, after its first snapshot."""
        path = "/api/events" + (f"?surface={surface}" if surface else "")
        response = urllib.request.urlopen(urllib.request.Request(self.server.origin + path, headers={"X-HQ-Token": self.server.token}), timeout=5)
        self.addCleanup(response.close)
        self.assertEqual(self.event(response)[0], "state")
        return response

    @staticmethod
    def event(response):
        lines = []
        while True:
            line = response.readline().decode().strip()
            if line:
                lines.append(line)
            elif lines:
                kind = next(l[6:].strip() for l in lines if l.startswith("event:"))
                return kind, json.loads(next(l[5:] for l in lines if l.startswith("data:")))

    def opened(self, response):
        """The next item the pill is told to open; snapshots and keepalives can come first."""
        for _ in range(10):
            kind, data = self.event(response)
            if kind == "open":
                return data["id"]
        self.fail("The pill was never told to open anything")

    def panels(self, count):
        # A pill that went away is noticed at the next keepalive that fails to send, a few seconds on.
        for _ in range(500):
            if self.server.panels == count:
                return
            threading.Event().wait(0.02)
        self.fail(f"{self.server.panels} pills listening, expected {count}")

    def test_with_no_pill_a_link_opens_the_dashboard_on_the_item(self):
        self.stream()  # a dashboard tab is not a pill
        self.panels(0)
        status, headers, _ = self.get(f"/i/{self.item['id'][:8]}")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/#/?item={self.item['id']}")
        self.assertEqual(self.server.feed.opened, 0)

    def test_with_a_pill_a_link_opens_there_and_its_tab_closes(self):
        pill = self.stream("float")
        self.panels(1)
        status, headers, body = self.get(f"/i/{self.item['id'][:8]}", {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"})
        self.assertEqual(status, 200)
        self.assertIn("data-close", body)
        self.assertIn('src="/open.js"', body)
        self.assertIn(f'href="/#/?item={self.item["id"]}"', body)
        self.assertNotIn(self.server.token, body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.opened(pill), self.item["id"])
        # The whole id works too, and each click is its own event.
        self.assertEqual(self.get(f"/i/{self.item['id']}")[0], 200)
        self.assertEqual(self.opened(pill), self.item["id"])
        pill.close()
        self.panels(0)

    def test_a_page_loading_a_link_by_itself_never_opens_the_pill(self):
        self.stream("float")
        self.panels(1)
        quiet = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
        self.assertEqual(self.get(f"/i/{self.item['id'][:8]}", quiet)[0], 303)
        self.assertEqual(self.server.feed.opened, 0)
        # A person clicking a link on some page still counts.
        clicked = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-User": "?1", "Sec-Fetch-Dest": "document"}
        self.assertEqual(self.get(f"/i/{self.item['id'][:8]}", clicked)[0], 200)
        self.assertEqual(self.server.feed.opened, 1)

    def test_unknown_ambiguous_and_odd_links_say_so(self):
        status, _, body = self.get("/i/0000000000")
        self.assertEqual(status, 404)
        self.assertIn("HQ has no item 0000000000", body)
        status, _, body = self.get("/i/%3Cscript%3E")
        self.assertEqual(status, 404)
        self.assertNotIn("<script>", body)
        other = self.store.add("a", "Second")
        with self.store.db() as db:
            db.execute("UPDATE items SET id=? WHERE id=?", ("abcdef12-0001", self.item["id"]))
            db.execute("UPDATE items SET id=? WHERE id=?", ("abcdef12-0002", other["id"]))
        status, _, body = self.get("/i/abcdef12")
        self.assertEqual(status, 300)
        self.assertIn('href="/i/abcdef12-0001"', body)
        self.assertIn("Restart HQ &lt;now&gt;", body)
        self.assertEqual(self.get("/i/abcdef12-0002")[0], 303)

    def test_links_answer_on_localhost_only(self):
        status, _, _ = self.get(f"/i/{self.item['id'][:8]}", {"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        self.assertEqual(self.get("/open.js")[0], 200)


class OpenerTests(Home):
    """Building HQ Links, with every outside command faked: no test registers anything with macOS."""

    def setUp(self):
        super().setUp()
        self.calls = []
        def run(*command):
            self.calls.append(command)
            if command[0] == "swiftc":
                Path(command[command.index("-o") + 1]).write_bytes(b"binary")
            return Mock(returncode=self.fail_on.get(command[0], 0), stdout="", stderr="no")
        self.fail_on = {}
        for patcher in (patch("sys.platform", "darwin"), patch("hq.opener.run", run),
                        patch("hq.opener.shutil.which", return_value="/usr/bin/swiftc")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_it_is_built_signed_and_registered_once_per_version(self):
        self.assertEqual(opener.ensure()["status"], "built")
        app = opener.app_path()
        with (app / "Contents/Info.plist").open("rb") as file:
            info = plistlib.load(file)
        self.assertEqual(info["CFBundleURLTypes"][0]["CFBundleURLSchemes"], ["hq"])
        self.assertTrue(info["LSBackgroundOnly"])  # never takes focus from the terminal
        self.assertEqual(info["HQHome"], str(self.home.resolve()))
        self.assertEqual(info["HQCommand"][-1].rsplit("/", 1)[-1], "hq")
        self.assertTrue((app / "Contents/MacOS" / opener.EXECUTABLE).exists())
        self.assertEqual([c[0] for c in self.calls], ["swiftc", "codesign", opener.LSREGISTER])
        self.assertEqual(self.calls[-1], (opener.LSREGISTER, "-f", str(app)))
        self.assertTrue(opener.ready())
        self.calls.clear()
        self.assertEqual(opener.ensure()["status"], "ready")
        self.assertEqual(self.calls, [])
        # A new command to run means a new build, replacing the old one.
        with patch("hq.opener.hq_command", return_value=["/usr/bin/python3", "/elsewhere/bin/hq"]):
            self.assertEqual(opener.ensure()["status"], "built")
        self.assertIn((opener.LSREGISTER, "-u", str(app)), self.calls)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".hq-links")], [])

    def test_a_failed_build_leaves_nothing_registered(self):
        for step in ("swiftc", "codesign", opener.LSREGISTER):
            self.fail_on = {step: 1}
            with self.assertRaises(ValueError, msg=step):
                opener.ensure()
            self.assertFalse(opener.ready(), step)
            self.assertEqual([p for p in self.home.iterdir() if "links" in p.name.lower()], [], step)

    def test_remove_unregisters_it(self):
        opener.ensure()
        self.assertEqual(opener.remove()["status"], "removed")
        self.assertFalse((opener.app_path()).exists())
        self.assertIn((opener.LSREGISTER, "-u", str(opener.app_path())), self.calls)
        self.assertEqual(opener.remove()["status"], "not installed")


class AgentLinkTests(Home):
    def test_tool_results_carry_each_items_link(self):
        tools, never = Tools(self.store), threading.Event()
        readable = {"minutes": 1, "matters": {"lead": "Unblocks billing"}, "needs_you": {"lead": "It's your call"}}
        item = tools.call("hq_report", {"session_id": "a", "title": "Pick a plan", "kind": "question", "options": ["A", "B"], **readable}, never)
        self.assertEqual(item["hq_link"], f"http://127.0.0.1:4310/i/{item['id'][:8]}")
        listed = tools.call("hq_list", {"session_id": "a"}, never)
        self.assertEqual([i["hq_link"] for i in listed["items"]], [item["hq_link"]])
        self.store.reply(item["id"], "A")
        self.assertEqual(tools.call("hq_inbox", {"session_id": "a"}, never)[0]["hq_link"], item["hq_link"])

    def test_hook_context_links_the_items_it_names(self):
        self.store.register("claude-code:x", "Agent x", "claude-code", "x", "/tmp/project")
        item = self.store.add("claude-code:x", "Pick [the] plan", kind="question", options=["A", "B"])
        self.store.reply(item["id"], "A")
        out = []
        handle("claude-code", {"session_id": "x", "hook_event_name": "UserPromptSubmit", "cwd": "/tmp/project"}, self.store, out.append)
        context = out[0]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[its title](http://127.0.0.1:4310/i/ + the first 8 characters of its id; HQ returns it as hq_link)", context)
        self.assertIn(f"[open in HQ](http://127.0.0.1:4310/i/{item['id'][:8]})", context)


if __name__ == "__main__":
    unittest.main()

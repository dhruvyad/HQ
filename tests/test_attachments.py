"""Files pasted into a reply reach the agent as paths it can open, and stay on this machine."""
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hq import attachments as files
from hq.store import Store

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.store = Store(self.base / "hq.sqlite3")
        self.store.register("a", "Agent", "claude-code")

    def test_a_reply_carries_the_path_of_each_file_for_the_agent(self):
        shot = files.save(self.base, PNG, "image/png", "../../screen shot.png")
        notes = files.save(self.base, "Step 3 failed".encode(), "text/plain; charset=utf-8", "")
        self.assertEqual(shot["name"], "screen shot.png")  # no path tricks
        self.assertEqual(notes["name"], "pasted.txt")
        self.assertEqual(oct(os.stat(shot["path"]).st_mode & 0o777), "0o600")
        self.assertTrue(Path(shot["path"]).is_relative_to(self.base / "attachments"))
        item = self.store.add("a", "Does the chart look right?", kind="question")
        answered = self.store.reply(item["id"], "The legend overlaps, see this", [shot["id"], notes["id"], shot["id"]])
        text = self.store.inbox("a")[0]["text"]
        self.assertTrue(text.startswith("The legend overlaps, see this\n\nAttached by the user. Open each file to see it:"))
        self.assertIn(shot["path"], text)
        self.assertIn(notes["path"], text)
        self.assertEqual([a["id"] for a in answered["answer_files"]], [shot["id"], notes["id"]])

    def test_a_file_alone_is_an_answer(self):
        shot = files.save(self.base, PNG, "image/png", "x.png")
        item = self.store.add("a", "Send the screenshot", owner="human")
        self.assertIn(shot["path"], self.store.reply(item["id"], "", [shot["id"]])["answer"])
        with self.assertRaises(ValueError):
            self.store.reply(self.store.add("a", "Empty")["id"], "", [])

    def test_questions_to_the_agent_take_files_too(self):
        shot = files.save(self.base, PNG, "image/png", "x.png")
        item = self.store.add("a", "Pick a layout", kind="question")
        question = self.store.ask_agent(item["id"], "Like this one?", [shot["id"]])
        self.assertIn(shot["path"], question["text"])
        self.assertEqual(question["attachments"][0]["id"], shot["id"])

    def test_only_real_files_of_known_types_are_kept(self):
        for data, kind in ((b"not a png", "image/png"), (PNG, "application/x-sh"), (b"", "image/png"),
                           (b"\xff\xfe\x00", "text/plain"), (b"RIFF0000WAVE", "image/webp")):
            with self.assertRaises(ValueError):
                files.save(self.base, data, kind)
        for bad in (["../../etc/passwd"], ["0" * 32], "abc", [files.save(self.base, PNG, "image/png")["id"]] * 0 + ["x"] * 11):
            with self.assertRaises(ValueError):
                files.resolve(self.base, bad)

    def test_uploads_never_sent_are_removed_after_a_day(self):
        used = files.save(self.base, PNG, "image/png")
        unused = files.save(self.base, PNG, "image/png")
        item = self.store.add("a", "Q", kind="question")
        self.store.reply(item["id"], "see", [used["id"]])
        old = time.time() - 2 * 86400
        for meta in (used, unused):
            os.utime(Path(meta["path"]).with_suffix(".json"), (old, old))
        self.assertEqual(self.store.sweep_files(), 1)
        self.assertTrue(Path(used["path"]).exists())
        self.assertFalse(Path(unused["path"]).exists())

    def test_files_go_thirty_days_after_their_item_is_done(self):
        from hq.store import KEEP_FILES
        old = time.time() - 60 * 86400
        kept = {}
        for name, done_ago in (("open", None), ("recent", 29 * 86400), ("long done", 31 * 86400)):
            shot = files.save(self.base, PNG, "image/png", f"{name}.png")
            os.utime(Path(shot["path"]).with_suffix(".json"), (old, old))
            item = self.store.add("a", name, kind="question")
            self.store.reply(item["id"], "see", [shot["id"]])
            if done_ago is not None:  # heard, acted on and closed that long ago
                with self.store.db() as db:
                    db.execute("UPDATE items SET status='done',updated=? WHERE id=?", (time.time() - done_ago, item["id"]))
            kept[name] = shot
        self.assertEqual(KEEP_FILES, 30 * 86400)
        self.assertEqual(self.store.sweep_files(), 1)
        self.assertTrue(Path(kept["open"]["path"]).exists())     # still being worked on
        self.assertTrue(Path(kept["recent"]["path"]).exists())   # done 29 days ago
        self.assertFalse(Path(kept["long done"]["path"]).exists())  # done 31 days ago


class AttachmentHTTPTests(unittest.TestCase):
    def setUp(self):
        from test_hq import HTTPTests  # reuse its throwaway server
        self.http = HTTPTests("test_http_skip")
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.store = self.http.store

    def post(self, data, kind, name="", token=True):
        headers = {"Content-Type": kind, "X-HQ-Filename": name, "Host": f"127.0.0.1:{self.http.server.server_port}"}
        if token:
            headers["X-HQ-Token"] = self.http.server.token
        request = urllib.request.Request(f"{self.http.server.origin}/api/attachments", data=data, headers=headers, method="POST")
        return urllib.request.urlopen(request)

    def test_upload_reply_and_read_back(self):
        with self.post(PNG, "image/png", "shot%20one.png") as response:
            meta = json.load(response)
        self.assertEqual(meta["name"], "shot one.png")
        item = self.store.add("a", "Q", kind="question")
        headers = {"X-HQ-Token": self.http.server.token}
        with self.http.request(f"/api/items/{item['id']}/reply", {"text": "here", "attachments": [meta["id"]]}, headers) as response:
            self.assertEqual(json.load(response)["answer_files"][0]["id"], meta["id"])
        request = urllib.request.Request(f"{self.http.server.origin}/api/attachments/{meta['id']}", headers=headers)
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.read(), PNG)
            self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        for call in (lambda: self.post(PNG, "image/png", token=False), lambda: self.post(b"x", "image/png"),
                     lambda: urllib.request.urlopen(f"{self.http.server.origin}/api/attachments/{meta['id']}")):
            with self.assertRaises(urllib.error.HTTPError) as refused:
                call()
            refused.exception.close()


if __name__ == "__main__":
    unittest.main()

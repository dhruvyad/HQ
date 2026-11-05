import json
import os
from pathlib import Path
import socket
import tempfile
import unittest

from hq.desktop_control import MARKER, SubscriptionControl, ensure_desktop_subscription


class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        (self.directory / "desktop-managed").write_text(MARKER)
        self.endpoint = self.directory / "runtime.sock"
        self.path = self.directory / "desktop.sock"

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_receipt_private_socket_and_native_cli_remains_independent(self):
        ids = []
        server = SubscriptionControl(self.path, ids.append)
        try:
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            ensure_desktop_subscription(self.endpoint, "exact")
            self.assertEqual(ids, ["exact"])
            ensure_desktop_subscription(self.directory / "unmanaged/runtime.sock", "other")
            self.assertEqual(ids, ["exact"])
        finally:
            server.close()
        self.assertFalse(self.path.exists())
        with self.assertRaises(OSError):
            ensure_desktop_subscription(self.endpoint, "exact")

    def test_failed_or_invalid_handshake_never_claims_a_subscription(self):
        def fail(tid):
            raise RuntimeError("Desktop has not joined this chat")
        server = SubscriptionControl(self.path, fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "has not joined"):
                ensure_desktop_subscription(self.endpoint, "exact")
            for request in [{"thread_id": ""}, {"thread_id": "exact", "turn": "unwanted"}, []]:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(self.path))
                    client.sendall(json.dumps(request).encode() + b"\n")
                    self.assertIn("error", json.loads(client.recv(4096)))
        finally:
            server.close()

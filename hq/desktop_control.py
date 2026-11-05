"""Private subscription handshake; user turns still use Codex's native API."""
import json
from pathlib import Path
import os
import socket
import socketserver
import threading

MARKER = "hq.desktop-runtime.v1"


class SubscriptionControl:
    def __init__(self, path, subscribe):
        self.path = Path(path)
        self.path.unlink(missing_ok=True)
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(15)
                try:
                    line = self.rfile.readline(1025)
                    if len(line) > 1024:
                        raise ValueError("Subscription request too large")
                    value = json.loads(line)
                    if not isinstance(value, dict) or set(value) != {"thread_id"}:
                        raise ValueError("Expected an exact chat subscription")
                    tid = value["thread_id"]
                    if not isinstance(tid, str) or not tid or len(tid) > 200:
                        raise ValueError("Invalid chat id")
                    subscribe(tid)
                    result = {"subscribed": tid}
                except (OSError, ValueError, RuntimeError) as error:
                    result = {"error": str(error)}
                try:
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.server = Server(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.path.unlink(missing_ok=True)


def ensure_desktop_subscription(endpoint, thread_id):
    if not endpoint:
        return
    directory = Path(endpoint).parent
    marker = directory / "desktop-managed"
    if not marker.is_file() or marker.read_text() != MARKER:
        return
    # Runtime identity is tied to its private directory, never a global relay.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(15)
        connection.connect(str(directory / "desktop.sock"))
        connection.sendall(json.dumps({"thread_id": thread_id}).encode() + b"\n")
        with connection.makefile("rb") as stream:
            line = stream.readline(1025)
            if len(line) > 1024:
                raise RuntimeError("Invalid desktop subscription receipt")
            response = json.loads(line)
        if not isinstance(response, dict):
            raise RuntimeError("Invalid desktop subscription receipt")
        if response.get("subscribed") != thread_id:
            raise RuntimeError(response.get("error", "Desktop did not acknowledge this exact chat"))

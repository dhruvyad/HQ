"""Small RFC 6455 client for the documented local Codex Unix socket transport."""
import base64
import hashlib
import json
import os
import socket
import struct
import threading

MAX_FRAME = 16 * 1024 * 1024


class UnixWebSocket:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.lock = threading.Lock()
        self.buffer = b""
        try:
            self.socket.settimeout(10)
            self.socket.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            self.socket.sendall(request.encode())
            while b"\r\n\r\n" not in self.buffer:
                part = self.socket.recv(4096)
                if not part:
                    raise RuntimeError("Codex socket closed during the WebSocket handshake")
                self.buffer += part
                if len(self.buffer) > 32768:
                    raise RuntimeError("Codex WebSocket handshake is too large")
            headers, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            lines = headers.decode("ascii").split("\r\n")
            fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            fields = {k.lower(): v.strip() for k, v in fields.items()}
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (lines[0].split()[1] != "101" or fields.get("sec-websocket-accept") != expected
                    or fields.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in {v.strip().lower() for v in fields.get("connection", "").split(",")}):
                raise RuntimeError("Codex did not accept the WebSocket handshake")
            self.socket.settimeout(None)
        except Exception:
            self.socket.close()
            raise

    def exact(self, count):
        while len(self.buffer) < count:
            part = self.socket.recv(min(65536, count-len(self.buffer)))
            if not part:
                raise EOFError("Codex control connection closed")
            self.buffer += part
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def frame(self, opcode, payload):
        mask = os.urandom(4)
        size = len(payload)
        if size > MAX_FRAME:
            raise ValueError("Codex message is too large")
        length = bytes([0x80 | size]) if size < 126 else (b"\xfe" + struct.pack("!H", size) if size < 65536 else b"\xff" + struct.pack("!Q", size))
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        with self.lock:
            self.socket.sendall(bytes([0x80 | opcode]) + length + mask + masked)

    def send(self, value):
        self.frame(1, json.dumps(value).encode())

    def receive(self):
        parts, total, started = [], 0, False
        while True:
            head, length_byte = self.exact(2)
            fin, opcode = bool(head & 0x80), head & 0x0f
            if head & 0x70 or length_byte & 0x80:
                raise RuntimeError("Unsupported Codex WebSocket frame")
            size = length_byte & 0x7f
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size > MAX_FRAME or (opcode >= 8 and (size > 125 or not fin)):
                raise RuntimeError("Invalid Codex WebSocket frame size")
            payload = self.exact(size)
            if opcode == 8:
                raise EOFError("Codex control connection closed")
            if opcode == 9:
                self.frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not started:
                started = True
            elif opcode != 0 or not started:
                raise RuntimeError("Unsupported Codex WebSocket message")
            total += size
            if total > MAX_FRAME:
                raise RuntimeError("Codex message is too large")
            parts.append(payload)
            if fin:
                return json.loads(b"".join(parts))

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()

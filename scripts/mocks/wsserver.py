"""A server-side websocket for the `http.server` mocks (RFC 6455, framed by `websockets`).

An `http.server` handler that gets `Upgrade: websocket` hands its socket to `WebSocketPeer`.
The handshake and the framing come from the `websockets` sans-I/O protocol, so pings,
fragments and the close handshake behave as a real server's do. `run()` reads frames in the
handler's thread; `send()` and `close()` may be called from any thread.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from http.client import HTTPMessage

from websockets.frames import Opcode
from websockets.http11 import Request
from websockets.protocol import State
from websockets.server import ServerProtocol


class WebSocketPeer:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.protocol = ServerProtocol(max_size=1 << 20)
        self.lock = threading.Lock()
        self.close_code: int | None = None  # the code the client closed with

    def handshake(self, request_line: str, headers: HTTPMessage,
                  reject: tuple[int, str] | None = None) -> bool:
        """Answer the upgrade request. `reject` = (status, text) refuses it."""
        raw = request_line + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        self.protocol.receive_data(raw.encode("latin-1"))
        events = self.protocol.events_received()
        request = events[0] if events and isinstance(events[0], Request) else None
        if request is None or reject is not None:
            status, text = reject or (400, "Bad Request")
            response = self.protocol.reject(status, text)
        else:
            response = self.protocol.accept(request)
        self.protocol.send_response(response)
        self._flush()
        return self.protocol.state is State.OPEN

    def run(self, on_text: Callable[[str], None]) -> None:
        """Read frames until the connection ends; call `on_text` for each text message."""
        try:
            while self.protocol.state is not State.CLOSED:
                try:
                    data = self.sock.recv(65536)
                except OSError:
                    break
                with self.lock:
                    if data:
                        self.protocol.receive_data(data)
                    else:
                        self.protocol.receive_eof()
                    events = self.protocol.events_received()
                    self._flush()
                for frame in events:
                    if frame.opcode is Opcode.TEXT:
                        on_text(frame.data.decode())
                    elif frame.opcode is Opcode.CLOSE and self.close_code is None:
                        rcvd = self.protocol.close_rcvd
                        self.close_code = rcvd.code if rcvd else 1005
                if not data:
                    break
        finally:
            self.drop()

    def send(self, text: str) -> bool:
        with self.lock:
            if self.protocol.state is not State.OPEN:
                return False
            self.protocol.send_text(text.encode())
            return self._flush()

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Start the close handshake; `run()` ends when the client answers or leaves."""
        with self.lock:
            if self.protocol.state is State.OPEN:
                self.protocol.send_close(code, reason)
                self._flush()

    def drop(self) -> None:
        """End the TCP connection without a close frame."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _flush(self) -> bool:
        try:
            for chunk in self.protocol.data_to_send():
                if chunk:
                    self.sock.sendall(chunk)
                else:  # the protocol asks to half-close after the close frame
                    self.sock.shutdown(socket.SHUT_WR)
            return True
        except OSError:
            return False

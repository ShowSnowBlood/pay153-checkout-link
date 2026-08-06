"""Local HTTP proxy bridge with optional upstream CONNECT target pinning."""

from __future__ import annotations

import base64
import json
import os
import select
import socket
import socketserver


HEADER_LIMIT = 64 * 1024


def _required(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


UPSTREAM_HOST = _required("BRIDGE_UPSTREAM_HOST")
UPSTREAM_PORT = int(_required("BRIDGE_UPSTREAM_PORT"))
UPSTREAM_USER = _required("BRIDGE_UPSTREAM_USER")
UPSTREAM_PASSWORD = _required("BRIDGE_UPSTREAM_PASSWORD")
LISTEN_HOST = str(os.getenv("BRIDGE_LISTEN_HOST") or "127.0.0.1")
LISTEN_PORT = int(os.getenv("BRIDGE_LISTEN_PORT") or "18888")
PINNED_HOSTS = {
    str(host).lower(): str(address)
    for host, address in json.loads(os.getenv("BRIDGE_PINNED_HOSTS") or "{}").items()
}
UPSTREAM_AUTH = base64.b64encode(
    f"{UPSTREAM_USER}:{UPSTREAM_PASSWORD}".encode("utf-8")
).decode("ascii")


def _read_headers(connection: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(16 * 1024)
        if not chunk:
            raise ConnectionError("connection closed before headers")
        data.extend(chunk)
        if len(data) > HEADER_LIMIT:
            raise ValueError("request headers are too large")
    return bytes(data)


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, 60)
        if exceptional or not readable:
            return
        for source in readable:
            data = source.recv(64 * 1024)
            if not data:
                return
            target = right if source is left else left
            target.sendall(data)


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(30)
        try:
            headers = _read_headers(client)
            first_line = headers.split(b"\r\n", 1)[0].decode("latin-1")
            if first_line.startswith("CONNECT "):
                self._connect(first_line, client)
            else:
                self._http(headers, client)
        except Exception as exc:
            print(f"bridge error: {type(exc).__name__}: {exc}", flush=True)
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass

    @staticmethod
    def _upstream() -> socket.socket:
        connection = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), 20)
        connection.settimeout(None)
        return connection

    def _connect(self, first_line: str, client: socket.socket) -> None:
        authority = first_line.split(" ", 2)[1]
        host, separator, raw_port = authority.rpartition(":")
        if not separator:
            host, raw_port = authority, "443"
        port = int(raw_port)
        connect_host = PINNED_HOSTS.get(host.lower(), host)
        print(f"CONNECT {host}:{port} via {connect_host}:{port}", flush=True)
        with self._upstream() as upstream:
            upstream.sendall(
                (
                    f"CONNECT {connect_host}:{port} HTTP/1.1\r\n"
                    f"Host: {connect_host}:{port}\r\n"
                    f"Proxy-Authorization: Basic {UPSTREAM_AUTH}\r\n"
                    "Proxy-Connection: keep-alive\r\n\r\n"
                ).encode("ascii")
            )
            response = _read_headers(upstream)
            print(response.split(b"\r\n", 1)[0].decode("latin-1"), flush=True)
            client.sendall(response)
            if not response.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                return
            client.settimeout(None)
            _relay(client, upstream)

    def _http(self, headers: bytes, client: socket.socket) -> None:
        head, tail = headers.split(b"\r\n", 1)
        method = head.split(b" ", 1)[0].decode("latin-1", "replace")
        print(f"HTTP {method}", flush=True)
        with self._upstream() as upstream:
            upstream.sendall(
                head
                + b"\r\nProxy-Authorization: Basic "
                + UPSTREAM_AUTH.encode("ascii")
                + b"\r\n"
                + tail
            )
            client.settimeout(None)
            _relay(client, upstream)


class ThreadingBridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    with ThreadingBridge((LISTEN_HOST, LISTEN_PORT), BridgeHandler) as server:
        print(f"bridge listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()

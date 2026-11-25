"""Exercise resource ownership at real HTTP and WebSocket boundaries."""
from contextlib import contextmanager
import http.client
import os
import json
from pathlib import Path
import selectors
import socket
import subprocess
import tempfile
import time
import unittest

import test_proxy_context

read_frame = test_proxy_context.ProxyContextIntegrationTests.read_frame

FIXTURE = Path(__file__).resolve().parent / "resource-fixture"
TERRANE = os.environ.get("TERRANE", "terrane")


def wait_for(predicate, error):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(error)
        time.sleep(.01)


class ResourceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = subprocess.run([TERRANE, "build", str(FIXTURE)], capture_output=True, text=True)
        if build.returncode:
            raise RuntimeError(build.stderr)
        cls.binary = Path(build.stdout.strip().splitlines()[-1]).resolve()

    @contextmanager
    def running_server(self):
        with tempfile.TemporaryDirectory(prefix="http-resources-") as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            with (root / "stderr").open("w+") as errors:
                server = subprocess.Popen([self.binary], stdout=subprocess.PIPE, stderr=errors,
                                          text=True, env={**os.environ, "TMPDIR": str(uploads)})
                try:
                    with selectors.DefaultSelector() as ready:
                        ready.register(server.stdout, selectors.EVENT_READ)
                        self.assertTrue(ready.select(5), "server did not publish its bound address")
                    host, port = server.stdout.readline().strip().rsplit(":", 1)
                    yield (host, int(port)), root
                    self.assertIsNone(server.poll())
                finally:
                    server.terminate()
                    server.wait(timeout=5)
                    server.stdout.close()

    def test_disconnect_removes_completed_and_partial_uploads(self):
        with self.running_server() as (address, root):
            uploads = root / "uploads"
            host, port = address
            partial = (
                b'--stage\r\nContent-Disposition: form-data; name="file"; filename="first"\r\n\r\n'
                b'first\r\n--stage\r\nContent-Disposition: form-data; name="file"; filename="second"\r\n\r\n'
                + b"x" * 65536
            )
            with socket.create_connection(address, timeout=5) as client:
                client.sendall((
                    f"POST /upload HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    "Content-Type: multipart/form-data; boundary=stage\r\n"
                    f"Content-Length: {len(partial) + 4096}\r\n\r\n"
                ).encode() + partial)
                wait_for(lambda: sum(path.is_file() for path in uploads.glob("*/*")) == 2,
                         "did not stage both uploads before the body completed")
            wait_for(lambda: not any(uploads.iterdir()),
                     "disconnect leaked a temporary upload")

    def open_websocket(self, address, path):
        client = socket.create_connection(address, timeout=5)
        self.addCleanup(client.close)
        host, port = address
        client.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        headers = bytearray()
        while not headers.endswith(b"\r\n\r\n"):
            byte = client.recv(1)
            self.assertTrue(byte, headers)
            headers.extend(byte)
        self.assertTrue(headers.startswith(b"HTTP/1.1 101 "), headers)
        self.assertEqual(read_frame(client), (1, b"ready"))
        return client

    def post(self, address, path):
        connection = http.client.HTTPConnection(*address, timeout=5)
        try:
            connection.request("POST", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_group_broadcast_and_targeted_close_reach_idle_sockets(self):
        with self.running_server() as (address, _):
            first = self.open_websocket(address, "/first")
            second = self.open_websocket(address, "/second")
            self.assertEqual(self.post(address, "/broadcast"), (200, b"2"))
            self.assertEqual(read_frame(first), (1, b"hello"))
            self.assertEqual(read_frame(second), (1, b"hello"))
            self.assertEqual(self.post(address, "/remove-first"), (200, b"removed"))
            self.assertEqual(read_frame(first)[0], 8)
            self.assertEqual(self.post(address, "/broadcast"), (200, b"1"))
            self.assertEqual(read_frame(second), (1, b"hello"))

    def test_group_drop_drains_queued_messages_before_close(self):
        with self.running_server() as (address, _):
            client = self.open_websocket(address, "/queued")
            self.assertEqual(read_frame(client), (1, b"one"))
            self.assertEqual(read_frame(client), (1, b"two"))
            self.assertEqual(read_frame(client)[0], 8)

    def test_canceling_idle_managed_receive_releases_socket_and_endpoint(self):
        with self.running_server() as (address, _):
            client = self.open_websocket(address, "/cancel")
            self.assertEqual(client.recv(1), b"")
            self.assertEqual(self.post(address, "/broadcast"), (200, b"0"))

    def test_observer_completes_http_and_sees_upgrade(self):
        with self.running_server() as (address, root):
            connection = http.client.HTTPConnection(*address, timeout=5)
            try:
                for method, path, status, payload in [
                    ("GET", "/sized", 200, b"hello"),
                    ("GET", "/empty", 204, b""),
                    ("GET", "/finite", 200, b"chunk"),
                    ("POST", "/read", 200, b"read"),
                ]:
                    body = b'{"x":1}' if path == "/read" else None
                    connection.request(method, path, body, {"X-Observation": b"\xff"})
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.read()), (status, payload))
                connection.request("GET", "/pending")
                response = connection.getresponse()
                self.assertEqual(response.read(5), b"chunk")
                connection.close()
                response.close()
            finally:
                connection.close()

            with socket.create_connection(address, timeout=5) as client:
                client.sendall(
                    b"POST /ignore HTTP/1.1\r\nHost: localhost\r\n"
                    b"Connection: close\r\nContent-Length: 6\r\n\r\n"
                )
                with http.client.HTTPResponse(client) as ignored:
                    ignored.begin()
                    self.assertEqual((ignored.status, ignored.read()), (200, b"hello"))
            self.open_websocket(address, "/second")

            def events_by_path():
                events = [
                    json.loads(line.removeprefix("observer "))
                    for line in (root / "stderr").read_text().splitlines()
                    if line.startswith("observer {") and line.endswith("}")
                ]
                return {
                    head["path"]: [event for event in events if event["request_id"] == head["request_id"]]
                    for head in events if head["event"] == "request_head"
                }

            def observed(path, name, **fields):
                return [
                    event for event in events_by_path().get(path, ())
                    if event["event"] == name and all(event.get(k) == v for k, v in fields.items())
                ]

            for path in ("/sized", "/empty", "/finite", "/read", "/second"):
                wait_for(lambda: observed(path, "transport_complete", direction="response"),
                         f"no completed response for {path}")
                self.assertEqual(len(observed(path, "transport_complete", direction="response")), 1)
                self.assertFalse(observed(path, "response_dropped"))
            self.assertEqual(observed("/second", "response_head")[0]["status"], 101)
            self.assertTrue(observed("/empty", "request_body_complete", total_bytes=0))
            self.assertTrue(observed("/read", "request_body_complete", total_bytes=7, truncated=True,
                                     captured={"encoding": "utf8", "data": '{"x"'}))
            self.assertIn({"name": "x-observation", "value": {"encoding": "hex", "data": "ff"}},
                          observed("/sized", "request_head")[0]["headers"])
            self.assertTrue(observed("/finite", "response_data", bytes=5, truncated=True,
                                     data={"encoding": "utf8", "data": "chun"}))
            wait_for(lambda: observed("/pending", "response_dropped"), "pending stream did not drop")
            self.assertTrue(observed("/ignore", "request_body_dropped", total_bytes=0))


if __name__ == "__main__":
    unittest.main()

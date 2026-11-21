"""Exercise resource ownership at real HTTP and WebSocket boundaries."""
from contextlib import contextmanager
import http.client
import os
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
            with (root / "stderr").open("w+") as errors:
                server = subprocess.Popen([self.binary], stdout=subprocess.PIPE, stderr=errors,
                                          text=True, env={**os.environ, "TMPDIR": directory})
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
                wait_for(lambda: len(list(root.glob("terrane-upload-*/*"))) == 2,
                         "did not stage both uploads before the body completed")
            wait_for(lambda: not list(root.glob("terrane-upload-*")),
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


if __name__ == "__main__":
    unittest.main()

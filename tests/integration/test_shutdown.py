"""Exercise public graceful serving against real, deliberately stalled HTTP bodies."""
import http.client
import os
from pathlib import Path
import selectors
import socket
import subprocess
import tempfile
import time
import unittest
import test_proxy_context as websocket_helpers

PACKAGE = Path(__file__).resolve().parents[2]
FIXTURE = PACKAGE / "tests/integration/shutdown-fixture"
TERRANE = os.environ.get("TERRANE", "terrane")


class ShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([TERRANE, "build", str(FIXTURE)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.binary = Path(result.stdout.strip().splitlines()[-1]).resolve()

    def drain(self, maximum, fail=False, websocket=False):
        with tempfile.TemporaryDirectory(prefix="http-shutdown-") as directory:
            root = Path(directory)
            with (root / "stderr").open("w+") as errors:
                process = subprocess.Popen([str(self.binary), str(maximum)], cwd=root, stdout=subprocess.PIPE, stderr=errors, text=True)
                connection = None
                try:
                    with selectors.DefaultSelector() as ready:
                        ready.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(ready.select(10), "server did not publish its bound address")
                    addresses = [process.stdout.readline().strip().rsplit(":", 1) for _ in range(2)]
                    host, port = addresses[0][0], int(addresses[0][1])
                    second_host, second_port = addresses[1][0], int(addresses[1][1])
                    probe = http.client.HTTPConnection(second_host, second_port, timeout=5)
                    probe.request("GET", "/peer", headers={"Connection": "close"})
                    self.assertEqual(probe.getresponse().read(), b"127.0.0.1")
                    probe.close()
                    # Verify peer context survived the custom limited transport path.
                    probe = http.client.HTTPConnection(host, port, timeout=5)
                    probe.request("GET", "/peer", headers={"Connection": "close"})
                    self.assertEqual(probe.getresponse().read(), b"127.0.0.1")
                    probe.close()
                    if websocket:
                        connection = socket.create_connection((host, port), timeout=5)
                        connection.sendall((
                            f"GET /events HTTP/1.1\r\nHost: {host}:{port}\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                            "Sec-WebSocket-Version: 13\r\n\r\n"
                        ).encode())
                        upgrade = bytearray()
                        while not upgrade.endswith(b"\r\n\r\n"):
                            byte = connection.recv(1)
                            self.assertTrue(byte, "connection ended before upgrade")
                            upgrade.extend(byte)
                        self.assertTrue(upgrade.startswith(b"HTTP/1.1 101 "), upgrade)
                    else:
                        connection = http.client.HTTPConnection(host, port, timeout=5)
                        connection.request("GET", "/slow")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(len(b"started\n")), b"started\n")
                    if maximum == 1:
                        blocked = http.client.HTTPConnection(second_host, second_port, timeout=.2)
                        try:
                            blocked.request("GET", "/peer", headers={"Connection": "close"})
                            with self.assertRaises(TimeoutError):
                                blocked.getresponse()
                        finally:
                            blocked.close()
                    if fail:
                        (root / "fail-shutdown").touch()
                    (root / "shutdown").touch()
                    deadline = time.monotonic() + 5
                    while True:
                        open_ports = []
                        for address in ((host, port), (second_host, second_port)):
                            try:
                                with socket.create_connection(address, timeout=.1):
                                    open_ports.append(address)
                            except (ConnectionRefusedError, ConnectionResetError):
                                pass
                        if not open_ports:
                            break
                        if time.monotonic() >= deadline:
                            self.fail(f"shutdown did not close all listening sockets: {open_ports}")
                        time.sleep(.005)
                    self.assertIsNone(process.poll(), "shutdown cancelled the active response or upgrade")
                    if websocket:
                        helpers = websocket_helpers.ProxyContextIntegrationTests
                        helpers.send_frame(connection, 1, b"still-open")
                        self.assertEqual(helpers.read_frame(connection), (1, b"still-open"))
                        helpers.send_frame(connection, 8, b"\x03\xe8")
                        self.assertEqual(helpers.read_frame(connection)[0], 8)
                        connection.shutdown(socket.SHUT_WR)
                    else:
                        (root / "release").touch()
                        self.assertEqual(response.read(), b"finished\n")
                    code = process.wait(timeout=10)
                    errors.seek(0)
                    diagnostic = errors.read()
                    self.assertEqual(code, 0, diagnostic)
                    reported = process.stdout.read()
                    if fail:
                        self.assertIn("caught-shutdown-error:requested shutdown failure", reported)
                        self.assertNotIn("drained", reported)
                    else:
                        self.assertIn("drained", reported)
                finally:
                    if connection:
                        connection.close()
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    process.stdout.close()

    def test_unlimited_drains_streaming_body(self):
        self.drain(0)

    def test_limited_drains_streaming_body_at_capacity(self):
        self.drain(1)

    def test_shutdown_error_preserves_identity_after_drain(self):
        self.drain(1, fail=True)

    def test_unlimited_drains_upgraded_session(self):
        self.drain(0, websocket=True)

    def test_limited_drains_upgraded_session_at_capacity(self):
        self.drain(1, websocket=True)


if __name__ == "__main__":
    unittest.main()

"""Exercise trusted HTTP metadata and public WebSocket routing on a native server.

Run: python packages/http-server/tests/integration/test_proxy_context.py
"""
import http.client
import base64
import hashlib
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import tempfile
import unittest


PACKAGE = Path(__file__).resolve().parents[2]
ROOT = PACKAGE.parents[1]
COMPILER = Path(os.environ.get("TERRANE", ROOT / "target/release/terrane"))
CONSUMER = PACKAGE / ".trn/consumers/http-server"


class ProxyContextIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="http-proxy-context-")
        cls.addClassCleanup(cls.workspace.cleanup)
        cls.directory = Path(cls.workspace.name)
        (cls.directory / "src").mkdir()
        (cls.directory / "package.toml").write_text("\n".join([
            'package = "http-proxy-context-test"',
            'artifact = "executable"',
            '[namespaces]',
            '"http-proxy-context-test" = "src"',
            '[terrane-dependencies.streaky.http-server]',
            f'path = "{os.path.relpath(PACKAGE, cls.directory)}"',
            '[consumers.http-server]',
            f'command = "{os.path.relpath(CONSUMER, cls.directory)}"',
            'declarations = ["/http-proxy-context-test::proxy-api", '
            '"/http-proxy-context-test::proxy-api::inspect"]',
            '',
        ]))
        source = """namespace http-proxy-context-test
from /streaky/http-server/annotations import endpoint, route-group
from /streaky/http-server import api as http-api, create-application, websocket-session
from /generated/http-server import mount-annotated
from /core/networking import listen-tcp
from /core/errors import throwable, dependency-error
from /core/process import arguments

@[route-group; prefix = '/proxy']
class proxy-api uses http-api
    @[endpoint; path = '/context', content-type = 'text/plain']
    async function inspect string;
        request = this.request
        if request != none
            scheme string = 'none'
            client string = 'none'
            url string = 'none'
            trusted string = 'false'
            effective-scheme = request.scheme;
            if effective-scheme != none
                scheme = effective-scheme
            effective-client = request.client-ip;
            if effective-client != none
                client = effective-client
            effective-url = request.url;
            if effective-url != none
                url = effective-url
            if request.via-trusted-proxy;
                trusted = 'true'
            return trusted + '|' + client + '|' + scheme + '|' + url
        return 'missing'

async function echo-session none throws dependency-error; session websocket-session
    message = await session.receive;
    if message.kind == 'text'
        text = message.text
        if text != none
            await session.send-text; 'echo:' + text
    closing = await session.receive;
    ended = await session.receive;
    print; closing.kind + '|' + ended.kind
    return none

async function main throws throwable;
    app = create-application;
    argv = arguments;
    if argv.length > 0
        app = app.with-connection-limit; 2
    app = mount-annotated; app
    app = app.websocket; '/events', echo-session
    app = app.allow-direct; '127.0.0.1'
    app = app.allow-client; '198.51.100.0/24'
    listener = await listen-tcp; '127.0.0.1:0'
    app = app.with-address; 'not-a-listen-address'
    print; listener.local-address
    await app.serve-listener; move listener
main
"""
        (cls.directory / "src/main.trn").write_text(source)
        built = subprocess.run([str(COMPILER), "build", str(cls.directory)],
            capture_output=True, text=True, timeout=300)
        if built.returncode != 0:
            raise RuntimeError(built.stderr)
        cls.binary = built.stdout.strip().splitlines()[-1]
        cls.server, cls.port = cls.start_server()
        cls.addClassCleanup(cls.stop_process, cls.server)
        status, body = cls.request(headers={
            "Forwarded": "for=198.51.100.24;proto=https"})
        if status != 200 or body != f"true|198.51.100.24|https|https://127.0.0.1:{cls.port}/proxy/context":
            raise AssertionError(f"unexpected trusted context: {status} {body!r}")

    @classmethod
    def start_server(cls, *arguments):
        server = subprocess.Popen([cls.binary, *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            readable, _, _ = select.select([server.stdout], [], [], 20)
            if not readable:
                raise RuntimeError("Proxy-context server did not report its bound address")
            address = server.stdout.readline().strip().decode()
            host, port = address.rsplit(":", 1)
            if host != "127.0.0.1" or not 1024 <= int(port) <= 65535:
                raise AssertionError(f"unexpected bound address: {address!r}")
            return server, int(port)
        except BaseException:
            cls.stop_process(server)
            raise

    @staticmethod
    def stop_process(server):
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        server.stderr.close()
        server.stdout.close()

    @classmethod
    def request(cls, headers=None, source_address=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5,
            source_address=source_address)
        try:
            connection.request("GET", "/proxy/context", headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def test_trusted_forwarding_reaches_request_context(self):
        status, body = self.request(headers={
            "Forwarded": "for=198.51.100.24;proto=https"})
        self.assertEqual(status, 200)
        self.assertEqual(body,
            f"true|198.51.100.24|https|https://127.0.0.1:{self.port}/proxy/context")

    def test_untrusted_forwarded_spoof_is_denied_before_handler(self):
        status, body = self.request(headers={
            "Forwarded": "for=198.51.100.24;proto=https"},
            source_address=("127.0.0.2", 0))
        self.assertEqual(status, 403)
        self.assertNotIn("198.51.100.24", body)

    def websocket_handshake(self, source="127.0.0.1", port=None):
        port = self.port if port is None else port
        connection = socket.socket()
        self.addCleanup(connection.close)
        connection.settimeout(5)
        connection.bind((source, 0))
        connection.connect(("127.0.0.1", port))
        key = base64.b64encode(b"terrane-ws-test!").decode()
        connection.sendall((
            f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            "Forwarded: for=198.51.100.24;proto=https\r\n\r\n").encode())
        response = bytearray()
        while not response.endswith(b"\r\n\r\n"):
            byte = connection.recv(1)
            if not byte:
                raise AssertionError("connection ended before upgrade response")
            response.extend(byte)
        return connection, key, bytes(response)

    @staticmethod
    def send_frame(connection, opcode, payload):
        mask = b"\x01\x02\x03\x04"
        assert len(payload) < 126
        connection.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
            + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))

    @staticmethod
    def read_frame(connection):
        def exact(count):
            data = bytearray()
            while len(data) < count:
                chunk = connection.recv(count - len(data))
                if not chunk:
                    raise AssertionError("connection ended mid-frame")
                data.extend(chunk)
            return bytes(data)
        header = exact(2)
        if header[1] & 0x80:
            raise AssertionError("server must not mask WebSocket frames")
        length = header[1] & 0x7f
        if length == 126:
            length = struct.unpack("!H", exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", exact(8))[0]
        return header[0] & 0x0f, exact(length)

    def test_public_websocket_upgrade_text_close_and_stream_end(self):
        connection, key, response = self.websocket_handshake()
        self.assertTrue(response.startswith(b"HTTP/1.1 101 "), response)
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        headers = {name.lower(): value.strip()
            for name, value in (line.split(b":", 1)
                for line in response.split(b"\r\n")[1:] if b":" in line)}
        self.assertEqual(headers[b"sec-websocket-accept"], expected)
        self.send_frame(connection, 1, "café".encode())
        self.assertEqual(self.read_frame(connection), (1, "echo:café".encode()))
        self.send_frame(connection, 8, struct.pack("!H", 1000))
        self.assertEqual(self.read_frame(connection)[0], 8)
        connection.shutdown(socket.SHUT_WR)
        readable, _, _ = select.select([self.server.stdout], [], [], 5)
        self.assertTrue(readable, "session did not report close and stream-end events")
        self.assertEqual(self.server.stdout.readline().strip(), b"Close|closed")

    def test_websocket_upgrade_obeys_access_policy(self):
        _, _, response = self.websocket_handshake("127.0.0.2")
        self.assertTrue(response.startswith(b"HTTP/1.1 403 "), response)

    def test_limit_covers_keepalive_upgrade_and_failed_connection(self):
        server, port = self.start_server("limited")
        self.addCleanup(self.stop_process, server)
        denied, _, response = self.websocket_handshake("127.0.0.2", port)
        self.assertTrue(response.startswith(b"HTTP/1.1 403 "), response)
        denied.close()
        headers = {"Forwarded": "for=198.51.100.24;proto=https"}

        def request(close=False):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", "/proxy/context",
                headers={**headers, **({"Connection": "close"} if close else {})})
            return connection

        def response(connection):
            result = connection.getresponse()
            self.assertEqual(result.status, 200)
            self.assertEqual(result.read().decode(),
                f"true|198.51.100.24|https|https://127.0.0.1:{port}/proxy/context")

        first = request()
        response(first)
        upgraded, _, handshake = self.websocket_handshake(port=port)
        self.assertTrue(handshake.startswith(b"HTTP/1.1 101 "), handshake)
        self.send_frame(upgraded, 1, b"held")
        self.assertEqual(self.read_frame(upgraded), (1, b"echo:held"))

        third = request()
        self.assertFalse(select.select([third.sock], [], [], 0.2)[0],
            "a third TCP connection ran while keep-alive and WebSocket occupied both slots")
        first.close()
        response(third)

        fourth = request(close=True)
        self.assertFalse(select.select([fourth.sock], [], [], 0.2)[0],
            "the upgrade released its connection slot before the WebSocket ended")
        self.send_frame(upgraded, 8, struct.pack("!H", 1000))
        self.assertEqual(self.read_frame(upgraded)[0], 8)
        upgraded.shutdown(socket.SHUT_WR)
        response(fourth)
        upgraded.close()

        # With the third keep-alive still occupying one slot, a protocol error must
        # release the other slot and leave its worker able to accept another client.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as malformed:
            malformed.sendall(b"not http\r\n\r\n")
            self.assertTrue(malformed.recv(4096).startswith(b"HTTP/1.1 400 "))
        response(request(close=True))


if __name__ == "__main__":
    unittest.main()

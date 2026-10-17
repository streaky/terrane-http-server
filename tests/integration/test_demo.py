"""Exercise the built class-based catalog demo over real HTTP.

Run: python packages/http-server/tests/integration/test_demo.py /path/to/app
"""
import base64
import hashlib
import http.client
from html.parser import HTMLParser
import json
from pathlib import Path
import socket
import shutil
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from xml.etree import ElementTree
import yaml


BINARY = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else None
DEMO = Path(__file__).resolve().parents[2] / "demo"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.titles = []
        self.headings = []
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag in ("title", "h1"):
            self.active = self.titles if tag == "title" else self.headings
        if tag == "a":
            self.links.append(dict(attrs).get("href"))

    def handle_data(self, data):
        if self.active is not None:
            self.active.append(data)

    def handle_endtag(self, tag):
        if tag in ("title", "h1"):
            self.active = None


class DemoHttpTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if BINARY is None:
            raise RuntimeError("Pass the built demo executable as the first argument")
        cls.runtime = tempfile.TemporaryDirectory(prefix="http-demo-runtime-")
        cls.addClassCleanup(cls.runtime.cleanup)
        shutil.copytree(DEMO / "public", Path(cls.runtime.name) / "public")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.server = subprocess.Popen([str(BINARY), f"127.0.0.1:{cls.port}"], cwd=cls.runtime.name,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError(cls.server.stderr.read().decode())
            try:
                status, _, body = cls.request("GET", "/api/health")
                if status == 200 and body == "ok":
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        raise RuntimeError("Demo did not become ready")

    @classmethod
    def stop_server(cls):
        if cls.server.poll() is None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.server.kill()
                cls.server.wait()
        cls.server.stderr.close()

    @classmethod
    def response(cls, method, path, body=None, content_type="application/json", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        try:
            request_headers = dict(headers or {})
            if body is not None:
                request_headers["Content-Type"] = content_type
            connection.request(method, path, body=body, headers=request_headers)
            return connection, connection.getresponse()
        except BaseException:
            connection.close()
            raise

    @classmethod
    def request(cls, method, path, body=None, content_type="application/json", headers=None):
        connection, response = cls.response(method, path, body, content_type, headers)
        try:
            payload = response.read().decode()
            response_type = response.getheader("Content-Type")
            parsed = json.loads(payload) if payload and response_type and response_type.startswith("application/json") else payload
            return response.status, response_type, parsed
        finally:
            response.close()
            connection.close()


class CatalogHttpTests(DemoHttpTestCase):
    def test_negotiated_yaml_and_xml_responses(self):
        path = "/api/search?term=notebook&limit=2"
        expected = self.request("GET", path)[2]
        status, content_type, body = self.request("GET", path, headers={"Accept": "application/yaml"})
        self.assertEqual((status, content_type), (200, "application/yaml"))
        self.assertEqual(yaml.safe_load(body), expected)
        status, content_type, body = self.request("GET", path, headers={"Accept": "application/xml"})
        self.assertEqual((status, content_type), (200, "application/xml"))
        root = ElementTree.fromstring(body)
        self.assertEqual(root.tag, "response")
        schema = self.request("GET", "/api/openapi.json")[2]
        default_content_type = schema["paths"]["/api/search"]["get"]["x-default-response-content-type"]
        default_status, actual_default_type, actual_default = self.request("GET", path)
        self.assertEqual(default_status, 200)
        self.assertEqual(actual_default_type, default_content_type)
        self.assertEqual(actual_default, expected)
        self.assertEqual(root.findtext("items/item/name"), expected["items"][0]["name"])
        self.assertEqual(root.findtext("items/item/price-cents"), str(expected["items"][0]["price-cents"]))
        status, content_type, body = self.request("GET", "/api/health", headers={"Accept": "application/xml"})
        self.assertEqual((status, content_type), (200, "application/xml"))
        self.assertEqual(ElementTree.fromstring(body).text, "ok")
        self.assertEqual(self.request("GET", "/api/health", headers={"Accept": "image/png"})[0], 406)
        status, content_type, body = self.request("GET", "/api/health",
            headers={"Accept": "application/json;q=0,application/yaml;q=0.5,*/*;q=0.1"})
        self.assertEqual((status, content_type, yaml.safe_load(body)), (200, "application/yaml", "ok"))

    def test_head_preserves_negotiated_get_headers_without_a_body(self):
        for path in ["/api/items/1", "/api/items/1?detailed=true",
                     "/api/items/99", "/api/items/abc", "/api/items/1?detailed=invalid"]:
            for accept in ["application/json", "application/yaml", "application/xml", "image/png"]:
                with self.subTest(path=path, accept=accept):
                    headers = {"Accept": accept}
                    connection, response = self.response("GET", path, headers=headers)
                    try:
                        expected_status = response.status
                        body = response.read()
                        expected_headers = {name: response.getheader(name)
                            for name in ("Content-Type", "Content-Length", "Vary")}
                        self.assertEqual(int(expected_headers["Content-Length"]), len(body))
                    finally:
                        response.close()
                        connection.close()
                    connection, response = self.response("HEAD", path, headers=headers)
                    try:
                        self.assertEqual(response.status, expected_status)
                        self.assertEqual(response.read(), b"")
                        self.assertEqual({name: response.getheader(name) for name in expected_headers},
                            expected_headers)
                    finally:
                        response.close()
                        connection.close()

    def test_utf8_charset_parameters_select_xml_and_yaml(self):
        for media_type in ("application/xml", "application/yaml"):
            for charset in ("utf-8", '"UTF-8"'):
                with self.subTest(media_type=media_type, charset=charset):
                    status, response_type, _ = self.request("GET", "/api/health",
                        headers={"Accept": f"{media_type}; charset={charset}"})
                    self.assertEqual((status, response_type), (200, media_type))
            self.assertEqual(self.request("GET", "/api/health",
                headers={"Accept": f"{media_type}; charset=iso-8859-1"})[0], 406)

    def test_negotiated_responses_include_vary_accept(self):
        for method, path, body, headers, expected_status in [
            ("GET", "/api/health", None, {"Accept": "application/json"}, 200),
            ("GET", "/api/items/99", None, {"Accept": "application/json"}, 404),
            ("GET", "/api/items/not-an-int", None, {"Accept": "application/json"}, 422),
            ("GET", "/api/items/%FF", None, {"Accept": "application/json"}, 422),
            ("GET", "/api/health", None, {"Accept": "image/png"}, 406),
            ("GET", "/api/items/not-an-int", None, {"Accept": "image/png"}, 406),
        ]:
            with self.subTest(path=path, expected_status=expected_status):
                connection, response = self.response(method, path, body, headers=headers)
                try:
                    self.assertEqual(response.status, expected_status)
                    self.assertIn("accept", {value.strip().lower() for value in response.getheader("Vary", "").split(",")})
                    response.read()
                finally:
                    response.close()
                    connection.close()

    def test_authored_examples_match_real_requests_and_negotiated_responses(self):
        _, _, schema = self.request("GET", "/api/openapi.json")
        quote = schema["paths"]["/api/orders/quote"]["post"]
        request_example = quote["requestBody"]["content"]["application/json"]["examples"]["three-notebooks"]["value"]
        content = quote["responses"]["200"]["content"]
        self.assertEqual(set(content), {"application/json", "application/yaml", "application/xml"})
        for media_type, media in content.items():
            expected = media["examples"]["three-notebooks"]["value"]
            status, actual_type, actual = self.request("POST", "/api/orders/quote",
                json.dumps(request_example), headers={"Accept": media_type})
            self.assertEqual((status, actual_type), (200, media_type))
            if media_type == "application/yaml":
                actual = yaml.safe_load(actual)
            if media_type == "application/xml":
                self.assertEqual(ElementTree.tostring(ElementTree.fromstring(actual)),
                    ElementTree.tostring(ElementTree.fromstring(expected)))
            else:
                self.assertEqual(actual, expected)
        item = schema["paths"]["/api/items/{item-id}"]["get"]
        parameters = {p["name"]: p["examples"]["default"]["value"] for p in item["parameters"]}
        path = f'/api/items/{parameters["item-id"]}?detailed={str(parameters["detailed"]).lower()}'
        self.assertEqual(self.request("GET", path)[2],
            item["responses"]["200"]["content"]["application/json"]["examples"]["detailed"]["value"])

    def test_api_routes_and_scalar_defaults(self):
        self.assertEqual(self.request("POST", "/api/health")[0], 405)
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        status, content_type, item = self.request("GET", "/api/items/1")
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertEqual(item, {"id": 1, "name": "Terrane notebook", "price-cents": 1200, "in-stock": False})
        self.assertTrue(self.request("GET", "/api/items/1?detailed=true")[2]["in-stock"])
        self.assertEqual(self.request("GET", "/api/search?term=Terrane+notebook")[2],
            {"term": "Terrane notebook", "limit": 10, "matches": True, "items": [{"id": 1, "name": "Terrane notebook", "price-cents": 1200, "in-stock": True}]})
        self.assertEqual(self.request("GET", "/api/search?term=notebook&limit=0")[2]["matches"], False)

    def test_missing_catalog_items_return_http_error_status(self):
        status, content_type, body = self.request("GET", "/api/items/99")
        self.assertEqual(status, 404)
        self.assertTrue(content_type.startswith("text/plain"))
        self.assertEqual(body, "Catalog item not found")
        status, _, body = self.request("POST", "/api/orders/quote", '{"item-id":99,"quantity":1}')
        self.assertEqual(status, 404)
        self.assertEqual(body, "Catalog item not found")

    def test_invalid_parameters_and_typed_bodies_return_422(self):
        for path in ["/api/items/not-an-int", "/api/items/1?detailed=maybe", "/api/search", "/api/search?term=x&limit=oops"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 422)
        for body in ["", "{broken", '{"item-id":"1","quantity":2}', '{"item-id":1,"quantity":2,"extra":true}']:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/orders/quote", body)[0], 422)

    def test_order_quote_is_typed_and_stateless(self):
        status, _, quote = self.request("POST", "/api/orders/quote", '{"item-id":1,"quantity":3}')
        self.assertEqual(status, 200)
        self.assertEqual(quote, {"item-id": 1, "quantity": 3, "total-cents": 3600})
        self.assertEqual(self.request("POST", "/api/orders/quote", '{"item-id":1}')[2],
            {"item-id": 1, "quantity": 0, "total-cents": 0})

    def test_json_utf8_and_body_size_boundary(self):
        self.assertEqual(self.request("POST", "/api/orders/quote", b'{"item-id":1,"quantity":1,"invalid":"\xff"}')[0], 422)
        self.assertEqual(self.request("POST", "/api/orders/quote", b'{"item-id":1}' + b' ' * 2097100)[0], 200)
        self.assertEqual(self.request("POST", "/api/orders/quote", b'{}' + b' ' * 2097151)[0], 422)

    def test_websocket_echo_messages_and_clean_peer_close(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            key = base64.b64encode(b"terrane-ws-test!").decode()
            connection.sendall((
                f"GET /ws/echo HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            with connection.makefile("rb") as stream:
                self.assertEqual(stream.readline().split()[1], b"101")
                headers = {}
                while True:
                    line = stream.readline()
                    if line == b"\r\n":
                        break
                    self.assertNotEqual(line, b"", "connection ended during upgrade")
                    name, value = line.split(b":", 1)
                    headers[name.lower()] = value.strip()
                expected_accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                self.assertEqual(headers[b"sec-websocket-accept"], expected_accept)

                def read_exact(count):
                    data = stream.read(count)
                    self.assertEqual(len(data), count, "connection ended mid-frame")
                    return data

                def read_frame():
                    header = read_exact(2)
                    self.assertEqual(header[1] & 0x80, 0, "server frame must not be masked")
                    length = header[1] & 0x7f
                    if length == 126:
                        length = struct.unpack("!H", read_exact(2))[0]
                    elif length == 127:
                        length = struct.unpack("!Q", read_exact(8))[0]
                    return header[0] & 0x0f, read_exact(length)

                def send_frame(opcode, payload):
                    mask = b"\x01\x02\x03\x04"
                    connection.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
                        + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))

                for text in ("café <b>&</b>", "second", ""):
                    with self.subTest(message=text):
                        payload = text.encode("utf-8")
                        send_frame(1, payload)
                        self.assertEqual(read_frame(), (1, b"echo: " + payload))
                send_frame(8, struct.pack("!H", 1000))
                opcode, payload = read_frame()
                self.assertEqual(opcode, 8)
                self.assertEqual(struct.unpack("!H", payload[:2])[0], 1000)
                self.assertEqual(stream.read(1), b"")
        self.assertEqual(self.request("GET", "/api/health"), (200, "application/json", "ok"))
        for path, media_type in (
            ("/websocket", "text/html"),
            ("/assets/websocket.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                status, content_type, _ = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(content_type.split(";")[0], media_type)

    def test_html_and_static_files(self):
        for path, title, heading, links in [
            ("/", "Terrane Catalog", "Terrane Catalog", {"/docs/", "/about", "/api/health"}),
            ("/about", "About — Terrane Catalog", "About this demo", {"/"}),
        ]:
            status, content_type, page = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(content_type.startswith("text/html"))
            parsed = PageParser()
            parsed.feed(page)
            self.assertEqual("".join(parsed.titles), title)
            self.assertEqual("".join(parsed.headings), heading)
            self.assertTrue(links.issubset(parsed.links))
        status, content_type, page = self.request("GET", "/docs/")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        self.assertIn('apiDescriptionUrl="/api/openapi.json"', page)
        status, content_type, css = self.request("GET", "/assets/site.css")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/css"))
        self.assertIn(":root", css)

    def test_openapi_uses_mounted_paths_and_typed_model_schemas(self):
        status, content_type, schema = self.request("GET", "/api/openapi.json")
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertEqual(schema["openapi"], "3.1.0")
        self.assertEqual(schema["servers"], [{"url": "/"}])
        overview = schema["info"]["description"]
        self.assertIn("curl", overview)
        self.assertIn("application/yaml", overview)
        self.assertIn("```console", overview)
        def resolve(value):
            if "$ref" in value:
                target = resolve(schema["components"]["schemas"][value["$ref"].split("/")[-1]])
                return {**target, **{key: item for key, item in value.items() if key != "$ref"}}
            return value

        paths = schema["paths"]
        self.assertEqual(set(paths), {"/api/health", "/api/items/{item-id}", "/api/search",
            "/api/orders/quote", "/api/request"})
        self.assertNotEqual(paths["/api/items/{item-id}"]["get"]["operationId"],
            paths["/api/items/{item-id}"]["head"]["operationId"])
        resource = paths["/api/items/{item-id}"]
        self.assertEqual(set(resource), {"get", "put", "patch", "delete", "head", "options"})
        self.assertNotIn("content", resource["head"]["responses"]["200"])
        replacement = resolve(resource["put"]["requestBody"]["content"]["application/json"]["schema"])
        patch = resolve(resource["patch"]["requestBody"]["content"]["application/json"]["schema"])
        self.assertEqual(replacement["properties"]["price-cents"]["type"], "integer")
        self.assertEqual(patch["properties"]["in-stock"]["anyOf"], [{"type": "boolean"}, {"type": "null"}])
        item = paths["/api/items/{item-id}"]["get"]
        parameters = {(p["in"], p["name"]): p for p in item["parameters"]}
        self.assertTrue(parameters["path", "item-id"]["required"])
        self.assertEqual(parameters["query", "detailed"]["schema"], {"type": "boolean", "default": False})
        search = paths["/api/search"]["get"]
        self.assertEqual(search["parameters"][1]["schema"]["default"], 10)
        search_schema = resolve(search["responses"]["200"]["content"]["application/json"]["schema"])
        items_schema = resolve(search_schema["properties"]["items"])
        item_schema = resolve(items_schema["items"])
        self.assertEqual(items_schema["type"], "array")
        self.assertEqual(resolve(item_schema)["properties"]["price-cents"]["type"], "integer")
        quote = paths["/api/orders/quote"]["post"]
        body_schema = resolve(quote["requestBody"]["content"]["application/json"]["schema"])
        self.assertEqual(body_schema["properties"]["item-id"]["type"], "integer")
        self.assertEqual(resolve(quote["responses"]["200"]["content"]["application/json"]["schema"])["properties"]["total-cents"]["type"], "integer")
        self.assertNotIn("/", paths)
        operations = [operation["operationId"] for operations in paths.values() for operation in operations.values()]
        self.assertEqual(len(operations), len(set(operations)))
        status, xml_type, xml_body = self.request("GET", "/api/search?term=notebook&limit=2",
            headers={"Accept": "application/xml"})
        self.assertEqual((status, xml_type), (200, "application/xml"))
        xml_root = ElementTree.fromstring(xml_body)
        xml_schema = resolve(search["responses"]["200"]["content"]["application/xml"]["schema"])
        self.assertEqual(xml_root.tag, xml_schema["xml"]["name"])
        self.assertEqual(xml_root.find("items").tag, items_schema["xml"].get("name", "items"))
        self.assertTrue(items_schema["xml"]["wrapped"])
        self.assertEqual(xml_root.find("items/item").tag, resolve(items_schema["items"])["xml"]["name"])

    def test_openapi_exports_comments_for_inputs_outputs_and_nested_fields(self):
        _, _, schema = self.request("GET", "/api/openapi.json")
        paths = schema["paths"]
        for path, method in [
            ("/api/health", "get"), ("/api/items/{item-id}", "get"),
            ("/api/search", "get"), ("/api/orders/quote", "post"),
            ("/api/items/{item-id}", "put"), ("/api/items/{item-id}", "patch"),
            ("/api/items/{item-id}", "delete"), ("/api/items/{item-id}", "head"),
            ("/api/items/{item-id}", "options"),
        ]:
            operation = paths[path][method]
            with self.subTest(path=path):
                self.assertNotIn("@returns", operation["description"])
                response = operation["responses"].get("200", operation["responses"].get("default"))
                self.assertTrue(response["description"].strip())
                for parameter in operation["parameters"]:
                    self.assertIn("description", parameter)
        quote = paths["/api/orders/quote"]["post"]
        self.assertIn("description", quote["requestBody"])
        for model in schema["components"]["schemas"].values():
            self.assertIn("description", model)
            for field in model["properties"].values():
                self.assertIsInstance(field.get("description"), str)
                self.assertNotEqual(field["description"].strip(), "")
                self.assertNotEqual(field["description"], "none")


class RequestContextTests(DemoHttpTestCase):
    def test_request_snapshot_and_status_override(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        target = "/api/request?status=201&note=a%20b"
        connection.putrequest("GET", target)
        connection.putheader("X-Tag", "first")
        connection.putheader("X-Tag", "second")
        connection.putheader("Cookie", "demo=abc==; other=z")
        connection.putheader("X-Request-ID", "snapshot")
        connection.putheader("X-Forwarded-For", "198.51.100.7")
        connection.putheader("X-Forwarded-Proto", "https")
        connection.endheaders()
        response = connection.getresponse()
        try:
            result = json.loads(response.read())
            self.assertEqual(response.status, 201)
            self.assertEqual(response.getheader("X-Request-ID"), "snapshot")
            self.assertEqual(result, {
                "method": "GET", "target": target, "path": "/api/request",
                "route-pattern": "/api/request", "http-version": "HTTP/1.1",
                "scheme": "http", "authority": f"127.0.0.1:{self.port}",
                "peer-ip": "127.0.0.1", "client-ip": "127.0.0.1",
                "via-trusted-proxy": False, "tags": ["first", "second"], "cookie": "abc==",
            })
        finally:
            response.close()
            connection.close()

    def test_concurrent_status_and_header_overrides_are_request_local(self):
        from concurrent.futures import ThreadPoolExecutor

        def inspect(index):
            marker = f"request-{index}"
            status = 200 + index % 3
            connection, response = self.response("GET", f"/api/request?status={status}",
                headers={"X-Request-ID": marker, "Cookie": f"demo={marker}"})
            try:
                data = json.loads(response.read())
                return response.status, response.getheader("X-Request-ID"), data["cookie"], data["target"]
            finally:
                response.close()
                connection.close()
        with ThreadPoolExecutor(max_workers=8) as clients:
            actual = list(clients.map(inspect, range(24)))
        self.assertEqual(actual, [
            (200 + i % 3, f"request-{i}", f"request-{i}", f"/api/request?status={200 + i % 3}")
            for i in range(24)
        ])
        self.assertIsNone(self.response_header_after_plain_request("X-Request-ID"))

    def response_header_after_plain_request(self, name):
        connection, response = self.response("GET", "/api/request")
        try:
            self.assertEqual(response.status, 200)
            response.read()
            return response.getheader(name)
        finally:
            response.close()
            connection.close()

    def test_metadata_only_head_and_redirect_without_exceptions(self):
        connection, response = self.response("GET", "/catalog-note")
        try:
            body = response.read()
            expected_length = response.getheader("Content-Length")
            self.assertEqual(int(expected_length), len(body))
            self.assertEqual(response.getheader("Content-Type"), "text/plain")
        finally:
            response.close()
            connection.close()
        connection, response = self.response("HEAD", "/catalog-note")
        try:
            self.assertEqual((response.status, response.read()), (200, b""))
            self.assertEqual(response.getheader("Content-Length"), expected_length)
            self.assertEqual(response.getheader("Content-Type"), "text/plain")
        finally:
            response.close()
            connection.close()
        connection, response = self.response("GET", "/catalog")
        try:
            self.assertEqual((response.status, response.getheader("Location"), response.read()),
                (303, "/", b""))
        finally:
            response.close()
            connection.close()


class CatalogMutationTests(DemoHttpTestCase):
    def test_put_patch_delete_change_subsequent_reads_searches_and_quotes(self):
        path = "/api/items/2"
        self.assertEqual(self.request("HEAD", path)[::2], (200, ""))
        connection, response = self.response("OPTIONS", path)
        try:
            self.assertEqual((response.status, response.read()), (204, b""))
            self.assertIsNone(response.getheader("Content-Type"))
            self.assertEqual({method.strip() for method in response.getheader("Allow").split(",")},
                {"GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
        finally:
            response.close()
            connection.close()

        replacement = {"name": "Terrane pencil", "price-cents": 300, "in-stock": True}
        expected = {"id": 2, **replacement}
        self.assertEqual(self.request("PUT", path, json.dumps(replacement))[2], expected)
        self.assertEqual(self.request("GET", path + "?detailed=true")[2], expected)
        self.assertEqual(self.request("POST", "/api/orders/quote",
            '{"item-id":2,"quantity":3}')[2], {"item-id": 2, "quantity": 3, "total-cents": 900})

        for invalid in ['{"price-cents":true}', '{"unknown":1}', "{broken"]:
            with self.subTest(invalid=invalid):
                self.assertEqual(self.request("PATCH", path, invalid)[0], 422)
                self.assertEqual(self.request("GET", path + "?detailed=true")[2], expected)

        expected.update({"name": "Terrane sketchbook", "price-cents": 0, "in-stock": False})
        self.assertEqual(self.request("PATCH", path,
            '{"name":"Terrane sketchbook","price-cents":0,"in-stock":false}')[2], expected)
        self.assertEqual(self.request("PATCH", path, '{"name":null}')[2], expected)
        self.assertEqual(self.request("PATCH", path, "{}")[2], expected)
        self.assertEqual(self.request("GET", path + "?detailed=true")[2], expected)
        self.assertEqual(self.request("GET", "/api/search?term=sketchbook")[2]["items"], [expected])
        self.assertEqual(self.request("GET", "/api/search?term=pencil")[2]["items"], [])
        self.assertEqual(self.request("POST", "/api/orders/quote",
            '{"item-id":2,"quantity":3}')[2]["total-cents"], 0)

        self.request("PATCH", path, '{"price-cents":350,"in-stock":true}')
        reset = {"id": 2, "name": "Minimal item", "price-cents": 0, "in-stock": False}
        self.assertEqual(self.request("PUT", path, '{"name":"Minimal item"}')[2], reset)
        self.assertEqual(self.request("DELETE", path), (204, None, ""))
        self.assertEqual(self.request("GET", "/api/search?term=Minimal")[2]["items"], [])
        for method, body in [("GET", None), ("HEAD", None), ("DELETE", None),
                             ("PUT", json.dumps(replacement)), ("PATCH", "{}")]:
            with self.subTest(method=method):
                self.assertEqual(self.request(method, path, body)[0], 404)
        self.assertEqual(self.request("POST", "/api/orders/quote",
            '{"item-id":2,"quantity":1}')[0], 404)
        self.assertEqual(self.request("OPTIONS", path), (204, None, ""))

    def test_concurrent_patches_preserve_independent_field_updates(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        ready = Barrier(2)
        def patch(changes):
            ready.wait(timeout=5)
            return self.request("PATCH", "/api/items/1", json.dumps(changes))[0]

        with ThreadPoolExecutor(max_workers=2) as clients:
            name = clients.submit(patch, {"name": "Updated notebook"})
            price = clients.submit(patch, {"price-cents": 1700})
            self.assertEqual((name.result(), price.result()), (200, 200))
        self.assertEqual(self.request("GET", "/api/items/1?detailed=true")[2],
            {"id": 1, "name": "Updated notebook", "price-cents": 1700, "in-stock": True})


if __name__ == "__main__":
    unittest.main()

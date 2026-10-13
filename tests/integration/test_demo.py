"""Exercise the built class-based catalog demo over real HTTP.

Run: python packages/http-server/tests/integration/test_demo.py /path/to/app
"""
import http.client
from html.parser import HTMLParser
import json
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


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


class CatalogHttpTests(unittest.TestCase):
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
    def request(cls, method, path, body=None, content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        try:
            headers = {"Content-Type": content_type} if body is not None else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read().decode()
            response_type = response.getheader("Content-Type")
            parsed = json.loads(payload) if response_type and response_type.startswith("application/json") else payload
            return response.status, response_type, parsed
        finally:
            connection.close()

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
        def resolve(value):
            if "$ref" in value:
                return schema["components"]["schemas"][value["$ref"].split("/")[-1]]
            return value

        paths = schema["paths"]
        self.assertEqual(set(paths), {"/api/health", "/api/items/{item-id}", "/api/search", "/api/orders/quote"})
        item = paths["/api/items/{item-id}"]["get"]
        parameters = {(p["in"], p["name"]): p for p in item["parameters"]}
        self.assertTrue(parameters["path", "item-id"]["required"])
        self.assertEqual(parameters["query", "detailed"]["schema"], {"type": "boolean", "default": False})
        search = paths["/api/search"]["get"]
        self.assertEqual(search["parameters"][1]["schema"]["default"], 10)
        search_schema = resolve(search["responses"]["200"]["content"]["application/json"]["schema"])
        self.assertEqual(search_schema["properties"]["items"]["type"], "array")
        self.assertEqual(resolve(search_schema["properties"]["items"]["items"])["properties"]["price-cents"], {"type": "integer"})
        quote = paths["/api/orders/quote"]["post"]
        body_schema = resolve(quote["requestBody"]["content"]["application/json"]["schema"])
        self.assertEqual(body_schema["properties"]["item-id"]["type"], "integer")
        self.assertEqual(resolve(quote["responses"]["200"]["content"]["application/json"]["schema"])["properties"]["total-cents"]["type"], "integer")
        self.assertNotIn("/", paths)
        operations = [operation["operationId"] for operations in paths.values() for operation in operations.values()]
        self.assertEqual(len(operations), len(set(operations)))


if __name__ == "__main__":
    unittest.main()

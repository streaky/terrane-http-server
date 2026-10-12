"""Consumer diagnostics through real Terrane package preparation.

Run after build-consumer.sh: python packages/http-server/tests/integration/test_consumer.py
"""
from pathlib import Path
import http.client
import json
import os
import subprocess
import socket
import tempfile
import unittest
import time


PACKAGE = Path(__file__).resolve().parents[2]
ROOT = PACKAGE.parents[1]
COMPILER = Path(os.environ.get("TERRANE", ROOT / "target/debug/terrane"))
CONSUMER = PACKAGE / ".trn/consumers/http-server"


class ConsumerDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="http-annotations-")
        cls.addClassCleanup(cls.workspace.cleanup)
        cls.directory = Path(cls.workspace.name)
        (cls.directory / "src").mkdir()

    def check(self, source, selections, message=None, code="S2061"):
        manifest = '\n'.join([
            'package = "http-annotation-test"',
            '[namespaces]',
            'http-annotation-test = "src"',
            '[terrane-dependencies]',
            f'http-server = {{ path = "{os.path.relpath(PACKAGE, self.directory)}" }}',
            '[consumers.http-server]',
            f'command = "{os.path.relpath(CONSUMER, self.directory)}"',
            'declarations = [' + ', '.join(f'"/http-annotation-test::{name}"' for name in selections) + ']',
        ])
        (self.directory / "package.toml").write_text(manifest)
        (self.directory / "src/main.trn").write_text(
            "namespace http-annotation-test\nfrom /streaky/http-server/annotations import endpoint, path, query, body\n"
            + source
            + "\nfunction main int;\n    return 0\n"
        )
        result = subprocess.run([str(COMPILER), "check", str(self.directory)], capture_output=True, text=True, timeout=300)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(code, result.stderr)
        if message is not None:
            self.assertIn(message, result.stderr)

    def test_duplicate_method_path(self):
        self.check("@[endpoint; path = '/same']\nasync function first string;\n    return 'a'\n@[endpoint; path = '/same']\nasync function second string;\n    return 'b'\n", ["first", "second"], "duplicate HTTP method/path route")

    def test_missing_selected_parameter(self):
        self.check("@[endpoint; path = '/search']\nasync function search string; @[query;] term string\n    return term\n", ["search"], "must have exactly one selected declaration record")

    def test_unbound_path_placeholder(self):
        self.check("@[endpoint; path = '/items/{id}']\nasync function item string;\n    return 'item'\n", ["item"], "each HTTP path placeholder requires exactly one matching parameter")

    def test_reserved_schema_route(self):
        self.check("@[endpoint; path = '/openapi.json']\nasync function schema string;\n    return 'schema'\n", ["schema"], "reserved")

    def test_conflicting_parameter_transports(self):
        self.check("@[endpoint; path = '/items/{id}']\nasync function item int; @[path;] @[query;] id int\n    return id\n", ["item", "item::id"], "exactly one path, query, or body annotation")

    def test_runtime_query_default(self):
        self.check("function fallback int;\n    return 3\n@[endpoint; path = '/items']\nasync function item int; @[query;] count int = (fallback;)\n    return count\n", ["item", "item::count"], code="T0006")

    def test_duplicate_operation_id(self):
        self.check("@[endpoint; path = '/first', operation-id = 'same']\nasync function first string;\n    return 'a'\n@[endpoint; path = '/second', operation-id = 'same']\nasync function second string;\n    return 'b'\n", ["first", "second"], "duplicate HTTP operation ID")

    def test_synchronous_handler_rejected(self):
        self.check("@[endpoint; path = '/sync']\nfunction sync string;\n    return 'sync'\n", ["sync"])

    def test_unsupported_query_type(self):
        self.check("@[endpoint; path = '/values']\nasync function values string; @[query;] ratio float\n    return 'values'\n", ["values", "values::ratio"], "path/query parameters must be")

    def test_invalid_method(self):
        self.check("@[endpoint; path = '/trace', method = 'TRACE']\nasync function trace string;\n    return 'trace'\n", ["trace"], "method must be")

    def test_all_supported_methods_and_scalar_responses(self):
        (self.directory / "package.toml").write_text('\n'.join([
            'package = "http-annotation-test"',
            'artifact = "executable"',
            '[namespaces]',
            'http-annotation-test = "src"',
            '[terrane-dependencies]',
            f'http-server = {{ path = "{os.path.relpath(PACKAGE, self.directory)}" }}',
            '[consumers.http-server]',
            f'command = "{os.path.relpath(CONSUMER, self.directory)}"',
            'declarations = ["/http-annotation-test::read", "/http-annotation-test::create", '
            '"/http-annotation-test::replace", "/http-annotation-test::change", "/http-annotation-test::remove", '
            '"/http-annotation-test::echo-model", "/http-annotation-test::echo-model::value", '
            '"/http-annotation-test::data-model", "/http-annotation-test::tree-node", '
            '"/http-annotation-test::failing-handler", "/http-annotation-test::throwing-success"]',
        ]))
        (self.directory / "src/main.trn").write_text("""namespace http-annotation-test
from /streaky/http-server/annotations import endpoint, body
from /streaky/http-server import create-application
from /generated/http-server import annotated-controller
from /core/process import arguments
from /core/collections import list
from /core/errors import throwable, coercion-error

class tree-node
    name string
    children list of tree-node

class data-model
    active bool = true
    ratio float = 1.5
    small float32 = 0.5
    nickname string|none
    values list of int16
    count uint8 = 7
    children list of tree-node
    selected tree-node|none
    private internal string = 'not-public'

@[endpoint; path = '/models', method = 'POST']
async function echo-model data-model; @[body;] value data-model
    return value

@[endpoint; path = '/method']
async function read int;
    return 7
@[endpoint; path = '/method', method = 'POST']
async function create int;
    return 8
@[endpoint; path = '/method', method = 'PUT']
async function replace bool;
    return true
@[endpoint; path = '/method', method = 'PATCH']
async function change bool;
    return false
@[endpoint; path = '/method', method = 'DELETE']
async function remove string;
    return 'deleted'

@[endpoint; path = '/failure', content-type = 'text/html']
async function failing-handler string throws throwable;
    throw coercion-error

@[endpoint; path = '/throwing-success']
async function throwing-success string throws throwable;
    return 'still works'

async function main throws throwable;
    app = create-application;
    app = app.mount; '/', (instance annotated-controller;)
    supplied = arguments;
    address = supplied[0].text
    app = app.with-address; address
    await app.serve;

main
""")
        build = subprocess.run([str(COMPILER), "build", str(self.directory)], capture_output=True, text=True, timeout=300)
        self.assertEqual(build.returncode, 0, build.stderr)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        executable = self.directory / ".trn/build/application/artifacts/debug/terrane_program"
        process = subprocess.Popen([str(executable), f"127.0.0.1:{port}"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        process.kill()
                        process.wait()
                        self.fail("Method fixture did not start: " + process.stderr.read().decode())
                    time.sleep(0.05)
            def request(method, path, body=None, status=200):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, status)
                    self.assertEqual(response.getheader("Content-Type"), "application/json")
                    return json.loads(response.read())
                finally:
                    connection.close()
            expected = {"GET": 7, "POST": 8, "PUT": True, "PATCH": False, "DELETE": "deleted"}
            for method, value in expected.items():
                with self.subTest(method=method):
                    actual = request(method, "/method")
                    self.assertIs(type(actual), type(value))
                    self.assertEqual(actual, value)
            self.assertEqual(request("GET", "/throwing-success"), "still works")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/failure")
                response = connection.getresponse()
                self.assertEqual(response.status, 500)
                self.assertNotIn(b"coercion-error", response.read())
            finally:
                connection.close()
            self.assertEqual(request("GET", "/method"), 7)
            defaults = request("POST", "/models", "{}")
            self.assertEqual(defaults, {"active": True, "ratio": 1.5, "small": 0.5, "nickname": None,
                "values": [], "count": 7, "children": [], "selected": None})
            model = {"active": False, "ratio": 2, "small": 1.5, "nickname": "Ada",
                "values": [-32768, 32767], "count": 255,
                "children": [{"name": "nested", "children": [{"name": "leaf", "children": []}]}],
                "selected": {"name": "selected", "children": []}}
            self.assertEqual(request("POST", "/models", json.dumps(model)), model)
            for invalid in ['{"active":1}', '{"nickname":1}', '{"values":["bad"]}', '{"values":[32768]}',
                    '{"children":[1]}', '{"children":{}}', '{"children":[{"extra":true}]}',
                    '{"count":256}', '{"count":-1}', '{"ratio":1e10000}', '{"small":1e40}', '{"internal":"leak"}', '{"selected":1}']:
                with self.subTest(invalid=invalid):
                    request("POST", "/models", invalid, status=422)
            schema = request("GET", "/openapi.json")
            operations = schema["paths"]["/method"]
            self.assertEqual(set(operations), {method.lower() for method in expected})
            for method, value in expected.items():
                expected_type = "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "string"
                self.assertEqual(operations[method.lower()]["responses"]["200"]["content"]["application/json"]["schema"]["type"], expected_type)
            model_schema = schema["paths"]["/models"]["post"]["requestBody"]
            model_schema = schema["components"]["schemas"][model_schema["content"]["application/json"]["schema"]["$ref"].split("/")[-1]]
            self.assertNotIn("internal", model_schema["properties"])
            child_ref = model_schema["properties"]["children"]["items"]["$ref"]
            child_schema = schema["components"]["schemas"][child_ref.split("/")[-1]]
            self.assertEqual(child_schema["properties"]["children"]["items"]["$ref"], child_ref)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()

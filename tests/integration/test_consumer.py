"""Consumer diagnostics through real Terrane package preparation.

Run after build-consumer.sh: python packages/http-server/tests/integration/test_consumer.py
"""
from pathlib import Path
import http.client
import json
import os
import re
import subprocess
import socket
import tempfile
import unittest
import time
from xml.etree import ElementTree
import yaml


PACKAGE = Path(__file__).resolve().parents[2]
ROOT = PACKAGE.parents[1]
COMPILER = Path(os.environ.get("TERRANE", ROOT / "target/release/terrane"))
CONSUMER = PACKAGE / ".trn/consumers/http-server"


class ConsumerDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="http-annotations-")
        cls.addClassCleanup(cls.workspace.cleanup)
        cls.directory = Path(cls.workspace.name)
        (cls.directory / "src").mkdir()

    def check(self, source, selections, origin=None, code="S2061"):
        manifest = '\n'.join([
            'package = "http-annotation-test"',
            '[namespaces]',
            'http-annotation-test = "src"',
            '[terrane-dependencies.streaky.http-server]',
            f'path = "{os.path.relpath(PACKAGE, self.directory)}"',
            '[consumers.http-server]',
            f'command = "{os.path.relpath(CONSUMER, self.directory)}"',
            'declarations = [' + ', '.join(f'"/http-annotation-test::{name}"' for name in selections) + ']',
        ])
        (self.directory / "package.toml").write_text(manifest)
        written = (
            "namespace http-annotation-test\nfrom /streaky/http-server/annotations import endpoint, path, query, body\n"
            + source
            + "\nfunction main int;\n    return 0\n"
        )
        source_path = self.directory / "src/main.trn"
        source_path.write_text(written)
        result = subprocess.run([str(COMPILER), "check", str(self.directory)], capture_output=True, text=True, timeout=300)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(code, result.stderr)
        if code == "S2061":
            self.assertIsNotNone(origin, "Consumer diagnostics need an expected declaration origin")
            function, _, parameter = origin.partition("::")
            declaration = re.search(r"\bfunction (" + re.escape(function) + r")\b", written)
            self.assertIsNotNone(declaration)
            offset = declaration.start(1)
            if parameter:
                signature_end = written.index("\n", offset)
                selected = re.search(r"\b" + re.escape(parameter) + r"\s+", written[declaration.end():signature_end])
                self.assertIsNotNone(selected)
                offset = declaration.end() + selected.start()
            line = written.count("\n", 0, offset) + 1
            column = offset - written.rfind("\n", 0, offset)
            self.assertRegex(
                result.stderr,
                rf"(?m)^{re.escape(str(source_path))}:{line}:{column}: error\[S2061\]:",
                "Consumer failure must point at the declaration under test",
            )

    def test_duplicate_method_path(self):
        self.check("@[endpoint; path = '/same']\nasync function first string;\n    return 'a'\n@[endpoint; path = '/same']\nasync function second string;\n    return 'b'\n", ["first", "second"], origin="second")

    def test_missing_selected_parameter(self):
        self.check("@[endpoint; path = '/search']\nasync function search string; @[query;] term string\n    return term\n", ["search"], origin="search")

    def test_unbound_path_placeholder(self):
        self.check("@[endpoint; path = '/items/{id}']\nasync function item string;\n    return 'item'\n", ["item"], origin="item")

    def test_reserved_schema_route(self):
        self.check("@[endpoint; path = '/openapi.json']\nasync function schema string;\n    return 'schema'\n", ["schema"], origin="schema")

    def test_conflicting_parameter_transports(self):
        self.check("@[endpoint; path = '/items/{id}']\nasync function item int; @[path;] @[query;] id int\n    return id\n", ["item", "item::id"], origin="item::id")

    def test_runtime_query_default(self):
        self.check("function fallback int;\n    return 3\n@[endpoint; path = '/items']\nasync function item int; @[query;] count int = (fallback;)\n    return count\n", ["item", "item::count"], code="T0006")

    def test_duplicate_operation_id(self):
        self.check("@[endpoint; path = '/first', operation-id = 'same']\nasync function first string;\n    return 'a'\n@[endpoint; path = '/second', operation-id = 'same']\nasync function second string;\n    return 'b'\n", ["first", "second"], origin="second")

    def test_synchronous_handler_rejected(self):
        self.check("@[endpoint; path = '/sync']\nfunction sync string;\n    return 'sync'\n", ["sync"], origin="sync")

    def test_unsupported_query_type(self):
        self.check("@[endpoint; path = '/values']\nasync function values string; @[query;] ratio float\n    return 'values'\n", ["values", "values::ratio"], origin="values::ratio")

    def test_invalid_method(self):
        self.check("@[endpoint; path = '/trace', method = 'TRACE']\nasync function trace string;\n    return 'trace'\n", ["trace"], origin="trace")

    def test_malformed_response_example_is_rejected(self):
        self.check("from /streaky/http-server/annotations import response-example\n"
            "@[endpoint; path = '/example']\n@[response-example; value = '{broken']\n"
            "async function example string;\n    return 'ok'\n", ["example"], origin="example")

    def test_duplicate_response_example_names_are_rejected(self):
        self.check("from /streaky/http-server/annotations import response-example\n"
            "@[endpoint; path = '/example']\n@[response-example; value = 'null']\n"
            "@[response-example; value = '1']\nasync function example string;\n    return 'ok'\n",
            ["example"], origin="example")

    def test_default_response_format_must_be_enabled(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "from /core/collections import list\n"
            "@[route-group; response-formats = (list; 'yaml'), default-response-format = 'json']\n"
            "class api\n    @[endpoint; path = '/example']\n"
            "    async function example string;\n        return 'ok'\n",
            ["api", "api::example"], origin="example")

    def test_multiple_api_documentation_owners_are_rejected(self):
        self.check("from /streaky/http-server/annotations import api-documentation\n"
            "@[api-documentation;]\n@[endpoint; path = '/first']\n"
            "async function first string;\n    return 'first'\n"
            "@[api-documentation;]\n@[endpoint; path = '/second']\n"
            "async function second string;\n    return 'second'\n",
            ["first", "second"], origin="second")

    def test_all_supported_methods_and_scalar_responses(self):
        (self.directory / "package.toml").write_text('\n'.join([
            'package = "http-annotation-test"',
            'artifact = "executable"',
            '[namespaces]',
            'http-annotation-test = "src"',
            '[terrane-dependencies.streaky.http-server]',
            f'path = "{os.path.relpath(PACKAGE, self.directory)}"',
            '[consumers.http-server]',
            f'command = "{os.path.relpath(CONSUMER, self.directory)}"',
            'declarations = ["/http-annotation-test::read", "/http-annotation-test::create", '
            '"/http-annotation-test::replace", "/http-annotation-test::change", "/http-annotation-test::remove", '
            '"/http-annotation-test::echo-model", "/http-annotation-test::echo-model::value", '
            '"/http-annotation-test::data-model", "/http-annotation-test::tree-node", '
            '"/http-annotation-test::data-model::nickname", "/http-annotation-test::data-model::selected", '
            '"/http-annotation-test::failing-handler", "/http-annotation-test::throwing-success", '
            '"/http-annotation-test::created", "/http-annotation-test::redirect", "/http-annotation-test::empty", '
            '"/http-annotation-test::denied", "/http-annotation-test::custom-error", '
            '"/http-annotation-test::invalid-code", "/http-annotation-test::invalid-header", '
            '"/http-annotation-test::override-content-type", "/http-annotation-test::final-continue", '
            '"/http-annotation-test::final-upgrade", '
            '"/http-annotation-test::nonfinite-model", "/http-annotation-test::nonfinite-model::single", '
            '"/http-annotation-test::tampered-error", "/http-annotation-test::quoted-default", '
            '"/http-annotation-test::xml-api", "/http-annotation-test::xml-api::value", '
            '"/http-annotation-test::xml-api::nothing", '
            '"/http-annotation-test::yaml-api", "/http-annotation-test::yaml-api::value", '
            '"/http-annotation-test::quoted-default::label"]',
        ]))
        (self.directory / "src/main.trn").write_text(r"""namespace http-annotation-test
from /streaky/http-server/annotations import endpoint, body, query, route-group, response-example, api-documentation
from /streaky/http-server import create-application, http-response, create-response, create-json-response, create-redirect-response, http-error, create-http-error, controller, routes, client-info
from /streaky/http-server import http-response as explicit-response
from /streaky/http-server import http-header
from /generated/http-server import mount-annotated
from /core/process import arguments
from /core/collections import list
from /core/errors import throwable, coercion-error
from /core/documents import document-value, document-map-entries, make-document-none, make-document-string, make-document-map

/// This declaration comment must not override an explicit empty overview.
@[api-documentation; title = 'Integration fixture', version = '2026.10', description = '']
@[route-group; prefix = '/xml', response-formats = (list; 'yaml', 'xml'), default-response-format = 'xml']
class xml-api
    @[endpoint; path = '/value']
    @[response-example; value = '"<café & units>"']
    async function value string;
        return '<café & units>'

    @[endpoint; path = '/nothing']
    @[response-example; value = 'null']
    async function nothing document-value;
        return make-document-none;

@[route-group; prefix = '/yaml', response-formats = (list; 'yaml', 'xml')]
class yaml-api
    @[endpoint; path = '/value']
    async function value string;
        return 'yaml-default'

class tree-node
    name string
    children list of tree-node

class data-model
    active bool = true
    ratio float = 1.5
    small float32 = 0.5
    /// Optional display name supplied by the caller.
    nickname string|none
    values list of int16
    count uint8 = 7
    children list of tree-node
    /// Optional selected node; null means no node was chosen.
    selected tree-node|none
    related list of (tree-node)|none
    private internal string = 'not-public'

@[endpoint; path = '/models', method = 'POST']
async function echo-model data-model; @[body;] value data-model
    return value

@[endpoint; path = '/nonfinite-model']
async function nonfinite-model data-model; @[query;] single bool = false
    value = instance data-model;
    if single
        value.small = rust
            f32::NAN
    else
        value.ratio = rust
            f64::INFINITY
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

@[endpoint; path = '/created', summary = 'A "quoted" café\nand a backslash \\']
async function created explicit-response;
    entries = instance document-map-entries;
    entries.append; 'value', (make-document-string; 'say "hello" \\ café\n\t')
    payload = make-document-map; entries
    if payload.failed
        throw coercion-error
    response = create-json-response; 201, payload.value
    response = response.with-header; 'location', '/created/1'
    response = response.with-header; 'set-cookie', 'first=1'
    return response.with-header; 'set-cookie', 'second=2'

/// An explicit redirect with a documentation example.
/// ````terrane
/// @returns This marker is code, not response documentation.
/// ```
/// ````
/// @returns The empty redirect body; follow the Location header.
@[endpoint; path = '/redirect', content-type = 'text/plain']
@[response-example; value = '""']
async function redirect http-response;
    return create-redirect-response; '/method', 307

@[endpoint; path = '/empty']
async function empty http-response;
    return create-response; 204, 'text/plain', ''

@[endpoint; path = '/override-content-type']
async function override-content-type http-response;
    response = create-response; 200, 'text/plain', 'content type'
    response = response.with-header; 'Content-Type', 'application/json'
    response = response.with-header; 'cOnTeNt-TyPe', 'application/problem+json'
    response.headers.append; (instance http-header; 'CONTENT-TYPE', 'application/vnd.example+json')
    return move response

@[endpoint; path = '/final-100']
async function final-continue http-response;
    return create-response; 100, 'text/plain', ''

@[endpoint; path = '/final-101']
async function final-upgrade http-response;
    return create-response; 101, 'text/plain', ''

@[endpoint; path = '/denied']
async function denied string;
    throw http-error; 403

@[endpoint; path = '/custom-error']
async function custom-error http-response;
    entries = instance document-map-entries;
    entries.append; 'detail', (make-document-string; 'No café "seat"\nleft')
    payload = make-document-map; entries
    if payload.failed
        throw coercion-error
    response = create-json-response; 409, payload.value
    response = response.with-header; 'x-error', 'capacity'
    throw (create-http-error; response)

@[endpoint; path = '/invalid-code']
async function invalid-code http-response;
    return create-response; 999, 'text/plain', 'must not escape'

@[endpoint; path = '/invalid-header']
async function invalid-header http-response;
    return (create-response; 200, 'text/plain', 'must not escape').with-header; 'x-invalid', 'a\r\nb'

@[endpoint; path = '/tampered-error']
async function tampered-error string;
    error = instance http-error; 403
    error.message = 'not-json'
    throw error

@[endpoint; path = '/quoted-default']
async function quoted-default string; @[query;] label string = 'say "hello" \\ café\n\t'
    return label

async function text-denied string;
    throw http-error; 401

async function client-denied string; _client client-info
    throw http-error; 403

class text-controller implements controller
    consuming function routes routes; registered-routes routes
        registered-routes = registered-routes.get; '/denied', text-denied
        return registered-routes.get-with-client; '/client-denied', client-denied

async function main throws throwable;
    app = create-application;
    app = mount-annotated; app
    app = app.mount; '/text', (instance text-controller;)
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
            def raw_request(path, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", path, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, response.getheaders(), response.read()
                finally:
                    connection.close()
            status, headers, payload = raw_request("/created")
            self.assertEqual(status, 201)
            self.assertEqual(dict(headers)["content-type"], "application/json")
            self.assertEqual(dict(headers)["location"], "/created/1")
            self.assertEqual([value for name, value in headers if name.lower() == "set-cookie"], ["first=1", "second=2"])
            special = 'say "hello" \\ café\n\t'
            self.assertEqual(json.loads(payload), {"value": special})
            status, headers, payload = raw_request("/redirect")
            self.assertEqual(status, 307)
            self.assertEqual(dict(headers)["location"], "/method")
            self.assertEqual(payload, b"")
            status, _, payload = raw_request("/empty")
            self.assertEqual((status, payload), (204, b""))
            status, headers, payload = raw_request("/override-content-type")
            self.assertEqual((status, payload), (200, b"content type"))
            self.assertEqual([value for name, value in headers if name.lower() == "content-type"],
                             ["application/vnd.example+json"])
            for path, code, body in [("/denied", 403, b"Forbidden"),
                                     ("/text/denied", 401, b"Unauthorized"),
                                     ("/text/client-denied", 403, b"Forbidden")]:
                with self.subTest(path=path):
                    status, _, payload = raw_request(path)
                    self.assertEqual((status, payload), (code, body))
            status, headers, payload = raw_request("/custom-error")
            self.assertEqual(status, 409)
            self.assertEqual(dict(headers)["content-type"], "application/json")
            self.assertEqual(dict(headers)["x-error"], "capacity")
            self.assertEqual(json.loads(payload), {"detail": 'No café "seat"\nleft'})
            for path in ("/invalid-code", "/invalid-header", "/tampered-error", "/final-100", "/final-101",
                         "/nonfinite-model", "/nonfinite-model?single=true"):
                with self.subTest(path=path):
                    status, _, payload = raw_request(path)
                    self.assertEqual(status, 500)
                    self.assertNotIn(b"must not escape", payload)
            self.assertEqual(request("GET", "/quoted-default"), special)
            schema = request("GET", "/openapi.json")
            self.assertNotIn("description", schema["info"])
            self.assertEqual(schema["paths"]["/created"]["get"]["summary"], 'A "quoted" café\nand a backslash \\')
            responses = schema["paths"]["/created"]["get"]["responses"]
            self.assertNotIn("200", responses)
            self.assertIn("default", responses)
            redirect = schema["paths"]["/redirect"]["get"]
            self.assertIn("@returns", redirect["description"])
            self.assertNotIn("```", redirect["responses"]["default"]["description"])
            self.assertEqual(redirect["responses"]["default"]["content"]["text/plain"]["examples"]["default"]["value"], "")
            default = schema["paths"]["/quoted-default"]["get"]["parameters"][0]
            self.assertEqual(default["schema"]["default"], special)
            self.assertFalse(default["required"])
            self.assertNotIn("description", default)
            self.assertNotIn("description", schema["paths"]["/models"]["post"]["requestBody"])
            self.assertEqual(request("GET", "/method"), 7)
            defaults = request("POST", "/models", "{}")
            self.assertEqual(defaults, {"active": True, "ratio": 1.5, "small": 0.5, "nickname": None,
                "values": [], "count": 7, "children": [], "selected": None, "related": None})
            model = {"active": False, "ratio": 2, "small": 1.5, "nickname": "Ada",
                "values": [-32768, 32767], "count": 255,
                "children": [{"name": "nested", "children": [{"name": "leaf", "children": []}]}],
                "selected": {"name": "selected", "children": []},
                "related": [{"name": special, "children": []}]}
            self.assertEqual(request("POST", "/models", json.dumps(model)), model)
            model["related"] = None
            self.assertEqual(request("POST", "/models", json.dumps(model)), model)
            model["related"] = []
            self.assertEqual(request("POST", "/models", json.dumps(model)), model)
            for invalid in ['{"active":1}', '{"nickname":1}', '{"values":["bad"]}', '{"values":[32768]}',
                    '{"children":[1]}', '{"children":{}}', '{"children":[{"extra":true}]}',
                    '{"related":{}}', '{"related":[1]}', '{"related":[{"extra":true}]}',
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
            status, headers, payload = raw_request("/xml/value")
            self.assertEqual((status, dict(headers)["content-type"]), (200, "application/xml"))
            self.assertEqual(ElementTree.fromstring(payload).text, "<café & units>")
            self.assertEqual(dict(headers)["vary"], "Accept")
            status, headers, payload = raw_request("/yaml/value")
            self.assertEqual((status, dict(headers)["content-type"], yaml.safe_load(payload)),
                (200, "application/yaml", "yaml-default"))
            self.assertEqual(raw_request("/xml/value", {"Accept": "application/json"})[0], 406)
            self.assertEqual(raw_request("/method", {"Accept": "application/yaml"})[0], 406)
            xml_schema = schema["paths"]["/xml/value"]["get"]["responses"]["200"]["content"]
            self.assertEqual(set(xml_schema), {"application/xml", "application/yaml"})
            self.assertEqual(ElementTree.fromstring(xml_schema["application/xml"]["examples"]["default"]["value"]).text,
                "<café & units>")
            null_content = schema["paths"]["/xml/nothing"]["get"]["responses"]["200"]["content"]
            self.assertIsNone(null_content["application/yaml"]["examples"]["default"]["value"])
            self.assertEqual(ElementTree.fromstring(null_content["application/xml"]["examples"]["default"]["value"]).get(
                "{http://www.w3.org/2001/XMLSchema-instance}nil"), "true")
            model_schema = schema["components"]["schemas"][model_schema["content"]["application/json"]["schema"]["$ref"].split("/")[-1]]
            self.assertNotIn("internal", model_schema["properties"])
            self.assertNotIn("description", model_schema)
            self.assertNotIn("description", model_schema["properties"]["active"])
            for name in ("nickname", "selected"):
                nullable = model_schema["properties"][name]
                self.assertIsInstance(nullable.get("description"), str)
                self.assertNotEqual(nullable["description"].strip(), "")
                self.assertNotEqual(nullable["description"], "none")
                self.assertEqual(nullable["anyOf"][1], {"type": "null"})
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

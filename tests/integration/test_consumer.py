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
import textwrap
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

    def check(self, source, selections, origin=None, code="S2061", class_bound=True):
        if class_bound and not re.search(r"(?m)^class ", source):
            lines = source.splitlines(keepends=True)
            imports = "".join(line for line in lines if line.startswith("from "))
            body = "".join(line for line in lines if not line.startswith("from "))
            source = (imports + "from /streaky/http-server/annotations import route-group\n"
                "@[route-group;]\nclass api uses http-api\n" + textwrap.indent(body, "    "))
            selections = ["api"] + ["api::" + name for name in selections]
            if origin is not None:
                origin = "api::" + origin
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
            "from /streaky/http-server import api as http-api\n"
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
            parts = origin.split("::")
            function = parts[-2] if len(parts) == 3 else parts[-1]
            parameter = parts[-1] if len(parts) == 3 else ""
            declaration = re.search(r"\b(?:function|class) (" + re.escape(function) + r")\b", written)
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
        self.check("from /streaky/http-server/annotations import route-group\n"
            "@[route-group;]\nclass api uses http-api\n"
            "    @[endpoint; path = '/same']\n    async function first string;\n        return 'a'\n"
            "    @[endpoint; path = '/same']\n    async function second string;\n        return 'b'\n",
            ["api", "api::first", "api::second"], origin="api::second")

    def test_missing_selected_parameter(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "@[route-group;]\nclass api uses http-api\n"
            "    @[endpoint; path = '/search']\n    async function search string; @[query;] term string\n        return term\n",
            ["api", "api::search"], origin="api::search")

    def test_unbound_path_placeholder(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "@[route-group;]\nclass api uses http-api\n"
            "    @[endpoint; path = '/items/{id}']\n    async function item string;\n        return 'item'\n",
            ["api", "api::item"], origin="api::item")

    def test_free_endpoint_handler_is_rejected(self):
        self.check("@[endpoint; path = '/free']\nasync function item string;\n    return 'item'\n",
            ["item"], origin="item", class_bound=False)

    def test_static_endpoint_handler_is_rejected(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "@[route-group;]\nclass api uses http-api\n"
            "    @[endpoint; path = '/static']\n    static async function item string;\n        return 'item'\n",
            ["api", "api::item"], origin="api::item")

    def test_route_group_requires_api_context_fields(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "@[route-group;]\nclass api\n"
            "    @[endpoint; path = '/value']\n    async function value int;\n        return 1\n",
            ["api", "api::value"], origin="api")

    def test_reserved_schema_route(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "@[route-group;]\nclass api uses http-api\n"
            "    @[endpoint; path = '/openapi.json']\n    async function schema string;\n        return 'schema'\n",
            ["api", "api::schema"], origin="api::schema")

    def test_conflicting_parameter_transports(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "@[route-group;]\nclass api uses http-api\n"
            "    @[endpoint; path = '/items/{id}']\n    async function item int; @[path;] @[query;] id int\n        return id\n",
            ["api", "api::item", "api::item::id"], origin="api::item::id")

    def test_runtime_query_default(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "function fallback int;\n    return 3\n@[route-group;]\nclass api uses http-api\n"
            "    @[endpoint; path = '/items']\n"
            "    async function item int; @[query;] count int = (fallback;)\n        return count\n",
            ["api", "api::item", "api::item::count"], code="T0006")

    def test_duplicate_operation_id(self):
        self.check("@[endpoint; path = '/first', operation-id = 'same']\nasync function first string;\n    return 'a'\n@[endpoint; path = '/second', operation-id = 'same']\nasync function second string;\n    return 'b'\n", ["first", "second"], origin="second")

    def test_synchronous_handler_rejected(self):
        self.check("@[endpoint; path = '/sync']\nfunction sync string;\n    return 'sync'\n", ["sync"], origin="sync")

    def test_unsupported_query_type(self):
        self.check("@[endpoint; path = '/values']\nasync function values string; @[query;] ratio float\n    return 'values'\n", ["values", "values::ratio"], origin="values::ratio")

    def test_invalid_method(self):
        self.check("@[endpoint; path = '/trace', method = 'TRACE']\nasync function trace string;\n    return 'trace'\n", ["trace"], origin="trace")

    def test_multi_method_sets_reject_duplicate_and_empty_methods(self):
        for methods in ("GET|GET", "GET|", "|HEAD", "GET||HEAD"):
            with self.subTest(methods=methods):
                self.check(f"@[endpoint; path = '/value', method = '{methods}']\n"
                    "async function value int;\n    return 1\n", ["value"], origin="value")

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
            ["api", "api::example"], origin="api")

    def test_unreferenced_route_group_policy_is_validated_at_the_class(self):
        for policy in (
            "response-formats = (list; 'yaml'), default-response-format = 'json'",
            "response-formats = (list; 'json', 'json')",
            "response-formats = (list; 'unsupported')",
        ):
            with self.subTest(policy=policy):
                self.check("from /streaky/http-server/annotations import route-group\n"
                    "from /core/collections import list\n"
                    f"@[route-group; {policy}]\nclass api\n",
                    ["api"], origin="api")

    def test_explicit_empty_response_formats_are_rejected_at_the_class(self):
        self.check("from /streaky/http-server/annotations import route-group\n"
            "from /core/collections import list\n"
            "@[route-group; response-formats = (list;)]\nclass api\n",
            ["api"], origin="api")

    def test_multiple_api_documentation_owners_are_rejected(self):
        self.check("from /streaky/http-server/annotations import api-documentation\n"
            "@[api-documentation;]\n@[endpoint; path = '/first']\n"
            "async function first string;\n    return 'first'\n"
            "@[api-documentation;]\n@[endpoint; path = '/second']\n"
            "async function second string;\n    return 'second'\n",
            ["first", "second"], origin="second")

    def test_document_wide_info_ownership_encoding_and_overview_precedence(self):
        (self.directory / "package.toml").write_text('\n'.join([
            'package = "http-annotation-test"',
            'artifact = "executable"',
            '[namespaces]',
            'http-annotation-test = "src"',
            '[terrane-dependencies.streaky.http-server]',
            f'path = "{os.path.relpath(PACKAGE, self.directory)}"',
            '[consumers.http-server]',
            f'command = "{os.path.relpath(CONSUMER, self.directory)}"',
            'declarations = ["/http-annotation-test::api", "/http-annotation-test::api::value"]',
        ]))
        title = 'Café "catalog" \\ API'
        version = "2026.10-preview"
        overview = "# API overview\n\n```console\ncurl /api/value\n```\n\n@returns This belongs to the overview."
        override = "**Custom overview**\n\nUse the documented request examples."
        def literal(value):
            return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"

        base_annotation = (
            "@[api-documentation; title = " + literal(title)
            + ", version = " + literal(version)
        )
        for annotation, expected_info in [
            ("", {"title": "HTTP API", "version": "1.0.0"}),
            (base_annotation + "]", {"title": title, "version": version, "description": overview}),
            (base_annotation + ", description = " + literal(override) + "]",
                {"title": title, "version": version, "description": override}),
        ]:
            with self.subTest(annotation=annotation):
                source = (
                    "namespace http-annotation-test\n"
                    "from /streaky/http-server/annotations import api-documentation, route-group, endpoint\n"
                    "from /streaky/http-server import api as http-api, create-application\n"
                    "from /generated/http-server import mount-annotated\n"
                    "from /core/process import arguments\n"
                    "from /core/errors import throwable\n"
                    + "\n".join("/// " + line if line else "///" for line in overview.splitlines())
                    + "\n" + (annotation + "\n" if annotation else "")
                    + "@[route-group; prefix = '/api']\nclass api uses http-api\n"
                    "    @[endpoint; path = '/value']\n"
                    "    async function value string;\n        return 'ready'\n"
                    "async function main throws throwable;\n"
                    "    app = mount-annotated; (create-application;)\n"
                    "    supplied = arguments;\n"
                    "    app = app.with-address; supplied[0].text\n"
                    "    await app.serve;\nmain\n"
                )
                (self.directory / "src/main.trn").write_text(source)
                built = subprocess.run([str(COMPILER), "build", str(self.directory)],
                    capture_output=True, text=True, timeout=300)
                self.assertEqual(built.returncode, 0, built.stderr)
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                server = subprocess.Popen([built.stdout.strip().splitlines()[-1], f"127.0.0.1:{port}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        try:
                            connection.request("GET", "/api/openapi.json")
                            with connection.getresponse() as response:
                                self.assertEqual(response.status, 200)
                                self.assertEqual(json.loads(response.read())["info"], expected_info)
                            break
                        except OSError:
                            if time.monotonic() >= deadline or server.poll() is not None:
                                raise
                            time.sleep(0.05)
                        finally:
                            connection.close()
                finally:
                    server.terminate()
                    try:
                        server.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.communicate()

    def test_all_supported_methods_and_scalar_responses(self):
        handlers = [
            "read", "create", "replace", "change", "remove", "head-read", "head-scalar",
            "method-options", "echo-model", "echo-model::value", "failing-handler",
            "throwing-success", "created", "redirect", "empty", "denied", "custom-error",
            "invalid-code", "invalid-header", "override-content-type", "final-continue",
            "final-upgrade", "nonfinite-model", "nonfinite-model::single", "tampered-error",
            "quoted-default", "quoted-default::label",
            "head-metadata",
        ]
        declarations = ["handlers"] + ["handlers::" + name for name in handlers] + [
            "data-model", "tree-node", "data-model::nickname", "data-model::selected",
            "xml-api", "xml-api::value", "xml-api::nothing", "xml-api::failure",
            "xml-api::denied", "xml-api::invalid-xml", "xml-api::model", "xml-api::model::value",
            "legacy-api", "legacy-api::value", "json-api", "json-api::value",
            "yaml-api", "yaml-api::value", "text-api", "text-api::denied",
        ]
        (self.directory / "package.toml").write_text('\n'.join([
            'package = "http-annotation-test"',
            'artifact = "executable"',
            '[namespaces]',
            'http-annotation-test = "src"',
            '[terrane-dependencies.streaky.http-server]',
            f'path = "{os.path.relpath(PACKAGE, self.directory)}"',
            '[consumers.http-server]',
            f'command = "{os.path.relpath(CONSUMER, self.directory)}"',
            'declarations = [' + ', '.join(json.dumps("/http-annotation-test::" + name)
                for name in declarations) + ']',
        ]))
        (self.directory / "src/main.trn").write_text(r"""namespace http-annotation-test
from /streaky/http-server/annotations import endpoint, body, query, route-group, response-example, api-documentation
from /streaky/http-server import api, response-context, create-application, http-error
from /generated/http-server import mount-annotated
from /core/process import arguments
from /core/collections import list
from /core/errors import throwable, coercion-error
from /core/documents import document-value, document-map-entries, make-document-none, make-document-string, make-document-map

/// This declaration comment must not override an explicit empty overview.
@[api-documentation; title = 'Integration fixture', version = '2026.10', description = '']
@[route-group; prefix = '/xml', response-formats = (list; 'yaml', 'xml'), default-response-format = 'xml']
class xml-api uses api
    @[endpoint; path = '/value']
    @[response-example; value = '"<café & units>"']
    async function value string;
        return '<café & units>'

    @[endpoint; path = '/nothing']
    @[response-example; value = 'null']
    async function nothing document-value;
        return make-document-none;

    @[endpoint; path = '/failure']
    async function failure string throws throwable;
        throw coercion-error

    @[endpoint; path = '/denied']
    async function denied string throws throwable;
        response = this.response
        if response != none
            response.vary; 'Origin'
        throw http-error; 404, 'not found'

    @[endpoint; path = '/invalid-xml']
    async function invalid-xml document-value throws throwable;
        fields = instance document-map-entries;
        fields.append; 'bad key', (make-document-string; 'cannot become an XML element')
        document = make-document-map; fields
        if document.failed
            throw coercion-error
        return document.value

    @[endpoint; method = 'POST', path = '/model']
    async function model data-model; @[body;] value data-model
        return value

@[route-group; prefix = '/yaml', response-formats = (list; 'yaml', 'xml')]
class yaml-api uses api
    @[endpoint; path = '/value']
    async function value string;
        return 'yaml-default'

@[route-group; prefix = '/legacy']
class legacy-api uses api
    @[endpoint; path = '/value']
    async function value int;
        return 7

@[route-group; prefix = '/json', response-formats = (list; 'json')]
class json-api uses api
    @[endpoint; path = '/value']
    async function value int;
        return 7

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

@[route-group;]
class handlers uses api
    function controls response-context throws coercion-error;
        response = this.response
        if response != none
            return response
        throw coercion-error

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
    @[endpoint; path = '/method', method = 'HEAD', content-type = 'text/plain']
    async function head-read string throws throwable;
        response = this.controls;
        response.set-status; 202
        response.set-header; 'x-head', 'explicit'
        return 'hidden'

    @[endpoint; path = '/head-scalar', method = 'HEAD']
    async function head-scalar bool;
        return true

    @[endpoint; path = '/unknown-length', method = 'HEAD']
    async function head-metadata none throws throwable;
        response = this.controls;
        response.set-status; 200
        response.set-content-type; 'text/plain'
        return none

    @[endpoint; path = '/method', method = 'OPTIONS']
    async function method-options none throws throwable;
        response = this.controls;
        response.set-header; 'allow', 'GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS'
        return none

    @[endpoint; path = '/failure', content-type = 'text/html']
    async function failing-handler string throws throwable;
        throw coercion-error

    @[endpoint; path = '/throwing-success']
    async function throwing-success string throws throwable;
        return 'still works'

    @[endpoint; path = '/created', summary = 'A "quoted" café\nand a backslash \\']
    async function created document-value throws throwable;
        entries = instance document-map-entries;
        entries.append; 'value', (make-document-string; 'say "hello" \\ café\n\t')
        payload = make-document-map; entries
        if payload.failed
            throw coercion-error
        response = this.controls;
        response.set-status; 201
        response.set-header; 'location', '/created/1'
        response.add-header; 'set-cookie', 'first=1'
        response.add-header; 'set-cookie', 'second=2'
        return payload.value

    /// An explicit redirect with a documentation example.
    /// ````terrane
    /// @returns This marker is code, not response documentation.
    /// ```
    /// ````
    ///   ~~~console
    /// @returns This tilde-fenced marker is also code.
    ///   ~~~
    /// ```inline```
    /// @returns The empty redirect body; follow the Location header.
    @[endpoint; path = '/redirect', content-type = 'text/plain']
    @[response-example; value = '""']
    async function redirect string throws throwable;
        response = this.controls;
        response.redirect; '/method', 307
        return ''

    @[endpoint; path = '/empty']
    async function empty none;
        return none

    @[endpoint; path = '/override-content-type', content-type = 'text/plain']
    async function override-content-type string throws throwable;
        response = this.controls;
        response.set-header; 'Content-Type', 'application/json'
        response.set-header; 'cOnTeNt-TyPe', 'application/problem+json'
        response.set-content-type; 'application/vnd.example+json'
        return 'content type'

    @[endpoint; path = '/final-100']
    async function final-continue none throws throwable;
        response = this.controls;
        response.set-status; 100
        return none

    @[endpoint; path = '/final-101']
    async function final-upgrade none throws throwable;
        response = this.controls;
        response.set-status; 101
        return none

    @[endpoint; path = '/denied']
    async function denied string throws throwable;
        throw http-error; 403

    @[endpoint; path = '/custom-error']
    async function custom-error document-value throws throwable;
        entries = instance document-map-entries;
        entries.append; 'detail', (make-document-string; 'No café "seat"\nleft')
        payload = make-document-map; entries
        if payload.failed
            throw coercion-error
        response = this.controls;
        response.set-status; 409
        response.set-header; 'x-error', 'capacity'
        return payload.value

    @[endpoint; path = '/invalid-code']
    async function invalid-code string throws throwable;
        response = this.controls;
        response.set-status; 999
        return 'must not escape'

    @[endpoint; path = '/invalid-header']
    async function invalid-header string throws throwable;
        response = this.controls;
        response.set-header; 'x-invalid', 'a\r\nb'
        return 'must not escape'

    @[endpoint; path = '/tampered-error']
    async function tampered-error string throws throwable;
        error = instance http-error; 403
        error.message = 'not-json'
        throw error

    @[endpoint; path = '/quoted-default']
    async function quoted-default string; @[query;] label string = 'say "hello" \\ café\n\t'
        return label

@[route-group; prefix = '/text']
class text-api uses api
    @[endpoint; path = '/denied']
    async function denied string throws throwable;
        throw http-error; 401

async function main throws throwable;
    app = create-application;
    app = mount-annotated; app
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
            def raw_request(path, headers=None, method="GET", body=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request(method, path, body=body, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, response.getheaders(), response.read()
                finally:
                    connection.close()
            status, headers, payload = raw_request("/method", method="HEAD")
            self.assertEqual((status, payload), (202, b""))
            self.assertEqual(dict(headers)["x-head"], "explicit")
            status, headers, payload = raw_request("/head-scalar", method="HEAD")
            self.assertEqual((status, payload), (200, b""))
            self.assertEqual(dict(headers)["content-type"], "application/json")
            status, headers, payload = raw_request("/unknown-length", method="HEAD")
            self.assertEqual((status, payload), (200, b""))
            self.assertEqual(dict(headers)["content-type"], "text/plain")
            self.assertNotIn("content-length", dict(headers))
            status, headers, payload = raw_request("/method", method="OPTIONS")
            self.assertEqual((status, payload), (204, b""))
            self.assertEqual(dict(headers)["allow"], "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS")
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
                                     ("/text/denied", 401, b"Unauthorized")]:
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
            self.assertIn("200", responses)
            redirect = schema["paths"]["/redirect"]["get"]
            self.assertIn("@returns", redirect["description"])
            self.assertNotIn("```", redirect["responses"]["200"]["description"])
            self.assertEqual(redirect["responses"]["200"]["description"],
                "The empty redirect body; follow the Location header.")
            self.assertEqual(redirect["responses"]["200"]["content"]["text/plain"]["examples"]["default"]["value"], "")
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
            self.assertEqual(set(operations), {method.lower() for method in expected} | {"head", "options"})
            for method, value in expected.items():
                expected_type = "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "string"
                self.assertEqual(operations[method.lower()]["responses"]["200"]["content"]["application/json"]["schema"]["type"], expected_type)
            self.assertNotIn("content", operations["head"]["responses"]["200"])
            self.assertNotIn("content", operations["options"]["responses"]["204"])
            self.assertNotIn("content", schema["paths"]["/head-scalar"]["head"]["responses"]["200"])
            model_schema = schema["paths"]["/models"]["post"]["requestBody"]
            status, headers, payload = raw_request("/xml/value")
            self.assertEqual((status, dict(headers)["content-type"]), (200, "application/xml"))
            self.assertEqual(ElementTree.fromstring(payload).text, "<café & units>")
            self.assertEqual(dict(headers)["vary"], "Accept")
            status, headers, payload = raw_request("/yaml/value")
            self.assertEqual((status, dict(headers)["content-type"], yaml.safe_load(payload)),
                (200, "application/yaml", "yaml-default"))
            self.assertEqual(raw_request("/xml/value", {"Accept": "application/json"})[0], 406)
            for path in ("/method", "/legacy/value"):
                for accept in ("text/plain", "text/html", "image/png", "application/json;q=0"):
                    with self.subTest(path=path, accept=accept):
                        status, headers, payload = raw_request(path, {"Accept": accept})
                        self.assertEqual((status, dict(headers)["content-type"], json.loads(payload)),
                            (200, "application/json", 7))
                        self.assertNotIn("vary", dict(headers))
                self.assertNotIn("406", schema["paths"][path]["get"]["responses"])
            for accept in ("application/json; charset=utf-8", 'application/json;charset="UTF-8"',
                           "application/json, text/\xe9"):
                status, headers, payload = raw_request("/json/value", {"Accept": accept})
                self.assertEqual((status, json.loads(payload)), (200, 7))
                self.assertEqual(dict(headers)["vary"], "Accept")
            for accept in ("text/plain", "", "application/json;q=0, text/\xe9, */*;q=1"):
                status, headers, _ = raw_request("/json/value", {"Accept": accept})
                self.assertEqual(status, 406)
                self.assertEqual(dict(headers)["vary"], "Accept")
            for path, expected_status in (("/xml/failure", 500), ("/xml/denied", 404),
                                          ("/xml/invalid-xml", 500)):
                for accept, status in (("application/xml", expected_status), ("image/png", 406)):
                    response_status, headers, _ = raw_request(path, {"Accept": accept})
                    self.assertEqual(response_status, status)
                    tokens = {token.strip().lower() for name, value in headers
                              if name.lower() == "vary" for token in value.split(",")}
                    self.assertIn("accept", tokens)
                    if path == "/xml/denied" and status == 404:
                        self.assertNotIn("origin", tokens)
            for body in ("{broken", '{"active":1}'):
                status, headers, _ = raw_request("/xml/model",
                    {"Accept": "application/xml", "Content-Type": "application/json"}, "POST", body)
                self.assertEqual(status, 422)
                self.assertEqual(dict(headers)["vary"], "Accept")
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

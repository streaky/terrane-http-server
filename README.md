# HTTP Server

`streaky/http-server` is a Terrane-native application server for dynamic web applications and static content. Axum, Tower HTTP, and Tokio are implementation details; applications use only Terrane classes, interfaces, and functions.

## Documentation

The [documentation publication](docs/src/manual.yaml) contains the [general documentation](docs/src/records/guides/introduction.yaml), a complete public API reference, and a [getting-started tutorial](docs/src/records/tutorials/first-server.yaml). These are YAML sources in the shared Terrane manual format; prose, declarations, code, and data examples retain their typed structure rather than being flattened into Markdown.

The [annotation guide](docs/src/records/guides/annotations.yaml) describes the package's explicit native Terrane consumer. `endpoint`, `path`, `query`, and `body` metadata generate class-based route adapters, typed scalar and model-body extraction, JSON or HTML responses, and OpenAPI 3.1 JSON. `route-group` supplies a class mount prefix and controls schema inclusion; repeatable `static-directory` metadata mounts cwd-relative assets. Signatures determine types and query defaults; malformed inputs return 422. Ordinary safe async handlers may throw `throwable`; handler and render failures return HTTP 500.

The [catalog demo](demo/README.md) exercises these capabilities with typed catalog models, AOT template-backed HTML pages, static Stoplight Elements docs, and a prefixed `/api` with OpenAPI at `/api/openapi.json`. Build both native consumers first with `sh packages/http-server/build-consumer.sh`, then build or run the demo through Terrane.

## Consumer source generation

The native consumer uses AOT-compiled `.trn.tpl` templates under
`consumer/templates/` for controller skeletons, generated helpers, and model
encoder/decoder blocks. Metadata validation and small source expressions remain in
Terrane. The [annotation guide](docs/src/records/guides/annotations.yaml) explains the
rendering and quoting boundaries. `build-consumer.sh` builds the template consumer
first and embeds these templates in the HTTP consumer; its installed executable
does not need template files at runtime.

## Application model

Create an application with `create-application`, add controllers with `application.mount`, add static content with `application.static-directory`, optionally replace the listen address with `application.with-address`, and start it with `application.serve`.

A controller implements `controller.routes`. The framework passes that callback a `routes` value scoped to the controller's mount prefix. The callback registers relative handlers with `routes.get` and returns the routes. Handlers are ordinary reusable Terrane asynchronous functions returning `string`; safe handlers may declare `throws throwable`, which becomes HTTP 500 on failure. Use a distinct parameter name such as `registered-routes` to avoid shadowing the `routes` type.

For handlers that need client identity, `routes.get-with-client` accepts an asynchronous function from `client-info` to `string`. `client-info.peer-ip` returns the TCP peer and `client-info.client-ip` returns the resolved client address, each as projected `ip-addr|none` (`IpAddr` imported with a kebab-case alias). Identity is resolved independently of the client allowlist, so logging does not require configuring that list.

`routes.endpoint` accepts an asynchronous `http-request` to `http-response` callback with a `throws throwable` bound. Path, query, and body conversion failures are input errors and return HTTP 422 before the callback runs. Handler failures, including template-render failures, propagate to the HTTP boundary and return HTTP 500.

The public model therefore exposes neither Axum's `Router` nor its move-and-return registration pattern. `application` and `routes` own that native state and preserve Terrane's ordinary consuming-builder semantics internally.

Listener creation uses core `listen-tcp`, then consumes the listener through `into-native` before passing it to Axum. The package neither declares nor imports Tokio directly; Tokio remains underneath core and Axum, with one compiler-bound runtime owner. Core's automatic backlog policy now applies to this server, rather than Axum/Tokio's direct-bind default. Peer metadata uses core's `native-socket-address`, preserving direct/client allowlist and forwarded-header behavior. Axum still owns HTTP serving and WebSockets; no HTTP-specific adapter is added to the compiler or platform support.

A root controller uses the prefix `/`. The framework merges root routes and nests non-root controller prefixes, allowing exact root routes, prefixed controllers, and static directories to coexist. Configuration rejects duplicate mount paths rather than assigning order-dependent precedence.

## YAML composition

`parse-config-yaml` accepts application-composition YAML and returns `config-result`. A configuration may provide an `address` and a `mounts` list. Each mount has a `path` and exactly one of:

- `controller`, a stable application-defined controller identifier;
- `static-directory`, a directory served at that path.

For example, the scalar source `address: 127.0.0.1:8080\nmounts:\n  - controller: site\n    path: /\n  - static-directory: public\n    path: /assets` describes one controller and one static directory.

`application.configure` applies the address and static-directory mounts. Application assembly resolves controller identifiers explicitly with `server-config.controller-path` and passes the matching concrete controller to `application.mount`. YAML decides where components are mounted; Terrane source still decides which executable object each stable identifier names. An unknown identifier therefore produces `none` instead of runtime reflection or string-based class construction.

The parser rejects malformed YAML, non-mapping roots, malformed mount entries, relative mount paths, duplicate controller identifiers, duplicate mount paths, and entries that specify both or neither mount kind. `config-result.failed` and `config-result.message` report configuration failures without beginning to serve.

A caller that wants a configuration file reads bounded UTF-8 text through Terrane's filesystem APIs, then passes that text to `parse-config-yaml`. File access remains an explicit application capability instead of being hidden inside the server library.

## Network access policy

`application.allow-direct` and `application.allow-client` maintain independent allowlists. Entries may be individual IPv4 or IPv6 addresses or CIDR networks.

The direct-connection list checks the TCP peer. In a Caddy deployment this normally contains only Caddy's loopback address or private container network. The client-address list checks the externally originating address.

Forwarded headers are trusted only when the direct-connection list is non-empty and the TCP peer matched it. Upstream `client-ip` extracts the rightmost `for` address from RFC 7239 `Forwarded`; only when that header is absent does it use the rightmost `X-Forwarded-For` address. A present malformed, unknown, obfuscated, or missing-`for` standard header never falls back to the legacy header. Configure the trusted edge proxy to emit the authoritative identity consistently.

An empty list is permissive independently of the other list. Invalid addresses and networks fail when serving begins. Rejected requests receive HTTP `403 Forbidden`, including WebSocket upgrade requests.

With no direct allowlist, forwarded headers are ignored and the client IP is the TCP peer. With a trusted proxy, missing or malformed forwarded identity remains `none`, never the proxy's IP: a non-empty client allowlist denies the request, while an empty client allowlist permits it with unresolved identity. A non-empty direct allowlist still rejects peers outside it.

This policy supports a single trusted edge proxy. Rightmost extraction is not multi-hop trusted-chain traversal: deployments with multiple proxy hops must normalize identity at the edge before using this policy.

Header-value parsing is provided by projected upstream `client-ip` with its `forwarded-header` feature, directly over Axum's borrowed `HeaderMap`. Terrane retains trust decisions, independent allowlists, and network matching over projected `ipnet` values. Denied requests convert projected `StatusCode::FORBIDDEN` through Axum's `IntoResponse` implementation. No handwritten header grammar or Rust adapter is maintained here.

The direct `ConnectInfo<SocketAddr>` lookup and `into_make_service_with_connect_info` construction use projected native types. The library has no authored Rust transport crate: parsing, policy, and composition are Terrane code over upstream native dependencies. Package tests and a live HTTP probe exercise allowed and denied peers, forwarded-client selection, legacy fallback, and malformed-header denial.

## WebSockets

Controllers register a WebSocket endpoint with `routes.websocket`. Its reusable asynchronous handler receives a Terrane `websocket-session`, not an Axum extractor or Rust stream.

`websocket-session.receive` returns a `websocket-event`. Text events carry `event.text`; close, binary, ping, and pong events retain their protocol kind. `send-text` sends a text frame and `close` performs an explicit close. Transport failures use the ordinary `dependency-error` contract. The framework catches a handler's dependency failure after upgrade so one failed session does not terminate the HTTP server.

The current typed payload surface is text-first. Binary event payloads and explicit ping/pong payload APIs remain future additions; non-text events are identified without exposing Axum's message enum.

## Statuses, redirects, and HTTP errors

Use `http-response` as the general response mechanism. `create-response(status,
content-type, body)` accepts already-encoded text; `create-json-response(status,
document)` uses Terrane's built-in JSON encoder. Add headers by retaining the result of
`.with-header; name, value`. Annotated handlers can return these responses directly,
without forced status 200 or a second encoding pass. `create-redirect-response(location,
status = 303)` supports 301, 302, 303, 307, and 308.

For early error exits, `throw http-error; 403` generates 403 Forbidden with a `Forbidden`
body. Supply a detail string, or use `throw (create-http-error; response)` to preserve an
explicit 400–599 response's JSON body and custom headers. This also works for text GET and
client-aware GET handlers. Other
exceptions and invalid responses remain HTTP 500, without exposing their details.

Caught Terrane throwables do not currently expose original object fields. As a package-only
implementation, `http-error` transports its response through a private built-in-JSON payload
in the preserved message, with human-readable `render` output. Applications must not mutate
or depend on that payload. Normal success and redirect responses do not use this codec.

The annotation consumer builds OpenAPI and model schemas as document values, then encodes
the complete document with the built-in JSON codec. It does not concatenate JSON syntax.

## Current boundary

Text GET handlers and WebSockets remain available without consumers. `routes.endpoint` adds explicit request/response callbacks for GET, POST, PUT, PATCH, and DELETE. The annotation consumer supports class-based route groups, typed scalar and model request/response values, HTML and plain-text handlers, static directories, and OpenAPI generation. Dependency/state injection, general middleware, authentication metadata, and streaming responses remain outside this surface; applications use typed Terrane APIs rather than native extractor traits, service types, lifetimes, or borrowing.

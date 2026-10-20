# HTTP Server

`streaky/http-server` is a Terrane-native application server for dynamic web applications and static content. Axum, Tower HTTP, and Tokio are implementation details; applications use only Terrane classes, interfaces, and functions.

## Documentation

The [documentation publication](docs/src/manual.yaml) contains the [general documentation](docs/src/records/guides/introduction.yaml), a complete public API reference, and a [getting-started tutorial](docs/src/records/tutorials/first-server.yaml). These are YAML sources in the shared Terrane manual format; prose, declarations, code, and data examples retain their typed structure rather than being flattened into Markdown.

The [annotation guide](docs/src/records/guides/annotations.yaml) describes the package's explicit native Terrane consumer. `endpoint`, `path`, `query`, and `body` metadata generate class-based route adapters, typed scalar and JSON model-body extraction, negotiated JSON/YAML/XML or HTML/text responses, and OpenAPI 3.1 JSON. `route-group` supplies a class mount prefix, schema inclusion, enabled response formats, and their default. Omitting formats preserves JSON-only behavior that ignores `Accept`; an explicit nonempty format list opts into negotiation. Repeatable `static-directory` metadata mounts cwd-relative assets. Signatures determine types and query defaults; malformed inputs return 422, and explicit negotiation returns 406 when no enabled response format is acceptable. Ordinary safe async handlers may throw `throwable`; handler and render failures return HTTP 500.

The [catalog demo](demo/README.md) exercises these capabilities with typed catalog models, AOT template-backed HTML pages, static Stoplight Elements docs, and a prefixed `/api` with OpenAPI at `/api/openapi.json`. Build both native consumers first with `sh packages/http-server/build-consumer.sh`, then build or run the demo through Terrane.

WebSocket upgrades remain publicly authorable with `app = app.websocket; '/events', handler`.
The asynchronous callback receives a `websocket-session`; ordinary HTTP endpoints stay
class-bound. See the [WebSockets guide](docs/src/records/guides/websockets.yaml) for the
complete example, upgrade access-policy behavior, and `Close` versus `closed` events.

## Build prerequisites

The compiler must support canonical `owner/package` Terrane dependency identities
(including dotted manifest keys), core `listen-tcp`, and consuming native TCP
handoff. Older compilers with dependency-alias tables cannot load these manifests.
Keep the sibling `streaky/template` checkout's consumer manifests and protocol
imports aligned with `/terrane/annotation-consumer-protocol`; the HTTP consumer
build depends on that migration as well. Build the selected compiler first, then
run `build-consumer.sh`, which builds the template consumer before the HTTP consumer
using the same `TERRANE` executable. Ship the compiler support and template migration
before this package; a successful build using uncommitted sibling changes is not
evidence that the package can be merged independently.

Projection state is disposable compiler-generated cache under `.trn/projection-cache.json`,
not a package-root lockfile to commit or refresh. Keep `terrane-dependencies.lock` committed:
it records resolved dependency versions, sources, and checksums. CLI and editor language
servers must use the current compiler; stale servers can recreate the retired filename.

## Consumer source generation

The native consumer uses AOT-compiled `.trn.tpl` templates under
`consumer/templates/` for controller skeletons, generated helpers, and model
encoder/decoder blocks. Metadata validation and schema construction remain in Terrane,
as do the hand-assembled per-operation adapter bodies, model import headers,
floating-point codecs, and per-field kind checks. The
[annotation guide](docs/src/records/guides/annotations.yaml) explains these rendering
and quoting boundaries. `build-consumer.sh` builds the template consumer first and
embeds the templates in the HTTP consumer; its installed executable does not need
template files at runtime.

## Application model

Create an application with `create-application`, compose selected annotated API classes through the native consumer integration, add static content with `application.static-directory`, optionally replace the listen address with `application.with-address`, and start it with `application.serve`.

To discover an OS-assigned port before serving, bind a core listener with `listen-tcp; '127.0.0.1:0'`, read its `local-address`, and transfer it with `await app.serve-listener; move listener`. This consumes the existing listener without rebinding; `application.address` is ignored on this path. Both serving methods apply the same network access policies.

Use `app = app.with-connection-limit; 64` to bound concurrently accepted TCP connections. The default is `0` (unlimited), and both serving methods honor the setting. Idle keep-alive connections and upgraded WebSocket sessions retain their slots until their final owner finishes. HTTP/2 streams share their transport's slot. When full, the server stops accepting until a worker becomes available; extra clients are subject to the operating system's listen backlog rather than an application-level HTTP 503 response.

The limit is implemented in Terrane: it eagerly starts `maximum` workers, each with a router handle and a pending accept on the shared core listener. Choose a limit appropriate to the deployment rather than treating it as a cost-free ceiling. A native channel sender is the connection lease: its last drop wakes the worker, including after an upgrade, without an async destructor or a per-connection runtime. Routes and access policies are composed once before serving; constant-size service wrappers attach peer identity and the lease to requests. Accept failures retry after a one-second backoff; per-connection setup and transport failures do not cancel the server. The limit does not change payloads, enable streaming, or trim allocator-retained RSS; use allocation profiles alongside RSS when investigating retention.

Explicit capability profiles need `clocks` for accept-retry backoff. Serving no longer requires `threads` for slot-release finalization.

Failed accepts emit a structured error to stderr with target `http-server.accept` before the one-second backoff. Explicit capability profiles therefore also need `logging`. Persistent failures continue retrying with diagnostics; there is no consecutive-failure shutdown threshold.

**Availability warning:** no header-read or idle timeout is configured by either serving path. `maximum` silent or idle keep-alive clients can occupy every limited slot indefinitely and prevent other requests from being served. Use a trusted deployment or a reverse proxy that bounds upstream connection lifetimes and idle/header waits; the connection limit alone is not slow-client protection. Idle clients release their slots only when they disconnect. No timeout setting is introduced by this API.

WebSocket handlers accept an owned session and may throw `throwable`, including concrete native transport errors. The framework isolates failures to that session: an abrupt disconnect ends the callback and releases its connection slot without terminating the HTTP server.

**WebSocket migration:** handlers that call session operations and previously declared `throws dependency-error` must widen to `throws throwable`. Native failures are not normalized to `dependency-error`; selectively catching one requires importing its native descriptor. A broad `catch throwable` remains available.

WebSocket registrations are installed after mounted/static routes, independent of builder call order. Route conflicts fail during serving setup, before the accept loop starts, in both modes.

Annotated endpoints are instance methods of selected classes that use the public
`api` trait. The framework copies each mounted source into a request-local invocation
and attaches optional request and response contexts to that copy; it never mutates
the mounted source or passes request context as a handler parameter. Mutable response
operations share request-local backing, while ordinary collection state is copied by
value. Applications that need shared state must opt into explicit shared-reference
state, as the catalog demo does.

The `routes` value is internal routing transport, not an application authoring API.
The public model exposes neither Axum's `Router` nor its move-and-return registration
pattern.

Listener creation uses core `listen-tcp`. The unlimited path consumes the listener through `into-native` and uses Axum's serving boundary. The limited path accepts core streams in Terrane and drives projected Hyper connection futures: HTTP/1.1 WebSocket upgrades and cleartext HTTP/2 prior knowledge (h2c) are supported, with the same peer policies and upgrade lifetimes. Limited mode does not enable RFC 8441 extended CONNECT for WebSockets over HTTP/2. A reverse proxy can terminate TLS/browser HTTP/2 and use HTTP/1.1 upgrades upstream. Explicit Hyper, Hyper-util and Tokio declarations provide the projected APIs; they share the compiler-bound runtime owner rather than creating another runtime. Core's automatic backlog policy applies to both paths. No handwritten listener adapter or HTTP-specific compiler/platform-support hook is used.

Two integration limits remain tracked: `networking/native-address-ip` removes the
narrow peer-IP Rust expression once core exposes a reusable native-address accessor
or projected-method boundary; `http-errors/typed-catch-fields` removes the error
payload codec once typed catches expose the original descriptor's fields. These are
removal criteria, not capabilities or scheduled compiler changes.


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

With no direct allowlist, forwarded headers are ignored and the client IP is the TCP peer. With a trusted proxy, missing or malformed forwarded identity remains `none`, never the proxy's IP: a non-empty client allowlist denies the request, while an empty client allowlist permits it with unresolved identity. The request context exposes peer and client addresses separately, as well as whether the direct peer was trusted.

This policy supports a single trusted edge proxy. Rightmost extraction is not multi-hop trusted-chain traversal: deployments with multiple proxy hops must normalize identity at the edge before using this policy. Effective scheme metadata follows trusted-proxy provenance: RFC `Forwarded` proto is considered first, legacy `X-Forwarded-Proto` only when `Forwarded` is absent, and malformed trusted scheme metadata remains unknown (`none`).

Header-value parsing is provided by projected upstream `client-ip` with its `forwarded-header` feature, directly over Axum's borrowed `HeaderMap`. Terrane retains trust decisions, independent allowlists, and network matching over projected `ipnet` values. Denied requests convert projected `StatusCode::FORBIDDEN` through Axum's `IntoResponse` implementation. No handwritten header grammar or Rust adapter is maintained here.

The direct `ConnectInfo<SocketAddr>` lookup and `into_make_service_with_connect_info` construction use projected native types. The library has no authored Rust transport crate: parsing, policy, and composition are Terrane code over upstream native dependencies. Package tests and a live HTTP probe exercise allowed and denied peers, forwarded-client selection, legacy fallback, and malformed-header denial.

## WebSockets

WebSocket endpoints are handled by the framework's internal route registration. Their asynchronous handlers receive a Terrane `websocket-session`, not an Axum extractor or Rust stream.

`websocket-session.receive` returns a `websocket-event`. Text events carry `event.text`; close, binary, ping, and pong events retain their protocol kind. `send-text` sends a text frame and `close` performs an explicit close. Transport failures use the ordinary `dependency-error` contract. The framework catches a handler's dependency failure after upgrade so one failed session does not terminate the HTTP server.

The current typed payload surface is text-first. Binary event payloads and explicit ping/pong payload APIs remain future additions; non-text events are identified without exposing Axum's message enum.

## Response control

Return a typed body (or `none`) and use the class's optional `response` context to
set status, headers, content type/length, `Vary`, or a redirect. The server applies
these overrides after successful handler completion. Throw `http-error` for an early
error status; unexpected handler failures remain HTTP 500.

The listener is plain TCP, not TLS. Terminate TLS at a trusted proxy and rely on
forwarded scheme/identity metadata only when the TCP peer matches the configured
direct allowlist. See the annotation and proxy-security guides for the class API,
request metadata, and trust rules.

The annotation consumer constructs OpenAPI and model schemas as document values and
encodes the result with the built-in JSON codec. `api-documentation` supplies the
document title, version, and overview. Repeatable request/response examples provide
named OpenAPI examples authored as JSON text; request bodies remain JSON regardless
of the negotiated response format.

## Current boundary

The class-bound annotation consumer supports GET, POST, PUT, PATCH, DELETE, HEAD, and
OPTIONS, typed scalar and model request/response values, JSON/YAML/XML response negotiation, HTML and plain-text
handlers, static directories, authored examples, Markdown descriptions, and OpenAPI
generation. HEAD suppresses the body while preserving representation metadata;
metadata-only handlers may provide a known length without retrieving a body. The
catalog demo shares one seeded in-memory store across requests. Dependency/state
injection, general middleware, authentication metadata, and streaming responses
remain outside this surface; applications use typed Terrane APIs rather than native
extractor traits, service types, lifetimes, or borrowing.

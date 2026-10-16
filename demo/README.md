# Annotated catalog demo

A real HTTP service built with class-based annotated APIs, ordinary typed Terrane models, and AOT-compiled templates. One in-memory catalog is seeded at startup; GET/search/quotes read it, PUT and PATCH update it, and DELETE removes items. HEAD checks existence without a body, and OPTIONS advertises the item resource's methods. A separate site serves template-backed HTML pages, Stoplight Elements documentation, and static assets.

From the Terrane repository root:

```console
cargo build --release -p terrane-cli
sh packages/http-server/build-consumer.sh
(cd packages/http-server/demo && "${TERRANE:-../../../target/release/terrane}" run .)
```

The build helper builds both native consumers: the HTTP annotation consumer and the `template` consumer. The default address is `127.0.0.1:3187`. Pass a different address as the application's first argument when launching its built executable.

```console
curl http://127.0.0.1:3187/
curl http://127.0.0.1:3187/about
curl http://127.0.0.1:3187/api/health
curl 'http://127.0.0.1:3187/api/items/1?detailed=true'
curl 'http://127.0.0.1:3187/api/search?term=Terrane+notebook&limit=2'
curl -H 'Content-Type: application/json' -d '{"item-id":1,"quantity":3}' http://127.0.0.1:3187/api/orders/quote
curl http://127.0.0.1:3187/api/openapi.json
curl http://127.0.0.1:3187/docs/
curl http://127.0.0.1:3187/assets/site.css
```

The initial notebook costs 1200 cents, so three cost 3600 cents. The order body is decoded into an `order-input` model; quote responses use `quote-output` and the current stored price. Invalid model bodies and malformed scalar path/query parameters return 422 before a handler runs. Handler or template-render failures return 500. Quotes are not stored; catalog changes last until the process restarts.

## Modify the in-memory catalog

Startup seeds item 1 (the notebook) and item 2 (a pencil costing 250 cents).
The API holds one `shared ref catalog-store`, backed by an ordered map keyed by item ID.
Synchronous store methods keep each read/modify/write operation together; controller copies
share that store rather than independently copying the catalog.

```console
curl -I http://127.0.0.1:3187/api/items/2
curl -i -X OPTIONS http://127.0.0.1:3187/api/items/2
curl -X PUT -H 'Content-Type: application/json' \
  -d '{"name":"Terrane pencil","price-cents":300,"in-stock":true}' \
  http://127.0.0.1:3187/api/items/2
curl -X PATCH -H 'Content-Type: application/json' \
  -d '{"price-cents":350,"in-stock":false}' http://127.0.0.1:3187/api/items/2
curl 'http://127.0.0.1:3187/api/items/2?detailed=true'
curl -H 'Content-Type: application/json' -d '{"item-id":2,"quantity":3}' \
  http://127.0.0.1:3187/api/orders/quote
curl -i -X DELETE http://127.0.0.1:3187/api/items/2
curl -i http://127.0.0.1:3187/api/items/2
```

PUT replaces all editable fields of an existing item; omitted fields reset to model defaults
(empty name, zero price, false availability). PATCH changes only non-null supplied fields;
missing or null fields are unchanged, while zero and false are real updates. It accepts a
partial JSON object, not JSON Patch or JSON Merge Patch. Both return the resulting item in
the negotiated JSON/YAML/XML format. They do not create missing items.

DELETE returns 204 without a body. Deleted identifiers return 404 for reads, writes, quotes,
and repeated deletes. Restarting restores the seed data. HEAD returns 200 or 404 with no
body. OPTIONS returns 204 and `Allow: GET, PUT, PATCH, DELETE, HEAD, OPTIONS`, even for a
missing item; it describes this route and does not implement CORS.

## Site rendering

Templates live in `templates/home.html` and `templates/about.html`; they use a shared header and footer. The AOT `template` consumer compiles these templates into the binary, so edits to template sources require rebuilding. `.html` rendering escapes values automatically; do not apply a safe filter to untrusted values. Each render uses a fresh context. No template source files or runtime template roots are needed in deployment; only public static assets must accompany the executable.

The demo currently constructs the compiled engine inside each `render-page` call and
assigns that request's title and heading. This is a simple isolation example, not a
measured engine-construction performance recommendation. Applications can instead
retain a compiled engine and pass independent context maps to `fetch-context`, as the
HTTP source consumer does. The `/api/health` load baseline does not measure these HTML
paths or establish their throughput.

Missing model fields retain their Terrane defaults; omitted `quantity` is zero. The codecs do not invent domain constraints such as positive quantities. `src/models.trn` defines the models, `src/api.trn` defines the API class, `src/site.trn` defines pages/static annotations, and `src/main.trn` only starts the configured application.

The demo's `///` comments are also its OpenAPI descriptions: parameter comments describe
path/query inputs and request bodies, model comments describe object schemas, and field
comments describe each request/response property, including nested list items. An endpoint's
final `@returns` section describes its response independently of the operation description.
These descriptions appear in `/api/openapi.json`, which the `/docs/` viewer loads; no
separate hand-maintained schema is needed.

The selected `catalog-api` class owns the document-wide `api-documentation` annotation:
its app-authored title is **Terrane Catalog API** and its API version is **0.1.0**.
The class's Markdown documentation supplies the `/docs/` overview, including a quick-start
curl command, representation choices, and catalog conventions. Apps can instead set an
explicit annotation `description`; an empty string suppresses the overview. Only one
selected declaration may own the API information, even when several route groups are mounted.

The catalog API declares illustrative request and response examples with the
`request-example` and `response-example` annotations. The example values are JSON
source text, including scalar path/query values and model-shaped body/response objects.
They document useful calls, not validation rules. The API group enables JSON, YAML,
and XML automatic responses, defaulting to JSON; clients can choose with `Accept`.
Unsupported formats return 406 and negotiated replies vary on `Accept`. Request bodies
continue to use JSON regardless of response format. XML wraps values in a `response`
root, arrays in a wrapper with `item` children, and null with `xsi:nil="true"`.
Explicit `http-response` and the site's HTML routes are not automatically serialized.
The explicit format list is the opt-in: apps that omit it keep legacy JSON-only responses
without rejecting `Accept` preferences. An explicit empty list is an error. Negotiated
validation/error replies keep their usual bodies but also vary on `Accept`.

OpenAPI descriptions preserve CommonMark formatting, so the catalog declarations include
renderable fenced curl examples alongside prose, lists, emphasis, and links. OpenAPI
documents media-specific examples according to the [rich-text formatting](https://spec.openapis.org/oas/v3.1.0.html#rich-text-formatting)
and [Media Type Object](https://spec.openapis.org/oas/v3.1.0.html#media-type-object)
contracts.

The explicit `[consumers.http-server]` manifest selects every model class and field, route group, API/site class, endpoint method, and annotated parameter. The demo also configures the `template` consumer using the dependency's staged executable. `mount-annotated` mounts the selected classes, and the `catalog-api` group prefix is `/api`; OpenAPI is at `/api/openapi.json` and its schema paths include that prefix. The `site` group disables OpenAPI documentation for its HTML methods. It serves `public/docs` at `/docs` and `public/assets` at `/assets`, relative to the process working directory. Run the demo with `demo` as cwd (the Docker image uses `/app`) so these paths resolve correctly. Generated files and consumer executables live beneath `.trn` and must not be edited by hand.

The runtime uses Debian Trixie: the locally prebuilt binary requires glibc 2.38 or newer, which Debian Bookworm does not provide. Build the binary for the container's Linux architecture.

## Behavioral tests

The Python integration suites use PyYAML to parse and verify YAML response bodies.

```console
sh packages/http-server/build-consumer.sh
artifact=$("${TERRANE:-target/release/terrane}" build packages/http-server/demo)
python packages/http-server/tests/integration/test_demo.py "$artifact"
python packages/http-server/tests/integration/test_consumer.py
"${TERRANE:-target/release/terrane}" test packages/http-server
```

The HTTP suite starts the real built application from an isolated directory containing only copied public assets, with no template sources. It checks typed bodies and outputs, defaults, templated HTML pages, static documentation and assets, and the mounted OpenAPI schema. The consumer suite checks malformed declarations through the real compiler/consumer pipeline, plus throwing handlers returning success or HTTP 500 without taking down the server.

## Docker deployment

From the repository root, build the Terrane consumers and demo executable, stage the binary inside the ignored Docker context, then build and start the Compose service:

```console
sh packages/http-server/demo/scripts/build-image.sh
```

The script uses the `TERRANE` environment variable when set, otherwise `target/release/terrane`, matching the consumer helper and integration-test default. Build that release compiler first, or export an absolute `TERRANE` path to use the same alternative compiler for every step. It copies the artifact path printed by `terrane build` into `demo/.docker/app`; Docker receives only that prebuilt executable and the demo's `public/` assets. Templates are compiled into the executable, so the image needs neither source templates nor a template runtime root. The image does not contain or run the Terrane compiler. Compose publishes the service on host port `3187` by default; override it with `HTTP_SERVER_PORT`, for example:

```console
HTTP_SERVER_PORT=8080 sh packages/http-server/demo/scripts/build-image.sh
```

To stop the service, run `docker compose -f packages/http-server/demo/compose.yaml down` from the repository root.

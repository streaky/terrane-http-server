# Annotated catalog demo

A real HTTP service built with class-based annotated APIs, ordinary typed Terrane models, and AOT-compiled templates. The catalog API provides item lookup, search, and stateless order quote pricing; a separate site serves template-backed HTML pages, Stoplight Elements documentation, and static assets.

From the Terrane repository root:

```console
sh packages/http-server/build-consumer.sh
(cd packages/http-server/demo && ../../../target/debug/terrane run .)
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

The notebook costs 1200 cents, so three cost 3600 cents. The order body is decoded into an `order-input` model; quote responses use `quote-output`. Invalid model bodies and malformed scalar path/query parameters return 422 before a handler runs. Handler or template-render failures return 500. Quotes are calculated from the catalog and are not persisted.

Templates live in `templates/home.html` and `templates/about.html`; they use a shared header and footer. The AOT `template` consumer compiles these templates into the binary, so edits to template sources require rebuilding. `.html` rendering escapes values automatically; do not apply a safe filter to untrusted values. Each render uses a fresh context. No template source files or runtime template roots are needed in deployment; only public static assets must accompany the executable.

Missing model fields retain their Terrane defaults; omitted `quantity` is zero. The codecs do not invent domain constraints such as positive quantities. `src/models.trn` defines the models, `src/api.trn` defines the API class, `src/site.trn` defines pages/static annotations, and `src/main.trn` only starts the configured application.

The explicit `[consumers.http-server]` manifest selects every model class and field, route group, API/site class, endpoint method, and annotated parameter. The demo also configures the `template` consumer using the dependency's staged executable. `mount-annotated` mounts the selected classes, and the `catalog-api` group prefix is `/api`; OpenAPI is at `/api/openapi.json` and its schema paths include that prefix. The `site` group disables OpenAPI documentation for its HTML methods. It serves `public/docs` at `/docs` and `public/assets` at `/assets`, relative to the process working directory. Run the demo with `demo` as cwd (the Docker image uses `/app`) so these paths resolve correctly. Generated files and consumer executables live beneath `.trn` and must not be edited by hand.

The runtime uses Debian Trixie: the locally prebuilt binary requires glibc 2.38 or newer, which Debian Bookworm does not provide. Build the binary for the container's Linux architecture.

## Behavioral tests

```console
sh packages/http-server/build-consumer.sh
artifact=$(target/debug/terrane build packages/http-server/demo)
python packages/http-server/tests/integration/test_demo.py "$artifact"
python packages/http-server/tests/integration/test_consumer.py
target/debug/terrane test packages/http-server
```

The HTTP suite starts the real built application from an isolated directory containing only copied public assets, with no template sources. It checks typed bodies and outputs, defaults, templated HTML pages, static documentation and assets, and the mounted OpenAPI schema. The consumer suite checks malformed declarations through the real compiler/consumer pipeline, plus throwing handlers returning success or HTTP 500 without taking down the server.

## Docker deployment

From the repository root, build the Terrane consumers and demo executable, stage the binary inside the ignored Docker context, then build and start the Compose service:

```console
sh packages/http-server/demo/scripts/build-image.sh
```

The script uses the `TERRANE` environment variable when set, otherwise `target/debug/terrane`. It copies the artifact path printed by `terrane build` into `demo/.docker/app`; Docker receives only that prebuilt executable and the demo's `public/` assets. Templates are compiled into the executable, so the image needs neither source templates nor a template runtime root. The image does not contain or run the Terrane compiler. Compose publishes the service on host port `3187` by default; override it with `HTTP_SERVER_PORT`, for example:

```console
HTTP_SERVER_PORT=8080 sh packages/http-server/demo/scripts/build-image.sh
```

To stop the service, run `docker compose -f packages/http-server/demo/compose.yaml down` from the repository root.

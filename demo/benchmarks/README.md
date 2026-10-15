# Running-demo HTTP baseline

This is a standalone load test of the deployed HTTP demo, separate from the compiler benchmark suites. It sends host traffic through the published Docker port to `http://127.0.0.1:3187/api/health`, expecting HTTP 200 and the four-byte JSON body `"ok"`. It never builds, restarts, or configures the service.

## Run

From the repository root:

```sh
# Build only if the retained client binary is unavailable.
go build -trimpath -o packages/http-server/demo/benchmarks/http-burst \
  packages/http-server/demo/benchmarks/http-burst.go

python -B packages/http-server/demo/benchmarks/benchmark-running-demo.py \
  --native-burst packages/http-server/demo/benchmarks/http-burst \
  --runs 7 --requests 5000 --seed 20261015 \
  --output packages/http-server/demo/benchmarks/results/baseline-20261015

python -B packages/http-server/demo/benchmarks/capture-soak.py \
  --seconds 30 \
  --output packages/http-server/demo/benchmarks/results/baseline-soak-20261015
```

`--output` is a filename stem, not a directory; choose a new stem for each run. Existing reports are never overwritten by a capture. Each invocation leaves exactly two files:

- `<stem>.json`: configuration, build/container identities, individual trials and failures, raw ApacheBench output, native burst request timings, TCP/resource observations, and final health. Observations are stored once, not copied into per-trial files and aggregate files.
- `<stem>.md`: derived results, ranges, counter changes, status, and interpretation limits.

There are no permanent TSVs, CSVs, per-trial JSON files, monitoring logs, separate summaries, or checksum manifests. ApacheBench's per-request export flags are omitted; its printed percentiles remain captured. Burst request rows are retained once in JSON. Full socket tables and host CPU dumps are not collected by default. Tracing belongs in a separately requested diagnostic.

Failures and partial trials are saved in the same report pair, with a nonzero process exit. A failure before capture setup completes (for example, an unavailable container) can prevent report creation. Progress is printed to the terminal. Reports are finalized after monitoring stops; a forcibly killed process cannot finalize them.

To regenerate Markdown from saved JSON without generating traffic:

```sh
python -B packages/http-server/demo/benchmarks/summarize.py \
  packages/http-server/demo/benchmarks/results/baseline-20261015.json
```

## Workload

| Experiment | Load |
|---|---|
| ApacheBench fresh connections | 5,000 GETs at concurrency 1, 10, 100, 1,000; no keep-alive |
| ApacheBench keep-alive | Same counts and concurrency with `-k` |
| Fresh HTTP/1.1 bursts | 100, 1,000, 2,000 clients released from a gate; one GET per connection, `Connection: close` |
| Preconnected HTTP/1.1 bursts | Same counts; finish client connects before releasing requests |

The matrix runs one shuffled warm-up round and seven measured rounds, seed `20261015`, serial cells, 0.5-second pauses, health probes, and five-second unloaded intervals at both ends. Failed trials are not retried. ApacheBench has a 60-second external timeout; native burst operations are bounded. `--runs 0 --requests 1000` is an optional preflight, not another mandatory saved result set. Omitting `--native-burst` selects the slower Python/asyncio diagnostic client; do not mix its measurements with native-client baselines.

The sustained capture runs one observation per connection policy at concurrency 1,000, with a 30-second limit and a two-million-request cap. Reaching the cap can finish early. It follows the matrix, never runs concurrently with it, and includes recovery monitoring. Its figures are single observations, not seven-repeat estimates.

## Interpretation

Compare equivalent settings and retained client checksums across runs. Keep the container/image/executable identity, uptime, CPU governor, resource limits, and failures in view. Warm-up does not erase aged-versus-fresh deployment differences.

ApacheBench is a rolling-concurrency generator using its native HTTP behavior (typically HTTP/1.0); the native client sends HTTP/1.1. A gate does not make kernel connects literally simultaneous: dispatch span is recorded. Preconnection completion does not prove server-side acceptance. Burst completion in Markdown is measured after barrier release and excludes pre-release setup.

TCP counters are corroborating evidence, not exact per-request attribution: host counters include unrelated workloads and container counters include health probes. `memory.peak` is historical, not reset per trial. `somaxconn` alone does not establish a listener's configured capacity. Monitoring and clients share the server host; these results are not an isolated language ranking or a maximum-capacity claim.

The earlier aged/replacement result sets were intentionally purged after establishing the deployment's non-regression and improved burst-tail behavior. The current reports establish a new baseline, not a retained before/after archive.

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
  --runs 7 --requests 5000 --seed 20261015 --soak-seconds 30 \
  --output packages/http-server/demo/benchmarks/results/baseline-20261015
```

One invocation runs the matrix followed by both sustained-load policies, writing one combined JSON/Markdown pair. `--output` is a filename stem, not a directory; choose a new stem for each run. Existing reports are never overwritten by a capture. Each invocation leaves exactly two files:

- `<stem>.json`: only report inputs—configuration, container/image identity, per-trial measurements and completed counts (including warm-ups), pass/fail, displayed TCP counter deltas, and any capture error.
- `<stem>.md`: derived results, ranges, counter changes, status, and interpretation limits.

The JSON is enough to recreate the Markdown, not a diagnostic archive. Keep one metric per
trial (`rps` or `post_release_ms`) so medians/ranges can be recomputed, plus completed counts,
pass/fail, trial grouping, and the two displayed counter deltas. Do not retain individual request
rows, raw command output, periodic resource samples, full TCP/resource snapshots, or Docker
inspection/log output. Capture-time checks and observations remain transient; the workload and
failure checks are unchanged. Detailed tracing requires a separate diagnostic run.

The saved baseline combines the earlier sequential matrix and sustained captures offline, after
confirming matching target/container/image identities. All 114 trials and 16 result cells remain;
completed requests total 2,801,276. Sustained rows are labelled `soak`, so they never join the
repeated `ab` matrix estimates. The combined JSON is **25,465 bytes**, versus about 19 MB of
original raw observations. This was a data merge, not a fresh combined measurement.
The previous raw-evidence retention policy is superseded.

Configured output/native-client paths are normalized to workspace-relative paths or redacted.
Full host-local Docker metadata and unused provenance records are omitted rather than copied.
Compaction changes current files only; it does not rewrite prior Git history.

Before publishing, make an explicit decision about historical host paths: current-file
redaction cannot remove earlier commits from clones. Squashing at merge
or rewriting affected history requires a separate owner-approved operation; neither is
performed by the capture/report tools. Historical privacy is separate from retaining only the
inputs needed to recreate the current reports.

Failures and partial trials are saved in the same report pair, with a nonzero process exit. A failure before capture setup completes (for example, an unavailable container) can prevent report creation. Progress is printed to the terminal. Reports are finalized after monitoring stops; a forcibly killed process cannot finalize them.
The final status is `passed` only when at least one trial completed, every trial passed,
and both monitoring and final container/health checks succeeded. Empty captures, failed
trials, interruptions, and final-check failures are saved as `failed`.
If a trial snapshot fails, the failed capture still saves its partial measurements and
original snapshot error. Counter deltas without both snapshots are `null` in JSON and
shown as `unavailable` in Markdown. A grouped counter total is also unavailable if any
of its trials lacks that delta; it is never reported as zero or a partial sum.

Run the report-finalization regressions offline, without Docker access or benchmark traffic:

```sh
python -B packages/http-server/tests/integration/test_benchmark_capture.py
```

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

The matrix runs one shuffled warm-up round and seven measured rounds, seed `20261015`, serial cells, 0.5-second pauses, health probes, and five-second unloaded intervals at both ends. Failed trials are not retried. ApacheBench has a 60-second external timeout; native burst operations are bounded. `--runs 0 --requests 1000 --soak-seconds 1` is an optional shorter preflight; it still runs both sustained policies. Omitting `--native-burst` selects the slower Python/asyncio diagnostic client; do not mix its measurements with native-client baselines.

The sustained phase runs one observation per connection policy at concurrency 1,000, with a `--soak-seconds` limit (default 30) and a two-million-request cap. Reaching the cap can finish early. It follows the matrix, never runs concurrently with it, and includes recovery monitoring. Its `soak` rows are single observations, not seven-repeat `ab` estimates. One capture lifetime checks container identity and health across both phases; a sustained-phase failure still saves the earlier matrix results in the same failed report.

## Interpretation

Compare equivalent configurations, targets, container/image identities, failures, and connection policies across runs. These compact reports reproduce the displayed results, not a full binary/provenance or host-resource audit. Warm-up does not erase aged-versus-fresh deployment differences.

ApacheBench is a rolling-concurrency generator using its native HTTP behavior (typically HTTP/1.0); the native client sends HTTP/1.1. A gate does not make kernel connects literally simultaneous. Per-request timings and dispatch-span details are not retained in these reports. Preconnection completion does not prove server-side acceptance. Burst completion in Markdown is measured after barrier release and excludes pre-release setup.

TCP counters are corroborating evidence, not exact per-request attribution: host counters include unrelated workloads and container counters include health probes. `memory.peak` is historical, not reset per trial. `somaxconn` alone does not establish a listener's configured capacity. Monitoring and clients share the server host; these results are not an isolated language ranking or a maximum-capacity claim.

The earlier aged/replacement result sets were intentionally purged after establishing the deployment's non-regression and improved burst-tail behavior. The current reports establish a new baseline, not a retained before/after archive.

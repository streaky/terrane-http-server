# Running-demo HTTP baseline

Status: **passed**.

Target: http://127.0.0.1:3187/api/health
Container: `e796835c8b0a75b95ff9138ac6ebbe4f9fa1c273eb6f5be4cac04f8b76f4adc7`
Image: `sha256:fa12993863e241ed9a9ccc8f427c2cd17b18304010998c39497871d106e49b7b`

| Test | Concurrency | Policy | Runs | Metric | Median | Min | Max | Passed |
|---|---:|---|---:|---|---:|---:|---:|---|
| ab | 1000 | fresh | 1 | rps | 14383.64 | 14383.64 | 14383.64 | True |
| ab | 1000 | keepalive | 1 | rps | 75096.77 | 75096.77 | 75096.77 | True |

Completed requests including warm-up: **2,431,676**.
All recorded trials passed: **True**.

## TCP counter changes (measured trials)

| Test | Concurrency | Policy | Container listen overflows | Host SYN retransmissions |
|---|---:|---|---:|---:|
| ab | 1000 | fresh | 0 | 7 |
| ab | 1000 | keepalive | 0 | 10 |

## Interpretation

ApacheBench reports requests/s; burst completion is milliseconds after barrier release. Preconnected bursts exclude connection setup from that interval. Sustained observations are single runs, not repeated estimates; a request cap can finish them early.

This measures the published Docker port on a shared host. Client dispatch and other workloads can affect results. Host TCP counters include unrelated traffic; container counters include health probes. Backlog capacity is not established by somaxconn alone. Memory peaks are historical, not reset per trial.

Configuration, identities, individual trials, failures, and resource samples: [baseline-soak-20261015.json](baseline-soak-20261015.json).

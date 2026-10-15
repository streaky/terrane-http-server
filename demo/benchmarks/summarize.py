#!/usr/bin/env python3
"""Read or write the demo's self-contained JSON/Markdown run reports."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics

WORKSPACE = Path(__file__).resolve().parents[4]
COUNTERS = (
    ("container_listen_overflows", "container_tcp", "TcpExt:ListenOverflows"),
    ("host_syn_retransmissions", "host_tcp", "TcpExt:TCPSynRetrans"),
)


def report_data(raw):
    """Reduce in-memory observations to the inputs needed by the Markdown report."""
    configuration = dict(raw["configuration"])
    for key in ("output", "native_burst"):
        if key in configuration:
            configuration[key] = redact_path_argument(configuration[key])
    trials = []
    for row in raw["trials"]:
        result = row["result"]
        before, after = row.get("before"), row.get("after")
        metric = "rps" if row["kind"] in ("ab", "soak") else "post_release_ms"
        trials.append({
            "kind": row["kind"], "concurrency": row["concurrency"],
            "mode": row["mode"], "phase": row["phase"],
            "result": {
                "passed": result["passed"],
                "complete": int(result.get("complete") or result.get("successes") or 0),
                metric: result.get(metric),
            },
            "tcp_counter_deltas": {
                name: after[scope].get(counter, 0) - before[scope].get(counter, 0)
                if before is not None and after is not None else None
                for name, scope, counter in COUNTERS
            },
        })
    data = {
        "status": raw["status"], "configuration": configuration,
        "container_before": {key: raw["container_before"][key] for key in ("id", "image_id")},
        "trials": trials,
    }
    if raw.get("error"):
        data["error"] = raw["error"]
    return data


def summarize(data):
    groups = defaultdict(list)
    for row in data['trials']:
        if row['phase'] == 'measurement':
            groups[row['kind'], row['concurrency'], row['mode']].append(row)
    cells = []
    for (kind, concurrency, mode), rows in sorted(groups.items()):
        metric = 'rps' if kind in ('ab', 'soak') else 'post_release_ms'
        values = [r['result'][metric] for r in rows if r['result'].get(metric) is not None]
        counters = {}
        for name, _, _ in COUNTERS:
            deltas = [row["tcp_counter_deltas"][name] for row in rows]
            counters[name] = None if None in deltas else sum(deltas)
        cells.append(dict(kind=kind, concurrency=concurrency, mode=mode, runs=len(rows),
                          passed=all(r['result']['passed'] for r in rows), metric=metric,
                          median=statistics.median(values) if values else None,
                          min=min(values) if values else None, max=max(values) if values else None,
                          tcp_counter_deltas=counters))
    return cells


def redact_path_argument(value):
    if not isinstance(value, str) or not value.startswith("/"):
        return value
    try:
        return Path(value).resolve().relative_to(WORKSPACE).as_posix()
    except (OSError, ValueError):
        return "[redacted host path]"


def write_report(stem, data):
    # Only compact report inputs are persisted; capture observations stay transient.
    stem.with_suffix('.json').write_text(json.dumps(data, separators=(',', ':')) + '\n')
    cells = summarize(data)
    lines = ['# Running-demo HTTP baseline', '', f"Status: **{data['status']}**.", '',
             f"Target: {data['configuration']['url']}",
             f"Container: `{data['container_before']['id']}`",
             f"Image: `{data['container_before']['image_id']}`", '',
             '| Test | Concurrency | Policy | Runs | Metric | Median | Min | Max | Passed |',
             '|---|---:|---|---:|---|---:|---:|---:|---|']
    for c in cells:
        fmt = lambda v: f'{v:.2f}' if v is not None else 'unavailable'
        lines.append(f"| {c['kind']} | {c['concurrency']} | {c['mode']} | {c['runs']} | {c['metric']} | {fmt(c['median'])} | {fmt(c['min'])} | {fmt(c['max'])} | {c['passed']} |")
    total = sum(row['result']['complete'] for row in data['trials'])
    lines += ['', f'Completed requests including warm-up: **{total:,}**.',
              f"All recorded trials passed: **{all(r['result']['passed'] for r in data['trials']) and bool(data['trials'])}**.", '',
              '## TCP counter changes (measured trials)', '',
              '| Test | Concurrency | Policy | Container listen overflows | Host SYN retransmissions |',
              '|---|---:|---|---:|---:|']
    for c in cells:
        d = c['tcp_counter_deltas']
        counters = ['unavailable' if d[name] is None else str(d[name]) for name, _, _ in COUNTERS]
        lines.append(f"| {c['kind']} | {c['concurrency']} | {c['mode']} | {' | '.join(counters)} |")
    if data.get('error'):
        lines += ['', 'Error: ' + data['error']]
    lines += ['', '## Interpretation', '',
              'ApacheBench reports requests/s; burst completion is milliseconds after barrier release. '
              'Preconnected bursts exclude connection setup from that interval. '
              'Rows labelled soak are sustained single observations, not repeated matrix estimates; a request cap can finish them early.', '',
              'This measures the published Docker port on a shared host. Client dispatch and other workloads can affect results. '
              'Host TCP counters include unrelated traffic; container counters include health probes. '
              'Backlog capacity is not established by somaxconn alone. Memory peaks are historical, not reset per trial.', '',
              'The JSON retains configuration, container/image identities, per-trial report measurements, counts, pass/fail, '
              'and displayed TCP counter deltas. Individual request rows, raw command output, resource samples, and full '
              'snapshots are not retained. Compaction does not rewrite prior Git history.', '',
              f"Inputs to recreate this report: [{stem.name}.json]({stem.name}.json).", '']
    stem.with_suffix('.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path, help='Existing run JSON; regenerates its Markdown without traffic')
    args = parser.parse_args()
    data = json.loads(args.report.read_text())
    write_report(args.report.with_suffix(''), data)


if __name__ == '__main__':
    main()

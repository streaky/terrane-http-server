#!/usr/bin/env python3
"""Read or write the demo's self-contained JSON/Markdown run reports."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


def summarize(data):
    groups = defaultdict(list)
    for row in data['trials']:
        if row['phase'] == 'measurement':
            groups[row['kind'], row['concurrency'], row['mode']].append(row)
    cells = []
    for (kind, concurrency, mode), rows in sorted(groups.items()):
        metric = 'rps' if kind == 'ab' else 'post_release_ms'
        values = [r['result'][metric] for r in rows if r['result'].get(metric) is not None]
        counters = Counter()
        for row in rows:
            for scope in ('container_tcp', 'host_tcp'):
                for key in ('TcpExt:ListenOverflows', 'TcpExt:ListenDrops', 'TcpExt:TCPSynRetrans', 'Tcp:RetransSegs'):
                    counters[f'{scope}:{key}'] += row['after'][scope].get(key, 0) - row['before'][scope].get(key, 0)
        cells.append(dict(kind=kind, concurrency=concurrency, mode=mode, runs=len(rows),
                          passed=all(r['result']['passed'] for r in rows), metric=metric,
                          median=statistics.median(values) if values else None,
                          min=min(values) if values else None, max=max(values) if values else None,
                          tcp_counter_deltas=dict(counters)))
    return cells


def write_report(stem, data):
    # Raw observations are stored once. Summaries are derived for Markdown only.
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
    total = sum(int(r['result'].get('complete') or r['result'].get('successes') or 0) for r in data['trials'])
    lines += ['', f'Completed requests including warm-up: **{total:,}**.',
              f"All recorded trials passed: **{all(r['result']['passed'] for r in data['trials']) and bool(data['trials'])}**.", '',
              '## TCP counter changes (measured trials)', '',
              '| Test | Concurrency | Policy | Container listen overflows | Host SYN retransmissions |',
              '|---|---:|---|---:|---:|']
    for c in cells:
        d = c['tcp_counter_deltas']
        lines.append(f"| {c['kind']} | {c['concurrency']} | {c['mode']} | {d['container_tcp:TcpExt:ListenOverflows']} | {d['host_tcp:TcpExt:TCPSynRetrans']} |")
    if data.get('error'):
        lines += ['', 'Error: ' + data['error']]
    lines += ['', '## Interpretation', '',
              'ApacheBench reports requests/s; burst completion is milliseconds after barrier release. '
              'Preconnected bursts exclude connection setup from that interval. '
              'Sustained observations are single runs, not repeated estimates; a request cap can finish them early.', '',
              'This measures the published Docker port on a shared host. Client dispatch and other workloads can affect results. '
              'Host TCP counters include unrelated traffic; container counters include health probes. '
              'Backlog capacity is not established by somaxconn alone. Memory peaks are historical, not reset per trial.', '',
              f"Configuration, identities, individual trials, failures, and resource samples: [{stem.name}.json]({stem.name}.json).", '']
    stem.with_suffix('.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path, help='Existing run JSON; regenerates its Markdown without traffic')
    args = parser.parse_args()
    data = json.loads(args.report.read_text())
    write_report(args.report.with_suffix(''), data)


if __name__ == '__main__':
    main()

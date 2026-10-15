#!/usr/bin/env python3
"""Bounded sustained-load observations, saved as one JSON/Markdown pair."""
import argparse
import importlib.util
import json
from pathlib import Path
import time

spec = importlib.util.spec_from_file_location('demo_capture', Path(__file__).with_name('benchmark-running-demo.py'))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Output filename stem, without extension')
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--container', default='terrane-http-server-demo-http-server-demo-1')
    parser.add_argument('--url', default='http://127.0.0.1:3187/api/health')
    args = parser.parse_args()
    with helper.Capture(args) as capture:
        capture.data['configuration'].update(concurrency=1000, request_cap=2000000,
            soak_driver_sha256=helper.hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        time.sleep(5)
        for keepalive in (False, True):
            mode = 'keepalive' if keepalive else 'fresh'
            record = dict(kind='ab', concurrency=1000, mode=mode, phase='measurement',
                          before=capture.observe(), result={'passed': False})
            capture.data['trials'].append(record)
            argv = ['ab', '-q', '-s', '10', '-t', str(args.seconds), '-n', '2000000', '-c', '1000']
            if keepalive:
                argv.append('-k')
            argv.append(args.url)
            started = time.perf_counter()
            try:
                raw = helper.command(argv, timeout=args.seconds+30)
                record['command'] = raw
                result = helper.parse_ab(raw['stdout'])
                result['passed'] = raw['returncode'] == 0 and result['complete'] is not None and result['complete'] > 0 and result['failed'] == 0 and result['non_2xx'] == 0 and result['body_bytes'] == 4
                record['result'] = result
            except helper.subprocess.TimeoutExpired as error:
                record['command'] = dict(argv=error.cmd, returncode=None, stdout=str(error.stdout or ''), stderr=str(error.stderr or ''))
                raise
            finally:
                record.update(after=capture.observe(), wall_seconds=time.perf_counter()-started)
            print(mode, json.dumps(result), flush=True)
            helper.health(args.url)
            time.sleep(5)
        time.sleep(10)
    print('Sustained-load capture complete:', args.output)


if __name__ == '__main__':
    main()

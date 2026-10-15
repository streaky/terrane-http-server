#!/usr/bin/env python3
"""Read-only running-container baseline; never builds, restarts, or configures a service."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import re
import resource
import statistics
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import urlopen
from summarize import write_report


def command(argv, timeout=30):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return {"argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def inspect(container):
    value = command(["docker", "inspect", container])
    if value["returncode"]:
        raise RuntimeError(value)
    obj = json.loads(value["stdout"])[0]
    # Deliberately omit environment variables and other potentially secret configuration.
    return {"id": obj["Id"], "image_id": obj["Image"], "image_name": obj["Config"]["Image"],
            "state": obj["State"], "restart_count": obj["RestartCount"], "labels": obj["Config"].get("Labels"),
            "limits": {k: obj["HostConfig"].get(k) for k in ("Memory", "NanoCpus", "CpuQuota", "CpuPeriod", "CpusetCpus", "Ulimits", "PidsLimit")},
            "ports": obj["NetworkSettings"]["Ports"], "mounts": obj["Mounts"]}


def text(path):
    try:
        return Path(path).read_text()
    except OSError as error:
        return {"unavailable": str(error)}


def tcp_counters(pid=None):
    root = f"/proc/{pid}/net" if pid else "/proc/net"
    result = {}
    for name in ("netstat", "snmp"):
        raw = text(f"{root}/{name}")
        if not isinstance(raw, str):
            result[name] = raw
            continue
        lines = raw.splitlines()
        for keys, values in zip(lines[::2], lines[1::2]):
            for key, value in zip(keys.split()[1:], values.split()[1:]):
                result[keys.split()[0] + key] = int(value)
    return result


def resource_snapshot(cgroup):
    return {name: text(cgroup / name) for name in ("cpu.stat", "memory.current", "memory.peak", "memory.events", "pids.current", "io.stat")}


def snapshot(pid, cgroup):
    return {"utc": datetime.now(timezone.utc).isoformat(), "monotonic": time.monotonic(),
            "container_tcp": tcp_counters(pid), "host_tcp": tcp_counters(), "resources": resource_snapshot(cgroup),
            "host_loadavg": text("/proc/loadavg")}


def health(url):
    with urlopen(url, timeout=10) as response:
        body = response.read()
        if response.status != 200 or body != b'"ok"':
            raise RuntimeError((response.status, body))
        return {"status": response.status, "body": body.decode(), "headers": dict(response.headers)}


def percentile(values, percent):
    values = sorted(values)
    return values[min(len(values)-1, max(0, int((len(values)-1) * percent / 100)))] if values else None


async def burst(url, count, preconnected):
    target = urlsplit(url)
    t0 = time.perf_counter()
    gate = asyncio.Event()
    rows = [{"index": i} for i in range(count)]
    connections = [None] * count
    request = f"GET {target.path} HTTP/1.1\r\nHost: {target.netloc}\r\nConnection: close\r\n\r\n".encode()
    def stamp():
        return (time.perf_counter() - t0) * 1000
    async def connect(i):
        rows[i]["connect_start_ms"] = stamp()
        connections[i] = await asyncio.wait_for(asyncio.open_connection(target.hostname, target.port or 80), 10)
        rows[i]["connected_ms"] = stamp()
    async def exchange(i):
        await gate.wait()
        try:
            async with asyncio.timeout(15):
                if not preconnected:
                    await connect(i)
                if connections[i] is None:
                    return
                reader, writer = connections[i]
                rows[i]["request_start_ms"] = stamp()
                writer.write(request)
                await writer.drain()
                first = await reader.readexactly(1)
                rows[i]["first_byte_ms"] = stamp()
                headers = first + await reader.readuntil(b"\r\n\r\n")
                parts = headers.decode("ascii").split("\r\n")
                status = int(parts[0].split()[1])
                fields = dict(line.split(":", 1) for line in parts[1:] if ":" in line)
                fields = {k.lower(): v.strip() for k, v in fields.items()}
                body = await reader.readexactly(int(fields["content-length"]))
                rows[i].update(status=status, body=body.decode(), complete_ms=stamp())
                if status != 200 or body != b'"ok"':
                    raise ValueError(f"unexpected response {status}: {body!r}")
        except Exception as error:
            rows[i]["error"] = f"{type(error).__name__}: {error}"
        finally:
            if connections[i] is not None:
                writer = connections[i][1]
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, TimeoutError):
                    pass
    if preconnected:
        async def prepare(i):
            try:
                await connect(i)
            except Exception as error:
                rows[i]["error"] = f"{type(error).__name__}: {error}"
        await asyncio.gather(*(prepare(i) for i in range(count)))
    tasks = [asyncio.create_task(exchange(i)) for i in range(count)]
    await asyncio.sleep(0)
    release_ms = stamp()
    gate.set()
    await asyncio.gather(*tasks)
    completed = [r for r in rows if "complete_ms" in r and "error" not in r]
    elapsed = (time.perf_counter() - t0) * 1000
    latency = [r["complete_ms"] - (release_ms if preconnected else r["connect_start_ms"]) for r in completed]
    starts = [r["request_start_ms"] if preconnected else r["connect_start_ms"] for r in rows if ("request_start_ms" if preconnected else "connect_start_ms") in r]
    return {"rows": rows, "successes": len(completed), "failures": count-len(completed), "batch_ms": elapsed,
            "release_ms": release_ms, "post_release_ms": elapsed-release_ms, "dispatch_span_ms": max(starts)-min(starts),
            "latency_p50_ms": percentile(latency, 50), "latency_p95_ms": percentile(latency, 95), "latency_p99_ms": percentile(latency, 99),
            "client_note": "One asyncio event-loop process; dispatch span is measured, not literally simultaneous kernel connects. No retries."}


def parse_ab(raw):
    result = {}
    for label, key in (("Complete requests", "complete"), ("Failed requests", "failed"), ("Non-2xx responses", "non_2xx"),
                       ("Keep-Alive requests", "keep_alive"), ("Requests per second", "rps"), ("Time taken for tests", "seconds"), ("Document Length", "body_bytes")):
        match = re.search(r"^" + re.escape(label) + r":\s+([\d.]+)", raw, re.M)
        result[key] = float(match.group(1)) if match else (0 if key in ("non_2xx", "keep_alive") else None)
    for p in (50, 95, 99, 100):
        match = re.search(rf"^\s*{p}%\s+(\d+)", raw, re.M)
        result[f"p{p}_ms"] = float(match.group(1)) if match else None
    return result


class Capture:
    """Own one capture and always finish its two report files, including on failure."""

    def __init__(self, args):
        self.args = args
        self.stem = args.output
        if self.stem.suffix:
            raise ValueError("--output must be a filename stem, without an extension")
        self.stem.parent.mkdir(parents=True, exist_ok=True)
        if any(self.stem.with_suffix(ext).exists() for ext in (".json", ".md")):
            raise FileExistsError(self.stem)
        self.initial = inspect(args.container)
        self.pid = self.initial["state"]["Pid"]
        name = Path(f"/proc/{self.pid}/cgroup").read_text().strip().split("::", 1)[1]
        self.cgroup = Path("/sys/fs/cgroup") / name.lstrip("/")
        native = getattr(args, "native_burst", None)
        self.data = {
            "status": "running",
            "configuration": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
            "container_before": self.initial,
            "environment": {
                "utc": datetime.now(timezone.utc).isoformat(), "uname": list(os.uname()),
                "client_fd_limit": resource.getrlimit(resource.RLIMIT_NOFILE), "cpu_count": os.cpu_count(),
                "cpu_governor": text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
                "somaxconn": text("/proc/sys/net/core/somaxconn"),
                "ephemeral_ports": text("/proc/sys/net/ipv4/ip_local_port_range"),
                "ab": command(["ab", "-V"]),
                "binary_sha256": command(["docker", "exec", args.container, "sha256sum", "/app/app"]),
                "native_client_sha256": hashlib.sha256(native.read_bytes()).hexdigest() if native else None,
                "native_source_sha256": hashlib.sha256(Path(__file__).with_name("http-burst.go").read_bytes()).hexdigest(),
                "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "reporter_sha256": hashlib.sha256(Path(__file__).with_name("summarize.py").read_bytes()).hexdigest(),
                "initial_health": health(args.url)},
            "trials": [], "samples": []}
        self.stop = threading.Event()

    def observe(self):
        return snapshot(self.pid, self.cgroup)

    def __enter__(self):
        def monitor():
            try:
                while not self.stop.is_set():
                    self.data["samples"].append(self.observe())
                    self.stop.wait(0.5)
            except Exception as error:
                self.data["monitor_error"] = repr(error)
        self.thread = threading.Thread(target=monitor)
        self.thread.start()
        return self

    def __exit__(self, kind, error, traceback):
        self.stop.set()
        self.thread.join()
        final_error = None
        try:
            final = inspect(self.args.container)
            self.data["container_after"] = final
            self.data["final_health"] = health(self.args.url)
            self.data["recent_container_logs"] = command(["docker", "logs", "--since", self.initial["state"]["StartedAt"], "--tail", "100", self.args.container])
            if final["id"] != self.initial["id"] or final["state"]["StartedAt"] != self.initial["state"]["StartedAt"] or final["restart_count"] != self.initial["restart_count"]:
                raise RuntimeError("Container identity/start time/restart count changed")
            if self.data.get("monitor_error"):
                raise RuntimeError(self.data["monitor_error"])
        except Exception as caught:
            final_error = caught
        self.data["status"] = "failed" if error or final_error or not self.data["trials"] or not all(r["result"]["passed"] for r in self.data["trials"]) else "passed"
        if error or final_error:
            self.data["error"] = repr(error or final_error)
        write_report(self.stem, self.data)
        if final_error and error is None:
            raise final_error
        if error is None and self.data["status"] != "passed":
            raise RuntimeError("One or more trials failed; see saved report")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="terrane-http-server-demo-http-server-demo-1")
    parser.add_argument("--url", default="http://127.0.0.1:3187/api/health")
    parser.add_argument("--output", type=Path, required=True, help="Output filename stem (writes .json and .md)")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--requests", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--native-burst", type=Path, help="Retained native burst binary; otherwise uses Python")
    args = parser.parse_args()
    with Capture(args) as capture:
        capture.data["configuration"]["warmups"] = 1
        time.sleep(5)
        cells = [("ab", n, keep) for n in (1, 10, 100, 1000) for keep in (False, True)]
        cells += [("burst", n, pre) for n in (100, 1000, 2000) for pre in (False, True)]
        rng = random.Random(args.seed)
        for round_index in range(args.runs + 1):
            order = cells[:]
            rng.shuffle(order)
            for kind, concurrency, mode in order:
                mode_name = ("preconnected" if mode else "fresh") if kind == "burst" else ("keepalive" if mode else "fresh")
                name = f"r{round_index:02}-{kind}-c{concurrency}-{mode_name}"
                record = {"name": name, "kind": kind, "concurrency": concurrency, "mode": mode_name,
                          "round": round_index, "phase": "warmup" if round_index == 0 else "measurement",
                          "before": capture.observe(), "result": {"passed": False}}
                capture.data["trials"].append(record)
                started = time.perf_counter()
                usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
                self_before = resource.getrusage(resource.RUSAGE_SELF)
                try:
                    if kind == "ab":
                        argv = ["ab", "-q", "-s", "10", "-n", str(args.requests), "-c", str(concurrency)]
                        if mode:
                            argv.append("-k")
                        argv.append(args.url)
                        raw = command(argv, timeout=60)
                        record["command"] = raw
                        result = parse_ab(raw["stdout"])
                        result["passed"] = raw["returncode"] == 0 and result["complete"] == args.requests and result["failed"] == 0 and result["non_2xx"] == 0 and result["body_bytes"] == 4
                    elif args.native_burst:
                        argv = [str(args.native_burst.resolve()), "--url", args.url, "--count", str(concurrency)]
                        if mode:
                            argv.append("--preconnected")
                        raw = command(argv, timeout=60)
                        record["command"] = {k: v for k, v in raw.items() if k != "stdout"}
                        if raw["returncode"] != 0:
                            record["command"]["stdout"] = raw["stdout"]
                            raise RuntimeError(raw)
                        result = json.loads(raw["stdout"])
                        result["passed"] = result["failures"] == 0
                    else:
                        result = asyncio.run(burst(args.url, concurrency, mode))
                        result["passed"] = result["failures"] == 0
                    record["result"] = result
                except subprocess.TimeoutExpired as error:
                    record["command"] = {"argv": error.cmd, "returncode": None,
                                         "stdout": str(error.stdout or ""), "stderr": str(error.stderr or "")}
                    raise
                finally:
                    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                    self_after = resource.getrusage(resource.RUSAGE_SELF)
                    record.update(wall_seconds=time.perf_counter()-started, after=capture.observe(),
                                  child_cpu_seconds=usage_after.ru_utime+usage_after.ru_stime-usage_before.ru_utime-usage_before.ru_stime,
                                  driver_cpu_seconds=self_after.ru_utime+self_after.ru_stime-self_before.ru_utime-self_before.ru_stime)
                print(name, json.dumps({k: v for k, v in result.items() if k != "rows"}), flush=True)
                health(args.url)
                time.sleep(0.5)
        time.sleep(5)
    print("Capture complete:", args.output, flush=True)


if __name__ == "__main__":
    main()

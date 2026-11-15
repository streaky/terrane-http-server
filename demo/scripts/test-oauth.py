#!/usr/bin/env python3
"""Build the real demo/client, then run owned loopback OAuth acceptance tests."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys


DEMO = Path(__file__).resolve().parents[1]
ROOT = DEMO.parents[2]


def stop_owned_group(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()


def run_owned(command, capture=False):
    with subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None, text=True,
            start_new_session=True) as child:
        try:
            output, _ = child.communicate(timeout=600)
        except BaseException:
            stop_owned_group(child)
            raise
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command)
        return output or ""


def build(compiler, package):
    output = run_owned([compiler, "build", str(package)], capture=True)
    for line in reversed(output.splitlines()):
        artifact = Path(line.strip())
        if not artifact.is_absolute():
            artifact = ROOT / artifact
        if artifact.is_file() and os.access(artifact, os.X_OK):
            return str(artifact)
    raise RuntimeError("Compiler did not report a built executable")


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Include existing catalog/context/WebSocket demo checks")
    options = parser.parse_args()
    selected = os.environ.get("TERRANE", "terrane")
    compiler = shutil.which(selected)
    if compiler is None:
        raise RuntimeError("Set TERRANE to a current installed compiler executable")
    compiler = str(Path(compiler).resolve())
    demo = build(compiler, DEMO)
    client = build(compiler, DEMO / "oauth-client")
    command = [sys.executable, "-B", str(DEMO.parent / "tests/integration/test_demo.py"),
        demo, "--oauth-client", client]
    if not options.all:
        command.extend(["OAuthDisabledTests", "OAuthProtocolTests", "OAuthClientTests"])
    command.append("-v")
    run_owned(command)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    main()

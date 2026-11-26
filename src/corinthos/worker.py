# SPDX-License-Identifier: MPL-2.0
"""A detached worker owns provider descendants and its runtime lease until reaped."""

import argparse
import subprocess
import threading
import time

from corinthos.filesystem import file_lock, read_json
from corinthos.process_control import start_process
from corinthos.runtime import _change, _run_directory, get
from corinthos.runtime_gate import client_runtime


def execute(root, run_id):
    target = _run_directory(root, run_id)
    run = get(root, run_id)
    with file_lock(target / "owner.lock", blocking=False) as owner, client_runtime():
        managed = None
        reader = writer = None
        status, error = "FAILED", None
        try:
            if (target / "cancel.json").exists():
                status, error = "CANCELLED", "Cancelled before provider launch."
                return
            request = read_json(target / "request.json")
            prompt = (target / "prompt.txt").read_bytes()
            managed = start_process(
                request["command"],
                owner_fd=owner.fileno(),
                cwd=run["workspace"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
            )
            process = managed.process
            _change(root, run_id, status="RUNNING")
            exceeded = threading.Event()

            def read():
                total = 0
                with (target / "output.txt").open("wb") as output:
                    while data := process.stdout.read(4096):
                        total += len(data)
                        if total > 1024 * 1024:
                            exceeded.set()
                            break
                        output.write(data)
                        output.flush()

            def write():
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

            reader, writer = (
                threading.Thread(target=read, daemon=True),
                threading.Thread(target=write, daemon=True),
            )
            reader.start()
            writer.start()
            deadline = time.monotonic() + request["timeout"]
            while process.poll() is None:
                if (target / "cancel.json").exists():
                    status, error = "CANCELLED", "Cancelled by caller."
                    break
                if time.monotonic() > deadline or exceeded.is_set():
                    status, error = "FAILED", "Provider exceeded its time or output limit."
                    break
                time.sleep(0.05)
            else:
                status = "SUCCEEDED" if process.returncode == 0 else "FAILED"
                error = None if process.returncode == 0 else "Provider process failed."
            if exceeded.is_set():
                status, error = "FAILED", "Provider exceeded its output limit."
        except Exception as failure:
            status, error = "FAILED", str(failure)[:2048]
        finally:
            if managed is not None:
                cleaned = managed.finish()
                if not cleaned:
                    _change(
                        root,
                        run_id,
                        status="RUNNING",
                        error="Provider cleanup is unconfirmed; runtime remains busy.",
                    )
                    # Do not release ownership while ordinary descendants may remain.
                    while not managed.finish():
                        time.sleep(0.25)
                for thread in (reader, writer):
                    if thread is not None:
                        thread.join(timeout=1)
                if error is None and managed.returncode != 0:
                    status, error = "FAILED", "Provider process failed."
                if reader is not None and exceeded.is_set():
                    status, error = "FAILED", "Provider exceeded its output limit."
                for stream in (managed.process.stdin, managed.process.stdout):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            _change(root, run_id, status=status, error=error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    execute(args.root, args.run_id)


if __name__ == "__main__":
    main()

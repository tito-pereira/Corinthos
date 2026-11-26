# SPDX-License-Identifier: MPL-2.0
"""Real bounded child processes test lifecycle ownership without agent calls."""

import os
import subprocess
import sys
import time

import pytest

from corinthos import process_control
from corinthos.process_control import ProcessStartError, start_process


def test_normal_exit_cleanup_is_confirmed_and_idempotent():
    managed = start_process(
        [sys.executable, "-c", "print('ordinary child')"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = managed.process.communicate(timeout=10)
    assert managed.process.returncode == 0 and stdout.strip() == b"ordinary child" and not stderr
    assert managed.finish() is True and managed.finish() is True


def test_remaining_child_cannot_write_after_parent_success(tmp_path):
    marker = tmp_path / "late.txt"
    release = tmp_path / "release.txt"
    child = (
        "import time; from pathlib import Path; release=Path("
        + repr(str(release))
        + "); deadline=time.monotonic()+5\n"
        "while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        "if release.exists(): Path(" + repr(str(marker)) + ").write_text('late')"
    )
    parent = (
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print('parent success')"
    )
    managed = start_process(
        [sys.executable, "-c", parent], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = managed.process.communicate(timeout=10)
    assert managed.process.returncode == 0 and stdout.strip() == b"parent success" and not stderr
    cleaned = managed.finish()
    if os.name == "nt":
        assert cleaned is True
    release.write_text("Attempt a late write only after cleanup.", encoding="utf-8")
    time.sleep(0.2)
    assert not marker.exists()


def test_finish_stops_live_parent_and_descendants(tmp_path):
    marker = tmp_path / "late.txt"
    managed = start_process(
        [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; time.sleep(2); Path("
            + repr(str(marker))
            + ").write_text('late')",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert managed.finish() is True
    assert managed.process.poll() is not None and not marker.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux provider guardian subreaping.")
def test_guardian_keeps_lock_until_children_die_after_supervisor_crash(tmp_path):
    import fcntl
    from pathlib import Path

    lock_path = tmp_path / "ownership.lock"
    marker = tmp_path / "provider-ids"
    late = tmp_path / "late"
    release = tmp_path / "release"
    helper = Path(process_control.__file__).resolve()
    descendant = (
        "import time; from pathlib import Path\n"
        f"release, late = Path({str(release)!r}), Path({str(late)!r})\n"
        "while not release.exists(): time.sleep(.01)\n"
        "late.write_text('must not execute')"
    )
    provider = (
        "import os,subprocess,sys,time; from pathlib import Path\n"
        f"child=subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
        f"Path({str(marker)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(30)"
    )
    supervisor = (
        "import fcntl, importlib.util, sys, time\n"
        f"spec = importlib.util.spec_from_file_location('process_control', {str(helper)!r})\n"
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        f"lock = open({str(lock_path)!r}, 'xb'); fcntl.flock(lock, fcntl.LOCK_EX)\n"
        f"managed = module.start_process([sys.executable, '-c', {provider!r}], owner_fd=lock.fileno())\n"
        "time.sleep(30)"
    )
    with subprocess.Popen([sys.executable, "-c", supervisor]) as process:
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.02)
            provider_pid, child_pid = map(int, marker.read_text().split())
            with lock_path.open("rb") as another:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process.kill()
                process.wait(timeout=3)
                deadline = time.monotonic() + 8
                while True:
                    try:
                        fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        assert time.monotonic() < deadline
                        time.sleep(0.02)
                assert not Path(f"/proc/{provider_pid}").exists()
                assert not Path(f"/proc/{child_pid}").exists()
                release.touch()
                assert not late.exists()
        finally:
            if process.poll() is None:
                process.kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended job assignment boundary.")
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_assignment_failure_never_runs_suspended_user_code(tmp_path, monkeypatch, error):
    marker = tmp_path / "must-not-run.txt"

    def fail(self, process):
        raise error("Synthetic job assignment failure")

    monkeypatch.setattr(process_control._WindowsJob, "assign_and_resume", fail)
    with pytest.raises(ProcessStartError if error is OSError else KeyboardInterrupt) as failure:
        start_process(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('unsafe')",
            ]
        )
    if error is OSError:
        assert failure.value.cleanup_confirmed is True
    time.sleep(0.1)
    assert not marker.exists()

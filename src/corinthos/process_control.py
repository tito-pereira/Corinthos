# SPDX-License-Identifier: MPL-2.0
"""Own ordinary child-process trees before execution and verify their cleanup.

Windows jobs and POSIX process groups contain ordinary descendants. They are
process-lifecycle controls, not a sandbox against deliberate OS escape APIs.
"""

import ctypes
import os
import select
import signal
import subprocess
import sys
from threading import Lock
from time import monotonic, sleep


class ProcessStartError(OSError):
    """Containment failed; report whether the suspended process was cleaned up."""

    def __init__(self, *, cleanup_confirmed):
        super().__init__("Cannot establish ownership of the local process tree.")
        self.cleanup_confirmed = cleanup_confirmed


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


class _WindowsJob:
    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, pointer = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
        signatures = {
            "CreateJobObjectW": ([pointer, ctypes.c_wchar_p], handle),
            "SetInformationJobObject": ([handle, ctypes.c_int, pointer, dword], ctypes.c_int),
            "AssignProcessToJobObject": ([handle, handle], ctypes.c_int),
            "TerminateJobObject": ([handle, ctypes.c_uint], ctypes.c_int),
            "QueryInformationJobObject": (
                [handle, ctypes.c_int, pointer, dword, pointer],
                ctypes.c_int,
            ),
            "CloseHandle": ([handle], ctypes.c_int),
            "CreateToolhelp32Snapshot": ([dword, dword], handle),
            "Thread32First": ([handle, pointer], ctypes.c_int),
            "Thread32Next": ([handle, pointer], ctypes.c_int),
            "OpenThread": ([dword, ctypes.c_int, dword], handle),
            "OpenProcess": ([dword, ctypes.c_int, dword], handle),
            "ResumeThread": ([handle], dword),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Cannot create a private process job.")
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise OSError("Cannot configure a private process job.")

    def assign_and_resume(self, process):
        # Popen closes CreateProcess's primary-thread handle. Enumerate only the
        # still-suspended child's documented thread IDs after assigning the job.
        child = self.api.OpenProcess(0x0101, False, process.pid)  # SET_QUOTA | TERMINATE
        if not child:
            raise OSError("Cannot open the suspended process for job assignment.")
        try:
            if not self.api.AssignProcessToJobObject(self.handle, child):
                raise OSError("Cannot assign the suspended process to its private job.")
        finally:
            self.api.CloseHandle(child)
        snapshot = self.api.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("Cannot inspect the suspended process thread.")
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = self.api.Thread32First(snapshot, ctypes.byref(entry))
            if not found:
                raise OSError("The suspended process thread is unavailable.")
            threads = []
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    threads.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18 or len(threads) != 1:  # NO_MORE_FILES
                raise OSError("The suspended process thread inventory is incomplete.")
            thread = self.api.OpenThread(0x0002, False, threads[0])  # SUSPEND_RESUME
            if not thread:
                raise OSError("Cannot open the suspended process thread.")
            try:
                if self.api.ResumeThread(thread) != 1:
                    raise OSError("Cannot safely resume the owned process.")
            finally:
                self.api.CloseHandle(thread)
        finally:
            self.api.CloseHandle(snapshot)

    def finish(self):
        if not self.handle:
            return False
        try:
            if not self.api.TerminateJobObject(self.handle, 1):
                return False
            deadline = monotonic() + 3
            while True:
                accounting = _Accounting()
                if not self.api.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                ):
                    return False
                if accounting.ActiveProcesses == 0:
                    return True
                if monotonic() >= deadline:
                    return False
                sleep(0.01)
        finally:
            self.close()

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            self.api.CloseHandle(handle)


class ManagedProcess:
    """A Popen plus the lifecycle boundary owned by this caller."""

    def __init__(self, process, job=None, *, reap_descendants=False):
        self.process, self._job = process, job
        self._lock, self._finished = Lock(), None
        self._reap_descendants = reap_descendants

    @property
    def returncode(self):
        return self.process.returncode

    def group_gone(self):
        if self._reap_descendants:
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return True
        return False

    def finish(self):
        """Stop remaining owned descendants even when their parent exited normally."""
        with self._lock:
            if self._finished is not None:
                return self._finished
            confirmed = False
            try:
                if self._job is not None:
                    confirmed = self._job.finish()
                    self.process.wait(timeout=3)
                else:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=3)
                    deadline = monotonic() + 3
                    while True:
                        if self.group_gone():
                            confirmed = True
                            break
                        if monotonic() >= deadline:
                            break
                        sleep(0.01)
            except (OSError, subprocess.TimeoutExpired):
                confirmed = False
            self._finished = confirmed
            return confirmed


def start_process(command, *, owner_fd=None, **kwargs):
    """Start an owned process; Windows execution remains suspended until assigned."""
    if kwargs.get("shell", False) or kwargs.get("preexec_fn") is not None:
        raise OSError("Owned processes require direct native execution.")
    if owner_fd is not None and os.name != "nt":
        return _start_guarded(command, owner_fd, **kwargs)
    job = None
    process = None
    try:
        if os.name == "nt":
            flags = kwargs.pop("creationflags", 0)
            if flags not in (0, subprocess.CREATE_NEW_PROCESS_GROUP):
                raise OSError("Unsupported process creation flags.")
            kwargs.pop("start_new_session", None)
            job = _WindowsJob()
            kwargs["creationflags"] = 0x00000004 | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        if job is not None:
            job.assign_and_resume(process)
        return ManagedProcess(process, job)
    except BaseException as error:
        cleaned = True
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                cleaned = False
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        cleaned = False
        if job is not None:
            if process is not None:
                cleaned = job.finish() and cleaned
            else:
                job.close()
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise ProcessStartError(cleanup_confirmed=cleaned) from None


class _GuardedProcess:
    """Parent-side transport to the POSIX supervisor-loss guardian."""

    def __init__(self, process, signal_fd, result_fd):
        self.process = process
        self._signal_fd = signal_fd
        self._result_fd = result_fd
        self._finished = None
        self._lock = Lock()
        self.returncode = None

    def finish(self):
        with self._lock:
            if self._finished is not None:
                return self._finished
            os.close(self._signal_fd)
            try:
                self._finished = self.process.wait(timeout=7) == 0
                if self._finished:
                    self.returncode = int(os.read(self._result_fd, 32))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self._finished = False
            finally:
                os.close(self._result_fd)
            return self._finished


def _start_guarded(command, owner_fd, **kwargs):
    guard_read, guard_write = os.pipe()
    try:
        result_read, result_write = os.pipe()
    except OSError:
        os.close(guard_read)
        os.close(guard_write)
        raise
    try:
        guardian = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                os.path.abspath(__file__),
                str(guard_read),
                str(result_write),
                *command,
            ],
            pass_fds=(guard_read, result_write, owner_fd),
            start_new_session=True,
            **kwargs,
        )
    except BaseException as error:
        os.close(guard_write)
        os.close(result_read)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise ProcessStartError(cleanup_confirmed=True) from None
    finally:
        os.close(guard_read)
        os.close(result_write)
    managed = _GuardedProcess(guardian, guard_write, result_read)
    try:
        ready = select.select([result_read], [], [], 5)[0] and os.read(result_read, 1)
        if ready != b"1":
            raise ProcessStartError(cleanup_confirmed=managed.finish())
    except BaseException:
        managed.finish()
        for stream in (guardian.stdin, guardian.stdout, guardian.stderr):
            if stream is not None:
                stream.close()
        raise
    return managed


def _guardian(liveness_fd, result_fd, command):
    """Keep the project lock inherited by this process until its native group is gone."""
    # Linux adopts its provider's orphaned grandchildren here so they can be reaped.
    reap = sys.platform == "linux" and ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    if select.select([liveness_fd], [], [], 0)[0]:
        os.write(result_fd, b"00")
        return
    try:
        managed = start_process(command)
    except ProcessStartError as error:
        if not error.cleanup_confirmed:
            while True:
                sleep(1)
        os.write(result_fd, b"01")
        return
    managed._reap_descendants = reap
    try:
        os.write(result_fd, b"1")
        # Only the native process keeps the protocol streams after launch.
        for descriptor in (0, 1, 2):
            try:
                os.close(descriptor)
            except OSError:
                pass
        while managed.process.poll() is None:
            if select.select([liveness_fd], [], [], 0.05)[0]:
                break
    finally:
        if not managed.finish():
            while True:
                try:
                    managed.process.poll()
                    if managed.group_gone():
                        break
                except OSError:
                    pass
                sleep(0.1)
        try:
            os.write(result_fd, str(managed.returncode).encode("ascii"))
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    _guardian(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:])

# SPDX-License-Identifier: MPL-2.0
"""Supervise one provider run per project without blocking the application peer."""

import re
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from corinthos.adapters import AdapterManager, SessionPlanError
from corinthos.bootstrap import _root, adapter_manager
from corinthos.contracts import ConflictError, ContractError
from corinthos.contracts.model import canonical_json
from corinthos.project_status import _absolute_root
from corinthos.providers import SUPPORTED_PROVIDERS
from corinthos.work import WorkService

from .model import TERMINAL_STATES, SessionError
from .ownership import ProjectOwnerBusy, project_owner
from .repository import SessionRepository

_DEFAULT_TIMEOUT = 120
_LEASE_SECONDS = 30
_HEARTBEAT = 5
_POLL = 0.25
_MAX_INSTRUCTION = 4000
_ACTIVE = {"STARTING", "RUNNING", "CANCELLING"}
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SECRET = re.compile(
    r"(?i)(?:\b(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]{16,}|github_pat_[a-z0-9_]{16,})\b"
    r"|(?:bearer|api[_ -]?key|password|secret|token)\s*[:=]?\s+[^\s,;]+)"
)


def _instruction(value):
    if (
        not isinstance(value, str)
        or len(value) > _MAX_INSTRUCTION
        or any(not (c.isprintable() or c in "\n\t") for c in value)
    ):
        raise SessionError("Invalid session instruction.")
    return value


def _prompt(work, instruction, read_only):
    request = {
        "work_item_id": work.work_item_id,
        "project_id": work.project_id,
        "scope": work.scope_level.value,
        "objective": work.objective,
        "acceptance_criteria": list(work.acceptance_criteria),
        "affected_modules": [row.module_id for row in work.affected_modules],
        "operator_instruction": instruction,
    }
    mode = (
        "Inspect and respond without modifying files."
        if read_only
        else ("Work within the selected project and the operator's authorized scope.")
    )
    return (
        mode + "\nThis is an operational Work Item. Report evidence and limitations. "
        "Do not claim human approval, contract publication, or independent Validation.\n"
        + canonical_json(request)
    )


def _envelope(session, native_binding, projection):
    data = {
        "native_plan": native_binding,
        "work_item_id": session.work_item_id,
        "project_id": session.project_id,
    }
    if projection is not None:
        data["projection_id"] = projection.projection_id
        data["projection_digest"] = projection.projection_digest
    return sha256(canonical_json(data).encode()).hexdigest()


def _observed_text(value, limit):
    from corinthos.context.redaction import sanitize_text

    if not isinstance(value, str):
        return None
    value = "".join(c if c.isprintable() or c in "\n\t" else " " for c in value)
    return sanitize_text(_SECRET.sub("[redacted]", value))[:limit] or None


class _Run:
    def __init__(self, repository, session, stack):
        self.repository = repository
        self.session = session
        self.stack = stack
        self.cancel = Event()
        self.shutdown = Event()
        self.done = Event()
        self.finished = Event()
        self.started_at = monotonic()
        self.error = None

    @property
    def fence(self):
        return (self.session.session_id, self.session.owner_id, self.session.run_generation)

    def remaining(self):
        return max(0.0, self.session.timeout_seconds - (monotonic() - self.started_at))


class SessionSupervisor:
    """Own worker lifetimes and use the same fence for events, renewal and outcomes."""

    def __init__(self, *, manager_factory=adapter_manager):
        self._manager_factory = manager_factory
        self._lock = Lock()
        self._runs = {}
        self._closing = False

    def start(
        self,
        root,
        work_item_id,
        provider,
        *,
        instruction="",
        timeout_seconds=_DEFAULT_TIMEOUT,
        read_only=False,
        executable=None,
        _check_work=None,
    ):
        if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
            raise SessionError("Select a supported provider.")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
            raise SessionError("Session timeout must be between 1 and 3600 seconds.")
        if type(read_only) is not bool:
            raise SessionError("Invalid session access mode.")
        return self._launch(
            root,
            work_item_id=work_item_id,
            provider=provider,
            instruction=_instruction(instruction),
            timeout_seconds=timeout_seconds,
            read_only=read_only,
            executable=self._override(executable),
            check_work=_check_work,
        )

    @staticmethod
    def _override(executable):
        if executable is None:
            return None
        if (
            not isinstance(executable, str)
            or not 0 < len(executable) <= 4096
            or not Path(executable).is_absolute()
            or any(not c.isprintable() for c in executable)
        ):
            raise SessionError("Use an absolute native provider executable path.")
        return executable

    def recover(self, root, session_id, *, instruction="", executable=None):
        root = _root(_absolute_root(root))
        instruction = _instruction(instruction)
        executable = self._override(executable)
        return self._launch(
            root, session_id=session_id, instruction=instruction, executable=executable
        )

    def cancel(self, root, session_id, *, generation=None):
        root = _root(_absolute_root(root))
        session = SessionRepository(root).request_cancel(session_id, generation=generation)
        with self._lock:
            run = self._runs.get((root, session_id))
            if run is not None:
                run.cancel.set()
        return session

    def preflight(self, root, provider, *, read_only):
        root = _root(_absolute_root(root))
        manager = self._manager_factory(root, operation_commands=None)
        manager.session_plan(
            provider,
            root,
            prompt="Verify native session capabilities.",
            read_only=read_only,
            prepare=False,
            timeout=8,
        )

    def reconcile(self, root, session):
        """Reconcile an expired fence only after acquiring native project ownership."""
        root = _root(_absolute_root(root))
        repository = SessionRepository(root)
        try:
            with project_owner(root):
                return repository.reconcile_expired(
                    session.session_id, owner_id=session.owner_id, generation=session.run_generation
                )
        except ProjectOwnerBusy:
            return None

    def start_switch(self, root, source, projection, handoff):
        return self._launch(
            root,
            work_item_id=source.work_item_id,
            provider=projection.target_provider,
            instruction="",
            read_only=source.read_only,
            timeout_seconds=source.timeout_seconds,
            continuation=(source, projection.projection_id, handoff),
        )

    def _launch(
        self,
        root,
        *,
        session_id=None,
        work_item_id=None,
        provider=None,
        instruction,
        timeout_seconds=None,
        read_only=False,
        executable=None,
        continuation=None,
        check_work=None,
    ):
        root = _root(_absolute_root(root))
        repository = SessionRepository(root)
        with self._lock, ExitStack() as guard:
            if self._closing:
                raise ConflictError("The session supervisor is closing.")
            owner_fd = guard.enter_context(project_owner(root))
            for expired in repository.expired():
                repository.reconcile_expired(
                    expired.session_id,
                    owner_id=expired.owner_id,
                    generation=expired.run_generation,
                )
            original = repository.get(session_id) if session_id else None
            if original is not None:
                if not original.recoverable:
                    raise SessionError("This session is unavailable for native recovery.")
                work_item_id, read_only = original.work_item_id, original.read_only
            work = WorkService(root).get(work_item_id)
            if work.status in {"COMPLETED", "CANCELLED"}:
                raise SessionError("The Work Item is closed.")
            from corinthos.context import ContextService, HandoffService
            from corinthos.context.redaction import sanitize_text

            context_service = ContextService(root)
            projection = None
            if continuation is not None:
                source, projection_id, handoff = continuation
                current = repository.get(source.session_id)
                if (
                    current.run_generation != source.run_generation
                    or current.state not in TERMINAL_STATES
                    or current.cleanup_unconfirmed
                    or current.work_item_id != work_item_id
                    or current.provider == provider
                ):
                    raise ConflictError("The source Agent Session changed before continuation.")
                projection = context_service.verify(projection_id)
                if (
                    projection.project_id != current.project_id
                    or projection.work_item_id != work_item_id
                    or projection.target_provider != provider
                    or projection.canonical_payload.get("required_handoff_id") != handoff.handoff_id
                ):
                    raise SessionError("The switch projection has inconsistent ownership.")
                if current.native_session_id and (
                    current.native_session_id.lower()
                    in canonical_json(projection.canonical_payload).lower()
                ):
                    raise SessionError("The switch projection contains a source native identity.")
                with repository._read() as connection:
                    if HandoffService(root).for_source(connection, current) != handoff:
                        raise ConflictError("The source Handoff changed before continuation.")
                    target = repository.switch_target(connection, projection)
                if target is not None:
                    return target
            elif original is None:
                projection = context_service.create(work_item_id, provider)
            elif original.context_projection_id is not None:
                projection = context_service.verify(original.context_projection_id)
                if (
                    projection.project_id != original.project_id
                    or projection.work_item_id != original.work_item_id
                    or projection.target_provider != original.provider
                ):
                    raise SessionError("The stored session projection has inconsistent ownership.")
            if projection is None:
                # v8 sessions retain their historical envelope and binding.
                prompt = _prompt(work, instruction, read_only)
            else:
                prompt = AdapterManager.render_session_projection(
                    projection, sanitize_text(instruction), read_only=read_only
                )
            owner_id = "supervisor-" + uuid4().hex
            if original is None:
                session = repository.create(
                    work_item_id,
                    provider=provider,
                    owner_id=owner_id,
                    timeout_seconds=timeout_seconds,
                    read_only=read_only,
                    context_projection_id=projection.projection_id,
                    lease_seconds=_LEASE_SECONDS,
                    _check_work=check_work,
                )
            else:
                session = repository.recover(
                    session_id,
                    owner_id=owner_id,
                    envelope_sha256=original.envelope_sha256,
                    lease_seconds=_LEASE_SECONDS,
                )
            run = _Run(repository, session, guard.pop_all())
            key = (root, session.session_id)
            self._runs[key] = run
            worker = Thread(
                target=self._execute,
                args=(key, run, prompt, executable, owner_fd, original, projection),
                name="corinthos-agent-session",
            )
            try:
                worker.start()
            except RuntimeError:
                self._runs.pop(key)
                try:
                    repository.finish(*run.fence, "INTERRUPTED", reason="WORKER_START_FAILED")
                finally:
                    run.stack.close()
                raise ConflictError("The session worker could not start.") from None
            return session

    def _heartbeat(self, run):
        next_renewal = monotonic() + _HEARTBEAT
        lease_expires_at = run.session.lease_expires_at
        while not run.done.wait(_POLL):
            try:
                if monotonic() >= next_renewal:
                    session = run.repository.renew(*run.fence, lease_seconds=_LEASE_SECONDS)
                    lease_expires_at = session.lease_expires_at
                    next_renewal = monotonic() + _HEARTBEAT
                else:
                    session = run.repository.owned_state(*run.fence)
                if (
                    session.owner_id != run.session.owner_id
                    or session.run_generation != run.session.run_generation
                ):
                    run.cancel.set()
                    return
                if session.state == "CANCELLING":
                    run.cancel.set()
            except ConflictError:
                if run.repository.now() >= lease_expires_at:
                    run.cancel.set()
                    return
            except (ContractError, OSError):
                run.cancel.set()
                return

    def _observe(self, run, event):
        kind = event["kind"]
        payload = event.get("payload") or {}
        safe = {
            key: value
            for key, value in payload.items()
            if key in {"text", "code", "tool", "message"} and isinstance(value, str)
        }
        safe = {
            key: _observed_text(value, 240 if key == "text" else 48) for key, value in safe.items()
        }
        usage = payload if kind == "USAGE" else None
        if usage is not None:
            safe = usage
        run.repository.observe(
            *run.fence,
            kind,
            safe,
            native_kind=_observed_text(event.get("provider_type"), 96),
            native_session_id=event.get("native_session_id"),
            usage=usage,
            model=_observed_text(event.get("model"), 128),
        )

    def _execute(self, key, run, prompt, executable, owner_fd, original, projection):

        root, _ = key
        session = run.session
        repository = run.repository
        heartbeat = Thread(target=self._heartbeat, args=(run,), name="corinthos-session-lease")
        result = None
        reason = None
        state = "FAILED"
        cleanup_confirmed = True
        try:
            heartbeat.start()
            overrides = {session.provider: [executable]} if executable else None
            manager = self._manager_factory(root, operation_commands=overrides)
            expected = None
            if original is not None:
                expected = manager.session_binding(
                    session.provider, root, prompt=prompt, read_only=session.read_only
                )
                if _envelope(session, expected, projection) != original.envelope_sha256:
                    raise SessionPlanError("RECOVERY_ENVELOPE_CHANGED")
            plan = manager.session_plan(
                session.provider,
                root,
                prompt=prompt,
                read_only=session.read_only,
                timeout=max(0.001, run.remaining()),
                cancel=run.cancel,
                owner_fd=owner_fd,
                prepare=original is None,
                expected_binding_sha256=expected,
            )
            binding = _envelope(session, plan.binding_sha256, projection)
            if original is not None and (
                binding != original.envelope_sha256 or not plan.capabilities.native_resume
            ):
                state, reason = "INTERRUPTED", "RECOVERY_ENVELOPE_CHANGED"
            elif run.remaining() <= 0:
                state, reason = "TIMED_OUT", "TIME_LIMIT"
            elif not run.cancel.is_set():
                repository.bind(
                    *run.fence,
                    envelope_sha256=binding,
                    native_resume=plan.capabilities.native_resume,
                )
                result = manager.run_session(
                    plan,
                    task_id=session.work_item_id,
                    attempt_id=f"{session.session_id}-{session.run_generation}",
                    timeout=max(0.001, run.remaining()),
                    cancel=run.cancel,
                    owner_fd=owner_fd,
                    on_event=lambda event: self._observe(run, event),
                    on_start=lambda: repository.running(
                        *run.fence,
                        provider_version=plan.capabilities.version,
                    ),
                    resume_session_id=original.native_session_id if original else None,
                )
                state = {
                    "COMPLETED": "SUCCEEDED",
                    "CANCELLED": "INTERRUPTED",
                    "TIMEOUT": "TIMED_OUT",
                }.get(result.status, "FAILED")
                reason = result.error_kind
                cleanup_confirmed = result.cleanup_confirmed
        except SessionPlanError as error:
            reason = error.error_kind
            cleanup_confirmed = error.cleanup_confirmed
            if original is not None:
                state = "INTERRUPTED"
        except (ContractError, OSError, ValueError):
            reason = "SUPERVISOR_OPERATION_FAILED"
            if original is not None or run.cancel.is_set():
                state = "INTERRUPTED"
        except Exception:
            # An unknown adapter exception is never evidence of process cleanup.
            reason, cleanup_confirmed = "SUPERVISOR_OPERATION_FAILED", False
        try:
            if not cleanup_confirmed:
                repository.block_reconciliation(*run.fence)
                return
            owned = repository.owned_state(*run.fence)
            if owned.state == "CANCELLING":
                state, reason = "CANCELLED", "CANCEL_REQUESTED"
            elif run.shutdown.is_set() or run.cancel.is_set():
                state, reason = "INTERRUPTED", "SUPERVISOR_STOPPED"
            elif result is None and (reason == "TIME_LIMIT" or run.remaining() <= 0):
                state, reason = "TIMED_OUT", "TIME_LIMIT"
            repository.finish(
                *run.fence,
                state,
                duration_ms=int((monotonic() - run.started_at) * 1000),
                reason=reason if isinstance(reason, str) and _REASON.fullmatch(reason) else None,
            )
        except (ContractError, OSError) as error:
            run.error = error
        finally:
            run.done.set()
            if heartbeat.ident is not None:
                heartbeat.join()
            run.stack.close()
            with self._lock:
                self._runs.pop(key, None)
            run.finished.set()

    def wait(self, root, session_id):
        root = _root(_absolute_root(root))
        with self._lock:
            run = self._runs.get((root, session_id))
        if run is not None:
            run.finished.wait()
            if run.error is not None:
                raise run.error
        return SessionRepository(root).get(session_id)

    def close(self):
        with self._lock:
            self._closing = True
            runs = list(self._runs.values())
            for run in runs:
                run.shutdown.set()
                run.cancel.set()
        deadline = monotonic() + 10
        return all(run.finished.wait(max(0.0, deadline - monotonic())) for run in runs)

    def has_active(self):
        """Refuse an application restart while this supervisor owns an active run."""
        with self._lock:
            return bool(self._runs)

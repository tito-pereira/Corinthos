# SPDX-License-Identifier: MPL-2.0
"""Project-local SQLite storage for immutable contract revisions."""

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .artifacts import is_link, private_directory, verify_artifact
from .authority import PublishContext, require_authority
from .model import (
    ConflictError,
    ContractRef,
    IntegrityError,
    StoreError,
    canonical_json,
    iter_references,
)
from .schema import validate_schema
from .semantics import validate_semantics

_APPLICATION_ID = 0x4E554B59
_SCHEMA_VERSION = 9
_LOCK_TIMEOUT = 5.0
_BUSY_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
_BUSY_MESSAGE = "The state database is busy. Retry with the current revision."
_ACTIVE_TYPES = frozenset({"PROJECT", "WORKFLOW", "MODULE", "TECHNOLOGY_DECISION", "VALIDATION"})
_MIGRATION_V1 = (
    """CREATE TABLE contracts (
        contract_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        contract_type TEXT NOT NULL,
        active_revision_id TEXT,
        FOREIGN KEY (contract_id, active_revision_id)
            REFERENCES revisions (contract_id, revision_id)
    )""",
    """CREATE UNIQUE INDEX single_project_and_workflow
       ON contracts (project_id, contract_type)
       WHERE contract_type IN ('PROJECT', 'WORKFLOW')""",
    """CREATE TABLE revisions (
        revision_id TEXT PRIMARY KEY NOT NULL,
        contract_id TEXT NOT NULL REFERENCES contracts (contract_id),
        version INTEGER NOT NULL CHECK (version >= 1),
        supersedes_revision_id TEXT,
        document TEXT NOT NULL,
        document_sha256 TEXT NOT NULL,
        UNIQUE (contract_id, version),
        UNIQUE (contract_id, revision_id),
        FOREIGN KEY (contract_id, supersedes_revision_id)
            REFERENCES revisions (contract_id, revision_id)
    )""",
    """CREATE TABLE contract_references (
        source_revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
        target_revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
        PRIMARY KEY (source_revision_id, target_revision_id)
    )""",
    """CREATE TABLE audit_events (
        sequence INTEGER PRIMARY KEY,
        revision_id TEXT NOT NULL UNIQUE REFERENCES revisions (revision_id),
        activated INTEGER NOT NULL CHECK (activated IN (0, 1)),
        occurred_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER revisions_no_update BEFORE UPDATE ON revisions
       BEGIN SELECT RAISE(ABORT, 'Published revisions are immutable'); END""",
    """CREATE TRIGGER revisions_no_delete BEFORE DELETE ON revisions
       BEGIN SELECT RAISE(ABORT, 'Published revisions are immutable'); END""",
    """CREATE TRIGGER revisions_no_replace BEFORE INSERT ON revisions
       WHEN EXISTS (SELECT 1 FROM revisions WHERE revision_id = NEW.revision_id
           OR (contract_id = NEW.contract_id AND version = NEW.version))
       BEGIN SELECT RAISE(ABORT, 'Published revisions are immutable'); END""",
    """CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END""",
    """CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END""",
    """CREATE TRIGGER audit_events_no_replace BEFORE INSERT ON audit_events
       WHEN EXISTS (SELECT 1 FROM audit_events WHERE sequence = NEW.sequence
           OR revision_id = NEW.revision_id)
       BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END""",
)
_MIGRATION_V2 = (
    """CREATE TABLE pm_sessions (
        session_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        snapshot TEXT NOT NULL
    )""",
    """CREATE TABLE pm_turns (
        turn_id TEXT PRIMARY KEY NOT NULL,
        session_id TEXT NOT NULL REFERENCES pm_sessions (session_id),
        version INTEGER NOT NULL CHECK (version >= 1),
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE (session_id, version)
    )""",
    """CREATE TABLE pm_approvals (
        approval_id TEXT PRIMARY KEY NOT NULL,
        session_id TEXT NOT NULL REFERENCES pm_sessions (session_id),
        session_version INTEGER NOT NULL CHECK (session_version >= 1),
        project_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        expires_at REAL NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1))
    )""",
    """CREATE TRIGGER pm_turns_no_update BEFORE UPDATE ON pm_turns
       BEGIN SELECT RAISE(ABORT, 'Negotiation turns are append-only'); END""",
    """CREATE TRIGGER pm_turns_no_delete BEFORE DELETE ON pm_turns
       BEGIN SELECT RAISE(ABORT, 'Negotiation turns are append-only'); END""",
    """CREATE TRIGGER pm_turns_no_replace BEFORE INSERT ON pm_turns
       WHEN EXISTS (SELECT 1 FROM pm_turns WHERE turn_id = NEW.turn_id
           OR (session_id = NEW.session_id AND version = NEW.version))
       BEGIN SELECT RAISE(ABORT, 'Negotiation turns are append-only'); END""",
    """CREATE TRIGGER pm_approvals_binding BEFORE UPDATE ON pm_approvals
       WHEN OLD.approval_id IS NOT NEW.approval_id
         OR OLD.session_id IS NOT NEW.session_id
         OR OLD.session_version IS NOT NEW.session_version
         OR OLD.project_id IS NOT NEW.project_id
         OR OLD.revision_id IS NOT NEW.revision_id
         OR OLD.content_sha256 IS NOT NEW.content_sha256
         OR OLD.actor_id IS NOT NEW.actor_id
         OR OLD.expires_at IS NOT NEW.expires_at
         OR NOT (OLD.consumed = 0 AND NEW.consumed = 1)
       BEGIN SELECT RAISE(ABORT, 'Approval bindings are immutable and single-use'); END""",
    """CREATE TRIGGER pm_approvals_no_delete BEFORE DELETE ON pm_approvals
       BEGIN SELECT RAISE(ABORT, 'Approval receipts are retained'); END""",
    """CREATE TRIGGER pm_approvals_no_replace BEFORE INSERT ON pm_approvals
       WHEN EXISTS (SELECT 1 FROM pm_approvals WHERE approval_id = NEW.approval_id)
       BEGIN SELECT RAISE(ABORT, 'Approval bindings are immutable and single-use'); END""",
)
_MIGRATION_V3 = (
    """CREATE TABLE wf_sessions (
        session_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        snapshot TEXT NOT NULL
    )""",
    """CREATE TABLE wf_turns (
        turn_id TEXT PRIMARY KEY NOT NULL,
        session_id TEXT NOT NULL REFERENCES wf_sessions (session_id),
        version INTEGER NOT NULL CHECK (version >= 1),
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE (session_id, version)
    )""",
    """CREATE TABLE wf_mandates (
        mandate_id TEXT PRIMARY KEY NOT NULL,
        session_id TEXT NOT NULL REFERENCES wf_sessions (session_id),
        project_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        invariant_sha256 TEXT NOT NULL,
        workflow_ref TEXT NOT NULL,
        expires_at REAL NOT NULL
    )""",
    """CREATE TABLE wf_approvals (
        approval_id TEXT PRIMARY KEY NOT NULL,
        session_id TEXT NOT NULL REFERENCES wf_sessions (session_id),
        session_version INTEGER NOT NULL CHECK (session_version >= 1),
        project_id TEXT NOT NULL,
        contract_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('HUMAN', 'DELEGATED')),
        content_sha256 TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        expires_at REAL NOT NULL,
        mandate_id TEXT REFERENCES wf_mandates (mandate_id),
        consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1))
    )""",
    """CREATE TRIGGER wf_turns_no_update BEFORE UPDATE ON wf_turns
       BEGIN SELECT RAISE(ABORT, 'Planning turns are append-only'); END""",
    """CREATE TRIGGER wf_turns_no_delete BEFORE DELETE ON wf_turns
       BEGIN SELECT RAISE(ABORT, 'Planning turns are append-only'); END""",
    """CREATE TRIGGER wf_turns_no_replace BEFORE INSERT ON wf_turns
       WHEN EXISTS (SELECT 1 FROM wf_turns WHERE turn_id = NEW.turn_id
           OR (session_id = NEW.session_id AND version = NEW.version))
       BEGIN SELECT RAISE(ABORT, 'Planning turns are append-only'); END""",
    """CREATE TRIGGER wf_approvals_binding BEFORE UPDATE ON wf_approvals
       WHEN OLD.approval_id IS NOT NEW.approval_id
         OR OLD.session_id IS NOT NEW.session_id
         OR OLD.session_version IS NOT NEW.session_version
         OR OLD.project_id IS NOT NEW.project_id
         OR OLD.contract_id IS NOT NEW.contract_id
         OR OLD.revision_id IS NOT NEW.revision_id
         OR OLD.kind IS NOT NEW.kind
         OR OLD.content_sha256 IS NOT NEW.content_sha256
         OR OLD.actor_id IS NOT NEW.actor_id
         OR OLD.expires_at IS NOT NEW.expires_at
         OR OLD.mandate_id IS NOT NEW.mandate_id
         OR NOT (OLD.consumed = 0 AND NEW.consumed = 1)
       BEGIN SELECT RAISE(ABORT, 'Planning approval bindings are immutable'); END""",
    """CREATE TRIGGER wf_approvals_no_delete BEFORE DELETE ON wf_approvals
       BEGIN SELECT RAISE(ABORT, 'Planning approvals are retained'); END""",
    """CREATE TRIGGER wf_approvals_no_replace BEFORE INSERT ON wf_approvals
       WHEN EXISTS (SELECT 1 FROM wf_approvals WHERE approval_id = NEW.approval_id)
       BEGIN SELECT RAISE(ABORT, 'Planning approval bindings are immutable'); END""",
    """CREATE TRIGGER wf_mandates_no_update BEFORE UPDATE ON wf_mandates
       BEGIN SELECT RAISE(ABORT, 'Planning mandates are immutable'); END""",
    """CREATE TRIGGER wf_mandates_no_delete BEFORE DELETE ON wf_mandates
       BEGIN SELECT RAISE(ABORT, 'Planning mandates are retained'); END""",
    """CREATE TRIGGER wf_mandates_no_replace BEFORE INSERT ON wf_mandates
       WHEN EXISTS (SELECT 1 FROM wf_mandates WHERE mandate_id = NEW.mandate_id)
       BEGIN SELECT RAISE(ABORT, 'Planning mandates are immutable'); END""",
)
_MIGRATION_V4 = (
    """CREATE TABLE wf_executions (
        execution_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        workflow_ref TEXT NOT NULL,
        workspace TEXT NOT NULL,
        bindings TEXT NOT NULL,
        bindings_sha256 TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        state TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE wf_checkpoints (
        execution_id TEXT NOT NULL REFERENCES wf_executions (execution_id),
        checkpoint_id TEXT NOT NULL,
        state TEXT NOT NULL,
        attempt_id TEXT REFERENCES wf_attempts (attempt_id),
        inherited_from TEXT,
        PRIMARY KEY (execution_id, checkpoint_id)
    )""",
    """CREATE TABLE wf_attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        fence INTEGER NOT NULL CHECK (fence >= 1),
        provider TEXT NOT NULL,
        state TEXT NOT NULL,
        context TEXT NOT NULL,
        details TEXT NOT NULL,
        previous_attempt_id TEXT REFERENCES wf_attempts (attempt_id),
        execution_version INTEGER NOT NULL CHECK (execution_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (execution_id, checkpoint_id)
            REFERENCES wf_checkpoints (execution_id, checkpoint_id)
    )""",
    """CREATE TABLE wf_workspace_leases (
        workspace TEXT PRIMARY KEY NOT NULL,
        execution_id TEXT NOT NULL REFERENCES wf_executions (execution_id),
        attempt_id TEXT UNIQUE REFERENCES wf_attempts (attempt_id),
        owner_id TEXT,
        fence INTEGER NOT NULL DEFAULT 0 CHECK (fence >= 0),
        expires_at REAL NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE wf_execution_events (
        sequence INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        execution_id TEXT NOT NULL REFERENCES wf_executions (execution_id),
        checkpoint_id TEXT,
        attempt_id TEXT REFERENCES wf_attempts (attempt_id),
        actor_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        data TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER wf_execution_events_no_update BEFORE UPDATE ON wf_execution_events
       BEGIN SELECT RAISE(ABORT, 'Execution observations are append-only'); END""",
    """CREATE TRIGGER wf_execution_events_no_delete BEFORE DELETE ON wf_execution_events
       BEGIN SELECT RAISE(ABORT, 'Execution observations are append-only'); END""",
    """CREATE TRIGGER wf_execution_events_no_replace BEFORE INSERT ON wf_execution_events
       WHEN EXISTS (SELECT 1 FROM wf_execution_events
                    WHERE sequence = NEW.sequence OR event_id = NEW.event_id)
       BEGIN SELECT RAISE(ABORT, 'Execution observations are append-only'); END""",
)
_MIGRATION_V5 = (
    """CREATE TABLE val_drafts (
        draft_id TEXT PRIMARY KEY NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        snapshot TEXT NOT NULL
    )""",
    """CREATE TABLE val_plan_bindings (
        revision_id TEXT PRIMARY KEY NOT NULL REFERENCES revisions (revision_id),
        bindings TEXT NOT NULL,
        reviews TEXT NOT NULL
    )""",
    """CREATE TRIGGER val_bindings_no_update BEFORE UPDATE ON val_plan_bindings
       BEGIN SELECT RAISE(ABORT, 'Validation plan bindings are immutable'); END""",
    """CREATE TRIGGER val_bindings_no_delete BEFORE DELETE ON val_plan_bindings
       BEGIN SELECT RAISE(ABORT, 'Validation plan bindings are retained'); END""",
    """CREATE TRIGGER val_bindings_no_replace BEFORE INSERT ON val_plan_bindings
       WHEN EXISTS (SELECT 1 FROM val_plan_bindings WHERE revision_id = NEW.revision_id)
       BEGIN SELECT RAISE(ABORT, 'Validation plan bindings are immutable'); END""",
    """CREATE TABLE val_runs (
        run_id TEXT PRIMARY KEY NOT NULL,
        plan_revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
        plan_ref TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        evaluator TEXT NOT NULL,
        owner TEXT NOT NULL,
        expires_at REAL NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('RUNNING', 'FROZEN', 'PUBLISHED')),
        context TEXT NOT NULL,
        previous_run_id TEXT REFERENCES val_runs (run_id),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        frozen_result TEXT,
        frozen_sha256 TEXT,
        result_revision_id TEXT UNIQUE REFERENCES revisions (revision_id),
        CHECK ((state = 'RUNNING') = (finished_at IS NULL)),
        CHECK ((state = 'RUNNING') = (frozen_result IS NULL)),
        CHECK ((state = 'RUNNING') = (frozen_sha256 IS NULL)),
        CHECK ((state = 'PUBLISHED') = (result_revision_id IS NOT NULL))
    )""",
    """CREATE UNIQUE INDEX val_one_unfinished_run_per_target
       ON val_runs (plan_revision_id, artifact_sha256)
       WHERE state IN ('RUNNING', 'FROZEN')""",
    """CREATE TRIGGER val_runs_binding BEFORE UPDATE ON val_runs
       WHEN OLD.run_id IS NOT NEW.run_id
         OR OLD.plan_revision_id IS NOT NEW.plan_revision_id
         OR OLD.plan_ref IS NOT NEW.plan_ref
         OR OLD.artifact_sha256 IS NOT NEW.artifact_sha256
         OR OLD.evaluator IS NOT NEW.evaluator
         OR OLD.owner IS NOT NEW.owner
         OR OLD.context IS NOT NEW.context
         OR OLD.previous_run_id IS NOT NEW.previous_run_id
         OR OLD.started_at IS NOT NEW.started_at
         OR OLD.state = 'PUBLISHED'
         OR (OLD.state = 'FROZEN' AND (
             NEW.state != 'PUBLISHED'
             OR OLD.finished_at IS NOT NEW.finished_at
             OR OLD.frozen_result IS NOT NEW.frozen_result
             OR OLD.frozen_sha256 IS NOT NEW.frozen_sha256
             OR OLD.expires_at IS NOT NEW.expires_at
         ))
         OR (OLD.state = 'RUNNING' AND NEW.state = 'PUBLISHED')
       BEGIN SELECT RAISE(ABORT, 'Validation runs retain exact bindings and frozen results'); END""",
    """CREATE TRIGGER val_runs_no_delete BEFORE DELETE ON val_runs
       BEGIN SELECT RAISE(ABORT, 'Validation runs are retained'); END""",
    """CREATE TRIGGER val_runs_no_replace BEFORE INSERT ON val_runs
       WHEN EXISTS (SELECT 1 FROM val_runs WHERE run_id = NEW.run_id
                    OR (plan_revision_id = NEW.plan_revision_id
                        AND artifact_sha256 = NEW.artifact_sha256
                        AND state IN ('RUNNING', 'FROZEN')
                        AND NEW.state IN ('RUNNING', 'FROZEN'))
                    OR (result_revision_id IS NOT NULL
                        AND result_revision_id = NEW.result_revision_id))
       BEGIN SELECT RAISE(ABORT, 'Validation runs are retained'); END""",
    """CREATE TABLE val_events (
        sequence INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES val_runs (run_id),
        kind TEXT NOT NULL,
        data TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    )""",
    """CREATE INDEX val_events_run_sequence ON val_events (run_id, sequence)""",
    """CREATE TRIGGER val_events_no_update BEFORE UPDATE ON val_events
       BEGIN SELECT RAISE(ABORT, 'Validation observations are append-only'); END""",
    """CREATE TRIGGER val_events_no_delete BEFORE DELETE ON val_events
       BEGIN SELECT RAISE(ABORT, 'Validation observations are append-only'); END""",
    """CREATE TRIGGER val_events_no_replace BEFORE INSERT ON val_events
       WHEN EXISTS (SELECT 1 FROM val_events WHERE sequence = NEW.sequence)
       BEGIN SELECT RAISE(ABORT, 'Validation observations are append-only'); END""",
)
_REQUIRED_TABLES_V1 = frozenset({"contracts", "revisions", "contract_references", "audit_events"})
_REQUIRED_TABLES_V2 = _REQUIRED_TABLES_V1 | {"pm_sessions", "pm_turns", "pm_approvals"}
_REQUIRED_TABLES_V3 = _REQUIRED_TABLES_V2 | {
    "wf_sessions",
    "wf_turns",
    "wf_approvals",
    "wf_mandates",
}
_REQUIRED_TABLES_V4 = _REQUIRED_TABLES_V3 | {
    "wf_executions",
    "wf_checkpoints",
    "wf_attempts",
    "wf_workspace_leases",
    "wf_execution_events",
}
_MIGRATION_V6 = (
    """CREATE TABLE lc_records (
        record_id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN
            ('VERSION','EVOLUTION','BUILD','SESSION','FEEDBACK','REVIEW','OPERATION')),
        project_id TEXT NOT NULL,
        logical_key TEXT,
        state TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        origin TEXT NOT NULL,
        origin_sha256 TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (record_id, project_id),
        UNIQUE (kind, project_id, logical_key)
    )""",
    """CREATE INDEX lc_records_project_kind ON lc_records(project_id, kind)""",
    """CREATE TRIGGER lc_records_binding BEFORE UPDATE ON lc_records
       WHEN OLD.record_id IS NOT NEW.record_id OR OLD.kind IS NOT NEW.kind
         OR OLD.project_id IS NOT NEW.project_id OR OLD.logical_key IS NOT NEW.logical_key
         OR OLD.origin IS NOT NEW.origin OR OLD.origin_sha256 IS NOT NEW.origin_sha256
         OR OLD.created_at IS NOT NEW.created_at OR NEW.version != OLD.version + 1
       BEGIN SELECT RAISE(ABORT, 'Lifecycle origins and identity bindings are immutable'); END""",
    """CREATE TRIGGER lc_records_no_delete BEFORE DELETE ON lc_records
       BEGIN SELECT RAISE(ABORT, 'Lifecycle metadata is retained'); END""",
    """CREATE TRIGGER lc_records_no_replace BEFORE INSERT ON lc_records
       WHEN EXISTS (SELECT 1 FROM lc_records WHERE record_id = NEW.record_id
          OR (kind = NEW.kind AND project_id = NEW.project_id
              AND logical_key = NEW.logical_key))
       BEGIN SELECT RAISE(ABORT, 'Lifecycle records cannot replace history'); END""",
    """CREATE TABLE lc_projects (
        project_id TEXT PRIMARY KEY NOT NULL,
        enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
        tracker_id TEXT NOT NULL UNIQUE,
        active_build_id TEXT,
        pending_build_id TEXT,
        version INTEGER NOT NULL CHECK (version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(active_build_id, project_id) REFERENCES lc_records(record_id, project_id),
        FOREIGN KEY(pending_build_id, project_id) REFERENCES lc_records(record_id, project_id)
    )""",
    """CREATE TRIGGER lc_projects_binding BEFORE UPDATE ON lc_projects
       WHEN OLD.project_id IS NOT NEW.project_id OR OLD.tracker_id IS NOT NEW.tracker_id
         OR OLD.created_at IS NOT NEW.created_at OR NEW.version != OLD.version + 1
       BEGIN SELECT RAISE(ABORT, 'Lifecycle tracker identity is stable'); END""",
    """CREATE TRIGGER lc_projects_insert_builds BEFORE INSERT ON lc_projects
       WHEN (NEW.active_build_id IS NOT NULL AND NOT EXISTS
             (SELECT 1 FROM lc_records WHERE record_id=NEW.active_build_id
                 AND project_id=NEW.project_id AND kind='BUILD'))
         OR (NEW.pending_build_id IS NOT NULL AND NOT EXISTS
             (SELECT 1 FROM lc_records WHERE record_id=NEW.pending_build_id
                 AND project_id=NEW.project_id AND kind='BUILD'))
       BEGIN SELECT RAISE(ABORT, 'Lifecycle pointers require same-project builds'); END""",
    """CREATE TRIGGER lc_projects_update_builds BEFORE UPDATE ON lc_projects
       WHEN (NEW.active_build_id IS NOT NULL AND NOT EXISTS
             (SELECT 1 FROM lc_records WHERE record_id=NEW.active_build_id
                 AND project_id=NEW.project_id AND kind='BUILD'))
         OR (NEW.pending_build_id IS NOT NULL AND NOT EXISTS
             (SELECT 1 FROM lc_records WHERE record_id=NEW.pending_build_id
                 AND project_id=NEW.project_id AND kind='BUILD'))
       BEGIN SELECT RAISE(ABORT, 'Lifecycle pointers require same-project builds'); END""",
    """CREATE TRIGGER lc_projects_no_delete BEFORE DELETE ON lc_projects
       BEGIN SELECT RAISE(ABORT, 'Lifecycle tracker history is retained'); END""",
    """CREATE TRIGGER lc_projects_no_replace BEFORE INSERT ON lc_projects
       WHEN EXISTS (SELECT 1 FROM lc_projects WHERE project_id=NEW.project_id)
       BEGIN SELECT RAISE(ABORT, 'Lifecycle tracker cannot be replaced'); END""",
    """CREATE TABLE lc_events (
        sequence INTEGER PRIMARY KEY,
        project_id TEXT NOT NULL,
        record_id TEXT,
        kind TEXT NOT NULL,
        data TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        FOREIGN KEY(record_id, project_id) REFERENCES lc_records(record_id, project_id)
    )""",
    """CREATE INDEX lc_events_project ON lc_events(project_id, sequence)""",
    """CREATE TRIGGER lc_events_no_update BEFORE UPDATE ON lc_events
       BEGIN SELECT RAISE(ABORT, 'Lifecycle events are append-only'); END""",
    """CREATE TRIGGER lc_events_no_delete BEFORE DELETE ON lc_events
       BEGIN SELECT RAISE(ABORT, 'Lifecycle events are append-only'); END""",
    """CREATE TRIGGER lc_events_no_replace BEFORE INSERT ON lc_events
       WHEN EXISTS (SELECT 1 FROM lc_events WHERE sequence=NEW.sequence)
       BEGIN SELECT RAISE(ABORT, 'Lifecycle events are append-only'); END""",
)
_REQUIRED_TABLES_V5 = _REQUIRED_TABLES_V4 | {
    "val_drafts",
    "val_plan_bindings",
    "val_runs",
    "val_events",
}
_MIGRATION_V7 = (
    """CREATE TABLE work_items (
        work_item_id TEXT PRIMARY KEY NOT NULL CHECK (length(work_item_id) = 37),
        project_id TEXT NOT NULL CHECK (length(project_id) BETWEEN 1 AND 128),
        parent_work_item_id TEXT CHECK (length(parent_work_item_id) = 37),
        scope_level TEXT NOT NULL CHECK (scope_level IN ('TASK', 'CHANGE', 'PROJECT', 'CRITICAL')),
        objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 600),
        acceptance_criteria TEXT NOT NULL CHECK (length(acceptance_criteria) BETWEEN 1 AND 32768),
        status TEXT NOT NULL CHECK (status IN ('OPEN', 'ACTIVE', 'BLOCKED', 'COMPLETED', 'CANCELLED')),
        validation_policy TEXT NOT NULL CHECK (length(validation_policy) BETWEEN 1 AND 64),
        creator_id TEXT NOT NULL CHECK (length(creator_id) BETWEEN 1 AND 128),
        created_at TEXT NOT NULL CHECK (length(created_at) = 32),
        updated_at TEXT NOT NULL CHECK (length(updated_at) = 32),
        completed_at TEXT CHECK (length(completed_at) = 32),
        CHECK (parent_work_item_id IS NULL OR parent_work_item_id <> work_item_id),
        CHECK ((status IN ('COMPLETED', 'CANCELLED')) = (completed_at IS NOT NULL)),
        UNIQUE (project_id, work_item_id),
        FOREIGN KEY (project_id, parent_work_item_id) REFERENCES work_items (project_id, work_item_id)
    )""",
    """CREATE INDEX work_items_parent
       ON work_items (parent_work_item_id, created_at, work_item_id)""",
    """CREATE INDEX work_items_order
       ON work_items (created_at, work_item_id)""",
    """CREATE TABLE work_item_dependencies (
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        dependency_id TEXT NOT NULL,
        PRIMARY KEY (work_item_id, dependency_id),
        CHECK (work_item_id <> dependency_id),
        FOREIGN KEY (project_id, work_item_id) REFERENCES work_items (project_id, work_item_id),
        FOREIGN KEY (project_id, dependency_id) REFERENCES work_items (project_id, work_item_id)
    )""",
    """CREATE TABLE work_item_modules (
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        module_id TEXT NOT NULL CHECK (length(module_id) BETWEEN 1 AND 128),
        module_contract_id TEXT CHECK (length(module_contract_id) BETWEEN 1 AND 128),
        module_revision_id TEXT CHECK (length(module_revision_id) BETWEEN 1 AND 128),
        PRIMARY KEY (work_item_id, module_id),
        CHECK ((module_contract_id IS NULL) = (module_revision_id IS NULL)),
        FOREIGN KEY (project_id, work_item_id) REFERENCES work_items (project_id, work_item_id),
        FOREIGN KEY (module_contract_id, module_revision_id)
            REFERENCES revisions (contract_id, revision_id)
    )""",
    """CREATE TRIGGER work_items_no_delete BEFORE DELETE ON work_items
       BEGIN SELECT RAISE(ABORT, 'Work Items are retained'); END""",
    """CREATE TRIGGER work_items_no_replace BEFORE INSERT ON work_items
       WHEN EXISTS (SELECT 1 FROM work_items WHERE work_item_id = NEW.work_item_id)
       BEGIN SELECT RAISE(ABORT, 'Work Item identities are immutable'); END""",
)
_MIGRATION_V8 = (
    """CREATE TABLE agent_sessions (
        session_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 128),
        read_only INTEGER NOT NULL CHECK (read_only IN (0, 1)),
        provider_version TEXT CHECK (length(provider_version) BETWEEN 1 AND 128),
        model TEXT CHECK (length(model) BETWEEN 1 AND 128),
        native_session_id TEXT CHECK (length(native_session_id) BETWEEN 1 AND 128),
        native_resume INTEGER NOT NULL CHECK (native_resume IN (0, 1)),
        envelope_sha256 TEXT CHECK (length(envelope_sha256) = 64),
        state TEXT NOT NULL CHECK (state IN (
            'STARTING', 'RUNNING', 'CANCELLING', 'SUCCEEDED', 'FAILED',
            'CANCELLED', 'TIMED_OUT', 'INTERRUPTED'
        )),
        run_generation INTEGER NOT NULL CHECK (run_generation BETWEEN 1 AND 9007199254740991),
        owner_id TEXT,
        lease_expires_at REAL,
        cleanup_unconfirmed INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_unconfirmed IN (0, 1)),
        timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds BETWEEN 1 AND 3600),
        created_at TEXT NOT NULL,
        started_at TEXT,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        input_tokens INTEGER CHECK (input_tokens BETWEEN 0 AND 9007199254740991),
        output_tokens INTEGER CHECK (output_tokens BETWEEN 0 AND 9007199254740991),
        cached_tokens INTEGER CHECK (cached_tokens BETWEEN 0 AND 9007199254740991),
        duration_ms INTEGER CHECK (duration_ms BETWEEN 0 AND 9007199254740991),
        reported_cost TEXT CHECK (length(reported_cost) BETWEEN 1 AND 64),
        reported_cost_currency TEXT CHECK (length(reported_cost_currency) = 3),
        CHECK ((owner_id IS NULL) = (lease_expires_at IS NULL)),
        CHECK ((state IN ('STARTING', 'RUNNING', 'CANCELLING')) = (owner_id IS NOT NULL)),
        CHECK ((owner_id IS NULL) = (finished_at IS NOT NULL)),
        CHECK ((reported_cost IS NULL) = (reported_cost_currency IS NULL)),
        FOREIGN KEY (project_id, work_item_id) REFERENCES work_items (project_id, work_item_id)
    )""",
    """CREATE UNIQUE INDEX agent_sessions_one_active
       ON agent_sessions ((1)) WHERE state IN ('STARTING', 'RUNNING', 'CANCELLING')""",
    """CREATE INDEX agent_sessions_recent ON agent_sessions (created_at DESC, session_id DESC)""",
    """CREATE TABLE agent_session_events (
        session_id TEXT NOT NULL REFERENCES agent_sessions (session_id),
        sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 9007199254740991),
        run_generation INTEGER NOT NULL CHECK (run_generation BETWEEN 1 AND 9007199254740991),
        kind TEXT NOT NULL CHECK (kind IN (
            'SESSION_CREATED', 'SESSION_STARTED', 'OUTPUT', 'TOOL_ACTIVITY', 'USAGE',
            'WARNING', 'ERROR', 'SESSION_FINISHED', 'CANCEL_REQUESTED', 'CANCELLED',
            'TIMED_OUT', 'INTERRUPTED', 'RECOVERY_STARTED'
        )),
        occurred_at TEXT NOT NULL,
        payload TEXT NOT NULL CHECK (length(CAST(payload AS BLOB)) BETWEEN 2 AND 4096),
        native_kind TEXT CHECK (length(native_kind) BETWEEN 1 AND 128),
        PRIMARY KEY (session_id, sequence)
    )""",
    """CREATE TRIGGER agent_sessions_identity BEFORE UPDATE ON agent_sessions
       WHEN NEW.session_id <> OLD.session_id OR NEW.project_id <> OLD.project_id
            OR NEW.work_item_id <> OLD.work_item_id OR NEW.provider <> OLD.provider
            OR NEW.read_only <> OLD.read_only OR NEW.timeout_seconds <> OLD.timeout_seconds
            OR NEW.created_at <> OLD.created_at
            OR (OLD.envelope_sha256 IS NOT NULL AND NEW.envelope_sha256 IS NOT OLD.envelope_sha256)
            OR (OLD.native_session_id IS NOT NULL AND NEW.native_session_id IS NOT OLD.native_session_id)
       BEGIN SELECT RAISE(ABORT, 'Agent Session identity is immutable'); END""",
    """CREATE TRIGGER agent_sessions_terminal BEFORE UPDATE ON agent_sessions
       WHEN OLD.state IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
       BEGIN SELECT RAISE(ABORT, 'Terminal Agent Sessions are immutable'); END""",
    """CREATE TRIGGER agent_sessions_no_delete BEFORE DELETE ON agent_sessions
       BEGIN SELECT RAISE(ABORT, 'Agent Sessions are retained'); END""",
    """CREATE TRIGGER agent_sessions_no_replace BEFORE INSERT ON agent_sessions
       WHEN EXISTS (SELECT 1 FROM agent_sessions WHERE session_id = NEW.session_id)
            OR (NEW.state IN ('STARTING', 'RUNNING', 'CANCELLING') AND EXISTS (
                SELECT 1 FROM agent_sessions WHERE state IN ('STARTING', 'RUNNING', 'CANCELLING')
            ))
       BEGIN SELECT RAISE(ABORT, 'Agent Session conflicts with retained state'); END""",
    """CREATE TRIGGER agent_session_events_no_update BEFORE UPDATE ON agent_session_events
       BEGIN SELECT RAISE(ABORT, 'Agent Session events are append-only'); END""",
    """CREATE TRIGGER agent_session_events_no_delete BEFORE DELETE ON agent_session_events
       BEGIN SELECT RAISE(ABORT, 'Agent Session events are append-only'); END""",
    """CREATE TRIGGER agent_session_events_no_replace BEFORE INSERT ON agent_session_events
       WHEN EXISTS (
           SELECT 1 FROM agent_session_events
           WHERE session_id = NEW.session_id AND sequence = NEW.sequence
       )
       BEGIN SELECT RAISE(ABORT, 'Agent Session events are append-only'); END""",
)

_PROJECTION_COLUMN = "context_projection_id TEXT REFERENCES context_projections (projection_id)"
_MIGRATION_V9 = (
    """CREATE TABLE context_projections (
        projection_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        target_provider TEXT NOT NULL CHECK (length(target_provider) BETWEEN 1 AND 128),
        selector_policy_version INTEGER NOT NULL CHECK (selector_policy_version BETWEEN 1 AND 9007199254740991),
        redaction_policy_version INTEGER NOT NULL CHECK (redaction_policy_version BETWEEN 1 AND 9007199254740991),
        canonical_payload TEXT NOT NULL CHECK (length(CAST(canonical_payload AS BLOB)) BETWEEN 2 AND 131072),
        projection_digest TEXT UNIQUE NOT NULL CHECK (length(projection_digest) = 64),
        source_set_digest TEXT NOT NULL CHECK (length(source_set_digest) = 64),
        created_at TEXT NOT NULL,
        FOREIGN KEY (project_id, work_item_id) REFERENCES work_items (project_id, work_item_id)
    )""",
    """CREATE INDEX context_projections_work ON context_projections
       (work_item_id, created_at DESC, projection_id DESC)""",
    """CREATE TABLE context_projection_sources (
        projection_id TEXT NOT NULL REFERENCES context_projections (projection_id) DEFERRABLE INITIALLY DEFERRED,
        ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 256),
        kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 128),
        authority TEXT NOT NULL CHECK (authority IN ('CANONICAL', 'OPERATIONAL', 'DERIVED')),
        identity TEXT NOT NULL CHECK (length(identity) BETWEEN 1 AND 512),
        revision TEXT CHECK (length(revision) BETWEEN 1 AND 512),
        digest TEXT NOT NULL CHECK (length(digest) = 64),
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 128),
        redaction TEXT NOT NULL CHECK (length(CAST(redaction AS BLOB)) BETWEEN 2 AND 2048),
        capture TEXT NOT NULL CHECK (length(CAST(capture AS BLOB)) BETWEEN 2 AND 131072),
        PRIMARY KEY (projection_id, ordinal)
    )""",
    """CREATE TABLE session_handoffs (
        handoff_id TEXT PRIMARY KEY NOT NULL,
        project_id TEXT NOT NULL,
        work_item_id TEXT NOT NULL,
        source_session_id TEXT NOT NULL REFERENCES agent_sessions (session_id),
        run_generation INTEGER NOT NULL CHECK (run_generation BETWEEN 1 AND 9007199254740991),
        provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 128),
        terminal_state TEXT NOT NULL CHECK (terminal_state IN (
            'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'INTERRUPTED'
        )),
        event_cutoff INTEGER NOT NULL CHECK (event_cutoff BETWEEN 1 AND 9007199254740991),
        canonical_payload TEXT NOT NULL CHECK (length(CAST(canonical_payload AS BLOB)) BETWEEN 2 AND 131072),
        digest TEXT NOT NULL CHECK (length(digest) = 64),
        created_at TEXT NOT NULL,
        UNIQUE (source_session_id, run_generation, event_cutoff),
        FOREIGN KEY (project_id, work_item_id) REFERENCES work_items (project_id, work_item_id),
        FOREIGN KEY (source_session_id, event_cutoff) REFERENCES agent_session_events (session_id, sequence)
    )""",
    """CREATE INDEX session_handoffs_work ON session_handoffs
       (work_item_id, created_at DESC, handoff_id DESC)""",
    f"ALTER TABLE agent_sessions ADD COLUMN {_PROJECTION_COLUMN}",
    """CREATE TRIGGER agent_sessions_projection_on_insert BEFORE INSERT ON agent_sessions
       WHEN NEW.context_projection_id IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM context_projections
           WHERE projection_id = NEW.context_projection_id AND project_id = NEW.project_id
                 AND work_item_id = NEW.work_item_id AND target_provider = NEW.provider
       )
       BEGIN SELECT RAISE(ABORT, 'Agent Session projection identity changed'); END""",
    """CREATE TRIGGER agent_sessions_projection_immutable BEFORE UPDATE ON agent_sessions
       WHEN NEW.context_projection_id IS NOT OLD.context_projection_id
       BEGIN SELECT RAISE(ABORT, 'Agent Session context projection is immutable'); END""",
    """CREATE TRIGGER context_projections_no_replace BEFORE INSERT ON context_projections
       WHEN EXISTS (
           SELECT 1 FROM context_projections
           WHERE projection_id = NEW.projection_id OR projection_digest = NEW.projection_digest
       )
       BEGIN SELECT RAISE(ABORT, 'Context Projection is immutable'); END""",
    """CREATE TRIGGER context_sources_sealed BEFORE INSERT ON context_projection_sources
       WHEN EXISTS (SELECT 1 FROM context_projections WHERE projection_id = NEW.projection_id)
       BEGIN SELECT RAISE(ABORT, 'Context Projection sources are sealed'); END""",
    """CREATE TRIGGER handoffs_source_matches BEFORE INSERT ON session_handoffs
       WHEN NOT EXISTS (
           SELECT 1 FROM agent_sessions AS s
           JOIN agent_session_events AS e ON e.session_id = s.session_id
           WHERE s.session_id = NEW.source_session_id AND s.project_id = NEW.project_id
                 AND s.cleanup_unconfirmed = 0
                 AND s.work_item_id = NEW.work_item_id AND s.provider = NEW.provider
                 AND s.state = NEW.terminal_state AND s.run_generation = NEW.run_generation
                 AND e.sequence = NEW.event_cutoff AND e.run_generation = NEW.run_generation
                 AND e.kind = CASE WHEN s.state IN ('SUCCEEDED', 'FAILED')
                                   THEN 'SESSION_FINISHED' ELSE s.state END
                 AND NOT EXISTS (
                     SELECT 1 FROM agent_session_events AS later
                     WHERE later.session_id = s.session_id AND later.sequence > NEW.event_cutoff
                 )
       )
       BEGIN SELECT RAISE(ABORT, 'Session Handoff source is unavailable'); END""",
    """CREATE TRIGGER handoffs_no_replace BEFORE INSERT ON session_handoffs
       WHEN EXISTS (
           SELECT 1 FROM session_handoffs WHERE handoff_id = NEW.handoff_id OR (
               source_session_id = NEW.source_session_id AND run_generation = NEW.run_generation
               AND event_cutoff = NEW.event_cutoff
           )
       )
       BEGIN SELECT RAISE(ABORT, 'Session Handoff is immutable'); END""",
    *(
        f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table}\n"
        "       BEGIN SELECT RAISE(ABORT, 'Derived context history is immutable'); END"
        for table in ("context_projections", "context_projection_sources", "session_handoffs")
        for operation in ("UPDATE", "DELETE")
    ),
)

_SUPPORTED_VERSIONS = range(1, _SCHEMA_VERSION + 1)
_REQUIRED_TABLES_V6 = _REQUIRED_TABLES_V5 | {"lc_records", "lc_projects", "lc_events"}
_REQUIRED_TABLES_V7 = _REQUIRED_TABLES_V6 | {
    "work_items",
    "work_item_dependencies",
    "work_item_modules",
}
_REQUIRED_TABLES_V8 = _REQUIRED_TABLES_V7 | {"agent_sessions", "agent_session_events"}
_REQUIRED_TABLES = _REQUIRED_TABLES_V8 | {
    "context_projections",
    "context_projection_sources",
    "session_handoffs",
}
_REVISIONS = """SELECT revisions.*, contracts.contract_type, contracts.project_id,
                contracts.active_revision_id
                FROM revisions JOIN contracts USING (contract_id)"""


def _digest(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _markers(connection: sqlite3.Connection) -> tuple[int, int]:
    return (
        connection.execute("PRAGMA application_id").fetchone()[0],
        connection.execute("PRAGMA user_version").fetchone()[0],
    )


def _verify_database(connection: sqlite3.Connection, version: int) -> None:
    """Check a supported schema without upgrading or rewriting its records."""
    if version not in _SUPPORTED_VERSIONS or _markers(connection) != (
        _APPLICATION_ID,
        version,
    ):
        raise StoreError("The state database application or schema version is unsupported.")
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
    }
    required = {
        1: _REQUIRED_TABLES_V1,
        2: _REQUIRED_TABLES_V2,
        3: _REQUIRED_TABLES_V3,
        4: _REQUIRED_TABLES_V4,
        5: _REQUIRED_TABLES_V5,
        6: _REQUIRED_TABLES_V6,
        7: _REQUIRED_TABLES_V7,
        8: _REQUIRED_TABLES_V8,
        9: _REQUIRED_TABLES,
    }[version]
    if not required <= tables:
        raise StoreError("The state database schema is incomplete.")
    if version >= 7:
        _verify_migration_schema(connection, version, exact=version == 8)
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise StoreError("The state database failed its integrity check.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StoreError("The state database failed its foreign-key integrity check.")


def _verify_migration_schema(
    connection: sqlite3.Connection, version: int, *, exact: bool = False
) -> None:
    """Verify the original definitions survive before and after verified migrations."""
    actual = {
        row[0]
        for row in connection.execute(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND substr(name, 1, 7) <> 'sqlite_'"
        )
    }
    expected = {
        *_MIGRATION_V1,
        *_MIGRATION_V2,
        *_MIGRATION_V3,
        *_MIGRATION_V4,
        *_MIGRATION_V5,
        *_MIGRATION_V6,
    }
    if version >= 7:
        expected.update(_MIGRATION_V7)
    if version >= 8:
        expected.update(_MIGRATION_V8)
    if version >= 9:
        # SQLite places ADD COLUMN immediately before the first table constraint.
        session_table = _MIGRATION_V8[0]
        expected.remove(session_table)
        expected.add(
            session_table.replace(
                ",\n        CHECK ((owner_id IS NULL)",
                f", {_PROJECTION_COLUMN},\n        CHECK ((owner_id IS NULL)",
                1,
            )
        )
        expected.update(sql for sql in _MIGRATION_V9 if not sql.startswith("ALTER TABLE "))
    if not expected <= actual or (exact and expected != actual):
        raise StoreError("The state database has missing or altered schema definitions.")


def _stored_document(row: sqlite3.Row) -> dict:
    serialized = row["document"]
    if not isinstance(serialized, str) or _digest(serialized) != row["document_sha256"]:
        raise IntegrityError("Stored contract revision has an inconsistent digest.")
    try:
        document = json.loads(serialized)
        reference = ContractRef.from_document(document)
        metadata = document["metadata"]
        version = metadata["version"]
        status = metadata["status"]
    except (TypeError, KeyError, ValueError):
        raise IntegrityError("Stored contract revision is invalid.") from None
    if (
        reference.contract_id != row["contract_id"]
        or reference.revision_id != row["revision_id"]
        or reference.contract_type != row["contract_type"]
        or reference.project_id != row["project_id"]
        or version != row["version"]
        or status != "PUBLISHED"
    ):
        raise IntegrityError("Stored contract revision has inconsistent metadata.")
    return document


def _check_database_files(database: Path, *, allow_existing: bool = True) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = database.with_name(database.name + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if suffix and stat.S_ISREG(info.st_mode) and info.st_nlink == 0 and not is_link(info):
            # SQLite may unlink a sidecar while lstat is completing on Linux.
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or is_link(info):
            raise StoreError("State database files must be regular, unlinked local files.")
        if not allow_existing:
            raise ConflictError("A state backup already exists or has existing sidecars.")


class _WriteLockBusy(ConflictError):
    """The write transaction did not start and can be attempted again."""


class ContractStore:
    """Open or initialize the state database under an existing project directory."""

    def __init__(self, project_root: str | Path, *, _write_lock_timeout: float | None = None):
        try:
            self._project_root = Path(project_root).resolve(strict=True)
            state = private_directory(self._project_root, create=True)
        except (IntegrityError, OSError, RuntimeError):
            raise StoreError("Cannot open the private project state directory.") from None
        self._database_path = state / "state.db"
        with self._transaction(create=True, lock_timeout=_write_lock_timeout) as connection:
            self._migrate(connection)

    @property
    def database_path(self) -> Path:
        """Return the private database location."""
        return self._database_path

    @staticmethod
    def verify_existing(project_root: Path) -> bool:
        """Inspect a saved database without creating or migrating project state."""
        with ContractStore.read_snapshot(project_root) as (_, version):
            return version is not None

    @staticmethod
    @contextmanager
    def read_snapshot(project_root: Path) -> Iterator[tuple[sqlite3.Connection | None, int | None]]:
        """Read coherent operational state while other connections may be writing."""
        database = private_directory(project_root) / "state.db"
        try:
            _check_database_files(database)
            if not database.exists():
                yield None, None
                return
            deadline = monotonic() + 2
            uri = f"{database.as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2, autocommit=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.set_progress_handler(lambda: monotonic() >= deadline, 1000)
                connection.execute("PRAGMA trusted_schema = OFF")
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN")
                _, version = _markers(connection)
                _verify_database(connection, version)
                yield connection, version
                connection.execute("COMMIT")
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 255
            if code in _BUSY_CODES or code == sqlite3.SQLITE_INTERRUPT:
                raise ConflictError("The database is busy. Retry the Agent Session read.") from None
            raise StoreError("Cannot read the existing project database.") from None
        except OSError:
            raise StoreError("Cannot read the existing project database.") from None

    @staticmethod
    def inspect_existing(project_root: Path, *, timeout: float = 2) -> int | None:
        """Return a verified schema version without writing SQLite sidecars."""
        with ContractStore.inspect_snapshot(project_root, timeout=timeout) as (_, version):
            return version

    @staticmethod
    @contextmanager
    def inspect_snapshot(
        project_root: Path, *, timeout: float = 2
    ) -> Iterator[tuple[sqlite3.Connection | None, int | None]]:
        """Read a verified snapshot without creating state, migrating, or writing sidecars."""
        database = private_directory(project_root) / "state.db"
        sidecars = [
            database.with_name(database.name + suffix) for suffix in ("-journal", "-wal", "-shm")
        ]

        def fingerprint():
            _check_database_files(database)
            if any(os.path.lexists(path) for path in sidecars):
                raise ConflictError("The database has an active journal or WAL.")
            try:
                info = database.lstat()
            except FileNotFoundError:
                return None
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

        try:
            before = fingerprint()
            if before is None:
                yield None, None
                if fingerprint() is not None:
                    raise ConflictError("The database changed during status inspection.")
                return
            deadline = monotonic() + timeout
            # An immutable read cannot create WAL/shared-memory files. Refuse
            # active sidecars and verify that the database stayed unchanged.
            with closing(
                sqlite3.connect(
                    f"{database.as_uri()}?mode=ro&immutable=1",
                    uri=True,
                    timeout=0,
                    autocommit=True,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.set_progress_handler(lambda: monotonic() >= deadline, 1000)
                connection.execute("PRAGMA trusted_schema = OFF")
                connection.execute("PRAGMA foreign_keys = ON")
                _, version = _markers(connection)
                _verify_database(connection, version)
                yield connection, version
            if fingerprint() != before:
                raise ConflictError("The database changed during status inspection.")
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", 0) & 255 == sqlite3.SQLITE_INTERRUPT:
                raise ConflictError("The database status inspection timed out.") from None
            raise StoreError("Cannot read the existing project database.") from None
        except OSError:
            raise StoreError("Cannot read the existing project database.") from None

    @staticmethod
    def current_schema_version() -> int:
        return _SCHEMA_VERSION

    @staticmethod
    def active_references(
        connection: sqlite3.Connection, project_id: str | None, contract_type: str, *, limit: int
    ) -> list[ContractRef]:
        """Project active identities without reading unbounded contract payloads."""
        query = (
            "SELECT contract_id, active_revision_id, project_id FROM contracts "
            "WHERE contract_type = ? AND active_revision_id IS NOT NULL"
        )
        parameters = (contract_type,)
        if project_id is not None:
            query += " AND project_id = ?"
            parameters += (project_id,)
        return [
            ContractRef(row[0], row[1], contract_type, row[2])
            for row in connection.execute(
                query + " ORDER BY contract_id LIMIT ?", (*parameters, limit)
            )
        ]

    @contextmanager
    def _connection(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            state = private_directory(self._project_root)
        except (IntegrityError, OSError, RuntimeError):
            raise StoreError("Cannot open the private project state directory.") from None
        if state != self.database_path.parent:
            raise StoreError("The private project state directory changed.")
        try:
            _check_database_files(self.database_path)
            mode = "rwc" if create else "rw"
            uri = f"{self.database_path.as_uri()}?mode={mode}"
            with closing(
                sqlite3.connect(uri, uri=True, timeout=_LOCK_TIMEOUT, autocommit=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                if not create and _markers(connection) != (_APPLICATION_ID, _SCHEMA_VERSION):
                    raise StoreError(
                        "The state database application or schema version is unsupported."
                    )
                yield connection
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0)
            primary_code = code & 255
            if primary_code in _BUSY_CODES:
                raise ConflictError(_BUSY_MESSAGE) from None
            if primary_code == sqlite3.SQLITE_READONLY:
                raise StoreError("The state database is read-only.") from None
            if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                raise StoreError("The state database is corrupt or is not SQLite.") from None
            if code in {sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, sqlite3.SQLITE_CONSTRAINT_UNIQUE}:
                raise ConflictError("A contract identity or revision already exists.") from None
            raise StoreError("The state database operation failed.") from None
        except OSError:
            raise StoreError("Cannot access private state database files.") from None

    @contextmanager
    def _transaction(
        self, *, create: bool = False, lock_timeout: float | None = None
    ) -> Iterator[sqlite3.Connection]:
        with self._connection(create=create) as connection:
            if lock_timeout is not None:
                connection.execute(f"PRAGMA busy_timeout = {int(lock_timeout * 1000)}")
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                primary_code = getattr(error, "sqlite_errorcode", 0) & 255
                if lock_timeout is not None and primary_code in _BUSY_CODES:
                    raise _WriteLockBusy(_BUSY_MESSAGE) from None
                raise
            try:
                if lock_timeout is not None:
                    # Keep the usual wait for readers when committing the acquired write.
                    connection.execute(f"PRAGMA busy_timeout = {int(_LOCK_TIMEOUT * 1000)}")
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _migrate(self, connection: sqlite3.Connection) -> None:
        application, version = _markers(connection)
        if application == _APPLICATION_ID and version in _SUPPORTED_VERSIONS:
            _verify_database(connection, version)
            if version == _SCHEMA_VERSION:
                return
            if version == 6:
                _verify_migration_schema(connection, version)
            if not self.database_path.stat().st_mode & stat.S_IWUSR:
                raise StoreError("The state database is read-only.")
            # The write reservation prevents another constructor or publisher
            # changing the source while a separate connection backs it up.
            # Backing up the connection holding BEGIN IMMEDIATE would stall.
            uri = f"{self.database_path.as_uri()}?mode=ro"
            with closing(
                sqlite3.connect(uri, uri=True, timeout=_LOCK_TIMEOUT, autocommit=True)
            ) as source:
                source.execute("PRAGMA trusted_schema = OFF")
                source.execute("PRAGMA foreign_keys = ON")
                backup = self.database_path.with_name(f"state.pre-v{version + 1}-{uuid4().hex}.db")
                self._copy_backup(source, backup, version=version)
        else:
            has_objects = connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
            if application != 0 or version != 0 or has_objects is not None:
                raise StoreError("The state database application or schema version is unsupported.")
            for statement in _MIGRATION_V1:
                connection.execute(statement)
            version = 1
        if version < 2:
            for statement in _MIGRATION_V2:
                connection.execute(statement)
        if version < 3:
            for statement in _MIGRATION_V3:
                connection.execute(statement)
        if version < 4:
            for statement in _MIGRATION_V4:
                connection.execute(statement)
        if version < 5:
            for statement in _MIGRATION_V5:
                connection.execute(statement)
        if version < 6:
            for statement in _MIGRATION_V6:
                connection.execute(statement)
        if version < 7:
            for statement in _MIGRATION_V7:
                connection.execute(statement)
        if version < 8:
            for statement in _MIGRATION_V8:
                connection.execute(statement)
        if version < 9:
            for statement in _MIGRATION_V9:
                connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        _verify_database(connection, _SCHEMA_VERSION)

    @staticmethod
    def _lookup(connection: sqlite3.Connection, reference: ContractRef) -> dict | None:
        row = connection.execute(
            f"{_REVISIONS} WHERE revisions.revision_id = ? AND revisions.contract_id = ? "
            "AND contracts.contract_type = ? AND contracts.project_id = ?",
            (
                reference.revision_id,
                reference.contract_id,
                reference.contract_type,
                reference.project_id,
            ),
        ).fetchone()
        return None if row is None else _stored_document(row)

    @staticmethod
    def _active(connection: sqlite3.Connection, contract_id: str) -> dict | None:
        row = connection.execute(
            f"{_REVISIONS} WHERE contracts.contract_id = ? "
            "AND contracts.active_revision_id = revisions.revision_id",
            (contract_id,),
        ).fetchone()
        return None if row is None else _stored_document(row)

    @staticmethod
    def _history(connection: sqlite3.Connection, contract_type: str, project_id: str) -> list[dict]:
        rows = connection.execute(
            f"{_REVISIONS} WHERE contracts.contract_type = ? AND contracts.project_id = ? "
            "ORDER BY revisions.contract_id, revisions.version",
            (contract_type, project_id),
        )
        return [_stored_document(row) for row in rows]

    @staticmethod
    def _predecessor(
        connection: sqlite3.Connection, reference: ContractRef, document: dict
    ) -> dict | None:
        metadata = document["metadata"]
        row = connection.execute(
            f"{_REVISIONS} WHERE revisions.contract_id = ? ORDER BY revisions.version DESC LIMIT 1",
            (reference.contract_id,),
        ).fetchone()
        if row is None:
            if metadata["version"] != 1 or metadata["supersedes"] is not None:
                raise ConflictError("The first revision must be version 1 without a predecessor.")
            return None
        previous = _stored_document(row)
        previous_ref = ContractRef.from_document(previous)
        if (previous_ref.project_id, previous_ref.contract_type) != (
            reference.project_id,
            reference.contract_type,
        ):
            raise ConflictError("The logical contract already belongs to another project or type.")
        if reference.contract_type == "VALIDATION_RESULT":
            raise ConflictError("Each validation run requires a distinct contract identity.")
        if (
            metadata["supersedes"] is None
            or ContractRef.from_json(metadata["supersedes"]) != previous_ref
            or metadata["version"] != row["version"] + 1
        ):
            raise ConflictError("The new revision does not follow the current contract head.")
        if (
            reference.contract_type in _ACTIVE_TYPES
            and row["active_revision_id"] != previous_ref.revision_id
        ):
            raise ConflictError("The active revision has changed. Reconcile before publishing.")
        if document["payload"] == previous["payload"]:
            raise ConflictError("An unchanged payload does not create a new content revision.")
        if reference.contract_type == "PROJECT_REVISION_REQUEST" and any(
            document["payload"][field] != previous["payload"][field]
            for field in ("request_id", "base_contract_version")
        ):
            raise ConflictError(
                "A revision request retains its request ID and exact base revision."
            )
        return previous

    def publish(self, document: dict, *, context: PublishContext) -> ContractRef:
        """Validate and atomically publish a revision, pointer and audit event."""
        with self._transaction() as connection:
            return self._publish(connection, document, context=context)

    def _publish(
        self, connection: sqlite3.Connection, document: dict, *, context: PublishContext
    ) -> ContractRef:
        """Publish under the caller's writer reservation with the same Kernel checks."""
        serialized = canonical_json(document)
        document = json.loads(serialized)
        validate_schema(document)
        metadata = document["metadata"]
        if metadata["status"] != "PUBLISHED":
            raise IntegrityError("Publication requires a PUBLISHED document.")
        reference = ContractRef.from_document(document)
        predecessor = metadata["supersedes"]
        digest = _digest(serialized)

        existing = connection.execute(
            f"{_REVISIONS} WHERE revisions.revision_id = ?", (reference.revision_id,)
        ).fetchone()
        if existing is not None:
            stored = _stored_document(existing)
            if canonical_json(stored) != serialized:
                raise ConflictError("The revision ID already identifies different content.")
            previous = (
                self._lookup(connection, ContractRef.from_json(predecessor))
                if predecessor is not None
                else None
            )
            if predecessor is not None and previous is None:
                raise IntegrityError("A stored revision has a missing predecessor.")
            require_authority(document, previous, context)
            return reference

        previous = self._predecessor(connection, reference, document)
        require_authority(document, previous, context)
        validate_semantics(
            document,
            lambda target: self._lookup(connection, target),
            lambda contract_id: self._active(connection, contract_id),
            history=lambda kind, project_id: self._history(connection, kind, project_id),
            verify_artifact=self.verify_artifact,
        )
        targets = {target.revision_id for target in iter_references(document)}
        self._insert_revision(connection, reference, metadata, serialized, digest, targets)
        return reference

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        reference: ContractRef,
        metadata: dict,
        serialized: str,
        digest: str,
        targets: set[str],
    ) -> None:
        predecessor = metadata["supersedes"]
        previous_id = None if predecessor is None else predecessor["revision_id"]
        if previous_id is None:
            connection.execute(
                "INSERT INTO contracts (contract_id, project_id, contract_type) VALUES (?, ?, ?)",
                (reference.contract_id, reference.project_id, reference.contract_type),
            )
        connection.execute(
            "INSERT INTO revisions (revision_id, contract_id, version, supersedes_revision_id, "
            "document, document_sha256) VALUES (?, ?, ?, ?, ?, ?)",
            (
                reference.revision_id,
                reference.contract_id,
                metadata["version"],
                previous_id,
                serialized,
                digest,
            ),
        )
        connection.executemany(
            "INSERT INTO contract_references VALUES (?, ?)",
            ((reference.revision_id, target) for target in sorted(targets)),
        )
        activates = reference.contract_type in _ACTIVE_TYPES
        if activates:
            cursor = connection.execute(
                "UPDATE contracts SET active_revision_id = ? "
                "WHERE contract_id = ? AND active_revision_id IS ?",
                (reference.revision_id, reference.contract_id, previous_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("The active revision has changed. Reconcile before publishing.")
        connection.execute(
            "INSERT INTO audit_events (revision_id, activated, occurred_at) VALUES (?, ?, ?)",
            (reference.revision_id, activates, datetime.now(UTC).isoformat()),
        )

    def get(self, reference: ContractRef) -> dict | None:
        """Retrieve the exact published revision, or None for an unmatched reference."""
        with self._connection() as connection:
            return self._lookup(connection, reference)

    def list_revisions(self, contract_id: str) -> list[dict]:
        """Return immutable revisions for a logical contract in content-version order."""
        with self._connection() as connection:
            rows = connection.execute(
                f"{_REVISIONS} WHERE revisions.contract_id = ? ORDER BY revisions.version",
                (contract_id,),
            )
            return [_stored_document(row) for row in rows]

    def resolve_active(self, contract_id: str) -> dict | None:
        """Read the active revision without replacing any exact reference."""
        with self._connection() as connection:
            return self._active(connection, contract_id)

    def list_events(self) -> list[dict]:
        """Read the publication audit log in commit sequence order."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence, audit_events.revision_id, contract_id, contract_type, project_id, "
                "supersedes_revision_id AS previous_revision_id, activated, occurred_at "
                "FROM audit_events JOIN revisions USING (revision_id) "
                "JOIN contracts USING (contract_id) ORDER BY sequence"
            )
            return [
                {
                    **dict(row),
                    "event_type": "CONTRACT_PUBLISHED",
                    "activated": bool(row["activated"]),
                }
                for row in rows
            ]

    def verify_artifact(self, relative_path: str, sha256: str) -> None:
        """Verify an existing private artifact by digest without modifying it."""
        verify_artifact(self._project_root, relative_path, sha256)

    def backup(self) -> Path:
        """Create a consistent private backup without replacing an existing copy."""
        backup_path = self.database_path.with_name("state.backup.db")
        with self._connection() as source:
            self._copy_backup(source, backup_path, version=_SCHEMA_VERSION)
        return backup_path

    @staticmethod
    def _copy_backup(source: sqlite3.Connection, backup_path: Path, *, version: int) -> None:
        """Publish a verified SQLite backup without replacing another file."""
        try:
            _check_database_files(backup_path, allow_existing=False)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="state.backup-", suffix=".db", dir=backup_path.parent
            )
            temporary = Path(temporary_name)
            try:
                os.close(descriptor)
                deadline = monotonic() + _LOCK_TIMEOUT
                best_remaining = None

                def check_progress(status: int, remaining: int, _total: int) -> None:
                    nonlocal deadline, best_remaining
                    if status == sqlite3.SQLITE_DONE:
                        return
                    if status == sqlite3.SQLITE_OK and (
                        best_remaining is None or remaining < best_remaining
                    ):
                        best_remaining = remaining
                        deadline = monotonic() + _LOCK_TIMEOUT
                    elif monotonic() >= deadline:
                        raise ConflictError("The state database remained busy during backup.")

                uri = f"{temporary.as_uri()}?mode=rw"
                with closing(
                    sqlite3.connect(uri, uri=True, timeout=_LOCK_TIMEOUT, autocommit=True)
                ) as destination:
                    destination.execute("PRAGMA foreign_keys = ON")
                    source.backup(destination, pages=128, progress=check_progress, sleep=0.05)
                    _verify_database(destination, version)
                    if version == 6:
                        _verify_migration_schema(destination, version)
                _check_database_files(backup_path, allow_existing=False)
                try:
                    # Both operations fail when the destination already exists.
                    if os.name == "nt":
                        os.rename(temporary, backup_path)
                    else:
                        os.link(temporary, backup_path)
                except FileExistsError:
                    raise ConflictError("A state backup already exists.") from None
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            raise StoreError("The state database backup could not be created safely.") from None

# SPDX-License-Identifier: MPL-2.0
"""Retain retired upgrade cases and verify current populated backup recovery."""

import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_execution_storage import (
    _EXECUTE_V4,
    _SETUP,
    _TABLES_V4,
    _historical_state,
    _records,
    _version,
)

from corinthos.contracts import ConflictError, ContractStore, StoreError
from corinthos.contracts import store as storage
from corinthos.contracts.migration import migrate_legacy_state
from corinthos.lifecycle.repository import LifecycleRepository
from corinthos.project_manager import ProjectManager
from corinthos.report.service import ReportService
from corinthos.workflow import WorkflowPlanner

_BASE_V5 = "32397d2bc0d02c8e77b5c6e09ca7e83f04995af0"
_TABLES_V5 = (*_TABLES_V4, "val_drafts", "val_plan_bindings", "val_runs", "val_events")


def test_real_v5_legacy_directory_copy_precedes_independent_v6_upgrade(populated_v5):
    private = populated_v5 / ".corinthos"
    before = _records(private / "state.db", _TABLES_V5)
    legacy = private.with_name(".nukytera")
    private.rename(legacy)
    original = (legacy / "state.db").read_bytes()
    assert migrate_legacy_state(populated_v5)
    assert (private / "state.db").read_bytes() == original
    assert _version(private / "state.db") == 5
    ContractStore(populated_v5)
    assert _version(private / "state.db") == 9
    assert _records(private / "state.db", _TABLES_V5) == before
    assert (legacy / "state.db").read_bytes() == original


_VALIDATE_REPORT_V5 = r"""
from corinthos.validation.planning import ValidationPlanner
from corinthos.validation.procedures import digest, file_hash
from corinthos.report.service import ReportService
from corinthos.contracts.model import canonical_json
import sys

project_ref = ContractRef.from_json(workflow['payload']['project_contract_ref'])
checkout = examples.parent.parent
validation = ValidationPlanner(store, checkout/'policies/standard.yaml', checkout/'schemas/standard.schema.json')
draft = validation.derive(project_ref, 'PRODUCT')
validation.repository.save_draft('migration-validation-draft', draft)
executable = str(Path(sys.executable).resolve(strict=True))
bindings, reviews = {}, {}
for row in draft['obligations']:
    binding = {
        'method': next(iter(row['verification_methods']), 'TEST'),
        'required_evidence': row['required_evidence'],
        'description': 'Synthetic migration fixture method; no product validation claim.',
        'review_paths': ['README.md'] if row['review_required'] else [],
        'command': {'argv':[executable,'-I','-c','print("FIXTURE_PASS")'], 'timeout':15,
                    'inputs':{executable:file_hash(executable)},
                    'success_line':'FIXTURE_PASS','failure_line':'FIXTURE_FAIL'},
    }
    bindings[row['check_id']] = binding
    reviews[row['check_id']] = {
        'actor_id':'synthetic-migration-reviewer',
        'criterion_sha256':digest({'source_ref':row['source_ref'],'criterion':row['criterion']}),
        'binding_sha256':digest(binding),
        'rationale':'Synthetic storage fixture adequacy, never a production review.',
    }
plan = validation.publish(draft, bindings, reviews, actor_id='synthetic-migration-validator')
run = validation.repository.start(plan, {'artifact_identity':{'snapshot_id':'migration-candidate'}},
                                  evaluator='synthetic-migration-evaluator')
reports = ReportService(store)
report = reports.generate(project_ref, artifact_identity={'snapshot_id':'migration-candidate'})
reports.publish(report['draft_id'])
pending = reports.generate(project_ref, kind='HISTORY', artifact_identity={'snapshot_id':'migration-candidate'})
(root/'historical-report-draft.txt').write_text(pending['draft_id'], encoding='utf-8')
artifact = root/'.corinthos/artifacts/retained/bytes.bin'
artifact.parent.mkdir(parents=True)
artifact.write_bytes(b'\x00Actual historical v5 retained artifact\xff')
"""


@pytest.fixture
def populated_v5(tmp_path):
    return _historical_state(tmp_path, _BASE_V5, 5, _EXECUTE_V4 + _VALIDATE_REPORT_V5)


@pytest.fixture
def populated_current(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    examples = Path(__file__).resolve().parents[1] / "examples" / "contracts"
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", [__file__, str(examples)])
    exec(_SETUP + _EXECUTE_V4 + _VALIDATE_REPORT_V5, {})
    return project


def test_v5_frozen_report_recovers_after_migration_without_relabelling_sources(populated_v5):
    reports = ReportService(ContractStore(populated_v5))
    draft = (populated_v5 / "historical-report-draft.txt").read_text("utf-8")
    package = reports.repository.get(draft)
    assert "lc_records" not in package["manifest"]["source_manifest"]["source_watermark"]
    reference = reports.recover(draft)
    assert reports.get(reference) == package["document"]
    assert reports.repository.get(draft)["manifest"] == package["manifest"]


def test_v5_frozen_report_rejects_new_lifecycle_sources_after_migration(populated_v5):
    store = ContractStore(populated_v5)
    LifecycleRepository(store).create("OPERATION", "demo-project", {}, state="COMPLETED")
    draft = (populated_v5 / "historical-report-draft.txt").read_text("utf-8")
    with pytest.raises(ConflictError, match="sources changed"):
        ReportService(store).recover(draft)


def _artifacts(project):
    base = project / ".corinthos/artifacts"
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file()
    }


def test_populated_v5_upgrade_preserves_all_twenty_tables_and_report_artifacts(populated_v5):
    database = populated_v5 / ".corinthos/state.db"
    before, artifacts = _records(database, _TABLES_V5), _artifacts(populated_v5)
    assert len(before) == 20 and all(before.values()) and artifacts
    store = ContractStore(populated_v5)
    assert _version(database) == 9 and _records(database, _TABLES_V5) == before
    assert _artifacts(populated_v5) == artifacts
    (backup,) = database.parent.glob("state.pre-v6-*.db")
    assert _version(backup) == 5 and _records(backup, _TABLES_V5) == before
    assert ProjectManager(populated_v5).list()[0]["state"] == "PUBLISHED"
    assert WorkflowPlanner(populated_v5).list()[0]["state"] == "PUBLISHED"
    assert ReportService(store).list("demo-project")
    with store._connection() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in ("lc_records", "lc_projects", "lc_events")
        )
    ContractStore(populated_v5)
    assert list(database.parent.glob("state.pre-v6-*.db")) == [backup]


def test_failed_v6_upgrade_rolls_back_and_reopens_actual_v5(populated_v5, monkeypatch):
    database = populated_v5 / ".corinthos/state.db"
    before, artifacts = _records(database, _TABLES_V5), _artifacts(populated_v5)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_MIGRATION_V6", (*storage._MIGRATION_V6, "INVALID SQL"))
        with pytest.raises(StoreError):
            ContractStore(populated_v5)
    assert _version(database) == 5 and _records(database, _TABLES_V5) == before
    assert _artifacts(populated_v5) == artifacts
    with sqlite3.connect(database) as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'lc_%'"
        ).fetchall()
    (backup,) = database.parent.glob("state.pre-v6-*.db")
    assert _version(backup) == 5 and _records(backup, _TABLES_V5) == before
    assert ContractStore(populated_v5).list_events()
    assert _version(database) == 9 and _records(database, _TABLES_V5) == before


def test_concurrent_v6_openers_preserve_history_and_one_inspected_backup(populated_v5):
    database = populated_v5 / ".corinthos/state.db"
    before, artifacts = _records(database, _TABLES_V5), _artifacts(populated_v5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(ContractStore, (populated_v5, populated_v5)))
    assert len(stores) == 2 and _version(database) == 9
    assert _records(database, _TABLES_V5) == before and _artifacts(populated_v5) == artifacts
    assert len(list(database.parent.glob("state.pre-v6-*.db"))) == 1


def test_populated_current_backup_restores_history_and_artifacts_in_another_root(populated_current):
    store = ContractStore(populated_current)
    repository = LifecycleRepository(store)
    record = repository.create(
        "OPERATION",
        "demo-project",
        {"operation": "SYNTHETIC_BACKUP_OBSERVATION"},
        state="COMPLETED",
    )
    repository.project("demo-project", create=True)
    tables = (*_TABLES_V5, "lc_records", "lc_projects", "lc_events")
    before = _records(store.database_path, tables)
    assert all(
        before[table]
        for table in (
            "contracts",
            "audit_events",
            "pm_turns",
            "wf_turns",
            "wf_execution_events",
            "val_events",
        )
    )
    artifacts = _artifacts(populated_current)
    assert artifacts
    backup = store.backup()
    assert _version(backup) == 9 and _records(backup, tables) == before
    recovered_root = populated_current.parent / "separate-recovery"
    private = recovered_root / ".corinthos"
    private.mkdir(parents=True, mode=0o700)
    shutil.copyfile(backup, private / "state.db")
    shutil.copytree(store.database_path.parent / "artifacts", private / "artifacts")
    recovered = ContractStore(recovered_root)
    assert _records(recovered.database_path, tables) == before
    assert _artifacts(recovered_root) == artifacts
    assert LifecycleRepository(recovered).get("OPERATION", record["record_id"]) == record
    assert ReportService(recovered).list("demo-project")
    assert _records(store.database_path, tables) == before

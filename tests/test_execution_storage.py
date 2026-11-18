# SPDX-License-Identifier: MPL-2.0
"""Retain retired schema cases and shared seeds for current storage verification."""

import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from corinthos.contracts import ContractStore, StoreError
from corinthos.contracts import store as storage
from corinthos.project_manager import ProjectManager
from corinthos.workflow import WorkflowPlanner

_BASE = "eb7dabbe443657524c6cf79880c9584d25675b91"
_BASE_V4 = "22592bf8a2d8ad1605abf332a65c34138d35069c"
_TABLES = (
    "contracts",
    "revisions",
    "contract_references",
    "audit_events",
    "pm_sessions",
    "pm_turns",
    "pm_approvals",
    "wf_sessions",
    "wf_turns",
    "wf_approvals",
    "wf_mandates",
)
_TABLES_V4 = (
    *_TABLES,
    "wf_executions",
    "wf_checkpoints",
    "wf_attempts",
    "wf_workspace_leases",
    "wf_execution_events",
)
_CONTRACT_SETUP = """
# Synthetic trusted fixture authority, never production terminal consent.
import json
import sqlite3
from pathlib import Path
from corinthos.contracts import ContractRef, ContractStore, PublishContext, approval_request
from corinthos.project_manager import ProjectManager
from corinthos.project_manager.approval import _record_consent, _verifier, recover_publication
from corinthos.workflow import WorkflowPlanner
from corinthos.workflow.authorization import _record, _publish

root = Path.cwd()
store = ContractStore(root)
examples = Path(__import__('sys').argv[1])
for name in ('project', 'technology-decision', 'module', 'workflow', 'validation',
             'validation-result', 'evidence-report', 'project-revision-request'):
    document = json.loads((examples / (name + '.json')).read_text(encoding='utf-8'))
    proof = approval_request(document) if document['metadata']['approval'] else None
    metadata = document['metadata']['provenance']
    store.publish(document, context=PublishContext(metadata['engine'], metadata['actor_id'],
                                                 lambda request, proof=proof: request == proof))
"""
_SETUP = (
    _CONTRACT_SETUP
    + """
manager = ProjectManager(root)
s = manager.start('A synthetic migration project.', actor_id='fixture-owner')
for operation in (
    {'action':'set_scope','section':'included','items':['Offline greeting']},
    {'action':'upsert_requirement','description':'Render a greeting.',
     'acceptance_criteria':[{'criterion':'Ada produces Hello, Ada.'}]},
    {'action':'upsert_success_criterion','criterion':'A clean environment runs the greeting.'},
):
    s = manager.edit(s['session_id'], s['version'], operation, actor_id='fixture-owner')
s = manager.review(s['session_id'], s['version'], actor_id='fixture-owner')
s = _record_consent(manager, s, 'fixture-owner')
d = s['review']['document']
store.publish(d, context=PublishContext('PROJECT_MANAGER','fixture-owner',_verifier(manager,s)))
recover_publication(manager,s)
planner = WorkflowPlanner(root)
s = planner.start(ContractRef.from_document(d), actor_id='fixture-owner')
requirement = d['payload']['requirements'][0]['requirement_id']
checkpoint = json.loads((examples/'workflow.json').read_text(encoding='utf-8'))['payload']['checkpoints'][0]
checkpoint['module_ids'] = ['migration-greeting']
checkpoint['requirement_ids'] = [requirement]
for operation in (
    {'action':'upsert_module','module_id':'migration-greeting',
     'purpose_and_boundaries':{'purpose':'Render the greeting.', 'responsibilities':['Greeting'],
                               'exclusions':['Network access']},
     'requirement_ids':[requirement], 'constraint_ids':[]},
    {'action':'upsert_checkpoint','checkpoint':checkpoint},
):
    s = planner.edit(s['session_id'], s['version'], operation, actor_id='fixture-owner')
s = planner.review(s['session_id'], s['version'], actor_id='fixture-owner')
s = _record(planner,s,'fixture-owner',delegate=True)
_publish(planner,s,'fixture-owner')
"""
)
_EXECUTE_V4 = """
from corinthos.workflow.execution_repository import ExecutionRepository
workflow = json.loads((examples/'workflow.json').read_text(encoding='utf-8'))
checkpoint = workflow['payload']['checkpoints'][0]['checkpoint_id']
reference = ContractRef.from_document(workflow)
repository = ExecutionRepository(store)
execution = repository.register(
    reference, {'workspace':str(root), 'checkpoints':{checkpoint:{'checks':['Synthetic fixture']}}},
    actor_id='fixture-owner',
)
execution = repository.prepare(
    execution['execution_id'], checkpoint, execution['version'], actor_id='fixture-owner',
)
attempt = repository.claim(
    execution['execution_id'], checkpoint, execution['version'], owner_id='fixture-owner',
    provider='codex', context={'source_snapshot':{'snapshot_id':'synthetic-fixture'}},
)
repository.transition(
    attempt['attempt_id'], attempt['fence'], 'INTERRUPTED', owner_id='fixture-owner',
    details={'cleanup_confirmed': True, 'reason': 'Synthetic historical interruption.'},
)
"""


def _records(database, tables=_TABLES):
    with closing(sqlite3.connect(database)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def _version(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def _historical_state(tmp_path, commit, version, extra_setup="", *, base_setup=_SETUP):
    pytest.skip("Archived-code compatibility fixtures are retired from routine verification.")


@pytest.fixture
def phase3_state(tmp_path):
    return _historical_state(tmp_path, _BASE, 3)


def test_actual_v3_upgrade_preserves_every_old_row_and_verified_backup(phase3_state):
    database = phase3_state / ".corinthos/state.db"
    before = _records(database)
    assert all(before[table] for table in _TABLES)
    store = ContractStore(phase3_state)
    assert _version(database) == 9 and _records(database) == before
    (backup,) = database.parent.glob("state.pre-v4-*.db")
    assert _version(backup) == 3 and _records(backup) == before
    assert ProjectManager(phase3_state).list()[0]["state"] == "PUBLISHED"
    assert WorkflowPlanner(phase3_state).list()[0]["state"] == "PUBLISHED"
    with store._connection() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM wf_executions").fetchone()[0] == 0
    ContractStore(phase3_state)
    assert list(database.parent.glob("state.pre-v4-*.db")) == [backup]


def test_failed_v4_upgrade_rolls_back_schema_and_retains_old_backup(phase3_state, monkeypatch):
    database = phase3_state / ".corinthos/state.db"
    before = _records(database)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_MIGRATION_V4", (*storage._MIGRATION_V4, "INVALID SQL"))
        with pytest.raises(StoreError):
            ContractStore(phase3_state)
    assert _version(database) == 3 and _records(database) == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name='wf_executions'"
            ).fetchone()
            is None
        )
    assert _records(next(database.parent.glob("state.pre-v4-*.db"))) == before
    assert ContractStore(phase3_state).list_events()


def test_concurrent_v4_openers_create_one_verified_backup(phase3_state):
    database = phase3_state / ".corinthos/state.db"
    before = _records(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(ContractStore, (phase3_state, phase3_state)))
    assert len(stores) == 2 and _version(database) == 9 and _records(database) == before
    assert len(list(database.parent.glob("state.pre-v4-*.db"))) == 1


def test_read_only_v3_is_preserved_without_upgrade(phase3_state):
    database = phase3_state / ".corinthos/state.db"
    before = database.read_bytes()
    original_mode = database.stat().st_mode
    database.chmod(stat.S_IREAD)
    try:
        with pytest.raises(StoreError, match="read-only"):
            ContractStore(phase3_state)
        assert database.read_bytes() == before
        assert not list(database.parent.glob("state.pre-v4-*.db"))
    finally:
        database.chmod(original_mode)


def test_populated_v4_rollback_and_upgrade_preserve_every_previous_record(tmp_path, monkeypatch):
    project = _historical_state(tmp_path, _BASE_V4, 4, _EXECUTE_V4)
    database = project / ".corinthos/state.db"
    before = _records(database, _TABLES_V4)
    assert all(before.values())

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_MIGRATION_V5", (*storage._MIGRATION_V5, "INVALID SQL"))
        with pytest.raises(StoreError):
            ContractStore(project)
    (backup,) = database.parent.glob("state.pre-v5-*.db")
    assert _version(database) == _version(backup) == 4
    assert _records(database, _TABLES_V4) == _records(backup, _TABLES_V4) == before
    with sqlite3.connect(database) as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE 'val_%'"
        ).fetchall()

    ContractStore(project)
    assert _version(database) == 9 and _records(database, _TABLES_V4) == before
    backups = list(database.parent.glob("state.pre-v5-*.db"))
    assert len(backups) == 2
    assert all(_version(item) == 4 and _records(item, _TABLES_V4) == before for item in backups)
    ContractStore(project)
    assert list(database.parent.glob("state.pre-v5-*.db")) == backups

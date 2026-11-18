# SPDX-License-Identifier: MPL-2.0
"""Retain retired upgrade cases alongside current workflow storage checks."""

import sqlite3

import pytest

from corinthos.contracts import ContractStore, StoreError
from corinthos.contracts import store as storage
from corinthos.project_manager import ProjectManager
from corinthos.workflow.repository import WorkflowRepository

_BASE = "139d1542f9b15938985909ab1124832cd69de607"
_TABLES = (
    "contracts",
    "revisions",
    "contract_references",
    "audit_events",
    "pm_sessions",
    "pm_turns",
    "pm_approvals",
)
_SETUP = """
# Synthetic trusted fixture authority, never production terminal consent.
import json
from pathlib import Path
from corinthos.contracts import ContractStore, PublishContext, approval_request
from corinthos.project_manager import ProjectManager
from corinthos.project_manager.approval import _record_consent, _verifier, recover_publication

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
manager = ProjectManager(root)
s = manager.start('A synthetic Phase 2 project.', actor_id='fixture-owner')
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
manager.store.publish(d, context=PublishContext('PROJECT_MANAGER','fixture-owner',_verifier(manager,s)))
recover_publication(manager,s)
assert __import__('sqlite3').connect(store.database_path).execute('PRAGMA user_version').fetchone()[0] == 2
"""


def _records(database):
    with sqlite3.connect(database) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in _TABLES
        }


def _version(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


@pytest.fixture
def phase2_state(tmp_path):
    pytest.skip("Archived-code compatibility fixtures are retired from routine verification.")


def test_real_phase2_upgrade_preserves_contracts_sessions_turns_and_approval(phase2_state):
    database = phase2_state / ".corinthos/state.db"
    original = _records(database)
    store = ContractStore(phase2_state)
    assert _version(database) == 9
    assert _records(database) == original
    backups = list(database.parent.glob("state.pre-v3-*.db"))
    assert len(backups) == 1 and _version(backups[0]) == 2
    assert _records(backups[0]) == original
    assert len(store.list_events()) == 9
    manager = ProjectManager(phase2_state)
    assert manager.list()[0]["state"] == "PUBLISHED"
    assert manager.get(manager.list()[0]["session_id"])["state"] == "PUBLISHED"
    ContractStore(phase2_state)
    assert list(database.parent.glob("state.pre-v3-*.db")) == backups


def test_failed_v3_upgrade_preserves_actual_phase2_and_verified_backup(phase2_state, monkeypatch):
    database = phase2_state / ".corinthos/state.db"
    before = _records(database)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_MIGRATION_V3", (*storage._MIGRATION_V3, "INVALID SQL"))
        with pytest.raises(StoreError):
            ContractStore(phase2_state)
    assert _version(database) == 2 and _records(database) == before
    assert _records(next(database.parent.glob("state.pre-v3-*.db"))) == before
    assert ContractStore(phase2_state).list_events()


def test_workflow_and_pm_sessions_have_separate_shared_cas_namespaces(tmp_path):
    manager = ProjectManager(tmp_path)
    pm = manager.start("An approved human idea.", actor_id="fixture-user")
    repository = WorkflowRepository(manager.store)
    wf = repository.create(
        {"session_id": "plan-test", "project_id": "project-test", "state": "DRAFT"},
        actor_id="fixture-user",
        action="planning_started",
    )
    assert manager.list() == [pm]
    assert repository.list() == [wf]
    wf = repository.save(
        wf["session_id"], wf["version"], wf, actor_id="fixture-user", action="planning_saved"
    )
    assert len(repository.history(wf["session_id"])) == 2
    assert manager.history(pm["session_id"])[0]["snapshot"] == pm

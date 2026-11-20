# SPDX-License-Identifier: MPL-2.0
import json
import os
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from corinthos.contracts import (
    AuthorityError,
    ContractStore,
    IntegrityError,
    PublishContext,
    SchemaError,
    approval_request,
)
from corinthos.contracts.artifacts import verify_artifact
from corinthos.contracts.model import canonical_json

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_approval_is_bound_to_exact_revision(tmp_path):
    original = json.loads((EXAMPLES / "contracts" / "project.json").read_text("utf-8"))
    provenance = original["metadata"]["provenance"]
    receipt = approval_request(original)
    context = PublishContext(provenance["engine"], provenance["actor_id"], {receipt}.__contains__)
    store = ContractStore(tmp_path)
    first = store.publish(original, context=context)
    changed = deepcopy(original)
    changed["metadata"].update(revision_id="unapproved-r2", version=2, supersedes=first.to_json())
    changed["payload"]["vision"] = "An unapproved project vision."
    with pytest.raises(AuthorityError):
        store.publish(changed, context=context)
    assert store.resolve_active(first.contract_id) == original
    assert len(store.list_events()) == 1


def test_fixture_demo_from_another_directory(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(EXAMPLES / "contract_kernel.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert "Published 8 contract types and reopened 9 revisions." in completed.stdout
    assert "rejected an invalid publication" in completed.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("value", [{1: "not a string key"}, float("nan"), b"not JSON"])
def test_canonical_json_rejects_non_json_values(value):
    with pytest.raises(SchemaError):
        canonical_json(value)


def test_artifact_digest_is_checked_against_the_file(tmp_path):
    artifacts = tmp_path / ".corinthos" / "artifacts"
    artifacts.mkdir(parents=True)
    path = artifacts / "result.txt"
    path.write_bytes(b"observed output")
    digest = sha256(b"observed output").hexdigest()
    verify_artifact(tmp_path, "result.txt", digest)
    path.write_bytes(b"modified output")
    with pytest.raises(IntegrityError, match="digest"):
        verify_artifact(tmp_path, "result.txt", digest)


@pytest.mark.parametrize(
    "name", ["../escape.txt", "C:/escape.txt", "x\\escape.txt", "CON", "x:log"]
)
def test_artifact_paths_reject_escapes_and_devices(tmp_path, name):
    with pytest.raises(IntegrityError, match="path or digest"):
        verify_artifact(tmp_path, name, "a" * 64)
    assert not (tmp_path / ".corinthos").exists()


def test_artifact_cannot_be_a_hard_link(tmp_path):
    artifacts = tmp_path / ".corinthos" / "artifacts"
    artifacts.mkdir(parents=True)
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"private")
    os.link(outside, artifacts / "linked.txt")
    with pytest.raises(IntegrityError, match="unlinked regular file"):
        verify_artifact(tmp_path, "linked.txt", sha256(b"private").hexdigest())

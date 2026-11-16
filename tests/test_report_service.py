# SPDX-License-Identifier: MPL-2.0
"""Publication/recovery over real execution with explicitly synthetic approval fixtures."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from test_validation_runtime import _review, audit  # noqa: F401

from corinthos.cli import main
from corinthos.contracts import (
    AuthorityError,
    ConflictError,
    ContractRef,
    IntegrityError,
    PublishContext,
    StoreError,
)
from corinthos.contracts.model import canonical_json
from corinthos.report import repository as report_repository
from corinthos.report import service as report_service
from corinthos.report import sources as report_sources
from corinthos.report.repository import content_digest
from corinthos.report.service import ReportService


@pytest.fixture
def report(request):
    runner, plan, candidate = request.getfixturevalue("audit")
    result = runner.run(plan, candidate, evaluator="synthetic-report-evaluator", reviewer=_review)
    project = next(
        raw
        for raw in runner.store.get(plan)["payload"]["source_contract_refs"]
        if raw["contract_type"] == "PROJECT"
    )
    return (
        ReportService(runner.store),
        ContractRef.from_json(project),
        candidate,
        runner,
        plan,
        result,
    )


def test_publish_restart_history_and_unchanged_capture_reuse(report):
    service, project, candidate, *_ = report
    draft = service.generate(project, candidate=candidate)
    ref = service.publish(draft["draft_id"])
    reopened = ReportService(service.store)
    assert reopened.recover(draft["draft_id"]) == ref
    document = reopened.get(ref)
    assert (
        document["payload"]["version_and_delivery_lineage"]["technical_validation"] == "VALIDATED"
    )
    assert document["payload"]["version_and_delivery_lineage"]["acceptance"] == "NOT_REVIEWED"
    assert reopened.latest(ref.contract_id) == document
    assert reopened.list(project.project_id)[0]["report_ref"] == ref.to_json()
    same = reopened.generate(project, candidate=candidate)
    assert same["draft_id"] == draft["draft_id"]
    assert len(reopened.history(ref.contract_id)) == 1
    assert reopened.verify(ref)["complete"]
    assert reopened.store.resolve_active(ref.contract_id) is None


def test_new_result_invalidates_frozen_current_capture(report):
    service, project, candidate, runner, plan, *_ = report
    draft = service.generate(project, candidate=candidate)
    runner.run(plan, candidate, evaluator="synthetic-report-evaluator", reviewer=_review)
    with pytest.raises(ConflictError, match="sources changed"):
        service.publish(draft["draft_id"])
    assert service.store.get(ContractRef.from_json(draft["report_ref"])) is None


def test_crash_after_kernel_commit_recovers_without_revalidating_old_files(report, monkeypatch):
    service, project, candidate, *_ = report
    draft = service.generate(project, candidate=candidate)
    expected = ContractRef.from_json(draft["report_ref"])

    def crash(*_):
        raise RuntimeError("Synthetic interruption after the Kernel commit")

    with monkeypatch.context() as patch:
        patch.setattr(service.repository, "complete", crash)
        with pytest.raises(RuntimeError, match="Kernel commit"):
            service.publish(draft["draft_id"])
    published = service.get(expected)
    evidence = published["payload"]["artifact_and_evidence_index"][0]
    source = service.store.database_path.parent / "artifacts" / evidence["path"]
    source.write_bytes(b"synthetically changed after publication")
    assert service.recover(draft["draft_id"]) == expected
    assert service.get(expected) == published
    assert not service.verify(expected)["complete"]
    assert len(service.history(expected.contract_id)) == 1


def test_damaged_proof_is_a_new_unverified_report_without_fabricated_blocker(report):
    service, project, candidate, *_ = report
    first = service.generate(project, candidate=candidate)
    before = service.publish(first["draft_id"])
    original = service.get(before)
    evidence = original["payload"]["artifact_and_evidence_index"][0]
    path = service.store.database_path.parent / "artifacts" / evidence["path"]
    path.write_bytes(b"missing non-indexed proof")
    next_draft = service.generate(project, candidate=candidate)
    next_ref = service.publish(next_draft["draft_id"])
    current = service.get(next_ref)
    assert (
        current["payload"]["version_and_delivery_lineage"]["technical_validation"] == "NOT_VERIFIED"
    )
    assert current["payload"]["execution_and_validation_digest"]["blocked_check_ids"] == []
    assert evidence not in current["payload"]["artifact_and_evidence_index"]
    assert current["metadata"]["supersedes"] == before.to_json()
    assert service.get(before) == original
    assert service.compare(before, next_ref)["changes"]
    assert service.generate(project, candidate=candidate)["draft_id"] == next_draft["draft_id"]


def test_concurrent_report_successors_cannot_replace_each_other(report):
    service, project, candidate, *_ = report
    first = service.generate(project, candidate=candidate)
    previous = service.publish(first["draft_id"])
    drafts = [
        service.generate(project, candidate=candidate, previous=previous, reason=reason)
        for reason in ("Synthetic explanation A", "Synthetic explanation B")
    ]

    def publish(draft):
        try:
            return service.publish(draft["draft_id"])
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, drafts))
    assert sum(item is not None for item in results) == 1
    assert len(service.history(previous.contract_id)) == 2


def test_incomplete_capture_does_not_block_a_fresh_capture(report, monkeypatch):
    service, project, candidate, *_ = report
    original_replace = report_repository.os.replace

    def fail(source, destination):
        if destination.name == "frozen.json":
            raise OSError("Synthetic frozen-file promotion failure")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(report_repository.os, "replace", fail)
        with pytest.raises(StoreError, match="artifact write"):
            service.generate(project, candidate=candidate)
    assert service.drafts()[0]["state"] == "INCOMPLETE"
    fresh = service.generate(project, candidate=candidate)
    assert service.publish(fresh["draft_id"])


def test_frozen_changes_and_synthetic_human_acceptance_cannot_publish(report):
    service, project, candidate, *_ = report
    draft = service.generate(project, candidate=candidate)
    package = service.repository.get(draft["draft_id"])
    forged = deepcopy(package["document"])
    forged["payload"]["version_and_delivery_lineage"].update(
        delivery="DELIVERED", acceptance="ACCEPTED"
    )
    forged["payload"]["feedback_and_acceptance"]["review_approval_id"] = "invented-review"
    forged["metadata"]["approval"] = {
        "kind": "HUMAN",
        "approval_id": "invented-review",
        "actor_id": "synthetic-owner",
    }
    manifest = deepcopy(package["manifest"])
    manifest["content_sha256"] = content_digest(forged["payload"])
    forged["metadata"]["provenance"]["source"] = "report-manifest-sha256:" + content_digest(
        manifest
    )
    saved = service.repository.freeze(forged, manifest)
    with pytest.raises(AuthorityError, match="approval verifier"):
        service.publish(saved["draft_id"])
    path = service.repository._directory(draft["draft_id"]) / "frozen.json"
    altered = json.loads(path.read_text("utf-8"))
    altered["document"]["payload"]["report_scope"]["audience"] = "altered frozen content"
    path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(IntegrityError, match="incomplete|digest"):
        service.publish(draft["draft_id"])


def test_rehashed_sidecar_cannot_replace_a_published_generation_manifest(report):
    service, project, candidate, *_ = report
    draft = service.generate(project, candidate=candidate)
    ref = service.publish(draft["draft_id"])
    original = service.get(ref)
    package = service.repository.get(draft["draft_id"])
    package["manifest"]["reason"] = "Synthetic forged private provenance"
    package["manifest_sha256"] = content_digest(package["manifest"])
    package["document"]["metadata"]["provenance"]["source"] = (
        "report-manifest-sha256:" + package["manifest_sha256"]
    )
    package["document_sha256"] = content_digest(package["document"])
    path = service.repository._directory(draft["draft_id"]) / "frozen.json"
    path.write_text(canonical_json(package), encoding="utf-8")
    with pytest.raises(IntegrityError, match="Kernel-published"):
        service.manifest(ref)
    with pytest.raises(IntegrityError, match="Kernel-published"):
        service.verify(ref)
    assert service.get(ref) == original


def test_bulk_preflight_does_not_hold_the_writer_reservation(report, monkeypatch):
    service, project, candidate, *_ = report
    draft = service.generate(project, candidate=candidate)
    original = report_service.require_unchanged
    observed = []

    def inspect(*args, **kwargs):
        guard = original(*args, **kwargs)
        with sqlite3.connect(service.store.database_path, timeout=0.1, autocommit=True) as probe:
            probe.execute("BEGIN IMMEDIATE")
            observed.append("Writer reservation remains available during preflight.")
            probe.execute("ROLLBACK")
        return guard

    monkeypatch.setattr(report_service, "require_unchanged", inspect)
    assert service.publish(draft["draft_id"])
    assert observed


@pytest.mark.parametrize("invalid_part", ["document", "manifest"])
def test_one_malformed_draft_does_not_poison_unrelated_report_lookup(
    report, invalid_part, monkeypatch
):
    service, project, candidate, *_ = report
    with service.store._connection() as connection:
        observed_at = [
            datetime.fromisoformat(row[0])
            for row in connection.execute("SELECT occurred_at FROM val_events")
        ]
    captured_at = max(observed_at) + timedelta(seconds=1)

    class CaptureClock(datetime):
        @classmethod
        def now(cls, tz):
            return captured_at.astimezone(tz)

    monkeypatch.setattr(report_sources, "datetime", CaptureClock)
    valid = service.generate(project, candidate=candidate)
    cutoff = datetime.fromisoformat(valid["document"]["payload"]["report_scope"]["as_of"])
    assert all(observation < cutoff for observation in observed_at)
    ref = service.publish(valid["draft_id"])
    invalid = service.generate(project, candidate=candidate, reason="Synthetic incomplete capture")
    package = service.repository.get(invalid["draft_id"])
    package[invalid_part] = {} if invalid_part == "document" else []
    path = service.repository._directory(invalid["draft_id"]) / "frozen.json"
    path.write_text(canonical_json(package), encoding="utf-8")
    drafts = service.drafts()
    assert any(item.get("state") == "INCOMPLETE" for item in drafts)
    assert service.manifest(ref)["report_ref"] == ref.to_json()
    assert service.generate(project, candidate=candidate)["draft_id"] == valid["draft_id"]


def test_comparison_cannot_expose_sensitive_legacy_canonical_prose(report, capsys):
    service, project, candidate, *_ = report
    draft = service.generate(project, candidate=candidate)
    ref = service.publish(draft["draft_id"])
    legacy = deepcopy(service.get(ref))
    legacy["metadata"].update(
        contract_id="synthetic-sensitive-report", revision_id="synthetic-sensitive-report-r1"
    )
    legacy["payload"]["risks_metrics_and_next_actions"]["risks"].append(
        "password=synthetic-secret-value"
    )
    service.store.publish(
        legacy, context=PublishContext("REPORT", legacy["metadata"]["provenance"]["actor_id"])
    )
    assert (
        main(
            [
                "report",
                "--root",
                str(service.store.database_path.parent.parent),
                "compare",
                ref.contract_id,
                "synthetic-sensitive-report",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "synthetic-secret-value" not in output.out + output.err

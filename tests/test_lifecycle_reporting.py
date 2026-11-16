# SPDX-License-Identifier: MPL-2.0
"""Real sealed bytes and audits; contract authority/review fixtures are synthetic."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_build_lifecycle import _plan, _ready, _review
from test_build_lifecycle import built as built
from test_version_lifecycle import build_fixture as build_fixture
from test_version_lifecycle import delivered as delivered

from corinthos.contracts import ConflictError, IntegrityError
from corinthos.lifecycle import playtest as playtest_service
from corinthos.lifecycle import repository as lifecycle_repository
from corinthos.lifecycle.playtest import PlaytestService
from corinthos.report import sources as report_sources
from corinthos.report.service import ReportService
from corinthos.report.sources import capture, require_unchanged

_INSPECTED = {
    "process_stopped": True,
    "effects_inspected": True,
    "summary": "Synthetic fixture inspected stopped logical session and isolated data.",
}


def _promotable(builds, build, planner, refs):
    _ready(builds, build)
    plan = _plan(planner, refs)
    builds.validate(
        build["record_id"], plan, evaluator="synthetic-report-playtest-evaluator", reviewer=_review
    )
    return plan


def test_report_uses_genuine_delivered_build_and_keeps_acceptance_separate(delivered):
    _, version, builds, build, _, refs, *_ = delivered
    reports = ReportService(builds.store)
    draft = reports.generate(
        refs["project"],
        candidate=build["origin"]["candidate_root"],
        artifact_identity=build["origin"]["artifact_identity"],
    )
    lineage = draft["document"]["payload"]["version_and_delivery_lineage"]
    assert lineage["product_version"] == version["origin"]["product_version"]
    assert lineage["delivery"] == "DELIVERED"
    assert lineage["acceptance"] == "NOT_REVIEWED"
    assert lineage["build_id"] == build["record_id"]
    ref = reports.publish(draft["draft_id"])
    assert reports.verify(ref)["complete"]
    assert reports.get(ref)["payload"]["version_and_delivery_lineage"] == lineage


def test_history_cutoff_preserves_tracker_session_feedback_despite_new_transitions(built):
    builds, first, planner, refs, execution, _ = built
    plan = _promotable(builds, first, planner, refs)
    playtests = PlaytestService(builds.store)
    project_id = refs["project"].project_id
    playtests.configure(project_id, enabled=True)
    playtests.promote(project_id, first["record_id"])
    session = playtests.start(project_id)
    feedback = playtests.feedback(
        project_id, first["record_id"], "Original observation", session_id=session["record_id"]
    )
    cutoff = datetime.now(UTC).isoformat()
    before = capture(
        builds.store,
        refs["project"],
        kind="HISTORY",
        as_of=cutoff,
        artifact_identity=first["origin"]["artifact_identity"],
    )
    old_project = before["lifecycle"]["project"]
    old_records = {row["record_id"]: row for row in before["lifecycle"]["records"]}
    assert old_records[session["record_id"]]["state"] == "OPEN"
    assert old_records[feedback["record_id"]]["state"] == "OPEN"

    playtests.end(session["record_id"], inspection=_INSPECTED)
    playtests.triage(feedback["record_id"], category="BUG", reason="Explicit Workflow follow-up.")
    # Same approved milestone repair, distinct retained bytes/ID and fresh exact proof.
    second = builds.prepare(refs["workflow"], "early", execution["execution_id"], builds.root)
    _ready(builds, second)
    builds.validate(
        second["record_id"],
        plan,
        evaluator="synthetic-report-successor-evaluator",
        reviewer=_review,
    )
    playtests.promote(project_id, second["record_id"])
    after = capture(
        builds.store,
        refs["project"],
        kind="HISTORY",
        as_of=cutoff,
        artifact_identity=first["origin"]["artifact_identity"],
    )
    assert after["lifecycle"] == before["lifecycle"]
    assert after["watermark"] == before["watermark"]
    assert after["lifecycle"]["project"] == old_project
    now = playtests.status(project_id)
    assert now["project"]["active_build_id"] == second["record_id"]
    assert now["project"]["tracker_id"] == old_project["tracker_id"]
    retained = playtests.repository.get("FEEDBACK", feedback["record_id"])
    assert retained["origin"] == feedback["origin"]
    assert retained["state"] == "TRIAGED"


def test_current_frozen_report_rejects_later_feedback_treatment(built):
    builds, build, planner, refs, *_ = built
    _promotable(builds, build, planner, refs)
    playtests = PlaytestService(builds.store)
    project_id = refs["project"].project_id
    playtests.configure(project_id, enabled=True)
    playtests.promote(project_id, build["record_id"])
    feedback = playtests.feedback(project_id, build["record_id"], "Report-bound observation")
    reports = ReportService(builds.store)
    draft = reports.generate(
        refs["project"],
        candidate=build["origin"]["candidate_root"],
        artifact_identity=build["origin"]["artifact_identity"],
    )
    playtests.triage(
        feedback["record_id"], category="REQUIREMENT", reason="Owner negotiation needed."
    )
    with pytest.raises(ConflictError, match="sources changed"):
        reports.publish(draft["draft_id"])
    snapshot = capture(
        builds.store,
        refs["project"],
        kind="PRODUCT",
        candidate_root=build["origin"]["candidate_root"],
        artifact_identity=build["origin"]["artifact_identity"],
    )
    with builds.store._transaction() as connection:
        require_unchanged(builds.store, connection, snapshot["manifest"])


def test_report_rejects_spoofed_build_identity_and_changed_retained_bytes(delivered):
    _, _, builds, build, _, refs, *_ = delivered
    reports = ReportService(builds.store)
    identity = deepcopy(build["origin"]["artifact_identity"])
    identity["artifact_id"] = "manifest-" + "f" * 64
    with pytest.raises(IntegrityError, match="identity"):
        reports.generate(
            refs["project"], candidate=build["origin"]["candidate_root"], artifact_identity=identity
        )
    (Path(build["origin"]["candidate_root"]) / "README.md").write_text(
        "Tampered retained build", encoding="utf-8"
    )
    with pytest.raises(IntegrityError, match="bytes changed"):
        reports.generate(
            refs["project"],
            candidate=build["origin"]["candidate_root"],
            artifact_identity=build["origin"]["artifact_identity"],
        )


def test_feedback_public_projection_redacts_private_text_keeps_original_private(built):
    builds, build, planner, refs, *_ = built
    _promotable(builds, build, planner, refs)
    playtests = PlaytestService(builds.store)
    project_id = refs["project"].project_id
    playtests.configure(project_id, enabled=True)
    playtests.promote(project_id, build["record_id"])
    body = "Observation\x1b[31m api_key=privatevalue https://notion.so/private-plan"
    feedback = playtests.feedback(project_id, build["record_id"], body)
    reports = ReportService(builds.store)
    draft = reports.generate(
        refs["project"],
        candidate=build["origin"]["candidate_root"],
        artifact_identity=build["origin"]["artifact_identity"],
    )
    text = str(draft["document"])
    assert "privatevalue" not in text and "https://notion.so/" not in text and "\x1b" not in text
    assert playtests.repository.get("FEEDBACK", feedback["record_id"])["origin"]["body"] == body
    assert (
        draft["document"]["payload"]["feedback_and_acceptance"]["feedback"][0]["build_id"]
        == build["record_id"]
    )


def test_public_reports_show_separate_readiness_scope_and_original_session_build(
    built, monkeypatch
):
    builds, build, planner, refs, *_ = built
    _promotable(builds, build, planner, refs)
    playtests = PlaytestService(builds.store)
    project_id = refs["project"].project_id
    playtests.configure(project_id, enabled=True)
    playtests.promote(project_id, build["record_id"])
    recorded_at = datetime.fromisoformat(
        playtests.repository.events(project_id)[-1]["occurred_at"]
    ) + timedelta(seconds=1)
    monkeypatch.setattr(lifecycle_repository, "now", lambda: recorded_at.isoformat())
    monkeypatch.setattr(playtest_service, "now", lambda: recorded_at.isoformat())
    session = playtests.start(project_id)
    cutoff = (recorded_at + timedelta(seconds=1)).isoformat()
    recorded_at += timedelta(seconds=2)
    playtests.end(session["record_id"], inspection=_INSPECTED)
    captured_at = recorded_at + timedelta(seconds=1)

    class CaptureClock(datetime):
        @classmethod
        def now(cls, tz):
            return captured_at.astimezone(tz)

    monkeypatch.setattr(report_sources, "datetime", CaptureClock)
    events = playtests.repository.events(project_id, record_id=session["record_id"])
    opened = next(event for event in events if event["kind"] == "PLAYTEST_SESSION_OPENED")
    ended = next(event for event in events if event["kind"] == "PLAYTEST_SESSION_ENDED")
    assert (
        datetime.fromisoformat(opened["occurred_at"])
        < datetime.fromisoformat(cutoff)
        < datetime.fromisoformat(ended["occurred_at"])
        <= captured_at
    )
    reports = ReportService(builds.store)

    def notes(as_of=None):
        draft = reports.generate(
            refs["project"],
            kind="HISTORY",
            as_of=as_of,
            candidate=build["origin"]["candidate_root"],
            artifact_identity=build["origin"]["artifact_identity"],
        )
        assert (
            draft["document"]["payload"]["version_and_delivery_lineage"]["acceptance"]
            == "NOT_REVIEWED"
        )
        return draft["document"]["payload"]["decisions_and_change_history"]["notes"]

    historical, at_end, current = notes(cutoff), notes(ended["occurred_at"]), notes()
    for public in (historical, at_end, current):
        readiness = next(note for note in public if note.startswith("Build " + build["record_id"]))
        assert "early" in readiness and "greet" in readiness
        assert refs["workflow"].revision_id in readiness
        assert "Workflow readiness SATISFIED" in readiness
        assert "separate from independent Validation" in readiness
        origin = next(note for note in public if note.startswith("Session " + session["record_id"]))
        assert build["record_id"] in origin and "milestone early" in origin
    assert any("current recorded state OPEN" in note for note in historical)
    assert not any("current recorded state ENDED" in note for note in historical)
    assert any("current recorded state ENDED" in note for note in at_end)
    assert any("current recorded state ENDED" in note for note in current)

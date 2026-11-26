# SPDX-License-Identifier: MPL-2.0
"""Real offline provider processes prove overlap, isolation and guarded integration."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from corinthos import continuity, runtime
from corinthos.errors import ConflictError, IntegrityError
from corinthos.workspace import group_paths, initialize, open_project


def test_provider_executable_is_pinned_before_cwd_changes(monkeypatch):
    from corinthos.providers import validate_command

    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    result = validate_command([Path(sys.executable).name, "-c", "pass"])
    assert Path(result[0]).is_absolute()
    assert Path(result[0]).resolve() == Path(sys.executable).resolve()


def test_clean_git_source_is_cloned_and_dirty_source_is_preserved(tmp_path):
    root = tmp_path / "project"
    initialize(root)
    repo = root / "repo"

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init")
    git("config", "user.name", "Disposable test")
    git("config", "user.email", "test@example.invalid")
    (repo / "source.txt").write_text("baseline")
    git("add", "source.txt")
    git("commit", "-m", "Disposable baseline")
    run = runtime.start(
        root,
        "implement-feature",
        command=command(
            "from pathlib import Path;assert Path('repo/source.txt').read_text()=='baseline';Path('repo/result.txt').write_text('isolated git result')"
        ),
        claims=["repo/result.txt"],
    )
    assert wait(root, run["run_id"])["status"] == "SUCCEEDED"
    assert (Path(run["workspace"]) / "repo/.git").is_dir()
    runtime.integrate(root, run["run_id"])
    with pytest.raises(ConflictError, match="preserve"):
        runtime.start(
            root, "implement-feature", command=command("pass"), claims=["repo/source.txt"]
        )
    assert (repo / "source.txt").read_text() == "baseline"


def wait(root, run_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = runtime.get(root, run_id)
        if value["status"] not in runtime.ACTIVE:
            return value
        time.sleep(0.05)
    runtime.cancel(root, run_id)
    pytest.fail("Controlled worker did not finish: " + json.dumps(value))


def command(code):
    return [sys.executable, "-c", code]


@pytest.mark.parametrize("code", ["raise SystemExit(7)", "print('x'*1100000)"])
def test_provider_failure_and_output_limit_are_not_success(tmp_path, code):
    root = tmp_path / "project"
    initialize(root)
    run = runtime.start(root, "implement-feature", command=command(code))
    result = wait(root, run["run_id"])
    assert result["status"] == "FAILED"
    if Path(result["output"]).exists():
        assert Path(result["output"]).stat().st_size <= 1024 * 1024
        assert len(result.get("output_tail", "")) <= 4096


@pytest.mark.parametrize("separate_projects", [False, True])
def test_two_real_workers_overlap_and_conflicting_claims_are_rejected(tmp_path, separate_projects):
    root = tmp_path / "project"
    initialize(root)
    second_root = tmp_path / "second" if separate_projects else root
    if separate_projects:
        initialize(second_root)
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    def worker(name):
        return command(
            "import sys,time;from pathlib import Path;sys.stdin.read();p=Path("
            + repr(str(barrier))
            + ");(p/"
            + repr(name)
            + ").write_text('ready');deadline=time.monotonic()+10\nwhile len(list(p.iterdir()))<2 and time.monotonic()<deadline: time.sleep(.02)\nassert len(list(p.iterdir()))==2\nPath('repo/"
            + name
            + ".txt').write_text('isolated')"
        )

    first = runtime.start(root, "implement-feature", command=worker("one"), claims=["repo/one.txt"])
    with pytest.raises(ConflictError, match="overlap"):
        runtime.start(root, "implement-feature", command=worker("bad"), claims=["repo"])
    second = runtime.start(
        second_root, "implement-feature", command=worker("two"), claims=["repo/two.txt"]
    )
    assert runtime.has_active()
    assert wait(root, first["run_id"])["status"] == "SUCCEEDED"
    assert wait(second_root, second["run_id"])["status"] == "SUCCEEDED"
    assert not (root / "repo/one.txt").exists()
    assert first["workspace"] != second["workspace"]
    runtime.integrate(root, first["run_id"])
    runtime.integrate(second_root, second["run_id"])
    assert (
        (root / "repo/one.txt").read_text()
        == (second_root / "repo/two.txt").read_text()
        == "isolated"
    )


def test_independent_projects_and_skill_review_correction_artifacts(tmp_path):
    roots = [tmp_path / "one", tmp_path / "two"]
    identities = [initialize(root) for root in roots]
    assert identities[0]["project_id"] != identities[1]["project_id"]
    for root in roots:
        group_paths(root, 1, create=True)
    for skill, artifact in [
        ("create-feature", "task-description.md"),
        ("implement-feature", "implement-report.md"),
        ("review-feature", "review-report.md"),
        ("implement-feature", "task-improvement.md"),
    ]:
        code = (
            "import sys;from pathlib import Path;p=sys.stdin.read();assert 'Skill: "
            + skill
            + "' in p;Path('task/task_1/"
            + artifact
            + "').write_text('"
            + skill
            + " result')"
        )
        run = runtime.start(
            roots[0], skill, group=1, command=command(code), claims=["task/task_1/" + artifact]
        )
        assert wait(roots[0], run["run_id"])["status"] == "SUCCEEDED"
        runtime.integrate(roots[0], run["run_id"])
        assert run["project_id"] == identities[0]["project_id"]
    assert (roots[1] / "task/task_1/review-report.md").read_text() == ""
    assert runtime.list_runs(roots[1]) == []


def test_outside_scope_and_stale_destination_remain_isolated(tmp_path):
    root = tmp_path / "project"
    initialize(root)
    run = runtime.start(
        root,
        "implement-feature",
        command=command("from pathlib import Path;Path('repo/other.txt').write_text('outside')"),
        claims=["repo/allowed.txt"],
    )
    assert wait(root, run["run_id"])["status"] == "SUCCEEDED"
    with pytest.raises(ConflictError, match="outside"):
        runtime.integrate(root, run["run_id"])
    assert not (root / "repo/other.txt").exists()
    run = runtime.start(
        root,
        "implement-feature",
        command=command("from pathlib import Path;Path('repo/allowed.txt').write_text('worker')"),
        claims=["repo/allowed.txt"],
    )
    assert wait(root, run["run_id"])["status"] == "SUCCEEDED"
    (root / "repo/allowed.txt").write_text("owner")
    with pytest.raises(ConflictError, match="Destination"):
        runtime.integrate(root, run["run_id"])
    assert (root / "repo/allowed.txt").read_text() == "owner"


def test_cancel_reaps_worker_and_discard_consolidates_first(tmp_path):
    root = tmp_path / "project"
    initialize(root)
    run = runtime.start(
        root,
        "implement-feature",
        command=command("import time;time.sleep(30)"),
        claims=["repo/result.txt"],
    )
    with pytest.raises(ConflictError):
        continuity.cleanup(root, "planning", summary="Do not remove active inputs")
    runtime.cancel(root, run["run_id"])
    assert wait(root, run["run_id"])["status"] == "CANCELLED"
    runtime.discard(
        root, run["run_id"], summary="Cancelled controlled worker; no output integrated."
    )
    assert runtime.list_runs(root) == []
    assert "Cancelled controlled" in continuity.show(root)["completed"]


def test_every_cleanup_boundary_survives_reopen_and_preserves_pending_decisions(tmp_path):
    root = tmp_path / "project"
    identity = initialize(root)
    task, auxiliary = group_paths(root, 1, create=True)
    for file in task.iterdir():
        file.write_text("temporary evidence")
    (auxiliary / "temporary.md").write_text("temporary")
    decisions = root / "context/DECISIONS.md"
    decisions.write_text("Unrelated pending decision")
    continuity.save(
        root,
        pending="Next feature",
        blockers="Provider not authenticated",
        verification="Offline check passed",
        resume="Use group 2",
    )
    with pytest.raises(IntegrityError):
        continuity.cleanup(root, "reports", group=1, summary="")
    assert (task / "implement-report.md").read_text() == "temporary evidence"
    continuity.cleanup(
        root, "reports", group=1, summary="Feature delivered; offline checks passed."
    )
    assert (task / "implement-report.md").read_text() == ""
    assert (task / "implementation-steps.md").read_text() == "temporary evidence"
    continuity.cleanup(root, "group", group=1, summary="Group complete; follow next feature.")
    assert not task.exists() and not auxiliary.exists()
    continuity.cleanup(
        root, "planning", summary="Documentation refreshed; pending decision retained."
    )
    assert decisions.read_text() == "Unrelated pending decision"
    assert open_project(root) == identity
    saved = continuity.show(root)
    assert saved["pending"] == "Next feature" and saved["resume"] == "Use group 2"
    assert "Feature delivered" in saved["completed"] and "Group complete" in saved["completed"]

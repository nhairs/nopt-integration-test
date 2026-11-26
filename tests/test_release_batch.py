"""Exercise batch allocation and verification without touching remote refs."""

import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tomllib
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "release_batch", Path(__file__).resolve().parents[1] / "scripts" / "release_batch.py"
)
assert spec is not None and spec.loader is not None
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)

EMPTY = {"testpypi": {"status": 404}, "github": []}


@pytest.mark.parametrize(
    ("main_version", "changelog", "refs", "published", "expected"),
    [
        ("10.8.1rc2", "", "", [], ("10.8.2", 11)),
        ("9.9.9", "## [10.0.0] - UNRELEASED", "", [], ("10.0.1", 11)),
        ("9.9.9", "", "abc\trefs/tags/v10.0.3.dev1", [], ("10.0.4", 11)),
        ("9.9.9", "", "abc\trefs/heads/release/10.x", [], ("9.9.10", 11)),
        ("9.9.9", "", "", ["10.2.0"], ("10.2.1", 11)),
    ],
)
def test_version_floors_follow_existing_versions(
    main_version, changelog, refs, published, expected
):
    assert batch.version_floors(main_version, changelog, refs, published) == expected


def test_allocate_version_skips_remote_tags():
    probe = Mock(return_value=EMPTY)
    assert batch.allocate_version("2.0.1", {"v2.0.1.dev0"}.__contains__, probe) == (
        "2.0.1.dev1",
        "v2.0.1.dev1",
        EMPTY,
    )
    probe.assert_called_once_with("2.0.1.dev1", "v2.0.1.dev1")


@pytest.mark.parametrize(
    "state",
    [
        {"testpypi": {"status": 200, "files": []}, "github": []},
        {"testpypi": {"status": 404}, "github": [{"id": 123, "draft": True}]},
        {"testpypi": {"status": 503}, "github": []},
    ],
)
def test_allocate_version_stops_for_unsafe_publication_state(state):
    probe = Mock(return_value=state)
    with pytest.raises(batch.BatchError, match="existing publication state"):
        batch.allocate_version("2.0.1", lambda _: False, probe)
    probe.assert_called_once_with("2.0.1.dev0", "v2.0.1.dev0")


def test_select_run_requires_all_identity_fields():
    scenario = {
        "tag": "v2.0.1.dev0",
        "commit": "a" * 40,
        "event": "workflow_dispatch",
        "triggered_at": "2026-11-26T00:00:00Z",
    }
    run = {
        "head_branch": scenario["tag"],
        "head_sha": scenario["commit"],
        "event": scenario["event"],
        "created_at": scenario["triggered_at"],
        "html_url": "https://github.com/example/runs/1",
    }
    others = [
        dict(run, head_branch="v2.0.1.dev1"),
        dict(run, head_sha="b" * 40),
        dict(run, event="push"),
        dict(run, created_at="2026-11-25T23:59:59Z"),
    ]
    assert batch.select_run(others, scenario) is None
    assert batch.select_run([*others, run], scenario) is run
    with pytest.raises(batch.BatchError, match="Ambiguous workflow runs"):
        batch.select_run([*others, run, dict(run, html_url=run["html_url"] + "2")], scenario)


def test_resume_records_rerun_without_replacing_original_attempt(monkeypatch):
    recorded = {"id": 1, "run_attempt": 1}
    scenario = {
        "tag": "v2.0.1",
        "commit": "a" * 40,
        "event": "push",
        "triggered_at": "2026-11-26T00:00:00Z",
        "run": recorded,
    }
    rerun = {
        "id": 1,
        "run_attempt": 2,
        "head_branch": scenario["tag"],
        "head_sha": scenario["commit"],
        "event": scenario["event"],
        "created_at": scenario["triggered_at"],
        "html_url": "https://github.com/example/runs/1",
        "status": "completed",
        "conclusion": "success",
    }
    monkeypatch.setattr(batch, "page_items", Mock(return_value=[rerun]))
    save = Mock()
    with pytest.raises(batch.BatchError, match="original workflow attempt"):
        batch.wait_for_run(scenario, 5, save)
    assert scenario["run"] is recorded
    assert scenario["unexpected_run_attempt"]["run_attempt"] == 2
    save.assert_called_once()


def test_success_requires_all_eight_jobs():
    jobs = [
        {"name": name, "conclusion": "success"}
        for name in (
            "preflight",
            "build",
            "publish",
            "github-release",
            "ci / lint",
            "ci / tests (ubuntu-latest)",
            "ci / tests (macos-latest)",
            "ci / tests (windows-latest)",
        )
    ]
    batch.expected_jobs("successful-dev", "success", jobs, None)
    with pytest.raises(batch.BatchError, match="Unexpected workflow conclusion or jobs"):
        batch.expected_jobs("successful-dev", "success", jobs[:-1], None)


def test_negative_case_checks_reason_and_skips():
    diagnostic = "expected exactly one changelog section for 2.0.1"
    jobs = [
        {"name": name, "conclusion": "skipped"}
        for name in (
            "ci",
            "build",
            "publish",
            "github-release",
        )
    ]
    preflight = {
        "name": "preflight",
        "conclusion": "failure",
        "failure_text": [f"TIMESTAMP ValueError: {diagnostic}"],
    }
    batch.expected_jobs("missing-changelog", "failure", [*jobs, preflight], diagnostic)
    with pytest.raises(batch.BatchError, match="Expected preflight diagnostic"):
        batch.expected_jobs(
            "missing-changelog",
            "failure",
            [
                *jobs,
                dict(preflight, failure_text=[f"TIMESTAMP raise ValueError('{diagnostic}')"]),
            ],
            diagnostic,
        )
    jobs[0]["conclusion"] = "success"
    with pytest.raises(batch.BatchError, match="Unexpected workflow conclusion or jobs"):
        batch.expected_jobs("missing-changelog", "failure", [*jobs, preflight], diagnostic)


def test_duplicate_checks_release_identity_and_actual_asset_hashes(monkeypatch):
    before = {"testpypi": {"status": 200}, "github": [{"id": 12, "assets": [{"id": 19}]}]}
    scenario = {
        "before": before,
        "after": before,
        "baseline_asset_sha256": {
            "SHA256SUMS": batch.digest(b"original"),
        },
    }
    download = Mock(return_value=[("SHA256SUMS", b"original")])
    monkeypatch.setattr(batch, "download_assets", download)
    batch.check_duplicate(scenario)
    scenario["after"] = dict(before, github=[{"id": 12, "assets": [{"id": 20}]}])
    with pytest.raises(batch.BatchError, match="changed publication state"):
        batch.check_duplicate(scenario)
    scenario["after"] = before
    download.return_value = [("SHA256SUMS", b"changed")]
    with pytest.raises(batch.BatchError, match="changed GitHub asset bytes"):
        batch.check_duplicate(scenario)


def test_resume_preserves_checkpoint_without_triggering_workflow(monkeypatch, tmp_path):
    original = tmp_path / "original.json"
    saved = {
        "nopt_requirement": "nopt==0.2.0",
        "scenarios": [
            {"name": "successful-dev", "result": "passed"},
            {
                "name": "missing-changelog",
                "result": "pending",
                "triggered_at": "2026-11-26T01:00:00Z",
            },
        ],
    }
    original.write_text(json.dumps(saved), encoding="utf-8")
    output = tmp_path / "resume"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_batch.py",
            "--resume",
            str(original),
            "--output",
            str(output),
        ],
    )
    command = Mock(return_value=b'{"permissions":{"push":true}}')
    monkeypatch.setattr(batch, "command", command)
    verify = Mock(side_effect=batch.BatchError("Timed out"))
    monkeypatch.setattr(batch, "verify", verify)
    assert batch.main() == 1
    command.assert_called_once_with("gh", "api", f"repos/{batch.REPO}")
    verify.assert_called_once()
    assert verify.call_args.args[0]["name"] == "missing-changelog"
    assert json.loads(original.read_bytes()) == saved
    report = json.loads((output / "report.json").read_bytes())
    assert report["error"] == "Timed out"
    assert (output / "summary.txt").is_file()


@pytest.mark.parametrize("pin", ["0.2.0", "a" * 40])
def test_maintenance_commit_keeps_workflow_and_dates_final(monkeypatch, tmp_path, pin):
    fixture = Path(__file__).resolve().parents[1]
    for name in ("pyproject.toml", batch.WORKFLOW, "docs/changelog.md"):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture / name, dest)

    plan = {scenario["name"]: scenario for scenario in batch.release_plan(1, "batch/test/main")}
    pre = plan["maintenance-prerelease"]
    final = plan["maintenance-final"]
    commit = Mock(return_value="a" * 40)
    monkeypatch.setattr(batch, "git", commit)
    requirement = batch.nopt_requirement(pin)
    batch.prepare_commit(tmp_path, pre, requirement)
    assert "## [1.0.1] - UNRELEASED" in (tmp_path / "docs/changelog.md").read_text()
    pre_config = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert pre_config["project"]["version"] == "1.0.1rc1"
    assert pre_config["tool"]["nopt"]["pre-release-branches"] == ["release/1.*"]

    batch.prepare_commit(tmp_path, final, requirement)
    config = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    changelog = (tmp_path / "docs/changelog.md").read_text()
    assert config["project"]["version"] == "1.0.1"
    assert config["tool"]["nopt"]["final-release-branches"] == ["release/1.*"]
    assert changelog.count("## [1.0.1]") == 1
    assert re.search(r"^## \[1\.0\.1\] - \d{4}-\d{2}-\d{2}$", changelog, re.M)
    assert (tmp_path / batch.WORKFLOW).read_bytes() == (fixture / batch.WORKFLOW).read_bytes()
    assert (tmp_path / batch.PIN_FILE).read_text().strip() == requirement


@pytest.mark.parametrize(("draft_major", "index_major"), [(26, 27), (27, 26)])
def test_release_batch_uses_maintenance_ancestry_and_preserves_main(
    monkeypatch, tmp_path, draft_major, index_major
):
    remote = tmp_path / "remote"
    remote.mkdir()
    git = batch.git
    monkeypatch.setattr(batch, "REMOTE", str(remote))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Fixture test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Fixture test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.test")
    git("init", "-b", "main", cwd=remote)
    fixture = Path(__file__).resolve().parents[1]
    files = ("pyproject.toml", "docs/changelog.md", batch.WORKFLOW, batch.PIN_FILE)
    for name in files:
        dest = remote / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture / name, dest)
    project_file = remote / "pyproject.toml"
    project_file.write_text(
        re.sub(r'^version = "[^"]+"$', 'version = "21.4.8"', project_file.read_text(), flags=re.M),
        encoding="utf-8",
    )
    changelog = remote / "docs/changelog.md"
    changelog.write_text("# Changelog\n\n## [22.1.0] - UNRELEASED\n\n- Manual fixture test.\n")
    git("add", "--", *files, cwd=remote)
    git("commit", "-m", "Fixture main", cwd=remote)
    main = git("rev-parse", "HEAD", cwd=remote)
    manual_tag = "v24.9.0rc1"
    manual_branch = "release/25.x"
    git("tag", manual_tag, cwd=remote)
    git("branch", manual_branch, cwd=remote)
    first_major = max(draft_major, index_major) + 1
    latest = {}

    def snapshot(scenario, key, _save):
        scenario[key] = {} if scenario["name"] == "duplicate-version" else EMPTY.copy()
        return scenario[key]

    def verify(scenario, _timeout, _save):
        assert batch.remote_tag(scenario["tag"]) == scenario["commit"]
        scenario.update(after={}, asset_sha256={}, result="passed")
        if scenario["name"] in {"first-final", "new-major-final"}:
            latest["tag_name"] = scenario["tag"]

    dispatch = Mock()
    monkeypatch.setattr(batch, "snapshot", snapshot)
    monkeypatch.setattr(batch, "verify", verify)
    monkeypatch.setattr(batch, "latest_release", lambda: latest.copy())
    monkeypatch.setattr(batch, "testpypi_versions", lambda: [f"{index_major}.1.0"])
    github_releases = Mock(return_value=[{"tag_name": f"v{draft_major}.0.0", "draft": True}])
    monkeypatch.setattr(batch, "page_items", github_releases)
    original_command = batch.command

    def command(*args, **kwargs):
        if args[0] == "gh":
            return dispatch(*args, **kwargs)
        return original_command(*args, **kwargs)

    monkeypatch.setattr(batch, "command", command)
    report = {"suite": "release", "nopt_requirement": "nopt==0.2.0", "scenarios": []}
    batch.run_batch(report, 5, False, lambda: None)
    first, new, rc, patch, missing, duplicate = report["scenarios"]
    expected_latest = f"v{first_major + 1}.0.0"
    assert [item["tag"] for item in (first, new, rc, patch, missing, duplicate)] == [
        f"v{first_major}.0.0",
        expected_latest,
        f"v{first_major}.0.1rc1",
        f"v{first_major}.0.1",
        f"v{first_major}.0.2",
        expected_latest,
    ]
    assert git("rev-parse", "refs/heads/main", cwd=remote) == main
    assert git("rev-parse", f"refs/tags/{manual_tag}", cwd=remote) == main
    assert git("rev-parse", f"refs/heads/{manual_branch}", cwd=remote) == main
    assert git("rev-parse", f"refs/tags/{rc['tag']}^", cwd=remote) == first["commit"]
    assert git("rev-parse", f"refs/tags/{patch['tag']}^", cwd=remote) == rc["commit"]
    assert git("rev-parse", f"refs/heads/release/{first_major}.x", cwd=remote) == patch["commit"]
    assert git("rev-parse", f"refs/tags/{missing['tag']}^", cwd=remote) == patch["commit"]
    assert missing["branch"] != patch["branch"]
    assert {item["expected_latest"] for item in (rc, patch, missing, duplicate)} == {
        expected_latest
    }
    github_releases.assert_called_once_with("releases?per_page=100")
    dispatch.assert_called_once_with(
        "gh", "workflow", "run", "release.yml", "--repo", batch.REPO, "--ref", expected_latest
    )

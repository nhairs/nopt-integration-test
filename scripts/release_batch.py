#!/usr/bin/env python3
"""Run and verify the fixture's TestPyPI release scenarios from a local shell."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

REPO = "nhairs/nopt-integration-test"
REMOTE = f"https://github.com/{REPO}.git"
NOPT = "https://github.com/nhairs/nicks-opinionated-python-package-publishing-tool"
DIST = "nopt-integration-test-nhairs-test"
WORKFLOW = ".github/workflows/release.yml"
PIN_FILE = ".nopt-requirement"
DEV_SCENARIOS = ("successful-dev", "missing-changelog", "duplicate-version")
RELEASE_SCENARIOS = (
    "first-final",
    "new-major-final",
    "maintenance-prerelease",
    "maintenance-final",
    "missing-changelog",
    "duplicate-version",
)
SUITES = {"release": RELEASE_SCENARIOS, "dev": DEV_SCENARIOS}
NEGATIVE_SCENARIOS = {"missing-changelog", "duplicate-version"}
BASE_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
JOBS = {"preflight", "build", "publish", "github-release"}
CI_JOBS = {
    "ci / lint",
    *(f"ci / tests ({os})" for os in ("ubuntu-latest", "macos-latest", "windows-latest")),
}


class BatchError(Exception):
    """A scenario could not establish its expected outcome."""


def require(condition, message):
    if not condition:
        raise BatchError(message)


def command(*args, cwd=None, timeout=60):
    result = subprocess.run(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False
    )
    if result.returncode:
        reason = result.stderr.decode(errors="replace").strip()
        raise BatchError(f"{args[0]} {' '.join(args[1:3])} failed ({result.returncode}): {reason}")
    return result.stdout


def git(*args, cwd=None):
    return command("git", *args, cwd=cwd).decode().strip()


def github(path, *flags, timeout=60):
    return command("gh", "api", *flags, f"repos/{REPO}/{path}", timeout=timeout)


def page_items(path, key=None, *, timeout=60):
    pages = json.loads(github(path, "--paginate", "--slurp", timeout=timeout))
    return [item for page in pages for item in (page if key is None else page[key])]


def testpypi(version):
    url = f"https://test.pypi.org/pypi/{DIST}/{version}/json"
    try:
        with urlopen(url, timeout=20) as response:
            require(response.status == 200, f"Unexpected TestPyPI status {response.status}")
            data = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return {"url": url, "status": 404}
        raise BatchError(f"TestPyPI returned HTTP {error.code} for {url}") from error
    return {
        "url": url,
        "status": 200,
        "version": data["info"]["version"],
        "files": sorted(
            (
                {
                    "name": item["filename"],
                    "sha256": item["digests"]["sha256"],
                    "yanked": item["yanked"],
                    "url": item["url"],
                }
                for item in data["urls"]
            ),
            key=lambda item: item["name"],
        ),
    }


def snapshot(scenario, key, save):
    state = {"testpypi": testpypi(scenario["version"])}
    scenario[key] = state
    save()
    releases = [
        release
        for release in page_items("releases?per_page=100")
        if release["tag_name"] == scenario["tag"]
    ]
    state["github"] = [
        {
            **{
                key: release[key]
                for key in (
                    "id",
                    "html_url",
                    "draft",
                    "prerelease",
                    "published_at",
                    "body",
                    "tag_name",
                )
            },
            "assets": sorted(
                (
                    {
                        key: asset[key]
                        for key in ("id", "name", "state", "digest", "browser_download_url")
                    }
                    for asset in release["assets"]
                ),
                key=lambda asset: asset["name"],
            ),
        }
        for release in releases
    ]
    save()
    return state


def absent(state):
    return state["testpypi"]["status"] == 404 and state["github"] == []


def remote_ref(ref):
    line = git("ls-remote", "--refs", REMOTE, ref)
    return line.split()[0] if line else None


def remote_tag(tag):
    return remote_ref(f"refs/tags/{tag}")


def allocate_version(base, occupied, probe):
    for number in range(1000):
        version = f"{base}.dev{number}"
        tag = f"v{version}"
        if occupied(tag):
            continue
        state = probe(version, tag)
        require(absent(state), f"{tag} has existing publication state. Inspect the report.")
        return version, tag, state
    raise BatchError(f"No unused dev tags found for {base}")


def nopt_requirement(pin):
    if re.fullmatch(r"[0-9a-fA-F]{40}", pin):
        return f"nopt @ git+{NOPT}@{pin.lower()}"
    if re.fullmatch(r"\d+(?:\.\d+)+(?:a\d+|b\d+|rc\d+)?(?:\.post\d+)?(?:\.dev\d+)?", pin):
        return f"nopt=={pin}"
    raise BatchError("Use a published nopt version or a full 40-character nopt commit SHA")


def configure_release_branch(config, scenario):
    """Set the branch policy in the candidate's nopt configuration."""
    prefix, section = config.split("\n[tool.nopt]\n", 1)
    section, *following = re.split(r"(?m)(?=^\[)", section, maxsplit=1)
    policy = (
        "final-release-branches"
        if re.fullmatch(BASE_VERSION, scenario["version"])
        else ("pre-release-branches")
    )
    selector = scenario.get("branch_selector", scenario["branch"])
    setting = f"{policy} = {json.dumps([selector])}\n"
    section = re.sub(rf"(?m)^{policy}\s*=[^\n]*\n?", "", section)
    edited = prefix + "\n[tool.nopt]\n" + setting + section + "".join(following)
    require(
        tomllib.loads(edited)["tool"]["nopt"][policy] == [selector],
        "Could not configure candidate branch policy",
    )
    return edited


def update_changelog(path, scenario, requirement):
    version = scenario["version"]
    base = re.match(BASE_VERSION, version).group()
    stamp = datetime.now(timezone.utc).date().isoformat() if version == base else "UNRELEASED"
    title, *sections = re.split(r"(?m)(?=^## )", path.read_text(encoding="utf-8"))
    others = [section for section in sections if not section.startswith(f"## [{base}]")]
    note = f"## [{base}] - {stamp}\n\n- Verify {scenario['name']} with {requirement}.\n"
    content = "\n\n".join([title.strip(), note.strip(), *(s.strip() for s in others)]) + "\n"
    path.write_bytes(content.encode())


def prepare_commit(tree, scenario, requirement):
    version = scenario["version"]
    config = tree / "pyproject.toml"
    original = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(original)
    require(
        parsed["tool"]["nopt"]["repository"] == "testpypi"
        and parsed["tool"]["nopt"]["testpypi-username"] == "nhairs-test",
        "Fixture must target its TestPyPI distribution",
    )
    edited, count = re.subn(r'^version = "[^"]+"$', f'version = "{version}"', original, flags=re.M)
    require(
        count == 1 and tomllib.loads(edited)["project"]["version"] == version,
        "Could not set the fixture project version",
    )
    if "branch" in scenario:
        edited = configure_release_branch(edited, scenario)
    config.write_bytes(edited.encode())
    workflow = (tree / WORKFLOW).read_text(encoding="utf-8")
    require(
        re.findall(r"repository-url:\s*(\S+)", workflow) == ["https://test.pypi.org/legacy/"],
        "Fixture workflow must publish only to TestPyPI",
    )
    require(
        workflow.count('uvx --from "$NOPT_REQUIREMENT" nopt ') == 4,
        "Expected four nopt workflow invocations using the fixture pin file",
    )
    (tree / PIN_FILE).write_bytes(f"{requirement}\n".encode())
    files = ["pyproject.toml", PIN_FILE]
    if scenario["name"] not in NEGATIVE_SCENARIOS:
        update_changelog(tree / "docs/changelog.md", scenario, requirement)
        files.append("docs/changelog.md")
    git("add", "--", *files, cwd=tree)
    # A unique commit makes an already-created identical tag distinguishable from our push.
    git("commit", "-m", f"Test {scenario['name']} for {version}", "-m", uuid4().hex, cwd=tree)
    return git("rev-parse", "HEAD", cwd=tree)


def select_run(runs, scenario):
    matches = [
        run
        for run in runs
        if (
            run["head_branch"] == scenario["tag"]
            and run["head_sha"] == scenario["commit"]
            and run["event"] == scenario["event"]
            and run["created_at"] >= scenario["triggered_at"]
        )
    ]
    require(
        len(matches) <= 1,
        f"Ambiguous workflow runs for {scenario['tag']}: {[run['html_url'] for run in matches]}",
    )
    return next(iter(matches), None)


def wait_for_run(scenario, timeout, save):
    query = urlencode(
        {
            "head_sha": scenario["commit"],
            "event": scenario["event"],
            "created": f">={scenario['triggered_at']}",
            "per_page": 100,
        }
    )
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        run = select_run(
            page_items(
                f"actions/workflows/release.yml/runs?{query}",
                "workflow_runs",
                timeout=min(remaining, 60),
            ),
            scenario,
        )
        if run:
            details = {
                key: run[key]
                for key in ("id", "html_url", "run_attempt", "status", "conclusion", "created_at")
            }
            require(
                "run" not in scenario or scenario["run"]["id"] == details["id"],
                "Run identity changed during verification",
            )
            scenario["run"] = details
            save()
            require(run["run_attempt"] == 1, "Expected the original workflow attempt")
            if run["status"] == "completed":
                return details
        time.sleep(max(0, min(10, deadline - time.monotonic())))
    raise BatchError(f"Timed out waiting for {scenario['event']} at {scenario['tag']}")


def expected_jobs(name, conclusion, jobs, failure_text):
    actual = {job["name"]: job["conclusion"] for job in jobs}
    expected = dict.fromkeys(JOBS | CI_JOBS, "success")
    expected_conclusion = "success"
    if name in NEGATIVE_SCENARIOS:
        expected = dict.fromkeys(JOBS | {"ci"}, "skipped")
        expected["preflight"] = "failure"
        expected_conclusion = "failure"
    require(
        conclusion == expected_conclusion and len(jobs) == len(expected) and actual == expected,
        f"Unexpected workflow conclusion or jobs: {conclusion}, {actual}",
    )
    if failure_text:
        diagnostic = next(job for job in jobs if job["name"] == "preflight")
        require(
            any(
                line.endswith(f"ValueError: {failure_text}")
                or line.endswith(f"##[error]{failure_text}")
                for line in diagnostic.get("failure_text", [])
            ),
            f"Expected preflight diagnostic: {failure_text}",
        )


def digest(data):
    return sha256(data).hexdigest()


def download_assets(release):
    names = set()
    for asset in release["assets"]:
        name = asset["name"]
        require(
            name not in names and asset["state"] == "uploaded",
            f"Duplicate or incomplete release asset: {name}",
        )
        names.add(name)
        data = github(
            f"releases/assets/{asset['id']}",
            "-H",
            "Accept: application/octet-stream",
            "--allow-escape-sequences",
        )
        require(
            asset["digest"] in (None, f"sha256:{digest(data)}"),
            f"GitHub asset digest mismatch for {name}",
        )
        yield name, data


def verify_success(scenario, save):
    state = scenario["after"]
    index = state["testpypi"]
    require(
        index["status"] == 200 and index["version"] == scenario["version"],
        "Expected exact TestPyPI version",
    )
    files = index["files"]
    expected = {item["name"]: item["sha256"] for item in files}
    wheels = [name for name in expected if name.endswith(".whl")]
    sdists = [name for name in expected if name.endswith(".tar.gz")]
    require(
        len(files) == 2
        and len(wheels) == len(sdists) == 1
        and not any(item["yanked"] for item in files),
        "Expected one wheel and one sdist",
    )
    require(len(state["github"]) == 1, "Expected exactly one GitHub release")
    release = state["github"][0]
    require(
        release["prerelease"] == (re.fullmatch(BASE_VERSION, scenario["version"]) is None)
        and not release["draft"]
        and release["published_at"],
        "Expected a published GitHub release with the matching release type",
    )
    require(
        {asset["name"] for asset in release["assets"]} == expected.keys() | {"SHA256SUMS"},
        "Unexpected GitHub release assets",
    )
    assets = dict(download_assets(release))
    scenario["asset_sha256"] = {name: digest(data) for name, data in assets.items()}
    save()
    require(
        all(scenario["asset_sha256"][name] == value for name, value in expected.items()),
        "GitHub distribution bytes differ from TestPyPI",
    )
    require(
        sorted(assets["SHA256SUMS"].decode("ascii").splitlines())
        == sorted(f"{value}  {name}" for name, value in expected.items()),
        "GitHub SHA256SUMS differs from TestPyPI",
    )
    metadata_path = "nopt_integration_test/_git_metadata.json"
    with ZipFile(BytesIO(assets[wheels[0]])) as wheel:
        wheel_metadata = json.loads(wheel.read(metadata_path))
    with tarfile.open(fileobj=BytesIO(assets[sdists[0]])) as sdist:
        members = [
            member for member in sdist.getmembers() if member.name.endswith(f"/src/{metadata_path}")
        ]
        require(len(members) == 1 and members[0].isfile(), "Invalid sdist Git metadata member")
        sdist_metadata = json.load(sdist.extractfile(members[0]))
    for metadata in (wheel_metadata, sdist_metadata):
        require(
            metadata["BUILD_GIT_HASH"] == scenario["commit"]
            and metadata["PACKAGE_VERSION"] == scenario["version"],
            "Distribution embedded metadata differs from the fixture tag",
        )
    run = scenario["run"]
    suffix = f"{run['id']}-{run['run_attempt']}"
    artifacts = scenario["artifacts"]
    expected_names = {f"release-bundle-{suffix}", f"publication-evidence-{suffix}"}
    require(
        len(artifacts) == 2
        and {item["name"] for item in artifacts} == expected_names
        and len({item["id"] for item in artifacts}) == 2
        and not any(item["expired"] for item in artifacts),
        "Unexpected Actions artifacts",
    )
    artifact = next(item for item in artifacts if item["name"] == f"release-bundle-{suffix}")
    bundle_bytes = github(f"actions/artifacts/{artifact['id']}/zip", "--allow-escape-sequences")
    require(
        artifact["digest"] in (None, f"sha256:{digest(bundle_bytes)}"),
        "Downloaded build artifact differs from its GitHub digest",
    )
    with ZipFile(BytesIO(bundle_bytes)) as bundle:
        manifest_bytes = bundle.read("release.json")
        manifest_hash = digest(manifest_bytes)
        require(
            bundle.read("release.json.sha256sum").splitlines()
            == [f"{manifest_hash}  release.json".encode()],
            "Invalid manifest sidecar",
        )
        manifest = json.loads(manifest_bytes)
        require(
            manifest["schema_version"] == 1
            and manifest["distribution_name"] == DIST
            and manifest["source"]
            == {"commit": scenario["commit"], "tag": scenario["tag"], "dirty": False}
            and manifest["context"]["github_repository"] == REPO
            and manifest["context"]["tag"] == scenario["tag"]
            and manifest["context"]["run_id"] == str(run["id"])
            and manifest["context"]["run_attempt"] == str(run["run_attempt"]),
            "Manifest provenance differs from the workflow run",
        )
        payload = manifest["files"]
        require(
            {name: value for name, value in payload.items() if name.startswith("dist/")}
            == {f"dist/{name}": value for name, value in expected.items()},
            "Build manifest distribution hashes differ from TestPyPI",
        )
        require(
            set(payload)
            == {"pyproject.toml", "release-notes.md"} | {f"dist/{name}" for name in expected}
            and sorted(bundle.namelist())
            == sorted(payload.keys() | {"release.json", "release.json.sha256sum"})
            and all(digest(bundle.read(name)) == value for name, value in payload.items()),
            "Build bundle bytes differ from the manifest",
        )
        require(
            release["body"].strip() == bundle.read("release-notes.md").decode().strip(),
            "GitHub release body differs from the build bundle",
        )
        scenario["manifest_sha256"] = manifest_hash


def capture_after(scenario, save):
    try:
        snapshot(scenario, "after", save)
    except (BatchError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        scenario["state_error"] = str(error)
    save()


def latest_release():
    release = json.loads(github("releases/latest"))
    return {key: release[key] for key in ("id", "tag_name", "html_url")}


def verify(scenario, timeout, save):
    try:
        run = wait_for_run(scenario, timeout, save)
    finally:
        # Collect publication state even after a timeout or interrupted wait.
        capture_after(scenario, save)
    require("state_error" not in scenario, scenario.get("state_error"))
    run_id, attempt = run["id"], run["run_attempt"]
    jobs = page_items(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", "jobs")
    scenario["jobs"] = [
        {key: job[key] for key in ("id", "name", "conclusion", "html_url")} for job in jobs
    ]
    save()
    scenario["artifacts"] = [
        {
            **{key: item[key] for key in ("id", "name", "expired", "digest")},
            "url": f"{run['html_url']}/artifacts/{item['id']}",
        }
        for item in page_items(f"actions/runs/{run_id}/artifacts?per_page=100", "artifacts")
    ]
    save()
    for job in scenario["jobs"]:
        if job["conclusion"] == "failure":
            log = github(f"actions/jobs/{job['id']}/logs", "--allow-escape-sequences")
            lines = re.sub(
                r"\x1b\[[0-?]*[ -/]*[@-~]", "", log.decode(errors="replace")
            ).splitlines()
            job["failure_text"] = [
                line for line in lines if re.search(r"(Error: |##\[error\]|::error::)", line)
            ] or lines[-20:]
            save()
    name, version, tag = scenario["name"], scenario["version"], scenario["tag"]
    scenario["remote_tag_after"] = remote_tag(tag)
    save()
    require(
        scenario["remote_tag_after"] == scenario["commit"], f"Remote tag {tag} changed or is absent"
    )
    base = re.match(BASE_VERSION, version).group()
    diagnostic = {
        "missing-changelog": f"expected exactly one changelog section for {base}",
        "duplicate-version": f"testpypi already has a record for {DIST} {version}",
    }.get(name)
    expected_jobs(name, run["conclusion"], scenario["jobs"], diagnostic)
    if name not in NEGATIVE_SCENARIOS:
        verify_success(scenario, save)
    else:
        require(not scenario["artifacts"], "Preflight failure unexpectedly produced artifacts")
        if name == "missing-changelog":
            require(absent(scenario["after"]), "Missing-changelog run left publication state")
        else:
            check_duplicate(scenario)
    if expected_latest := scenario.get("expected_latest"):
        latest = scenario["github_latest_after"] = latest_release()
        save()
        require(
            latest["tag_name"] == expected_latest,
            f"Expected GitHub latest release {expected_latest}, got {latest['tag_name']}",
        )
        if "github_latest_before" in scenario:
            require(latest == scenario["github_latest_before"], "GitHub latest release changed")
    scenario["result"] = "passed"
    scenario.pop("error", None)
    save()


def check_duplicate(scenario):
    require(scenario["after"] == scenario["before"], "Duplicate run changed publication state")
    hashes = {name: digest(data) for name, data in download_assets(scenario["after"]["github"][0])}
    scenario["asset_sha256"] = hashes
    require(hashes == scenario["baseline_asset_sha256"], "Duplicate run changed GitHub asset bytes")


def release_plan(major, branch):
    """Schedule newer finals before the older series' prerelease and patch release."""
    old, new = f"{major}.0.0", f"{major + 1}.0.0"
    maintenance = f"release/{major}.x"
    latest = f"v{new}"
    return [
        {"name": "first-final", "version": old, "branch": branch, "expected_latest": f"v{old}"},
        {
            "name": "new-major-final",
            "version": new,
            "branch": branch,
            "parent": "first-final",
            "expected_latest": latest,
        },
        {
            "name": "maintenance-prerelease",
            "version": f"{major}.0.1rc1",
            "branch": maintenance,
            "branch_selector": f"release/{major}.*",
            "parent": "first-final",
            "expected_latest": latest,
            "keep_latest": True,
        },
        {
            "name": "maintenance-final",
            "version": f"{major}.0.1",
            "branch": maintenance,
            "branch_selector": f"release/{major}.*",
            "parent": "maintenance-prerelease",
            "expected_latest": latest,
            "keep_latest": True,
        },
        {
            "name": "missing-changelog",
            "version": f"{major}.0.2",
            "branch": f"{branch.rsplit('/', 1)[0]}/missing-changelog",
            "parent": "maintenance-final",
            "expected_latest": latest,
            "keep_latest": True,
        },
        {
            "name": "duplicate-version",
            "parent": "new-major-final",
            "expected_latest": latest,
            "keep_latest": True,
        },
    ]


def prepare_new_tag(tree, scenario, parent, report, dev_base, save):
    """Prepare a commit and check its tags and publication targets before writing."""

    def probe(version, tag):
        scenario.update(version=version, tag=tag)
        save()
        return snapshot(scenario, "before", save)

    if "version" in scenario:
        version = scenario["version"]
        tag = f"v{version}"
        scenario["tag"] = tag
        scenario["remote_tag_before_push"] = remote_tag(tag)
        require(
            absent(probe(version, tag)) and scenario["remote_tag_before_push"] is None,
            f"{tag} already exists or has publication state. Inspect the report.",
        )
    else:
        version, tag, _ = allocate_version(dev_base, remote_tag, probe)

    source = scenario["source_commit"] = parent.get("commit", report["fixture_main"])
    git("checkout", "--detach", source, cwd=tree)
    scenario["commit"] = prepare_commit(tree, scenario, report["nopt_requirement"])
    save()

    if branch := scenario.get("branch"):
        ref = f"refs/heads/{branch}"
        previous = scenario["branch_before"] = remote_ref(ref)
        expected = source if parent.get("branch") == branch else None
        save()
        require(previous == expected, f"Branch {branch} changed. Inspect the report.")
        git("push", REMOTE, f"{scenario['commit']}:{ref}", cwd=tree)
        scenario["branch_at_trigger"] = remote_ref(ref)
        save()
        require(
            scenario["branch_at_trigger"] == scenario["commit"],
            f"Branch {branch} changed before pushing the tag.",
        )

    scenario["remote_tag_before_push"] = remote_tag(tag)
    require(
        absent(probe(version, tag)) and scenario["remote_tag_before_push"] is None,
        f"{tag} became occupied before pushing. Inspect the remote state.",
    )


def prepare_duplicate(scenario, baseline, save):
    for key in ("version", "tag", "commit"):
        scenario[key] = baseline[key]
    snapshot(scenario, "before", save)
    scenario["baseline_asset_sha256"] = baseline["asset_sha256"]
    save()
    require(
        scenario["before"] == baseline["after"],
        "Publication state changed before duplicate dispatch",
    )
    require(
        remote_tag(scenario["tag"]) == scenario["commit"],
        "Successful tag changed before duplicate dispatch",
    )


def trigger(tree, scenario, dispatch, save):
    duplicate = scenario["name"] == "duplicate-version"
    scenario["event"] = "workflow_dispatch" if dispatch or duplicate else "push"
    scenario["triggered_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save()
    tag = scenario["tag"]
    try:
        if not duplicate:
            git("push", REMOTE, f"{scenario['commit']}:refs/tags/{tag}", cwd=tree)
        if scenario["event"] == "workflow_dispatch":
            command("gh", "workflow", "run", "release.yml", "--repo", REPO, "--ref", tag)
    except (BatchError, OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
        capture_after(scenario, save)
        raise


def run_batch(report, timeout, dispatch, save):
    with tempfile.TemporaryDirectory(prefix="nopt-fixture-") as directory:
        tree = Path(directory)
        git("clone", "--branch", "main", "--single-branch", "--no-tags", REMOTE, str(tree))
        report["fixture_main"] = git("rev-parse", "HEAD", cwd=tree)
        require(
            (tree / PIN_FILE).is_file(),
            "Fixture main needs the release workflow that reads .nopt-requirement",
        )
        refs = git("ls-remote", "--refs", REMOTE, "refs/tags/v*", "refs/heads/release/*")
        changelog = (tree / "docs/changelog.md").read_text(encoding="utf-8")
        config = tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))
        known = re.findall(rf"refs/tags/v({BASE_VERSION})", refs)
        known += re.findall(rf"^## \[({BASE_VERSION})\]", changelog, flags=re.M)
        known.append(re.match(BASE_VERSION, config["project"]["version"]).group())
        major, minor, patch = max(tuple(map(int, version.split("."))) for version in known)
        dev_base = f"{major}.{minor}.{patch + 1}"

        if report["suite"] == "release":
            maintenance_majors = [
                int(value) for value in re.findall(r"refs/heads/release/([0-9]+)\.", refs)
            ]
            next_major = max([major, *maintenance_majors]) + 1
            plan = release_plan(next_major, f"batch/{uuid4().hex[:12]}/main")
        else:
            plan = [{"name": name} for name in DEV_SCENARIOS]
            plan[-1]["parent"] = "successful-dev"

        scenarios = {}
        for scenario in plan:
            name = scenario["name"]
            parent = scenarios.get(scenario.get("parent"), {})
            scenarios[name] = scenario
            scenario["result"] = "pending"
            report["scenarios"].append(scenario)
            save()
            if scenario.get("keep_latest"):
                latest = scenario["github_latest_before"] = latest_release()
                save()
                require(
                    latest["tag_name"] == scenario["expected_latest"],
                    "GitHub latest release changed before trigger",
                )
            if name == "duplicate-version":
                prepare_duplicate(scenario, parent, save)
            else:
                prepare_new_tag(tree, scenario, parent, report, dev_base, save)
            trigger(tree, scenario, dispatch, save)
            verify(scenario, timeout, save)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--nopt", help="published version or full nopt commit SHA")
    mode.add_argument("--resume", type=Path, help="verify attempted scenarios from a JSON report")
    parser.add_argument("--suite", choices=SUITES, help="scenario suite (default: release)")
    parser.add_argument("--output", required=True, type=Path, help="new report directory")
    parser.add_argument("--timeout", type=int, default=1800, help="wait seconds per workflow run")
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="dispatch new tags pushed with an Actions GITHUB_TOKEN",
    )
    args = parser.parse_args()
    if args.timeout <= 0 or (args.resume and (args.dispatch or args.suite)):
        parser.error("Use a positive timeout and reserve --dispatch and --suite for a new batch")

    try:
        report = (
            json.loads(args.resume.read_bytes())
            if args.resume
            else {
                "repository": REPO,
                "suite": args.suite or "release",
                "nopt_requirement": nopt_requirement(args.nopt),
                "scenarios": [],
            }
        )
        args.output.mkdir(parents=True, exist_ok=False)
    except (BatchError, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")

    def save():
        temporary = args.output / "report.json.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.output / "report.json")

    report.pop("error", None)
    report.setdefault("suite", "dev")
    save()
    try:
        access = json.loads(command("gh", "api", f"repos/{REPO}"))
        require(
            access.get("permissions", {}).get("push"), "GitHub push access is needed to see drafts"
        )
        if args.resume:
            for scenario in report["scenarios"]:
                if "triggered_at" in scenario and scenario["result"] != "passed":
                    scenario.pop("state_error", None)
                    verify(scenario, args.timeout, save)
        else:
            run_batch(report, args.timeout, args.dispatch, save)
        require(
            [item["name"] for item in report["scenarios"] if item["result"] == "passed"]
            == list(SUITES[report["suite"]]),
            "Batch is incomplete. See the scenario records.",
        )
    except KeyboardInterrupt:
        report["error"] = "Interrupted"
    except (
        BatchError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
        tarfile.TarError,
        BadZipFile,
    ) as error:
        report["error"] = str(error)
    if "error" in report:
        pending = next((item for item in report["scenarios"] if item["result"] != "passed"), None)
        if pending is not None:
            pending["result"] = "failed"
            pending["error"] = report["error"]
    save()
    summary = ["Nopt fixture batch", report["nopt_requirement"], ""]
    for item in report["scenarios"]:
        summary.append(
            f"{item['name']}: {item['result']} {item.get('tag', '')} "
            f"{item.get('run', {}).get('html_url', '')}".rstrip()
        )
    if error := report.get("error"):
        summary.append(f"Batch failed: {error}")
    summary.append(f"JSON report: {args.output / 'report.json'}")
    output = "\n".join(summary) + "\n"
    (args.output / "summary.txt").write_text(output, encoding="utf-8")
    print(output, end="")
    return int("error" in report)


if __name__ == "__main__":
    sys.exit(main())

"""Exercise installed nopt through real local repositories and build artifacts."""

from pathlib import Path
import json
import os
import subprocess
import sys
import uuid

import pytest


def git(project: Path, *arguments: str) -> None:
    """Apply a Git operation only to the disposable test repository."""
    subprocess.run(["git", *arguments], cwd=project, check=True, capture_output=True, text=True)


def nopt(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Invoke the public installed CLI without inheriting workflow identity."""
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith("GITHUB_") or name in {"GH_TOKEN", "GITHUB_TOKEN"}:
            environment.pop(name)
    environment["UV_HTTP_RETRIES"] = "0"
    return subprocess.run(
        [sys.executable, "-m", "nopt", *arguments, "--local", "--project", str(project)],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a tagged TestPyPI-only project, never uploading the fixture."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f"""# Preserve these original bytes during TestPyPI name rewriting.
[build-system]
requires = ["setuptools>=80"]
build-backend = "setuptools.build_meta"

[project]
name = "nopt-local-fixture-{uuid.uuid4().hex}"
version = "1.2.3.dev4"

[tool.setuptools.packages.find]
where = ["src"]

[tool.nopt]
repository = "testpypi"
testpypi-username = "fixture-user"
changelog = false
github-release-publish = false
""",
        encoding="utf-8",
    )
    package = root / "src" / "fixture_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('VALUE = "fixture"\n', encoding="utf-8")
    (root / ".gitignore").write_text(
        "build/\ndist/\n*.egg-info/\n__pycache__/\nrelease-bundle/\n", encoding="utf-8"
    )
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Integration fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "Create disposable release fixture")
    git(root, "tag", "v1.2.3.dev4")
    return root


def build(project: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
    """Build the isolated sdist and wheel through nopt's public command."""
    result = nopt(project, "build", "--output", str(bundle))
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_testpypi_build_restores_source(project: Path, tmp_path: Path) -> None:
    """A successful suffixed build preserves source and bundled TOML bytes."""
    original = (project / "pyproject.toml").read_bytes()
    bundle = tmp_path / "release-bundle"
    build(project, bundle)
    assert (project / "pyproject.toml").read_bytes() == original
    assert (bundle / "pyproject.toml").read_bytes() == original
    manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
    assert manifest["distribution_name"].endswith("-fixture-user")
    distributions = list((bundle / "dist").iterdir())
    assert len(distributions) == 2
    assert len(list((bundle / "dist").glob("*.whl"))) == 1
    assert len(list((bundle / "dist").glob("*.tar.gz"))) == 1
    assert all("fixture_user-1.2.3.dev4" in path.name for path in distributions)
    assert (bundle / "release.json").is_file()
    assert (bundle / "SHA256SUMS").is_file()
    wheel = next((bundle / "dist").glob("*.whl"))
    imported = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import fixture_package; print(fixture_package.VALUE)",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.stdout.strip() == "fixture"
    result = nopt(project, "publish-release-preflight", "--bundle", str(bundle))
    assert result.returncode == 0, result.stdout + result.stderr
    checked_identity = json.loads(result.stdout)
    assert checked_identity["distribution_name"] == manifest["distribution_name"]
    assert checked_identity["version"] == "1.2.3.dev4"
    assert checked_identity["tag"] == "v1.2.3.dev4"


def test_noncanonical_version_is_rejected(project: Path, tmp_path: Path) -> None:
    """Two-component versions fail canonical validation without producing a bundle."""
    configuration = project / "pyproject.toml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace('"1.2.3.dev4"', '"1.2"'),
        encoding="utf-8",
    )
    bundle = tmp_path / "invalid-version-bundle"
    result = nopt(project, "build", "--output", str(bundle))
    assert result.returncode != 0
    assert "version must be canonical X.Y.Z" in result.stderr
    assert not bundle.exists()


def test_tag_must_match_checkout(project: Path, tmp_path: Path) -> None:
    """A newer checkout cannot build under the previous commit's release tag."""
    (project / "next-change.txt").write_text("Another commit\n", encoding="utf-8")
    git(project, "add", "next-change.txt")
    git(project, "commit", "-m", "Move HEAD beyond the release tag")
    bundle = tmp_path / "mismatched-tag-bundle"
    result = nopt(project, "build", "--output", str(bundle))
    assert result.returncode != 0
    assert "release tag does not point to HEAD" in result.stderr
    assert not bundle.exists()


def test_tampered_bundle_is_rejected(project: Path, tmp_path: Path) -> None:
    """Local publishing preflight rejects altered bytes before index checks."""
    bundle = tmp_path / "tampered-bundle"
    build(project, bundle)
    wheel = next((bundle / "dist").glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    result = nopt(project, "publish-release-preflight", "--bundle", str(bundle))
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert wheel.name in result.stderr

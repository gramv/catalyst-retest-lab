"""Release builder against a throwaway git repository; the uv sync step is replaced.

No network, no real virtual environment and no owner directory: every release is built under
pytest's temporary directory.
"""

import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalyst_lab.managed_ops import code_version, package_sha256

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
SPEC = importlib.util.spec_from_file_location("build_release_fixture", SCRIPT)
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
BUILT_AT = datetime(2026, 9, 24, 12, tzinfo=UTC)
PYTHON = {"implementation": "CPython", "version": "3.12.7"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(autouse=True)
def writable_after_test(tmp_path):
    """Built releases are read-only; restore owner write access so pytest can clean up."""
    yield
    for current, directories, _ in os.walk(tmp_path):
        for name in directories:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, 0o700)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)  # Isolated from the owner's git configuration.
    root = tmp_path / "repo"
    package = root / "src" / "catalyst_lab"
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_text('"""Fixture release package."""\n')
    (package / "managed_ops.py").write_text("VALUE = 1\n")
    (package / "migrations" / "001_fixture.sql").write_text("SELECT 1;\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "run_managed_private.py").write_text("# fixture launcher\n")
    (root / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "0"\n')
    (root / "uv.lock").write_text("# fixture lock\n")
    (root / ".gitignore").write_text("*.log\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture release")
    return root


def fake_sync(calls=None, **overrides):
    """Stands in for ``uv sync --locked`` plus the release interpreter's import probe."""

    def sync(target, *, python=None):
        if calls is not None:
            calls.append((target, python))
        (target / "venv" / "bin").mkdir(parents=True)
        (target / "venv" / "bin" / "python").write_text("# fixture interpreter\n")
        package = target / "src" / "catalyst_lab"
        return {"package": str(package), "source_sha256": package_sha256(package),
                "python": PYTHON, **overrides}

    return sync


def test_release_is_the_exported_commit_read_only_with_metadata(repo, tmp_path):
    commit = git(repo, "rev-parse", "HEAD")
    (repo / "ignored.log").write_text("ignored local file")  # Ignored files never ship.
    calls = []
    result = build.build_release(repo, releases=tmp_path / "releases", sync=fake_sync(calls),
                                 now=BUILT_AT)
    target = tmp_path / "releases" / commit
    assert result["directory"] == str(target) and calls == [(target, None)]
    assert result["read_only"] and not result["installed"] and not result["started"]
    metadata = json.loads((target / "release.json").read_text())
    source = package_sha256(repo / "src" / "catalyst_lab")
    assert metadata == {"commit": commit, "source_sha256": source,
                        "built_at": "2026-09-24T12:00:00+00:00", "python": PYTHON}
    assert {k: result[k] for k in metadata} == metadata
    # The launcher's identity function recognises the release from the imported package.
    assert code_version(target / "src" / "catalyst_lab") == {
        "source_sha256": metadata["source_sha256"], "release_commit": commit,
        "release_metadata": "MATCH"}
    files = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert files == [".gitignore", "pyproject.toml", "release.json",
                     "scripts/run_managed_private.py", "src/catalyst_lab/__init__.py",
                     "src/catalyst_lab/managed_ops.py",
                     "src/catalyst_lab/migrations/001_fixture.sql", "uv.lock", "venv/bin/python"]
    for path in [target, *target.rglob("*")]:
        assert not path.lstat().st_mode & 0o222, path
    assert target.stat().st_mode & 0o777 == 0o500
    with pytest.raises(PermissionError):
        (target / "src" / "catalyst_lab" / "managed_ops.py").write_text("VALUE = 2\n")


def test_release_exports_the_requested_ref_not_the_working_copy(repo, tmp_path):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "paper-release-1")
    (repo / "src" / "catalyst_lab" / "managed_ops.py").write_text("VALUE = 2\n")
    git(repo, "commit", "-q", "-am", "later change")
    result = build.build_release(repo, "paper-release-1", releases=tmp_path / "releases",
                                 sync=fake_sync(), now=BUILT_AT)
    target = Path(result["directory"])
    assert result["commit"] == first and target.name == first
    assert (target / "src" / "catalyst_lab" / "managed_ops.py").read_text() == "VALUE = 1\n"


def test_dirty_working_tree_is_refused(repo, tmp_path):
    releases = tmp_path / "releases"
    tracked = repo / "src" / "catalyst_lab" / "managed_ops.py"
    for dirty, clean in (
        (lambda: tracked.write_text("VALUE = 9\n"), lambda: git(repo, "checkout", "--", ".")),
        (lambda: (repo / "untracked.py").write_text("x = 1\n"),
         lambda: (repo / "untracked.py").unlink()),
        (lambda: (tracked.write_text("VALUE = 9\n"), git(repo, "add", "-A")),
         lambda: git(repo, "reset", "-q", "--hard")),
    ):
        dirty()
        with pytest.raises(build.ReleaseError, match="DIRTY_WORKING_TREE"):
            build.build_release(repo, releases=releases, sync=fake_sync(), now=BUILT_AT)
        clean()
    assert not releases.exists()
    assert build.build_release(repo, releases=releases, sync=fake_sync(), now=BUILT_AT)


def test_existing_release_directory_is_never_overwritten(repo, tmp_path):
    commit = git(repo, "rev-parse", "HEAD")
    existing = tmp_path / "releases" / commit
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("owner data")
    calls = []
    with pytest.raises(build.ReleaseError, match="RELEASE_DIRECTORY_EXISTS"):
        build.build_release(repo, releases=tmp_path / "releases", sync=fake_sync(calls),
                            now=BUILT_AT)
    assert calls == [] and (existing / "keep.txt").read_text() == "owner data"
    assert sorted(p.name for p in existing.iterdir()) == ["keep.txt"]


def test_failed_or_inconsistent_environment_leaves_no_release(repo, tmp_path):
    releases = tmp_path / "releases"
    commit = git(repo, "rev-parse", "HEAD")

    def broken(target, *, python=None):
        (target / "venv").mkdir()
        raise build.ReleaseError("RELEASE_ENVIRONMENT_FAILED")

    for sync, code in (
        (broken, "RELEASE_ENVIRONMENT_FAILED"),
        (fake_sync(package=str(tmp_path / "elsewhere" / "catalyst_lab")),
         "RELEASE_IMPORT_PATH_MISMATCH"),
        (fake_sync(source_sha256="0" * 64), "RELEASE_HASH_MISMATCH"),
        (lambda target, python=None: {"unexpected": True}, "RELEASE_ENVIRONMENT_FAILED"),
    ):
        with pytest.raises(build.ReleaseError, match=code):
            build.build_release(repo, releases=releases, sync=sync, now=BUILT_AT)
        assert not (releases / commit).exists()
    # Nothing was left behind, so a corrected build of the same commit succeeds.
    assert build.build_release(repo, releases=releases, sync=fake_sync(), now=BUILT_AT)


def test_refs_are_validated_and_refusals_are_codes(repo, tmp_path):
    releases = tmp_path / "releases"
    for ref, code in (("--upload-pack=touch", "RELEASE_REF_INVALID"),
                      ("no-such-tag", "RELEASE_REF_UNRESOLVED")):
        with pytest.raises(build.ReleaseError, match=code):
            build.build_release(repo, ref, releases=releases, sync=fake_sync(), now=BUILT_AT)
    with pytest.raises(build.ReleaseError, match="ABSOLUTE_RELEASES_DIRECTORY_REQUIRED"):
        build.build_release(repo, releases=Path("relative"), sync=fake_sync(), now=BUILT_AT)
    with pytest.raises(SystemExit, match="RELEASE_BUILD_REFUSED: RELEASE_REF_UNRESOLVED"):
        build.main(["--repo", str(repo), "--releases", str(releases), "--ref", "no-such-tag"])
    assert not releases.exists()


def test_uv_sync_targets_the_release_venv_from_the_lock_only(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/shared/dev-venv-must-not-be-used")
    monkeypatch.setenv("FIXTURE_SECRET_TOKEN", "never-passed-to-uv")
    monkeypatch.setattr(build.shutil, "which", lambda name: "/fixture/bin/uv")
    probe = {"package": str(tmp_path / "src" / "catalyst_lab"), "source_sha256": "f" * 64,
             "python": PYTHON}
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps(probe) if "-c" in command else "")

    monkeypatch.setattr(build.subprocess, "run", run)
    assert build.uv_sync(tmp_path, python="3.12") == probe
    (sync, sync_kwargs), (interpreter, probe_kwargs) = commands[0], commands[-1]
    assert sync == ["/fixture/bin/uv", "sync", "--locked", "--compile-bytecode",
                    "--python", "3.12"]
    assert sync_kwargs["cwd"] == tmp_path and sync_kwargs["check"] is True
    assert sync_kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "venv")
    assert sync_kwargs["env"]["UV_LINK_MODE"] == "copy"
    assert "FIXTURE_SECRET_TOKEN" not in sync_kwargs["env"]
    assert interpreter[:3] == [str(tmp_path / "venv" / "bin" / "python"), "-I", "-B"]
    assert "FIXTURE_SECRET_TOKEN" not in probe_kwargs["env"]
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    with pytest.raises(build.ReleaseError, match="UV_REQUIRED"):
        build.uv_sync(tmp_path)

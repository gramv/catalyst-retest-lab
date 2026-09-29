"""Cloud release identity: staging a committed tree and sealing it at image build time.

A throwaway git repository under pytest's temporary directory; nothing is uploaded.
"""

import importlib.util
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalyst_lab import cloud_release
from catalyst_lab.managed_ops import code_version, package_sha256
from tests.test_build_release import GIT_ENV, git  # noqa: F401
from tests.test_build_release import repo as repo  # noqa: F401  (fixture)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloud_release.py"
SPEC = importlib.util.spec_from_file_location("cloud_release_script_fixture", SCRIPT)
staging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging)
BUILT_AT = datetime(2026, 9, 27, 22, tzinfo=UTC)


def test_stage_exports_the_committed_tree_with_its_identity(repo, tmp_path):
    (repo / "ignored.log").write_text("never staged\n")  # Ignored by .gitignore: tree stays clean.
    out = tmp_path / "stage" / "release"
    result = staging.stage(repo, out)
    commit = git(repo, "rev-parse", "HEAD")
    source = json.loads((out / cloud_release.SOURCE_FILE).read_text())
    assert source == {"format": cloud_release.SOURCE_FORMAT, "commit": commit,
                      "source_sha256": package_sha256(out / "src" / "catalyst_lab")}
    assert result["uploaded"] is False and result["directory"] == str(out.resolve())
    assert not (out / "ignored.log").exists() and not (out / ".git").exists()
    assert any("--service trader" in line and "--path-as-root" in line
               for line in result["deploy"])
    with pytest.raises(staging.ReleaseError, match="STAGE_DIRECTORY_EXISTS"):
        staging.stage(repo, out)


def test_stage_never_carries_a_config_as_code_file(repo, tmp_path):
    """The legacy review surface's root railway.json must never reach a deploy: Railway reads
    Config as Code from the uploaded source for legacy services (first deploy, 2026-09-28)."""
    (repo / "railway.json").write_text('{"build": {"dockerfilePath": "Dockerfile.review"}}\n')
    (repo / "deploy").mkdir()
    (repo / "deploy" / "railway.review-worker.json").write_text("{}\n")
    git(repo, "add", "railway.json", "deploy/railway.review-worker.json")
    git(repo, "commit", "-q", "-m", "legacy config as code")
    out = tmp_path / "stage" / "release"
    result = staging.stage(repo, out)
    assert not (out / "railway.json").exists() and not (out / "railway.toml").exists()
    assert (out / "deploy" / "railway.review-worker.json").exists()  # Only the root is read.
    assert result["excluded_config_as_code"] == ["railway.json"]
    assert (repo / "railway.json").exists()  # The repository itself is untouched.
    without = staging.stage(repo, tmp_path / "stage" / "again")
    assert without["excluded_config_as_code"] == ["railway.json"]


def test_stage_refuses_a_dirty_tree_a_relative_or_in_repo_directory(repo, tmp_path):
    with pytest.raises(staging.ReleaseError, match="ABSOLUTE_STAGE_DIRECTORY_REQUIRED"):
        staging.stage(repo, "relative/out")
    with pytest.raises(staging.ReleaseError, match="STAGE_DIRECTORY_INSIDE_REPOSITORY"):
        staging.stage(repo, repo / "stage")
    (repo / "src" / "catalyst_lab" / "managed_ops.py").write_text("VALUE = 2\n")
    with pytest.raises(staging.ReleaseError, match="DIRTY_WORKING_TREE"):
        staging.stage(repo, tmp_path / "dirty")
    assert not (tmp_path / "dirty").exists()


def test_seal_writes_the_release_metadata_code_version_reads(repo, tmp_path):
    out = tmp_path / "image-root"
    staging.stage(repo, out)
    metadata = cloud_release.seal(out, now=BUILT_AT)
    commit = git(repo, "rev-parse", "HEAD")
    assert metadata["commit"] == commit and metadata["built_at"] == BUILT_AT.isoformat()
    release = out / "release.json"
    assert stat.S_IMODE(release.stat().st_mode) == 0o444
    identity = code_version(out / "src" / "catalyst_lab")
    assert identity == {"source_sha256": metadata["source_sha256"], "release_commit": commit,
                        "release_metadata": "MATCH"}
    verified = cloud_release.verify_image_release(package_dir=out / "src" / "catalyst_lab")
    assert verified == {"release_commit": commit, "source_sha256": metadata["source_sha256"]}
    with pytest.raises(ValueError, match="RELEASE_METADATA_EXISTS"):
        cloud_release.seal(out)


def test_seal_refuses_a_changed_missing_or_malformed_source(repo, tmp_path):
    out = tmp_path / "changed"
    staging.stage(repo, out)
    (out / "src" / "catalyst_lab" / "managed_ops.py").write_text("VALUE = 'tampered'\n")
    with pytest.raises(ValueError, match="RELEASE_SOURCE_HASH_MISMATCH"):
        cloud_release.seal(out)
    assert not (out / "release.json").exists()
    missing = tmp_path / "missing"
    staging.stage(repo, missing)
    (missing / cloud_release.SOURCE_FILE).unlink()
    with pytest.raises(ValueError, match="RELEASE_SOURCE_MISSING"):
        cloud_release.seal(missing)
    for document in ({"format": "OTHER", "commit": "a" * 40, "source_sha256": "b" * 64},
                     {"format": cloud_release.SOURCE_FORMAT, "commit": "main",
                      "source_sha256": "b" * 64},
                     {"format": cloud_release.SOURCE_FORMAT, "commit": "a" * 40,
                      "source_sha256": "b" * 64, "extra": 1}):
        (missing / cloud_release.SOURCE_FILE).write_text(json.dumps(document))
        with pytest.raises(ValueError, match="RELEASE_SOURCE_INVALID"):
            cloud_release.seal(missing)


def test_unsealed_package_is_refused_at_runtime(tmp_path):
    package = tmp_path / "root" / "src" / "catalyst_lab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    with pytest.raises(ValueError, match="RELEASE_IDENTITY_UNVERIFIED"):
        cloud_release.verify_image_release(package_dir=package)
    (tmp_path / "root" / "release.json").write_text(json.dumps({
        "commit": "a" * 40, "source_sha256": "0" * 64, "built_at": BUILT_AT.isoformat(),
        "python": {"implementation": "CPython", "version": "3.12.7"}}))
    with pytest.raises(ValueError, match="RELEASE_IDENTITY_UNVERIFIED"):
        cloud_release.verify_image_release(package_dir=package)  # Hash does not match.


def test_module_cli_prints_codes_only(repo, tmp_path, capsys):
    out = tmp_path / "cli"
    staging.stage(repo, out)
    (out / cloud_release.SOURCE_FILE).unlink()
    with pytest.raises(SystemExit, match="CLOUD_RELEASE_REFUSED: RELEASE_SOURCE_MISSING"):
        cloud_release.main(["seal", str(out)])

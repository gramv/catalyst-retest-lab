"""Build an immutable, read-only paper release from a clean tag or commit.

    python scripts/build_release.py [--ref TAG_OR_COMMIT] [--releases DIR] [--python REQUEST]

The committed tree (``git archive``, never the working copy) is exported to
``<releases>/<commit>/``. The release gets its own virtual environment at ``<release>/venv`` from
``uv sync --locked`` (runtime dependencies only; the project resolves to the exported ``src``).
The release's own interpreter then confirms where ``catalyst_lab`` imports from and hashes it.
``release.json`` records {commit, source_sha256, built_at, python}, and every file and
directory loses its write permission. A dirty working tree, an unresolvable ref and an existing
release directory are refused; a failed build removes only the directory it created itself.
Nothing is installed as a service, started, migrated or pushed. Copy ``source_sha256`` and the
release directory into the private configuration (``release`` section, config_version 2).
"""

import argparse
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from catalyst_lab.managed_ops import RELEASE_METADATA, package_sha256

DEFAULT_RELEASES = Path.home() / ".local" / "share" / "catalyst-retest-lab" / "releases"
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@^~-]{0,199}")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PASSTHROUGH_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR", "UV_OFFLINE",
                   "UV_PYTHON_INSTALL_DIR")
# Runs inside the new release with its own interpreter: -I ignores PYTHONPATH and user site,
# -B writes no bytecode into the exported tree.
PROBE = (
    "import json, platform\n"
    "from catalyst_lab.managed_ops import package_directory, package_sha256\n"
    "d = package_directory()\n"
    "print(json.dumps({'package': str(d), 'source_sha256': package_sha256(d),\n"
    "    'python': {'implementation': platform.python_implementation(),\n"
    "               'version': platform.python_version()}}))\n"
)


class ReleaseError(ValueError):
    """A refusal with a code only; never paths, command output or credentials."""


def git(repo, *args):
    try:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              timeout=300).stdout
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("GIT_COMMAND_FAILED") from None


def resolve_commit(repo, ref):
    if not isinstance(ref, str) or not REF.fullmatch(ref):
        raise ReleaseError("RELEASE_REF_INVALID")
    try:
        output = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "--end-of-options",
             ref + "^{commit}"], check=True, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("RELEASE_REF_UNRESOLVED") from None
    if not COMMIT.fullmatch(output):
        raise ReleaseError("RELEASE_REF_UNRESOLVED")
    return output


def export_tree(repo, commit, target):
    """The committed tree only: uncommitted or ignored files never enter a release."""
    archive = git(repo, "archive", "--format=tar", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")


def uv_sync(target, *, python=None):
    """Create ``<target>/venv`` with the locked runtime dependencies and probe the import."""
    uv = shutil.which("uv")
    if uv is None:
        raise ReleaseError("UV_REQUIRED")
    env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    # Explicit project environment (an inherited UV_PROJECT_ENVIRONMENT would sync another
    # venv) and copied files (hard links into uv's cache would share the read-only modes).
    env.update(UV_PROJECT_ENVIRONMENT=str(target / "venv"), UV_LINK_MODE="copy")
    command = [uv, "sync", "--locked", "--compile-bytecode"]
    if python:
        command += ["--python", python]
    try:
        subprocess.run(command, cwd=target, env=env, check=True, timeout=1800)
        if sys.platform == "darwin":
            # Python skips .pth files carrying macOS UF_HIDDEN (same fix as ./run).
            for path in sorted((target / "venv" / "lib").glob("python*/site-packages/*.pth")):
                subprocess.run(["chflags", "nohidden", str(path)], check=True, timeout=60)
        probe = subprocess.run(
            [str(target / "venv" / "bin" / "python"), "-I", "-B", "-c", PROBE], cwd=target,
            env={k: v for k, v in env.items() if k in {"HOME", "PATH", "LANG", "LC_ALL"}},
            check=True, capture_output=True, text=True, timeout=300,
        )
        return json.loads(probe.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ReleaseError("RELEASE_ENVIRONMENT_FAILED") from None


def make_read_only(root):
    """Remove every write bit; symlinks (the venv's interpreter links) are left untouched."""
    for current, directories, files in os.walk(root, topdown=False):
        for name in files + directories:
            path = os.path.join(current, name)
            info = os.lstat(path)
            if not stat.S_ISLNK(info.st_mode):
                os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o222)
    os.chmod(root, stat.S_IMODE(os.lstat(root).st_mode) & ~0o222)


def remove_partial(root):
    """Remove a release directory this build created and did not finish."""
    for current, directories, _ in os.walk(root):
        for name in directories:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, 0o700)
    os.chmod(root, 0o700)
    shutil.rmtree(root)


def build_release(repo, ref="HEAD", *, releases=DEFAULT_RELEASES, sync=uv_sync, python=None,
                  now=None):
    repo = Path(repo).resolve()
    if git(repo, "status", "--porcelain", "--untracked-files=normal").strip():
        raise ReleaseError("DIRTY_WORKING_TREE")
    commit = resolve_commit(repo, ref)
    root = Path(releases)
    if not root.is_absolute():
        raise ReleaseError("ABSOLUTE_RELEASES_DIRECTORY_REQUIRED")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / commit
    try:
        os.mkdir(target, 0o700)  # Exclusive: an existing release is never overwritten.
    except FileExistsError:
        raise ReleaseError("RELEASE_DIRECTORY_EXISTS") from None
    try:
        export_tree(repo, commit, target)
        package = target / "src" / "catalyst_lab"
        try:
            source = package_sha256(package)
        except ValueError:
            raise ReleaseError("RELEASE_PACKAGE_MISSING") from None
        installed = sync(target, python=python)
        try:
            imported = Path(installed["package"]).resolve()
            reported = installed["source_sha256"]
            interpreter = installed["python"]
        except (KeyError, TypeError):
            raise ReleaseError("RELEASE_ENVIRONMENT_FAILED") from None
        if imported != package.resolve():
            raise ReleaseError("RELEASE_IMPORT_PATH_MISMATCH")
        if reported != source:
            raise ReleaseError("RELEASE_HASH_MISMATCH")
        metadata = {"commit": commit, "source_sha256": source,
                    "built_at": (now or datetime.now(UTC)).isoformat(), "python": interpreter}
        fd = os.open(target / RELEASE_METADATA, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(metadata, stream, sort_keys=True, indent=2)
            stream.write("\n")
        make_read_only(target)
    except BaseException:
        remove_partial(target)
        raise
    return {**metadata, "directory": str(target), "read_only": True, "installed": False,
            "started": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", default="HEAD", help="clean tag or commit (default HEAD)")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--releases", default=str(DEFAULT_RELEASES))
    parser.add_argument("--python", help="interpreter request passed to uv sync --python")
    args = parser.parse_args(argv)
    try:
        result = build_release(args.repo, args.ref, releases=Path(args.releases),
                               python=args.python)
    except ReleaseError as exc:
        raise SystemExit(f"RELEASE_BUILD_REFUSED: {exc}") from None
    print(json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    main()

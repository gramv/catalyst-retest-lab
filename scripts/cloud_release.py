"""Stage a committed tree for ``railway up`` with its release identity (package cloud).

    python scripts/cloud_release.py --out /abs/new-directory [--ref TAG_OR_COMMIT]

``railway up`` uploads local files and gives the build no git SHA. This script exports the
committed tree of a clean working copy (``git archive``, never the working copy, ignored files or
``.env``) into a new directory and writes ``release-source.json`` = {format, commit,
source_sha256} beside it. Deploy that directory; the image build seals it
(``python -m catalyst_lab.cloud_release seal``) and fails if the uploaded package differs.
Nothing is uploaded, installed, started or pushed here, and no credential is read.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_release import ReleaseError, export_tree, git, resolve_commit  # noqa: E402

from catalyst_lab.cloud_release import SOURCE_FILE, source_document  # noqa: E402
from catalyst_lab.managed_ops import package_sha256  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[1]
SERVICES = ("provision", "trader", "ops", "experiment")
# Config as Code files of the legacy review surface (docs/RAILWAY-DEPLOYMENT.md 2.3). This
# deployment is Infrastructure as Code only, so a staged release never carries one: no deploy
# can pick up its build or start settings (first deploy, 2026-09-28).
CONFIG_AS_CODE = ("railway.json", "railway.toml")


def stage(repo, out, ref="HEAD"):
    repo = Path(repo).resolve()
    if git(repo, "status", "--porcelain", "--untracked-files=normal").strip():
        raise ReleaseError("DIRTY_WORKING_TREE")
    commit = resolve_commit(repo, ref)
    target = Path(out)
    if not target.is_absolute():
        raise ReleaseError("ABSOLUTE_STAGE_DIRECTORY_REQUIRED")
    target = target.resolve(strict=False)
    if target.is_relative_to(repo):
        raise ReleaseError("STAGE_DIRECTORY_INSIDE_REPOSITORY")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.mkdir(target, 0o700)  # Exclusive: a staged tree is never reused or overwritten.
    except FileExistsError:
        raise ReleaseError("STAGE_DIRECTORY_EXISTS") from None
    export_tree(repo, commit, target)
    excluded = []
    for name in CONFIG_AS_CODE:
        path = target / name
        if path.is_symlink() or path.exists():
            path.unlink()
            excluded.append(name)
    try:
        digest = package_sha256(target / "src" / "catalyst_lab")
    except ValueError:
        raise ReleaseError("RELEASE_PACKAGE_MISSING") from None
    document = source_document(commit, digest)
    fd = os.open(target / SOURCE_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as stream:
        json.dump(document, stream, sort_keys=True, indent=2)
        stream.write("\n")
    return {**document, "directory": str(target), "uploaded": False,
            "excluded_config_as_code": excluded,
            "deploy": [f"railway up {target} --path-as-root --service {name} --ci"
                       for name in SERVICES]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="new absolute directory outside the repo")
    parser.add_argument("--ref", default="HEAD", help="clean tag or commit (default HEAD)")
    parser.add_argument("--repo", default=str(REPOSITORY))
    args = parser.parse_args(argv)
    try:
        result = stage(args.repo, args.out, args.ref)
    except ReleaseError as exc:
        raise SystemExit(f"CLOUD_STAGE_REFUSED: {exc}") from None
    print(json.dumps(result, sort_keys=True, indent=2))
    return result


if __name__ == "__main__":
    main()

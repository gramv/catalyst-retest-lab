"""Release identity for an image built from an uploaded tree (package cloud, 2026-09-27).

``railway up`` uploads local files, so the build has no git metadata. The owner stages the
committed tree first (``scripts/cloud_release.py stage``: ``git archive`` of a clean commit into
a new directory plus ``release-source.json`` = {format, commit, source_sha256}) and uploads that
directory. The image build then runs ``python -m catalyst_lab.cloud_release seal /app``: it
recomputes the package SHA-256 exactly as ``managed_ops.package_sha256`` does and writes the same
``release.json`` that ``scripts/build_release.py`` writes for a Mac release, so the running app
reports ``release_commit`` through the unchanged ``managed_ops.code_version``. A tree that differs
from what was staged fails the build (``RELEASE_SOURCE_HASH_MISMATCH``); a missing
``release-source.json`` fails the Dockerfile's COPY. Nothing here contacts git, Railway or the
network, and nothing here is secret.
"""

import argparse
import json
import os
import platform
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from catalyst_lab.jev_contract import strict_json
from catalyst_lab.managed_ops import (
    COMMIT_HEX,
    RELEASE_METADATA,
    SHA256_HEX,
    code_version,
    package_sha256,
)

SOURCE_FILE = "release-source.json"
SOURCE_FORMAT = "CATALYST_CLOUD_RELEASE_SOURCE_V1"
SOURCE_KEYS = frozenset({"format", "commit", "source_sha256"})
SOURCE_LIMIT = 4096
CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def source_document(commit, source_sha256):
    """The staged identity, validated before it is written."""
    document = {"format": SOURCE_FORMAT, "commit": commit, "source_sha256": source_sha256}
    return _validated_source(document)


def _validated_source(document):
    if (not isinstance(document, dict) or set(document) != SOURCE_KEYS
            or document["format"] != SOURCE_FORMAT
            or not isinstance(document["commit"], str)
            or not COMMIT_HEX.fullmatch(document["commit"])
            or not isinstance(document["source_sha256"], str)
            or not SHA256_HEX.fullmatch(document["source_sha256"])):
        raise ValueError("RELEASE_SOURCE_INVALID")
    return document


def read_source(root):
    path = Path(root) / SOURCE_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ValueError("RELEASE_SOURCE_MISSING") from None
    except OSError:
        raise ValueError("RELEASE_SOURCE_INVALID") from None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("RELEASE_SOURCE_INVALID")
        raw = stream.read(SOURCE_LIMIT + 1)
    if len(raw) > SOURCE_LIMIT:
        raise ValueError("RELEASE_SOURCE_INVALID")
    try:
        return _validated_source(strict_json(raw))
    except (TypeError, ValueError):
        raise ValueError("RELEASE_SOURCE_INVALID") from None


def seal(root, *, uid=None, gid=None, now=None):
    """Build step: check the uploaded package against the staged identity, write release.json.

    ``uid``/``gid`` hand the file to the non-root runtime user (``managed_ops.release_metadata``
    reads only a release.json the current user owns); it is left read-only (0444).
    """
    root = Path(root)
    source = read_source(root)
    try:
        actual = package_sha256(root / "src" / "catalyst_lab")
    except ValueError:
        raise ValueError("RELEASE_PACKAGE_MISSING") from None
    if actual != source["source_sha256"]:
        raise ValueError("RELEASE_SOURCE_HASH_MISMATCH")
    metadata = {
        "commit": source["commit"],
        "source_sha256": actual,
        "built_at": (now or datetime.now(UTC)).isoformat(),
        "python": {"implementation": platform.python_implementation(),
                   "version": platform.python_version()},
    }
    target = root / RELEASE_METADATA
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError("RELEASE_METADATA_EXISTS") from None
    with os.fdopen(fd, "w") as stream:
        json.dump(metadata, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if uid is not None:
        os.chown(target, uid, gid if gid is not None else uid)
    os.chmod(target, 0o444)
    return metadata


def verify_image_release(*, package_dir=None):
    """Runtime (railway mode): the imported package must be a sealed release.

    Unlike ``managed_ops.verify_release`` there is no host release directory to compare with:
    the image root that holds ``src/catalyst_lab`` is the release, and its ``release.json`` must
    match the imported code's hash.
    """
    identity = code_version(package_dir)
    if identity["release_metadata"] != "MATCH" or not identity["release_commit"]:
        raise ValueError("RELEASE_IDENTITY_UNVERIFIED")
    return {"release_commit": identity["release_commit"],
            "source_sha256": identity["source_sha256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m catalyst_lab.cloud_release",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    sealing = commands.add_parser("seal", help="image build step")
    sealing.add_argument("root", type=Path)
    sealing.add_argument("--uid", type=int)
    sealing.add_argument("--gid", type=int)
    commands.add_parser("verify", help="check the imported package (no output of secrets)")
    args = parser.parse_args(argv)
    try:
        if args.command == "seal":
            result = seal(args.root, uid=args.uid, gid=args.gid)
        else:
            result = verify_image_release()
    except (OSError, ValueError) as exc:
        code = str(exc)
        raise SystemExit("CLOUD_RELEASE_REFUSED: " + (
            code if CODE.fullmatch(code) else "CLOUD_RELEASE_FAILED")) from None
    print(json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    main()

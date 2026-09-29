"""Repository hygiene: no credential-shaped strings and no oversized files in tracked content.

Runs against git-tracked plus untracked-but-not-ignored files, so a stray file is caught
before it is ever committed. Patterns are tuned to skip fixtures (PKFIXTURE…, test-only
tokens) and snake_case identifiers; a genuine key shape fails loudly with file:line only.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from catalyst_lab.audit import CREDENTIAL_PATTERNS, credential_matches
from catalyst_lab.audit import FIXTURE_MARKERS as FIXTURE_MARKERS  # Used by test_ledger_backup.

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1_048_576
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2"}
# One list of credential shapes, shared with the audit checkpoint writer (plan 4.7).
PATTERNS = CREDENTIAL_PATTERNS


def _candidate_files():
    if not shutil.which("git"):
        pytest.skip("git is required to enumerate tracked files")
    listing = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode()
    return [ROOT / name for name in listing.split("\0") if name]


def test_no_tracked_file_exceeds_size_cap():
    oversized = [
        f"{p.relative_to(ROOT)} ({p.stat().st_size} bytes)"
        for p in _candidate_files()
        if p.is_file() and p.stat().st_size > MAX_BYTES
    ]
    hint = "Move to ~/.local/share/catalyst-retest-lab/evidence: "
    assert not oversized, hint + ", ".join(oversized)


def test_no_credential_shaped_strings():
    findings = []
    for path in _candidate_files():
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, match in credential_matches(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{name} at {path.relative_to(ROOT)}:{line}")
    assert not findings, "Credential-shaped content (values withheld): " + "; ".join(findings)

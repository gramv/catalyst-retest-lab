"""Build a clean public export of this repository (``OSS_EXPORT_V1``, package oss-packaging).

    ./run python scripts/oss_export.py [--out DIR] [--ref HEAD]

**Prepares a folder only. It never pushes, publishes, creates a remote or runs git write
commands**; publishing is the owner's separate decision.

What it does:

1. Reads the committed tree of ``--ref`` (default ``HEAD``) with ``git ls-tree`` and
   ``git cat-file`` -- never the working tree, so untracked or ignored files (a local ``.env``,
   run folders) cannot leak in.
2. Excludes, with a reason recorded per file (``EXCLUDE_RULES``): ``artifacts/`` and
   ``exports/`` (run evidence: captured third-party pages, provider responses, ledger exports),
   every ``.env*`` file except a names-only ``.env.example`` (checked: every line ``NAME=``),
   the owner's coding-agent operating notes (``AGENTS.md``), and the export's own README
   template.
3. Scrubs personal details from the files it keeps (``SCRUBS``): home-folder paths become
   ``~``, the private deployment's public domains a placeholder, any e-mail address outside the
   reserved example domains a placeholder, and every private term (``private_terms``: the OS
   login, and the entries of a terms file kept outside the repository, such as a GitHub
   account) a placeholder -- so no owner-specific value is written in this script. Each scrub is
   counted per file in the manifest (terms by name, never by value).
   With ``--exclude-history`` (the default; ``--keep-history`` turns it off) the project's
   history and run-log docs are excluded too (``HISTORY_RULES``: the phase log, package
   records, checkpoints, dated run records, validation runs and runtime evidence), the
   reference-deployment rulings are replaced by their summary ``docs/REFERENCE-RULES.md``, and
   links from kept Markdown files to an excluded doc are rewritten (``scrub_history_links``).
   Code files are never rewritten; mentions of excluded docs left in them are counted in the
   manifest (``history_mentions_left``).
4. Writes ``README.md`` for newcomers (``scripts/oss/README.public.md``), keeps ``LICENSE``,
   ``CONTRIBUTING.md`` and ``SECURITY.md``, and writes ``EXPORT-MANIFEST.json`` (every included
   file with its SHA-256, every exclusion and scrub with its reason, the scan results).
5. Scans the export and **fails (exit 1) if anything is found**:
   - the prohibited live-trading endpoint literal (derived from ``config.PAPER_ENDPOINT``, as
     ``tests/test_safety.py`` does, so this script never contains it), in every file, binary
     included;
   - secret shapes (``SECRET_PATTERNS``: the repository's own ``audit.CREDENTIAL_PATTERNS``
     plus gitleaks-style rules for cloud, GitHub, Slack, Google and Stripe keys, JWTs, private
     key blocks, Alpaca live key ids, and database URLs with an inline password), with a
     narrow, documented allowlist for test fixtures (``FIXTURE_MARKERS``);
   - personal details left after the scrubs (``PERSONAL_PATTERNS``).
   Findings name the file, line and rule, never the matched value.

The default output is ``~/.local/share/catalyst-oss-export/<UTC date>/``; the folder must be
outside the repository and empty (or absent).
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

VERSION = "OSS_EXPORT_V1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_ROOT = Path.home() / ".local/share/catalyst-oss-export"
README_TEMPLATE = "scripts/oss/README.public.md"
MANIFEST = "EXPORT-MANIFEST.json"
REQUIRED_FILES = ("LICENSE", "CONTRIBUTING.md", "SECURITY.md", "README.md")

# (glob on the repository path, reason). The first match wins.
EXCLUDE_RULES = (
    ("artifacts/*", "RUN_EVIDENCE: captured third-party pages, provider responses and ledger "
                    "exports of the owner's runs; may carry account and order details"),
    ("exports/*", "RUN_EVIDENCE: ledger exports"),
    ("AGENTS.md", "OPERATOR_NOTES: the owner's operating instructions for coding agents "
                  "(private runtime paths, owner rulings in progress)"),
    (README_TEMPLATE, "EXPORT_TEMPLATE: becomes README.md in the export"),
    ("runs/*", "RUN_EVIDENCE: research-agent run folders"),
    (".claude/*", "OPERATOR_NOTES: local agent worktrees and settings"),
)
ENV_TEMPLATE = ".env.example"

# The project's history and run-log docs (``--exclude-history``, the default). (glob, reason);
# the first match wins. Reference and spec docs (QUICKSTART, CONFIGURATION, API-CONTRACT, the
# runbook, ...) are kept. A doc whose name carries a date (``-2026-``) is a dated run record.
REFERENCE_RULES = "docs/REFERENCE-RULES.md"
RULINGS = "docs/CONTRACT-RESOLUTIONS.md"
_OPERATIONS = ("HISTORY: an operations doc of the owner's reference deployment (its hosting, "
               "research agent and runbook, with its dated runs)")
_RUN_LOG = "HISTORY: a dated run log, checkpoint or validation run of the owner's deployment"
HISTORY_RULES = (
    ("docs/PHASES.md", "HISTORY: the project's phase log"),
    ("docs/packages/*", "HISTORY: per-package build records"),
    ("docs/PROJECT-CHECKPOINTS.md", "HISTORY: project checkpoints"),
    (RULINGS, "HISTORY: the reference deployment's rulings in order, with owner quotes, dates "
              "and account results; summarized in " + REFERENCE_RULES),
    ("docs/*-20[0-9][0-9]-*", _RUN_LOG),
    ("docs/PHASE-*.md", _RUN_LOG),
    ("docs/phase*-*.json", _RUN_LOG),
    ("docs/step4-*.json", _RUN_LOG),
    ("docs/STEP-4-*.md", _RUN_LOG),
    ("docs/PROVING-RUN*", _RUN_LOG),
    ("docs/GATE-*.md", "HISTORY: superseded build gates"),
    ("docs/*-runtime-evidence.json", _RUN_LOG),
    ("docs/frozen-rulings-runtime.json", _RUN_LOG),
    ("docs/jev-adapter-provider-evidence.json", _RUN_LOG),
    ("docs/jev-model-pin.json", _RUN_LOG),
    ("docs/VALIDATION.md", _RUN_LOG),
    ("docs/FROZEN-RULINGS-VALIDATION.md", _RUN_LOG),
    ("docs/JEV-ADAPTER.md", _RUN_LOG),
    ("docs/DATA-RETENTION.md", _RUN_LOG),
    ("docs/BUILD-LOOP.md", _RUN_LOG),
    ("docs/DASHBOARD-SIMPLIFICATION.md", _RUN_LOG),
    ("docs/REVIEW-WORKER-LOCAL.md", _RUN_LOG),
    ("docs/RESEARCH-REPORT-SELECTION.md", _RUN_LOG),
    ("docs/US-JEV-INTEGRATION.md", _RUN_LOG),
    ("docs/TRADING-QUALITY-PLAN.md", "HISTORY: a dated build plan with live results"),
    ("docs/NEXT-BUILD-PLAN.md", "HISTORY: a superseded build plan"),
    ("docs/TREND-CORE-SWITCH-PLAN.md", "HISTORY: a shelved plan"),
    ("docs/FAST-CYCLE-PLAN.md", "HISTORY: a superseded build plan"),
    ("docs/TEST-READINESS-PLAN.md", "HISTORY: a superseded build plan"),
    ("docs/MUSE-JEV-MULTIMARKET.md", "HISTORY: a dated record of owner directions"),
    ("docs/V4.2-BUILD.md", "HISTORY: a dated record of owner instructions"),
    ("docs/RAILWAY-REVIEW-DEPLOYMENT.md", "HISTORY: superseded deployment notes"),
    # The reference deployment's own operations docs: its hosting, research agent and runbook,
    # full of its dated runs. A deployer's choices are in CONFIGURATION.md and QUICKSTART.md.
    ("docs/MUSE-CONNECTION.md", _OPERATIONS),
    ("docs/OPERATIONS-RUNBOOK.md", _OPERATIONS),
    ("docs/RAILWAY-DEPLOYMENT.md", _OPERATIONS),
    ("docs/MANAGED-RUNTIME.md", _OPERATIONS),
)
# A link named by its file to one of these points at the deployer's guide instead.
REDIRECTS = {path: "docs/CONFIGURATION.md" for path, reason in HISTORY_RULES
             if reason == _OPERATIONS}

# Scrubs: (name, pattern, replacement, reason).
EXAMPLE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.test",
                         "example.invalid", "invalid.test")
SCRUBS = (
    ("home_path", re.compile(r"/Users/[A-Za-z0-9._-]+/"), "~/",
     "PERSONAL_DETAIL: the owner's home-folder path"),
    ("private_deployment_domain",
     re.compile(r"\b[a-z0-9-]+-production-[0-9a-f]{4}\.up\.railway\.app\b"),
     "<your-service>.up.railway.app", "PRIVATE_DEPLOYMENT: the owner's live public domains"),
)
# Not after ":" -- a ``user:password@host`` URL is a credential for the secret scan, not an
# address to scrub away silently.
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+:/-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")

# Owner-specific terms (a login, a GitHub account, ...) are never written in this script, which
# is itself exported: they come from a private terms file outside the repository
# (``--private-terms``, default PRIVATE_TERMS) and from this machine (the OS login), as
# ``{"terms": [{"name": ..., "literal": ..., "replacement": ...}]}``. Each literal is scrubbed
# (whole word) and must not remain.
PRIVATE_TERMS = DEFAULT_OUT_ROOT / "private-terms.json"


def private_terms(path=None, *, login=None):
    """``[(name, literal, replacement)]``: the terms file's entries, then the OS login."""
    import getpass

    terms = []
    path = Path(path) if path is not None else PRIVATE_TERMS
    if path.exists():
        try:
            document = json.loads(path.read_text())
            for item in document["terms"]:
                name, literal = item["name"], item["literal"]
                if not (re.fullmatch(r"[a-z_]{3,40}", name) and isinstance(literal, str)
                        and len(literal) >= 3):
                    raise ValueError
                terms.append((name, literal, str(item.get("replacement", "<owner>"))))
        except (OSError, ValueError, KeyError, TypeError):
            raise ExportError("EXPORT_PRIVATE_TERMS_INVALID") from None
    login = getpass.getuser() if login is None else login
    if login and len(login) >= 3 and login not in {"root", "runner", "user"}:
        terms.append(("owner_login", login, "<owner>"))
    return terms


def term_pattern(literal):
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(literal) + r"(?![A-Za-z0-9_])")


# Personal details that must not remain after the scrubs (checked on the export).
PERSONAL_PATTERNS = {
    "home_path": re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    "private_deployment_domain":
        re.compile(r"\b[a-z0-9-]+-production-[0-9a-f]{4}\.up\.railway\.app\b"),
    "railway_project_id": re.compile(
        r"(?i)\b(?:project|environment|service)[_ -]?id\b[\s:=\"'`]+"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
}


def _secret_patterns():
    sys.path.insert(0, str(ROOT / "src"))
    from catalyst_lab.audit import CREDENTIAL_PATTERNS

    extra = {
        "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "github_token": re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
        "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "stripe_live_key": re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}"),
        "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "alpaca_live_key_id": re.compile(r"(?<![A-Z0-9])AK[A-Z0-9]{18}(?![A-Z0-9])"),
        "anthropic_or_openai_key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}"),
        "database_url_password": re.compile(
            r"\bpostgres(?:ql)?://[^\s:/@'\"]+:([^\s@'\"$<{]{6,})@[^\s'\"]+"),
        "generic_secret_assignment": re.compile(
            r"(?i)\b(?:secret|password|passwd|api[_-]?key|token)\b[A-Za-z_]*[ \t]*[=:][ \t]*"
            r"['\"]([A-Za-z0-9/+_=.-]{24,})['\"]"),
    }
    return {**CREDENTIAL_PATTERNS, **extra}


# A match whose text (or line) carries one of these is a documented fixture or placeholder.
FIXTURE_MARKERS = ("fixture", "test", "fake", "example", "placeholder", "replace", "required",
                   "dummy", "sample", "xxxx", "aaaa", "0000", "redact", "not-a-real", "invalid")
# Outside tests/, only these documentation placeholders are allowed.
DOC_MARKERS = ("placeholder", "example", "replace", "required", "<", "redact")


def prohibited_literal():
    sys.path.insert(0, str(ROOT / "src"))
    from catalyst_lab.config import PAPER_ENDPOINT

    return PAPER_ENDPOINT.replace("paper-", "")


class ExportError(RuntimeError):
    pass


# --- reading the committed tree ------------------------------------------------------------------


def committed_files(repo, ref):
    """``{path: bytes}`` of every blob in ``ref``'s tree (no symlinks, no submodules)."""
    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", ref],
                            capture_output=True, check=True).stdout
    entries = []
    for item in listed.decode().split("\0"):
        if not item:
            continue
        meta, path = item.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob" or mode == "120000":
            raise ExportError(f"UNSUPPORTED_TREE_ENTRY {path}")
        entries.append((path, sha, mode))
    batch = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                           input="".join(f"{sha}\n" for _, sha, _ in entries).encode(),
                           capture_output=True, check=True).stdout
    files, at = {}, 0
    for path, _, mode in entries:
        header_end = batch.index(b"\n", at)
        size = int(batch[at:header_end].split()[2])
        start = header_end + 1
        files[path] = (batch[start:start + size], mode == "100755")
        at = start + size + 1
    return files


def resolve_ref(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref + "^{commit}"],
                          capture_output=True, check=True, text=True).stdout.strip()


# --- the plan ------------------------------------------------------------------------------------


def history_reason(path):
    for pattern, reason in HISTORY_RULES:
        if fnmatch(path, pattern):
            return reason
    return None


def exclusion_reason(path, data, *, exclude_history=False):
    for pattern, reason in EXCLUDE_RULES:
        if fnmatch(path, pattern):
            return reason
    if exclude_history and (reason := history_reason(path)):
        return reason
    name = path.rsplit("/", 1)[-1]
    if name.startswith(".env"):
        if path == ENV_TEMPLATE and names_only(data):
            return None
        return "SECRETS: environment files are never exported"
    return None


def names_only(data):
    try:
        lines = data.decode().splitlines()
    except UnicodeError:
        return False
    return bool(lines) and all(re.fullmatch(r"[A-Z][A-Z0-9_]*=", line) for line in lines)


def scrub(text, terms=()):
    """``(text, {scrub name: count})`` after every scrub, the private terms and the e-mail
    rule."""
    counts = {}
    for name, pattern, replacement, _ in SCRUBS:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    for name, literal, replacement in terms:
        text, n = term_pattern(literal).subn(replacement, text)
        if n:
            counts[name] = counts.get(name, 0) + n

    def email(match):
        domain = match.group(0).rsplit("@", 1)[1].lower()
        if any(domain == d or domain.endswith("." + d) for d in EXAMPLE_EMAIL_DOMAINS):
            return match.group(0)
        if domain.endswith((".alpaca.markets", ".railway.app", "hc-ping.com")):
            return match.group(0)  # user:password@host URL fixtures, not addresses.
        counts["email"] = counts.get("email", 0) + 1
        return "<email>"

    text = EMAIL.sub(email, text)
    return text, counts


_PATH_LABEL = re.compile(r"`?[A-Za-z0-9_./-]+\.(?:md|json)/?`?|`?[A-Za-z0-9_./-]+/`?")
_MD_LINK = re.compile(r"(!?)\[([^\]\n]*)\]\(([^)\s]+)\)")


def _resolve(source, target):
    """The repository path a relative link in ``source`` points at (``None``: not a file
    link)."""
    if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith(("#", "/")):
        return None
    parts = source.split("/")[:-1]
    for part in target.split("#", 1)[0].split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _relative(source, path):
    return os.path.relpath(path, os.path.dirname(source) or ".").replace(os.sep, "/")


HISTORY_ONLY = re.compile(r"<!--\s*history-only\s*-->.*?<!--\s*/history-only\s*-->", re.S)


def scrub_history_links(path, text, excluded):
    """``(text, count)``: in a kept Markdown file, a ``<!-- history-only -->`` ...
    ``<!-- /history-only -->`` span is removed, a link to an excluded history doc becomes its
    text (an image is dropped), and the rulings point at their summary instead."""
    text, count = HISTORY_ONLY.subn("", text)

    def link(match):
        nonlocal count
        image, label, target = match.groups()
        resolved = _resolve(path, target)
        if resolved is None or not (resolved in excluded
                                    or any(e.startswith(resolved.rstrip("/") + "/")
                                           for e in excluded)):
            return match.group(0)
        count += 1
        to_summary = f"({_relative(path, REFERENCE_RULES)})"
        if resolved == RULINGS:
            return f"[{label.replace('CONTRACT-RESOLUTIONS', 'REFERENCE-RULES')}]{to_summary}"
        if image:
            return ""
        if resolved in REDIRECTS and _PATH_LABEL.fullmatch(label):
            target = REDIRECTS[resolved]
            name = target.rsplit("/", 1)[-1]
            prefix = "docs/" if label.strip("`").startswith("docs/") else ""
            return f"[{prefix}{name}]({_relative(path, target)})"
        if resolved.startswith("docs/packages/") and _PATH_LABEL.fullmatch(label):
            # A package record named by its file: its rules are summarized in the reference.
            prefix = "docs/" if label.strip("`").startswith("docs/") else ""
            return f"[{prefix}REFERENCE-RULES.md]{to_summary}"
        return label

    text = _MD_LINK.sub(link, text)
    if RULINGS not in excluded:
        return text, count
    text, n = re.subn(r"(?<![A-Za-z0-9_/.-])(docs/)?CONTRACT-RESOLUTIONS\.md",
                      lambda m: (m.group(1) or "") + "REFERENCE-RULES.md", text)
    return text, count + n


_PACKAGE_RECORD = re.compile(r"(?<![A-Za-z0-9_./-])(?:docs/)?packages/[a-z0-9-]+\.md")


def history_mentions(text, excluded_names):
    return sum(len(_PACKAGE_RECORD.findall(text)) if name == "packages/" else
               len(re.findall(r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])",
                              text))
               for name in excluded_names)


# --- scans ---------------------------------------------------------------------------------------


def _line_of(text, index):
    return text.count("\n", 0, index) + 1


def allowed_fixture(rule, path, line_text, matched):
    """A fixture marker on the line: allowed in ``tests/``; elsewhere only a documentation
    placeholder (``DOC_MARKERS``) is."""
    lowered = (line_text + " " + matched).lower()
    if path.startswith("tests/"):
        return any(marker in lowered for marker in FIXTURE_MARKERS)
    return any(marker in lowered for marker in DOC_MARKERS)


def scan_tree(root, *, secret_patterns=None, literal=None, terms=()):
    """``{"prohibited_literal": [...], "secrets": [...], "personal": [...],
    "allowed_fixtures": n}`` over every file under ``root``; values are never recorded."""
    secret_patterns = secret_patterns or _secret_patterns()
    literal = (literal or prohibited_literal()).encode()
    found = {"prohibited_literal": [], "secrets": [], "personal": [], "allowed_fixtures": 0,
             "files_scanned": 0}
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == MANIFEST:
            continue
        data = path.read_bytes()
        found["files_scanned"] += 1
        if literal in data:
            found["prohibited_literal"].append(rel)
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            continue
        lines = text.splitlines()
        for rule, pattern in secret_patterns.items():
            for match in pattern.finditer(text):
                line_no = _line_of(text, match.start())
                line_text = lines[line_no - 1] if line_no - 1 < len(lines) else ""
                if allowed_fixture(rule, rel, line_text, match.group(0)):
                    found["allowed_fixtures"] += 1
                    continue
                found["secrets"].append({"file": rel, "line": line_no, "rule": rule})
        personal = [*PERSONAL_PATTERNS.items(),
                    *((name, term_pattern(term)) for name, term, _ in terms)]
        for rule, pattern in personal:
            for match in pattern.finditer(text):
                found["personal"].append({"file": rel, "line": _line_of(text, match.start()),
                                          "rule": rule})
        for match in EMAIL.finditer(text):
            domain = match.group(0).rsplit("@", 1)[1].lower()
            if not (any(domain == d or domain.endswith("." + d) for d in EXAMPLE_EMAIL_DOMAINS)
                    or domain.endswith((".alpaca.markets", ".railway.app", "hc-ping.com"))):
                found["personal"].append({"file": rel, "line": _line_of(text, match.start()),
                                          "rule": "email"})
    return found


def scan_failed(found):
    return bool(found["prohibited_literal"] or found["secrets"] or found["personal"])


# --- the export ----------------------------------------------------------------------------------


def check_out_dir(out, repo):
    out = Path(out).expanduser().resolve()
    if out == repo or out.is_relative_to(repo):
        raise ExportError("EXPORT_OUT_INSIDE_REPOSITORY")
    if out.exists() and any(out.iterdir()):
        raise ExportError("EXPORT_OUT_NOT_EMPTY")
    return out


def export(repo=ROOT, out=None, *, ref="HEAD", now=None, files=None, readme=None, terms=None,
           exclude_history=True):
    """Write the export and return the manifest (a dict). ``files`` and ``readme`` replace the
    committed tree and the README template, ``terms`` the private terms (tests).
    ``exclude_history`` (default) leaves the project's history docs out."""
    terms = private_terms() if terms is None else list(terms)
    repo = Path(repo).resolve()
    now = now or datetime.now(UTC)
    out = check_out_dir(out or DEFAULT_OUT_ROOT / now.date().isoformat(), repo)
    commit = resolve_ref(repo, ref) if files is None else "FIXTURE"
    files = committed_files(repo, ref) if files is None else files
    if readme is None:
        if README_TEMPLATE not in files:
            raise ExportError("EXPORT_README_TEMPLATE_MISSING")
        readme = files[README_TEMPLATE][0]
    included, excluded, scrubbed = {}, [], []
    history = {p for p, (d, _) in files.items()
               if exclude_history and exclusion_reason(p, d, exclude_history=True)
               and not exclusion_reason(p, d)}
    if RULINGS in history and REFERENCE_RULES not in files:
        raise ExportError("EXPORT_REFERENCE_RULES_MISSING")
    history_names = sorted({"packages/" if p.startswith("docs/packages/")
                            else p.rsplit("/", 1)[-1] for p in history})
    links_rewritten, mentions_left = [], []
    for path, (data, executable) in sorted(files.items()):
        reason = exclusion_reason(path, data, exclude_history=exclude_history)
        if reason:
            excluded.append({"path": path, "reason": reason})
            continue
        if path == "README.md":
            excluded.append({"path": "README.md (repository)",
                             "reason": "REPLACED: the project's own README is replaced by the "
                                       "newcomer README (" + README_TEMPLATE + ")"})
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            included[path] = (data, executable)
            continue
        text, counts = scrub(text, terms)
        if counts:
            scrubbed.append({"path": path, "scrubs": counts})
        if history and path.endswith(".md"):
            text, n = scrub_history_links(path, text, history)
            if n:
                links_rewritten.append({"path": path, "links": n})
        if history and (n := history_mentions(text, history_names)):
            mentions_left.append({"path": path, "mentions": n})
        included[path] = (text.encode("utf-8"), executable)
    if history:
        text, n = scrub_history_links("README.md", readme.decode("utf-8"), history)
        readme = text.encode("utf-8")
        if n:
            links_rewritten.append({"path": "README.md", "links": n})
    included["README.md"] = (readme, False)
    missing = [name for name in REQUIRED_FILES if name not in included]
    if missing:
        raise ExportError("EXPORT_REQUIRED_FILE_MISSING " + ",".join(missing))
    out.mkdir(parents=True, exist_ok=True)
    for path, (data, executable) in included.items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o755 if executable else 0o644)
    found = scan_tree(out, terms=terms)
    manifest = {
        "version": VERSION, "generated_at": now.isoformat(), "source_ref": ref,
        "source_commit": commit, "out": str(out),
        "files_included": len(included),
        "files": {p: hashlib.sha256(d).hexdigest() for p, (d, _) in sorted(included.items())},
        "excluded": excluded,
        "excluded_by_reason": _count(e["reason"].split(":")[0] for e in excluded),
        "scrubbed": scrubbed,
        "scrub_rules": [{"name": n, "reason": r} for n, _, _, r in SCRUBS]
        + [{"name": "email", "reason": "PERSONAL_DETAIL: e-mail addresses outside the "
                                       "reserved example domains"}]
        + [{"name": name, "reason": "PERSONAL_DETAIL: a private term (its value is not "
                                    "recorded)"} for name in sorted({n for n, _, _ in terms})],
        "exclude_history": bool(exclude_history),
        "history_excluded": len(history),
        "history_rules": ([{"glob": g, "reason": r} for g, r in HISTORY_RULES]
                          if exclude_history else []),
        "history_links_rewritten": links_rewritten,
        "history_mentions_left": mentions_left,
        "scans": found, "result": "FAILED" if scan_failed(found) else "PASSED",
        "published": False,
        "note": "Prepared locally only: nothing was pushed or published. Publishing is the "
                "owner's separate decision.",
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def _count(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scripts/oss_export.py",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path,
                        help="export folder (default ~/.local/share/catalyst-oss-export/<date>)")
    parser.add_argument("--ref", default="HEAD", help="the commit to export (default HEAD)")
    history = parser.add_mutually_exclusive_group()
    history.add_argument("--exclude-history", dest="exclude_history", action="store_true",
                         default=True, help="leave the project's history and run-log docs out "
                                            "(the default for the public export)")
    history.add_argument("--keep-history", dest="exclude_history", action="store_false",
                         help="keep the history docs (scrubbed), as OSS_EXPORT_V1 first did")
    parser.add_argument("--private-terms", type=Path,
                        help=f"owner-specific terms to scrub (default {PRIVATE_TERMS})")
    args = parser.parse_args(argv)
    try:
        manifest = export(ROOT, args.out, ref=args.ref,
                          terms=private_terms(args.private_terms),
                          exclude_history=args.exclude_history)
    except (ExportError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"result": "REFUSED", "code": str(exc)}))
        return 2
    scans = manifest["scans"]
    print(json.dumps({
        "result": manifest["result"], "out": manifest["out"],
        "source_commit": manifest["source_commit"], "files_included": manifest["files_included"],
        "files_excluded": len(manifest["excluded"]),
        "excluded_by_reason": manifest["excluded_by_reason"],
        "files_scrubbed": len(manifest["scrubbed"]),
        "exclude_history": manifest["exclude_history"],
        "history_excluded": manifest["history_excluded"],
        "history_links_rewritten": sum(e["links"] for e in manifest["history_links_rewritten"]),
        "files_with_history_mentions_left": len(manifest["history_mentions_left"]),
        "scans": {"files_scanned": scans["files_scanned"],
                  "prohibited_literal": scans["prohibited_literal"],
                  "secrets": scans["secrets"], "personal": scans["personal"],
                  "allowed_fixtures": scans["allowed_fixtures"]},
        "published": False}, indent=1))
    return 1 if manifest["result"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())

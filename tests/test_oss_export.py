"""``scripts/oss_export.py`` (``OSS_EXPORT_V1``, package oss-packaging): exclusions, scrubs and the
safety scans, on temporary trees and a temporary git repository. Nothing is pushed anywhere; the
script has no push or remote code at all.

Credential- and scrub-shaped strings are assembled at run time, so neither the repository's hygiene
scans nor the exporter's scrubs ever see one in this file (the export keeps these tests intact).
"""

import importlib.util
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalyst_lab.config import PAPER_ENDPOINT

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("oss_export", ROOT / "scripts" / "oss_export.py")
oss_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oss_export)
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
README = b"# Newcomer README\n"
BASE = {
    "LICENSE": b"MIT License\n",
    "CONTRIBUTING.md": b"# Contributing\n",
    "SECURITY.md": b"# Security\n",
    "README.md": b"# The project's own README\n",
    oss_export.README_TEMPLATE: README,
    "src/catalyst_lab/app.py": b"print('paper only')\n",
}


def tree(**extra):
    files = {**BASE, **{k.replace("__", "/"): v for k, v in extra.items()}}
    return {path: (data, False) for path, data in files.items()}


# Fixture private terms (the real ones live in a file outside the repository).
TERMS = [("github_account", "octo-fixture", "<owner>"), ("owner_login", "fixturelogin", "<owner>")]


def run(tmp_path, files, name="out"):
    return oss_export.export(ROOT, tmp_path / name, files=files, readme=README, now=NOW,
                             terms=TERMS)


def test_a_clean_tree_is_exported_with_its_manifest(tmp_path):
    manifest = run(tmp_path, tree())
    out = tmp_path / "out"
    assert manifest["result"] == "PASSED" and manifest["published"] is False
    assert (out / "README.md").read_bytes() == README
    assert (out / "LICENSE").exists() and (out / "SECURITY.md").exists()
    assert not (out / oss_export.README_TEMPLATE).exists()
    assert set(manifest["files"]) == {"LICENSE", "CONTRIBUTING.md", "SECURITY.md", "README.md",
                                      "src/catalyst_lab/app.py"}
    saved = json.loads((out / oss_export.MANIFEST).read_text())
    assert saved["files_included"] == 5 and saved["scans"]["prohibited_literal"] == []
    replaced = [e for e in manifest["excluded"] if e["path"].startswith("README.md")]
    assert replaced and replaced[0]["reason"].startswith("REPLACED")


def test_evidence_operator_notes_and_environment_files_are_excluded_with_reasons(tmp_path):
    files = tree(**{
        "artifacts__run-1__events.json": b"{}",
        "exports__ledger.jsonl": b"{}",
        "AGENTS.md": b"# owner notes\n",
        ".env": b"TYPESAFE_API_KEY=x\n",
        ".env.paper": b"APCA_API_KEY_ID=x\n",
        "deploy__.env.local": b"X=1\n",
        ".env.example": b"DATABASE_URL=\nTYPESAFE_API_KEY=\n",
    })
    manifest = run(tmp_path, files)
    reasons = {e["path"]: e["reason"].split(":")[0] for e in manifest["excluded"]}
    assert reasons["artifacts/run-1/events.json"] == "RUN_EVIDENCE"
    assert reasons["exports/ledger.jsonl"] == "RUN_EVIDENCE"
    assert reasons["AGENTS.md"] == "OPERATOR_NOTES"
    assert reasons[".env"] == reasons[".env.paper"] == reasons["deploy/.env.local"] == "SECRETS"
    assert ".env.example" in manifest["files"]  # A names-only template is kept.
    valued = run(tmp_path, tree(**{".env.example": b"DATABASE_URL=host\n"}), name="out2")
    assert ".env.example" not in valued["files"]
    assert manifest["excluded_by_reason"]["SECRETS"] == 3


def test_personal_details_are_scrubbed_and_counted(tmp_path):
    home = "/" + "Users/someone/"
    files = tree(**{
        "docs__RUN.md": (f"Saved at {home}.local/share/run.json and {home}x.\n"
                         "Domain https://trader-production-" + "06b6.up.railway.app.\n"
                         "Repo https://github.com/octo-fixture/x (account `octo-fixture`).\n"
                         "Run by fixturelogin on fixturelogin-mac.\n"
                         "Mail me" + "@private-mail.org; keep owner@example.com.\n").encode(),
    })
    manifest = run(tmp_path, files)
    text = (tmp_path / "out" / "docs" / "RUN.md").read_text()
    assert home not in text and "~/.local/share/run.json" in text
    assert "<your-service>.up.railway.app" in text and "github.com/<owner>/x" in text
    assert "account `<owner>`" in text and "<email>" in text and "owner@example.com" in text
    assert "Run by <owner> on <owner>-mac." in text
    [scrubbed] = manifest["scrubbed"]
    assert scrubbed["path"] == "docs/RUN.md"
    assert scrubbed["scrubs"] == {"home_path": 2, "private_deployment_domain": 1,
                                  "github_account": 2, "owner_login": 2, "email": 1}
    assert manifest["result"] == "PASSED"
    # The manifest names the private terms, never their values.
    assert "octo-fixture" not in json.dumps(manifest) and "fixturelogin" not in json.dumps(
        manifest)


def planted():
    """Credential shapes and the prohibited literal, assembled so no literal is in this file."""
    return {
        "aws_access_key_id": "AKIA" + "Q7" * 8,
        "github_token": "ghp_" + "a1B2" * 9,
        "private_key_block": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "database_url_password": "postgresql://app:" + "s3cretpass" + "@db.internal/x",
        "alpaca_key_id": "PK" + "Z9" * 9,
        "slack_token": "xoxb-" + "1234567890-abcdef",
    }


@pytest.mark.parametrize("rule", sorted(planted()))
def test_a_secret_outside_tests_fails_the_export_and_is_never_recorded(tmp_path, rule):
    value = planted()[rule]
    manifest = run(tmp_path, tree(**{"src__leak.py": f"VALUE = '{value}'\n".encode()}))
    assert manifest["result"] == "FAILED"
    found = manifest["scans"]["secrets"]
    assert {"file": "src/leak.py", "line": 1, "rule": rule} in found
    assert value not in json.dumps(manifest)
    assert value not in (tmp_path / "out" / oss_export.MANIFEST).read_text()


def test_the_prohibited_literal_fails_the_export_even_in_a_binary_file(tmp_path):
    literal = PAPER_ENDPOINT.replace("paper-", "").encode()
    manifest = run(tmp_path, tree(**{"src__static__font.woff2": b"\x00\xff" + literal + b"\x00"}))
    assert manifest["result"] == "FAILED"
    assert manifest["scans"]["prohibited_literal"] == ["src/static/font.woff2"]


def test_labelled_test_fixtures_are_allowed_only_in_tests(tmp_path):
    fixture = "postgresql://app:" + "fixture-password" + "@db.internal/x"
    manifest = run(tmp_path, tree(**{"tests__test_x.py": f"URL = '{fixture}'\n".encode()}))
    assert manifest["result"] == "PASSED" and manifest["scans"]["allowed_fixtures"] == 1
    leaked = run(tmp_path, tree(**{"src__x.py": f"URL = '{fixture}'\n".encode()}), name="o2")
    assert leaked["result"] == "FAILED"


def test_a_leftover_personal_detail_fails_the_export(tmp_path):
    found = oss_export.scan_tree(_written(tmp_path, "docs/x.md", "login fixturelogin\n"),
                                 terms=TERMS)
    assert found["personal"] == [{"file": "docs/x.md", "line": 1, "rule": "owner_login"}]
    assert oss_export.scan_failed(found)
    mail = oss_export.scan_tree(_written(tmp_path / "m", "a.md", "me" + "@private-mail.org\n"))
    assert mail["personal"] == [{"file": "a.md", "line": 1, "rule": "email"}]


def _written(root, path, text):
    target = root / "tree" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return root / "tree"


def test_private_terms_come_from_a_file_outside_the_repository_and_the_login(tmp_path):
    path = tmp_path / "terms.json"
    path.write_text(json.dumps({"terms": [{"name": "github_account", "literal": "octo-fixture",
                                           "replacement": "<owner>"}]}))
    assert oss_export.private_terms(path, login="fixturelogin") == [
        ("github_account", "octo-fixture", "<owner>"), ("owner_login", "fixturelogin", "<owner>")]
    assert oss_export.private_terms(tmp_path / "absent.json", login="root") == []
    path.write_text('{"terms": [{"name": "X", "literal": "ab"}]}')
    with pytest.raises(oss_export.ExportError, match="EXPORT_PRIVATE_TERMS_INVALID"):
        oss_export.private_terms(path, login="")
    assert oss_export.PRIVATE_TERMS.parent == oss_export.DEFAULT_OUT_ROOT


def test_the_output_folder_must_be_outside_the_repository_and_empty(tmp_path):
    with pytest.raises(oss_export.ExportError, match="EXPORT_OUT_INSIDE_REPOSITORY"):
        oss_export.export(ROOT, ROOT / "exports" / "x", files=tree(), readme=README, now=NOW,
                          terms=TERMS)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "file").write_text("x")
    with pytest.raises(oss_export.ExportError, match="EXPORT_OUT_NOT_EMPTY"):
        oss_export.export(ROOT, tmp_path / "busy", files=tree(), readme=README, now=NOW,
                          terms=TERMS)
    missing = tree()
    del missing["SECURITY.md"]
    with pytest.raises(oss_export.ExportError, match="EXPORT_REQUIRED_FILE_MISSING SECURITY.md"):
        oss_export.export(ROOT, tmp_path / "o3", files=missing, readme=README, now=NOW,
                          terms=TERMS)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


def test_the_export_reads_the_committed_tree_never_the_working_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    for path, data in {**BASE, "run.sh": b"#!/bin/sh\n"}.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (repo / "run.sh").chmod(0o755)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "fixture")
    (repo / ".env").write_text("TYPESAFE_API_KEY=not-committed\n")  # Untracked: never read.
    (repo / "LICENSE").write_text("changed but not committed\n")
    manifest = oss_export.export(repo, tmp_path / "out", now=NOW, terms=TERMS)
    out = tmp_path / "out"
    assert manifest["result"] == "PASSED"
    assert manifest["source_commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert not (out / ".env").exists() and (out / "LICENSE").read_text() == "MIT License\n"
    assert (out / "README.md").read_bytes() == README
    assert (out / "run.sh").stat().st_mode & 0o111


# --- history docs (the owner's 2026-10-03 decision: the public export leaves history out) --------

HISTORY_DOCS = ("docs/PHASES.md", "docs/packages/cloud.md", "docs/PROJECT-CHECKPOINTS.md",
                "docs/CONTRACT-RESOLUTIONS.md", "docs/REAL-WORLD-SESSION-2026-09-19.md",
                "docs/images/moves-2026-09-28.svg", "docs/PHASE-4.md", "docs/phase-4-runtime.json",
                "docs/phase41-runtime-evidence.json", "docs/step4-local-proof.json",
                "docs/STEP-4-REPORT.md", "docs/PROVING-RUN-2026-09-18.md",
                "docs/TRADING-QUALITY-PLAN.md", "docs/NEXT-BUILD-PLAN.md",
                "docs/TREND-CORE-SWITCH-PLAN.md", "docs/FAST-CYCLE-PLAN.md",
                "docs/GATE-1-RELIABILITY-PROPOSAL.md", "docs/jev-adapter-provider-evidence.json",
                "docs/VALIDATION.md", "docs/MUSE-CONNECTION.md", "docs/OPERATIONS-RUNBOOK.md",
                "docs/RAILWAY-DEPLOYMENT.md", "docs/MANAGED-RUNTIME.md")
KEPT_DOCS = ("docs/QUICKSTART.md", "docs/CONFIGURATION.md", "docs/API-CONTRACT.md",
             "docs/JEV-VALIDATION-PROTOCOL.md",
             "docs/images/dashboard.png")


def history_tree():
    files = tree()
    files.update({path: (b"# history\n", False) for path in HISTORY_DOCS})
    files.update({path: (b"# reference\n", False) for path in KEPT_DOCS})
    files["docs/REFERENCE-RULES.md"] = (b"# rules\n", False)
    return files


def test_history_docs_are_excluded_by_default_and_reference_docs_kept(tmp_path):
    manifest = run(tmp_path, history_tree())
    assert manifest["result"] == "PASSED" and manifest["exclude_history"] is True
    reasons = {e["path"]: e["reason"] for e in manifest["excluded"]}
    for path in HISTORY_DOCS:
        assert reasons[path].startswith("HISTORY"), path
        assert not (tmp_path / "out" / path).exists()
    for path in (*KEPT_DOCS, "docs/REFERENCE-RULES.md"):
        assert path in manifest["files"], path
    assert manifest["history_excluded"] == len(HISTORY_DOCS)
    assert {r["glob"] for r in manifest["history_rules"]} >= {"docs/PHASES.md", "docs/packages/*"}
    assert "docs/REFERENCE-RULES.md" in reasons["docs/CONTRACT-RESOLUTIONS.md"]


def test_keep_history_keeps_them_and_the_command_line_defaults_to_excluding(tmp_path,
                                                                            monkeypatch):
    files = history_tree()
    manifest = oss_export.export(ROOT, tmp_path / "out", files=files, readme=README, now=NOW,
                                 terms=TERMS, exclude_history=False)
    assert manifest["exclude_history"] is False and manifest["history_rules"] == []
    assert all(path in manifest["files"] for path in HISTORY_DOCS)
    seen = {}
    monkeypatch.setattr(oss_export, "export",
                        lambda *a, **k: seen.update(k) or (_ for _ in ()).throw(
                            oss_export.ExportError("STOP")))
    assert oss_export.main(["--out", str(tmp_path / "x")]) == 2
    assert seen["exclude_history"] is True
    oss_export.main(["--out", str(tmp_path / "x"), "--keep-history"])
    assert seen["exclude_history"] is False


def test_the_rulings_need_their_summary(tmp_path):
    files = tree()
    files["docs/CONTRACT-RESOLUTIONS.md"] = (b"# rulings\n", False)
    with pytest.raises(oss_export.ExportError, match="EXPORT_REFERENCE_RULES_MISSING"):
        run(tmp_path, files)


def test_links_to_excluded_history_are_rewritten_in_kept_markdown(tmp_path):
    guide = (b"# Guide\n\nSee [the phases](PHASES.md#today) and [rules](CONTRACT-RESOLUTIONS.md)."
             b"\n![chart](images/moves-2026-09-28.svg)\n![page](images/dashboard.png)\n"
             b"Records: [packages](packages/) and [cloud](packages/cloud.md).\n"
             b"Rule: [`docs/packages/cloud.md`](packages/cloud.md#x).\n"
             b"Ops: [the runbook](OPERATIONS-RUNBOOK.md), "
             b"[RAILWAY-DEPLOYMENT.md](RAILWAY-DEPLOYMENT.md).\n"
             b"Kept: [quick](QUICKSTART.md), [web](https://example.com/PHASES.md).\n"
             b"Also docs/CONTRACT-RESOLUTIONS.md.\n"
             b"Summary<!-- history-only -->, and the\nlog<!-- /history-only -->.\n")
    files = history_tree()
    files["docs/GUIDE.md"] = (guide, False)
    files["src/catalyst_lab/x.py"] = (b"# see docs/PHASES.md and docs/packages/cloud.md\n", False)
    readme = b"[rules](docs/CONTRACT-RESOLUTIONS.md) [log](docs/PHASES.md)\n"
    manifest = oss_export.export(ROOT, tmp_path / "out", files=files, readme=readme, now=NOW,
                                 terms=TERMS)
    text = (tmp_path / "out" / "docs" / "GUIDE.md").read_text()
    assert text == ("# Guide\n\nSee the phases and [rules](REFERENCE-RULES.md).\n\n"
                    "![page](images/dashboard.png)\nRecords: packages and cloud.\n"
                    "Rule: [docs/REFERENCE-RULES.md](REFERENCE-RULES.md).\n"
                    "Ops: the runbook, [CONFIGURATION.md](CONFIGURATION.md).\n"
                    "Kept: [quick](QUICKSTART.md), [web](https://example.com/PHASES.md).\n"
                    "Also docs/REFERENCE-RULES.md.\nSummary.\n")
    assert (tmp_path / "out" / "README.md").read_text() == (
        "[rules](docs/REFERENCE-RULES.md) log\n")
    rewritten = {e["path"]: e["links"] for e in manifest["history_links_rewritten"]}
    assert rewritten["docs/GUIDE.md"] == 10 and rewritten["README.md"] == 2
    # Code is never rewritten; its mentions are counted.
    assert (tmp_path / "out" / "src/catalyst_lab/x.py").read_text().startswith("# see docs/PH")
    left = {e["path"]: e["mentions"] for e in manifest["history_mentions_left"]}
    assert left["src/catalyst_lab/x.py"] == 2


def test_history_only_markers_in_the_repository_are_balanced():
    for path in [*ROOT.joinpath("docs").glob("*.md"), *ROOT.joinpath("research_agent").glob("*.md"),
                 ROOT / "scripts" / "oss" / "README.public.md"]:
        if not path.exists():  # The README template is not shipped in the export.
            continue
        text = path.read_text()
        opens = len(re.findall(r"<!--\s*history-only\s*-->", text))
        assert opens == len(re.findall(r"<!--\s*/history-only\s*-->", text)), path
        assert "history-only" not in oss_export.HISTORY_ONLY.sub("", text), path

"""Incremental audit checkpoints (plan 4.7): disposable PostgreSQL and tmp files only.

Every checkpoint after the first holds only the events after the previous checkpoint's
head and chains to it; a weekly full export re-anchors the chain; the files verify end to
end and a missing or altered checkpoint fails. A checkpoint whose text matches a
credential shape is refused before any file is written. Fixture evidence only.
"""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalyst_lab.audit import ZERO_HASH, credential_findings, verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.managed_ops import export_checkpoint, restore_audit_copy, verify_checkpoint
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def append(repo, count, **payload):
    with repo.connect() as conn:
        for index in range(count):
            system_event(repo, conn, "AUDIT_VOLUME_FIXTURE",
                         {"source": "LAB_FIXTURE", "index": index, **payload})


def head(repo):
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT seq,event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    return row["seq"], row["event_hash"]


def rows_in(path):
    return [json.loads(line) for line in Path(path).read_bytes().splitlines()]


def test_incremental_checkpoints_chain_to_the_previous_head_and_verify_end_to_end(er, tmp_path):
    directory = tmp_path / "audit"
    append(er, 3)
    first = export_checkpoint(er, directory, now=T0)
    assert first["mode"] == "FULL" and first["written"] and first["start_seq"] == 0
    assert first["previous_checkpoint"] is None
    assert (first["last_seq"], first["head_hash"]) == head(er)
    append(er, 4)
    second = export_checkpoint(er, directory, now=T0 + timedelta(hours=1))
    assert second["mode"] == "INCREMENTAL"
    assert (second["start_seq"], second["start_hash"]) == (first["last_seq"], first["head_hash"])
    assert second["event_count"] == 4 and (second["last_seq"], second["head_hash"]) == head(er)
    assert second["previous_checkpoint"] == Path(first["file"]).name
    # Only the new events are in the file, and they verify from the retained head alone.
    assert [row["seq"] for row in rows_in(second["file"])] == list(
        range(first["last_seq"] + 1, second["last_seq"] + 1)
    )
    assert verify_checkpoint(second["file"], expected_head=second["head_hash"])["valid"]
    nothing = export_checkpoint(er, directory, now=T0 + timedelta(hours=2))
    assert nothing["written"] is False and nothing["file"] is None
    assert nothing["event_count"] == 0 and nothing["head_hash"] == second["head_hash"]
    append(er, 2)
    third = export_checkpoint(er, directory, now=T0 + timedelta(hours=3))
    chain = verify_checkpoint(directory, expected_head=head(er)[1])
    assert chain["valid"] and chain["independent_head_verified"]
    assert chain["checkpoint_count"] == 3 and chain["full_exports"] == 1
    assert (chain["last_seq"], chain["head_hash"]) == (third["last_seq"], third["head_hash"])
    # The concatenated segments are exactly the full chain.
    joined = [row for item in (first, second, third) for row in rows_in(item["file"])]
    assert joined == er.export_events()
    assert verify_events(joined, head(er)[1])["event_count"] == third["last_seq"]
    for item in (first, second, third):
        assert os.stat(item["file"]).st_mode & 0o777 == 0o600
        assert item["credential_scan"] == "NO_CREDENTIAL_SHAPED_CONTENT"
    with pytest.raises(ValueError, match="AUDIT_CHECKPOINT_HEAD_MISMATCH"):
        verify_checkpoint(directory, expected_head=second["head_hash"])  # Tail truncated.


def test_weekly_full_export_reanchors_and_must_contain_the_chain(er, tmp_path):
    directory = tmp_path / "audit"
    append(er, 2)
    first = export_checkpoint(er, directory, now=T0)
    append(er, 2)
    daily = export_checkpoint(er, directory, now=T0 + timedelta(days=6))
    assert daily["mode"] == "INCREMENTAL"
    append(er, 1)
    weekly = export_checkpoint(er, directory, now=T0 + timedelta(days=7))
    assert weekly["mode"] == "FULL" and weekly["start_seq"] == 0
    assert weekly["event_count"] == weekly["last_seq"] == head(er)[0]
    append(er, 1)
    after = export_checkpoint(er, directory, now=T0 + timedelta(days=7, hours=1))
    assert after["mode"] == "INCREMENTAL" and after["start_seq"] == weekly["last_seq"]
    forced = export_checkpoint(er, directory, now=T0 + timedelta(days=7, hours=2), full=True)
    assert forced["mode"] == "FULL" and forced["last_seq"] == after["last_seq"]
    chain = verify_checkpoint(directory, expected_head=head(er)[1])
    assert chain["checkpoint_count"] == 5 and chain["full_exports"] == 3
    assert first["mode"] == "FULL"


def test_a_missing_or_altered_incremental_checkpoint_is_detected(er, tmp_path):
    directory = tmp_path / "audit"
    append(er, 2)
    export_checkpoint(er, directory, now=T0)
    append(er, 3)
    middle = export_checkpoint(er, directory, now=T0 + timedelta(hours=1))
    append(er, 2)
    export_checkpoint(er, directory, now=T0 + timedelta(hours=2))
    assert verify_checkpoint(directory)["checkpoint_count"] == 3

    # Tampering: one edited payload with a recomputed file hash still breaks the chain.
    original = Path(middle["file"]).read_bytes()
    rows = rows_in(middle["file"])
    rows[1]["payload_json"]["index"] = 99
    forged = b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in rows)
    manifest_path = Path(middle["file"]).with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_bytes())
    Path(middle["file"]).write_bytes(forged)
    with pytest.raises(ValueError, match="AUDIT_FILE_HASH_MISMATCH"):
        verify_checkpoint(directory)
    manifest["file_sha256"] = hashlib.sha256(forged).hexdigest()
    manifest_path.write_bytes(json.dumps(manifest, sort_keys=True).encode())
    with pytest.raises(ValueError, match="Envelope mismatch"):
        verify_checkpoint(directory)
    Path(middle["file"]).write_bytes(original)
    manifest["file_sha256"] = middle["file_sha256"]
    manifest_path.write_bytes(json.dumps(manifest, sort_keys=True).encode())
    assert verify_checkpoint(directory)["valid"]

    # A gap: the middle segment (file and manifest) is gone.
    moved = tmp_path / "moved"
    moved.mkdir(mode=0o700)
    for source in (Path(middle["file"]), manifest_path):
        source.rename(moved / source.name)
    with pytest.raises(ValueError, match="AUDIT_CHECKPOINT_CHAIN_GAP"):
        verify_checkpoint(directory)
    # A re-chained forgery (the later segment moved onto a different head) fails too.
    for source in moved.iterdir():
        source.rename(directory / source.name)
    later = sorted(directory.glob("*.manifest.json"))[-1]
    forged_manifest = json.loads(later.read_bytes())
    forged_manifest["start_hash"] = "f" * 64
    later.write_bytes(json.dumps(forged_manifest, sort_keys=True).encode())
    with pytest.raises(ValueError):
        verify_checkpoint(directory)


def test_database_that_no_longer_holds_the_previous_head_is_refused(er, tmp_path):
    directory = tmp_path / "audit"
    append(er, 2)
    first = export_checkpoint(er, directory, now=T0)
    manifest_path = Path(first["file"]).with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_bytes())
    manifest["head_hash"] = "e" * 64  # A checkpoint from another ledger.
    manifest_path.write_bytes(json.dumps(manifest, sort_keys=True).encode())
    append(er, 1)
    with pytest.raises(ValueError, match="AUDIT_CHECKPOINT_CHAIN_MISMATCH"):
        export_checkpoint(er, directory, now=T0 + timedelta(hours=1))


def test_credential_shaped_content_is_never_written(er, tmp_path):
    directory = tmp_path / "audit"
    append(er, 1)
    export_checkpoint(er, directory, now=T0)
    # Built at run time so no credential-shaped literal exists in this repository.
    shaped = "PK" + "QWERTYUIOPASDFGHJK"
    assert credential_findings(json.dumps({"k": shaped})) == ["alpaca_key_id"]
    append(er, 1, leaked=shaped)
    before = sorted(p.name for p in directory.iterdir())
    with pytest.raises(ValueError, match="AUDIT_CHECKPOINT_CREDENTIAL_SHAPED_CONTENT"):
        export_checkpoint(er, directory, now=T0 + timedelta(hours=1))
    assert sorted(p.name for p in directory.iterdir()) == before  # Nothing written.
    with pytest.raises(ValueError, match="AUDIT_CHECKPOINT_CREDENTIAL_SHAPED_CONTENT"):
        export_checkpoint(er, tmp_path / "fresh", now=T0, full=True)
    assert not list((tmp_path / "fresh").iterdir())
    # Fixture identifiers and ordinary ledger content are not credential-shaped.
    assert credential_findings("PKFIXTURE000000000001 Bearer fixture-token-" + "x" * 40) == []


def test_incremental_file_restores_and_verifies_from_its_recorded_start(er, tmp_path):
    directory = tmp_path / "audit"
    append(er, 2)
    export_checkpoint(er, directory, now=T0)
    append(er, 2)
    second = export_checkpoint(er, directory, now=T0 + timedelta(hours=1))
    restored = restore_audit_copy(second["file"], tmp_path / "copy" / "segment.jsonl",
                                  expected_head=second["head_hash"])
    assert restored["valid"] and restored["start_seq"] == second["start_seq"]
    assert restored["last_seq"] == second["last_seq"]
    rows = rows_in(second["file"])
    with pytest.raises(ValueError, match="Broken event ordering"):
        verify_events(rows)  # Not a genesis segment.
    assert verify_events(rows, start_seq=second["start_seq"],
                         start_hash=second["start_hash"])["head_hash"] == second["head_hash"]
    with pytest.raises(ValueError, match="previous head hash"):
        verify_events(rows, start_seq=second["start_seq"])
    with pytest.raises(ValueError, match="start hash"):
        verify_events([], start_seq=0, start_hash="a" * 64)
    assert verify_events([], start_seq=0, start_hash=ZERO_HASH)["head_hash"] == ZERO_HASH

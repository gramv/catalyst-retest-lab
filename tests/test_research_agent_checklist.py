"""research_agent.checklist: the research checklist kept outside the app (plan 5c).

Offline only, in temporary owner-only state folders; the default state folder under the
home directory is never touched here.
"""

import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from research_agent import checklist, run

NOW = datetime(2026, 9, 29, 6, 30, tzinfo=UTC)


def movers_doc(day, tags_by_symbol, *, kind="NEW_YORK_DAY"):
    coins = {symbol.split("/")[0]: {"symbol": symbol, "pre_window": {"tags": tags}}
             for symbol, tags in tags_by_symbol.items()}
    return {"schema": checklist.MOVERS_SCHEMA, "window": {"kind": kind, "day": day},
            "coins": coins, "movers": []}


def lessons(*days):
    return {"recent_days": [{"day": day, "mover_share": share,
                             "movers": [{"symbol": s} for s in movers]}
                            for day, share, movers in days]}


def technical_entries(day, *, hits, misses, share="0.1", start=0):
    return [("TECHNICAL", "VOLUME_SPIKE", "Coins with a volume spike",
             {"day": day, "subject": f"C{start + i:03}/USD", "hit": i < hits,
              "source": "movers+recent_days", "day_mover_share": share})
            for i in range(hits + misses)]


def factor_entries(day, *, hits, misses, key="EXCHANGE_LISTING", start=0):
    return [("NEWS_TYPE", key, "Exchange listings",
             {"day": day, "subject": f"MOVER:{day}:C{start + i:03}/USD", "hit": i < hits,
              "source": "postmortem"})
            for i in range(hits + misses)]


# --- The owner-only state folder ----------------------------------------------------------------

def test_state_is_created_owner_only_and_written_whole(tmp_path):
    folder = tmp_path / "muse-state"
    state = checklist.load(folder)
    assert state["patterns"] == {} and stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    path = checklist.save(folder, state)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert sorted(p.name for p in folder.iterdir()) == ["checklist.json"]  # No temp file left.
    assert checklist.load(folder)["schema"] == checklist.CHECKLIST_SCHEMA


def test_a_state_file_others_can_read_or_a_link_is_refused(tmp_path):
    folder = tmp_path / "muse-state"
    path = checklist.save(folder, checklist.empty_state())
    path.chmod(0o644)
    with pytest.raises(checklist.ChecklistError, match="CHECKLIST_STATE_NOT_PRIVATE"):
        checklist.load(folder)
    path.chmod(0o600)
    elsewhere = tmp_path / "elsewhere.json"
    path.rename(elsewhere)
    path.symlink_to(elsewhere)
    with pytest.raises(checklist.ChecklistError, match="CHECKLIST_STATE_UNREADABLE"):
        checklist.load(folder)
    open_folder = tmp_path / "open"
    open_folder.mkdir(mode=0o755)
    open_folder.chmod(0o755)
    with pytest.raises(checklist.ChecklistError, match="CHECKLIST_STATE_DIR_NOT_PRIVATE"):
        checklist.load(open_folder)


# --- TECHNICAL patterns: lift against the app's mover share --------------------------------------

def test_movers_evidence_takes_hits_and_mover_share_from_the_apps_record():
    doc = movers_doc("2026-09-28", {"SOL/USD": ["VOLUME_SPIKE", "NEAR_7D_HIGH"],
                                    "ETH/USD": ["VOLUME_SPIKE"], "BTC/USD": []})
    found, scopes = checklist.movers_evidence(doc, lessons(("2026-09-28", "0.08", ["SOL/USD"])))
    assert scopes == [("TECHNICAL_DAY", "2026-09-28")]  # The day it describes completely.
    entries = {(key, entry["subject"]): entry for _kind, key, _d, entry in found}
    assert set(entries) == {("VOLUME_SPIKE", "SOL/USD"), ("NEAR_7D_HIGH", "SOL/USD"),
                            ("VOLUME_SPIKE", "ETH/USD")}
    assert entries[("VOLUME_SPIKE", "SOL/USD")]["hit"] is True
    assert entries[("VOLUME_SPIKE", "ETH/USD")]["hit"] is False
    assert entries[("VOLUME_SPIKE", "ETH/USD")]["day_mover_share"] == "0.08"


def test_movers_evidence_needs_the_apps_record_of_that_day_and_a_whole_day():
    doc = movers_doc("2026-09-28", {"SOL/USD": ["VOLUME_SPIKE"]})
    with pytest.raises(checklist.ChecklistError, match="CHECKLIST_REALITY_DAY_NOT_RECORDED"):
        checklist.movers_evidence(doc, lessons(("2026-09-27", "0.1", [])))
    unmeasured = lessons(("2026-09-28", "0.1", []))
    unmeasured["recent_days"][0]["mover_share"] = None
    with pytest.raises(checklist.ChecklistError, match="CHECKLIST_NO_MOVER_SHARE"):
        checklist.movers_evidence(doc, unmeasured)
    with pytest.raises(checklist.ChecklistError, match="CHECKLIST_NEEDS_A_NEW_YORK_DAY"):
        checklist.movers_evidence(movers_doc("2026-09-28", {}, kind="LAST_HOURS"),
                                  lessons(("2026-09-28", "0.1", [])))


def test_a_technical_pattern_is_judged_by_lift_not_a_fixed_hit_rate():
    """3 hits in 10 is a 30% hit rate, far under 50%, but 3x the 10% mover share."""
    state = checklist.empty_state()
    report = checklist.apply(state, technical_entries("2026-09-28", hits=3, misses=7), now=NOW,
                             inputs=["movers.json"])
    pattern = state["patterns"]["TECHNICAL:VOLUME_SPIKE"]
    assert (pattern["status"], pattern["measure"], pattern["value"]) == ("ACTIVE", "LIFT", "3.00")
    assert pattern["hit_rate"] == "0.30"
    assert report["status_changes"] == [
        "TECHNICAL:VOLUME_SPIKE: CANDIDATE -> ACTIVE (lift 3.00 over 10 occurrences and 3.00 "
        "over the last 10: at least 2.0)"]


def test_a_technical_pattern_retires_below_lift_1_25_and_can_return():
    state = checklist.empty_state()
    checklist.apply(state, technical_entries("2026-09-26", hits=3, misses=7), now=NOW, inputs=[])
    # The last 10: 1 hit where 1 was expected, lift 1.0.
    checklist.apply(state, technical_entries("2026-09-27", hits=1, misses=9), now=NOW, inputs=[])
    pattern = state["patterns"]["TECHNICAL:VOLUME_SPIKE"]
    assert pattern["status"] == "RETIRED" and pattern["rolling_value"] == "1.00"
    # 3 hits in the next 10: last-10 lift 3.0; overall 7 hits against 3 expected, 2.33.
    checklist.apply(state, technical_entries("2026-09-28", hits=3, misses=7), now=NOW, inputs=[])
    assert pattern["status"] == "ACTIVE" and pattern["value"] == "2.33"
    assert [h["status"] for h in pattern["history"]] == ["CANDIDATE", "ACTIVE", "RETIRED",
                                                          "ACTIVE"]


def test_fewer_than_ten_occurrences_never_activate():
    state = checklist.empty_state()
    checklist.apply(state, technical_entries("2026-09-28", hits=9, misses=0), now=NOW, inputs=[])
    assert state["patterns"]["TECHNICAL:VOLUME_SPIKE"]["status"] == "CANDIDATE"


# --- Factor patterns from post-mortems: hit rate ------------------------------------------------

def test_a_factor_pattern_activates_at_a_50_percent_hit_rate_and_retires_below_40():
    state = checklist.empty_state()
    checklist.apply(state, factor_entries("2026-09-20", hits=5, misses=5), now=NOW, inputs=[])
    pattern = state["patterns"]["NEWS_TYPE:EXCHANGE_LISTING"]
    assert (pattern["status"], pattern["measure"], pattern["value"]) == (
        "ACTIVE", "HIT_RATE", "0.50")
    checklist.apply(state, factor_entries("2026-09-27", hits=3, misses=7, start=10), now=NOW,
                    inputs=[])
    assert pattern["status"] == "RETIRED" and pattern["rolling_value"] == "0.30"


def submissions(*notes):
    """A postmortem-submit.json with one attempt per note: ``[(note_id, [item, ...])]``."""
    return {"schema": checklist.SUBMISSIONS_SCHEMA, "day": "2026-09-28",
            "attempts": [{"at": f"2026-09-29T06:{minute:02}:00+00:00",
                          "notes": [{"note_id": note_id, "items": items}]}
                         for minute, (note_id, items) in enumerate(notes)]}


def sent(subject_id, *, status="ACCEPTED", knowable=True, factors=None, notable=True,
         kind="MOVER"):
    return {"subject_id": subject_id, "subject": {"kind": kind}, "day": "2026-09-28",
            "notable": notable, "cause": "COIN_NEWS", "knowable_before_move": knowable,
            "factors": factors if factors is not None else [
                {"kind": "NEWS_TYPE", "key": "EXCHANGE_LISTING",
                 "description": "A listing on a major exchange"}],
            "status": status, "code": None if status == "ACCEPTED" else "NOT_A_RECORDED_MOVER"}


def test_only_items_the_app_accepted_count_and_hits_are_knowable_true():
    found, scopes, left_out = checklist.submission_evidence(submissions(("n1", [
        sent("MOVER:2026-09-28:SOL/USD", knowable=True),
        sent("MOVER:2026-09-28:ETH/USD", knowable=None),  # Accepted: unknown, not a hit.
        sent("MOVER:2026-09-28:BTC/USD", status="REJECTED"),  # Refused by the app.
        sent("MOVER:2026-09-28:XRP/USD", status=None),  # Never answered.
        sent("TRADE:t1", kind="TRADE", notable=False),  # Not a notable trade.
        sent("TRADE:t2", kind="TRADE", knowable=False),
    ])))
    assert [(entry["subject"], entry["hit"]) for *_rest, entry in found] == [
        ("MOVER:2026-09-28:SOL/USD", True), ("MOVER:2026-09-28:ETH/USD", False),
        ("TRADE:t2", False)]
    assert left_out == ["TRADE:t1"]
    assert ("SUBJECT", "MOVER:2026-09-28:BTC/USD") not in scopes


@pytest.mark.parametrize("factor, code", [
    ({"kind": "TECHNICAL", "key": "VOLUME_SPIKE", "description": "x"}, "must be one of"),
    ({"kind": "NEWS_TYPE", "key": "exchange listing", "description": "x"}, "KEY_INVALID"),
    ({"kind": "EVENT", "key": "TOKEN_UNLOCK", "description": " "}, "description is required"),
])
def test_factors_are_checked_before_anything_is_kept(factor, code):
    with pytest.raises(checklist.ChecklistError, match=code):
        checklist.submission_evidence(submissions(("n1", [sent("TRADE:t", kind="TRADE",
                                                                factors=[factor])])))


# --- Append-only, audited evidence ----------------------------------------------------------------

def test_reapplying_changes_nothing():
    state = checklist.empty_state()
    entries = factor_entries("2026-09-28", hits=1, misses=1)
    checklist.apply(state, entries, now=NOW, inputs=["a"])
    again = checklist.apply(state, entries, now=NOW, inputs=["a"])
    assert (again["added"], again["corrected"], again["withdrawn"], again["unchanged"]) == (
        0, 0, 0, 2)
    assert len(state["patterns"]["NEWS_TYPE:EXCHANGE_LISTING"]["evidence"]) == 2


def test_a_later_accepted_note_corrects_and_withdraws_by_appending_never_editing():
    """The app counts the latest accepted item of a subject; so does the checklist, through
    audited entries: a changed hit is corrected, a factor no longer given is withdrawn."""
    state, sid = checklist.empty_state(), "MOVER:2026-09-28:SOL/USD"
    upgrade = {"kind": "EVENT", "key": "NETWORK_UPGRADE", "description": "Upgrades"}
    listing = {"kind": "NEWS_TYPE", "key": "EXCHANGE_LISTING", "description": "Listings"}
    first = submissions(("n1", [sent(sid, knowable=False, factors=[listing, upgrade])]))
    found, scopes, _ = checklist.submission_evidence(first)
    checklist.apply(state, found, now=NOW, inputs=["n1"], scopes=scopes)
    original = [dict(e) for e in state["patterns"]["NEWS_TYPE:EXCHANGE_LISTING"]["evidence"]]

    later = submissions(("n1", [sent(sid, knowable=False, factors=[listing, upgrade])]),
                        ("n2", [sent(sid, knowable=True, factors=[listing])]))
    found, scopes, _ = checklist.submission_evidence(later)
    report = checklist.apply(state, found, now=NOW, inputs=["n2"], scopes=scopes)
    assert (report["corrected"], report["withdrawn"]) == (1, 1)
    listing_pattern = state["patterns"]["NEWS_TYPE:EXCHANGE_LISTING"]
    assert listing_pattern["evidence"][:1] == original  # The first entry, untouched.
    assert listing_pattern["evidence"][1]["note_id"] == "n2"
    assert (listing_pattern["occurrences"], listing_pattern["hits"]) == (1, 1)
    upgrade_pattern = state["patterns"]["EVENT:NETWORK_UPGRADE"]
    assert upgrade_pattern["evidence"][-1]["withdrawn"] is True
    assert upgrade_pattern["occurrences"] == 0
    assert checklist.apply(state, found, now=NOW, inputs=["n2"], scopes=scopes)["unchanged"] == 1


def test_a_rebuilt_movers_day_corrects_its_own_day_only():
    state = checklist.empty_state()
    day1 = movers_doc("2026-09-27", {"SOL/USD": ["VOLUME_SPIKE"], "ETH/USD": ["VOLUME_SPIKE"]})
    day2 = movers_doc("2026-09-28", {"SOL/USD": ["VOLUME_SPIKE"]})
    recorded = lessons(("2026-09-27", "0.1", ["SOL/USD"]), ("2026-09-28", "0.1", []))
    for doc in (day1, day2):
        found, scopes = checklist.movers_evidence(doc, recorded)
        checklist.apply(state, found, now=NOW, inputs=["m"], scopes=scopes)
    rebuilt = movers_doc("2026-09-27", {"SOL/USD": ["VOLUME_SPIKE"]})  # ETH's tag is gone.
    found, scopes = checklist.movers_evidence(rebuilt, recorded)
    report = checklist.apply(state, found, now=NOW, inputs=["m2"], scopes=scopes)
    assert (report["withdrawn"], report["unchanged"]) == (1, 1)
    pattern = state["patterns"]["TECHNICAL:VOLUME_SPIKE"]
    assert pattern["occurrences"] == 2  # 2026-09-27 SOL and 2026-09-28 SOL; nothing lost.


# --- Export: ACTIVE items only; ordering and emphasis, never coverage -----------------------------

def test_export_lists_active_items_only_and_says_it_never_narrows_coverage():
    state = checklist.empty_state()
    checklist.apply(state, technical_entries("2026-09-28", hits=4, misses=6), now=NOW, inputs=[])
    checklist.apply(state, factor_entries("2026-09-28", hits=1, misses=9), now=NOW, inputs=[])
    exported = checklist.export(state, now=NOW)
    assert [item["id"] for item in exported["items"]] == ["TECHNICAL:VOLUME_SPIKE"]
    assert "Every coin in the universe is still researched" in exported["use"]
    assert set(exported["items"][0]) == {
        "id", "kind", "key", "description", "occurrences", "hits", "hit_rate", "measure",
        "value", "rolling_value", "first_seen", "last_seen"}  # Nothing that could drop a coin.


# --- The command ------------------------------------------------------------------

def test_cli_update_show_and_export(tmp_path, capsys):
    state_dir, run_dir = tmp_path / "state", tmp_path / "evening"
    run_dir.mkdir()
    (run_dir / "movers.json").write_text(json.dumps(movers_doc(
        "2026-09-28", {f"C{i:02}/USD": ["VOLUME_SPIKE"] for i in range(10)})))
    (run_dir / "context.json").write_text(json.dumps({
        "context_version": "RESEARCH_CONTEXT_V2",
        "lessons": lessons(("2026-09-28", "0.1", ["C00/USD", "C01/USD", "C02/USD"]))}))
    (run_dir / "postmortem-submit.json").write_text(json.dumps(submissions(("n1", [
        sent("MOVER:2026-09-28:C00/USD")]))))
    base = ["--run-dir", str(run_dir), "checklist"]
    assert run.main([*base, "update", "--state-dir", str(state_dir), "--from",
                     str(run_dir / "movers.json"), "--from",
                     str(run_dir / "postmortem-submit.json"), "--now", NOW.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "11 occurrences added" in out and "CANDIDATE -> ACTIVE" in out
    assert run.main([*base, "show", "--state-dir", str(state_dir)]) == 0
    assert "TECHNICAL:VOLUME_SPIKE: 3/10 hits, lift 3.00" in capsys.readouterr().out
    assert run.main([*base, "export", "--state-dir", str(state_dir),
                     "--now", NOW.isoformat()]) == 0
    exported = json.loads((run_dir / "checklist-export.json").read_text())
    assert [item["id"] for item in exported["items"]] == ["TECHNICAL:VOLUME_SPIKE"]
    assert Decimal(exported["items"][0]["value"]) == 3


def test_cli_update_refuses_an_unknown_input_and_the_worksheet(tmp_path, capsys):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"schema": "SOMETHING_ELSE"}))
    base = ["--run-dir", str(tmp_path), "checklist", "update", "--state-dir",
            str(tmp_path / "state"), "--from"]
    assert run.main([*base, str(other)]) == 2
    assert "CHECKLIST_INPUT_UNKNOWN" in capsys.readouterr().err
    worksheet_path = tmp_path / "postmortem.json"
    worksheet_path.write_text(json.dumps({"schema": checklist.POSTMORTEM_SCHEMA, "subjects": []}))
    assert run.main([*base, str(worksheet_path)]) == 2  # Only accepted items are evidence.
    assert "CHECKLIST_WORKSHEET_NOT_EVIDENCE" in capsys.readouterr().err


def test_the_last_ten_occurrences_are_whole_days_never_a_day_cut_in_two():
    """A TECHNICAL day brings many occurrences at once, with no order among them: the window
    takes whole days, newest first, until it holds at least 10."""
    older = [entry for *_rest, entry in technical_entries("2026-09-26", hits=5, misses=0)]
    middle = [entry for *_rest, entry in technical_entries("2026-09-27", hits=0, misses=6)]
    latest = [entry for *_rest, entry in technical_entries("2026-09-28", hits=0, misses=8)]
    window = checklist.recent(older + middle + latest)
    assert {entry["day"] for entry in window} == {"2026-09-27", "2026-09-28"}
    assert len(window) == 14  # 8, then all 6 of the day before: never 2 of them.
    assert len(checklist.recent(latest)) == 8  # Fewer than 10 in all: all of them.


def test_cli_update_skips_the_technical_scoring_when_lessons_are_unavailable(tmp_path, capsys):
    """lessons.available false: movers.json cannot be scored against the app's record of
    the day, and the update says so; a post-mortem's factors still count."""
    run_dir = tmp_path / "evening"
    run_dir.mkdir()
    (run_dir / "movers.json").write_text(json.dumps(movers_doc(
        "2026-09-28", {"SOL/USD": ["VOLUME_SPIKE"]})))
    (run_dir / "context.json").write_text(json.dumps({
        "context_version": "RESEARCH_CONTEXT_V2",
        "lessons": {"lessons_version": "RESEARCH_LESSONS_V1", "agent_id": "claude",
                    "as_of": NOW.isoformat(), "available": False,
                    "code": "LESSONS_UNAVAILABLE"}}))
    (run_dir / "postmortem-submit.json").write_text(json.dumps(submissions(("n1", [
        sent("MOVER:2026-09-28:SOL/USD")]))))
    state_dir = tmp_path / "state"
    assert run.main(["--run-dir", str(run_dir), "checklist", "update", "--state-dir",
                     str(state_dir), "--from", str(run_dir / "movers.json"), "--from",
                     str(run_dir / "postmortem-submit.json"), "--now", NOW.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "lessons unavailable this run (LESSONS_UNAVAILABLE)" in out
    assert "1 occurrences added" in out
    assert set(checklist.load(state_dir)["patterns"]) == {"NEWS_TYPE:EXCHANGE_LISTING"}

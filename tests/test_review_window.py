"""The trade window as a setting: ``CRYPTO_WINDOW_REVIEW_V1`` and ``CRYPTO_WINDOW_HOLD_V1``.

Package review-window (2026-09-28; owner direction of 2026-09-28, the first window 4 hours).
``MANAGED_CRYPTO_WINDOW_JSON`` is parsed strictly; while it is set, report-V3 crypto admissions
record the window versions and ``holding_window_seconds``; without it they record the 24-hour
versions exactly as before. The review's T, the agent's request, the fail-safe and a continue
move by the window, the control arm exits at the window, Jev is asked in the window's words
(``JEV_DAY_REVIEW_QUESTIONS_V3``), a setup keeps the window it recorded, and the learning
replays use the recorded window (``*_V2``).

Fixture evidence only: disposable per-test databases, the fake paper venue, scripted bars, a
mock Jev transport and the real agent routes in process. No broker, provider, network or
owner-ledger contact.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from catalyst_lab import crypto_holding as ch
from catalyst_lab import day_review as dr
from catalyst_lab import day_review_dossier as drd
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.learning_replays import (
    CONTINUE_WINDOW,
    DAY_REPLAY_METHOD,
    DAY_REPLAY_WINDOW_METHOD,
    day_decision_replay,
    day_replay_ready_at,
)
from catalyst_lab.managed_review import ManagedContext
from catalyst_lab.pick_outcomes import parse_bars
from tests.day_review_fixtures import (
    AGENTS,
    LEGACY,
    OPERATOR,
    STATUS,
    at_request,
    at_review,
    bearer,
    decisions,
    events,
    kit,
    managed_arm,
    move_to,
    open_trade,
    review_at,
    run,
    sell_to_close,
    state,
)
from tests.maintenance_fixtures import mt as mt
from tests.maintenance_fixtures import pre_jev_b1_admission as pre_jev_b1_admission
from tests.maintenance_fixtures import quote
from tests.test_execution import er as er
from tests.test_execution import pristine_cluster as pristine_cluster
from tests.test_managed_execution import mx as mx

_ = managed_arm
CHECKOUT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((CHECKOUT / "deploy/private-paper.example.json").read_text())["environment"]
SETTING = EXAMPLE["MANAGED_CRYPTO_WINDOW_JSON"]  # The deploy example's: 240 minutes.
WINDOW = timedelta(hours=4)
FAIL_SAFE = timedelta(minutes=80)  # Review window 75 minutes plus 5 minutes' grace.


@pytest.fixture
def window(mt):
    """The deploy example's setting on the fixture engine (as ``build_runtime_from_env`` sets
    it from ``MANAGED_CRYPTO_WINDOW_JSON``)."""
    mt[0].crypto_window = ch.parse_window_setting(SETTING)
    return mt[0].crypto_window


def first_fill(mt, sid):
    return datetime.fromisoformat(state(mt, sid)["opened_at"])


# --- The setting ---------------------------------------------------------------------------------


def test_the_example_sets_four_hours():
    setting = ch.parse_window_setting(SETTING)
    assert setting == ch.CryptoWindowSetting("CRYPTO_WINDOW_REVIEW_V1", 240)
    assert setting.seconds == 14400 and setting.record() == json.loads(SETTING)
    assert ch.window_setting_from_env({}) is None  # Absent: the 24-hour versions.
    assert ch.window_setting_from_env({ch.WINDOW_ENV: SETTING}) == setting


@pytest.mark.parametrize("minutes", [60, 75, 120, 240, 360, 720, 1440])
def test_every_quarter_hour_from_one_hour_to_a_day_is_a_window(minutes):
    raw = json.dumps({"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": minutes})
    assert ch.parse_window_setting(raw).seconds == minutes * 60


@pytest.mark.parametrize("raw,code", [
    ("", "CRYPTO_WINDOW_SETTING_INVALID"),
    ("not json", "CRYPTO_WINDOW_SETTING_INVALID"),
    ("[240]", "CRYPTO_WINDOW_SETTING_INVALID"),
    ("240", "CRYPTO_WINDOW_SETTING_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240, '
     '"window_minutes": 120}', "CRYPTO_WINDOW_SETTING_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": NaN}',
     "CRYPTO_WINDOW_SETTING_INVALID"),
    ("{}", "CRYPTO_WINDOW_SETTING_KEYS"),
    ('{"window_minutes": 240}', "CRYPTO_WINDOW_SETTING_KEYS"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240, "arm": "ALL"}',
     "CRYPTO_WINDOW_SETTING_KEYS"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_seconds": 14400}',
     "CRYPTO_WINDOW_SETTING_KEYS"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V2", "window_minutes": 240}',
     "CRYPTO_WINDOW_VERSION_UNKNOWN"),
    ('{"version": "CRYPTO_24H_REVIEW_V2", "window_minutes": 240}',
     "CRYPTO_WINDOW_VERSION_UNKNOWN"),
    ('{"version": null, "window_minutes": 240}', "CRYPTO_WINDOW_VERSION_UNKNOWN"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 45}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 1455}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 250}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 240.0}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": "240"}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": true}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": null}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
    ('{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 0}',
     "CRYPTO_WINDOW_MINUTES_INVALID"),
])
def test_anything_else_is_refused_with_its_code(raw, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        ch.parse_window_setting(raw)
    with pytest.raises(ValueError, match=f"^{code}$"):
        ch.window_setting_from_env({ch.WINDOW_ENV: raw})


def test_the_runtime_reads_the_setting_and_passes_it_to_admission(monkeypatch):
    """``build_runtime_from_env`` (the Mac launcher and the Railway trader): absent, admission
    gets no window; the example's value reaches ``ManagedExecution``; anything else refuses
    to start. Broker, database and Jev wiring are stubbed; construction stops at the engine."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    import catalyst_lab.authorization as auth
    import catalyst_lab.managed_broker as broker_module
    import catalyst_lab.managed_execution as execution_module
    import catalyst_lab.review_worker as workers
    import catalyst_lab.risk as risk_module
    import catalyst_lab.scan_sources as sources
    from catalyst_lab.account_risk import AccountRiskPolicy
    from catalyst_lab.alpaca import AlpacaCredentials
    from catalyst_lab.managed_runtime import build_runtime_from_env
    from tests.test_managed_runtime import configure_env

    class Built(Exception):
        """The engine was reached with these options."""

    def engine(*_, **options):
        raise Built(options)

    configure_env(monkeypatch)
    monkeypatch.setattr(AlpacaCredentials, "from_env",
                        lambda: AlpacaCredentials("PKFACTORYFIXTURE", "fixture-secret-only"))
    repo = SimpleNamespace(check_role=lambda: None, require_same_database=lambda _: None,
                           connect=lambda: nullcontext(None))
    monkeypatch.setattr(auth, "RiskRepository", lambda *_, **__: repo)
    frozen = AccountRiskPolicy("CATALYST_RETEST_V1", "FROZEN_V1", D("0.01"), D("0.02"),
                               {"US_STOCKS": D("0.02")}, 1, 1, ("US_STOCKS",), False, D(1),
                               None, 0, "LAB_FIXTURE")
    monkeypatch.setattr(risk_module, "load_policy", lambda *_, **__: frozen)
    monkeypatch.setattr(workers, "ReviewWorker", lambda _: SimpleNamespace(store=object()))
    monkeypatch.setattr(broker_module, "ManagedPaperBroker", lambda *_, **__: object())
    monkeypatch.setattr(sources, "AlpacaMarketSource", lambda *_: object())
    monkeypatch.setattr(execution_module, "ManagedExecution", engine)
    monkeypatch.delenv(ch.WINDOW_ENV, raising=False)
    with pytest.raises(Built) as built:
        build_runtime_from_env()
    assert built.value.args[0]["crypto_window"] is None
    monkeypatch.setenv(ch.WINDOW_ENV, SETTING)
    with pytest.raises(Built) as built:
        build_runtime_from_env()
    assert built.value.args[0]["crypto_window"] == ch.CryptoWindowSetting(
        "CRYPTO_WINDOW_REVIEW_V1", 240)
    for bad in ("", "{}", '{"version": "CRYPTO_WINDOW_REVIEW_V1", "window_minutes": 250}'):
        monkeypatch.setenv(ch.WINDOW_ENV, bad)
        with pytest.raises(ValueError, match="REQUIRED_MANAGED_CONFIGURATION_MISSING_OR_INVALID"):
            build_runtime_from_env()


# --- The versions --------------------------------------------------------------------------------


def test_the_window_review_is_v2_with_the_window_as_its_interval():
    review = ch.parse_window_setting(SETTING).review_policy()
    v2 = ch.CRYPTO_24H_REVIEW_V2.record()
    record = review.record()
    assert {k for k in record if record[k] != v2[k]} == {
        "policy_id", "question_version", "review_interval_seconds"}
    assert (record["policy_id"], record["question_version"], record["review_interval_seconds"]) \
        == ("CRYPTO_WINDOW_REVIEW_V1", "JEV_DAY_REVIEW_QUESTIONS_V3", 14400)
    # V2's context, answer rule, lead, discussion, Jev windows, grace and exit reasons.
    assert (record["context_version"], record["answer_rule"]) == (
        "JEV_DAY_REVIEW_CONTEXT_V2", "DAY_REVIEW_ANSWER_RULE_V2")
    assert (record["request_lead_seconds"], record["agent_reply_seconds"],
            record["jev_answer_seconds"], record["review_window_seconds"],
            record["deadline_grace_seconds"]) == (1800, 900, 1800, 4500, 300)
    assert (record["exit_reason"], record["deadline_exit_reason"]) == (
        "DAY_REVIEW_EXIT", "DAY_REVIEW_DEADLINE_EXIT")
    assert isinstance(review, ch.CryptoReviewPolicyV2) and review != ch.CRYPTO_24H_REVIEW_V2
    assert dr.review_reader(record) is dr.read_review_answer_v2  # V2's answer rule reads it.
    assert ch.review_policy_from_record(record) == review and review.window_seconds == 14400
    for change in ({"review_interval_seconds": 14400 + 60}, {"review_interval_seconds": 1800},
                   {"review_interval_seconds": 86400 + 900}, {"review_interval_seconds": "14400"},
                   {"question_version": "JEV_DAY_REVIEW_QUESTIONS_V2"},
                   {"context_version": "JEV_DAY_REVIEW_CONTEXT_V1"},
                   {"request_lead_seconds": 900}, {"exit_reason": "HOLD_24H_EXIT"},
                   {"answer_rule": "DAY_REVIEW_ANSWER_RULE_V1"}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_REVIEW_POLICY_REQUIRED"):
            ch.CryptoWindowReviewPolicy(**{**record, **change})
    # A V2 record never passes for the window version, nor the reverse.
    with pytest.raises(ValueError):
        ch.CryptoWindowReviewPolicy(**v2)
    with pytest.raises(ValueError):
        ch.CryptoReviewPolicyV2(**record)


def test_the_window_hold_is_the_24_hour_hold_with_the_window():
    hold = ch.parse_window_setting(SETTING).hold_policy()
    assert hold.record() == {"policy_id": "CRYPTO_WINDOW_HOLD_V1", "max_hold_seconds": 14400,
                             "exit_reason": "HOLD_24H_EXIT"}
    fill = datetime.fromisoformat("2026-09-28T12:00:00+00:00")
    assert hold.exit_at(fill) == fill + WINDOW and hold.window_seconds == 14400
    for bad in (("CRYPTO_24H_HOLD_V1", 14400, "HOLD_24H_EXIT"),
                ("CRYPTO_WINDOW_HOLD_V1", 14400, "TIME_EXIT"),
                ("CRYPTO_WINDOW_HOLD_V1", 3000, "HOLD_24H_EXIT"),
                ("CRYPTO_WINDOW_HOLD_V1", 14400.0, "HOLD_24H_EXIT")):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_HOLD_POLICY_REQUIRED"):
            ch.CryptoWindowHoldPolicy(*bad)
    with pytest.raises(ValueError):
        ch.CryptoHoldPolicy(*hold.record().values())  # Never the 24-hour hold's record.


def test_admission_records_the_window_versions_only_with_the_setting():
    setting = ch.parse_window_setting(SETTING)
    assert ch.admission_policy(True, "JEV_MANAGED") == ch.CRYPTO_24H_REVIEW_V2
    assert ch.admission_policy(True, "FIXED_EXIT") == ch.CRYPTO_24H_HOLD
    assert ch.admission_policy(True, "JEV_MANAGED", None) == ch.CRYPTO_24H_REVIEW_V2
    assert ch.admission_policy(True, "JEV_MANAGED", setting) == setting.review_policy()
    assert ch.admission_policy(True, "FIXED_EXIT", setting) == setting.hold_policy()
    assert ch.admission_policy(False, "JEV_MANAGED", setting) is None
    assert ch.window_fields(setting.review_policy()) == {"holding_window_seconds": 14400}
    assert ch.window_fields(setting.hold_policy()) == {"holding_window_seconds": 14400}
    for hold in (ch.CRYPTO_24H_REVIEW_V2, ch.CRYPTO_24H_REVIEW, ch.CRYPTO_24H_HOLD, None):
        assert ch.window_fields(hold) == {}  # The 24-hour states stay exactly as before.


def test_a_state_keeps_the_window_it_recorded_and_an_altered_one_is_refused():
    setting = ch.parse_window_setting(SETTING)
    review, hold = setting.review_policy(), setting.hold_policy()
    reviewed = {"holding_policy": review.record(), "holding_window_seconds": 14400}
    held = {"holding_policy": hold.record(), "holding_window_seconds": 14400}
    assert ch.recorded_hold_policy(reviewed) == review and ch.review_active(reviewed)
    assert ch.recorded_hold_policy(held) == hold and not ch.review_active(held)
    assert ch.recorded_window_seconds(reviewed) == ch.recorded_window_seconds(held) == 14400
    for v2 in ({"holding_policy": ch.CRYPTO_24H_REVIEW_V2.record()},
               {"holding_policy": ch.CRYPTO_24H_HOLD.record()}, {}):
        assert ch.recorded_window_seconds(v2) is None
    for altered in ({**reviewed, "holding_window_seconds": 7200},
                    {**reviewed, "holding_window_seconds": 14400.0},
                    {**reviewed, "holding_window_seconds": "14400"},
                    {"holding_policy": review.record()},
                    {**held, "holding_window_seconds": None},
                    {"holding_policy": ch.CRYPTO_24H_REVIEW_V2.record(),
                     "holding_window_seconds": 86400},
                    {"holding_policy": ch.CRYPTO_24H_HOLD.record(),
                     "holding_window_seconds": 14400}):
        with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_WINDOW_REQUIRED"):
            ch.recorded_hold_policy(altered)
    # The clock: T every window, the fail-safe 80 minutes after T, the hold at the window.
    fill = datetime.fromisoformat("2026-09-28T12:00:00+00:00")
    assert review.review_at(fill) == fill + WINDOW
    assert review.review_at(fill, 2) == fill + 3 * WINDOW
    assert ch.hard_exit_deadline(review, fill, reviewed) == fill + WINDOW + FAIL_SAFE
    assert ch.hard_exit_deadline(review, fill, {**reviewed, "continuations": 1}) == (
        fill + 2 * WINDOW + FAIL_SAFE)
    assert ch.review_fields(review, fill, reviewed) == {
        "day_review_at": (fill + WINDOW).isoformat(), "continuations": 0}
    assert ch.hard_exit_deadline(hold, fill, held) == fill + WINDOW
    assert ch.review_fields(hold, fill, held) == {}
    assert ch.time_exit_reason(held) == "HOLD_24H_EXIT"
    assert ch.time_exit_reason(reviewed) == "DAY_REVIEW_DEADLINE_EXIT"


# --- JEV_DAY_REVIEW_QUESTIONS_V3 -----------------------------------------------------------------

V2_TO_4H = (("its 24-hour review", "its 4-hour review"),
            ("in the next 24 hours", "in the next 4 hours"),
            ("for another 24 hours", "for another 4 hours"),
            ("a new 24-hour plan", "a new 4-hour plan"),
            ("reviewed again in 24 hours", "reviewed again in 4 hours"),
            ("for the next 24 hours", "for the next 4 hours"))


def context_for(policy, *, status="ANSWERED", options=None):
    options = options or {"stop": [], "target": []}
    state = {"as_of": "2026-09-28T16:00:00+00:00",
             "trade": {"bid": "106.02", "entry": "100.10", "risk_per_coin": "5.10"},
             "review": {"agent_answer_status": status}}
    text = encoded({"state": state, "options": options, "policy": policy.record()})
    return ManagedContext(text, digest(text))


def test_window_words():
    assert drd.window_words(14400) == ("4-hour", "4 hours")
    assert drd.window_words(3600) == ("1-hour", "1 hour")
    assert drd.window_words(86400) == ("24-hour", "24 hours")
    assert drd.window_words(5400) == ("90-minute", "90 minutes")
    assert drd.window_words(4500) == ("75-minute", "75 minutes")


@pytest.mark.parametrize("status", ["ANSWERED", "NO_ANSWER"])
def test_the_v3_questions_are_v2s_in_the_windows_words(status):
    four = ch.parse_window_setting(SETTING).review_policy()
    day = ch.CryptoWindowReviewPolicy(**{**four.record(), "review_interval_seconds": 86400})
    v2 = drd.review_questions_for(context_for(ch.CRYPTO_24H_REVIEW_V2, status=status))
    v3 = drd.review_questions_for(context_for(four, status=status))
    v3_day = drd.review_questions_for(context_for(day, status=status))
    assert (v2.version, v3.version, v3_day.version) == (
        "JEV_DAY_REVIEW_QUESTIONS_V2", "JEV_DAY_REVIEW_QUESTIONS_V3", "JEV_DAY_REVIEW_QUESTIONS_V3")
    # At a 1,440-minute window the texts are V2's exactly.
    assert v3_day.questions == v2.questions
    # At 240 minutes each text is V2's with the review's horizon in the window's words.
    expected = json.loads(json.dumps(v2.questions))
    for question in expected.values():
        for old, new in V2_TO_4H:
            question["instructions"] = question["instructions"].replace(old, new)
        question["criteria"] = {k: v.replace("for another 24 hours", "for another 4 hours")
                                if isinstance(v, str) else v
                                for k, v in question["criteria"].items()}
    assert v3.questions == expected and v3.questions != v2.questions
    text = json.dumps(v3.questions)
    assert "its 4-hour review" in text and "continue for another 4 hours" in text
    assert "reviewed again in 4 hours" in text and "Continue for another 4 hours" in text
    # The only 24-hour words left name price levels (the target options' 24-hour high).
    assert text.count("24") == text.count("the 24-hour and 7-day highs") == 1
    assert drd.QUESTIONS_V3_TEMPLATE_SHA256 == drd.questions_v3_template_hashes() == {
        "ANSWERED": "27bd85a7a100664ccd1bf2571886e16462d37c7fd01932b6c83443a7117c5b69",
        "NO_ANSWER": "9e6f11a12f79b80de9f1743bda37437adcc34619ad0900453c3d6cf95dfe6cfa"}
    # V2's pin is untouched, and the early-exit questions keep their words.
    assert drd.questions_v2_template_hashes() == drd.QUESTIONS_V2_TEMPLATE_SHA256
    assert "next 24-hour review" in json.dumps(drd.EARLY_EXIT_QUESTIONS.questions)


# --- Admission on the fixture venue --------------------------------------------------------------


def test_without_the_setting_admission_records_todays_policies(mt, managed_arm, monkeypatch):
    engine, _, _ = mt
    assert engine.crypto_window is None
    managed = open_trade(mt)
    assert state(mt, managed)["holding_policy"] == ch.CRYPTO_24H_REVIEW_V2.record()
    assert "holding_window_seconds" not in state(mt, managed)
    assert review_at(mt, managed) == first_fill(mt, managed) + timedelta(hours=24)
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    control = open_trade(mt, "ETH/USD")
    assert state(mt, control)["holding_policy"] == ch.CRYPTO_24H_HOLD.record()
    assert "holding_window_seconds" not in state(mt, control)
    assert datetime.fromisoformat(state(mt, control)["hard_exit_at"]) == (
        first_fill(mt, control) + timedelta(hours=24))


def test_with_the_setting_admission_records_the_window_versions(mt, managed_arm, window,
                                                                monkeypatch):
    managed = open_trade(mt)
    current = state(mt, managed)
    assert current["holding_policy"] == window.review_policy().record()
    assert current["holding_window_seconds"] == 14400
    t = first_fill(mt, managed) + WINDOW
    assert review_at(mt, managed) == t and current["continuations"] == 0
    assert datetime.fromisoformat(current["hard_exit_at"]) == t + FAIL_SAFE
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    control = open_trade(mt, "ETH/USD")
    held = state(mt, control)
    assert held["holding_policy"] == window.hold_policy().record()
    assert held["holding_window_seconds"] == 14400
    assert "day_review_at" not in held and "continuations" not in held
    assert datetime.fromisoformat(held["hard_exit_at"]) == first_fill(mt, control) + WINDOW
    with pytest.raises(ValueError, match="EXPLICIT_CRYPTO_WINDOW_REQUIRED"):
        type(mt[0])(mt[0].repo, mt[0].broker, policy=mt[0].policy, clock=mt[0].now,
                    review_store=mt[0].review_store, crypto_window=json.loads(SETTING))
    # The readback routes and the owner's position view show the recorded window.
    from catalyst_lab.cloud_runtime import POSITION_STATE_KEYS
    from catalyst_lab.managed_service import STATE_FIELDS

    assert "holding_window_seconds" in STATE_FIELDS
    assert "holding_window_seconds" in POSITION_STATE_KEYS


# --- The review every window ---------------------------------------------------------------------


def test_the_request_jevs_questions_and_each_continue_move_by_the_window(mt, managed_arm, window):
    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    first_t = review_at(mt, sid)
    assert first_t == first_fill(mt, sid) + WINDOW
    at_request(mt, sid, seconds=-2)  # Not yet 30 minutes before T.
    run(mt, k, bid="106")
    assert events(engine, dr.REQUESTED, sid) == []
    for number in (1, 2):
        at_request(mt, sid)
        run(mt, k, bid="106")
        request = events(engine, dr.REQUESTED, sid)[-1]
        assert (request["policy_id"], request["review_number"]) == (
            "CRYPTO_WINDOW_REVIEW_V1", number)
        assert request["holding_window_seconds"] == 14400
        assert request["review_at"] == (first_t + (number - 1) * WINDOW).isoformat()
        at_review(mt, sid)
        run(mt, k, bid="106")  # The agent stays silent; Jev continues alone.
        body = events(engine, dr.DECISION, sid)[-1]
        assert (body["policy_id"], body["review_number"], body["outcome"], body["code"],
                body["continuations_after"]) == (
            "CRYPTO_WINDOW_REVIEW_V1", number, "CONTINUE", "AGENT_SILENT_JEV_ALONE", number)
        assert body["next_review_at"] == (first_t + number * WINDOW).isoformat()
    current = state(mt, sid)
    assert current["continuations"] == 2
    assert current["day_review_at"] == (first_t + 2 * WINDOW).isoformat()
    assert current["hard_exit_at"] == (first_t + 2 * WINDOW + FAIL_SAFE).isoformat()
    hold = ch.recorded_hold_policy(current)
    assert hold.exit_at(first_fill(mt, sid), 2).isoformat() == current["hard_exit_at"]
    engine.manage(sid, quote(mt, "106"))  # The protection loop keeps the moved clock.
    assert state(mt, sid)["hard_exit_at"] == current["hard_exit_at"]
    # Jev was asked in the window's words, with context V2 and the setup's own record.
    sent = k.jev.of("DAY_REVIEW")
    assert len(sent) == 2 and sent[0]["state"]["context_version"] == "JEV_DAY_REVIEW_CONTEXT_V2"
    questions = sent[0]["questions"]
    assert "reached its 4-hour review" in questions["trade_reason"]["instructions"]
    assert "continue for another 4 hours" in questions["decision"]["instructions"]
    assert "24 hours" not in json.dumps(questions)
    assert sent[0]["state"]["review"]["hours_in_trade"] == 4
    [jev_request, _] = events(engine, dr.JEV_REQUEST, sid)
    assert jev_request["context"]["policy"] == window.review_policy().record()
    assert jev_request["context"]["identity"]["holding_policy_id"] == "CRYPTO_WINDOW_REVIEW_V1"


def test_the_fail_safe_exits_eighty_minutes_after_the_windows_t(mt, managed_arm, window):
    engine, venue, _ = mt
    sid = open_trade(mt)
    t = review_at(mt, sid)
    move_to(mt, t)  # No forced exit at T under the review.
    engine.manage(sid, quote(mt, "106"))
    assert not state(mt, sid).get("exit_requested")
    move_to(mt, t + FAIL_SAFE - timedelta(seconds=1))
    engine.manage(sid, quote(mt, "106"))
    assert not state(mt, sid).get("exit_requested")
    move_to(mt, t + FAIL_SAFE)
    sell_to_close(mt, sid, "SOL/USD", "106")
    final = state(mt, sid)
    assert (final["state"], final["reason"]) == ("CLOSED", "DAY_REVIEW_DEADLINE_EXIT")
    assert [d["reason"] for d in decisions(engine, sid, "EXIT")] == ["DAY_REVIEW_DEADLINE_EXIT"]
    assert t + FAIL_SAFE < first_fill(mt, sid) + timedelta(hours=24)


def test_the_control_arm_exits_at_the_window_with_its_exit_reason(mt, window, monkeypatch):
    engine, venue, _ = mt
    monkeypatch.setattr("catalyst_lab.managed_execution.assign_arm", lambda *_: "FIXED_EXIT")
    sid = open_trade(mt)
    k = kit(mt)
    deadline = datetime.fromisoformat(state(mt, sid)["hard_exit_at"])
    assert deadline == first_fill(mt, sid) + WINDOW
    for at in (deadline - timedelta(minutes=29), deadline - timedelta(seconds=1)):
        move_to(mt, at)
        run(mt, k, bid="106")
        engine.manage(sid, quote(mt, "106"))
        assert not state(mt, sid).get("exit_requested")
    move_to(mt, deadline)
    engine.manage(sid, quote(mt, "106"))
    assert state(mt, sid)["exit_requested"] == "HOLD_24H_EXIT"  # The code reused at 4 hours.
    sell_to_close(mt, sid, "SOL/USD", "106")
    final = state(mt, sid)
    assert (final["state"], final["reason"]) == ("CLOSED", "HOLD_24H_EXIT")
    assert events(engine, dr.REQUESTED) == [] and events(engine, dr.DECISION) == []
    assert k.jev.calls == []


def test_a_setup_admitted_under_v2_keeps_24_hours_after_the_setting_appears(mt, managed_arm):
    engine, venue, _ = mt
    older = open_trade(mt)  # Admitted without the setting: CRYPTO_24H_REVIEW_V2.
    engine.crypto_window = ch.parse_window_setting(SETTING)  # A restart with the setting.
    k = kit(mt)
    t = review_at(mt, older)
    assert t == first_fill(mt, older) + timedelta(hours=24)
    at_request(mt, older)
    run(mt, k, bid="106")
    request = events(engine, dr.REQUESTED, older)[-1]
    assert request["policy_id"] == "CRYPTO_24H_REVIEW_V2"
    assert "holding_window_seconds" not in request
    at_review(mt, older)
    run(mt, k, bid="106")
    [sent] = k.jev.of("DAY_REVIEW")
    assert "reached its 24-hour review" in sent["questions"]["trade_reason"]["instructions"]
    body = events(engine, dr.DECISION, older)[-1]
    assert (body["policy_id"], body["outcome"]) == ("CRYPTO_24H_REVIEW_V2", "CONTINUE")
    assert body["next_review_at"] == (t + timedelta(hours=24)).isoformat()
    kept = state(mt, older)
    assert kept["holding_policy"] == ch.CRYPTO_24H_REVIEW_V2.record()
    assert kept["hard_exit_at"] == (t + timedelta(hours=24) + FAIL_SAFE).isoformat()
    # A trade admitted now records the window; the older one keeps its own.
    newer = open_trade(mt, "ETH/USD")
    assert state(mt, newer)["holding_policy"]["policy_id"] == "CRYPTO_WINDOW_REVIEW_V1"
    assert review_at(mt, newer) == first_fill(mt, newer) + WINDOW
    engine.manage(older, quote(mt, "106"))
    assert state(mt, older)["hard_exit_at"] == kept["hard_exit_at"]
    status = k.day.status(engine.store.active())
    assert status["open_trades_by_policy"] == {"CRYPTO_24H_REVIEW_V2": 1,
                                               "CRYPTO_WINDOW_REVIEW_V1": 1}
    assert (status["policy_id"], status["window_minutes"]) == ("CRYPTO_WINDOW_REVIEW_V1", 240)


def test_the_research_context_and_the_status_show_the_window(mt, managed_arm, window):
    from fastapi.testclient import TestClient

    from catalyst_lab.managed_service import create_managed_app
    from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
    from tests.test_research_context import Feeds, service_for

    engine, venue, _ = mt
    sid = open_trade(mt)
    k = kit(mt)
    at_request(mt, sid)
    run(mt, k, bid="106")
    feeds = Feeds(lambda: venue.now)
    context, _ = service_for(engine.repo, lambda: venue.now, feeds)
    context.reviews = k.service
    cycle = ResearchCycle(engine.repo, k.reviewer, CyclePolicy(10, 10, 15, 60, 30),
                          clock=lambda: venue.now)
    client = TestClient(create_managed_app(
        cycle, engine.store, api_token=LEGACY, runtime_status=lambda: {}, status_token=STATUS,
        operator_token=OPERATOR, agent_tokens=AGENTS, research_context=context,
        trade_reviews=k.service))
    mine = client.get("/api/v1/lab/research-context", headers=bearer(AGENTS["claude"])).json()
    [item] = mine["pending_reviews"]
    assert (item["kind"], item["setup_id"], item["review_at"]) == (
        "DAY_REVIEW", str(sid), review_at(mt, sid).isoformat())
    assert item["request"]["holding_window_seconds"] == 14400
    [trade] = mine["open_trades"]
    assert trade["holding_window_seconds"] == 14400
    assert trade["review_at"] == (first_fill(mt, sid) + WINDOW).isoformat()
    status = k.day.status(engine.store.active())
    assert (status["policy_id"], status["window_minutes"]) == ("CRYPTO_WINDOW_REVIEW_V1", 240)
    assert status["open_trades_by_policy"] == {"CRYPTO_WINDOW_REVIEW_V1": 1}
    engine.crypto_window = None  # Without the setting, admission records V2 again.
    status = k.day.status(engine.store.active())
    assert (status["policy_id"], status["window_minutes"]) == ("CRYPTO_24H_REVIEW_V2", None)


# --- The learning replays ------------------------------------------------------------------------


def test_the_day_review_replay_of_a_window_setup_continues_for_its_window():
    at = datetime.fromisoformat("2026-09-28T16:00:00+00:00")

    def rows(*bars):
        return parse_bars([{"t": (at + timedelta(minutes=m)).isoformat(), "o": o, "h": h,
                            "l": low, "c": c, "v": "1"} for m, o, h, low, c in bars])

    decision = {"outcome": "EXIT", "at": at.isoformat(), "old_stop": "99", "old_target": "115"}
    bars = rows((5, "103", "104", "102", "103"), (4 * 60, "106", "107", "105", "106"),
                (24 * 60, "108", "109", "107", "108"))
    window = day_decision_replay(decision, bars, entry_price=D("100.10"), initial_stop=D("95"),
                                 window_seconds=14400)
    assert (window["method"], window["alternative"], window["window_seconds"]) == (
        DAY_REPLAY_WINDOW_METHOD, CONTINUE_WINDOW, 14400)
    assert (window["exit_reason"], window["exit_price"], window["exit_at"]) == (
        "HOLD_24H_EXIT", "106", (at + WINDOW).isoformat())
    day = day_decision_replay(decision, bars, entry_price=D("100.10"), initial_stop=D("95"))
    assert (day["method"], day["alternative"], day["exit_price"]) == (
        DAY_REPLAY_METHOD, "CONTINUE_24H_ON_LEVELS_IN_FORCE", "108")
    assert "window_seconds" not in day  # V1's record exactly.
    assert day_replay_ready_at(decision, 14400) == at + WINDOW + timedelta(minutes=5)
    assert day_replay_ready_at(decision) == at + timedelta(hours=24, minutes=5)
    continued = {**decision, "outcome": "CONTINUE"}
    assert day_replay_ready_at(continued, 14400) == day_replay_ready_at(continued)


@pytest.mark.usefixtures("pre_jev_b1_admission")  # V4's maintenance (before jev-b1).
def test_the_unchanged_plan_holds_for_the_window_the_setup_recorded(mt, managed_arm):
    from catalyst_lab.unchanged_plan import unchanged_plan_comparisons
    from tests.maintenance_fixtures import bodies, maintainer
    from tests.test_maintenance_replay import FakeBars, reviewed

    engine, venue, _ = mt
    older = open_trade(mt)  # CRYPTO_24H_REVIEW_V2: UNCHANGED_PLAN_REPLAY_V1, 24 hours.
    engine.crypto_window = ch.parse_window_setting(SETTING)
    newer = open_trade(mt, "ETH/USD")  # CRYPTO_WINDOW_REVIEW_V1: _V2, the recorded window.
    maintenance = maintainer(mt)
    maintenance.jev.answer("RAISE_STOP", stop="first")
    reviewed(mt, maintenance, symbol="SOL/USD")
    reviewed(mt, maintenance, symbol="ETH/USD")
    outcomes = {}
    for sid, symbol in ((older, "SOL/USD"), (newer, "ETH/USD")):
        [decision] = bodies(engine, "MAINTENANCE_DECISION", sid)
        assert decision["outcome"] == "APPLIED"
        at = datetime.fromisoformat(decision["decided_at"])
        bars = FakeBars().flat(symbol, at, hours=25, price="105")
        [outcomes[symbol]] = unchanged_plan_comparisons(engine.repo, bars, sid,
                                                        now=at + timedelta(hours=25))
    day, window = outcomes["SOL/USD"], outcomes["ETH/USD"]
    assert (day.method, day.exit_reason, day.exit_at) == (
        "UNCHANGED_PLAN_REPLAY_V1", "HOLD_24H_EXIT", first_fill(mt, older) + timedelta(hours=24))
    assert (window.method, window.exit_reason, window.exit_at) == (
        "UNCHANGED_PLAN_REPLAY_V2", "HOLD_24H_EXIT", first_fill(mt, newer) + WINDOW)
    assert window.to_dict()["method"] == "UNCHANGED_PLAN_REPLAY_V2" and window.data_complete


def test_the_weekly_review_counts_the_window_review_as_the_current_version(mx):
    from catalyst_lab.learning_intake import day_bounds
    from catalyst_lab.learning_replays import DAY_REPLAY_EVENT
    from catalyst_lab.weekly_review import closed_trades, compute_review, last_completed_week_end
    from tests.learning_fixtures import close_attributed

    engine, venue, _ = mx
    store = engine.store
    sid, _ = close_attributed(mx, "BTC/USD")
    review = ch.parse_window_setting(SETTING).review_policy()
    with store.transaction() as conn:  # A closed trade that recorded the window review.
        store.transition(conn, sid, "CLOSED", holding_policy=review.record(),
                         holding_window_seconds=14400)
        store.event(conn, DAY_REPLAY_EVENT, {
            "method": DAY_REPLAY_WINDOW_METHOD, "setup_id": str(sid), "decision": "EXIT",
            "at": venue.now.isoformat(), "alternative_net_r": "0.25", "window_seconds": 14400,
            "data_complete": True})
    week_end = last_completed_week_end(venue.now + timedelta(days=7))
    _, until = day_bounds(week_end)
    trades = closed_trades(store.repo, until)
    assert trades[str(sid)]["holding_policy"] == "CRYPTO_WINDOW_REVIEW_V1"  # The ID, not a dict.
    body = compute_review(store.repo, week_end, now=until + timedelta(hours=1, minutes=30))
    [day] = [t for t in body["tests"] if t["test_id"] == "DAY_REVIEW_DECISIONS"]
    assert day["current_version"] == "CRYPTO_WINDOW_REVIEW_V1"
    assert day["by_decision"]["EXIT"]["count"] == 1
    assert "24-hour" not in day["question"]


def test_the_scorecard_names_the_replay_methods_behind_each_group():
    from types import SimpleNamespace

    from catalyst_lab.learning_replays import DAY_REPLAY_EVENT
    from catalyst_lab.scorecard import maintenance

    trades = [SimpleNamespace(setup_id="a", symbol="SOL/USD", r_net=D("1")),
              SimpleNamespace(setup_id="b", symbol="ETH/USD", r_net=D("-1"))]

    def replay(setup, method):
        return {"kind": DAY_REPLAY_EVENT, "body": {
            "setup_id": setup, "decision": "EXIT", "method": method, "at": "2026-09-28T16:00:00Z",
            "alternative_net_r": "0.5"}}

    only_v1 = maintenance(trades, [replay("a", DAY_REPLAY_METHOD)], items=False)
    assert only_v1["DAY_REVIEW_EXIT"]["method"] == "DAY_REVIEW_DECISION_REPLAY_V1"
    assert only_v1["DAY_REVIEW_CONTINUE"]["method"] == "DAY_REVIEW_DECISION_REPLAY_V1"
    assert only_v1["STOP_RAISE"]["method"] == "UNCHANGED_PLAN_REPLAY_V1"
    both = maintenance(trades, [replay("a", DAY_REPLAY_METHOD),
                                replay("b", DAY_REPLAY_WINDOW_METHOD)], items=False)
    assert both["DAY_REVIEW_EXIT"]["method"] == (
        "DAY_REVIEW_DECISION_REPLAY_V1+DAY_REVIEW_DECISION_REPLAY_V2")
    assert both["DAY_REVIEW_EXIT"]["changes"] == 2

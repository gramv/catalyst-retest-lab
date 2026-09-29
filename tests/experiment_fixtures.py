"""Fixture ledgers for the public experiment page (package experiment-page).

Disposable PostgreSQL only; every row written here is LAB_FIXTURE data and never touches an
owner ledger. Research runs are appended with the event kinds and bodies the research cycle and
top-K selection write (as tests/test_pick_outcomes_ledger.py does); every setup is bound to a
real Jev receipt from a mock TypeSafe transport and inserted as catalyst_risk with a report-V3
admission record and the admission state fields ManagedExecution.admit writes; fills, fee
evidence (through managed_analytics.import_fill_cost_correction), maintenance decisions, early
exits and closes use the same tables and event kinds as the engine. Shadow outcomes go through
position samples, Jev's holds and raises, 24-hour reviews and agent exit flags follow the bodies
the engine writes.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from catalyst_lab import crypto_holding, crypto_maintenance, crypto_trigger, gap_resume
from catalyst_lab.account_risk import ARM_METHOD
from catalyst_lab.authorization import RiskRepository
from catalyst_lab.jev_contract import SKEPTIC, digest, encoded
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore
from catalyst_lab.managed_analytics import import_fill_cost_correction
from catalyst_lab.managed_store import COHORT, POLICY, ManagedStore
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3
from catalyst_lab.system_check import SYSTEM_CHECK_VERSION
from tests.test_jev_review import FIXTURE_KEY
from tests.test_research_reports import reply

TOPK = "JEV_TOP_K_SELECTION_V2"
QUESTION_SETS = {
    "NEWS": {"version": "NEWS_PICK_QUESTIONS_V2", "template_hash": "0" * 64},
    "CHART": {"version": "CHART_PICK_QUESTIONS_V1", "template_hash": "1" * 64},
    "BOTH": {"version": "BOTH_PICK_QUESTIONS_V2", "template_hash": "2" * 64},
}
TAKER = D("0.0025")
FIXTURE_AGENT_ID = "fixture-research-agent"
# One-line reasons the dashboard shows (fixture wording; a real agent writes its own).
THESES = {
    "CHART": "Pulled back to the 4-hour breakout level on falling volume; stop under the swing "
             "low.",
    "NEWS": "Exchange listing announced this morning; buying the first orderly pullback.",
    "BOTH": "Upgrade news plus a reclaimed daily range high; volume above the 20-day average.",
}
LONG_THESIS = ("Range reclaim after a two-week base.\n\nVolume expanded on the reclaim, funding "
               "is flat and the 1-hour structure shows higher lows since Tuesday; the stop sits "
               "under the base so a failed reclaim exits quickly. Target is the prior swing high.")
FIXTURE_ACCOUNT_MARKER = "fixture-paper-account-000"


def role_url(url, role):
    return re.sub(r"user=\w+", "user=" + role, url)


def public_url(url):
    return role_url(url, "catalyst_public")


def agent_block(agent_id=FIXTURE_AGENT_ID):
    return {"agent_id": agent_id, "agent_version": "fixture-v1",
            "guidelines_version": "MUSE_RESEARCH_GUIDELINES_V5", "guidelines_sha256": "3" * 64,
            "run_id": str(uuid4())}


@dataclass
class Pick:
    symbol: str
    kind: str = "CHART"
    levels: dict = field(default_factory=lambda: {
        "entry_trigger": "100", "max_entry_price": "100.10", "stop": "95", "target": "111"})
    agent_price: str = "100.60"
    status: str = "RANKED"  # RANKED, VETOED or NOT_RANKED
    jev_rank: int | None = None
    selected: bool = False
    replacement_for: str | None = None
    declined: str | None = None
    item_key: str = ""
    body: dict = field(default_factory=dict)
    selection: dict | None = None
    thesis: str | None = None


@dataclass
class Run:
    cycle_id: str
    run_slot: datetime
    picks: dict


class ExperimentLedger:
    """Append-only fixture writer bound to one disposable database (``app_url``: catalyst_app)."""

    def __init__(self, app_url):
        self.app_url = app_url
        self.app = Repository(app_url)
        self.store = ManagedStore(RiskRepository(role_url(app_url, "catalyst_risk")))
        self.reviews = JevStore(role_url(app_url, "catalyst_jev"))

    def event(self, conn, kind, body, *, at=None, setup_id=None, key=None):
        """``ManagedStore.event``; ``at`` back-dates ``recorded_at`` so a fixture history reads
        like one written over several days (disposable databases only)."""
        if at is None:
            return self.store.event(conn, kind, body, setup_id=setup_id, key=key)
        return conn.execute(
            """INSERT INTO lab.managed_events(event_id,setup_id,idempotency_key,kind,body,
            recorded_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *""",
            (uuid4(), setup_id, key or str(uuid4()), kind, Jsonb(json_safe(body)), at),
        ).fetchone()

    # --- Research runs ------------------------------------------------------------------------

    def run(self, run_slot, picks, *, k=10, submitted=None, agent=FIXTURE_AGENT_ID):
        """One report-V3 research run, recorded as a run happens: the agent's report (the run
        and its picks) arrives 10 minutes after the slot; Jev ranks and selects at 25 minutes."""
        cycle_id = str(uuid4())
        created = run_slot + timedelta(minutes=10)
        ranked = run_slot + timedelta(minutes=25)
        expires = run_slot + timedelta(hours=23)
        started = {
            "cycle_id": cycle_id, "report_schema_version": REPORT_SCHEMA_V3,
            "run_slot": run_slot.isoformat(), "agent": agent_block(agent),
            "contender_count": len(picks), "submitted_count": submitted or len(picks),
            "rejected_count": (submitted or len(picks)) - len(picks),
            "expires_at": expires.isoformat(), "research_origin": "EXTERNAL_RESEARCH_AGENT",
            "purpose": "ENGINEERING_TEST", "dossier_version": "REVIEW_DOSSIER_V3",
            "selection_policy": TOPK,
            "selection_rule": {"selection_policy": TOPK, "k": k, "question_sets": QUESTION_SETS,
                               "quality_policy": "MUSE_JEV_COMPARATIVE_QUALITY_V3",
                               "veto_min_probability": "0.70"},
            "report": {"fixture": "report text withheld from the public views"},
        }
        with self.store.transaction() as conn:
            self.event(conn, "RESEARCH_STARTED", started, key=f"research:{cycle_id}:start",
                       at=created)
            for rank, pick in enumerate(picks, 1):
                pick.item_key = "CRYPTO:" + pick.symbol
                pick.body = {
                    "cycle_id": cycle_id, "item_key": pick.item_key,
                    "signal_id": f"fixture-{pick.symbol.split('/')[0].lower()}-{rank}",
                    "revision": 1, "market": "CRYPTO", "symbol": pick.symbol, "rank": rank,
                    "agent": agent_block(agent), "selection_policy": TOPK, "levels": pick.levels,
                    "state": {"market": "CRYPTO", "symbol": pick.symbol, "kind": pick.kind,
                              "agent_current_price": pick.agent_price,
                              "agent_price_at": created.isoformat(),
                              "thesis": pick.thesis or THESES[pick.kind],
                              "sources": [{"source_id": "fixture-source",
                                           "excerpt": "Fixture source excerpt, never public."}]},
                    "created_at": created.isoformat(), "expires_at": expires.isoformat(),
                    "evidence_hash": digest(cycle_id + pick.item_key),
                    "report_schema_version": REPORT_SCHEMA_V3, "run_slot": run_slot.isoformat(),
                }
                self.event(conn, "RESEARCH_PACKET", pick.body,
                           key=f"research:{cycle_id}:{pick.item_key}:1:packet", at=created)
            entries = [{
                "item_key": p.item_key, "revision": 1, "rank": p.jev_rank,
                "adjusted_score": "70.0000", "quality_score": "70.0000",
                "quality_category": "ADEQUATE", "status": p.status,
                "veto_reasons": ["PRICES_CONSISTENT_NO"] if p.status == "VETOED" else [],
                "uncertain": [], "dissent": "APPROVE", "receipt_id": None,
                "quality_receipt_id": None, "symbol": p.symbol, "kind": p.kind,
                "question_set_version": QUESTION_SETS[p.kind]["version"], "dissent_tied": False,
                "agent_rank": rank, "reason": "MISSING_VALID_REVIEW"
                if p.status == "NOT_RANKED" else None,
            } for rank, p in enumerate(picks, 1)]
            self.event(conn, "RESEARCH_RANKING", {
                "policy": TOPK, "cycle_id": cycle_id, "run_slot": run_slot.isoformat(), "k": k,
                "entries": entries, "complete": True,
                "quality_policy": "MUSE_JEV_COMPARATIVE_QUALITY_V3",
            }, key=f"research:ranking:{cycle_id}", at=ranked)
            for rank, pick in enumerate(picks, 1):
                if not pick.selected:
                    continue
                packet = {**pick.body, "agent_rank": rank, "jev_rank": pick.jev_rank,
                          "replacement_for": pick.replacement_for,
                          "review_valid_until": (created + timedelta(minutes=30)).isoformat()}
                event = self.event(conn, "RESEARCH_SELECTED",
                                   {"cycle_id": cycle_id, "packet": packet}, at=ranked)
                pick.selection = {**packet, "selection_event_seq": event["event_seq"]}
                if pick.declined:
                    self.event(conn, "RESEARCH_ADMISSION_DECLINED", {
                        "selection_event_seq": event["event_seq"], "reason": pick.declined,
                        "cycle_id": cycle_id, "item_key": pick.item_key, "symbol": pick.symbol,
                        "run_slot": run_slot.isoformat()}, at=ranked + timedelta(minutes=1))
        return Run(cycle_id, run_slot, {p.symbol: p for p in picks})

    # --- Setups and trades -------------------------------------------------------------------

    def receipt(self, symbol, now):
        state = {"market": "CRYPTO", "symbol": symbol, "fixture": str(uuid4())}
        reviewer = JevReviewer(
            self.reviews, ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 1, 0.25, 100000, 30),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply())),
            key_provider=lambda: FIXTURE_KEY,
        )
        result = asyncio.run(reviewer.jev_review(
            request_id=uuid4(),
            identity={"cycle_id": str(uuid4()), "candidate_revision": 1,
                      "research_item_key": "CRYPTO:" + symbol,
                      "evidence_hash": digest(encoded(state))},
            state=state, question_set=SKEPTIC, expires_at=datetime.now(UTC) + timedelta(seconds=10),
            purpose="ENGINEERING_TEST",
        ))
        assert result.status == "RECORDED"
        return result.receipt_ids[0]

    def setup(self, pick, *, arm, admitted_at, record=None, cycle_id=None):
        """A report-V3 setup (``record`` overrides the admission record, e.g. a V2 packet)."""
        sid = uuid4()
        receipt_id = self.receipt(pick.symbol, admitted_at)
        record = dict(record if record is not None else (pick.selection or pick.body))
        record["receipt_id"] = str(receipt_id)
        levels = record["levels"]
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO lab.managed_setups(setup_id,cycle_id,revision,symbol,market,
                strategy_version,policy_id,cohort,receipt_id,evidence_hash,expires_at,record_json)
                VALUES(%s,%s,1,%s,'CRYPTO','CRYPTO_STRUCTURAL_RETEST_TEST_V1',%s,%s,%s,%s,%s,%s)""",
                (sid, cycle_id or record["cycle_id"], pick.symbol, POLICY, COHORT, receipt_id,
                 record["evidence_hash"], record["expires_at"], Jsonb(json_safe(record))))
            v3 = crypto_trigger.applies(record)
            holding = crypto_holding.admission_policy(v3, arm)
            self.store.transition(
                conn, sid, "WATCHING", stop=str(levels["stop"]), target=str(levels["target"]),
                qty="0", lifecycle_id=str(uuid4()), admitted_at=admitted_at.isoformat(),
                receipt_id=str(receipt_id), exit_requested=None, crypto_day_policy=None,
                crypto_entry_deadline=None, crypto_flat_deadline=None,
                risk_policy_id="JEV_MANAGED_RISK_V3", arm=arm, arm_method=ARM_METHOD,
                fixed_exit_arm_pct=30,
                **({"holding_policy": holding.record()} if holding else {}),
                **({"system_check": {"version": SYSTEM_CHECK_VERSION, "result": "PASSED",
                                     "entry_type": "PULLBACK"},
                    "entry_type": "PULLBACK"} if v3 else {}),
                **({"trigger_version": crypto_trigger.CRYPTO_TRIGGER_VERSION} if v3 else {}),
                **gap_resume.admission_fields(record),
                **crypto_maintenance.admission_fields(record, arm),
            )
        return sid

    def fill(self, sid, *, side, qty, price, at, fee_usd=None, broker_order_id=None):
        fill_id = f"fixture-fill-{uuid4().hex[:16]}"
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO lab.managed_fills(fill_id,setup_id,broker_order_id,side,qty,price,
                filled_at,fee_usd,source) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'ALPACA_PAPER')""",
                (fill_id, sid, broker_order_id or f"fixture-order-{uuid4().hex[:12]}", side,
                 str(qty), str(price), at, None if fee_usd is None else str(fee_usd)))
            row = conn.execute("SELECT * FROM lab.managed_fills WHERE fill_id=%s",
                               (fill_id,)).fetchone()
        return row

    def transition(self, sid, state, **changes):
        with self.store.transaction() as conn:
            return self.store.transition(conn, sid, state, **changes)

    def open(self, sid, *, qty, price, at, arm):
        buy = self.fill(sid, side="buy", qty=qty, price=price, at=at)
        extra = {}
        if arm == "JEV_MANAGED":
            extra = {"day_review_at": (at + timedelta(hours=24)).isoformat(), "continuations": 0,
                     "hard_exit_at": (at + timedelta(hours=24, seconds=4800)).isoformat()}
        else:
            extra = {"hard_exit_at": (at + timedelta(hours=24)).isoformat()}
        self.transition(sid, "OPEN", qty=str(qty), fill_price=str(price),
                        opened_at=at.isoformat(), **extra)
        return buy

    def close(self, sid, *, qty, price, at, reason, fee_usd=None):
        sell = self.fill(sid, side="sell", qty=qty, price=price, at=at, fee_usd=fee_usd)
        self.transition(sid, "OPEN", exit_requested=reason if reason != "BROKER_EXIT" else None)
        self.transition(sid, "CLOSED", qty="0", closed_at=at.isoformat(), reason=reason)
        return sell

    def fee(self, fill, *, fee_usd, source="ALPACA_PAPER_ACTIVITY", base_qty="0",
            base_value="0", observed=None, supersedes=None):
        observed = observed or fill["filled_at"] + timedelta(seconds=2)
        return import_fill_cost_correction(self.store, {
            "correction_id": str(uuid4()), "setup_id": str(fill["setup_id"]),
            "fill_id": fill["fill_id"], "fill_event_seq": fill["event_seq"],
            "fee_usd": str(fee_usd), "base_asset_fee_qty": str(base_qty),
            "base_asset_fee_usd": str(base_value), "source": source,
            "broker_source_reference": "fixture-activity-" + fill["fill_id"][-8:],
            "observed_at": observed.isoformat(), "supersedes_correction_id": supersedes,
        }, recorded_at=observed + timedelta(seconds=1))

    def trade(self, pick, *, arm, entry_at, exit_at=None, exit_price=None, reason=None,
              qty=D("10"), fees="ALPACA_PAPER_ACTIVITY", changes=(), early_exit=False,
              continuations=0):
        """A whole trade: admitted, filled at the max entry (coin fee 0.25%), optionally
        changed by Jev, and closed with a USD sell fee of 0.25% of proceeds."""
        levels = pick.levels
        m = D(levels["max_entry_price"])
        sid = self.setup(pick, arm=arm, admitted_at=entry_at - timedelta(minutes=20))
        buy = self.open(sid, qty=qty, price=m, at=entry_at, arm=arm)
        coin_fee = (qty * TAKER).quantize(D("0.000000001"))
        if fees is not None:
            self.fee(buy, fee_usd=coin_fee * m, base_qty=coin_fee, base_value=coin_fee * m,
                     source=fees)
        stop, target = D(levels["stop"]), D(levels["target"])
        for at, new_stop, new_target in changes:
            self.maintenance_raise(sid, at=at, before=(stop, target), after=(new_stop, new_target))
            stop, target = new_stop, new_target
        if continuations:
            self.transition(sid, "OPEN", continuations=continuations,
                            day_review_at=(entry_at + timedelta(hours=24 * (continuations + 1))
                                           ).isoformat())
        if exit_at is None:
            return sid, buy, None
        if early_exit:
            self.early_exit(sid, at=exit_at - timedelta(minutes=5), levels=(stop, target))
        sold = qty - coin_fee
        proceeds = sold * D(str(exit_price))
        sell = self.close(sid, qty=sold, price=exit_price, at=exit_at, reason=reason)
        if fees is not None:
            cash = (proceeds * TAKER).quantize(D("0.01"))
            self.fee(sell, fee_usd=cash, source=fees)
        return sid, buy, sell

    @staticmethod
    def quote(bid, at):
        bid = D(str(bid))
        ask = bid * D("1.0005")
        return {"quote_source": "ALPACA_CRYPTO_STREAM", "bid": str(bid), "ask": str(ask),
                "mid": str((bid + ask) / 2), "quote_at": at.isoformat(),
                "read_at": at.isoformat(), "quote_age_seconds": "0", "last": str(bid),
                "last_at": at.isoformat(), "last_trade_id": "fixture-trade",
                "last_source": "ALPACA_CRYPTO_STREAM"}

    def maintenance_raise(self, sid, *, at, before, after, bid=None, top_p="0.78",
                          basis="SWING_LOW_15M"):
        action = ("RAISE_STOP_AND_TARGET" if after[0] > before[0] and after[1] > before[1]
                  else "RAISE_STOP" if after[0] > before[0] else "RAISE_TARGET")
        body = {
            "policy_id": "CRYPTO_MAINTENANCE_V2", "outcome": "APPLIED", "code": None,
            "action": action, "trade_reason": "INTACT",
            "answers": {"action": {"choice": action, "top_p": top_p},
                        "trade_reason": {"choice": "INTACT", "top_p": "0.81"}},
            "stop": {"old": str(before[0]), "new": str(after[0]), "option_id": "S1",
                     "bases": [basis]} if after[0] > before[0] else None,
            "target": {"old": str(before[1]), "new": str(after[1]), "option_id": "T1",
                       "bases": ["HIGH_24H"]} if after[1] > before[1] else None,
            "levels_before": {"stop": str(before[0]), "target": str(before[1])},
            "levels_after": {"stop": str(after[0]), "target": str(after[1])},
            "requested_at": (at - timedelta(seconds=8)).isoformat(),
            "answered_at": (at - timedelta(seconds=2)).isoformat(), "decided_at": at.isoformat(),
            "quote": self.quote(bid or after[0] * D("1.02"), at),
            "receipt_ids": [str(uuid4())], "trigger_reasons": ["BAR_1M"],
        }
        with self.store.transaction() as conn:
            self.event(conn, "MAINTENANCE_DECISION", body, setup_id=sid,
                       key=f"maintenance-decision:{uuid4()}", at=at)
            self.store.transition(conn, sid, "OPEN", stop=str(after[0]), target=str(after[1]))

    def maintenance_hold(self, sid, *, at, levels, bid, top_p="0.74"):
        body = {
            "policy_id": "CRYPTO_MAINTENANCE_V2", "outcome": "HELD", "code": None,
            "action": "HOLD", "trade_reason": "INTACT",
            "answers": {"action": {"choice": "HOLD", "top_p": top_p}},
            "levels_before": {"stop": str(levels[0]), "target": str(levels[1])},
            "levels_after": {"stop": str(levels[0]), "target": str(levels[1])},
            "decided_at": at.isoformat(), "quote": self.quote(bid, at),
            "receipt_ids": [str(uuid4())], "trigger_reasons": ["BAR_1M"],
        }
        with self.store.transaction() as conn:
            self.event(conn, "MAINTENANCE_DECISION", body, setup_id=sid,
                       key=f"maintenance-decision:{uuid4()}", at=at)

    def maintenance_failed(self, sid, *, at, code="REVIEW_UNAVAILABLE"):
        """A review Jev did not answer (trade_maintenance: outcome FAILED); levels unchanged."""
        body = {
            "policy_id": "CRYPTO_MAINTENANCE_V2", "outcome": "FAILED", "code": code,
            "action": None, "answers": None, "decided_at": at.isoformat(),
            "receipt_ids": [], "trigger_reasons": ["BAR_1M"],
        }
        with self.store.transaction() as conn:
            self.event(conn, "MAINTENANCE_DECISION", body, setup_id=sid,
                       key=f"maintenance-decision:{uuid4()}", at=at)

    def protective_decision(self, sid, *, action="AMEND", method="PATCH"):
        """An approved protective decision (stop change, cancel or exit): the engine records
        equity 0 on these (managed_execution._authorize_mutation)."""
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO lab.managed_risk_decisions(decision_id,setup_id,action,outcome,
                reason,method,path,payload,context,equity,expires_at)
                VALUES(%s,%s,%s,'APPROVED','FIXTURE_PROTECTION',%s,'/v2/orders/fixture-stop',
                %s,%s,0,clock_timestamp()+interval '5 seconds')""",
                (uuid4(), sid, action, method, Jsonb({}), Jsonb({"fixture": True})))

    def position_sample(self, sid, *, at, bid, qty):
        bid = D(str(bid))
        body = {"lifecycle_id": "fixture", "sampling_method": "FIXTURE", "price": str(bid),
                "bid": str(bid), "ask": str(bid * D("1.0005")), "position_qty": str(qty),
                "data_provider": "ALPACA", "data_feed": "CRYPTO_US",
                "market_data_timestamp": at.isoformat(), "received_at": at.isoformat()}
        with self.store.transaction() as conn:
            self.event(conn, "POSITION_MARKET_SNAPSHOT", body, setup_id=sid, at=at)

    def agent_review(self, sid, *, at, agent, decision, why, jev_decision, top_p="0.72"):
        review = str(uuid4())
        with self.store.transaction() as conn:
            self.event(conn, "DAY_REVIEW_AGENT_ANSWER", {
                "review_id": review, "setup_id": str(sid), "lifecycle_id": "fixture",
                "round": "FIRST", "agent_id": agent,
                "answer": {"schema_version": "AGENT_REVIEW_ANSWER_V1", "answer_id": str(uuid4()),
                           "decision": decision, "what_changed": why,
                           "next_24h": "Fixture expectation.", "proves_wrong": "Fixture."},
                "request_hash": "5" * 64, "received_at": (at - timedelta(minutes=20)).isoformat(),
            }, setup_id=sid, key=f"day-review-answer:{review}:FIRST",
                at=at - timedelta(minutes=20))
            self.event(conn, "DAY_REVIEW_DECISION", {
                "policy_id": "CRYPTO_24H_REVIEW_V2", "review_id": review, "setup_id": str(sid),
                "outcome": jev_decision, "code": "AGREED" if jev_decision == decision
                else "AGENT_SILENT_JEV_ALONE", "decided_at": at.isoformat(),
                "jev": {"results": [{"round": "FIRST", "status": "ANSWERED", "answer": {
                    "decision": jev_decision, "summary": {
                        "decision": {"choice": jev_decision, "top_p": top_p}}}}]},
            }, setup_id=sid, key=f"day-review-decision:{review}", at=at)

    def agent_flag(self, sid, *, at, agent, why, levels):
        flag_id = str(uuid4())
        with self.store.transaction() as conn:
            self.event(conn, "EXIT_FLAG_RAISED", {
                "flag_id": flag_id, "setup_id": str(sid), "side": "AGENT",
                "raised_by": {"agent_id": agent, "flag_ref": str(uuid4()),
                              "request_hash": "6" * 64},
                "reasons": {"what_changed": why, "next_24h": "Fixture.", "proves_wrong": "Fx."},
                "evidence": {"levels": {"stop": str(levels[0]), "target": str(levels[1])},
                             "sources": [{"source_id": "fixture-news",
                                          "excerpt": "Fixture source excerpt, never public."}]},
                "raised_at": at.isoformat()}, setup_id=sid, key=f"exit-flag:{flag_id}", at=at)
            self.event(conn, "EXIT_FLAG_RESOLVED", {
                "flag_id": flag_id, "outcome": "EXIT_AGREED",
                "resolved_at": (at + timedelta(minutes=4)).isoformat()},
                setup_id=sid, key=f"exit-flag-resolved:{flag_id}", at=at + timedelta(minutes=4))

    def equity_reading(self, sid, *, equity):
        """A REJECTED (capacity) entry decision: the engine records the account's equity on
        every risk decision, approved or not."""
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO lab.managed_risk_decisions(decision_id,setup_id,action,outcome,
                reason,method,path,payload,context,equity,expires_at)
                VALUES(%s,%s,'ENTRY','REJECTED','CAPACITY_FULL','NONE','',%s,%s,%s,
                clock_timestamp())""",
                (uuid4(), sid, Jsonb({}), Jsonb({"fixture": True}), str(equity)))

    def early_exit(self, sid, *, at, levels):
        flag_id = str(uuid4())
        with self.store.transaction() as conn:
            self.event(conn, "EXIT_FLAG_RAISED", {
                "flag_id": flag_id, "setup_id": str(sid), "side": "JEV",
                "reasons": {"what_changed": "Fixture reason"},
                "evidence": {"levels": {"stop": str(levels[0]), "target": str(levels[1])}},
                "raised_at": (at - timedelta(minutes=3)).isoformat(),
                "answer_due_at": (at + timedelta(minutes=12)).isoformat(),
                "trade_levels_unchanged": True}, setup_id=sid, key=f"exit-flag:{flag_id}",
                at=at - timedelta(minutes=3))
            self.event(conn, "EXIT_FLAG_RESOLVED", {
                "flag_id": flag_id, "outcome": "EXIT_AGREED", "resolved_at": at.isoformat(),
                "answered_by": "AGENT"}, setup_id=sid, key=f"exit-flag-resolved:{flag_id}",
                at=at)

    def reviews_off(self, sid):
        with self.store.transaction() as conn:
            self.store.event(conn, "POSITION_REVIEW_SKIPPED",
                             {"reason": "MANAGEMENT_REVIEWS_DISABLED", "lifecycle_id": "fixture"},
                             setup_id=sid)

    def heartbeat(self, **overrides):
        body = {"runtime_id": str(uuid4()), "paper_only": True, "entry_ready": True,
                "management_reviews": "ENABLED",
                "jev_breaker": {"state": "CLOSED", "epoch": 0, "blocked_until": None},
                "configuration_hash": "4" * 64, "execution_halts": {"count": 0},
                "account_hint": FIXTURE_ACCOUNT_MARKER, **overrides}
        with self.store.transaction() as conn:
            self.store.event(conn, "RUNTIME_HEARTBEAT", body)

    def engineering_trade(self, symbol="DOGE/USD", *, at):
        """An operator ENGINEERING_TEST enrollment (the real SQL function), its setup and a
        closed round trip: never part of the experiment."""
        operator = Repository(role_url(self.app_url, "catalyst_operator"))
        self.classify(symbol)
        with operator.connect() as conn:
            result = conn.execute(
                "SELECT lab.operator_enroll_managed_engineering(%s,%s,%s,%s,%s,%s,%s,%s) AS r",
                ("TEST-FIXTURE-" + uuid4().hex[:8].upper(), symbol, D("100"), D("100.10"),
                 D("95"), D("111"), 60, "Fixture engineering round trip (LAB_FIXTURE)"),
            ).fetchone()["r"]
        with self.app.connect() as conn:
            row = conn.execute("""SELECT event_seq,body->'packet' AS packet FROM lab.managed_events
                WHERE event_seq=%s""", (result["selection_event_seq"],)).fetchone()
        packet = {**row["packet"], "selection_event_seq": row["event_seq"]}
        sid = uuid4()
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO lab.managed_setups(setup_id,cycle_id,revision,symbol,market,
                strategy_version,policy_id,cohort,receipt_id,evidence_hash,expires_at,record_json)
                VALUES(%s,%s,%s,%s,'CRYPTO','CRYPTO_STRUCTURAL_RETEST_TEST_V1',%s,%s,NULL,%s,%s,%s)""",
                (sid, packet["cycle_id"], packet["revision"], symbol, POLICY, COHORT,
                 packet["evidence_hash"], packet["expires_at"], Jsonb(json_safe(packet))))
            self.store.transition(conn, sid, "WATCHING", stop="95", target="111", qty="0",
                                  lifecycle_id=str(uuid4()), admitted_at=at.isoformat(),
                                  arm="JEV_MANAGED", risk_policy_id="JEV_MANAGED_RISK_V3")
        buy = self.open(sid, qty=D("5"), price=D("100.10"), at=at, arm="JEV_MANAGED")
        self.fee(buy, fee_usd="0.25", source="ALPACA_PAPER_ACTIVITY")
        sell = self.close(sid, qty=D("5"), price=D("111"), at=at + timedelta(hours=2),
                          reason="TARGET_EXIT")
        self.fee(sell, fee_usd="1.39", source="ALPACA_PAPER_ACTIVITY")
        return sid

    def classify(self, symbol, sector="CRYPTO", theme="MEME"):
        from catalyst_lab.execution import system_event

        with self.store.transaction() as conn:
            event = system_event(self.store.repo, conn, "CLASSIFICATION_IMPORTED",
                                 {"ticker": symbol, "source": "LAB_FIXTURE"})
            conn.execute("INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                         (event["seq"], symbol, sector, theme, "LAB_FIXTURE"))


def enable_public_login(cluster_root):
    """Tests only: enable LOGIN for catalyst_public in a disposable cluster (production does
    this in the cloud provisioner from its own secret)."""
    from catalyst_lab import localdb

    with psycopg.connect(localdb.connection_url(cluster_root, "lab_owner"), autocommit=True) as c:
        c.execute("ALTER ROLE catalyst_public LOGIN")


# --- A whole demo experiment (screenshots and the leak test) ---------------------------------

DEMO_PRICES = {
    "BTC/USD": "64250", "ETH/USD": "2410", "SOL/USD": "142.3", "AVAX/USD": "23.1",
    "LINK/USD": "11.2", "DOGE/USD": "0.1034", "XRP/USD": "0.584", "LTC/USD": "66.7",
    "ADA/USD": "0.352", "DOT/USD": "4.21", "UNI/USD": "7.02", "AAVE/USD": "151.4",
    "ARB/USD": "0.522", "RENDER/USD": "5.11", "FIL/USD": "3.62", "BCH/USD": "331.5",
    "PEPE/USD": "0.00000812", "SHIB/USD": "0.0000142", "GRT/USD": "0.1571", "ONDO/USD": "0.743",
}
KINDS = ("CHART", "NEWS", "BOTH", "CHART")
OPEN_DRIFT = {9: D("0.992"), 10: D("1.012"), 11: D("1.012"), 12: D("1.018")}


def demo_levels(price):
    """Pullback levels: entry 1% below, max entry +0.2%, stop 3% below max, 2.4R target."""
    p = D(price)
    exponent = p.adjusted() - 5

    def r(value):
        return value.quantize(D(1).scaleb(exponent))

    entry = r(p * D("0.99"))
    maximum = r(entry * D("1.002"))
    stop = r(maximum * D("0.97"))
    target = r(maximum + (maximum - stop) * D("2.4"))
    return {"entry_trigger": str(entry), "max_entry_price": str(maximum), "stop": str(stop),
            "target": str(target)}


def demo_run(ledger, slot, order, *, declined=None):
    """20 picks: 16 ranked (top 10 selected), 2 vetoed, 2 not ranked."""
    picks = []
    for i, symbol in enumerate(order):
        status = "VETOED" if i in (16, 17) else "NOT_RANKED" if i in (18, 19) else "RANKED"
        rank = i + 1 if status == "RANKED" else None
        picks.append(Pick(symbol, kind=KINDS[i % 4], levels=demo_levels(DEMO_PRICES[symbol]),
                          agent_price=str(D(DEMO_PRICES[symbol])), status=status, jev_rank=rank,
                          selected=status == "RANKED" and rank <= 10,
                          declined=declined if i == 2 else None,
                          thesis=LONG_THESIS if i == 5 else None))
    if declined:
        picks[10].selected, picks[10].replacement_for = True, picks[2].item_key or (
            "CRYPTO:" + picks[2].symbol)
    return ledger.run(slot, picks, submitted=21)


def demo_exit(levels, outcome):
    m, s, p = (D(levels[k]) for k in ("max_entry_price", "stop", "target"))
    return {"TARGET": p, "STOP": s * D("0.998"), "HOLD": m + (m - s) * D("0.4"),
            "LOSS_HOLD": m - (m - s) * D("0.35"), "EARLY": m - (m - s) * D("0.2")}[outcome]


def build_demo_experiment(ledger, now):
    """Four daily research runs by one agent, twelve trades (open and closed, both arms), Jev's
    raises and holds, a 24-hour review, an agent exit flag, fee records, live position samples,
    an equity reading and a current heartbeat."""
    today = now.astimezone(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    if today > now - timedelta(hours=2):
        today -= timedelta(days=1)
    slots = [today - timedelta(days=k) for k in (3, 2, 1, 0)]
    symbols = list(DEMO_PRICES)
    runs = []
    for index, slot in enumerate(slots):
        order = symbols[index * 3:] + symbols[:index * 3]
        runs.append(demo_run(ledger, slot, order,
                             declined="PRICE_MISMATCH" if index == 1 else None))
    plan = [  # (run, rank, arm, hours after the slot, outcome or None if open, hours held)
        (0, 1, "JEV_MANAGED", 2, "TARGET", 9), (0, 2, "JEV_MANAGED", 3, "STOP", 4),
        (0, 4, "FIXED_EXIT", 5, "HOLD", 24), (1, 2, "JEV_MANAGED", 1, "EARLY", 13),
        (1, 4, "FIXED_EXIT", 2, "STOP", 6), (1, 5, "JEV_MANAGED", 4, "LOSS_HOLD", 24),
        (2, 1, "JEV_MANAGED", 1, "TARGET", 5), (2, 3, "FIXED_EXIT", 2, "TARGET", 15),
        (2, 6, "JEV_MANAGED", 3, None, 0), (3, 1, "JEV_MANAGED", 1, None, 0),
        (3, 2, "FIXED_EXIT", 1, None, 0), (3, 7, "JEV_MANAGED", 1, None, 0),
    ]
    trades = []
    for number, (run_index, rank, arm, after, outcome, held) in enumerate(plan, 1):
        run = runs[run_index]
        pick = next(p for p in run.picks.values() if p.jev_rank == rank)
        levels = pick.levels
        m, s, p = (D(levels[k]) for k in ("max_entry_price", "stop", "target"))
        entry_at = run.run_slot + timedelta(hours=after, minutes=7 * number)
        qty = (D("1000") / m).quantize(D("0.0001") if m > 1 else D("1"))
        changes = []
        if arm == "JEV_MANAGED" and outcome in ("TARGET", None) and number % 2 == 1:
            raised = (m - (m - s) * D("0.3")).quantize(s)
            changes.append((entry_at + timedelta(minutes=45), raised, p))
        if outcome is None:
            continued = int((now - entry_at).total_seconds() // 86400)
            sid = ledger.trade(pick, arm=arm, entry_at=entry_at, qty=qty, changes=changes,
                               continuations=continued if arm == "JEV_MANAGED" else 0)[0]
            stop = changes[-1][1] if changes else s
            bid = (m * OPEN_DRIFT[number]).quantize(s)
            held_qty = qty - (qty * TAKER).quantize(D("0.000000001"))
            if arm == "JEV_MANAGED" and continued:
                ledger.agent_review(sid, at=entry_at + timedelta(hours=24),
                                    agent=FIXTURE_AGENT_ID, decision="CONTINUE",
                                    why="Trend intact above the raised stop; volume steady.",
                                    jev_decision="CONTINUE")
            if arm == "JEV_MANAGED":  # Jev reviews every completed 1-minute bar.
                for minutes in range(9, 0, -1):
                    at = now - timedelta(minutes=minutes)
                    if number == 12 and minutes == 4:
                        ledger.maintenance_raise(sid, at=at, before=(stop, p), after=(m, p),
                                                 bid=bid, basis="BREAKEVEN", top_p="0.81")
                        stop = m
                    else:
                        ledger.maintenance_hold(sid, at=at, levels=(stop, p), bid=bid)
            ledger.position_sample(sid, at=now - timedelta(seconds=40), bid=bid, qty=held_qty)
        else:
            exit_price = demo_exit(levels, outcome).quantize(s)
            reason = {"TARGET": "TARGET_EXIT", "STOP": "BROKER_EXIT",
                      "HOLD": "HOLD_24H_EXIT" if arm == "FIXED_EXIT" else "DAY_REVIEW_EXIT",
                      "LOSS_HOLD": "DAY_REVIEW_EXIT" if arm == "JEV_MANAGED"
                      else "HOLD_24H_EXIT", "EARLY": "EARLY_EXIT_AGREED"}[outcome]
            fees = None if number == 8 else "ALPACA_PAPER_ACTIVITY"
            exit_at = entry_at + timedelta(hours=held)
            sid = ledger.trade(pick, arm=arm, entry_at=entry_at, exit_at=exit_at,
                               exit_price=exit_price, reason=reason, qty=qty, fees=fees,
                               changes=changes)[0]
            if outcome == "EARLY":
                ledger.agent_flag(sid, at=exit_at - timedelta(minutes=9), agent=FIXTURE_AGENT_ID,
                                  why="Exchange listing news reversed; the catalyst is gone.",
                                  levels=(s, p))
            if reason == "DAY_REVIEW_EXIT":
                ledger.agent_review(sid, at=exit_at - timedelta(minutes=2),
                                    agent=FIXTURE_AGENT_ID, decision="EXIT",
                                    why="Momentum faded and the reason for the trade is gone.",
                                    jev_decision="EXIT", top_p="0.83")
        trades.append({"setup_id": sid, "pick": pick, "arm": arm, "entry_at": entry_at,
                       "outcome": outcome})
    ledger.equity_reading(trades[-1]["setup_id"], equity="10084.27")
    ledger.heartbeat()
    return runs, trades

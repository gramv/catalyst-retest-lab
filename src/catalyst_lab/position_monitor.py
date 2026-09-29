"""The second Jev role: contextual position review, separate from protection ticks."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import NAMESPACE_URL, uuid4, uuid5

from catalyst_lab.account_risk import FIXED_EXIT_ARM
from catalyst_lab.jev_contract import QuestionSet, digest, encoded, strict_json, validated_answers
from catalyst_lab.jev_review import ReviewResult
from catalyst_lab.managed_dossier import ContextBudgetUnsatisfiable
from catalyst_lab.managed_engineering import is_engineering
from catalyst_lab.managed_review import (
    CONTEXT_VERSION,
    EXIT_POLICY,
    MANAGED_COHORT,
    CompletedBar,
    ManagedContext,
    ManagedPolicy,
    NewsEvidence,
    PositionSnapshot,
    ProtectiveOrder,
    build_managed_context,
    managed_questions,
)
from catalyst_lab.repository import json_safe

RETAINED_VERSION = "MANAGED_POSITION_RETAINED_REFERENCES_V1"
# Staging switch for management reviews (plan 0.10 / R4), from the required launch value
# MANAGED_MANAGEMENT_REVIEWS. DISABLED sends nothing to Jev; protection and mechanical exits
# run in the execution loop either way.
MANAGEMENT_REVIEWS_ENABLED = "ENABLED"
MANAGEMENT_REVIEWS_DISABLED = "DISABLED"
MANAGEMENT_REVIEWS_SETTINGS = frozenset({MANAGEMENT_REVIEWS_ENABLED, MANAGEMENT_REVIEWS_DISABLED})
HISTORY_KINDS = (
    "MANAGED_JEV_JUDGMENT", "MANAGEMENT_PLAN_AUTHORIZED", "POSITION_REVIEW_OBSOLETE",
    "POSITION_REVIEW_RECOVERED", "AMENDMENT_REJECTED", "MANAGEMENT_EXPIRED",
)


def review_bar_window(bars, structural_bars, policy):
    """Every raw bar a review can use (and so every bar that can back an option), once.

    The runtime passes one window as both arguments; the result is then that window's
    newest ``max(max_bars, max_structural_bars)`` bars, oldest first.
    """
    window = list(structural_bars[-policy.max_structural_bars:])
    window.extend(b for b in bars[-policy.max_bars:] if b not in window)
    return window[-max(policy.max_bars, policy.max_structural_bars):]


def review_bars_event(bars, setup_id):
    """``POSITION_REVIEW_BARS`` body and its content-derived key; one row per window."""
    body = json_safe({"bars": [asdict(b) for b in bars]})
    return body, f"position-review-bars:{setup_id}:{digest(encoded(body))}"


@dataclass(frozen=True)
class MonitorTriggerPolicy:
    """Scheduling only: never changes prices, quantity or the hard exit deadline."""

    policy_id: str
    minimum_interval_seconds: int
    near_target_progress_fraction: D | None

    def __post_init__(self):
        threshold = self.near_target_progress_fraction
        if (
            self.policy_id != "JEV_MONITOR_SCHEDULING_ENGINEERING_V1"
            or type(self.minimum_interval_seconds) is not int
            or not 1 <= self.minimum_interval_seconds <= 60
            or (threshold is not None and (
                not isinstance(threshold, D) or not threshold.is_finite() or not 0 < threshold < 1
            ))
        ):
            raise ValueError("EXPLICIT_MONITOR_TRIGGER_POLICY_REQUIRED")


def engineering_monitor_trigger_policy():
    return MonitorTriggerPolicy("JEV_MONITOR_SCHEDULING_ENGINEERING_V1", 5, D("0.8"))


class PositionMonitor:
    def __init__(self, execution, reviewer, *, policy: ManagedPolicy, review_seconds: int, clock,
                 management_reviews: str, trigger_policy: MonitorTriggerPolicy | None = None):
        if review_seconds != 10:
            raise ValueError("APPROVED_REVIEW_DEADLINE_REQUIRED")
        if management_reviews not in MANAGEMENT_REVIEWS_SETTINGS:
            raise ValueError("MANAGEMENT_REVIEWS_SETTING_REQUIRED")
        execution.repo.require_same_database(reviewer.store)
        self.execution, self.reviewer, self.policy = execution, reviewer, policy
        self.review_seconds, self.now = review_seconds, clock
        self.management_reviews = management_reviews
        self.trigger_policy = trigger_policy

    @property
    def reviews_enabled(self):
        return self.management_reviews == MANAGEMENT_REVIEWS_ENABLED

    def current_evidence(self, setup):
        from catalyst_lab.position_news import current_position_evidence

        with self.execution.repo.connect() as conn:
            state = self.execution.store.state(conn, setup["setup_id"])
            return current_position_evidence(conn, setup, state.get("lifecycle_id"))

    def snapshot(self, setup_id, observation):
        setup, state, orders, roles, position, uncertain = self.execution._broker_view(
            setup_id, read_only=True
        )
        if uncertain or not position or state["state"] != "OPEN":
            raise ValueError("POSITION_NOT_READY_FOR_REVIEW")
        qty = D(position["qty"])
        valid = {"new", "accepted", "partially_filled"}
        stops = [
            o
            for o in orders
            if roles[o["id"]] == "PROTECT"
            and o["status"] in valid | {"held"}  # Rule A: a filled bracket's stop stays held.
            and D(o["qty"]) - D(o.get("filled_qty", "0")) == qty
        ]
        targets = [
            o
            for o in orders
            if roles[o["id"]] == "TARGET"
            and o["status"] in valid
            and D(o["qty"]) - D(o.get("filled_qty", "0")) == qty
        ]
        if len(stops) != 1 or (setup["market"] == "US_STOCKS" and len(targets) != 1):
            raise ValueError("PROTECTION_RECOVERY_BEFORE_REVIEW")
        stop_order = stops[0]
        stop = ProtectiveOrder(
            stop_order["id"],
            "STOP",
            D(stop_order["stop_price"]),
            qty,
            "ACTIVE",
            state["revision"],
            "BROKER",
        )
        if setup["market"] == "US_STOCKS":
            order = targets[0]
            target = ProtectiveOrder(
                order["id"],
                "TARGET",
                D(order["limit_price"]),
                qty,
                "ACTIVE",
                state["revision"],
                "BROKER",
            )
            tick = D(".01") if D(observation["bid"]) >= 1 else D(".0001")
        else:
            target = ProtectiveOrder(
                str(setup_id),
                "TARGET",
                D(state["target"]),
                qty,
                "ACTIVE",
                state["revision"],
                "MECHANICAL_LOCAL",
            )
            tick = D(self.execution.broker.asset(setup["symbol"])["price_increment"])
        with self.execution.repo.connect() as conn:
            fills = conn.execute(
                """SELECT coalesce(sum(qty),0) AS qty FROM lab.managed_fills
                WHERE setup_id=%s AND side='buy'""",
                (setup_id,),
            ).fetchone()["qty"]
        if fills < qty:
            raise ValueError("BROKER_FILL_LEDGER_RECOVERY_REQUIRED")
        packet = setup["record_json"]
        news_revision, _ = self.current_evidence(setup)
        source_ids = tuple(
            str(uuid5(NAMESPACE_URL, source["content_hash"])) for source in packet["sources"]
        )
        quote_id = str(uuid5(NAMESPACE_URL, digest(str(observation))))
        return PositionSnapshot(
            candidate_id=str(setup_id),
            position_id=str(setup_id),
            lifecycle_id=state["lifecycle_id"],
            context_revision=state["revision"],
            market="US" if setup["market"] == "US_STOCKS" else "CRYPTO",
            symbol=setup["symbol"],
            strategy_version=setup["strategy_version"],
            exit_policy=EXIT_POLICY,
            cohort=MANAGED_COHORT,
            thesis=packet["thesis"],
            disproof=packet["disproof"],
            original_evidence_ids=source_ids,
            original_fill_price=D(state["fill_price"]),
            filled_qty=fills,
            remaining_qty=qty,
            opened_at=datetime.fromisoformat(state["opened_at"]),
            hard_exit_at=datetime.fromisoformat(state["hard_exit_at"]),
            quote_id=quote_id,
            bid=D(observation["bid"]),
            ask=D(observation["ask"]),
            quote_at=datetime.fromisoformat(observation["quote_at"]),
            data_provider=observation["data_provider"],
            data_feed=observation["data_feed"],
            feed_healthy=observation["feed_healthy"],
            tick_size=tick,
            stop=stop,
            target=target,
            state="OPEN",
            news_revision=news_revision,
            snapshot_at=self.now(),
        )

    @staticmethod
    def _bar(bar):
        if isinstance(bar, CompletedBar):
            return bar
        return CompletedBar(
            str(uuid5(NAMESPACE_URL, bar.source_id)), bar.start_at, bar.end_at,
            bar.open, bar.high, bar.low, bar.close, bar.volume, bar.provider, bar.feed,
        )

    def _source_subset(self, sources):
        # Retain adverse/withdrawn evidence before source-order ties; identical
        # syndicated excerpts are one model input, with all copies kept in the ledger.
        ordered = sorted(enumerate(sources), key=lambda row: (
            row[1].get("stance") not in {"ADVERSE", "WITHDRAWN"}, row[0]
        ))
        seen, selected = set(), []
        for _, source in ordered:
            if source["content_hash"] not in seen:
                selected.append(source)
                seen.add(source["content_hash"])
            if len(selected) == self.policy.max_news:
                break
        return selected

    @staticmethod
    def _news(source, revision, origin):
        return NewsEvidence(
            str(uuid5(NAMESPACE_URL, source["content_hash"])), revision,
            source["source_id"], source["excerpt"],
            datetime.fromisoformat(source["retrieved_at"]), source["content_hash"],
            url=source.get("url"),
            published_at=datetime.fromisoformat(source["published_at"])
            if source.get("published_at") else None,
            primary_source=source.get("primary_source"),
            asset_relevant=source.get("asset_relevant"), novelty=source.get("novelty"),
            stance=source.get("stance"), origin=origin,
        )

    def _selection_judgment(self, receipt_id):
        if not receipt_id:
            return None
        if not self.reviewer.store.verify(receipt_id)["valid"]:
            raise ValueError("SELECTION_RECEIPT_INTEGRITY_FAILED")
        with self.reviewer.store.connect() as conn:
            row = conn.execute(
                """SELECT r.response_bytes,r.outcome,r.actual_model,r.completed_at,
                q.question_set_version,q.stage,q.request_json
                FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)
                WHERE r.receipt_id=%s""", (receipt_id,),
            ).fetchone()
        if not row or row["outcome"] != "VALID":
            raise ValueError("SELECTION_RECEIPT_INTEGRITY_FAILED")
        template = QuestionSet(row["question_set_version"], row["stage"],
                               encoded(strict_json(row["request_json"])["questions"]))
        return {
            "receipt_id": str(receipt_id), "actual_model": row["actual_model"],
            "question_set_version": row["question_set_version"],
            "completed_at": row["completed_at"].isoformat(),
            "answers": validated_answers(bytes(row["response_bytes"]), template),
        }

    def dossier(self, setup, snapshot, sources):
        from catalyst_lab.managed_measurement import managed_measurement

        packet = setup["record_json"]
        original = packet.get("state", packet)
        with self.execution.repo.connect() as conn:
            history = conn.execute(
                """SELECT event_seq,kind,body FROM lab.managed_events WHERE setup_id=%s
                AND kind IN ('MANAGED_JEV_JUDGMENT','MANAGEMENT_PLAN_AUTHORIZED',
                             'POSITION_REVIEW_OBSOLETE','POSITION_REVIEW_RECOVERED')
                ORDER BY event_seq DESC LIMIT %s""",
                (setup["setup_id"], self.policy.max_history),
            ).fetchall()
        measured = managed_measurement(self.execution.repo, setup["setup_id"], as_of=self.now())
        return {
            "original_research": {k: original.get(k, packet.get(k)) for k in (
                "thesis", "disproof", "economic_relationship", "catalyst", "technical_context",
                "technical_facts", "levels",
            )},
            "selection_judgment": {
                "eligibility": self._selection_judgment(packet.get("receipt_id")),
                "quality": self._selection_judgment(packet.get("quality_receipt_id")),
            },
            "management_history": [
                {"event_seq": row["event_seq"], "kind": row["kind"], "decision": row["body"]}
                for row in reversed(history)
            ],
            "sampled_excursions": {k: measured[k] for k in (
                "sampling_method", "sample_count", "first_sample_at", "last_sample_at",
                "max_observation_gap_seconds", "observed_mfe_pnl", "observed_mae_pnl",
                "provenance", "limitations",
            )},
            "evidence_manifest": {
                "research_evidence_hash": packet.get("evidence_hash"),
                "original_source_hashes": [s["content_hash"] for s in packet["sources"]],
                "current_source_hashes": [s["content_hash"] for s in sources],
                "included_original_source_hashes": [
                    s["content_hash"] for s in self._source_subset(packet["sources"])
                ],
                "included_current_source_hashes": [
                    s["content_hash"] for s in self._source_subset(sources)
                ],
                "management_history_limit": self.policy.max_history,
                "full_evidence_storage": "APPEND_ONLY_RESEARCH_POSITION_AND_RECEIPT_LEDGERS",
            },
        }

    @staticmethod
    def _distinct(sources):
        """One input per excerpt hash, in ledger order; V3's compiler orders and selects."""
        seen, result = set(), []
        for source in sources:
            if source["content_hash"] not in seen:
                seen.add(source["content_hash"])
                result.append(source)
        return result

    def _news_references(self, conn, setup, lifecycle_id):
        """Where each excerpt hash is recorded: the newest ledger event, never a copy."""
        references = {}
        rows = conn.execute(
            """SELECT event_seq,body->'sources' AS sources FROM lab.managed_events
            WHERE kind='POSITION_NEWS' AND setup_id=%s AND body->>'lifecycle_id'=%s
            ORDER BY event_seq DESC""", (setup["setup_id"], str(lifecycle_id)),
        ).fetchall()
        packet = setup["record_json"]
        research = conn.execute(
            """SELECT event_seq,body->'state'->'sources' AS sources FROM lab.managed_events
            WHERE kind='RESEARCH_PACKET' AND body->>'cycle_id'=%s AND body->>'item_key'=%s
            AND (body->>'revision')::int>%s ORDER BY event_seq DESC LIMIT 1""",
            (str(setup["cycle_id"]), packet.get("item_key"), setup["revision"]),
        ).fetchone()
        groups = [("POSITION_NEWS", row["event_seq"], row["sources"]) for row in rows]
        if research:
            groups.append(("RESEARCH_PACKET", research["event_seq"], research["sources"]))
        groups.append(("MANAGED_SETUP", setup["event_seq"], packet["sources"]))
        for kind, seq, sources in groups:
            for source in sources or ():
                references.setdefault(source["content_hash"], {"kind": kind, "event_seq": seq})
        return references

    def dossier_v3(self, setup, snapshot):
        """V3 inputs: the packet's research state, receipt-verified selection answers,
        recent management events, SQL-aggregated excursions and ledger references."""
        from catalyst_lab.managed_measurement import sampled_excursions

        packet = setup["record_json"]
        original = packet.get("state", packet)
        with self.execution.repo.connect() as conn:
            history = conn.execute(
                """SELECT event_seq,kind,body FROM lab.managed_events WHERE setup_id=%s
                AND kind=ANY(%s) ORDER BY event_seq DESC LIMIT %s""",
                (setup["setup_id"], list(HISTORY_KINDS), 4 * self.policy.max_history),
            ).fetchall()
            news = self._news_references(conn, setup, snapshot.lifecycle_id)
        measured = sampled_excursions(
            self.execution.repo, setup["setup_id"], snapshot.lifecycle_id, as_of=self.now()
        )
        return {
            # The review state's rationale never carries agent_confidence (research_dossier).
            "original_research": {k: original.get(k, packet.get(k)) for k in (
                "thesis", "disproof", "economic_relationship", "catalyst", "technical_context",
                "technical_facts", "levels", "rationale",
            )},
            "selection_judgment": {
                "eligibility": self._selection_judgment(packet.get("receipt_id")),
                "quality": self._selection_judgment(packet.get("quality_receipt_id")),
            },
            "management_history": [
                {"event_seq": row["event_seq"], "kind": row["kind"], "decision": row["body"]}
                for row in reversed(history)
            ],
            "sampled_excursions": measured,
            "retained": {
                "version": RETAINED_VERSION,
                "research_packet": {
                    "setup_event_seq": setup["event_seq"],
                    "selection_event_seq": packet.get("selection_event_seq"),
                    "evidence_hash": packet.get("evidence_hash"),
                    "record_sha256": digest(encoded(packet)),
                },
                "selection_receipts": {
                    "eligibility": packet.get("receipt_id"),
                    "quality": packet.get("quality_receipt_id"),
                },
                "news_sources": news,
                "management_history_seqs": sorted(row["event_seq"] for row in history),
                "excursion_samples": {k: measured[k] for k in (
                    "aggregate_version", "sample_count", "first_event_seq", "last_event_seq",
                )},
            },
        }

    def _retain_bars(self, conn, setup_id, bars, structural_bars):
        """Persist every bar this review can use (idempotent) and return its reference."""
        window = review_bar_window(bars, structural_bars, self.policy)
        body, key = review_bars_event(window, setup_id)
        row = self.execution.store.event(
            conn, "POSITION_REVIEW_BARS", body, setup_id=setup_id, key=key
        )
        return {"kind": "POSITION_REVIEW_BARS", "event_seq": row["event_seq"],
                "sha256": digest(encoded(body)), "bar_count": len(window)}

    def _skip_unsatisfiable(self, conn, setup_id, snapshot, trigger, exc):
        """One POSITION_REVIEW_SKIPPED per lifecycle and cause; protection is unaffected."""
        key = f"position-review-skipped:{setup_id}:{snapshot.lifecycle_id}:{exc.code}"
        if conn.execute(
            "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
        ).fetchone():
            return
        self.execution.store.event(conn, "POSITION_REVIEW_SKIPPED", {
            "reason": exc.code, "lifecycle_id": snapshot.lifecycle_id,
            "context_revision": snapshot.context_revision,
            "news_revision": snapshot.news_revision, "state_byte_budget": exc.budget,
            "smallest_state_bytes": exc.state_bytes, "trigger_reasons": trigger["reasons"],
            "manifest": exc.manifest,
        }, setup_id=setup_id, key=key)

    @staticmethod
    def _position_fingerprint(snapshot):
        def stable(value):
            return str(value.normalize()) if isinstance(value, D) else str(value)

        return digest(encoded({
            "filled_qty": stable(snapshot.filled_qty),
            "remaining_qty": stable(snapshot.remaining_qty),
            "fill_price": stable(snapshot.original_fill_price),
            "hard_exit_at": snapshot.hard_exit_at.isoformat(),
            "stop": {k: stable(v) for k, v in asdict(snapshot.stop).items() if k != "revision"},
            "target": {k: stable(v) for k, v in asdict(snapshot.target).items() if k != "revision"},
        }))

    def _trigger(self, conn, setup_id, snapshot, bar_key):
        """Use durable evidence changes; quote oscillation alone never buys another vote."""
        lifecycle = snapshot.lifecycle_id
        latest = conn.execute(
            """SELECT body FROM lab.managed_events WHERE setup_id=%s
            AND kind='POSITION_REVIEW_REQUEST'
            AND body->'context'->'identity'->>'lifecycle_id'=%s
            ORDER BY event_seq DESC LIMIT 1""", (setup_id, lifecycle),
        ).fetchone()
        previous = latest["body"] if latest else None
        if previous:
            done = conn.execute(
                """SELECT 1 FROM lab.managed_events WHERE setup_id=%s AND kind IN
                ('MANAGED_JEV_JUDGMENT','POSITION_REVIEW_OBSOLETE')
                AND body->>'request_id'=%s""", (setup_id, previous["request_id"]),
            ).fetchone()
            if not done:
                return None
        bar_stamp = bar_key.isoformat() if bar_key else None
        if self.trigger_policy is None:
            if bar_stamp is None:
                return None
            return {
                "policy_id": "LEGACY_COMPLETED_BAR_NEWS_V1",
                "reasons": ["COMPLETED_BAR_OR_NEWS"], "bar_end_at": bar_stamp,
                "news_revision": snapshot.news_revision,
                "dedup_key": f"position-review:{setup_id}:{lifecycle}:{bar_stamp}:"
                f"{snapshot.news_revision}",
            }
        fingerprint = self._position_fingerprint(snapshot)
        span = snapshot.target.price - snapshot.original_fill_price
        progress = (snapshot.bid - snapshot.original_fill_price) / span if span > 0 else None
        threshold = self.trigger_policy.near_target_progress_fraction
        near = threshold is not None and progress is not None and threshold <= progress < 1
        prior = previous.get("trigger", {}) if previous else {}
        reasons = []
        if not previous:
            reasons.append("INITIAL_OPEN_CONTEXT")
        else:
            previous_bar = prior.get("bar_end_at")
            if bar_key and (not previous_bar or bar_key > datetime.fromisoformat(previous_bar)):
                reasons.append("COMPLETED_BAR")
            if snapshot.news_revision != previous["context"]["identity"]["news_revision"]:
                reasons.append("MATERIAL_NEWS")
            if fingerprint != prior.get("position_fingerprint"):
                reasons.append("POSITION_OR_PROTECTION_CHANGED")
        near_seen = near and conn.execute(
            """SELECT 1 FROM lab.managed_events WHERE setup_id=%s
            AND kind='POSITION_REVIEW_REQUEST'
            AND body->'context'->'identity'->>'lifecycle_id'=%s
            AND body->'trigger'->>'near_target_fingerprint'=%s LIMIT 1""",
            (setup_id, lifecycle, fingerprint),
        ).fetchone()
        if near and not near_seen:
            reasons.append("NEAR_TARGET")
        if not reasons:
            return None
        if previous:
            requested = datetime.fromisoformat(previous.get(
                "requested_at", previous["context"]["snapshot"]["snapshot_at"]
            ))
            elapsed = (self.now() - requested).total_seconds()
            if elapsed < self.trigger_policy.minimum_interval_seconds:
                return None
        signal = {
            "policy_id": self.trigger_policy.policy_id,
            "minimum_interval_seconds": self.trigger_policy.minimum_interval_seconds,
            "near_target_progress_fraction": str(threshold) if threshold is not None else None,
            "reasons": reasons, "bar_end_at": bar_stamp, "news_revision": snapshot.news_revision,
            "position_fingerprint": fingerprint,
            "near_target_fingerprint": fingerprint if near else None,
        }
        signal["dedup_key"] = f"position-review:{setup_id}:{lifecycle}:" + digest(encoded(signal))
        return signal

    def review_withheld(self, setup_id):
        """True when no management review may run for this setup (plan 0.10).

        An operator ENGINEERING_TEST setup is never reviewed, so Jev never receives it; while
        the staging switch is DISABLED no position is. POSITION_REVIEW_SKIPPED is written once
        per lifecycle and reason. Protection and mechanical exits run in the execution loop
        either way.
        """
        setup, state = self.execution._load(setup_id)
        if is_engineering(setup["record_json"]):
            reason = "ENGINEERING_TEST"
        elif not self.reviews_enabled:
            reason = "MANAGEMENT_REVIEWS_DISABLED"
        else:
            return False
        lifecycle = state.get("lifecycle_id")
        key = f"position-review-skipped:{setup_id}:{lifecycle}:{reason}"
        with self.execution.repo.connect() as conn:
            recorded = conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone()
        if not recorded:
            self.execution._event(
                "POSITION_REVIEW_SKIPPED",
                {"reason": reason, "lifecycle_id": lifecycle},
                setup_id,
                key=key,
            )
        return True

    def _fixed_exit_arm(self, setup_id):
        """The randomized FIXED_EXIT control arm (plan 2.2) is never reviewed; its original
        bracket, protection and mechanical exits still run in the execution loop."""
        _, state = self.execution._load(setup_id)
        if state.get("arm") != FIXED_EXIT_ARM:
            return False
        key = f"position-review-skipped:{setup_id}:{state.get('lifecycle_id')}"
        with self.execution.repo.connect() as conn:
            recorded = conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone()
        if not recorded:
            self.execution._event(
                "POSITION_REVIEW_SKIPPED",
                {"reason": "FIXED_EXIT_ARM", "lifecycle_id": state.get("lifecycle_id")},
                setup_id,
                key=key,
            )
        return True

    async def review(self, setup_id, observation, bars, *, fresh_observation, structural_bars=()):
        """Coalesce fresh evidence changes into one bounded, receipt-bound review."""
        if self.review_withheld(setup_id) or self._fixed_exit_arm(setup_id):
            return None
        self.recover_pending(setup_id, fresh_observation=fresh_observation)
        setup, _ = self.execution._load(setup_id)
        news_revision, sources = self.current_evidence(setup)
        snapshot = self.snapshot(setup_id, observation)
        if snapshot.news_revision != news_revision:
            return None
        converted = tuple(self._bar(b) for b in bars[-self.policy.max_bars:])
        seen = {b.observation_id for b in converted}
        structural = tuple(self._bar(b) for b in structural_bars[-self.policy.max_structural_bars:])
        structural = tuple(b for b in structural if b.observation_id not in seen)
        bar_key = max((b.ends_at for b in converted), default=None)
        with self.execution.store.transaction() as conn:
            trigger = self._trigger(conn, setup_id, snapshot, bar_key)
            if not trigger or conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                (trigger["dedup_key"],),
            ).fetchone():
                return None
        # V3 hands every distinct source to the budgeted compiler, which selects adverse
        # evidence first and records the rest in its manifest; V2 preselects here.
        v3 = self.policy.context_version == CONTEXT_VERSION
        subset = self._distinct if v3 else self._source_subset
        news = tuple(self._news(s, news_revision, "CURRENT_EVIDENCE") for s in subset(sources))
        original = tuple(self._news(s, min(setup["revision"], news_revision), "ORIGINAL_RESEARCH")
                         for s in subset(setup["record_json"]["sources"]))
        dossier = self.dossier_v3(setup, snapshot) if v3 else self.dossier(setup, snapshot, sources)
        now = self.now()
        request_id = uuid4()
        with self.execution.store.transaction() as conn:
            # Repeat the claim while holding the shared lock: concurrent callers cannot
            # both see an empty slot and issue two judgments for the same observations.
            trigger = self._trigger(conn, setup_id, snapshot, bar_key)
            if not trigger or conn.execute(
                "SELECT 1 FROM lab.managed_events WHERE idempotency_key=%s",
                (trigger["dedup_key"],),
            ).fetchone():
                return None
            from catalyst_lab.position_news import current_position_evidence

            current = self.execution.store.state(conn, setup_id)
            revision, _ = current_position_evidence(conn, setup, snapshot.lifecycle_id)
            if (current.get("state") != "OPEN"
                    or current.get("revision") != snapshot.context_revision
                    or revision != snapshot.news_revision):
                return None
            if v3:
                dossier["retained"]["market_bars"] = self._retain_bars(
                    conn, setup_id, bars, structural_bars
                )
            try:
                context = build_managed_context(
                    snapshot, converted, news, self.policy, now=now,
                    review_deadline=now + timedelta(seconds=self.review_seconds),
                    structural_bars=structural, original_news=original, dossier=dossier,
                    trigger=trigger,
                )
            except ContextBudgetUnsatisfiable as exc:
                # Skipped, never sent oversized; mechanical protection is unaffected.
                self._skip_unsatisfiable(conn, setup_id, snapshot, trigger, exc)
                return None
            self.execution.store.event(
                conn, "POSITION_REVIEW_REQUEST", {
                    "request_id": str(request_id), "context_hash": context.context_hash,
                    "context": context.data, "expires_at": context.expires_at,
                    "requested_at": now, "trigger": trigger,
                }, setup_id=setup_id, key=trigger["dedup_key"],
            )
        result = await self.reviewer.jev_review(
            request_id=request_id, identity=context.identity, state=context.state,
            question_set=managed_questions(context), expires_at=context.expires_at,
            purpose="ENGINEERING_TEST",
        )
        try:
            fresh = self.snapshot(setup_id, fresh_observation())
        except (ValueError, KeyError, TypeError):
            self.execution._event(
                "POSITION_REVIEW_OBSOLETE", {
                    "request_id": str(request_id), "receipt_ids": result.receipt_ids,
                    "reason": "POSITION_OR_PROTECTION_CHANGED",
                }, setup_id, key="position-obsolete:" + str(request_id),
            )
            return result
        self.execution.accept_management(setup_id, context, result, fresh)
        return result

    def recover_pending(self, setup_id, *, fresh_observation):
        """Resume the recorded response after a crash, never obtain a second vote."""
        with self.execution.repo.connect() as conn:
            row = conn.execute(
                """SELECT e.body FROM lab.managed_events e
                WHERE e.setup_id=%s AND e.kind='POSITION_REVIEW_REQUEST'
                AND NOT EXISTS(SELECT 1 FROM lab.managed_events done
                  WHERE done.setup_id=e.setup_id AND done.kind IN
                    ('MANAGED_JEV_JUDGMENT','POSITION_REVIEW_OBSOLETE')
                  AND done.body->>'request_id'=e.body->>'request_id')
                ORDER BY e.event_seq DESC LIMIT 1""",
                (setup_id,),
            ).fetchone()
        if not row:
            return None
        pending = row["body"]
        context = ManagedContext(encoded(pending["context"]), pending["context_hash"])
        result = self.recover_receipt(pending["request_id"], managed_questions(context))
        reason = None
        if self.now() >= datetime.fromisoformat(pending["expires_at"]):
            reason = "REVIEW_EXPIRED_DURING_RECOVERY"
        elif result is None:
            return None
        else:
            try:
                fresh = self.snapshot(setup_id, fresh_observation())
            except (ValueError, KeyError, TypeError):
                reason = "POSITION_OR_PROTECTION_CHANGED"
        if reason:
            self.execution._event(
                "POSITION_REVIEW_OBSOLETE",
                {
                    "request_id": pending["request_id"],
                    "reason": reason,
                    "receipt_ids": result.receipt_ids if result else (),
                },
                setup_id,
                key="position-obsolete:" + pending["request_id"],
            )
            return None
        self.execution.accept_management(setup_id, context, result, fresh)
        self.execution._event(
            "POSITION_REVIEW_RECOVERED",
            {"request_id": pending["request_id"], "receipt_ids": result.receipt_ids},
            setup_id,
            key="position-recovered:" + pending["request_id"],
        )
        return result

    def recover_receipt(self, request_id, questions):
        """Recovery reads the original bytes; it never asks for another favorable answer."""
        with self.reviewer.store.connect() as conn:
            receipt = conn.execute(
                """SELECT * FROM lab.jev_receipts WHERE request_id=%s
                ORDER BY attempt DESC LIMIT 1""",
                (request_id,),
            ).fetchone()
        if not receipt:
            return None
        self.reviewer.store.verify(receipt["receipt_id"])
        answers = (
            validated_answers(bytes(receipt["response_bytes"]), questions)
            if receipt["outcome"] == "VALID"
            else {}
        )
        return ReviewResult(
            str(request_id),
            "RECORDED" if answers else "NEEDS_REVIEW",
            receipt["error_code"],
            (str(receipt["receipt_id"]),),
            answers,
        )

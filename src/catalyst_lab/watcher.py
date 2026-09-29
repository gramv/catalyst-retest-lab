"""Transactional, append-only watch checkpoints. Broker mutations are unavailable."""

from dataclasses import asdict
from decimal import Decimal

from psycopg.types.json import Jsonb

from catalyst_lab.domain import Candidate
from catalyst_lab.market import Session, timestamp
from catalyst_lab.repository import json_safe
from catalyst_lab.trigger import TriggerPolicy, advance


class Watcher:
    def __init__(
        self, repository, policy=None, *, feed="iex", provider="ALPACA", reconciliation_gate=None
    ):
        self.repo = repository
        self.reconciliation_gate = reconciliation_gate or (lambda: False)
        self.policy = policy or TriggerPolicy()
        self.feed, self.provider = feed, provider

    def active(self):
        with self.repo.connect() as conn:
            return conn.execute("""
                SELECT c.candidate_id, c.ticker, c.session_date, s.state
                FROM lab.candidates c JOIN lab.candidate_states s USING(candidate_id)
                WHERE s.state IN ('VALIDATED', 'WATCHING')
            """).fetchall()

    def system_event(self, kind, payload):
        with self.repo.connect() as conn:
            self._system_event(conn, kind, payload)

    def _system_event(self, conn, kind, payload, candidate_id=None):
        event = self.repo.append_event(
            conn, "SYSTEM_EVENT", {"kind": kind, **payload}, candidate_id
        )
        conn.execute(
            """
            INSERT INTO lab.system_events(event_id, event_type, payload_json) VALUES (%s, %s, %s)
        """,
            (event["event_id"], kind, Jsonb(json_safe(payload))),
        )
        return event

    def tick(self, sessions, now, healthy_symbols, observation=None, *, recovering=False):
        if observation and (
            observation.data_feed != self.feed or observation.data_provider != self.provider
        ):
            raise ValueError("Observer provenance does not match its configured feed")
        # No subscription or fixture evidence alone can unlock the broker startup gate.
        if not self.reconciliation_gate():
            healthy_symbols, observation = set(), None
        changed = []
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            rows = conn.execute("""
                SELECT c.*, s.state, d.expires_at, d.evidence_json, w.payload_json AS watch
                FROM lab.candidates c JOIN lab.candidate_states s USING(candidate_id)
                JOIN lab.validation_decisions d ON d.candidate_id = c.candidate_id
                LEFT JOIN LATERAL (
                    SELECT payload_json FROM lab.watch_contexts
                    WHERE candidate_id = c.candidate_id ORDER BY event_seq DESC LIMIT 1
                ) w ON true
                WHERE s.state IN ('VALIDATED', 'WATCHING')
                ORDER BY c.candidate_id
            """).fetchall()
            for row in rows:
                if observation and observation.ticker != row["ticker"]:
                    continue
                candidate_id = row["candidate_id"]
                previous = row["watch"] or {"state": row["state"]}
                if previous.get("data_feed", self.feed) != self.feed:
                    raise ValueError("Cannot change the feed of an active watch")
                if previous.get("session"):
                    stored = previous["session"]
                    session = Session(
                        row["session_date"], timestamp(stored["opens"]), timestamp(stored["closes"])
                    )
                else:
                    session = sessions.get(row["session_date"])
                    if session is None and row["evidence_json"]:
                        evidence = row["evidence_json"]
                        session = Session(
                            row["session_date"],
                            timestamp(evidence["official_open"]),
                            timestamp(evidence["official_close"]),
                        )
                if session is None:
                    continue  # No invented holiday/session times.
                body = {
                    "strategy_version": row["strategy_version"],
                    **row["payload_json"],
                    "expires_at": row["expires_at"],
                }
                candidate = Candidate.model_validate(body)
                context = dict(previous)
                if recovering and row["state"] == "WATCHING":
                    context["failed_since"] = context.get("failed_since") or context["checked_at"]
                frozen_policy = previous.get("policy")
                policy = (
                    TriggerPolicy(
                        Decimal(frozen_policy["max_spread_bps"]),
                        Decimal(frozen_policy["feed_failure_tolerance_seconds"]),
                    )
                    if frozen_policy
                    else self.policy
                )
                obs = observation if observation and observation.ticker == row["ticker"] else None
                if (
                    obs
                    and conn.execute(
                        """
                    SELECT 1 FROM lab.market_snapshots
                    WHERE candidate_id = %s AND observation_key = %s
                """,
                        (candidate_id, obs.key),
                    ).fetchone()
                ):
                    obs = None
                context, reason = advance(
                    candidate, session, context, obs, now, row["ticker"] in healthy_symbols, policy
                )
                if context == previous and obs is None:
                    continue
                context.update(
                    session=json_safe(asdict(session)),
                    policy=json_safe(asdict(policy)),
                    data_feed=self.feed,
                    data_provider=self.provider,
                )
                event = self.repo.append_event(
                    conn,
                    "SYSTEM_EVENT",
                    {
                        "kind": "WATCH_EVALUATION",
                        "context": context,
                        "observation": obs.to_json() if obs else None,
                        "observation_key": obs.key if obs else None,
                        "reason": reason,
                        "recovering": recovering,
                    },
                    candidate_id,
                )
                conn.execute(
                    """
                    INSERT INTO lab.watch_contexts VALUES (%s, %s, %s)
                """,
                    (event["seq"], candidate_id, Jsonb(context)),
                )
                if obs:
                    price = obs.price if obs.price is not None else (obs.bid + obs.ask) / 2
                    conn.execute(
                        """
                        INSERT INTO lab.market_snapshots(candidate_id, market_data_timestamp,
                            price, bid, ask, spread_bps, volume, data_provider, data_feed,
                            observation_resolution, event_id, observation_key, observation_type,
                            provider_timestamp)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                        (
                            candidate_id,
                            obs.at,
                            price,
                            obs.bid,
                            obs.ask,
                            obs.spread_bps,
                            obs.volume,
                            obs.data_provider,
                            obs.data_feed,
                            "1_MINUTE" if obs.kind.startswith("bar") else "TICK",
                            event["event_id"],
                            obs.key,
                            obs.kind,
                            obs.provider_timestamp,
                        ),
                    )
                before, after = row["state"], context["state"]
                if before != after:
                    if before == "VALIDATED" and after not in {"WATCHING", "EXPIRED_UNTRIGGERED"}:
                        self.repo.transition(conn, candidate_id, before, "WATCHING")
                        before = "WATCHING"
                    self.repo.transition(
                        conn,
                        candidate_id,
                        before,
                        after,
                        {
                            "reason": reason,
                            "evaluated_at": now.isoformat(),
                            "entry_intent": context.get("entry_intent"),
                            "execution_enabled": False,
                        },
                    )
                    changed.append({"candidate_id": str(candidate_id), "state": after})
                    if reason == "DATA_FEED_FAILURE":
                        self._system_event(conn, reason, {"reason": reason}, candidate_id)
        return changed

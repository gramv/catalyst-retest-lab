"""Read broker-derived measurements; collect in-position market observations without orders."""

import threading
from datetime import UTC, datetime

from catalyst_lab.execution import system_event


class Measurements:
    def __init__(self, repository):
        self.repo = repository
        self.quotes = {}
        self.lock = threading.Lock()
        self.quote_seconds = {}

    def active(self, now):
        # Subscribe before a fill and retain a short tail for delayed bars/stream receipts.
        with self.repo.connect() as conn:
            return conn.execute(
                """SELECT c.candidate_id,c.ticker,c.session_date FROM lab.candidates c
                JOIN lab.candidate_states s USING(candidate_id)
                JOIN lab.trade_events e ON e.seq=s.last_event_seq
                WHERE s.state IN ('TRIGGER_CONFIRMED','RISK_CHECK','ORDER_SUBMITTED',
                  'PARTIALLY_FILLED','FILLED','OPEN')
                OR (s.state='CLOSED' AND e.created_at>=%s-interval '2 minutes')""",
                (now,),
            ).fetchall()

    def record_sessions(self, sessions):
        with self.repo.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            for session in sessions:
                old = conn.execute(
                    "SELECT * FROM lab.current_exchange_sessions WHERE session_date=%s",
                    (session.session_date,),
                ).fetchone()
                if old and old["opens_at"] == session.opens and old["closes_at"] == session.closes:
                    continue
                event = system_event(
                    self.repo,
                    conn,
                    "EXCHANGE_CALENDAR_OBSERVED",
                    {
                        "session_date": session.session_date,
                        "opens": session.opens,
                        "closes": session.closes,
                        "data_provider": "ALPACA",
                    },
                )
                conn.execute(
                    "INSERT INTO lab.exchange_sessions VALUES(%s,%s,%s,%s,'ALPACA')",
                    (event["seq"], session.session_date, session.opens, session.closes),
                )

    def observe(self, obs, now):
        if obs.at > now or (now - obs.at).total_seconds() > 180:
            return 0
        with self.lock:
            if obs.kind == "quote":
                old = self.quotes.get(obs.ticker)
                if old is None or old.at <= obs.at:
                    self.quotes[obs.ticker] = obs
                bucket = int(obs.at.timestamp())
                if self.quote_seconds.get(obs.ticker) == bucket:
                    return 0
                self.quote_seconds[obs.ticker] = bucket
            rows = [r for r in self.active(now) if r["ticker"] == obs.ticker]
            if not rows:
                return 0
            quote = self.quotes.get(obs.ticker)
            if quote and not 0 <= (obs.at - quote.at).total_seconds() <= 5:
                quote = None
            with self.repo.connect() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(719172026)")
                count = 0
                for row in rows:
                    if conn.execute(
                        "SELECT 1 FROM lab.market_snapshots "
                        "WHERE candidate_id=%s AND observation_key=%s",
                        (row["candidate_id"], obs.key),
                    ).fetchone():
                        continue
                    event = system_event(
                        self.repo,
                        conn,
                        "TRADE_MARKET_OBSERVATION",
                        {
                            "observation": obs.to_json(),
                            "observation_key": obs.key,
                            "quote": quote.to_json() if quote else None,
                        },
                        row["candidate_id"],
                    )
                    bid, ask = (quote.bid, quote.ask) if quote else (obs.bid, obs.ask)
                    price = obs.price if obs.price is not None else (obs.bid + obs.ask) / 2
                    conn.execute(
                        """INSERT INTO lab.market_snapshots(candidate_id,trade_id,
                        market_data_timestamp,price,bid,ask,spread_bps,volume,data_provider,data_feed,
                        observation_resolution,event_id,observation_key,observation_type,
                        provider_timestamp,high,low)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            row["candidate_id"],
                            row["candidate_id"],
                            obs.at,
                            price,
                            bid,
                            ask,
                            quote.spread_bps if quote else obs.spread_bps,
                            obs.volume,
                            obs.data_provider,
                            obs.data_feed,
                            "1_MINUTE"
                            if obs.kind.startswith("bar")
                            else "1_SECOND_QUOTE"
                            if obs.kind == "quote"
                            else "TICK",
                            event["event_id"],
                            obs.key,
                            obs.kind,
                            obs.provider_timestamp,
                            obs.high,
                            obs.low,
                        ),
                    )
                    count += 1
                return count

    def refresh(self):
        with self.repo.connect() as conn:
            return conn.execute("SELECT lab.refresh_measurements() AS seq").fetchone()["seq"]

    def rollup(self, session_date):
        with self.repo.connect() as conn:
            return conn.execute(
                "SELECT lab.rollup_session(%s) AS revision", (session_date,)
            ).fetchone()["revision"]


class MeasurementWorker:
    """One in-process projector; rollups run after calendar close and catch up after restart."""

    def __init__(self, measurements, clock=None):
        self.measurements = measurements
        self.now = clock or (lambda: datetime.now(UTC))
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None

    def tick(self):
        self.measurements.refresh()
        with self.measurements.repo.connect() as conn:
            dates = conn.execute(
                """SELECT DISTINCT c.session_date FROM lab.strategy_candidates c
                JOIN lab.current_exchange_sessions s USING(session_date) WHERE s.closes_at<=%s""",
                (self.now(),),
            ).fetchall()
        for row in dates:
            self.measurements.rollup(row["session_date"])

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.error = None
            except Exception:
                if self.error is None:
                    try:
                        with self.measurements.repo.connect() as conn:
                            system_event(
                                self.measurements.repo,
                                conn,
                                "MEASUREMENT_FAILURE",
                                {"reason": "PROJECTION_OR_ROLLUP_FAILED"},
                            )
                    except Exception:
                        # A database outage must not kill the worker while recording its failure.
                        pass
                self.error = "PROJECTION_OR_ROLLUP_FAILED"
            self.stop_event.wait(15)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)

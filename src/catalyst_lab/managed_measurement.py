"""Independent sampled market evidence and read-only fill-based measurements.

These observations are neither minute bars nor tick extrema. The recorder has no
broker or model transport and cannot alter trading decisions.
"""

import threading
from collections import OrderedDict
from datetime import UTC, datetime
from decimal import Decimal as D
from decimal import InvalidOperation

from catalyst_lab.managed_analytics import resolved_fill_costs
from catalyst_lab.managed_store import TERMINAL
from catalyst_lab.repository import json_safe

SAMPLING = "FIRST_VALID_OBSERVATION_PER_RECEIVED_SECOND"
# Sampling version 2 (plan 4.7): the same first valid observation per received second, but
# a row is written only when a measured input changed (price, position quantity or the
# entry fills before it) and at least every KEYFRAME_SECONDS as a full keyframe. Every
# skipped second repeats the previous row's values, so observed MFE/MAE are unchanged.
CHANGE_SAMPLING = "FIRST_VALID_OBSERVATION_PER_RECEIVED_SECOND_ON_CHANGE_V2"
KEYFRAME_SECONDS = 60
# Owner ruling R5 (package fees-net-r, plan phase 0): the official R denominator is the
# actually filled buy quantity times (admitted max entry minus admitted initial stop),
# not the reservation's authorized quantity (``test_r``, kept as a labelled engineering
# alternative) and never a later trailing-stop amendment.
OFFICIAL_R_METHOD = "MANAGED_OFFICIAL_R_PLANNED_FILLED_V1"
_EVALUATED = OrderedDict()  # (setup, lifecycle) -> last evaluated second; process-local.
_EVALUATED_LOCK = threading.Lock()
_EVALUATED_LIMIT = 10000


def _evaluated(memo, bucket):
    with _EVALUATED_LOCK:
        return _EVALUATED.get(memo) == bucket


def _remember(memo, bucket):
    with _EVALUATED_LOCK:
        _EVALUATED[memo] = bucket
        _EVALUATED.move_to_end(memo)
        while len(_EVALUATED) > _EVALUATED_LIMIT:
            _EVALUATED.popitem(last=False)


def _sample_reason(previous, lifecycle_id, price, qty, entry_fills, now):
    """Why this second needs a row under CHANGE_SAMPLING; ``None`` repeats the last row."""
    if (
        not isinstance(previous, dict)
        or previous.get("lifecycle_id") != lifecycle_id
        or previous.get("sampling_method") != CHANGE_SAMPLING
    ):
        return "FIRST_SAMPLE"
    try:
        if number(previous["price"]) != price:
            return "PRICE_CHANGED"
        if number(previous["position_qty"]) != qty:
            return "POSITION_CHANGED"
        if previous.get("entry_fill_count") != entry_fills:
            return "ENTRY_FILLS_CHANGED"
        if (now - stamp(previous["received_at"])).total_seconds() >= KEYFRAME_SECONDS:
            return "KEYFRAME"
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return "FIRST_SAMPLE"
    return None


def number(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("NONFINITE_MEASUREMENT")
    return result


def stamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("AWARE_MEASUREMENT_TIMESTAMP_REQUIRED")
    return result.astimezone(UTC)


def record_position_snapshot(
    store, setup, state, position, observation, *, received_at, sampling=CHANGE_SAMPLING
):
    """Record at most one fresh valid observation per setup/lifecycle/UTC second.

    ``sampling`` is the sampling version written into the row. ``CHANGE_SAMPLING`` (the
    default) evaluates the same first valid observation of each second but writes it
    only when the price, position quantity or preceding entry fills changed, or when
    the last row is ``KEYFRAME_SECONDS`` old; ``SAMPLING`` writes every such second.
    """
    if sampling not in {SAMPLING, CHANGE_SAMPLING}:
        raise ValueError("UNKNOWN_SAMPLING_METHOD")
    if not position or not observation or state.get("state") in TERMINAL:
        return False
    try:
        now = stamp(received_at)
        trade_at, quote_at = stamp(observation["trade_at"]), stamp(observation["quote_at"])
        price, bid, ask, qty = (
            number(v)
            for v in (
                observation["trade_price"],
                observation["bid"],
                observation["ask"],
                position["qty"],
            )
        )
        if (
            observation.get("feed_healthy") is not True
            or not 0 <= (now - trade_at).total_seconds() <= 5
            or not 0 <= (now - quote_at).total_seconds() <= 5
            or not 0 < bid <= ask
            or price <= 0
            or qty <= 0
            or not state.get("lifecycle_id")
            or any(
                not isinstance(observation.get(k), str) or not observation[k].strip()
                for k in ("data_provider", "data_feed")
            )
        ):
            return False
        volume = observation.get("volume")
        volume = number(volume) if volume is not None else None
        if volume is not None and volume < 0:
            return False
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False
    bucket = now.replace(microsecond=0).isoformat()
    key = f"market-snapshot:{setup['setup_id']}:{state['lifecycle_id']}:{bucket}"
    memo = (str(setup["setup_id"]), state["lifecycle_id"])
    if sampling == CHANGE_SAMPLING and _evaluated(memo, bucket):
        return False  # This second's first valid observation was already evaluated.

    def evaluate(conn):
        """``(evidence, reason)``; evidence is None when this is not a sample at all."""
        found = conn.execute(
            """SELECT (SELECT body FROM lab.managed_states WHERE setup_id=%(sid)s) AS state,
            EXISTS(SELECT 1 FROM lab.managed_events WHERE idempotency_key=%(key)s) AS written,
            f.entry_at,f.entry_fills,
            (SELECT body FROM lab.managed_events WHERE setup_id=%(sid)s
              AND kind='POSITION_MARKET_SNAPSHOT' ORDER BY event_seq DESC LIMIT 1) AS previous
            FROM (SELECT min(filled_at) FILTER(WHERE side='buy') AS entry_at,
                count(*) FILTER(WHERE side='buy' AND filled_at<=%(at)s) AS entry_fills
                FROM lab.managed_fills WHERE setup_id=%(sid)s) f""",
            {"sid": setup["setup_id"], "key": key, "at": trade_at},
        ).fetchone()
        latest = found["state"] or {}
        if (
            latest.get("state") in TERMINAL
            or latest.get("lifecycle_id") != state["lifecycle_id"]
            or found["written"]
            or not found["entry_at"]
            or trade_at < found["entry_at"]
        ):
            return None, None
        if sampling == SAMPLING:
            return found, None
        return found, _sample_reason(
            found["previous"], state["lifecycle_id"], price, qty, found["entry_fills"], now
        )

    if sampling == CHANGE_SAMPLING:
        # An unchanged second needs one read and never takes the ledger write lock.
        with store.repo.connect() as conn:
            found, reason = evaluate(conn)
        if found is None:
            return False
        if reason is None:
            _remember(memo, bucket)
            return False
    with store.transaction() as conn:
        found, reason = evaluate(conn)  # Re-checked under the lock before any write.
        if found is None:
            return False
        body = {
            "lifecycle_id": state["lifecycle_id"],
            "sampling_method": sampling,
            "sample_bucket": bucket,
            "price": price,
            "bid": bid,
            "ask": ask,
            "spread_bps": (ask - bid) / ((ask + bid) / 2) * 10000,
            "volume": volume,
            "position_qty": qty,
            "data_provider": observation["data_provider"],
            "data_feed": observation["data_feed"],
            "market_data_timestamp": trade_at,
            "quote_timestamp": quote_at,
            "received_at": now,
        }
        if sampling == CHANGE_SAMPLING:
            body.update(sample_reason=reason, entry_fill_count=found["entry_fills"],
                        keyframe_seconds=KEYFRAME_SECONDS)
        if sampling == SAMPLING or reason is not None:
            store.event(
                conn, "POSITION_MARKET_SNAPSHOT", body, setup_id=setup["setup_id"], key=key
            )
    if sampling == CHANGE_SAMPLING:
        _remember(memo, bucket)  # Only once the evaluation is durable.
    return sampling == SAMPLING or reason is not None


def managed_measurement(repository, setup_id, *, as_of=None):
    """Return fill cash flows and observed excursions; unknown inputs stay null."""
    with repository.connect() as conn:
        row = conn.execute(
            "SELECT body FROM lab.managed_states WHERE setup_id=%s", (setup_id,)
        ).fetchone()
        state = row["body"] if row else {}
        setup = conn.execute(
            "SELECT record_json FROM lab.managed_setups WHERE setup_id=%s", (setup_id,)
        ).fetchone()
        fills = conn.execute(
            "SELECT * FROM lab.managed_fills WHERE setup_id=%s ORDER BY filled_at,event_seq",
            (setup_id,),
        ).fetchall()
        costs = resolved_fill_costs(conn, fills)
        reservation = conn.execute(
            "SELECT planned_risk FROM lab.managed_reservations WHERE setup_id=%s", (setup_id,)
        ).fetchone()
        snapshots = conn.execute(
            "SELECT body FROM lab.managed_events WHERE setup_id=%s "
            "AND kind='POSITION_MARKET_SNAPSHOT' ORDER BY event_seq",
            (setup_id,),
        ).fetchall()
    buys, sells = ([f for f in fills if f["side"] == side] for side in ("buy", "sell"))
    bought, sold = (sum((f["qty"] for f in rows), D(0)) for rows in (buys, sells))
    cost, proceeds = (sum((f["qty"] * f["price"] for f in rows), D(0)) for rows in (buys, sells))
    entry = cost / bought if bought else None
    exit_price = proceeds / sold if sold else None
    planned = reservation["planned_risk"] if reservation else None
    # Owner ruling R5 (package fees-net-r): official R is PLANNED_FILLED — the actually
    # filled buy quantity times (admitted max entry minus admitted initial stop), read
    # from the immutable admission record, never the reservation's authorized quantity
    # and never any later trailing-stop amendment recorded only in the setup's state.
    try:
        levels = setup["record_json"]["levels"] if setup else {}
        m, s = D(str(levels["max_entry_price"])), D(str(levels["stop"]))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        m = s = None
    planned_filled = bought * (m - s) if bought and m is not None and s is not None else None
    closed = state.get("state") == "CLOSED"
    gross = proceeds - cost if closed and buys and sells else None
    fees_known = bool(fills) and all(costs[f["fill_id"]]["fee_usd"] is not None for f in fills)
    base_fee_qty = sum((cost["base_asset_fee_qty"] for cost in costs.values()), D(0))
    inventory_reconciled = bool(buys) and bought == sold + base_fee_qty
    cash_fees = sum((cost["cash_fee_usd"] for cost in costs.values()), D(0)) if fees_known else None
    economic_fees = sum((cost["fee_usd"] for cost in costs.values()), D(0)) if fees_known else None
    # Lost base-asset inventory already reduces cash proceeds relative to purchase
    # cost. Its verified USD economic value must not be subtracted a second time.
    net = gross - cash_fees if gross is not None and fees_known and inventory_reconciled else None
    net_r = net / planned if net is not None and planned and planned > 0 else None
    official_r = (
        net / planned_filled if net is not None and planned_filled and planned_filled > 0 else None
    )
    first_fill = buys[0]["filled_at"] if buys else None
    end = sells[-1]["filled_at"] if closed and sells else stamp(as_of or datetime.now(UTC))
    samples, moves, provenance = [], [], set()
    for event in snapshots:
        s = event["body"]
        try:
            at = stamp(s["market_data_timestamp"])
            if (
                not first_fill
                or not first_fill <= at <= end
                or s.get("lifecycle_id") != state.get("lifecycle_id")
            ):
                continue
            preceding = [f for f in buys if f["filled_at"] <= at]
            qty = sum((f["qty"] for f in preceding), D(0))
            if qty <= 0:
                continue
            actual_average = sum(f["qty"] * f["price"] for f in preceding) / qty
            moves.append((number(s["price"]) - actual_average) * number(s["position_qty"]))
            samples.append(at)
            provenance.add((s["data_provider"], s["data_feed"]))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
    points = sorted([first_fill, *samples, end]) if first_fill and samples else []
    gap = (
        max((b - a).total_seconds() for a, b in zip(points, points[1:], strict=False))
        if points
        else None
    )
    mfe, mae = (max(D(0), max(moves)), min(D(0), min(moves))) if moves else (None, None)
    methods = sorted({m for m in (e["body"].get("sampling_method") for e in snapshots)
                      if isinstance(m, str)}) or [SAMPLING]
    limitations = [
        "One first valid sample per received second; not bar or tick extrema.",
        "Excursions use the entry fill average known at the market timestamp "
        "and observed broker quantity.",
        "Missing intervals are not interpolated; extrema outside observations are unknown.",
    ]
    if CHANGE_SAMPLING in methods:
        limitations.append("Change-only samples: an unchanged second is not written (a keyframe at "
                           "least every 60 s), so sample counts and gaps are not per-second.")
    if any("iex" in feed.lower() for _, feed in provenance):
        limitations.append("IEX observations are not a consolidated US market feed.")
    if not fees_known or not inventory_reconciled:
        limitations.append(
            "Fees or base-asset inventory adjustments are unverified; net P&L is unavailable."
        )
    if base_fee_qty:
        limitations.append(
            "Fill cash-flow P&L includes lost base-asset inventory; net subtracts only cash fees."
        )
    if any(cost["invalid_correction"] for cost in costs.values()):
        limitations.append("Invalid bound cost evidence keeps affected fees and net P&L unknown.")
    if bought and planned_filled is None:
        limitations.append(
            "Official R is unavailable without the setup's admitted max entry and stop."
        )
    return json_safe(
        {
            "sampling_method": ",".join(methods),
            "sample_count": len(samples),
            "first_sample_at": min(samples) if samples else None,
            "last_sample_at": max(samples) if samples else None,
            "max_observation_gap_seconds": gap,
            "entry_fill_average": entry,
            "exit_fill_average": exit_price,
            "bought_qty": bought,
            "sold_qty": sold,
            "initial_planned_risk": planned,
            "planned_filled_risk": planned_filled,
            "gross_realized_pnl": gross,
            "gross_realized_pnl_scope": "COMPLETE_CLOSED_LIFECYCLE_CASH_FLOWS",
            "test_r": gross / planned if gross is not None and planned and planned > 0 else None,
            "test_r_label": "ENGINEERING_ALTERNATIVE_AUTHORIZED_QUANTITY_DENOMINATOR",
            "net_pnl": net,
            "net_r": net_r,
            "official_r": official_r,
            "official_r_method": OFFICIAL_R_METHOD,
            "fees_verified": fees_known and inventory_reconciled,
            "inventory_reconciled_with_costs": inventory_reconciled,
            "verified_total_economic_fee_usd": economic_fees,
            "verified_cash_fee_usd": cash_fees,
            "verified_base_asset_fee_qty": base_fee_qty
            if fees_known and inventory_reconciled else None,
            "cost_correction_ids": [cost["correction_id"] for cost in costs.values()
                                    if cost["correction_id"]],
            "cost_evidence": [
                {"fill_id": fill_id, "source": cost["cost_source"],
                 "correction_id": cost["correction_id"],
                 "invalid_correction": cost["invalid_correction"]}
                for fill_id, cost in costs.items()
            ],
            "observed_mfe_pnl": mfe,
            "observed_mae_pnl": mae,
            "observed_mfe_r": mfe / planned
            if mfe is not None and planned and planned > 0
            else None,
            "observed_mae_r": mae / planned
            if mae is not None and planned and planned > 0
            else None,
            "provenance": [
                {"data_provider": provider, "data_feed": feed}
                for provider, feed in sorted(provenance)
            ],
            "limitations": limitations,
        }
    )


EXCURSION_AGGREGATE = "SAMPLED_EXCURSION_SQL_AGGREGATE_V1"
_NUMBER_TEXT = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$"
_STAMP_TEXT = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
    r"([+-][0-9]{2}:[0-9]{2}|Z)$"
)
_EXCURSION_SQL = """
WITH averages AS (
  SELECT start_at, avg_price
  FROM unnest(%(starts)s::timestamptz[], %(averages)s::numeric[]) AS a(start_at, avg_price)
), samples AS (
  SELECT e.event_seq, (e.body->>'market_data_timestamp')::timestamptz AS at,
         (e.body->>'price')::numeric AS price, (e.body->>'position_qty')::numeric AS qty,
         e.body->>'data_provider' AS provider, e.body->>'data_feed' AS feed,
         e.body->>'sampling_method' AS method
  FROM lab.managed_events e
  WHERE e.setup_id=%(setup)s AND e.kind='POSITION_MARKET_SNAPSHOT'
    AND e.body->>'lifecycle_id'=%(lifecycle)s
    AND e.body->>'market_data_timestamp' ~ %(stamp)s
    AND e.body->>'price' ~ %(number)s AND e.body->>'position_qty' ~ %(number)s
    AND jsonb_typeof(e.body->'data_provider')='string'
    AND jsonb_typeof(e.body->'data_feed')='string'
), moves AS MATERIALIZED (
  SELECT s.*, a.avg_price, (s.price - a.avg_price) * s.qty AS move,
         s.at - lag(s.at) OVER (ORDER BY s.at, s.event_seq) AS gap
  FROM samples s CROSS JOIN LATERAL (
    SELECT avg_price FROM averages WHERE start_at <= s.at ORDER BY start_at DESC LIMIT 1
  ) a
  WHERE s.at <= %(end)s
)
SELECT n.*, hi.price AS hi_price, hi.qty AS hi_qty, hi.avg_price AS hi_avg,
       lo.price AS lo_price, lo.qty AS lo_qty, lo.avg_price AS lo_avg
FROM (SELECT count(*) AS sample_count, min(at) AS first_at, max(at) AS last_at,
             max(gap) AS max_gap, min(event_seq) AS first_seq, max(event_seq) AS last_seq,
             coalesce(jsonb_agg(DISTINCT jsonb_build_array(provider, feed)), '[]'::jsonb)
               AS provenance,
             coalesce(jsonb_agg(DISTINCT method) FILTER (WHERE method IS NOT NULL),
                      '[]'::jsonb) AS methods
      FROM moves) n
LEFT JOIN LATERAL (SELECT * FROM moves ORDER BY move DESC, event_seq LIMIT 1) hi ON true
LEFT JOIN LATERAL (SELECT * FROM moves ORDER BY move ASC, event_seq LIMIT 1) lo ON true
"""


def sampled_excursions(repository, setup_id, lifecycle_id, *, as_of):
    """Observed excursions of an open lifecycle from one SQL aggregate per review.

    Same samples, filters and arithmetic as ``managed_measurement`` for an open
    position (the entry-fill average known at each market timestamp, the observed
    broker quantity, ``as_of`` as the end), but the per-second samples are aggregated
    inside PostgreSQL instead of being loaded and parsed on every review. Only the two
    extreme samples come back; their moves are recomputed here in Decimal exactly as
    ``managed_measurement`` computes them.
    """
    end = stamp(as_of)
    with repository.connect() as conn:
        buys = conn.execute(
            """SELECT filled_at,qty,price FROM lab.managed_fills
            WHERE setup_id=%s AND side='buy' ORDER BY filled_at,event_seq""",
            (setup_id,),
        ).fetchall()
        starts, averages, qty, cost = [], [], D(0), D(0)
        for fill in buys:  # Average of every buy filled at or before each boundary.
            qty += fill["qty"]
            cost += fill["qty"] * fill["price"]
            if starts and starts[-1] == fill["filled_at"]:
                averages[-1] = cost / qty
            else:
                starts.append(fill["filled_at"])
                averages.append(cost / qty)
        row = conn.execute(_EXCURSION_SQL, {
            "starts": starts, "averages": averages, "setup": setup_id,
            "lifecycle": str(lifecycle_id), "stamp": _STAMP_TEXT, "number": _NUMBER_TEXT,
            "end": end,
        }).fetchone() if buys else None
    count = row["sample_count"] if row else 0
    # The sampling versions actually present (managed_measurement reports the same label).
    methods = sorted(row["methods"]) if count and row["methods"] else [SAMPLING]
    gap = mfe = mae = None
    if count:
        gaps = (row["first_at"] - starts[0], row["max_gap"], end - row["last_at"])
        gap = max(g for g in gaps if g is not None).total_seconds()
        mfe = max(D(0), (row["hi_price"] - row["hi_avg"]) * row["hi_qty"])
        mae = min(D(0), (row["lo_price"] - row["lo_avg"]) * row["lo_qty"])
    return json_safe({
        "aggregate_version": EXCURSION_AGGREGATE,
        "sampling_method": ",".join(methods),
        "sample_count": count,
        "first_sample_at": row["first_at"] if count else None,
        "last_sample_at": row["last_at"] if count else None,
        "max_observation_gap_seconds": gap,
        "observed_mfe_pnl": mfe,
        "observed_mae_pnl": mae,
        "provenance": [
            {"data_provider": provider, "data_feed": feed}
            for provider, feed in sorted(map(tuple, row["provenance"]))
        ] if count else [],
        "first_event_seq": row["first_seq"] if count else None,
        "last_event_seq": row["last_seq"] if count else None,
    })

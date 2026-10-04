"""Read-only engineering analytics and operator-imported append-only cost evidence.

The importer has no broker transport and no Muse route. A source reference attests
what the operator reconciled; it is not a claim this module contacted the broker.
"""

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, InvalidOperation
from decimal import Decimal as D
from uuid import UUID, uuid5

from catalyst_lab.agent_identity import agent_fields
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.jev_review import _privacy_check
from catalyst_lab.managed_engineering import ENGINEERING_PURPOSE, is_engineering
from catalyst_lab.managed_store import COHORT
from catalyst_lab.market import NY, MarketDataError, timestamp
from catalyst_lab.repository import json_safe

COST_VERSION = "MANAGED_BROKER_COST_CORRECTION_V1"
COST_KIND = "FILL_COST_CORRECTION"
ANALYTICS_VERSION = "MANAGED_ENGINEERING_ANALYTICS_V1"
# Package fees-net-r (plan phase 0): fee evidence read from Alpaca's own account
# activities, matched to a fill and appended through the same append-only correction
# path as an operator's manual import. The shape it assumed (an order id and a transaction
# time on every row) was wrong for the live paper account's crypto rows, read on 2026-09-29;
# those bind by ALPACA_FEE_MATCH_V2 below (see ``normalize_fee_activity`` and
# docs/packages/fees-net-r.md).
FEE_ACTIVITY_SOURCE = "ALPACA_PAPER_ACTIVITY"
SOURCES = {"BROKER_ACTIVITY", "BROKER_STATEMENT", "LAB_FIXTURE", FEE_ACTIVITY_SOURCE}
LABEL = "PAPER_ENGINEERING_OBSERVATIONS_NOT_VALIDATED_STRATEGY_PERFORMANCE"
# Operator ENGINEERING_TEST setups (plan 0.10) are flagged on every payload and never enter a
# performance aggregate; account risk and reconciliation still include them.
ENGINEERING_SCOPE = "ENGINEERING_TEST_SETUPS_EXCLUDED_FROM_AGGREGATES"
# A closed setup whose net/official R rests on any LAB_FIXTURE-sourced cost correction is
# never counted as verified real-account performance in an aggregate (fees-net-r); it is
# still visible, flagged in ``fixture_tainted_count``. Fixture sources remain fine for a
# disposable test database's own per-fill fields (``managed_measurement``, ``/results``).
FIXTURE_NOT_VERIFIED_SCOPE = "LAB_FIXTURE_COST_EVIDENCE_EXCLUDED_FROM_VERIFIED_AGGREGATES"
# Fixed, arbitrary namespace for a deterministic correction id per (broker activity, fill):
# the same activity re-imported for the same fill always yields the same idempotency key.
FEE_CORRECTION_NAMESPACE = UUID("c9a6f6f0-2b34-4c1a-9b7a-1a9c2f5e6d7b")
# A fee activity's transaction time may lag the fill it belongs to by a few seconds; a gap
# wider than this is not a match (fees-net-r matches "by order ID and time").
FEE_MATCH_TOLERANCE_SECONDS = 5
# ALPACA_FEE_MATCH_V2 (2026-09-29, docs/CONTRACT-RESOLUTIONS.md). The live paper account's
# crypto CFEE rows carry no order id and no transaction time, so a row without an order id
# binds by amount: to the one fill without fee evidence of its side (a coin fee: a buy of
# that coin; a USD fee: any crypto sell), filled at most 24 h before the row's posting time
# (``created_at``, FEE_MATCH_TOLERANCE_SECONDS of clock tolerance after it), whose fee at an
# Alpaca tier-1 crypto rate rounds up to the posted amount. The fees-net-r rule (order id and
# closest time, ``match_fee_activity``) is named ALPACA_FEE_MATCH_V1 and still binds every
# row that carries an order id.
FEE_MATCH_V1 = "ALPACA_FEE_MATCH_V1"
FEE_MATCH_V2 = "ALPACA_FEE_MATCH_V2"
# ALPACA_FEE_MATCH_V3 (2026-10-02, docs/CONTRACT-RESOLUTIONS.md). For a coin whose price has
# more decimals than the activity's ``price`` field carries (PEPE, SHIB and BONK on the live
# paper account, 2026-09-30 to 10-01), Alpaca posts the coin-fee row with ``price`` "0". Such a
# row is a coin fee with no posted price: it binds exactly as V2 (its coin, a buy, the window
# and the rate-rounded quantity) and its USD value is the posted quantity at the price of the
# fill it binds to, the fill the coins were charged from. A negative price, or a zero price
# with a cash debit, is still invalid.
FEE_MATCH_V3 = "ALPACA_FEE_MATCH_V3"
# ALPACA_FEE_MATCH_V4 (2026-10-02, package learning-measure, docs/CONTRACT-RESOLUTIONS.md). V2
# and V3 leave a row ``ambiguous`` when it has two or more candidate fills. When a group of
# ambiguous rows and their candidate fills is closed (no candidate is shared with a row outside
# the group), the counts match, a complete assignment exists, and every complete assignment
# gives the same fee to each trade (the fills are of one setup and side, or the rows carry the
# same amounts), the group binds in time order (``assign_fee_activities_v4``). Anything else
# stays ambiguous: V4 never chooses between outcomes that would differ.
FEE_MATCH_V4 = "ALPACA_FEE_MATCH_V4"
FEE_MATCH_V2_RATES = (D("0.0015"), D("0.0025"))  # Tier-1 crypto maker and taker.
FEE_MATCH_V2_COIN_QUANTUM = D("0.000000001")  # A coin fee: rate x fill qty, rounded up.
FEE_MATCH_V2_USD_QUANTUM = D("0.01")  # A USD fee: rate x fill notional, rounded up.
FEE_MATCH_V2_MAX_LAG = timedelta(hours=24)
# The fee read reaches back to the earliest fill still without fee evidence, at most this far
# (Alpaca filters activities by date, and posts some fees hours after their fill).
FEE_PENDING_MAX_AGE = timedelta(days=7)


def _stamp(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("AWARE_COST_TIMESTAMP_REQUIRED")
    return parsed.astimezone(UTC)


def _amount(value):
    if isinstance(value, (float, bool)) or value is None:
        raise ValueError("EXACT_NONNEGATIVE_COST_REQUIRED")
    try:
        number = D(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
    except (ValueError, ArithmeticError):
        raise ValueError("EXACT_NONNEGATIVE_COST_REQUIRED") from None
    return number


def _fill_binding(fill):
    return digest(encoded(json_safe({k: fill[k] for k in (
        "fill_id", "setup_id", "broker_order_id", "side", "qty", "price",
        "filled_at", "fee_usd", "source", "event_seq",
    )})))


def _validate_costs(fee, base_qty, base_value):
    fee, base_qty, base_value = (_amount(v) for v in (fee, base_qty, base_value))
    if base_value > fee or (base_qty > 0) != (base_value > 0):
        raise ValueError("BASE_ASSET_FEE_VALUE_BINDING_REQUIRED")
    return fee, base_qty, base_value


def import_fill_cost_correction(store, raw, *, recorded_at):
    """Append an exact-fill broker reconciliation; never alter fills or inventory.

    ``fee_usd`` is the verified total economic fee. A fee charged in base assets
    additionally needs its explicit USD component; only the remaining cash fee is
    subtracted from cash-flow P&L. Missing conversion rates are never invented.
    """
    required = {
        "correction_id", "setup_id", "fill_id", "fill_event_seq", "fee_usd", "source",
        "broker_source_reference", "observed_at",
    }
    optional = {"base_asset_fee_qty", "base_asset_fee_usd", "supersedes_correction_id"}
    if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - optional:
        raise ValueError("EXACT_FILL_COST_CORRECTION_REQUIRED")
    try:
        correction_id, setup_id = (str(UUID(str(raw[k]))) for k in ("correction_id", "setup_id"))
        supersedes = raw.get("supersedes_correction_id")
        supersedes = str(UUID(str(supersedes))) if supersedes is not None else None
        recorded, observed = _stamp(recorded_at), _stamp(raw["observed_at"])
        fee, base_qty, base_value = _validate_costs(
            raw["fee_usd"], raw.get("base_asset_fee_qty", "0"), raw.get("base_asset_fee_usd", "0")
        )
    except (TypeError, ValueError, AttributeError):
        raise ValueError("INVALID_FILL_COST_CORRECTION") from None
    if (
        type(raw["fill_event_seq"]) is not int or raw["fill_event_seq"] < 1
        or not isinstance(raw["fill_id"], str) or not 1 <= len(raw["fill_id"]) <= 200
        or not isinstance(raw["source"], str) or raw["source"] not in SOURCES
        or not isinstance(raw["broker_source_reference"], str)
        or not 1 <= len(raw["broker_source_reference"].strip()) <= 300
        or observed > recorded
    ):
        raise ValueError("INVALID_FILL_COST_CORRECTION")
    body = json_safe({
        "policy_version": COST_VERSION, "correction_id": correction_id, "setup_id": setup_id,
        "fill_id": raw["fill_id"], "fill_event_seq": raw["fill_event_seq"],
        "fee_usd": fee, "base_asset_fee_qty": base_qty, "base_asset_fee_usd": base_value,
        "source": raw["source"], "broker_source_reference": raw["broker_source_reference"],
        "observed_at": observed, "supersedes_correction_id": supersedes,
    })
    _privacy_check(encoded(body))
    request_hash = digest(encoded(body))
    key = "fill-cost-correction:" + correction_id

    def replay(existing):
        if existing["body"].get("request_hash") != request_hash:
            raise ValueError("COST_CORRECTION_IDEMPOTENCY_MISMATCH")
        return {"status": "COST_EVIDENCE_RECORDED", "event_seq": existing["event_seq"],
                "correction_id": correction_id, "idempotent_replay": True}

    # A recorded event never changes, so a replay is answered without the shared managed
    # lock (the periodic fee import replays every recorded fee it reads again); only a
    # first write takes the lock, and checks again under it.
    with store.repo.connect() as conn:
        existing = conn.execute(
            "SELECT event_seq,body FROM lab.managed_events WHERE idempotency_key=%s", (key,),
        ).fetchone()
    if existing:
        return replay(existing)
    with store.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM lab.managed_events WHERE idempotency_key=%s", (key,),
        ).fetchone()
        if existing:
            return replay(existing)
        fill = conn.execute(
            """SELECT f.*,s.market,s.cohort FROM lab.managed_fills f
            JOIN lab.managed_setups s USING(setup_id) WHERE f.fill_id=%s""", (raw["fill_id"],),
        ).fetchone()
        if (not fill or str(fill["setup_id"]) != setup_id
                or fill["event_seq"] != raw["fill_event_seq"] or fill["cohort"] != COHORT
                or observed < fill["filled_at"] or base_qty > fill["qty"]
                or (base_qty > 0 and fill["market"] != "CRYPTO")):
            raise ValueError("FILL_COST_BINDING_MISMATCH")
        prior = conn.execute(
            """SELECT body FROM lab.managed_events WHERE setup_id=%s AND kind=%s
            AND body->>'fill_id'=%s ORDER BY event_seq DESC LIMIT 1""",
            (setup_id, COST_KIND, raw["fill_id"]),
        ).fetchone()
        expected = prior["body"]["correction_id"] if prior else None
        if supersedes != expected:
            raise ValueError("LATEST_COST_CORRECTION_BINDING_REQUIRED")
        body.update({"request_hash": request_hash, "recorded_at": recorded.isoformat(),
                     "fill_binding_hash": _fill_binding(fill)})
        body["evidence_hash"] = digest(encoded(body))
        event = store.event(conn, COST_KIND, body, setup_id=setup_id, key=key)
    return {"status": "COST_EVIDENCE_RECORDED", "event_seq": event["event_seq"],
            "correction_id": correction_id, "idempotent_replay": False}


def resolved_fill_costs(conn, fills):
    """Read validated correction chains; any malformed chain keeps that fill unknown."""
    results = {}
    for fill in fills:
        try:
            native = _amount(fill["fee_usd"])
        except ValueError:
            native = None
        results[fill["fill_id"]] = {
            "fee_usd": native, "cash_fee_usd": native, "base_asset_fee_qty": D(0),
            "base_asset_fee_usd": D(0), "correction_id": None,
            "cost_source": "FILL_EVENT" if native is not None else "UNKNOWN",
            "invalid_correction": False,
        }
    if not fills:
        return results
    rows = conn.execute(
        """SELECT body,event_seq,setup_id FROM lab.managed_events WHERE kind=%s
        AND setup_id=ANY(%s) ORDER BY event_seq""",
        (COST_KIND, list({fill["setup_id"] for fill in fills})),
    ).fetchall()
    indexed = {fill["fill_id"]: fill for fill in fills}
    for event in rows:
        body = event["body"]
        fill = indexed.get(body.get("fill_id"))
        if fill is None:
            continue
        cost = results[fill["fill_id"]]
        try:
            fee, base_qty, base_value = _validate_costs(
                body["fee_usd"], body["base_asset_fee_qty"], body["base_asset_fee_usd"]
            )
            observed, recorded = _stamp(body["observed_at"]), _stamp(body["recorded_at"])
            if (cost["invalid_correction"] or body["policy_version"] != COST_VERSION
                    or str(event["setup_id"]) != str(fill["setup_id"])
                    or body["setup_id"] != str(fill["setup_id"])
                    or body["fill_event_seq"] != fill["event_seq"]
                    or body["fill_binding_hash"] != _fill_binding(fill)
                    or body["supersedes_correction_id"] != cost["correction_id"]
                    or body["source"] not in SOURCES or not body["broker_source_reference"]
                    or not fill["filled_at"] <= observed <= recorded
                    or base_qty > fill["qty"]
                    or body["evidence_hash"] != digest(encoded({
                        k: v for k, v in body.items() if k != "evidence_hash"
                    }))):
                raise ValueError("INVALID_BOUND_COST_CORRECTION")
        except (ValueError, KeyError, TypeError, ArithmeticError):
            cost.update(fee_usd=None, cash_fee_usd=None, invalid_correction=True,
                        cost_source="INVALID_CORRECTION")
            continue
        cost.update(fee_usd=fee, cash_fee_usd=fee - base_value, base_asset_fee_qty=base_qty,
                    base_asset_fee_usd=base_value, correction_id=body["correction_id"],
                    cost_source=body["source"], correction_event_seq=event["event_seq"])
    return results


def normalize_fee_activity(raw):
    """Validate one Alpaca ``CFEE``/``FEE`` account activity; never raises for content.

    Package fees-net-r (plan phase 0). Assumed shape, pending an owner-run read of the
    live account's activities to confirm it (docs/packages/fees-net-r.md): ``order_id``
    and ``transaction_time`` bind the activity to a fill of that order; ``net_amount``
    is the signed USD ledger impact of the fee (``"0"`` when the fee was taken entirely
    in the base asset received, per Alpaca's crypto-fees documentation); an in-kind fee
    additionally carries a positive ``qty`` (the base-asset quantity charged) and the
    ``price`` used to value it in USD. ``net_amount`` may be reported either signed
    (a debit) or unsigned; both are read as a magnitude. A row that explicitly reports
    zero (both fields absent or ``"0"``) is accepted as confirmed evidence of no fee,
    the same as any other amount — it is still one read account activity, not a guess.

    That is ALPACA_FEE_MATCH_V1, kept for every row that carries an ``order_id``. A row
    without one is read under ALPACA_FEE_MATCH_V2 (``_normalize_fee_activity_v2``): the
    two crypto ``CFEE`` shapes the live paper account reported on 2026-09-29. The result
    names its rule in ``match_policy``.
    """
    try:
        if not isinstance(raw, dict) or raw.get("activity_type") not in {"CFEE", "FEE"}:
            raise ValueError
        if raw.get("order_id") is None:
            return _normalize_fee_activity_v2(raw)
        order_id, activity_id = raw.get("order_id"), raw.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError
        if not isinstance(activity_id, str) or not 1 <= len(activity_id) <= 160:
            raise ValueError
        cash = D(str(raw.get("net_amount", "0")))
        qty = D(str(raw.get("qty", "0")))
        if not cash.is_finite() or not qty.is_finite() or qty < 0:
            raise ValueError
        cash = abs(cash)
        base_value = D(0)
        if qty > 0:
            price = D(str(raw["price"]))
            if not price.is_finite() or price <= 0:
                raise ValueError
            base_value = qty * price
        return {
            "activity_id": activity_id,
            "match_policy": FEE_MATCH_V1,
            "order_id": order_id,
            "at": timestamp(raw["transaction_time"]),
            "cash_fee_usd": cash,
            "base_asset_fee_qty": qty,
            "base_asset_fee_usd": base_value,
        }
    except (
        KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation, AttributeError,
        MarketDataError,
    ):
        raise ValueError("INVALID_FEE_ACTIVITY") from None


def _exact_number(value):
    if isinstance(value, (float, bool)) or value is None:
        raise ValueError
    number = D(str(value))
    if not number.is_finite():
        raise ValueError
    return number


def _normalize_fee_activity_v2(raw):
    """One of the two verified crypto ``CFEE`` shapes (ALPACA_FEE_MATCH_V2); else raises.

    Both are ``activity_type`` ``CFEE``, ``status`` ``executed``, ``currency`` ``USD``,
    with an ``id`` and ``created_at`` (the posting time) and no order id:

    - ``"Coin Pair Transaction Fee (Non USD)"``, a buy's fee taken in the coin: ``symbol``
      (``UNIUSD`` for the ledger's ``UNI/USD``), a negative ``qty`` (the coin charged), the
      ``price`` in USD per coin, and ``net_amount`` ``"0"``. Its USD value is |qty| x price.
    - ``"Coin Pair Transaction Fee (USD)"``, a sell's fee in USD: a negative ``net_amount``
      and no symbol, qty or price.

    A coin-fee row posted with ``price`` "0" is read under ALPACA_FEE_MATCH_V3: the same
    binding, valued at the price of the fill it binds to (``_fee_correction``). Any other
    combination is not guessed at: the row is invalid.
    """
    activity_id = raw.get("id")
    if (raw.get("activity_type") != "CFEE" or raw.get("status") != "executed"
            or raw.get("currency") != "USD"
            or not isinstance(activity_id, str) or not 1 <= len(activity_id) <= 160):
        raise ValueError
    cash = _exact_number(raw["net_amount"])
    common = {"activity_id": activity_id, "match_policy": FEE_MATCH_V2,
              "at": timestamp(raw["created_at"])}
    if all(raw.get(k) is None for k in ("symbol", "qty", "price")):
        if cash >= 0:
            raise ValueError
        return {**common, "fee_kind": "USD", "symbol": None, "cash_fee_usd": -cash,
                "base_asset_fee_qty": D(0), "base_asset_fee_usd": D(0)}
    symbol, qty, price = raw["symbol"], _exact_number(raw["qty"]), _exact_number(raw["price"])
    if not isinstance(symbol, str) or not symbol or cash != 0 or qty >= 0 or price < 0:
        raise ValueError
    coin = {**common, "fee_kind": "COIN", "symbol": symbol.replace("/", ""),
            "cash_fee_usd": D(0), "base_asset_fee_qty": -qty}
    if price == 0:
        # ALPACA_FEE_MATCH_V3: no posted price; valued at the fill it binds to.
        return {**coin, "match_policy": FEE_MATCH_V3, "base_asset_fee_usd": None}
    return {**coin, "base_asset_fee_usd": -qty * price}


def match_fee_activity(fills, activity):
    """The one managed fill this activity binds to, by broker order id and closest time.

    ``None`` when the order has no recorded fill, or none within
    ``FEE_MATCH_TOLERANCE_SECONDS`` of the activity's transaction time. Ties (fills of
    the same order equally close in time) resolve to the earliest fill, deterministically.
    """
    candidates = [f for f in fills if f["broker_order_id"] == activity["order_id"]]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda f: (abs((f["filled_at"] - activity["at"]).total_seconds()), f["filled_at"]),
    )
    gap = abs((best["filled_at"] - activity["at"]).total_seconds())
    return best if gap <= FEE_MATCH_TOLERANCE_SECONDS else None


def fee_correction_id(activity_id, fill_id):
    """The deterministic correction id of one (broker activity, fill) binding."""
    return str(uuid5(FEE_CORRECTION_NAMESPACE, f"alpaca-fee:{activity_id}:{fill_id}"))


def _v2_in_time(activity, fill):
    lag = activity["at"] - fill["filled_at"]
    return -timedelta(seconds=FEE_MATCH_TOLERANCE_SECONDS) <= lag <= FEE_MATCH_V2_MAX_LAG


def _v2_consistent(activity, fill):
    """Whether this fill could have produced this V2 fee: market, side, coin, time, amount.

    Fee evidence is not considered here (``assign_fee_activities_v2`` does that).
    """
    if fill.get("market") != "CRYPTO" or not _v2_in_time(activity, fill):
        return False
    try:
        qty = D(str(fill["qty"]))
        if activity["fee_kind"] == "COIN":
            symbol = fill.get("symbol")
            if (fill["side"] != "buy" or not isinstance(symbol, str)
                    or symbol.replace("/", "") != activity["symbol"]):
                return False
            base, quantum, posted = qty, FEE_MATCH_V2_COIN_QUANTUM, activity["base_asset_fee_qty"]
        else:
            if fill["side"] != "sell":
                return False
            base, quantum = qty * D(str(fill["price"])), FEE_MATCH_V2_USD_QUANTUM
            posted = activity["cash_fee_usd"]
        return any(
            (rate * base).quantize(quantum, rounding=ROUND_CEILING) == posted
            for rate in FEE_MATCH_V2_RATES
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def _without_fee_evidence(fill):
    """No native fee and no correction. A fill read without ``cost_correction_ids`` (not
    through ``fee_match_fills``) has unknown evidence and is never a V2 candidate."""
    corrections = fill.get("cost_correction_ids")
    return fill.get("fee_usd") is None and corrections is not None and not corrections


def assign_fee_activities_v2(fills, activities, *, taken=()):
    """ALPACA_FEE_MATCH_V2's bindings for normalized V2 ``activities``; writes nothing.

    Returns ``(recorded, bound, unmatched, ambiguous)``:

    - ``recorded``: ``(activity, fill)`` pairs whose correction id (``fee_correction_id``)
      the fill already carries: an earlier run bound the activity there. It is replayed,
      never matched again.
    - ``bound``: new ``(activity, fill)`` pairs. A candidate is a fill without fee evidence
      (``_without_fee_evidence``), not in ``taken`` (bound in this run by another rule),
      for which ``_v2_consistent`` holds. An activity binds when it has exactly one
      candidate and no other activity has that same fill as its only candidate. Bound fills
      leave every other candidate list and the pass repeats until nothing binds, so the
      outcome does not depend on the order of the rows.
    - ``unmatched``: activities left with no candidate. ``ambiguous``: left with two or more
      candidates, or with one that another activity also needs; never guessed.
    """
    taken = set(taken)
    recorded, pending = [], []
    for activity in activities:
        prior = next((
            fill for fill in fills
            if fill.get("cost_correction_ids") and _v2_in_time(activity, fill)
            and fee_correction_id(activity["activity_id"], fill["fill_id"])
            in fill["cost_correction_ids"]
        ), None)
        if prior is not None:
            recorded.append((activity, prior))
        else:
            pending.append(activity)
    pool = [f for f in fills if f["fill_id"] not in taken and _without_fee_evidence(f)]
    options = [[f for f in pool if _v2_consistent(activity, f)] for activity in pending]
    bound, used, remaining = [], set(), list(range(len(pending)))
    while True:
        claims = {}
        for index in remaining:
            live = [f for f in options[index] if f["fill_id"] not in used]
            if len(live) == 1:
                claims.setdefault(live[0]["fill_id"], []).append((index, live[0]))
        winners = [claimed[0] for claimed in claims.values() if len(claimed) == 1]
        if not winners:
            break
        for index, fill in winners:
            bound.append((pending[index], fill))
            used.add(fill["fill_id"])
        won = {index for index, _ in winners}
        remaining = [index for index in remaining if index not in won]
    unmatched, ambiguous = [], []
    for index in remaining:
        live = [f for f in options[index] if f["fill_id"] not in used]
        (ambiguous if live else unmatched).append(pending[index])
    return recorded, bound, unmatched, ambiguous


def _amounts(activity):
    return (activity["fee_kind"], activity.get("symbol"), activity["cash_fee_usd"],
            activity["base_asset_fee_qty"], activity["base_asset_fee_usd"])


def _time_order_matching(activities, fills, options):
    """A complete assignment of ``activities`` to ``fills``, or None.

    The time-order pairing (the n-th row, by posting time then id, to the n-th fill, by fill
    time then id) when every pair is consistent; else the first complete assignment found by
    augmenting paths, rows in time order, each trying its candidates in time order."""
    rows = sorted(activities, key=lambda a: (a["at"], a["activity_id"]))
    ordered = sorted(fills, key=lambda f: (f["filled_at"], f["fill_id"]))
    allowed = {a["activity_id"]: {f["fill_id"] for f in options[a["activity_id"]]} for a in rows}
    if all(f["fill_id"] in allowed[a["activity_id"]] for a, f in zip(rows, ordered, strict=True)):
        return list(zip(rows, ordered, strict=True))
    by_id = {f["fill_id"]: f for f in ordered}
    owner = {}  # fill_id -> row

    def augment(row, seen):
        for fill in sorted(options[row["activity_id"]],
                           key=lambda f: (f["filled_at"], f["fill_id"])):
            if fill["fill_id"] in seen:
                continue
            seen.add(fill["fill_id"])
            if fill["fill_id"] not in owner or augment(owner[fill["fill_id"]], seen):
                owner[fill["fill_id"]] = row
                return True
        return False

    for row in rows:
        if not augment(row, set()):
            return None
    return sorted(((row, by_id[fill_id]) for fill_id, row in owner.items()),
                  key=lambda pair: (pair[0]["at"], pair[0]["activity_id"]))


def assign_fee_activities_v4(fills, ambiguous, *, taken=()):
    """ALPACA_FEE_MATCH_V4's bindings for the rows V2/V3 left ``ambiguous``; writes nothing.

    Returns ``(bound, ambiguous)``. ``taken`` holds the fills bound in this run by V1, V2 or
    V3. A row's candidates are V2's (``_v2_consistent``, a fill without fee evidence, not
    taken). Rows that share a candidate, directly or through other rows, form one group with
    the union of their candidates. A group binds only when all of these hold:

    - as many rows as candidate fills (a fill whose fee row has not been read yet keeps the
      group open: which fill lacks its row is unknown);
    - every complete assignment gives each trade the same fee: all candidate fills are of one
      setup and one side (a coin fee posted without a price, V3, also needs one fill price,
      since it is valued at its fill's price), or every row carries the same amounts;
    - a complete, consistent assignment exists (``_time_order_matching``: time order first).

    Otherwise every row of the group stays ambiguous, never guessed.
    """
    taken = set(taken)
    pool = [f for f in fills if f["fill_id"] not in taken and _without_fee_evidence(f)]
    options = {a["activity_id"]: [f for f in pool if _v2_consistent(a, f)] for a in ambiguous}
    parent = {a["activity_id"]: a["activity_id"] for a in ambiguous}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    first_claim = {}
    for activity in ambiguous:
        for fill in options[activity["activity_id"]]:
            other = first_claim.setdefault(fill["fill_id"], activity["activity_id"])
            parent[root(activity["activity_id"])] = root(other)
    groups = {}
    for activity in ambiguous:
        groups.setdefault(root(activity["activity_id"]), []).append(activity)
    bound, left = [], []
    for rows in groups.values():
        candidates = {f["fill_id"]: f for a in rows for f in options[a["activity_id"]]}
        group_fills = list(candidates.values())
        one_trade = (len({str(f["setup_id"]) for f in group_fills}) == 1
                     and len({f["side"] for f in group_fills}) == 1
                     and (all(a["base_asset_fee_usd"] is not None for a in rows)
                          or len({D(str(f["price"])) for f in group_fills}) == 1))
        same_amounts = len({_amounts(a) for a in rows}) == 1
        pairs = (_time_order_matching(rows, group_fills, options)
                 if len(rows) == len(group_fills) and (one_trade or same_amounts) else None)
        if pairs is None:
            left += rows
            continue
        size = len(rows)
        bound += [({**activity, "match_policy": FEE_MATCH_V4, "row_policy":
                    activity["match_policy"], "group_size": size}, fill)
                  for activity, fill in pairs]
    order = {a["activity_id"]: n for n, a in enumerate(ambiguous)}
    left.sort(key=lambda a: order[a["activity_id"]])
    return bound, left


def _fee_correction(activity, fill):
    reference = "alpaca-activity:" + activity["activity_id"]
    if activity["match_policy"] in (FEE_MATCH_V2, FEE_MATCH_V3):
        reference += ";" + activity["match_policy"]  # The rule that bound it, with the evidence.
    elif activity["match_policy"] == FEE_MATCH_V4:
        reference += (f";{FEE_MATCH_V4};row:{activity['row_policy']}"
                      f";interchangeable-group:{activity['group_size']}")
    base_usd = activity["base_asset_fee_usd"]
    if base_usd is None:
        # ALPACA_FEE_MATCH_V3: the posted quantity at the price of the fill it binds to.
        base_usd = activity["base_asset_fee_qty"] * D(str(fill["price"]))
        reference += ";valued-at-fill-price:" + str(fill["price"])
    return {
        "correction_id": fee_correction_id(activity["activity_id"], fill["fill_id"]),
        "setup_id": str(fill["setup_id"]),
        "fill_id": fill["fill_id"],
        "fill_event_seq": fill["event_seq"],
        "fee_usd": str(activity["cash_fee_usd"] + base_usd),
        "base_asset_fee_qty": str(activity["base_asset_fee_qty"]),
        "base_asset_fee_usd": str(base_usd),
        "source": FEE_ACTIVITY_SOURCE,
        "broker_source_reference": reference,
        "observed_at": max(activity["at"], fill["filled_at"]).isoformat(),
    }


def import_alpaca_fee_activities(store, fills, activities, *, recorded_at):
    """Match Alpaca fee activities to managed fills and append idempotent cost evidence.

    Package fees-net-r (plan phase 0). Uses the existing ``FILL_COST_CORRECTION`` event
    path (``import_fill_cost_correction``) with source ``ALPACA_PAPER_ACTIVITY``; a fill
    or its recorded quantity is never touched. The correction id is derived from the
    (activity id, fill id) pair, so re-importing the same activities is idempotent without
    a separate high-water mark. An activity that cannot be parsed, that matches no fill,
    or whose fill already carries a different latest correction (a conflict this
    automated import never resolves by overwriting) is counted, never guessed.

    A row with an order id binds by ALPACA_FEE_MATCH_V1 (``match_fee_activity``); a row
    without one by ALPACA_FEE_MATCH_V2 (``assign_fee_activities_v2``; a coin row posted with
    no price binds the same way under ALPACA_FEE_MATCH_V3 and is valued at its fill's price),
    after V1's fills are taken out of its candidates; the rows V2/V3 leave ambiguous then
    go to ALPACA_FEE_MATCH_V4 (``assign_fee_activities_v4``). V2 needs ``fills`` as
    ``fee_match_fills`` reads them (with each setup's symbol and market and each fill's
    existing correction ids) and reaching 24 hours before the earliest activity
    (``fee_match_fills_after``).

    ``fills`` are candidate ``lab.managed_fills`` rows (any setup) the caller has already
    read; matching happens in Python so the same pure logic is unit-testable without a
    database. Returns counts: ``activities_read``, ``matched`` (new evidence appended),
    ``already_recorded`` (idempotent replay), ``unmatched``, ``invalid``, ``conflicting``,
    ``ambiguous`` (more than one possible fill that V4 could not bind either, never guessed).
    """
    result = {
        "activities_read": len(activities), "matched": 0, "already_recorded": 0,
        "unmatched": 0, "invalid": 0, "conflicting": 0, "ambiguous": 0,
    }
    by_order, by_amount = [], []  # V1 rows; V2 and V3 rows, which bind by amount together.
    for raw in activities:
        try:
            activity = normalize_fee_activity(raw)
        except ValueError:
            result["invalid"] += 1
            continue
        (by_order if activity["match_policy"] == FEE_MATCH_V1 else by_amount).append(activity)
    bindings = []
    for activity in by_order:
        fill = match_fee_activity(fills, activity)
        if fill is None:
            result["unmatched"] += 1
        else:
            bindings.append((activity, fill))
    taken = {fill["fill_id"] for _, fill in bindings}
    recorded, bound, unmatched, ambiguous = assign_fee_activities_v2(fills, by_amount, taken=taken)
    # ALPACA_FEE_MATCH_V4: only rows V2/V3 left ambiguous, never a fill bound in this run.
    bound_v4, ambiguous = assign_fee_activities_v4(
        fills, ambiguous, taken=taken | {fill["fill_id"] for _, fill in bound})
    result["unmatched"] += len(unmatched)
    result["ambiguous"] += len(ambiguous)
    # A row whose own correction id its fill already carries was recorded by an earlier run:
    # a replay, counted and never written again (a V4 correction names the group it bound in,
    # which a later run, with that group already bound, cannot rebuild).
    result["already_recorded"] += len(recorded)
    bindings += sorted(bound + bound_v4, key=lambda pair: (pair[0]["at"], pair[0]["activity_id"]))
    for activity, fill in bindings:
        try:
            outcome = import_fill_cost_correction(
                store, _fee_correction(activity, fill), recorded_at=recorded_at
            )
        except ValueError:
            result["conflicting"] += 1
            continue
        result["already_recorded" if outcome["idempotent_replay"] else "matched"] += 1
    return result


def fee_match_fills(conn, after):
    """Managed fills filled at or after ``after``, as fee matching needs them, oldest first.

    Each is a ``lab.managed_fills`` row plus its setup's ``symbol`` and ``market`` and
    ``cost_correction_ids``: the ids of its ``FILL_COST_CORRECTION`` events, oldest first
    (ALPACA_FEE_MATCH_V2: a coin fee names only its coin, a fill with fee evidence is
    never a candidate, and an activity already bound is known by its own correction id).
    """
    return conn.execute(
        """SELECT f.*,s.symbol,s.market,coalesce((
            SELECT array_agg(e.body->>'correction_id' ORDER BY e.event_seq)
            FROM lab.managed_events e WHERE e.setup_id=f.setup_id AND e.kind=%s
            AND e.body->>'fill_id'=f.fill_id), ARRAY[]::text[]) AS cost_correction_ids
        FROM lab.managed_fills f JOIN lab.managed_setups s USING(setup_id)
        WHERE f.filled_at>=%s ORDER BY f.filled_at,f.fill_id""",
        (COST_KIND, after),
    ).fetchall()


def fee_match_fills_after(after, activities):
    """Where the fills for these activities start: 24 h (FEE_MATCH_V2_MAX_LAG) before the
    earliest of ``after`` and the activities' own times. A date-filtered read returns rows
    from before ``after``, and an activity bound earlier must find its own fill again."""
    times = [after]
    for raw in activities:
        try:
            times.append(normalize_fee_activity(raw)["at"])
        except ValueError:
            continue
    return min(times) - FEE_MATCH_V2_MAX_LAG


def pending_fee_window_start(conn, *, now, lookback):
    """Where a fee read must start so a fee still missing cannot be lost, or ``None``.

    The earliest managed fill of the last ``FEE_PENDING_MAX_AGE`` without fee evidence (no
    native fee, no ``FILL_COST_CORRECTION``), less ``lookback``, and never earlier than
    ``now - FEE_PENDING_MAX_AGE``. ``None`` when every such fill has evidence.
    """
    floor = now - FEE_PENDING_MAX_AGE
    earliest = conn.execute(
        """SELECT min(f.filled_at) AS at FROM lab.managed_fills f
        WHERE f.filled_at>=%s AND f.fee_usd IS NULL AND NOT EXISTS(
            SELECT 1 FROM lab.managed_events e WHERE e.setup_id=f.setup_id AND e.kind=%s
            AND e.body->>'fill_id'=f.fill_id)""",
        (floor, COST_KIND),
    ).fetchone()["at"]
    return None if earliest is None else max(earliest - lookback, floor)


def _cursor(after_event_seq, limit):
    if (type(after_event_seq) is not int or after_event_seq < 0
            or type(limit) is not int or not 1 <= limit <= 500):
        raise ValueError("INVALID_ANALYTICS_CURSOR")


def paginated_managed_results(repository, *, after_event_seq=0, limit=100):
    """Keyset pages over every closed managed lifecycle, with current cost evidence.

    Operator ENGINEERING_TEST lifecycles stay listed, marked ``engineering``.
    """
    from catalyst_lab.managed_measurement import managed_measurement

    _cursor(after_event_seq, limit)
    with repository.connect() as conn:
        rows = conn.execute(
            """SELECT s.setup_id,s.symbol,s.market,s.cohort,s.strategy_version,t.body AS state,
            t.event_seq AS cursor_event_seq,s.record_json->'agent' AS agent,
            coalesce(s.record_json->>'purpose'=%s,false) AS engineering
            FROM lab.managed_setups s
            JOIN lab.managed_states t USING(setup_id) WHERE s.cohort=%s
            AND t.body->>'state'='CLOSED' AND t.event_seq>%s
            ORDER BY t.event_seq LIMIT %s""",
            (ENGINEERING_PURPOSE, COHORT, after_event_seq, limit + 1),
        ).fetchall()
    items = []
    for row in rows[:limit]:
        measurement = managed_measurement(repository, row["setup_id"])
        # Attribution is a reporting dimension; legacy setups read LEGACY_UNATTRIBUTED.
        agent = agent_fields(row.pop("agent"))
        items.append({**row, **agent, "measurement": measurement,
                      "gross_pnl_usd": measurement["gross_realized_pnl"],
                      "net_pnl_usd": measurement["net_pnl"],
                      "test_r": measurement["test_r"], "net_r": measurement["net_r"],
                      "official_r": measurement["official_r"]})
    return json_safe({
        "analytics_version": ANALYTICS_VERSION, "cohort": COHORT, "baseline_included": False,
        "result_label": LABEL, "items": items,
        "next_cursor": rows[limit - 1]["cursor_event_seq"] if len(rows) > limit else None,
    })


def managed_daily_rollups(repository, *, start_date=None, end_date=None):
    """Aggregate all history by entry/admission NY day; unknown net never becomes zero.

    Operator ENGINEERING_TEST setups (plan 0.10) never enter an item: they are counted only,
    as ``engineering_count`` over the same date range.
    """
    from catalyst_lab.managed_measurement import managed_measurement

    def day(value):
        if value is None:
            return None
        return value if type(value) is date else date.fromisoformat(value)

    start_date, end_date = day(start_date), day(end_date)
    if start_date and end_date and start_date > end_date:
        raise ValueError("INVALID_ANALYTICS_DATE_RANGE")
    with repository.connect() as conn:
        setups = conn.execute(
            """SELECT s.*,t.body AS state,min(f.filled_at) FILTER(WHERE f.side='buy') AS opened
            FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
            LEFT JOIN lab.managed_fills f USING(setup_id) WHERE s.cohort=%s
            GROUP BY s.setup_id,t.body ORDER BY s.event_seq""", (COHORT,),
        ).fetchall()
        failures = conn.execute(
            """SELECT d.setup_id,d.reason FROM lab.managed_risk_decisions d
            JOIN lab.managed_setups s USING(setup_id) WHERE s.cohort=%s AND d.outcome='REJECTED'
            UNION ALL SELECT e.setup_id,e.body->>'reason' AS reason FROM lab.managed_events e
            JOIN lab.managed_setups s USING(setup_id) WHERE s.cohort=%s
            AND e.body->>'reason' IS NOT NULL AND e.kind IN
            ('POSITION_REVIEW_OBSOLETE','POSITION_REVIEW_UNAVAILABLE','MANAGED_JEV_JUDGMENT',
            'CRYPTO_ENTRY_PACING_WAIT','DAILY_SOFT_LOSS_ENTRY_WAIT')""",
            (COHORT, COHORT),
        ).fetchall()
    reasons = {}
    for row in failures:
        reasons.setdefault(row["setup_id"], Counter())[row["reason"]] += 1
    grouped, engineering_count = {}, 0
    for setup in setups:
        state = setup["state"]
        entry_day = _stamp(setup["opened"] or state["admitted_at"]).astimezone(NY).date()
        if (start_date and entry_day < start_date) or (end_date and entry_day > end_date):
            continue
        if is_engineering(setup["record_json"]):
            engineering_count += 1
            continue
        key = (entry_day.isoformat(), setup["market"], setup["cohort"], setup["strategy_version"])
        aggregate = grouped.setdefault(key, {
            "date": key[0], "market": key[1], "cohort": key[2], "strategy_version": key[3],
            "setup_count": 0, "entered_count": 0, "closed_count": 0, "state_counts": Counter(),
            "failure_reason_event_counts": Counter(), "terminal_reason_counts": Counter(),
            "gross_known_count": 0,
            "net_known_count": 0, "net_unknown_closed_count": 0,
            "gross_known_sum_usd": D(0), "net_known_sum_usd": D(0),
        })
        aggregate["setup_count"] += 1
        aggregate["state_counts"][state["state"]] += 1
        if state["state"] in {"CLOSED", "INVALIDATED", "EXPIRED_UNTRIGGERED", "RISK_REJECTED"}:
            reason = (state.get("exit_reason") or state.get("revocation_reason")
                      or state.get("reason") or state.get("last_error"))
            if isinstance(reason, str) and reason:
                aggregate["terminal_reason_counts"][reason] += 1
        aggregate["failure_reason_event_counts"].update(reasons.get(setup["setup_id"], {}))
        if setup["opened"]:
            aggregate["entered_count"] += 1
        if state["state"] == "CLOSED":
            aggregate["closed_count"] += 1
            measured = managed_measurement(repository, setup["setup_id"])
            for kind, field in (("gross", "gross_realized_pnl"), ("net", "net_pnl")):
                if measured[field] is not None:
                    aggregate[kind + "_known_count"] += 1
                    aggregate[kind + "_known_sum_usd"] += D(measured[field])
            if measured["net_pnl"] is None:
                aggregate["net_unknown_closed_count"] += 1
    items = []
    for key in sorted(grouped):
        row = grouped[key]
        row["gross_pnl_usd"] = row["gross_known_sum_usd"] if row["gross_known_count"] else None
        row["net_pnl_usd"] = (
            row["net_known_sum_usd"]
            if row["closed_count"] and row["net_known_count"] == row["closed_count"] else None
        )
        items.append(row)
    return json_safe({
        "analytics_version": ANALYTICS_VERSION, "cohort": COHORT, "baseline_included": False,
        "result_label": LABEL, "day_basis": "FIRST_ENTRY_FILL_ELSE_ADMISSION_AMERICA_NEW_YORK",
        "items": items, "engineering_count": engineering_count,
        "engineering_scope": ENGINEERING_SCOPE,
    })


AGGREGATE_VERSION = "MANAGED_RESULT_AGGREGATES_V1"
# Grouping dimensions available to ``managed_result_aggregates`` (owner ruling, package
# fees-net-r): market, the randomized arm, the research selection policy and question-set
# version (both ``None`` for a legacy/pre-B1 setup), and the proposing agent (attribution).
AGGREGATE_DIMENSIONS = ("market", "arm", "selection_policy", "question_set_version", "agent_id")


def managed_result_aggregates(repository, *, group_by=AGGREGATE_DIMENSIONS):
    """Count, win rate and mean R/fees over every closed managed setup, grouped as asked.

    ENGINEERING_TEST setups are counted (visible, via ``engineering_count``) but never
    enter a group. Within a group: ``count`` is every non-engineering closed setup;
    ``win_rate`` and ``mean_gross_r`` use whatever setups have known gross P&L (fees are
    not required); ``mean_net_r``, ``mean_official_r`` and ``total_fees_usd`` use only
    setups with verified fees whose cost evidence carries no ``LAB_FIXTURE`` source
    (``fixture_tainted_count`` reports how many closed setups were excluded for that
    reason — fixture evidence is fine in a disposable database, but is never counted as
    verified real-account performance here). A statistic with no known input is ``null``,
    never a default of zero.
    """
    from catalyst_lab.managed_measurement import managed_measurement

    if not group_by or not set(group_by) <= set(AGGREGATE_DIMENSIONS):
        raise ValueError("INVALID_AGGREGATE_DIMENSIONS")
    with repository.connect() as conn:
        setups = conn.execute(
            """SELECT s.setup_id,s.market,s.record_json,t.body AS state
            FROM lab.managed_setups s JOIN lab.managed_states t USING(setup_id)
            WHERE s.cohort=%s AND t.body->>'state'='CLOSED'""",
            (COHORT,),
        ).fetchall()
    groups, engineering_count = {}, 0
    for row in setups:
        record = row["record_json"]
        if is_engineering(record):
            engineering_count += 1
            continue
        dims = {
            "market": row["market"],
            "arm": row["state"].get("arm"),
            "selection_policy": record.get("selection_policy"),
            "question_set_version": record.get("question_set_version"),
            "agent_id": agent_fields(record.get("agent"))["agent_id"],
        }
        key = tuple(dims[k] for k in group_by)
        group = groups.setdefault(key, {
            "dims": {k: dims[k] for k in group_by}, "count": 0,
            "gross_known_count": 0, "win_count": 0,
            "gross_r_count": 0, "gross_r_sum": D(0),
            "net_r_count": 0, "net_r_sum": D(0),
            "official_r_count": 0, "official_r_sum": D(0),
            "fees_count": 0, "fees_sum": D(0),
            "fixture_tainted_count": 0,
        })
        group["count"] += 1
        measurement = managed_measurement(repository, row["setup_id"])
        if measurement["gross_realized_pnl"] is not None:
            group["gross_known_count"] += 1
            group["win_count"] += D(measurement["gross_realized_pnl"]) > 0
        if measurement["test_r"] is not None:
            group["gross_r_count"] += 1
            group["gross_r_sum"] += D(measurement["test_r"])
        fixture_tainted = any(
            item["source"] == "LAB_FIXTURE" for item in measurement["cost_evidence"]
        )
        if fixture_tainted:
            group["fixture_tainted_count"] += 1
            continue
        if measurement["net_r"] is not None:
            group["net_r_count"] += 1
            group["net_r_sum"] += D(measurement["net_r"])
        if measurement["official_r"] is not None:
            group["official_r_count"] += 1
            group["official_r_sum"] += D(measurement["official_r"])
        if measurement["verified_cash_fee_usd"] is not None:
            group["fees_count"] += 1
            group["fees_sum"] += D(measurement["verified_cash_fee_usd"])
    items = []
    for group in groups.values():
        items.append({
            **group["dims"],
            "count": group["count"],
            "win_rate": (group["win_count"] / group["gross_known_count"])
            if group["gross_known_count"] else None,
            "mean_gross_r": (group["gross_r_sum"] / group["gross_r_count"])
            if group["gross_r_count"] else None,
            "mean_net_r": (group["net_r_sum"] / group["net_r_count"])
            if group["net_r_count"] else None,
            "mean_official_r": (group["official_r_sum"] / group["official_r_count"])
            if group["official_r_count"] else None,
            "total_fees_usd": group["fees_sum"] if group["fees_count"] else None,
            "fixture_tainted_count": group["fixture_tainted_count"],
        })
    items.sort(key=lambda item: [str(item[k]) for k in group_by])
    return json_safe({
        "aggregate_version": AGGREGATE_VERSION, "cohort": COHORT, "baseline_included": False,
        "result_label": LABEL, "group_by": list(group_by), "items": items,
        "engineering_count": engineering_count, "engineering_scope": ENGINEERING_SCOPE,
        "fixture_scope": FIXTURE_NOT_VERIFIED_SCOPE,
    })

"""Read-only broker eligibility for the managed bridge, preserving US admission checks."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from catalyst_lab.crypto_execution import off_grid_levels
from catalyst_lab.market import NY, decimal, timestamp
from catalyst_lab.repository import json_safe
from catalyst_lab.us_admission import AdmissionProfile


class _Refused(Exception):
    pass


def _reject(reason):
    raise _Refused(reason)


def packet_levels(packet):
    return packet.get("levels") or packet["record_json"]["levels"]


def validate_managed_eligibility(broker, packet, now, profile: AdmissionProfile):
    """Return ``{passed, reason, evidence}``; never submit or persist broker mutations.

    ``now`` may be an aware datetime or a clock callable. Runtime callers should
    pass their clock so expiry/session/quote age are rechecked after GET latency.
    The explicit existing AdmissionProfile supplies the same daily-volume policy
    used by the frozen US admission engine; intraday scanner liquidity is not a substitute.
    """
    clock = now if callable(now) else lambda: now
    evidence = {"source": "ALPACA_PAPER", "policy_id": getattr(profile, "policy_id", None)}
    try:
        if not isinstance(profile, AdmissionProfile):
            _reject("POLICY_NOT_CONFIGURED")
        started = clock()
        if not isinstance(started, datetime) or started.tzinfo is None:
            _reject("INVALID_TIMESTAMP")
        market, symbol = packet["market"], packet["symbol"]
        if market not in {"US_STOCKS", "CRYPTO"}:
            _reject("UNSUPPORTED_EXECUTION_MARKET")
        asset = broker.asset(symbol)
        expected_class = "us_equity" if market == "US_STOCKS" else "crypto"
        if not isinstance(asset, dict) or not (
            asset.get("symbol") == symbol
            and asset.get("class") == expected_class
            and asset.get("status") == "active"
            and asset.get("tradable") is True
        ):
            _reject("ASSET_NOT_TRADABLE")
        evidence.update(
            symbol=symbol,
            market=market,
            asset_class=expected_class,
            asset_status="active",
            tradable=True,
            observed_at=started,
        )
        if market == "CRYPTO":
            if asset.get("fractionable") is not True:
                _reject("CRYPTO_FRACTIONAL_EXECUTION_UNAVAILABLE")
            try:
                increments = {
                    k: decimal(asset[k])
                    for k in ("price_increment", "min_trade_increment", "min_order_size")
                }
                if (increments["min_order_size"] / increments["min_trade_increment"]) % 1:
                    _reject("CRYPTO_PRECISION_UNAVAILABLE")
            except (KeyError, ValueError, ArithmeticError):
                _reject("CRYPTO_PRECISION_UNAVAILABLE")
            evidence.update(increments, eligibility_scope="ASSET_AND_PRECISION_ONLY")
            # Setups admitted before the admission grid check must not enter off-grid either.
            if off_grid_levels(increments["price_increment"], packet_levels(packet)):
                _reject("CRYPTO_LEVEL_OFF_PRICE_GRID")
            return {"passed": True, "reason": None, "evidence": json_safe(evidence)}
        if getattr(broker, "feed", None) not in profile.allowed_feeds:
            _reject("DATA_FEED_FAILURE")
        day = started.astimezone(NY).date()
        sessions = broker.calendar(day - timedelta(days=profile.completed_sessions * 3 + 14), day)
        if not isinstance(sessions, list) or len({s.session_date for s in sessions}) != len(
            sessions
        ):
            _reject("DATA_FEED_FAILURE")
        for session in sessions:
            if (
                session.opens.tzinfo is None
                or session.closes.tzinfo is None
                or not session.opens < session.closes
                or session.opens.astimezone(NY).date() != session.session_date
                or session.closes.astimezone(NY).date() != session.session_date
                or session.session_date > day
            ):
                _reject("DATA_FEED_FAILURE")
        current = next((s for s in sessions if s.session_date == day), None)
        if current is None or not current.opens <= started < current.flatten_time:
            _reject("ENTRY_WINDOW_CLOSED")
        previous = sorted(
            (s for s in sessions if s.session_date < day and s.closes < started),
            key=lambda s: s.session_date,
        )[-profile.completed_sessions :]
        if len(previous) != profile.completed_sessions:
            _reject("LIQUIDITY_EVIDENCE_INCOMPLETE")
        first = datetime.combine(previous[0].session_date, datetime.min.time(), NY)
        end = datetime.combine(day, datetime.min.time(), NY).astimezone(UTC) - timedelta(
            microseconds=1
        )
        bars = broker.daily_bars(symbol, first, end)
        expected, by_day = {s.session_date for s in previous}, {}
        for bar in bars:
            bar_day = timestamp(bar["t"]).astimezone(NY).date()
            if bar_day not in expected or bar_day in by_day:
                _reject("LIQUIDITY_EVIDENCE_INCOMPLETE")
            volume, vwap = decimal(bar["v"], positive=False), decimal(bar["vw"])
            by_day[bar_day] = {
                "session_date": bar_day,
                "volume": volume,
                "vwap": vwap,
                "dollar_volume": volume * vwap,
            }
        if set(by_day) != expected:
            _reject("LIQUIDITY_EVIDENCE_INCOMPLETE")
        average = sum((b["dollar_volume"] for b in by_day.values()), Decimal(0)) / len(by_day)
        evidence.update(
            session_date=day,
            official_open=current.opens,
            official_close=current.closes,
            entry_deadline=current.flatten_time,
            completed_sessions=len(by_day),
            average_daily_dollar_volume=average,
            liquidity_method="MEAN_RAW_DAILY_VOLUME_TIMES_PROVIDER_VWAP",
            daily_bars=[by_day[key] for key in sorted(by_day)],
            data_feed=broker.feed,
            feed_coverage="IEX_ONLY_NOT_CONSOLIDATED"
            if broker.feed == "iex"
            else "SIP_CONSOLIDATED",
        )
        if average < profile.minimum_dollar_volume:
            _reject("MIN_DOLLAR_VOLUME")
        quotes = broker.quotes([symbol])
        if len(quotes) != 1 or quotes[0].ticker != symbol or quotes[0].kind != "quote":
            _reject("DATA_FEED_FAILURE")
        quote = quotes[0]
        finished = clock()
        if not isinstance(finished, datetime) or finished.tzinfo is None or finished < started:
            _reject("DATA_FEED_FAILURE")
        if (
            finished.astimezone(NY).date() != day
            or not current.opens <= finished < current.flatten_time
        ):
            _reject("ENTRY_WINDOW_CLOSED")
        if quote.data_feed != broker.feed or not quote.data_provider:
            _reject("DATA_FEED_FAILURE")
        if not 0 <= (finished - quote.at).total_seconds() <= 5:
            _reject("STALE_QUOTE")
        bid, ask = decimal(quote.bid), decimal(quote.ask)
        if ask < bid:
            _reject("INVALID_QUOTE")
        spread = (ask - bid) / ((ask + bid) / 2) * 10000
        evidence.update(
            quote_timestamp=quote.at, bid=bid, ask=ask, spread_bps=spread, completed_at=finished
        )
        if spread > 10:
            _reject("MAX_SPREAD")
        levels = packet_levels(packet)
        if ask > decimal(levels["max_entry_price"]):
            _reject("PRICE_BEYOND_MAX_ENTRY")
        return {"passed": True, "reason": None, "evidence": json_safe(evidence)}
    except _Refused as exc:
        return {"passed": False, "reason": str(exc), "evidence": json_safe(evidence)}
    except Exception:
        # No provider body, credential, account identifier or raw exception crosses this boundary.
        return {
            "passed": False,
            "reason": "ELIGIBILITY_EVIDENCE_UNAVAILABLE",
            "evidence": json_safe(evidence),
        }

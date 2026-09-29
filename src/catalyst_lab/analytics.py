"""Fill-derived paper statistics. No user-calculated outcomes and no broker mutations."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from catalyst_lab.config import REPORTING_R_METHOD, STRATEGY_VERSION
from catalyst_lab.repository import json_safe

R_FIELDS = {
    "ACTUAL_FILLED": ("r_actual_filled", "initial_risk_dollars"),
    "PLANNED_FILLED": ("r_planned_filled", "planned_filled_risk"),
    "AUTHORIZED_ORDER": ("r_authorized_order", "authorized_order_risk"),
}


def measured_trade(row, method=REPORTING_R_METHOD):
    result = dict(row)
    r_field, risk_field = R_FIELDS.get(method, (None, None))
    risk = row.get(risk_field) if risk_field else None
    result["r_multiple"] = row.get(r_field) if r_field else None
    result["r_method"] = method
    result["mfe_r"] = (
        row["mfe"] * row["entry_qty"] / risk
        if risk and risk > 0 and row.get("mfe") is not None
        else None
    )
    result["mae_r"] = (
        row["mae"] * row["entry_qty"] / risk
        if risk and risk > 0 and row.get("mae") is not None
        else None
    )
    return result


def summarize(trades, baseline=Decimal(0), method=REPORTING_R_METHOD):
    unresolved = sum(t["status"] == "REVIEW_REQUIRED" for t in trades)
    rows = [measured_trade(t, method) for t in trades if t["status"] == "CLOSED"]
    rows.sort(key=lambda r: (r["closed_at"], str(r["trade_id"])))
    wins = [r for r in rows if r["broker_paper_pnl"] > 0]
    losses = [r for r in rows if r["broker_paper_pnl"] < 0]
    profits = sum((r["broker_paper_pnl"] for r in wins), Decimal(0))
    loss = -sum((r["broker_paper_pnl"] for r in losses), Decimal(0))

    def average(group):
        values = [r["r_multiple"] for r in group]
        return sum(values) / len(values) if values and all(v is not None for v in values) else None

    running = peak = drawdown = Decimal(0)
    cumulative_r = Decimal(0) if method else None
    curve, streak, worst = [], 0, 0
    for row in rows:
        running += row["broker_paper_pnl"]
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
        streak = streak + 1 if row["broker_paper_pnl"] < 0 else 0
        worst = max(worst, streak)
        if cumulative_r is not None:
            cumulative_r = (
                cumulative_r + row["r_multiple"] if row["r_multiple"] is not None else None
            )
        curve.append(
            {
                "at": row["closed_at"],
                "pnl": running,
                "equity": baseline + running,
                "r": cumulative_r,
            }
        )
    result = {
        "closed_trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(rows) - len(wins) - len(losses),
        "win_rate": Decimal(len(wins)) / len(rows) if rows else None,
        "gross_pnl": profits - loss,
        "avg_r": average(rows),
        "avg_win_r": average(wins),
        "avg_loss_r": average(losses),
        "profit_factor": profits / loss if loss else None,
        "profit_factor_status": "DEFINED" if loss else "NO_LOSSES" if rows else "NO_TRADES",
        "max_drawdown_dollars": drawdown if rows else None,
        "worst_losing_streak": worst if rows else None,
        "cumulative_r": cumulative_r if rows else None,
        "curve": curve,
        "missing_excursion_trades": sum(r.get("snapshot_count", 0) == 0 for r in rows),
        "r_method": method,
        "pnl_basis": "GROSS_BROKER_FILLS_FEES_NOT_MODELLED",
        "review_required_trades": unresolved,
        "measurement_status": "REVIEW_REQUIRED" if unresolved else "AVAILABLE",
    }
    if unresolved:
        # A busted fill cannot silently disappear from the denominator or curve.
        for key in (
            "wins",
            "losses",
            "breakeven",
            "win_rate",
            "gross_pnl",
            "avg_r",
            "avg_win_r",
            "avg_loss_r",
            "profit_factor",
            "max_drawdown_dollars",
            "worst_losing_streak",
            "cumulative_r",
        ):
            result[key] = None
        result["profit_factor_status"] = "REVIEW_REQUIRED"
        result["curve"] = []
    return result


class Analytics:
    def __init__(self, repository, *, clock=None, r_method=REPORTING_R_METHOD):
        if r_method is not None and r_method not in R_FIELDS:
            raise ValueError("Unknown R definition")
        self.repo, self.method = repository, r_method
        self.now = clock or (lambda: datetime.now(UTC))

    def trades(self, *, public=False, days=None, version=None, limit=100, offset=0):
        relation = "public_trades" if public else "strategy_trades"
        since = self.now() - timedelta(days=days) if days else None
        with self.repo.connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM lab.{relation} WHERE (%s::timestamptz IS NULL OR opened_at>=%s)
                AND (%s::text IS NULL OR strategy_version=%s)
                ORDER BY opened_at DESC,trade_id LIMIT %s OFFSET %s""",
                (since, since, version, version, limit, offset),
            ).fetchall()
        now = self.now()
        for row in rows:
            if row["marked_at"] is None or (now - row["marked_at"]).total_seconds() > 5:
                row["open_unrealized_pnl"] = None
        return json_safe([measured_trade(r, self.method) for r in rows])

    def stats(self, *, days=None, version=None):
        # Headline aggregates may be current; individual symbols/records must use delayed views.
        with self.repo.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM lab.strategy_trades WHERE execution_source='ALPACA_PAPER'
                AND market='US' AND (%s::text IS NULL OR strategy_version=%s)""",
                (version, version),
            ).fetchall()
            baseline = conn.execute("SELECT * FROM lab.reporting_baseline").fetchone()
            status = conn.execute("SELECT * FROM lab.reporting_status").fetchone()
        equity = baseline["equity"] if baseline else Decimal(0)
        total = summarize(rows, equity, self.method)
        since = self.now() - timedelta(days=days) if days else None
        selected = [r for r in rows if since is None or (r["closed_at"] or r["opened_at"]) >= since]
        stats = summarize(selected, equity, self.method)
        # Selecting a reporting window never resets the all-time equity curve or all-time drawdown.
        stats["curve"] = total["curve"]
        stats["all_time_gross_pnl"] = total["gross_pnl"]
        stats["all_time_max_drawdown_dollars"] = total["max_drawdown_dollars"]
        stats["all_time_review_required_trades"] = total["review_required_trades"]
        stats["baseline"] = baseline
        stats["open_positions"] = sum(r["status"] == "OPEN" for r in rows)
        stats["by_catalyst"] = self._groups(selected, "catalyst")
        stats["by_strategy_version"] = self._groups(selected, "strategy_version")
        stats["source_status"] = status
        stats.update(
            label="PAPER TRADING — SIMULATED. Not real money.",
            market="US",
            execution_source="ALPACA_PAPER",
            record_purpose="STRATEGY",
            engineering_tests_excluded=True,
            manual_results_excluded=True,
            strategy_version=version or STRATEGY_VERSION,
            days=days,
            cost_adjustment_status="NOT_MODELLED",
            observed_at=self.now(),
            r_status="CONFIGURED" if self.method else "DENOMINATOR_PENDING",
        )
        return json_safe(stats)

    def _groups(self, rows, key):
        result = []
        for name in sorted({r[key] or "UNSPECIFIED" for r in rows}):
            metrics = summarize(
                [r for r in rows if (r[key] or "UNSPECIFIED") == name], method=self.method
            )
            result.append(
                {
                    "name": name,
                    **{
                        k: metrics[k]
                        for k in (
                            "closed_trades",
                            "gross_pnl",
                            "win_rate",
                            "avg_r",
                            "profit_factor",
                            "review_required_trades",
                        )
                    },
                }
            )
        return result

    def log(self, *, public=False, limit=100, offset=0, state=None, version=None):
        relation = "public_candidates" if public else "reporting_candidates"
        with self.repo.connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM lab.{relation} WHERE (%s::text IS NULL OR state=%s)
                AND (%s::text IS NULL OR strategy_version=%s)
                ORDER BY session_date DESC,received_at DESC,candidate_id LIMIT %s OFFSET %s""",
                (state, state, version, version, limit, offset),
            ).fetchall()
            count = conn.execute(
                f"""SELECT count(*) AS n FROM lab.{relation}
                WHERE (%s::text IS NULL OR state=%s)
                  AND (%s::text IS NULL OR strategy_version=%s)""",
                (state, state, version, version),
            ).fetchone()["n"]
        return json_safe({"items": rows, "total": count, "limit": limit, "offset": offset})

    def detail(self, candidate_id):
        with self.repo.connect() as conn:
            candidate = conn.execute(
                "SELECT * FROM lab.public_candidates WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
            if not candidate:
                return None
            trade = conn.execute(
                "SELECT * FROM lab.public_trades WHERE candidate_id=%s", (candidate_id,)
            ).fetchone()
        return json_safe(
            {"candidate": candidate, "trade": measured_trade(trade, self.method) if trade else None}
        )

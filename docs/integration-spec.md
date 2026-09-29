# Trading System Integration Spec — for the external deployment

How Muse (the AI assistant) connects to the externally deployed trading system.

## Connection requirements
1. **Public HTTPS URL.** Muse's computer must reach it over the internet — `localhost` or home-network-only deployments will NOT work. Any host that gives a public URL (Render, Railway, Fly.io, Vercel, a VPS, etc.) is fine.
2. **Auth (pick one):**
   - API key: sent as `Authorization: Bearer <key>` header. The key is stored in Muse's secure vault — never paste it in chat. You will enter it on a secure page when connecting.
   - Or login (username/password) on the site's sign-in page, handled through the secure sign-in flow.
3. **Polling, not streaming.** Muse checks the system on a schedule (cron jobs), not via a persistent connection. Design endpoints to be cheap to poll.

## What Muse SENDS (candidates, POST to your ingest endpoint)

Muse sends **candidates**, not orders. The system validates, sizes, and executes. Every submission carries the strategy version.

```json
{
  "strategy_version": "CATALYST_RETEST_V1",
  "date": "2026-09-18",
  "candidates": [
    {
      "signal_id": "2026-09-18-INTC-01",
      "market": "US",
      "ticker": "INTC",
      "entry_trigger": 101.00,
      "max_entry_price": 101.15,
      "stop": 96.50,
      "target": 109.00,
      "risk_reward": "1:2.4",
      "catalyst": "EARNINGS | GUIDANCE | M&A | CONTRACT | REGULATORY | PRODUCT | ANALYST | MACRO | LEGAL | MANAGEMENT | SYMPATHY | PARTNERSHIP | LISTING | FLOWS",
      "thesis": "one-line reason",
      "disproof": "what would prove this trade wrong"
    }
  ]
}
```

Notes:
- No `size_shares` — the system computes position size itself from the risk budget.
- `max_entry_price`: if the trigger confirms above this level, the system skips the trade (guideline: ~0.15% above trigger for liquid large caps).
- `signal_id` is unique per ticker per day — the system rejects duplicates (one attempt per ticker per day).
- V1 execution is US equities only; India/Crypto candidates are not submitted for execution (India results via `POST /api/india-result`, crypto via `POST /api/crypto-result` until V2).
- Muse's API permissions: `candidate:create`, `candidate:read`, `analytics:read` — no order placement or override endpoints exist for Muse.

## What Muse READS BACK (per candidate, GET from your status endpoint)
```json
{
  "ticker": "INTC",
  "date": "2026-09-18",
  "strategy_version": "CATALYST_RETEST_V1",
  "status": "rejected | expired | not_entered | open | closed_target | closed_stop | closed_time",
  "rejection_reason": "only when rejected, e.g. CORRELATION | RISK_REWARD | DAILY_HALT | INVALID_LEVELS",
  "fill_price": 101.02,
  "size_shares": 11,
  "mfe_r": 1.6,
  "mae_r": -0.4,
  "exit_price": 108.90,
  "net_r": 1.9,
  "notes": "optional"
}
```

All P&L/R/MFE/MAE figures are computed by the system from broker events, never from Muse's numbers. The underlying record is append-only: nothing is edited after the fact.

## Hard rules
- **Paper trading is fully autonomous by design.** Simulated fills happen mechanically when entry levels print — no human confirmation per trade. This is what makes the validation honest: every signal counted, no cherry-picking.
- **No autonomous REAL trading, ever.** The external system must NOT place, modify, or cancel live orders. Every real-money trade needs the human's explicit per-trade confirmation. This boundary is absolute.

## Alpaca paper trading (the planned deployment)

Architecture: the user's system holds the Alpaca API keys and talks to Alpaca. Muse never holds the keys — Muse sends picks to the user's system, the user's system places paper orders, Muse monitors and grades through the user's system.

- **Endpoint allowlist: `https://paper-api.alpaca.markets` ONLY.** The live endpoint (`[PROHIBITED ENDPOINT REDACTED]`) is a hard never. If Muse ever detects orders or positions on the live endpoint, it halts the integration and alerts the human immediately.
- **Order type: bracket orders.** Each pick maps to one Alpaca bracket order: entry (limit at the entry price) + stop-loss leg + take-profit leg (at the target). This matches the system's entry/stop/target structure exactly. Time exits (flat by session close for stocks) are handled by the user's system closing any open paper position at the deadline, or by Muse instructing the close through the status endpoint.
- **What Muse sends** (per pick, as defined above): ticker, entry, stop, target, size in shares, catalyst, thesis, disproof. The user's system converts size/levels into the bracket order.
- **What Muse reads back**: order status (new / filled / canceled / rejected), fill price and time, open paper positions with unrealized P&L, closed orders with realized P&L. The Auditor uses this for MFE/MAE/R grading.
- Fractional shares: Alpaca paper supports them — useful for high-priced names where the $50 risk budget buys less than one share.
- Paper equity should mirror account.md ($5,000) so R multiples stay comparable.
- **V1 execution scope: US equities only.** Crypto execution comes in V2; India is manual-tracked permanently (Alpaca has no NSE stocks). The dashboard reports all three markets from day one.

## Public dashboard (the experiment, visible to everyone)

The dashboard is the product's proof. Its credibility comes from showing everything.

- **Public URL, no login required.** Clearly labeled at the top: "PAPER TRADING — SIMULATED. Not real money."
- **Display:**
  - Equity curve (in R and in $), from day one
  - Win rate, average win/loss in R, profit factor, max drawdown, worst streak
  - Per-market breakdown (US / India / Crypto) and per-catalyst breakdown (EARNINGS, M&A, SYMPATHY…)
  - Full trade log: every paper trade with date, ticker, catalyst, thesis, disproof, entry, stop, target, exit, exit reason, loss category, net R
- **Honesty rules (non-negotiable):**
  - Every trade appears — winners, losers, and NOT ENTERED signals. No hiding losers.
  - The equity curve NEVER resets. A fresh curve after a drawdown is the oldest trick in the book; capital will check.
  - Everything timestamped. Late-added or edited trades are marked as such.
- **Live picks: publish on delay.** Show today's picks only after the session close, not live. Reasons: (1) live signals get copied, which can distort the very prices the system trades on; (2) if the system ever graduates to real money, live public signals become a liability. Performance stats stay real-time; individual picks go public after close.

## Suggested endpoints (adapt freely, then tell Muse the real paths)
- `POST /api/candidates` — ingest candidates with strategy_version (or a list)
- `GET /api/candidates?date=2026-09-18` — candidate statuses (pending / validated / rejected+reason / triggered / expired)
- `GET /api/positions` — open paper positions
- `GET /api/trades?days=30` — closed-trade log
- `GET /api/stats?days=30` — aggregate stats (win rate, avg R, profit factor, per-catalyst, per-strategy-version)
- `POST /api/india-result` / `POST /api/crypto-result` — manual results for non-Alpaca markets
- `GET /health` — simple liveness check (`{ status, alpaca: "paper", strategy_version, time }`)

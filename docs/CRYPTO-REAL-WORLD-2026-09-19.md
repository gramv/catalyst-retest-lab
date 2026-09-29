# Real crypto research and market observation

2026-09-19. **Actual market data and actual Jev calls; no paper trade opened.**

Codex acted as Muse for BTC/USD, ETH/USD and SOL/USD. The app accepted an authenticated
CRYPTO engineering research report, called pinned `jev-1.13.0` once per packet and retained
the real answers and receipts. All three final dispositions were **NEEDS_REVIEW**.
ETH and SOL had APPROVE verdict answers, but every packet lacked a supported
already-priced judgment. The fixed selection policy did not turn ambiguity into consent.

Sources:

- [Federal Reserve policy statement](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm): adverse macro context, not a confirmed bullish Bitcoin catalyst.
- [Ethereum Foundation protocol priorities](https://blog.ethereum.org/2026/09/07/protocol-priorities): planned faster-finality work, not a completed deployment or proven token-demand shock.
- [Solana engineering changelog](https://solana.com/news/solana-changelog-september-18-2026): network changes, with no demonstrated unpriced token-demand surprise.
- Alpaca's public `CRYPTO_US` quotes, trades and one-minute bars. Exact retrieval/provider timestamps,
  response hashes and original short excerpts are retained. Bars without trades/volume were excluded
  from the analyst reference geometry; quote-derived bars were never called printed trades.

The proposed levels were explicitly research references from the latest completed traded
bar, current ask and observed traded-hour extrema. No executable crypto strategy, quantity,
daily reset, time exit or trailing formula was introduced. V1 remains unchanged.

## Results

- Two-minute REST observation, approximately five-second polling: **24 batches / 72 symbol checks**.
- Changing provider quotes observed: **11 BTC, 5 ETH, 6 SOL distinct quote timestamps**.
- Against the existing US five-second freshness diagnostic, **53/72 checks had old quotes**
  (BTC 15, ETH 19, SOL 19). This is a diagnostic comparison, not adoption of a crypto policy.
  A successful HTTP response alone is not proof of fresh market evidence. REST polling is not
  streaming acceptance, and unchanged quote timestamps do not by themselves prove a provider outage.
- Three TypeSafe attempts, three valid receipts, all integrity checks passed.
- Provider latency p50 **564.95 ms**, p95 **642.28 ms**, n=3. No event-to-order claim.
- All three research records expired at their original deadline; no queued entry permission.
- **Zero** candidates, orders, fills or risk decisions created. Broker mutations: **zero**.
- Existing account broker/local exposure remained zero; account observer continued independently.
- **10 real-data browser checks passed**; **2 crypto/India-to-US isolation tests passed**.
  Lint/diff checks passed. The prior full 584-test result was not represented as a fresh full run.

The isolated review audit includes the market observations through normal SYSTEM_EVENT
records: **65 events**, verified head:

```text
a9637abc1b365ae3815827ce493f0a5682e94ae96589d52e02ba9f3297fb3c9b
```

Artifacts: `artifacts/crypto-real-world-2026-09-19/` contains source provenance, submitted
report, real judgments, receipts in the audit export, market observations, expired readback,
before/after account snapshots, summary and desktop/mobile screenshots.
Private database: `~/.local/share/catalyst-retest-lab/crypto-20260919`.
The bounded observer, review service and private database stopped after the test.

## Execution blocker

`CRYPTO_EXECUTION_NOT_IMPLEMENTED`. The app's existing authorizing bridge only admits
US-stock engineering candidates. The stock DAY bracket, whole-share sizing and calendar
exit cannot be silently reused for crypto. [Alpaca's crypto documentation](https://docs.alpaca.markets/us/docs/crypto-trading)
specifies different order/TIF and fractional-quantity behavior. The separate crypto controller
still needs risk-authorized orders, protection/exit recovery, account-wide exposure accounting,
fees, quantity increments and an approved crypto timing policy. No raw-order workaround was used.

This test proves real research/review, durable observation and market isolation. It does not
prove crypto fills, position management, streaming reliability or continuous Jev trade monitoring.
Jev was called for the research batch; no open trade existed to manage.

## Harness setup corrections

Before any report intake or provider vote, the test runner encountered a macOS socket-path
length limit, then an unsupported audit event type and a too-narrow role for its resume query.
The fixes shortened the private path, used the existing SYSTEM_EVENT writer and used the
existing review-worker read grants. No database grants or application policy were relaxed.
Resume explicitly verified zero research reports and zero Jev requests before continuing;
no model answer was discarded or rerun. Failed setup files were retained separately.

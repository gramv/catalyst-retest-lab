"""Supervised Muse crypto report -> real Jev SKEPTIC reviews (2026-09-22 test).

Claude acted as Muse and did the research. Same pattern as crypto_broad_research.py:
a disposable research ledger, primary sources re-fetched and checked word for word at
run time, the owner-approved Gate1 policy unchanged, and real TypeSafe reviews.
It has no broker client and does no admission, risk authorization or order work.
Levels are aggregate 24h reference prices, not an executable crypto strategy.

Usage (from the project root):
    ./run python scripts/muse_crypto_test.py --prepare-only   # checks sources, no Jev calls
    ./run python scripts/muse_crypto_test.py                  # submits + real Jev review
"""

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from real_world_review_session import PageText, save

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import JEV_MODEL
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchItem, ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings

PROJECT = Path(__file__).resolve().parents[1]
OUT = PROJECT / "artifacts/muse-crypto-test-2026-09-22"
# Short path: macOS Unix sockets have a 103-byte limit.
ROOT = Path.home() / ".local/share/catalyst-retest-lab/muse-0922"
MARKET_URL = "https://api.coingecko.com/api/v3/coins/markets"
LEVEL_NOTE = (
    " Levels are CoinGecko 24h aggregate low/current/high reference prices only; "
    "no executable entry, venue quote or 2R setup is asserted."
)

# Muse research, fixed before any Jev call. 32 assets screened; 17 had no qualifying
# primary-source catalyst and are listed in NOT_SUBMITTED rather than padded in.
PACKETS = [
    {
        "symbol": "ETH", "coingecko": "ethereum", "catalyst": "SCHEDULED_EVENT",
        "sources": [
            ("https://blog.ethereum.org/2026/09/07/protocol-priorities",
             "Shipping Glamsterdam in December 2026 and L* in December 2029 requires an "
             "average cadence of 7.2 months per fork."),
            ("https://blog.ethereum.org/2026/09/07/protocol-priorities",
             "The EF Protocol cluster is aiming for Ethereum L1 to be quantum-resistant across "
             "all three layers (execution, consensus, and data) by December 2029."),
        ],
        "thesis": "Quoted fact: the EF Protocol post (2026-09-07) plans around shipping the "
        "Glamsterdam hard fork in December 2026 and a quantum-resistant L1 by December 2029. "
        "Inference: an EF-committed date for the next fork gives a visible Q4 2026 event.",
        "economic": "Indirect. If Glamsterdam ships on time and added L1 capacity is used, "
        "more activity could raise fee burn and ETH demand for gas and collateral. Depends on "
        "no schedule slip, real usage of new capacity, and the date not already being priced. "
        "The 2029 quantum goal is a narrative/risk-premium effect, not near-term cash flow.",
        "disproof": "The post itself calls the cadence aggressive. Mainnet activation is not "
        "yet scheduled; public testnet dates are October and tentative. Glamsterdam has been "
        "on the public roadmap all year, so this restates a known target rather than "
        "disclosing new information.",
    },
    {
        "symbol": "SOL", "coingecko": "solana", "catalyst": "SCHEDULED_EVENT",
        "sources": [
            ("https://solana.com/upgrades/alpenglow",
             "Votor is the first phase of the rollout, replacing vote transactions with direct "
             "validator votes and aggregate certificates and reducing finality from roughly "
             "12.8 seconds to a target of around 150ms."),
        ],
        "thesis": "Quoted fact (Solana Foundation): Alpenglow's Votor phase replaces vote "
        "transactions and targets roughly 150ms finality versus about 12.8s. Separately, Agave "
        "v4.3.0 was tagged stable on 2026-09-18 and Anza's tentative schedule lists mainnet "
        "feature activation from 2026-09-28. Inference: a major consensus change may activate "
        "soon.",
        "economic": "Faster finality could improve payments/trading UX, raising on-chain "
        "activity and SOL demand for fees, priority fees and staking. Offsetting: removing vote "
        "transactions removes vote-fee burn; the net supply effect is unverified. Depends on "
        "activation without outage and real app adoption.",
        "disproof": "Anza says dates are tentative; the cited page says expected October and "
        "'In Development'. A consensus swap carries liveness risk. Alpenglow was announced in "
        "May 2025 and dated in August 2026, so it is widely expected; the cited page predates "
        "September (updated later).",
    },
    {
        "symbol": "AVAX", "coingecko": "avalanche-2", "catalyst": "SCHEDULED_EVENT",
        "sources": [
            ("https://build.avax.network/blog/helicon-upgrade",
             "It lowers the mean consumption rate from 10% to 7.5%, pulling the overall "
             "annualized reward rate (ARR) down from about 5.4% to 4% while leaving the "
             "one-year rate at the top of the curve where it is."),
        ],
        "thesis": "Quoted fact (Ava Labs, 2026-09-08): Helicon (ACP-285) cuts the mean staking "
        "consumption rate from 10% to 7.5%. The same post says minimum staking falls from 14 "
        "days to 48 hours. Mainnet activation was scheduled for 2026-09-22. Inference: lower "
        "new issuance plus institution-friendly staking could support AVAX.",
        "economic": "Lower reward emissions slow circulating-supply growth; a 48-hour minimum "
        "stake suits products with short redemption windows. Depends on the roughly 90-day "
        "ramp completing, lower yield not driving unstaking, and products adopting the "
        "shorter duration. Inflation estimates are Ava Labs' own model.",
        "disproof": "Staking APR falls from about 5.4% to 4%, which may push yield-seekers out. "
        "The cut phases in over about 90 days. The upgrade was on Fuji testnet since "
        "2026-07-28 and pre-announced, so likely priced. Successful activation not verified "
        "at research time.",
    },
    {
        "symbol": "LINK", "coingecko": "chainlink", "catalyst": "CONTINUATION",
        "sources": [
            ("https://www.globenewswire.com/news-release/2026/09/17/3363840/28640/en/"
             "bottomline-launches-global-pay-connect-to-provide-financial-institutions-with-a-"
             "future-proofed-payment-messaging-ecosystem.html",
             "Global Pay Connect enables interoperability through Chainlink, connecting banks "
             "seamlessly to multiple blockchain networks through a single, network-agnostic "
             "integration model via their existing Bottomline connectivity."),
        ],
        "thesis": "Quoted fact (Bottomline release, 2026-09-17): Bottomline launched Global Pay "
        "Connect with interoperability through Chainlink. Inference: a large payments-"
        "messaging provider shipping a product that uses Chainlink is a concrete adoption step.",
        "economic": "If banks route volume through Chainlink services via Bottomline, Chainlink "
        "earns fees; Chainlink's stated reserve converts revenue into LINK. Depends on banks "
        "enabling the blockchain features, fees being material, and undisclosed contract terms "
        "flowing to the reserve.",
        "disproof": "The release is mostly traditional payment messaging and gives no volumes, "
        "bank sign-ups or fee terms. The partnership was announced 2026-09-03; this is the "
        "follow-through launch and may be priced. Many prior bank pilots produced no measurable "
        "LINK fee flow.",
    },
    {
        "symbol": "DOT", "coingecko": "polkadot", "catalyst": "SCHEDULED_EVENT",
        "sources": [
            ("https://polkadot.subsquare.io/referenda/1944",
             "The fundamental operation is straightforward: a user locks DOT, and against that "
             "collateral mints dotUSD at a value less than the collateral deposited."),
        ],
        "thesis": "Quoted fact: OpenGov Referendum 1944 proposes dotUSD, a stablecoin minted "
        "against locked DOT collateral. It was in the Deciding phase with strong aye weight "
        "at research time. Inference: if passed and phase 2 ships, DOT gains a lock-up use "
        "as collateral.",
        "economic": "Minting dotUSD in phase 2 requires locking DOT above 150% collateral, "
        "removing DOT from liquid supply. Depends on the referendum passing, phase 2 "
        "(oracle, vaults, liquidations) shipping, and real dotUSD demand. Phase 1 is "
        "USDT-backed and locks no user DOT.",
        "disproof": "Not yet passed; Root-track support was low relative to the required "
        "threshold. The proposal itself warns of liquidation reflexivity. Treasury DOT seeding "
        "a pool is a supply outflow. Secondary coverage attributes a large weekly DOT move to "
        "the vote, so it may be priced.",
    },
    {
        "symbol": "AAVE", "coingecko": "aave", "catalyst": "CONTINUATION",
        "sources": [
            ("https://aave.com/docs/resources/changelog",
             "The market deploys a Core Hub with Main and Forex Spokes, listing USDC, EURC, "
             "cirBTC, and WETH."),
        ],
        "thesis": "Quoted fact (changelog, 2026-09-16): Aave V4 launched on Arc with a Core Hub "
        "and Main and Forex Spokes. It is V4's third market after Ethereum and Avalanche. "
        "Inference: a stablecoin-focused chain could add USDC/EURC borrowing and protocol "
        "revenue.",
        "economic": "More deposits and borrowing on Arc raise reserve-factor income to the DAO, "
        "which can acquire AAVE via its treasury steward. Depends on real (not incentive-farmed) "
        "demand, revenue exceeding incentive spend, and the DAO actually buying AAVE. None of "
        "this is shown on the page.",
        "disproof": "New-chain deployments are routine for Aave and likely priced. Arc is new "
        "with unproven activity. The DAO budgeted incentives for new V4 instances, offsetting "
        "revenue. Refuted if Arc borrowing stays small after 4-8 weeks.",
    },
    {
        "symbol": "UNI", "coingecko": "uniswap", "catalyst": "FRESH_CATALYST",
        "sources": [
            ("https://www.cmegroup.com/media-room/press-releases/2026/9/22/"
             "cme_group_to_expandcryptoderivativessuitewithbitcoincashandunisw.html",
             "Uniswap futures (10,000 UNI) and Micro Uniswap futures (1,000 UNI)"),
            ("https://investor.cmegroup.com/news-releases/news-release-details/"
             "cme-group-expand-crypto-derivatives-suite-bitcoin-cash-and",
             "Uniswap futures (10,000 UNI) and Micro Uniswap futures (1,000 UNI)"),
        ],
        "thesis": "Quoted fact: CME Group announced (2026-09-22) Uniswap futures and Micro "
        "futures, planned to launch 19 October pending regulatory review. Inference: a "
        "regulated futures market widens institutional access to UNI.",
        "economic": "Futures do not create spot demand directly. Possible routes: hedging and "
        "basis trading support spot liquidity; a regulated futures history may later help a "
        "spot product qualify (analyst understanding, not stated in the source). Depends on "
        "approval, open interest, and issuer interest.",
        "disproof": "Launch is pending regulatory review. Futures also ease shorting. Past CME "
        "altcoin launches had mixed spot effects. Public same-day news is likely priced within "
        "hours.",
    },
    {
        "symbol": "BCH", "coingecko": "bitcoin-cash", "catalyst": "FRESH_CATALYST",
        "sources": [
            ("https://www.cmegroup.com/media-room/press-releases/2026/9/22/"
             "cme_group_to_expandcryptoderivativessuitewithbitcoincashandunisw.html",
             "Bitcoin Cash futures (250 BCH) and Micro Bitcoin Cash futures (25 BCH)"),
            ("https://investor.cmegroup.com/news-releases/news-release-details/"
             "cme-group-expand-crypto-derivatives-suite-bitcoin-cash-and",
             "Bitcoin Cash futures (250 BCH) and Micro Bitcoin Cash futures (25 BCH)"),
        ],
        "thesis": "Quoted fact: CME Group announced (2026-09-22) Bitcoin Cash futures and Micro "
        "futures, planned to launch 19 October pending regulatory review. Inference: regulated "
        "derivatives widen institutional access to BCH.",
        "economic": "Indirect and demand-side only (BCH issuance is unchanged): regulated "
        "hedging can raise institutional participation and spot liquidity, and a futures "
        "history may later help a spot product qualify (analyst understanding, not in the "
        "source).",
        "disproof": "Secondary reports say BCH rose about 27% on the announcement day, so much "
        "may be priced and mean-reversion risk is high. Futures ease shorting. Launch is "
        "pending review. Network fundamentals are unchanged.",
    },
    {
        "symbol": "ARB", "coingecko": "arbitrum", "catalyst": "FRESH_CATALYST",
        "sources": [
            ("https://blog.arbitrum.foundation/"
             "arbitrum-foundation-reports-first-half-2026-progress-update/",
             "In July, AEP licence fees of $360,000 were 35% of ArbitrumDAO income."),
        ],
        "thesis": "Quoted fact (Arbitrum Foundation H1 report, 2026-09-02): July licence fees "
        "from the Expansion Program were $360,000, 35% of DAO income, after Robinhood Chain "
        "went live on 1 July. Inference: a new recurring revenue line may change how ARB "
        "fundamentals are viewed.",
        "economic": "Expansion Program chains pay a share of net revenue to the DAO treasury "
        "that ARB governs. Value reaches ARB only if the market prices governance over a "
        "growing treasury or the DAO adopts buybacks/fee-sharing; neither is in place per the "
        "source.",
        "disproof": "ARB holders have no direct income claim. Annualised income is small versus "
        "market cap. Token vesting continues through March 2027. The Q3 growth guidance is "
        "extrapolated from one month. The Robinhood Chain launch was known since July. The "
        "report is unaudited.",
    },
    {
        "symbol": "CRV", "coingecko": "curve-dao-token", "catalyst": "CONTINUATION",
        "sources": [
            ("https://news.curve.finance/curve-monthly-recap-august-2026/",
             "Borrower-minted crvUSD grew from $36.7M to $72.2M in August, a 97% increase."),
        ],
        "thesis": "Quoted fact (Curve August recap, 2026-09-07): borrower-minted crvUSD grew "
        "97% to $72.2M in August. The recap also reports annual CRV emissions falling below "
        "100M. Inference: rising lending use plus lower issuance modestly improves CRV "
        "supply/demand.",
        "economic": "More crvUSD and lending activity produces fees for veCRV, making CRV locking "
        "more attractive and reducing circulating float; lower emissions add less new CRV. "
        "Depends on borrowing growth persisting and fees being material.",
        "disproof": "Growth is concentrated: one position drove about 56% of the August "
        "increase. The debt base and veCRV distributions are small. The emission cut was "
        "scheduled for years. This is backward-looking data, not a new event. Refuted if "
        "borrower-minted crvUSD falls back in September.",
    },
    {
        "symbol": "FIL", "coingecko": "filecoin", "catalyst": "SCHEDULED_EVENT",
        "sources": [
            ("https://www.filecoin.io/blog/the-2026-filecoin-network-strategy",
             "Later this year the final network vesting periods will end, shepherding in a new "
             "era of token economics."),
        ],
        "thesis": "Quoted fact (Filecoin 2026 strategy): the final network vesting periods end "
        "later this year. A 2020 Filecoin post describes 6-year linear vesting from the "
        "October 2020 launch, implying an end around mid-October 2026. Inference: a large "
        "scheduled source of new supply stops within weeks.",
        "economic": "When vesting ends, gross new FIL comes mostly from block rewards, lowering "
        "supply growth and potential vesting-holder selling. Depends on vesting being a large "
        "share of issuance (secondary estimate, unverified), those tokens having been sold, "
        "and demand holding.",
        "disproof": "Known since 2020 and fully scheduled, so likely priced. A gross issuance "
        "cut is not a net supply cut (burns, pledge unlocks). The cited post dates from "
        "February 2026, not September. Exact end date comes from secondary coverage.",
    },
    {
        "symbol": "ONDO", "coingecko": "ondo-finance", "catalyst": "FRESH_CATALYST",
        "sources": [
            ("https://ondo.finance/blog/ondo-joins-dtcc-fund-serv",
             "First tokenization company to join the operational backbone of the U.S. fund "
             "industry."),
        ],
        "thesis": "Quoted fact (Ondo, 2026-09-16): Ondo's broker-dealer subsidiary joined "
        "DTCC Fund/SERV, the first tokenization company to do so. Inference: a distribution "
        "channel to traditional wealth platforms could grow Ondo's tokenized-fund assets.",
        "economic": "Weak and indirect: distribution could raise Ondo's assets and fees, "
        "improving perceptions of the ecosystem ONDO governs. ONDO has no stated fee share "
        "or buyback in this source; the thesis assumes the market treats ONDO as a proxy for "
        "the company.",
        "disproof": "Infrastructure access, not proven flows; no volumes or assets given. ONDO "
        "is a governance token without a revenue claim. Earlier DTCC ties existed (July). "
        "Refuted by no reported asset growth via Fund/SERV next quarter.",
    },
    {
        "symbol": "HYPE", "coingecko": "hyperliquid", "catalyst": "FRESH_CATALYST",
        "sources": [
            ("https://www.payward.com/press-release/payward-hyperliquid-onchain-perpetual-futures",
             "Hyperliquid is the first protocol Payward plans to deploy on for US clients."),
        ],
        "thesis": "Quoted fact (Payward, 2026-09-16): Payward plans onchain perpetual futures "
        "for US clients, starting with Hyperliquid, via a CFTC-regulated exchange and "
        "clearinghouse, subject to regulatory approval. Inference: a regulated route for US "
        "order flow onto Hyperliquid.",
        "economic": "US volume would generate protocol fees; under Hyperliquid's design part "
        "funds HYPE buybacks, and market deployers stake HYPE (analyst background knowledge, "
        "not in this source). Depends on approval, meaningful volume, and those mechanisms "
        "staying unchanged.",
        "disproof": "Stated as intent, subject to regulatory approval, with no launch date, fees "
        "or volume targets. Access is allowlisted. HYPE was near all-time highs per secondary "
        "sources, so it may be priced. Ongoing token unlocks add supply.",
    },
    {
        "symbol": "SKY", "coingecko": "sky", "catalyst": "CONTINUATION",
        "sources": [
            ("https://raw.githubusercontent.com/sky-ecosystem/executive-votes/main/2026/"
             "executive-vote-2026-09-10-august-msc-lssky-staking-rewards-update-sky-burn.md",
             "The Treasury Management Function will be executed, including burning SKY from the "
             "Pause Proxy"),
        ],
        "thesis": "Quoted fact (Sky executive vote, 2026-09-10): the spell burns SKY from the "
        "Pause Proxy, updates staking rewards and shortens the buyback cycle. Inference: this "
        "continues surplus-funded supply reduction; it is not a new program.",
        "economic": "Protocol surplus funds SKY buybacks; part is burned and part paid as "
        "staking rewards. A shorter buyback interval may raise the effective buyback rate if "
        "per-cycle size is unchanged (unverified). Depends on positive surplus.",
        "disproof": "The burn is tiny relative to supply, and the same spell starts a much "
        "larger 90-day staking-reward vest that adds float. Monthly spells with burns are "
        "routine and presumably priced. Earlier in 2026 the buyback budget was cut sharply.",
    },
    {
        "symbol": "XTZ", "coingecko": "tezos", "catalyst": "FRESH_CATALYST",
        "sources": [
            ("https://spotlight.tezos.com/why-post-quantum-why-now/",
             "Today, Tezos core developers are unveiling a previewnet for a post-quantum version "
             "of Tezos."),
        ],
        "thesis": "Quoted fact (Tezos, 2026-09-02): core developers unveiled a previewnet for a "
        "post-quantum Tezos, to be made public during September, with components to be "
        "submitted as future protocol amendments. Inference: if quantum risk becomes a market "
        "theme, Tezos could be re-rated as an early mover.",
        "economic": "Narrative and positioning, not cash flow: holders worried about quantum "
        "risk might prefer a chain with a concrete migration path. No change to XTZ supply or "
        "fee capture is stated. Depends on market attention, a working public previewnet, and "
        "later governance approval.",
        "disproof": "This is an experimental testnet preview. Mainnet requires several future "
        "amendments with no dates. A first post-quantum step was already disclosed in June. "
        "Other chains pursue the same goal. XTZ upgrades have historically not driven price.",
    },
]

NOT_SUBMITTED = {
    "BTC": "No primary-source development since 2026-09-01; ETF flows are secondary data.",
    "XRP": "Amendment status only via JS page/secondary outlets; weak link to XRP value.",
    "DOGE": "No mechanism-bearing primary development.",
    "ADA": "No fetchable post-Sept-1 primary source; immaterial fee effect.",
    "LTC": "Maintenance releases only.",
    "GRT": "Only neutral/negative governance items (issuance reallocation).",
    "LDO": "Primary evidence points negative (buyback budget shortfall).",
    "POL": "Burn plan only on X, secondhand; pending sign-off.",
    "RENDER": "Monthly activity report only.",
    "SUSHI": "Latest news predates September; JS-only sources.",
    "YFI": "Only a non-proposal forum idea with zero replies.",
    "BAT": "No BAT-specific September development.",
    "SHIB": "Remediation after a reorg, not a catalyst.",
    "PEPE": "Old pending ETF filing only.",
    "BONK": "Net negative news (reported exchange delisting).",
    "WIF": "Price action only.",
    "PAXG": "Value tracks gold; no issuer catalyst.",
}


def page_text(html):
    parser = PageText()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts))


async def verify_source(client, symbol, candidates):
    """First candidate whose excerpt appears verbatim on the live page wins."""
    failures = []
    for url, excerpt in candidates:
        try:
            async with asyncio.timeout(25):
                response = await client.get(url)
                response.raise_for_status()
        except (TimeoutError, httpx.HTTPError) as exc:
            failures.append({"url": url, "failure": "FETCH_FAILED:" + type(exc).__name__})
            continue
        if excerpt not in page_text(response.text):
            failures.append({"url": url, "failure": "EXCERPT_NOT_FOUND"})
            continue
        return {
            "source": {
                "source_id": symbol.lower() + "-primary",
                "url": url,
                "excerpt": excerpt,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "published_at": None,
            },
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            "earlier_candidate_failures": failures,
        }
    raise ValueError(json.dumps(failures))


async def prepare():
    dropped = []
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        results = await asyncio.gather(
            *(verify_source(client, p["symbol"], p["sources"]) for p in PACKETS),
            return_exceptions=True,
        )
        verified = []
        for packet, result in zip(PACKETS, results, strict=True):
            if isinstance(result, Exception):
                detail = str(result) if isinstance(result, ValueError) else type(result).__name__
                dropped.append({"symbol": packet["symbol"], "stage": "SOURCE", "detail": detail})
            else:
                verified.append((packet, result))
        if not verified:
            raise SystemExit("NO_SOURCES_VERIFIED: " + json.dumps(dropped))
        response = await client.get(
            MARKET_URL,
            params={"vs_currency": "usd", "ids": ",".join(p["coingecko"] for p, _ in verified)},
        )
        response.raise_for_status()
        rows = {r["id"]: r for r in response.json()}
        received = datetime.now(UTC)
    items, provenance = [], []
    for packet, result in verified:
        symbol, row = packet["symbol"], rows.get(packet["coingecko"])
        if not row or row.get("symbol", "").upper() != symbol:
            dropped.append({"symbol": symbol, "stage": "PRICE", "detail": "ASSET_IDENTITY"})
            continue
        try:
            price, low, high = (float(row[k]) for k in ("current_price", "low_24h", "high_24h"))
        except (KeyError, TypeError, ValueError):
            dropped.append({"symbol": symbol, "stage": "PRICE", "detail": "PRICE_MISSING"})
            continue
        if not 0 < low <= price <= high:
            dropped.append({"symbol": symbol, "stage": "PRICE", "detail": "INCONSISTENT_RANGE"})
            continue
        market = {k: row[k] for k in ("id", "symbol", "current_price", "low_24h", "high_24h",
                                      "total_volume", "price_change_percentage_24h",
                                      "last_updated")}
        market["provider"] = "COINGECKO_AGGREGATED_NOT_EXECUTION_QUOTES"
        item = {
            "signal_id": "TEST-CRYPTO-MUSE-20260922-" + symbol,
            "symbol": symbol + "/USD",
            "direction": "LONG",
            "catalyst": packet["catalyst"],
            "thesis": packet["thesis"],
            "disproof": packet["disproof"] + LEVEL_NOTE,
            "economic_relationship": packet["economic"],
            "levels": {"entry_trigger": str(row["current_price"]),
                       "max_entry_price": str(row["current_price"]),
                       "stop": str(row["low_24h"]), "target": str(row["high_24h"])},
            "sources": [
                result["source"],
                {"source_id": "aggregate-market-reference", "url": MARKET_URL,
                 "excerpt": json.dumps(market, separators=(",", ":")),
                 "retrieved_at": received.isoformat(), "published_at": None},
            ],
        }
        try:
            ResearchItem.model_validate(item)  # same contract + privacy check the intake uses
        except Exception as exc:  # never echo field contents; they could carry page text
            dropped.append({"symbol": symbol, "stage": "CONTRACT", "detail": type(exc).__name__})
            continue
        items.append(item)
        provenance.append({"symbol": symbol, **result, "market_reference": market})
    if not items:
        raise SystemExit("NO_VALID_ITEMS: " + json.dumps(dropped))
    now = datetime.now(UTC)
    oldest = min(datetime.fromisoformat(s["retrieved_at"]) for i in items for s in i["sources"])
    if (now - oldest).total_seconds() >= 55:
        raise SystemExit("SOURCES_TOO_OLD_BEFORE_INTAKE_RETRY_LATER")
    packet = {
        "submission_id": str(uuid4()),
        "report_id": str(uuid4()),
        "report_key": "TEST-CRYPTO-MUSE-2026-09-22",
        "revision": 1,
        "market": "CRYPTO",
        "timeframe": "MUSE_RESEARCH_TEST",
        "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(),
        "items": items,
    }
    screening = {
        "screened": len(PACKETS) + len(NOT_SUBMITTED),
        "researched_contenders": len(PACKETS),
        "submitted": len(items),
        "not_submitted_no_catalyst": NOT_SUBMITTED,
        "dropped_at_runtime": dropped,
        "target_20_30_met": len(items) >= 20,
        "shortage_reason": "Muse guidelines forbid padding; only primary-source catalysts kept.",
    }
    return packet, provenance, screening


async def run(args):
    OUT.mkdir(parents=True, exist_ok=True)
    if args.prepare_only:
        packet, provenance, screening = await prepare()
        save(OUT / "prepared-not-submitted.json", packet)
        save(OUT / "source-provenance.json", provenance)
        save(OUT / "screening.json", screening)
        print(json.dumps({"mode": "PREPARE_ONLY", "jev_calls": 0,
                          "submitted_if_run": [i["symbol"] for i in packet["items"]],
                          "dropped": screening["dropped_at_runtime"]}, indent=2))
        return
    typesafe_key()  # Private .env (mode 0600) or keychain; value never printed or stored here.
    localdb.start(ROOT)  # Disposable research ledger, separate from the runtime database.
    worker, stopped = None, False
    try:
        repo = Repository(localdb.connection_url(ROOT))
        repo.check_role()
        gate = Gate1Inputs(APPROVED_GATE1)
        reports = ResearchReports(localdb.connection_url(ROOT, "catalyst_review"), gate)
        with reports.connect() as conn:
            if conn.execute("SELECT count(*) AS n FROM lab.research_reports").fetchone()["n"]:
                raise SystemExit("REPORT_ALREADY_REVIEWED_IN_THIS_LEDGER_NO_REVOTE")
        worker = ReviewWorker(WorkerSettings(
            localdb.connection_url(ROOT, "catalyst_jev"), gate, "muse-crypto-0922", 20, 0.1))
        packet, provenance, screening = await prepare()
        save(OUT / "submitted-report.json", packet)
        save(OUT / "source-provenance.json", provenance)
        save(OUT / "screening.json", screening)
        intake = reports.submit(packet)
        save(OUT / "intake.json", intake)
        if not intake.get("accepted"):
            raise SystemExit("INTAKE_REJECTED: " + json.dumps(json_safe(intake)))
        for _ in range(200):
            await worker.tick()
            report = reports.report(packet["report_id"])
            if all(i["recorded_disposition"] for i in report["items"]):
                break
            await asyncio.sleep(0.1)
        else:
            raise SystemExit("REVIEW_DID_NOT_RESOLVE")
        save(OUT / "review-result.json", report)
        worker.runtime.heartbeat("STOPPED")
        stopped = True
        with worker.store.connect() as conn:
            receipts = conn.execute(
                "SELECT receipt_id FROM lab.jev_receipts ORDER BY event_seq").fetchall()
        with repo.connect() as conn:
            counts = {t: conn.execute(f"SELECT count(*) AS n FROM lab.{t}").fetchone()["n"]
                      for t in ("candidates", "orders", "fills", "risk_decisions")}
        events = repo.export_events()
        rows = []
        for item in report["items"]:
            answers = item.get("answers_json") or {}
            rows.append({
                "symbol": item["symbol"],
                "disposition": item["recorded_disposition"],
                "reason": item.get("reason"),
                **{q: (answers.get(q) or {}).get("choice")
                   for q in ("verdict", "news_stale", "already_priced", "unsupported_inference")},
            })
        summary = {
            "completed_at": datetime.now(UTC),
            "mode": "CLAUDE_AS_MUSE_REAL_JEV_CRYPTO_RESEARCH",
            "model": JEV_MODEL,
            "submitted": len(packet["items"]),
            "dispositions": dict(Counter(r["disposition"] for r in rows)),
            "items": rows,
            "provider_receipts": len(receipts),
            "receipt_verifications": [worker.store.verify(r["receipt_id"]) for r in receipts],
            "metrics": worker.store.metrics("ENGINEERING_TEST"),
            "execution_counts": counts,
            "broker_mutations": 0,
            "audit": verify_events(events),
        }
        save(OUT / "audit.json", events)
        save(OUT / "summary.json", summary)
        print(json.dumps(json_safe({k: v for k, v in summary.items()
                                    if k != "receipt_verifications"}), indent=2), flush=True)
    finally:
        if worker and not stopped:
            worker.runtime.heartbeat("STOPPED")
        localdb.stop(ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    asyncio.run(run(parser.parse_args()))

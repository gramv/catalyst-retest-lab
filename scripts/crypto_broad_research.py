"""Supervised 250-asset screen -> 20 Muse packets -> real Jev research reviews.

No broker transport, admission, risk authorization, executable crypto levels, or
automated re-review. Existing, owner-approved Jev policy is used unchanged.
"""

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from real_world_review_session import PageText, broker_observer_snapshot, save

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import JEV_MODEL
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings

MARKET_URL = "https://api.coingecko.com/api/v3/coins/markets"
PROJECT = Path(__file__).resolve().parents[1]
OUT = PROJECT / "artifacts/crypto-broad-scan-2026-09-19"
ROOT = Path.home() / ".local/share/catalyst-retest-lab/crypto-broad-0919"

# Analyst research priority, fixed before calling Jev. Not a probability forecast.
# Quotes are deliberately short; qualifications preserve what the pages do not prove.
PACKETS = [
    (
        "INJ",
        "injective-protocol",
        "https://injective.com/blog/inj-on-solana",
        "Initial liquidity pools for INJ are live on Raydium",
        "2026-09-17",
        "Live Solana liquidity and INJ-denominated trading expand access to INJ. "
        "This supports a research hypothesis of incremental token utility.",
        "INJ is used as the base asset in the described trading integration. Further lending "
        "integrations are prospective; current demand growth and market surprise are unproven.",
    ),
    (
        "MORPHO",
        "morpho",
        "https://morpho.org/blog/morpho-is-live-on-arc-as-its-credit-infrastructure",
        "Morpho is live on Arc from day one as its credit infrastructure",
        "2026-09-16",
        "Arc deployment adds distribution for Morpho lending products, supporting a hypothesis "
        "of additional protocol usage.",
        "The source describes live credit infrastructure, not a new cash distribution to "
        "MORPHO holders. Earlier Base and Coinbase integrations were already public.",
    ),
    (
        "UNI",
        "uniswap",
        "https://blog.uniswap.org/uniswap-is-live-on-arc",
        "Uniswap v2, v3, v4 and UniswapX are live on Arc",
        "2026-09-16",
        "Deployment on Arc adds a stablecoin-oriented distribution channel for Uniswap. "
        "The research hypothesis is increased venue usage after launch.",
        "Deployment is distinct from UNI-holder value capture. The source does not establish "
        "incremental UNI purchases or that the launch was unexpected.",
    ),
    (
        "SUI",
        "sui",
        "https://www.sui.io/blog/daya-partners-with-sui-for-gasless-stablecoin-payments-in-africa",
        "Daya has integrated Sui as settlement infrastructure",
        "2026-09-17",
        "Daya's settlement integration provides a concrete payments adoption development "
        "for the Sui network.",
        "The product is gasless for businesses: users need not hold the native token. "
        "Expansion beyond the initial market is planned; direct SUI buying is not established.",
    ),
    (
        "ONDO",
        "ondo-finance",
        "https://ondo.finance/blog/ondo-joins-dtcc-fund-serv",
        "Oasis Pro Markets can now seamlessly transact with fund companies, "
        "wealth platforms, and service providers.",
        "2026-09-16",
        "Fund/SERV membership expands institutional distribution capabilities "
        "for an Ondo subsidiary, supporting tokenization adoption research.",
        "An operating-company integration is not evidence of a cash-flow claim for ONDO. "
        "The announcement does not quantify incremental token demand.",
    ),
    (
        "STX",
        "blockstack",
        "https://www.stacks.co/blog/stack-sats-the-1-btc-a-month-defi-rewards-program-on-stacks",
        "These are the pairs that connect Bitcoin and STX to dollar liquidity on Stacks",
        "2026-09-11",
        "The announced trading incentive program includes STX liquidity pairs, a concrete "
        "mechanism that could attract activity to STX markets.",
        "Rewards are BTC, not guaranteed STX purchases. The program was already disclosed; "
        "incentivized turnover is not proof of durable demand after incentives end.",
    ),
    (
        "AAVE",
        "aave",
        "https://www.aave.com/blog/introducing-aave-mcp-server",
        "The official Aave MCP server is live at mcp.aave.com",
        "2026-09-08",
        "Aave's official MCP server lowers integration friction for applications using its "
        "lending protocols, supporting an adoption hypothesis.",
        "The server prepares unsigned transactions and does not itself prove new borrowing "
        "or AAVE-token demand. The launch is a previously disclosed product development.",
    ),
    (
        "ENA",
        "ethena",
        "https://gov.ethenafoundation.com/t/ena-fee-switch-activation/830",
        "If the vote passes, ENA buybacks will begin when the above milestones are reached.",
        "2026-08-27",
        "A governance proposal ties prospective ENA buybacks to explicit milestones, "
        "providing a concrete conditional value-capture hypothesis for research.",
        "ENA is distinct from USDe. ENA fee-switch buybacks are conditional in the supplied "
        "governance proposal, not proven active. "
        "Stablecoin adoption alone does not prove ENA demand.",
    ),
    (
        "AVAX",
        "avalanche-2",
        "https://docs.avax.network/blog/helicon-upgrade",
        "this upgrade brings Continuous Execution to the C-Chain",
        "2026-09-08",
        "Helicon's published performance upgrade is an upcoming network catalyst to monitor; "
        "testnet availability does not establish mainnet activation.",
        "Code-side date comparison places scheduled mainnet activation after this research run. "
        "The schedule was already disclosed; staking changes phase in rather than occur at once.",
    ),
    (
        "ALLO",
        "allora",
        "https://www.allora.network/blog/allora-launches-triple-barrier-forecasts-for-gold-silver-and-oil-on-forge-testnet",
        "Allora has launched triple-barrier forecasts for Gold, Silver, and Oil on Forge testnet",
        "2026-09-15",
        "New commodity forecasting tasks demonstrate a concrete expansion in Allora's "
        "prediction capabilities, making ALLO a small-cap research contender.",
        "This is explicitly testnet, not a paid mainnet rollout or commodity ownership. "
        "Incremental ALLO demand and commercial adoption are not established.",
    ),
    (
        "ATH",
        "aethir",
        "https://aethir.com/blog-posts/aethir-accelerate-will-bring-depin-data-center-capacity-online",
        "Aethir ACCELERATE has secured access to sites totaling up to 20 MW",
        "2026-09-15",
        "Secured site access could expand Aethir's compute supply and is a tangible planning "
        "development for the network.",
        "Access to sites is not energized capacity. Revenue and contract totals are forecasts, "
        "not completed sales; ACCELERATE was already announced earlier.",
    ),
    (
        "NIL",
        "nillion",
        "https://nillion.com/roadmap/",
        "Anyone can operate a node by staking NIL, with rewards following stake.",
        None,
        "Nillion's Dusk roadmap describes a staking-linked application primitive, providing "
        "a potential utility hypothesis for NIL.",
        "The source is a roadmap, not independent proof of mainnet activation or paid usage. "
        "Dusk here is Nillion's phase name, not the separate DUSK token.",
    ),
    (
        "FIL",
        "filecoin",
        "https://github.com/filecoin-project/core-devs/issues/223",
        "A proposed incentive reform that would replace FIL+ with a direct block-reward "
        "split between consensus, Filecoin services, and burn.",
        None,
        "Proposed storage-incentive changes warrant research into FIL utility and supply "
        "effects if adopted and activated.",
        "This is a scope proposal, not proof of completed activation. The latest inspected "
        "Lotus patch is about RPC correctness and security, not evidence that this reform is live.",
    ),
    (
        "AR",
        "arweave",
        "https://github.com/ArweaveTeam/arweave/releases/tag/N.2.9.7-alpha1-dev-sync-performance-20260909",
        "This is a development prerelease containing changes under active development.",
        "2026-09-11",
        "A sync-performance development release provides a technical milestone to examine "
        "alongside AR market activity.",
        "The release is explicitly a prerelease that may not work correctly. It is not proof "
        "of production performance, paid storage growth, or a new unanticipated demand catalyst.",
    ),
    (
        "AIOZ",
        "aioz-network",
        "https://aioz.network/blog/aioz-network-report-august-2026",
        "AIOZ Token is now listed on Upbit",
        "2026-09-01",
        "The project reports expanded exchange access, which could broaden participation "
        "in AIOZ markets.",
        "This is a retrospective report of an earlier listing, not a newly announced listing "
        "today. The packet does not establish fresh demand or a surprise.",
    ),
    (
        "LINK",
        "chainlink",
        "https://chain.link/everything",
        "accumulating LINK tokens using offchain revenue from large enterprises",
        None,
        "The documented reserve links enterprise and service revenues to LINK accumulation, "
        "a direct economic mechanism worth comparing with current market activity.",
        "The mechanism is already public. The page does not establish an unexpected change "
        "in current purchase pace, or that this research pass found a new catalyst.",
    ),
    (
        "HYPE",
        "hyperliquid",
        "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
        "It converts trading fees to HYPE in a fully automated manner as part of the L1 execution.",
        None,
        "The assistance fund's fee conversion supplies a direct HYPE demand mechanism for "
        "research into sustained trading activity.",
        "This is an established fee mechanism. It is not evidence of a fresh increase in "
        "fees or buybacks, and it does not establish a price floor.",
    ),
    (
        "JTO",
        "jito-governance-token",
        "https://forum.jito.network/t/jip-38-confirm-jito-as-a-token-centric-network/973",
        "this JIP commits 100% of the DAO’s JTX revenue share to JTO buybacks and burns",
        "2026-07-13",
        "A governance proposal links a revenue stream to JTO buybacks and burns, a concrete "
        "value-capture hypothesis to investigate.",
        "The topic is an older proposal; recent forum activity is not a new announcement. "
        "This packet does not establish a passed vote or current buyback execution.",
    ),
    (
        "ZEC",
        "zcash",
        "https://zodl.com/ironwood-is-live-on-zcash/",
        "Ironwood NU6.3 activated on Zcash mainnet.",
        "2026-07-28",
        "The activated privacy-pool upgrade supplies a verifiable technical improvement "
        "behind research into renewed ZEC interest.",
        "This is an older activation after a security issue, not a fresh launch. This packet "
        "does not establish a new demand catalyst or unexpected adoption.",
    ),
    (
        "XRP",
        "ripple",
        "https://ripple.com/ripple-press/ripple-treasury-brings-industry-s-first-governed-ai-for-enterprise-treasury/",
        "Today, Ripple announced a major expansion of GSmart",
        "2026-09-10",
        "Ripple's enterprise treasury product expansion warrants research into wider usage "
        "of its financial infrastructure.",
        "Enterprise AI features are not proof of required XRP purchases. Ripple company "
        "adoption and XRP-token demand must remain distinct; the launch was already public.",
    ),
]


async def fetch_source(client, url, excerpt, source_id):
    try:
        async with asyncio.timeout(25):
            response = await client.get(url)
            response.raise_for_status()
    except (TimeoutError, httpx.HTTPError):
        raise ValueError("SOURCE_FETCH_FAILED:" + source_id) from None
    parser = PageText()
    parser.feed(response.text)
    content = re.sub(r"\s+", " ", " ".join(parser.parts))
    if excerpt not in content:
        raise ValueError("EXCERPT_NOT_FOUND:" + source_id)
    return {
        "source": {
            "source_id": source_id,
            "url": url,
            "excerpt": excerpt,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "published_at": None,
        },
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
    }


async def prepare():
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        responses = await asyncio.gather(
            *(fetch_source(client, p[2], p[3], p[0].lower() + "-primary") for p in PACKETS),
            return_exceptions=True,
        )
        errors = [str(r) for r in responses if isinstance(r, ValueError)]
        errors += [
            type(r).__name__
            for r in responses
            if isinstance(r, Exception) and not isinstance(r, ValueError)
        ]
        if errors:
            raise ValueError("SOURCE_VERIFICATION_FAILED:" + ",".join(errors))
        response = await client.get(
            MARKET_URL,
            params={
                "vs_currency": "usd",
                "order": "volume_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) != 250:
            raise ValueError("EXPECTED_250_ASSET_UNIVERSE")
        received = datetime.now(UTC)
    by_id = {r["id"]: r for r in rows}
    items, shortlist, provenance = [], [], []
    for rank, (definition, result) in enumerate(zip(PACKETS, responses, strict=False), 1):
        symbol, coin_id, _, _, pub_date, thesis, economic = definition
        row = by_id.get(coin_id)
        if not row or row["symbol"].upper() != symbol:
            raise ValueError("ASSET_IDENTITY_MISMATCH:" + symbol)
        price, low, high = (Decimal(str(row[k])) for k in ("current_price", "low_24h", "high_24h"))
        if not (0 < low <= high and price > 0):
            raise ValueError("INVALID_RESEARCH_PRICE_RANGE:" + symbol)
        market = {
            k: row[k]
            for k in (
                "id",
                "symbol",
                "current_price",
                "low_24h",
                "high_24h",
                "total_volume",
                "market_cap",
                "price_change_percentage_24h",
                "last_updated",
            )
        }
        market["provider"] = "COINGECKO_AGGREGATED_NOT_EXECUTION_QUOTES"
        market["reference_only"] = True
        market["price_reference_status"] = (
            "AGGREGATE_REFERENCE_ONLY"
            if low <= price <= high
            else "INCONSISTENT_PROVIDER_RANGE_UNUSABLE_FOR_ORDER_PLANNING"
        )
        if not low <= price <= high:
            economic += (
                " Provider current price is outside its reported range. Preserve this "
                "inconsistency; these numbers are unusable for order planning."
            )
        sources = [
            result["source"],
            {
                "source_id": "aggregate-market-reference",
                "url": MARKET_URL,
                "excerpt": json.dumps(market, separators=(",", ":")),
                "retrieved_at": received.isoformat(),
                "published_at": row["last_updated"],
            },
        ]
        levels = {
            "entry_trigger": str(price),
            "max_entry_price": str(price),
            "stop": str(low),
            "target": str(high),
        }
        # Required intake geometry is explicitly reference-only: no made-up 2R target,
        # spread, venue quote, entry timing, or executable crypto strategy is asserted.
        items.append(
            {
                "signal_id": "TEST-CRYPTO-BROAD-20260919-" + symbol,
                "symbol": symbol + "/USD",
                "direction": "LONG",
                "catalyst": "RESEARCH_HYPOTHESIS",
                "thesis": thesis,
                "disproof": "The cited implementation or economic connection is refuted, "
                "withdrawn or remains unsupported. The supplied price range is descriptive "
                "only; no executable crypto strategy or valid entry setup is asserted.",
                "economic_relationship": economic,
                "levels": levels,
                "sources": sources,
            }
        )
        provenance.append({"symbol": symbol, "stated_publication_date": pub_date, **result})
        shortlist.append(
            {
                "muse_priority": rank,
                "symbol": symbol,
                **market,
                "thesis": thesis,
                "qualification": economic,
                "levels": levels,
                "levels_reference_only": True,
                "primary_source": definition[2],
            }
        )
    now = datetime.now(UTC)
    if any(
        (now - datetime.fromisoformat(s["retrieved_at"])).total_seconds() >= 60
        for i in items
        for s in i["sources"]
    ):
        raise ValueError("SOURCES_EXPIRED_BEFORE_INTAKE")
    packet = {
        "submission_id": str(uuid4()),
        "report_id": str(uuid4()),
        "report_key": "TEST-CRYPTO-BROAD-2026-09-19",
        "revision": 1,
        "market": "CRYPTO",
        "timeframe": "BROAD_RESEARCH_ONLY",
        "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(),
        "items": items,
    }
    save(
        OUT / "refreshed-market-universe.json",
        {
            "provider": "COINGECKO",
            "retrieved_at": received,
            "rows": rows,
            "coverage": "250 assets by reported 24h volume; not every listed cryptocurrency",
        },
    )
    save(OUT / "muse-shortlist.json", shortlist)
    save(OUT / "source-provenance.json", provenance)
    selected_ids = {p[1] for p in PACKETS}
    save(
        OUT / "screen-dispositions.json",
        [
            {
                "id": r["id"],
                "symbol": r["symbol"],
                "volume_rank": n,
                "reported_24h_volume": r["total_volume"],
                "reported_change_pct": r["price_change_percentage_24h"],
                "screen": "MUSE_RESEARCH_CONTENDER"
                if r["id"] in selected_ids
                else "NOT_PRIORITIZED_THIS_RESEARCH_PASS",
                "not_a_trading_rejection": True,
            }
            for n, r in enumerate(rows, 1)
        ],
    )
    return packet


async def run(args):
    if args.prepare_only:
        packet = await prepare()
        save(OUT / "prepared-report-not-submitted.json", packet)
        print(
            json.dumps(
                {
                    "source_verification": "passed",
                    "contenders": len(packet["items"]),
                    "provider_calls": 0,
                }
            )
        )
        return
    typesafe_key()  # Existing private .env / secure fallback; value never retained here.
    if ROOT.exists() and not args.resume_before_intake:
        raise ValueError("SESSION_EXISTS_NO_REROLL")
    owner = Path.home() / ".local/share/catalyst-retest-lab/runtime"
    save(OUT / "broker-before.json", broker_observer_snapshot(owner))
    localdb.start(ROOT)  # Separate disposable research ledger; never owner account database.
    worker, stopped = None, False
    try:
        repo = Repository(localdb.connection_url(ROOT))
        repo.check_role()
        gate = Gate1Inputs(APPROVED_GATE1)
        reports = ResearchReports(localdb.connection_url(ROOT, "catalyst_review"), gate)
        with reports.connect() as conn:
            if conn.execute("SELECT count(*) AS n FROM lab.research_reports").fetchone()["n"]:
                raise ValueError("EXISTING_RESEARCH_CANNOT_BE_RERUN")
        worker = ReviewWorker(
            WorkerSettings(
                localdb.connection_url(ROOT, "catalyst_jev"),
                gate,
                "crypto-broad-supervised",
                20,
                0.1,
            )
        )
        packet = await prepare()
        save(OUT / "submitted-report.json", packet)
        with repo.connect() as conn:
            system_event(
                repo,
                conn,
                "MUSE_BROAD_SCREEN",
                {
                    "record_purpose": "ENGINEERING_TEST",
                    "cohort": "JEV_ENGINEERING_TEST",
                    "universe_count": 250,
                    "research_contenders": 20,
                    "requested_final_range": [5, 10],
                    "force_selection_count": False,
                    "ranking": "MUSE_SOURCE_BASED_RESEARCH_PRIORITY_FIXED_BEFORE_JEV",
                    "authorizes_entry": False,
                    "universe_sha256": hashlib.sha256(
                        (OUT / "refreshed-market-universe.json").read_bytes()
                    ).hexdigest(),
                    "report_id": packet["report_id"],
                },
            )
        intake = reports.submit(packet)
        save(OUT / "intake.json", intake)
        if not intake["accepted"]:
            raise ValueError("RESEARCH_INTAKE_REJECTED")
        for _ in range(100):
            await worker.tick()
            report = reports.report(packet["report_id"])
            if all(i["recorded_disposition"] for i in report["items"]):
                break
            await asyncio.sleep(0.1)
        else:
            raise ValueError("REVIEW_DID_NOT_RESOLVE")
        save(OUT / "review-result.json", report)
        selected = [i for i in report["items"] if i["recorded_disposition"] == "SELECTED"]
        final_list = selected[:10]  # Research attention cap only; retain every provider answer.
        save(
            OUT / "final-research-list.json",
            {
                "items": final_list,
                "selected_count": len(selected),
                "target_range": [5, 10],
                "forced_minimum": False,
                "overflow": selected[10:],
                "authorizes_entry": False,
            },
        )
        worker.runtime.heartbeat("STOPPED")
        stopped = True
        with worker.store.connect() as conn:
            receipts = conn.execute(
                "SELECT receipt_id FROM lab.jev_receipts ORDER BY event_seq"
            ).fetchall()
        with repo.connect() as conn:
            counts = {
                t: conn.execute(f"SELECT count(*) AS n FROM lab.{t}").fetchone()["n"]
                for t in ("candidates", "orders", "fills", "risk_decisions")
            }
        events = repo.export_events()
        summary = {
            "completed_at": datetime.now(UTC),
            "model": JEV_MODEL,
            "mode": "REAL_JEV_BROAD_CRYPTO_RESEARCH",
            "universe": 250,
            "contenders": 20,
            "dispositions": dict(Counter(i["recorded_disposition"] for i in report["items"])),
            "final_research_symbols": [i["symbol"] for i in final_list],
            "provider_receipts": len(receipts),
            "receipt_verifications": [worker.store.verify(r["receipt_id"]) for r in receipts],
            "metrics": worker.store.metrics("ENGINEERING_TEST"),
            "execution_counts": counts,
            "broker_mutations": 0,
            "audit": verify_events(events),
        }
        assert summary["audit"]["valid"] and not any(counts.values())
        assert len(receipts) == 20 and all(v["valid"] for v in summary["receipt_verifications"])
        save(OUT / "audit.json", events)
        save(OUT / "summary.json", summary)
        save(OUT / "broker-after.json", broker_observer_snapshot(owner))
        print(
            json.dumps(
                json_safe({k: v for k, v in summary.items() if k != "receipt_verifications"})
            ),
            flush=True,
        )
    finally:
        if worker and not stopped:
            worker.runtime.heartbeat("STOPPED")
        localdb.stop(ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume-before-intake", action="store_true")
    asyncio.run(run(parser.parse_args()))

"""Complete the existing supervised research cycle with five material evidence updates.

Original expired setups stay expired. This is post-expiry research assessment only,
using the existing pinned Jev adapter and policy, in the existing isolated ledger.
No candidate admission, risk grant, broker transport, or model reroll is available.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from crypto_broad_research import OUT as PRIOR_OUT
from crypto_broad_research import PROJECT, ROOT
from real_world_review_session import PageText, broker_observer_snapshot, save

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.execution import system_event
from catalyst_lab.jev_contract import JEV_MODEL
from catalyst_lab.jev_secrets import typesafe_key
from catalyst_lab.repository import Repository, json_safe
from catalyst_lab.research_reports import ResearchReport, ResearchReports
from catalyst_lab.review_config import APPROVED_GATE1, Gate1Inputs
from catalyst_lab.review_worker import ReviewWorker, WorkerSettings

OUT = PROJECT / "artifacts/crypto-complete-cycle-2026-09-19"
SYMBOLS = ("INJ", "UNI", "SUI", "ONDO", "FIL")
EXTRA_SOURCES = {
    "INJ": [
        (
            "prior-cosmos-usdc",
            "https://injective.com/blog/injective-usdc-canonical-standard",
            ("dYdX will adopt USDC.inj as the sole eligible trading collateral on dYdX Chain.",),
        ),
    ],
    "UNI": [
        (
            "arc-fee-proposal",
            "https://gov.uniswap.org/t/temp-check-protocol-fee-expansion-arc/26287",
            (
                "Protocol fees generated on Arc burn UNI on mainnet",
                "five-day Snapshot followed by an onchain vote.",
            ),
        ),
        (
            "prior-burn-policy",
            "https://blog.uniswap.org/unification",
            ("Turn on Uniswap protocol fees and use these fees to burn UNI",),
        ),
    ],
    "SUI": [
        (
            "stablecoin-buyback-mechanism",
            "https://www.sui.io/buybacks",
            (
                "The Sui Foundation converts stablecoin yield to SUI "
                "through daily open-market buybacks.",
                "Total supply is unchanged by the buybacks.",
            ),
        ),
        (
            "prior-gasless-launch",
            "https://www.sui.io/blog/sui-launches-gasless-stablecoin-transfers",
            (
                "supported stablecoins peer-to-peer without paying gas fees "
                "or managing a separate SUI token balance.",
            ),
        ),
    ],
    "ONDO": [
        (
            "token-governance-scope",
            "https://docs.ondo.foundation/ondo-token",
            ("is the governance token for the Ondo DAO and Flux Finance",),
        ),
        (
            "token-economic-rights",
            "https://docs.ondo.foundation/coinlist/coinlist-risk-factors",
            (
                "Neither the T&Cs nor the Tokens will give holders any rights "
                "to any of the profits or revenues of the Company",
            ),
        ),
    ],
    "FIL": [
        (
            "accepted-solstice-spec",
            "https://raw.githubusercontent.com/filecoin-project/FIPs/master/FIPS/fip-0118.md",
            ("status: Accepted", "**Burn stream** pays to the burn actor."),
        ),
        (
            "latest-lotus-patch",
            "https://github.com/filecoin-project/lotus/releases/tag/v1.36.3",
            (
                "Lotus Node v1.36.3 is a recommended patch release "
                "focused on Ethereum RPC correctness",
            ),
        ),
        (
            "prior-acceptance-commit",
            "https://github.com/filecoin-project/FIPs/commit/9fbc58118435bcbbcbdc75959576f8bde0a908ae",
            ("Update fip-0118.md Status to Acceptance",),
        ),
    ],
}

UPDATES = {
    "INJ": (
        "INJ's new Solana distribution has observable trading activity in pools whose token "
        "identity matches the issuer's published mint. The issuer also documents INJ as a "
        "base trading asset. This supports actual additional venue utility, not just a roadmap.",
        "The prior Cosmos disclosure concerns USDC collateral, not the Solana INJ listing. "
        "Observed pool turnover is not unique-user growth, net buying or proof of causal price "
        "impact. Small-pool liquidity and venue price dispersion remain limitations. "
        "Independent Coinbase observations supersede the inconsistent aggregate range as "
        "research references only; the original observation remains retained.",
    ),
    "UNI": (
        "Arc deployment has a newly located, specific proposal to collect protocol fees "
        "and burn canonical UNI. This supplies a concrete conditional token-value mechanism "
        "missing from the original deployment-only packet.",
        "The prior UNIfication policy already proposed fee-funded burns, so the general "
        "mechanism was anticipated. Arc is the specific extension in the new proposal. "
        "The source requires governance votes; it does not prove Arc fee activation, "
        "realized Arc fee revenue or any resulting price increase.",
    ),
    "SUI": (
        "Daya's live integration broadens stablecoin-payment access. The newly retrieved "
        "Sui disclosure describes stablecoin-yield-funded SUI purchases, providing an "
        "indirect token-economic connection even though eligible transfers are gasless.",
        "Gasless transfers were already public. The Foundation reports daily buybacks, "
        "but repurchased tokens are redistributed, not burned. This is not proof that "
        "Daya adds yield-bearing float or increases buybacks. Numerical dashboard totals "
        "are not independently verified here; current-month yield includes estimates.",
    ),
    "ONDO": (
        "The Fund/SERV integration expands the subsidiary's distribution access. The "
        "additional token documentation describes governance, while historical issuer "
        "disclosures deny a token-holder claim on company or platform revenue. The "
        "integration does not establish a direct revenue entitlement for ONDO.",
        "The original operating-company development remains real, but the searched token "
        "documents do not establish a new distribution, buyback or required token purchase "
        "from that development. Historical rights disclosures are identified as historical, "
        "not represented as a newly enacted policy. No profitable long setup is asserted.",
    ),
    "FIL": (
        "The current Solstice specification is Accepted and includes a block-reward burn "
        "stream, advancing the earlier Draft agenda description. This establishes a "
        "governance/specification change relevant to FIL incentives.",
        "Acceptance is distinct from mainnet activation. The inspected latest Lotus release "
        "is a patch for correctness and security, not proof that Solstice is live. The "
        "older agenda already scoped the reform, and the acceptance commit was already "
        "public before this research pass. No activated supply reduction or "
        "incremental paying storage demand is asserted.",
    ),
}


async def source(client, source_id, url, segments):
    async with asyncio.timeout(25):
        response = await client.get(url)
        response.raise_for_status()
    parser = PageText()
    parser.feed(response.text)
    content = re.sub(r"\s+", " ", " ".join(parser.parts))
    if not all(segment in content for segment in segments):
        raise ValueError("EXCERPT_NOT_FOUND:" + source_id)
    result = {
        "source": {
            "source_id": source_id,
            "url": url,
            "excerpt": " [...] ".join(segments),
            "retrieved_at": datetime.now(UTC).isoformat(),
            "published_at": None,
        },
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
    }
    return result, content


async def venue(client, symbol):
    base = "https://api.exchange.coinbase.com/products/" + symbol + "-USD/"
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    ticker, bars = await asyncio.gather(
        client.get(base + "ticker"),
        client.get(
            base + "candles",
            params={
                "granularity": 3600,
                "start": (end - timedelta(hours=72)).isoformat(),
                "end": end.isoformat(),
            },
        ),
    )
    ticker.raise_for_status()
    bars.raise_for_status()
    received = datetime.now(UTC)
    t = ticker.json()
    candles = sorted(
        [
            b
            for b in bars.json()
            if end.timestamp() - 72 * 3600 <= b[0] < end.timestamp() and b[5] > 0
        ],
        key=lambda b: b[0],
    )
    if len(candles) < 24 or any(len(b) != 6 for b in candles):
        raise ValueError("INSUFFICIENT_VENUE_RESEARCH_DATA:" + symbol)
    bid, ask, price = (Decimal(t[k]) for k in ("bid", "ask", "price"))
    if not 0 < bid <= ask or price <= 0:
        raise ValueError("INVALID_VENUE_MARKET_REFERENCE:" + symbol)
    ref = {
        "data_provider": "COINBASE_EXCHANGE",
        "data_feed": "PUBLIC_REST",
        "product_id": symbol + "-USD",
        "last_trade_at": datetime.fromisoformat(t["time"].replace("Z", "+00:00")).isoformat(),
        "last_trade_price": str(price),
        "bid": str(bid),
        "ask": str(ask),
        "bid_ask_timestamp_unavailable": True,
        "spread_bps_computed_by_code": str((ask - bid) / ((ask + bid) / 2) * 10000),
        "completed_hour_bars": len(candles),
        "window_start_epoch": candles[0][0],
        "window_last_completed_epoch": candles[-1][0],
        "window_first_close": str(candles[0][4]),
        "window_last_close": str(candles[-1][4]),
        "window_price_change_pct_computed_by_code": str(
            (Decimal(str(candles[-1][4])) / Decimal(str(candles[0][4])) - 1) * 100
        ),
        "execution_quote": False,
        "proves_causal_news_reaction": False,
    }
    low, high = (min(Decimal(str(b[1])) for b in candles), max(Decimal(str(b[2])) for b in candles))
    levels = {
        "entry_trigger": str(price),
        "max_entry_price": str(max(price, ask)),
        "stop": str(low),
        "target": str(high),
    }
    return {
        "source": {
            "source_id": "independent-venue-observation",
            "url": base + "ticker",
            "excerpt": json.dumps(ref, separators=(",", ":")),
            "retrieved_at": received.isoformat(),
            "published_at": None,
        },
        "references": ref,
        "raw_ticker": t,
        "completed_candles": candles,
        "levels": levels,
        "levels_reference_only": True,
        "candle_endpoint": base + "candles",
    }


async def pool_activity(client, issuer_text):
    identity = re.search(r"Official INJ CA on Solana:\s*([1-9A-HJ-NP-Za-km-z]{32,44})", issuer_text)
    if identity is None:
        raise ValueError("ISSUER_TOKEN_IDENTITY_UNAVAILABLE")
    response = await client.get("https://api.dexscreener.com/latest/dex/tokens/" + identity[1])
    response.raise_for_status()
    pools = [
        p
        for p in response.json().get("pairs", [])
        if p["chainId"] == "solana" and p["baseToken"]["address"] == identity[1]
    ]
    if not pools:
        raise ValueError("NO_ISSUER_MATCHED_POOLS")
    # Wallet/token identifiers are used for local identity verification, never sent to Jev.
    rows = [
        {
            "venue": p["dexId"],
            "price_usd": p.get("priceUsd"),
            "volume_24h_usd": p.get("volume", {}).get("h24"),
            "liquidity_usd": p.get("liquidity", {}).get("usd"),
            "trades_24h": p.get("txns", {}).get("h24"),
            "pool_created_epoch_ms": p.get("pairCreatedAt"),
        }
        for p in pools
    ]
    rows.sort(key=lambda p: Decimal(str(p["volume_24h_usd"] or 0)), reverse=True)
    evidence = {
        "provider": "DEXSCREENER",
        "identity_matched_to_issuer": True,
        "chain": "solana",
        "matched_pool_count": len(rows),
        "largest_pools": rows[:3],
        "turnover_is_not_net_buying_or_unique_users": True,
    }
    return {
        "source": {
            "source_id": "issuer-matched-pool-activity",
            "url": "https://api.dexscreener.com/latest/dex/tokens",
            "excerpt": json.dumps(evidence, separators=(",", ":")),
            "retrieved_at": datetime.now(UTC).isoformat(),
            "published_at": None,
        },
        "sanitized_pools": rows,
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
        "issuer_identity_sha256": hashlib.sha256(identity[1].encode()).hexdigest(),
    }


async def prepare():
    original = json.loads((PRIOR_OUT / "submitted-report.json").read_text())
    old_items = {i["symbol"].split("/")[0]: i for i in original["items"]}
    provenance = {}
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, trust_env=False, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:

        async def item(symbol):
            old = old_items[symbol]
            primary = next(s for s in old["sources"] if s["source_id"].endswith("-primary"))
            segments = (primary["excerpt"],)
            if symbol == "INJ":
                segments += (
                    "Traders exchange INJ for that token, and sell the token back into INJ.",
                )
            tasks = [source(client, primary["source_id"], primary["url"], segments)]
            tasks += [source(client, *definition) for definition in EXTRA_SOURCES[symbol]]
            results = await asyncio.gather(*tasks)
            v = await venue(client, symbol)
            records = [result[0] for result in results] + [v]
            if symbol == "INJ":
                records.append(await pool_activity(client, results[0][1]))
            provenance[symbol] = records
            result = copy.deepcopy(old)
            result.update(
                {
                    "signal_id": "TEST-CRYPTO-CYCLE-FOLLOWUP-20260919-" + symbol,
                    "thesis": UPDATES[symbol][0],
                    "economic_relationship": UPDATES[symbol][1],
                    "disproof": "Unsupported or refuted economic connection, inactive promised "
                    "mechanism, or evidence of full prior anticipation invalidates this research "
                    "case. No causal return forecast or executable crypto setup is asserted.",
                    "levels": v["levels"],
                    "sources": [record["source"] for record in records],
                }
            )
            return result

        items = await asyncio.gather(*(item(symbol) for symbol in SYMBOLS))
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
        "report_key": "TEST-CRYPTO-CYCLE-FOLLOWUP-2026-09-19",
        "revision": 1,
        "market": "CRYPTO",
        "timeframe": "POST_EXPIRY_RESEARCH_ONLY",
        "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(),
        "items": items,
    }
    ResearchReport.model_validate(packet)  # Privacy, finite decimals, source/excerpt bounds.
    save(OUT / "source-provenance.json", provenance)
    return original, packet


async def run(args):
    if args.prepare_only:
        _, packet = await prepare()
        save(OUT / "prepared-not-submitted.json", packet)
        print(json.dumps({"evidence_updates_verified": len(packet["items"]), "provider_calls": 0}))
        return
    typesafe_key()
    owner = ROOT.parent / "runtime"
    save(OUT / "broker-before.json", broker_observer_snapshot(owner))
    prior = json.loads((PRIOR_OUT / "closeout-summary.json").read_text())["audit"]
    localdb.start(ROOT)
    worker, stopped = None, False
    try:
        repo = Repository(localdb.connection_url(ROOT))
        repo.check_role()
        if verify_events(repo.export_events()) != prior:
            raise ValueError("LEDGER_CHANGED_OR_CYCLE_ALREADY_ATTEMPTED")
        gate = Gate1Inputs(APPROVED_GATE1)
        reports = ResearchReports(localdb.connection_url(ROOT, "catalyst_review"), gate)
        original, packet = await prepare()
        original_readback = reports.report(original["report_id"])
        if not all(i["status"] == "EXPIRED" for i in original_readback["items"]):
            raise ValueError("ORIGINAL_RESEARCH_EXPIRY_NOT_ESTABLISHED")
        with repo.connect() as conn:
            system_event(
                repo,
                conn,
                "MUSE_MATERIAL_EVIDENCE_FOLLOWUP",
                {
                    "record_purpose": "ENGINEERING_TEST",
                    "cohort": "JEV_ENGINEERING_TEST",
                    "original_report_id": original["report_id"],
                    "new_report_id": packet["report_id"],
                    "original_deadline": original["valid_until"],
                    "original_remains_expired": True,
                    "authorizes_entry": False,
                    "reason": "OWNER_REQUESTED_COMPLETE_RESEARCH_CYCLE",
                    "additional_evidence": {
                        symbol: [s[0] for s in EXTRA_SOURCES[symbol]] for symbol in SYMBOLS
                    },
                },
            )
        worker = ReviewWorker(
            WorkerSettings(
                localdb.connection_url(ROOT, "catalyst_jev"),
                gate,
                "crypto-broad-cycle-followup",
                5,
                0.1,
            )
        )
        save(OUT / "submitted-report.json", packet)
        intake = reports.submit(packet)
        save(OUT / "intake.json", intake)
        if not intake["accepted"]:
            raise ValueError("FOLLOWUP_INTAKE_REJECTED")
        for _ in range(100):
            await worker.tick()
            report = reports.report(packet["report_id"])
            if all(i["recorded_disposition"] for i in report["items"]):
                break
            await asyncio.sleep(0.1)
        else:
            raise ValueError("FOLLOWUP_DID_NOT_RESOLVE")
        save(OUT / "review-result.json", report)
        final = []
        for item in report["items"]:
            approved = item["recorded_disposition"] == "SELECTED"
            # Muse can withhold agreement; an ONDO issuer integration still supplies no
            # demonstrated token-demand mechanism in this researched case.
            muse_agrees = approved and item["symbol"] != "ONDO/USD"
            decision = {
                "symbol": item["symbol"],
                "item_id": item["item_id"],
                "receipt_id": item["receipt_id"],
                "jev_disposition": item["recorded_disposition"],
                "jev_reason": item["reason"],
                "muse_agrees": muse_agrees,
                "cycle_disposition": "CLOSED_RESEARCH_SELECTED"
                if muse_agrees
                else "CLOSED_NO_SELECTION",
                "execution_blocker": "CRYPTO_EXECUTION_NOT_IMPLEMENTED",
                "original_report_remains_expired": True,
                "authorizes_entry": False,
                "no_pending_owner_review": True,
                "record_purpose": "ENGINEERING_TEST",
                "cohort": "JEV_ENGINEERING_TEST",
            }
            with repo.connect() as conn:
                system_event(repo, conn, "MUSE_RESEARCH_CYCLE_DISPOSITION", decision)
            final.append(decision)
        with repo.connect() as conn:
            system_event(
                repo,
                conn,
                "MUSE_RESEARCH_CYCLE_COMPLETED",
                {
                    "original_report_id": original["report_id"],
                    "followup_report_id": packet["report_id"],
                    "original_contenders": 20,
                    "previously_closed_without_selection": 15,
                    "followup_assessments": 5,
                    "remaining_pending_research": 0,
                    "research_selected": [i["symbol"] for i in final if i["muse_agrees"]],
                    "authorizes_entry": False,
                    "broker_mutations": 0,
                    "record_purpose": "ENGINEERING_TEST",
                    "cohort": "JEV_ENGINEERING_TEST",
                },
            )
        worker.runtime.heartbeat("STOPPED")
        stopped = True
        with worker.store.connect() as conn:
            receipts = conn.execute(
                "SELECT receipt_id FROM lab.jev_receipts WHERE event_seq>%s ORDER BY event_seq",
                (prior["event_count"],),
            ).fetchall()
            latencies = conn.execute(
                "SELECT latency_ms FROM lab.jev_receipts WHERE event_seq>%s ORDER BY latency_ms",
                (prior["event_count"],),
            ).fetchall()
        with repo.connect() as conn:
            counts = {
                t: conn.execute(f"SELECT count(*) AS n FROM lab.{t}").fetchone()["n"]
                for t in ("candidates", "orders", "fills", "risk_decisions")
            }
        events = repo.export_events()
        summary = {
            "completed_at": datetime.now(UTC),
            "cycle_complete": True,
            "model": JEV_MODEL,
            "additional_provider_receipts": len(receipts),
            "cycle_total_provider_receipts": 20 + len(receipts),
            "followup_dispositions": dict(Counter(i["jev_disposition"] for i in final)),
            "final_decisions": final,
            "receipt_verifications": [worker.store.verify(r["receipt_id"]) for r in receipts],
            "latency_ms": [r["latency_ms"] for r in latencies],
            "execution_counts": counts,
            "previous_audit": prior,
            "audit": verify_events(events),
            "broker_mutations": 0,
            "original_report_remains_expired": True,
            "pending_owner_review": False,
        }
        assert summary["audit"]["valid"] and not any(counts.values())
        assert len(receipts) == 5 and all(v["valid"] for v in summary["receipt_verifications"])
        save(OUT / "original-expired-readback.json", original_readback)
        save(OUT / "cycle-dispositions.json", final)
        save(OUT / "audit.json", events)
        save(OUT / "summary.json", summary)
        save(OUT / "broker-after.json", broker_observer_snapshot(owner))
        print(
            json.dumps(
                json_safe(
                    {
                        k: v
                        for k, v in summary.items()
                        if k not in {"receipt_verifications", "final_decisions"}
                    }
                )
            ),
            flush=True,
        )
        print(
            json.dumps(
                [
                    {
                        "symbol": i["symbol"],
                        "jev": i["jev_disposition"],
                        "final": i["cycle_disposition"],
                    }
                    for i in final
                ]
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
    asyncio.run(run(parser.parse_args()))

"""Muse v2.1: finish the v2 evidence test for assets v2 could not test fairly.

Scope: only assets that v2 did not validly test, so nothing is re-voted on unchanged
evidence (Muse guidelines):
- LINK, AAVE, FIL were dropped by v2's price-range check and never reached Jev.
- UNI, BCH reached Jev with key excerpts missing (blocked sites, one unmatched
  Federal Register excerpt). They are resubmitted with that evidence restored,
  which is a material change. v2 results remain on record.
Controls (researched without being told what result to expect; the evidence shows
little that is new): FIL (vesting disclosed 2020) and BCH (regulated BCH futures since 2024).

Harness fixes versus v2 (scripts/muse_crypto_test_v2.py):
1. Price outside CoinGecko's own 24h range no longer drops the asset; reference
   levels use min/max and the inconsistency is disclosed in the price excerpt.
2. BTC benchmark: the price excerpt reports BTC over the same windows and the
   asset's excess move, so market-wide rallies are not credited to the news.
3. Price history retries with backoff on rate limits.
4. Each excerpt may list fallback URLs carrying the same text; the first one
   verified word for word at run time is used and recorded in provenance.

Unchanged: separate disposable ledger, owner-approved Gate1 policy, no broker
client, no admission, no risk authorization, no orders.

Usage (from the project root):
    ./run python scripts/muse_crypto_test_v21.py --prepare-only   # no Jev calls
    ./run python scripts/muse_crypto_test_v21.py                  # real Jev review
"""

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
PACKETS = json.loads(Path(__file__).with_name("muse_crypto_v21_packets.json").read_text())
OUT = PROJECT / "artifacts/muse-crypto-test-v2.1-2026-09-23"
PRIOR = {"v1": PROJECT / "artifacts/muse-crypto-test-2026-09-22/summary.json",
         "v2": PROJECT / "artifacts/muse-crypto-test-v2-2026-09-22/summary.json"}
ROOT = Path.home() / ".local/share/catalyst-retest-lab/muse-0923"  # fresh ledger
MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
CHART_URL = "https://api.coingecko.com/api/v3/coins/{}/market_chart"
BENCHMARK = "bitcoin"
CONTROLS = {"FIL", "BCH"}
QUESTIONS = ("verdict", "news_stale", "already_priced", "unsupported_inference")
QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                        " ": " "})


def canon(text):
    """Comparison form only: quote style and whitespace (incl. tag-boundary spaces)."""
    return re.sub(r"\s+", "", text.translate(QUOTES))


def page_text(html):
    parser = PageText()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts))


async def fetch_pages(client, urls):
    async def one(url):
        try:
            async with asyncio.timeout(25):
                response = await client.get(url)
                response.raise_for_status()
            return url, {"text": page_text(response.text),
                         "sha256": hashlib.sha256(response.content).hexdigest(),
                         "at": datetime.now(UTC)}
        except (TimeoutError, httpx.HTTPError) as exc:
            detail = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                detail += ":" + str(exc.response.status_code)
            return url, {"error": "FETCH_FAILED:" + detail}
    return dict(await asyncio.gather(*(one(u) for u in urls)))


def price_history(client, coin_id):
    """Pre-intake, not time-critical. Backs off on rate limits and transient errors."""
    last = None
    for wait in (0, 20, 40, 60):
        time.sleep(wait)
        try:
            response = client.get(CHART_URL.format(coin_id),
                                  params={"vs_currency": "usd", "days": 30})
            response.raise_for_status()
            return response.json()["prices"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last = exc
    raise RuntimeError("HISTORY_UNAVAILABLE:" + type(last).__name__)


def price_at(points, moment):
    ms = moment.timestamp() * 1000
    earlier = [p for p in points if p[0] <= ms]
    if not earlier:
        return None
    stamp, value = earlier[-1]
    return datetime.fromtimestamp(stamp / 1000, UTC), Decimal(str(value))


def change(new, old):
    return (new - old) / old * 100


def q2(value):
    return str(value.quantize(Decimal("0.01")))


def collect_histories():
    histories = {}
    with httpx.Client(timeout=20, trust_env=False,
                      headers={"User-Agent": "Mozilla/5.0"}) as client:
        for coin_id in [BENCHMARK] + [p["coingecko_id"] for p in PACKETS]:
            try:
                histories[coin_id] = {"points": price_history(client, coin_id),
                                      "retrieved_at": datetime.now(UTC)}
            except RuntimeError as exc:
                histories[coin_id] = {"error": str(exc)}
            time.sleep(6)  # CoinGecko public rate limit
    return histories


def windows(history, current, day_start):
    """Price change 7d before announcement and since announcement-day start."""
    if "points" not in history:
        return None
    before = price_at(history["points"], day_start)
    week = price_at(history["points"], day_start - timedelta(days=7))
    if not (before and week):
        return None
    return {"week_at": week[0], "week": week[1], "start_at": before[0], "start": before[1],
            "run_up": change(before[1], week[1]), "since": change(current, before[1])}


def price_excerpt(packet, histories, rows, received):
    announced = date.fromisoformat(packet["announcement_date"])
    day_start = datetime(announced.year, announced.month, announced.day, tzinfo=UTC)
    row, btc_row = rows[packet["coingecko_id"]], rows[BENCHMARK]
    current = Decimal(str(row["current_price"]))
    low, high = Decimal(str(row["low_24h"])), Decimal(str(row["high_24h"]))
    facts = {
        "kind": "CODE_COMPUTED_PRICE_FACTS",
        "provider": "COINGECKO_AGGREGATED_NOT_EXECUTION_QUOTES",
        "announcement_date_utc": packet["announcement_date"],
        "current": {"price": str(current), "low_24h": str(low), "high_24h": str(high),
                    "as_of": row["last_updated"]},
        "levels_note": "Item levels are 24h low/current/high reference only; "
                       "no executable entry or 2R setup is asserted.",
    }
    if not low <= current <= high:
        facts["provider_range_inconsistent"] = True
    asset = windows(histories[packet["coingecko_id"]], current, day_start)
    btc = windows(histories[BENCHMARK], Decimal(str(btc_row["current_price"])), day_start)
    if asset:
        facts["reference"] = {
            "seven_days_before_announcement": {"at": asset["week_at"].isoformat(),
                                               "price": str(asset["week"])},
            "announcement_day_start": {"at": asset["start_at"].isoformat(),
                                       "price": str(asset["start"])},
        }
        facts["change_pct"] = {"run_up_7d_before_announcement": q2(asset["run_up"]),
                               "announcement_day_start_to_now": q2(asset["since"])}
        if btc:
            facts["btc_benchmark_change_pct"] = {
                "run_up_7d_before_announcement": q2(btc["run_up"]),
                "announcement_day_start_to_now": q2(btc["since"])}
            facts["excess_vs_btc_pct_points"] = {
                "run_up_7d_before_announcement": q2(asset["run_up"] - btc["run_up"]),
                "announcement_day_start_to_now": q2(asset["since"] - btc["since"])}
        else:
            facts["btc_benchmark"] = "UNAVAILABLE"
    else:
        facts["history"] = "UNAVAILABLE_NO_PRICED_IN_CALCULATION"
    source = {"source_id": packet["symbol"].lower() + "-price-facts", "url": MARKETS_URL,
              "excerpt": json.dumps(facts, separators=(",", ":")),
              "retrieved_at": received.isoformat(), "published_at": None}
    return source, facts, (min(low, current), max(high, current))


async def prepare(histories):
    dropped, omitted, provenance, items = [], {}, [], []
    urls = sorted({c["url"] for p in PACKETS for s in p["sources"] for c in s["candidates"]})
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        pages = await fetch_pages(client, urls)
        response = await client.get(MARKETS_URL, params={
            "vs_currency": "usd",
            "ids": ",".join([BENCHMARK] + [p["coingecko_id"] for p in PACKETS])})
        response.raise_for_status()
        rows = {r["id"]: r for r in response.json()}
        received = datetime.now(UTC)
    if BENCHMARK not in rows:
        raise SystemExit("BENCHMARK_PRICE_UNAVAILABLE")
    for packet in PACKETS:
        symbol, verified, failed = packet["symbol"], [], []
        for source in packet["sources"]:
            attempts = []
            for candidate in source["candidates"]:
                page = pages[candidate["url"]]
                if "error" in page:
                    attempts.append({"url": candidate["url"], "failure": page["error"]})
                elif canon(candidate["excerpt"]) not in canon(page["text"]):
                    attempts.append({"url": candidate["url"], "failure": "EXCERPT_NOT_FOUND"})
                else:
                    verified.append({
                        "source": {"source_id": source["source_id"], "url": candidate["url"],
                                   "excerpt": candidate["excerpt"],
                                   "retrieved_at": page["at"].isoformat(),
                                   "published_at": None},
                        "role": source["role"], "response_sha256": page["sha256"],
                        "fallbacks_tried_first": attempts})
                    break
            else:
                failed.append({"source_id": source["source_id"], "attempts": attempts})
        if not any(v["role"] == "new-fact" for v in verified):
            dropped.append({"symbol": symbol, "stage": "SOURCE", "failures": failed})
            continue
        thesis = packet["thesis"]
        if failed:
            note = (" [Muse note: " + ", ".join(f["source_id"] for f in failed)
                    + " could not be re-verified at run time and is omitted.]")
            if len(thesis + note) > 1000:
                dropped.append({"symbol": symbol, "stage": "SOURCE_NOTE_TOO_LONG",
                                "failures": failed})
                continue
            thesis += note
            omitted[symbol] = failed
        row = rows.get(packet["coingecko_id"])
        if not row or row.get("symbol", "").upper() != symbol:
            dropped.append({"symbol": symbol, "stage": "PRICE", "detail": "ASSET_IDENTITY"})
            continue
        try:
            price = Decimal(str(row["current_price"]))
            source, facts, (stop, target) = price_excerpt(packet, histories, rows, received)
        except (KeyError, TypeError, ArithmeticError):
            dropped.append({"symbol": symbol, "stage": "PRICE", "detail": "PRICE_MISSING"})
            continue
        item = {
            "signal_id": "TEST-CRYPTO-MUSE-V21-20260923-" + symbol,
            "symbol": symbol + "/USD", "direction": "LONG", "catalyst": packet["catalyst"],
            "thesis": thesis, "disproof": packet["disproof"],
            "economic_relationship": packet["economic_relationship"],
            "levels": {"entry_trigger": str(price), "max_entry_price": str(price),
                       "stop": str(stop), "target": str(target)},
            "sources": [v["source"] for v in verified] + [source],
        }
        try:
            ResearchItem.model_validate(item)
        except Exception as exc:  # never echo field contents
            dropped.append({"symbol": symbol, "stage": "CONTRACT", "detail": type(exc).__name__})
            continue
        items.append(item)
        provenance.append({
            "symbol": symbol, "control": symbol in CONTROLS,
            "verified": [{"source_id": v["source"]["source_id"], "url": v["source"]["url"],
                          "role": v["role"], "response_sha256": v["response_sha256"],
                          "fallbacks_tried_first": v["fallbacks_tried_first"]}
                         for v in verified],
            "omitted": failed, "price_facts": facts})
    if not items:
        raise SystemExit("NO_VALID_ITEMS: " + json.dumps(dropped))
    now = datetime.now(UTC)
    oldest = min(datetime.fromisoformat(s["retrieved_at"]) for i in items for s in i["sources"])
    if (now - oldest).total_seconds() >= 55:
        raise SystemExit("SOURCES_TOO_OLD_BEFORE_INTAKE_RETRY")
    report = {
        "submission_id": str(uuid4()), "report_id": str(uuid4()),
        "report_key": "TEST-CRYPTO-MUSE-V21-2026-09-23", "revision": 1, "market": "CRYPTO",
        "timeframe": "MUSE_RESEARCH_TEST_V21", "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(), "items": items,
    }
    screening = {"submitted": [i["symbol"] for i in items], "controls": sorted(CONTROLS),
                 "dropped": dropped, "sources_omitted": omitted,
                 "history_errors": {k: v["error"] for k, v in histories.items() if "error" in v}}
    return report, provenance, screening


def comparison(rows):
    earlier = {}
    for name, path in PRIOR.items():
        try:
            earlier[name] = {r["symbol"]: r for r in json.loads(path.read_text())["items"]}
        except (OSError, ValueError, KeyError):
            earlier[name] = {}
    keys = ("disposition",) + QUESTIONS
    return [{"symbol": r["symbol"], "control": r["symbol"].split("/")[0] in CONTROLS,
             **{name: ({k: runs[r["symbol"]].get(k) for k in keys}
                       if r["symbol"] in runs else "NOT_SUBMITTED")
                for name, runs in earlier.items()},
             "v2.1": {k: r[k] for k in keys}} for r in rows]


async def run(args):
    OUT.mkdir(parents=True, exist_ok=True)
    print("Collecting 30-day price history incl. BTC benchmark (about 1-2 minutes)...",
          flush=True)
    histories = collect_histories()
    if args.prepare_only:
        report, provenance, screening = await prepare(histories)
        save(OUT / "prepared-not-submitted.json", report)
        save(OUT / "source-provenance.json", provenance)
        save(OUT / "screening.json", screening)
        print(json.dumps({
            "mode": "PREPARE_ONLY", "jev_calls": 0,
            "would_submit": screening["submitted"], "dropped": screening["dropped"],
            "sources_omitted": {k: [f["source_id"] for f in v]
                                for k, v in screening["sources_omitted"].items()},
            "fallback_urls_used": {p["symbol"]: [v["source_id"] for v in p["verified"]
                                                 if v["fallbacks_tried_first"]]
                                   for p in provenance},
            "history_errors": screening["history_errors"],
            "price_facts": {p["symbol"]: {
                "asset": p["price_facts"].get("change_pct", "UNAVAILABLE"),
                "excess_vs_btc": p["price_facts"].get("excess_vs_btc_pct_points",
                                                      "UNAVAILABLE")}
                for p in provenance}}, indent=2))
        return
    typesafe_key()
    localdb.start(ROOT)
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
            localdb.connection_url(ROOT, "catalyst_jev"), gate, "muse-crypto-0923", 20, 0.1))
        report, provenance, screening = await prepare(histories)
        save(OUT / "submitted-report.json", report)
        save(OUT / "source-provenance.json", provenance)
        save(OUT / "screening.json", screening)
        intake = reports.submit(report)
        save(OUT / "intake.json", intake)
        if not intake.get("accepted"):
            raise SystemExit("INTAKE_REJECTED: " + json.dumps(json_safe(intake)))
        for _ in range(200):
            await worker.tick()
            result = reports.report(report["report_id"])
            if all(i["recorded_disposition"] for i in result["items"]):
                break
            await asyncio.sleep(0.1)
        else:
            raise SystemExit("REVIEW_DID_NOT_RESOLVE")
        save(OUT / "review-result.json", result)
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
        for item in result["items"]:
            answers = item.get("answers_json") or {}
            rows.append({"symbol": item["symbol"], "disposition": item["recorded_disposition"],
                         "reason": item.get("reason"),
                         **{q: (answers.get(q) or {}).get("choice") for q in QUESTIONS},
                         "verdict_probabilities": (answers.get("verdict") or {}).get(
                             "probabilities")})
        summary = {
            "completed_at": datetime.now(UTC),
            "mode": "CLAUDE_AS_MUSE_V21_EVIDENCE_COMPLETE_REAL_JEV",
            "model": JEV_MODEL, "submitted": len(report["items"]),
            "dispositions": dict(Counter(r["disposition"] for r in rows)),
            "items": rows, "runs_compared": comparison(rows),
            "screening": screening,
            "provider_receipts": len(receipts),
            "receipt_verifications": [worker.store.verify(r["receipt_id"]) for r in receipts],
            "metrics": worker.store.metrics("ENGINEERING_TEST"),
            "execution_counts": counts, "broker_mutations": 0,
            "audit": verify_events(events),
        }
        save(OUT / "audit.json", events)
        save(OUT / "summary.json", summary)
        print(json.dumps(json_safe({k: summary[k] for k in (
            "mode", "submitted", "dispositions", "items", "execution_counts", "audit")}),
            indent=2), flush=True)
    finally:
        if worker and not stopped:
            worker.runtime.heartbeat("STOPPED")
        localdb.stop(ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    asyncio.run(run(parser.parse_args()))

"""Muse v2: evidence-complete crypto packets -> real Jev SKEPTIC reviews (2026-09-22).

What changed from v1 (scripts/muse_crypto_test.py) and why:
- v1 gave Jev one short excerpt per asset plus unsourced asides ("likely priced",
  "mechanism is background knowledge"). Jev sees only {source_id, excerpt} pairs
  (no URLs, no dates), so it correctly marked those claims unsupported.
- v2 gives each asset up to 7 quoted excerpts: new fact (with dateline where
  available), prior disclosures, the token mechanism from project documents, and
  sourced adverse facts. It adds one code-computed price excerpt (7-day run-up
  before the announcement and move since) so "already priced" is judged on data.
- Excerpts are re-fetched and checked at run time. A source that fails is omitted
  and the thesis says so; an asset with no verified new-fact source is dropped.
- FIL, SKY and BCH were researched without being told what result to expect, and
  the evidence shows little that is new. They act as controls: if Jev approves them,
  it is responding to text volume, not evidence.

Same guarantees as v1: separate disposable ledger, owner-approved Gate1 policy
unchanged, no broker client, no admission, no risk authorization, no orders.

Usage (from the project root):
    ./run python scripts/muse_crypto_test_v2.py --prepare-only   # no Jev calls
    ./run python scripts/muse_crypto_test_v2.py                  # real Jev review
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
PACKETS = json.loads((Path(__file__).with_name("muse_crypto_v2_packets.json")).read_text())
OUT = PROJECT / "artifacts/muse-crypto-test-v2-2026-09-22"
V1_SUMMARY = PROJECT / "artifacts/muse-crypto-test-2026-09-22/summary.json"
ROOT = Path.home() / ".local/share/catalyst-retest-lab/muse-0922b"  # fresh ledger, no re-vote
MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
CHART_URL = "https://api.coingecko.com/api/v3/coins/{}/market_chart"
CONTROLS = {"FIL", "SKY", "BCH"}
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
            return url, {"error": "FETCH_FAILED:" + type(exc).__name__}
    return dict(await asyncio.gather(*(one(u) for u in urls)))


def price_history(client, coin_id):
    """Pre-intake, not time-critical: 30 days of CoinGecko points for one asset."""
    for attempt in range(2):
        response = client.get(CHART_URL.format(coin_id),
                              params={"vs_currency": "usd", "days": 30})
        if response.status_code == 429 and attempt == 0:
            time.sleep(30)
            continue
        response.raise_for_status()
        return response.json()["prices"]
    raise RuntimeError("RATE_LIMITED")


def price_at(points, moment):
    ms = moment.timestamp() * 1000
    earlier = [p for p in points if p[0] <= ms]
    if not earlier:
        return None
    stamp, value = earlier[-1]
    return datetime.fromtimestamp(stamp / 1000, UTC), Decimal(str(value))


def pct(new, old):
    return str(((new - old) / old * 100).quantize(Decimal("0.01")))


def collect_histories():
    histories = {}
    with httpx.Client(timeout=20, trust_env=False,
                      headers={"User-Agent": "Mozilla/5.0"}) as client:
        for packet in PACKETS:
            try:
                histories[packet["symbol"]] = {"points": price_history(client,
                                                                       packet["coingecko_id"]),
                                               "retrieved_at": datetime.now(UTC)}
            except Exception as exc:
                histories[packet["symbol"]] = {"error": type(exc).__name__}
            time.sleep(4)  # CoinGecko public rate limit
    return histories


def price_excerpt(packet, history, row, received):
    announced = date.fromisoformat(packet["announcement_date"])
    day_start = datetime(announced.year, announced.month, announced.day, tzinfo=UTC)
    current = Decimal(str(row["current_price"]))
    facts = {
        "kind": "CODE_COMPUTED_PRICE_FACTS",
        "provider": "COINGECKO_AGGREGATED_NOT_EXECUTION_QUOTES",
        "announcement_date_utc": packet["announcement_date"],
        "current": {"price": str(current), "low_24h": str(row["low_24h"]),
                    "high_24h": str(row["high_24h"]), "as_of": row["last_updated"]},
        "levels_note": "Item levels are 24h low/current/high reference only; "
                       "no executable entry or 2R setup is asserted.",
    }
    if "points" in history:
        before = price_at(history["points"], day_start)
        week = price_at(history["points"], day_start - timedelta(days=7))
        if before and week:
            facts["reference"] = {
                "seven_days_before_announcement": {"at": week[0].isoformat(),
                                                   "price": str(week[1])},
                "announcement_day_start": {"at": before[0].isoformat(),
                                           "price": str(before[1])},
            }
            facts["change_pct"] = {
                "run_up_7d_before_announcement": pct(before[1], week[1]),
                "announcement_day_start_to_now": pct(current, before[1]),
            }
            facts["history_retrieved_at"] = history["retrieved_at"].isoformat()
    if "change_pct" not in facts:
        facts["history"] = "UNAVAILABLE_NO_PRICED_IN_CALCULATION"
    return {"source_id": packet["symbol"].lower() + "-price-facts", "url": MARKETS_URL,
            "excerpt": json.dumps(facts, separators=(",", ":")),
            "retrieved_at": received.isoformat(), "published_at": None}


async def prepare(histories):
    dropped, omitted, provenance, items = [], {}, [], []
    urls = sorted({s["url"] for p in PACKETS for s in p["sources"]})
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        pages = await fetch_pages(client, urls)
        response = await client.get(MARKETS_URL, params={
            "vs_currency": "usd", "ids": ",".join(p["coingecko_id"] for p in PACKETS)})
        response.raise_for_status()
        rows = {r["id"]: r for r in response.json()}
        received = datetime.now(UTC)
    for packet in PACKETS:
        symbol, verified, failed = packet["symbol"], [], []
        for source in packet["sources"]:
            page = pages[source["url"]]
            if "error" in page:
                failed.append({"source_id": source["source_id"], "failure": page["error"]})
            elif canon(source["excerpt"]) not in canon(page["text"]):
                failed.append({"source_id": source["source_id"], "failure": "EXCERPT_NOT_FOUND"})
            else:
                verified.append({"source": {"source_id": source["source_id"],
                                            "url": source["url"],
                                            "excerpt": source["excerpt"],
                                            "retrieved_at": page["at"].isoformat(),
                                            "published_at": None},
                                 "role": source["role"], "response_sha256": page["sha256"]})
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
            low, price, high = (Decimal(str(row[k])) for k in
                                ("low_24h", "current_price", "high_24h"))
        except (KeyError, TypeError, ArithmeticError):
            dropped.append({"symbol": symbol, "stage": "PRICE", "detail": "PRICE_MISSING"})
            continue
        if not 0 < low <= price <= high:
            dropped.append({"symbol": symbol, "stage": "PRICE", "detail": "INCONSISTENT_RANGE"})
            continue
        item = {
            "signal_id": "TEST-CRYPTO-MUSE-V2-20260922-" + symbol,
            "symbol": symbol + "/USD",
            "direction": "LONG",
            "catalyst": packet["catalyst"],
            "thesis": thesis,
            "disproof": packet["disproof"],
            "economic_relationship": packet["economic_relationship"],
            "levels": {"entry_trigger": str(price), "max_entry_price": str(price),
                       "stop": str(low), "target": str(high)},
            "sources": [v["source"] for v in verified]
            + [price_excerpt(packet, histories[symbol], row, received)],
        }
        try:
            ResearchItem.model_validate(item)
        except Exception as exc:  # never echo field contents
            dropped.append({"symbol": symbol, "stage": "CONTRACT", "detail": type(exc).__name__})
            continue
        items.append(item)
        provenance.append({"symbol": symbol, "control": symbol in CONTROLS,
                           "verified": [{k: v[k] for k in ("role", "response_sha256")}
                                        | {"source_id": v["source"]["source_id"],
                                           "url": v["source"]["url"]} for v in verified],
                           "omitted": failed,
                           "price_facts": json.loads(item["sources"][-1]["excerpt"])})
    if not items:
        raise SystemExit("NO_VALID_ITEMS: " + json.dumps(dropped))
    now = datetime.now(UTC)
    oldest = min(datetime.fromisoformat(s["retrieved_at"]) for i in items for s in i["sources"])
    if (now - oldest).total_seconds() >= 55:
        raise SystemExit("SOURCES_TOO_OLD_BEFORE_INTAKE_RETRY")
    report = {
        "submission_id": str(uuid4()), "report_id": str(uuid4()),
        "report_key": "TEST-CRYPTO-MUSE-V2-2026-09-22", "revision": 1, "market": "CRYPTO",
        "timeframe": "MUSE_RESEARCH_TEST_V2", "generated_at": now.isoformat(),
        "valid_until": (now + timedelta(seconds=60)).isoformat(), "items": items,
    }
    screening = {"submitted": [i["symbol"] for i in items], "controls": sorted(CONTROLS),
                 "dropped": dropped, "sources_omitted": omitted}
    return report, provenance, screening


def comparison(rows):
    try:
        v1 = {r["symbol"]: r for r in json.loads(V1_SUMMARY.read_text())["items"]}
    except (OSError, ValueError, KeyError):
        v1 = {}
    return [{"symbol": r["symbol"], "control": r["symbol"].split("/")[0] in CONTROLS,
             "v1": {q: v1[r["symbol"]].get(q) for q in ("disposition",) + QUESTIONS}
             if r["symbol"] in v1 else "NOT_IN_V1",
             "v2": {q: r[q] for q in ("disposition",) + QUESTIONS}} for r in rows]


async def run(args):
    OUT.mkdir(parents=True, exist_ok=True)
    print("Collecting 30-day price history (rate-limited, about 1 minute)...", flush=True)
    histories = collect_histories()
    if args.prepare_only:
        report, provenance, screening = await prepare(histories)
        save(OUT / "prepared-not-submitted.json", report)
        save(OUT / "source-provenance.json", provenance)
        save(OUT / "screening.json", screening)
        print(json.dumps({"mode": "PREPARE_ONLY", "jev_calls": 0,
                          "would_submit": screening["submitted"],
                          "dropped": screening["dropped"],
                          "sources_omitted": screening["sources_omitted"],
                          "price_facts": {p["symbol"]: p["price_facts"].get(
                              "change_pct", "UNAVAILABLE") for p in provenance}},
                         indent=2))
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
            localdb.connection_url(ROOT, "catalyst_jev"), gate, "muse-crypto-0922b", 20, 0.1))
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
            "mode": "CLAUDE_AS_MUSE_V2_EVIDENCE_COMPLETE_REAL_JEV",
            "model": JEV_MODEL,
            "submitted": len(report["items"]),
            "dispositions": dict(Counter(r["disposition"] for r in rows)),
            "items": rows,
            "v1_vs_v2": comparison(rows),
            "provider_receipts": len(receipts),
            "receipt_verifications": [worker.store.verify(r["receipt_id"]) for r in receipts],
            "metrics": worker.store.metrics("ENGINEERING_TEST"),
            "execution_counts": counts,
            "broker_mutations": 0,
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

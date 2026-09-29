"""Production-sized, clearly synthetic management-review inputs (fixtures only).

Sized like a real open position under the runtime's bar window: 64 completed one-minute
bars passed as both the recent and the structural bars, near-maximum research text,
six news items at the 1,200-character excerpt limit (two adverse), a full rationale,
eight management-history events and five stop plus five target options. Used by
tests/test_managed_dossier.py and by scripts/prove_managed_jev.py; no real market data.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from uuid import NAMESPACE_URL, uuid5

from catalyst_lab.jev_contract import digest
from catalyst_lab.managed_review import (
    CONTEXT_VERSION,
    EXIT_POLICY,
    MANAGED_COHORT,
    STATE_BYTE_BUDGET,
    CompletedBar,
    ManagedPolicy,
    NewsEvidence,
    PositionSnapshot,
    ProtectiveOrder,
)

NOW = datetime(2026, 9, 18, 16, 0, 30, tzinfo=UTC)
PROVIDER, FEED = "ALPACA", "iex"
V2_PRODUCTION_POLICY = ManagedPolicy(5, 60, 5, 20, 4)  # The retired V2 runtime profile.
V3_PRODUCTION_POLICY = ManagedPolicy(
    5, 60, 5, 20, 4, max_structural_bars=64, max_history=4,
    context_version=CONTEXT_VERSION, state_byte_budget=STATE_BYTE_BUDGET,
)
LABEL = "SYNTHETIC ENGINEERING FIXTURE, NOT A REAL OPPORTUNITY. "


def ident(name, seed=0):
    return str(uuid5(NAMESPACE_URL, f"managed-dossier-fixture:{seed}:{name}"))


def prose(topic, length, seed=0):
    """Deterministic readable filler of at most ``length`` characters."""
    words = (
        f"{LABEL}{topic} (variant {seed}): the issuer reported a signed multi-year supply "
        "agreement with a named customer, unit volumes rose against the prior quarter, "
        "management reaffirmed guidance, and completed one-minute bars held above the "
        "retest level while volume expanded on advances and contracted on pullbacks. "
    )
    return (words * (length // len(words) + 1))[:length - 1].rstrip() + "."


def snapshot(*, market="US", seed=0, now=NOW, **changes):
    crypto = market == "CRYPTO"
    qty = D("0.35") if crypto else D("40")
    value = PositionSnapshot(
        candidate_id=ident("candidate", seed), position_id=ident("position", seed),
        lifecycle_id=ident("lifecycle", seed), context_revision=7,
        market=market, symbol="BTC/USD" if crypto else "SYNT",
        strategy_version="CRYPTO_STRUCTURAL_RETEST_TEST_V1" if crypto else "CATALYST_RETEST_V1",
        exit_policy=EXIT_POLICY, cohort=MANAGED_COHORT,
        thesis=prose("Thesis", 1000, seed), disproof=prose("Disproof", 1000, seed),
        original_evidence_ids=(ident("source-0", seed), ident("source-1", seed)),
        original_fill_price=D("103.10"), filled_qty=qty, remaining_qty=qty,
        opened_at=now - timedelta(minutes=48), hard_exit_at=now + timedelta(hours=3),
        quote_id=ident("quote", seed), bid=D("105.37"), ask=D("105.39"), quote_at=now,
        data_provider=PROVIDER, data_feed=FEED, feed_healthy=True, tick_size=D("0.01"),
        stop=ProtectiveOrder(ident("stop", seed), "STOP", D("101.20"), qty, "ACTIVE", 7,
                             "BROKER"),
        target=ProtectiveOrder(ident("target", seed), "TARGET", D("108.40"), qty, "ACTIVE", 7,
                               "MECHANICAL_LOCAL" if crypto else "BROKER"),
        state="OPEN", news_revision=9, snapshot_at=now,
    )
    return replace(value, **changes)


def bars(count=64, *, seed=0, now=NOW):
    """``count`` completed one-minute bars, oldest first; lows and spikes create options."""
    spikes = {5: "108.55", 12: "108.93", 20: "109.25", 33: "108.71", 47: "109.62",
              58: "108.47"}
    end = now.replace(second=0, microsecond=0)
    result = []
    for index in range(count):
        position = index + 64 - count
        base = D("103.50") + D("0.027") * position + D(seed % 7) / 100
        low = (base - D("0.18")).quantize(D("0.01"))
        high = D(spikes[position]) if position in spikes else (base + D("0.21")).quantize(
            D("0.01"))
        opened = (base - D("0.05")).quantize(D("0.01"))
        closed = (base + D("0.07")).quantize(D("0.01"))
        stop_at = end - timedelta(minutes=count - 1 - index)
        result.append(CompletedBar(
            ident(f"bar-{position}", seed), stop_at - timedelta(minutes=1), stop_at,
            opened, high, low, closed, D(18_000 + 137 * position), PROVIDER, FEED,
        ))
    return tuple(result)


def monitor_bars(now, count=60, *, provider="LAB_FIXTURE", feed="FIXTURE"):
    """A runtime-sized window for the managed-execution fixture position.

    That position fills near 100 with stop 95 and target 111 and is reviewed at bid 108:
    recent lows between 101 and 107 back stop options and six highs above 111 back
    target options. Oldest first; the last bar ends on ``now``'s minute.
    """
    spikes = {3: "111.40", 9: "112.10", 17: "111.85", 26: "112.60", 38: "113.20",
              count - 4: "111.30"}
    end = now.replace(second=0, microsecond=0)
    result = []
    for index in range(count):
        base = D("101.20") + D("0.1") * index
        low = min(base, D("107.40"))
        stop_at = end - timedelta(minutes=count - 1 - index)
        result.append(CompletedBar(
            ident("monitor-bar-" + stop_at.isoformat()), stop_at - timedelta(minutes=1), stop_at,
            low + D("0.10"), D(spikes.get(index, str(low + D("0.60")))), low,
            low + D("0.30"), D(9_000 + 41 * index), provider, feed,
        ))
    return tuple(result)


def news_item(name, stance, *, origin, seed=0, now=NOW, length=1200, published=True):
    excerpt = prose(f"{name} {stance or 'research'} excerpt", length, seed)
    return NewsEvidence(
        ident("news-" + name, seed), 9 if origin == "CURRENT_EVIDENCE" else 1,
        "source-" + name, excerpt, now - timedelta(minutes=3), digest(excerpt),
        url=f"https://issuer.example/filings/{name}",
        published_at=now - timedelta(minutes=12) if published else None,
        primary_source=True if stance else None, asset_relevant=True if stance else None,
        novelty="NEW_FACT" if stance else None, stance=stance, origin=origin,
    )


def news(seed=0, now=NOW):
    """Four current items (two adverse, two supporting) and two original research items."""
    current = (
        news_item("current-support-a", "SUPPORTS", origin="CURRENT_EVIDENCE", seed=seed, now=now),
        news_item("current-adverse-a", "ADVERSE", origin="CURRENT_EVIDENCE", seed=seed, now=now),
        news_item("current-support-b", "SUPPORTS", origin="CURRENT_EVIDENCE", seed=seed, now=now),
        news_item("current-withdrawn", "WITHDRAWN", origin="CURRENT_EVIDENCE", seed=seed,
                  now=now),
    )
    original = (
        news_item("original-0", None, origin="ORIGINAL_RESEARCH", seed=seed, now=now,
                  published=False),
        news_item("original-1", None, origin="ORIGINAL_RESEARCH", seed=seed, now=now),
    )
    return current, original


def rationale(*, confidence=False, seed=0):
    block = {
        "status": "UNVERIFIED_PROPOSER_CLAIMS",
        "claims": [
            {"claim_id": f"c{i}", "kind": kind, "text": prose(f"Claim {i}", 240, seed),
             "supported_by": {"source_ids": ["source-original-0"] if i % 2 == 0 else [],
                              "bar_ids": [f"research-bar-{i}"] if i % 2 else []}}
            for i, kind in enumerate(("CATALYST", "NOVELTY", "ECONOMIC_LINK", "TECHNICAL",
                                      "RISK", "CATALYST"))
        ],
        "why_now": prose("Why now", 420, seed),
        "why_these_levels": prose("Why these levels", 420, seed),
        "why_over_peers": prose("Why over peers", 400, seed),
        "what_would_change_my_mind": prose("Disproof trigger", 280, seed),
        "known_risks": [prose(f"Risk {i}", 100, seed) for i in range(3)],
    }
    if confidence:
        block["agent_confidence"] = {"level": "HIGH", "basis": "Analytics-only fixture basis."}
    return block


def choice_answer(chosen, options, top="0.83"):
    rest = (1 - float(top)) / (len(options) - 1)
    return {"type": "choice", "choice": chosen, "confidence": 0.8,
            "probabilities": {o: float(top) if o == chosen else rest for o in options}}


def score_answer(level, top="0.71"):
    rest = (1 - float(top)) / 2
    probabilities = {str(i): float(top) if i == level else rest for i in range(3)}
    return {"type": "score", "score": sum(i * p for i, p in enumerate(probabilities.values())),
            "legend": {"0": "Weak.", "1": "Mixed.", "2": "Strong."}, "confidence": 0.7,
            "probabilities": probabilities}


def selection(seed=0):
    insufficient = "Insufficient evidence"
    return {
        "eligibility": {
            "receipt_id": ident("eligibility-receipt", seed), "actual_model": "jev-1.13.0",
            "question_set_version": "SKEPTIC_QUESTIONS_V1",
            "completed_at": (NOW - timedelta(hours=1)).isoformat(),
            "answers": {
                "news_stale": choice_answer("NO", ("YES", "NO", insufficient)),
                "already_priced": choice_answer("LOW", ("LOW", "MEDIUM", "HIGH", insufficient)),
                "unsupported_inference": choice_answer("NO", ("YES", "NO", insufficient)),
                "verdict": choice_answer("APPROVE", ("APPROVE", "REJECT", "NEEDS_REVIEW",
                                                     insufficient), top="0.91"),
            },
        },
        "quality": {
            "receipt_id": ident("quality-receipt", seed), "actual_model": "jev-1.13.0",
            "question_set_version": "MUSE_JEV_COMPARATIVE_QUALITY_V1",
            "completed_at": (NOW - timedelta(minutes=59)).isoformat(),
            "answers": {name: score_answer(2) for name in (
                "evidence_quality", "catalyst_specificity", "disproof_quality",
            )},
        },
    }


def history(count=8, seed=0):
    rows = []
    for index in range(count // 2):
        context_hash = digest(f"context-{seed}-{index}")
        seq = 1000 + 10 * index
        rows.append({"event_seq": seq, "kind": "MANAGED_JEV_JUDGMENT", "decision": {
            "context_hash": context_hash, "action": "TIGHTEN_STOP" if index % 2 else "HOLD",
            "reason": None, "receipt_ids": [ident(f"judgment-{index}", seed)],
            "request_id": ident(f"request-{index}", seed)}})
        rows.append({"event_seq": seq + 1, "kind": "MANAGEMENT_PLAN_AUTHORIZED"
                     if index % 2 else "POSITION_REVIEW_OBSOLETE", "decision": {
                         "context_hash": context_hash, "stop": f"10{index}.25",
                         "target": "108.4", "receipt_ids": [], "reason": "FIXTURE",
                         "request_id": ident(f"request-{index}", seed)}})
    return rows


def research(*, confidence=False, seed=0, analysis_chars=4000):
    return {
        "thesis": prose("Thesis", 1000, seed), "disproof": prose("Disproof", 1000, seed),
        "catalyst": "PRODUCT_SUPPLY_AGREEMENT",
        "economic_relationship": prose("Economic relationship", 1000, seed),
        "levels": {"entry_trigger": "103.00", "max_entry_price": "103.20", "stop": "101.20",
                   "target": "108.40"},
        "technical_context": {
            "origin": "EXTERNAL_MUSE_RESEARCH",
            "analysis": prose("Technical analysis", analysis_chars, seed),
            "execution_checks": "PENDING_INDEPENDENT_APP_VALIDATION",
            "observed_facts": {
                "observations_hash": digest(f"observations-{seed}"),
                "metrics": {"last_completed_close": "103.05", "sma_5": "102.968",
                            "sma_20": "102.4415", "last_volume_vs_prior_19_mean":
                            "1.482936507936507936507936508", "observed_dollar_volume_20":
                            "38123456.25", "spread_bps": "1.941747572815533980582524272",
                            "last_bar_age_seconds": "31", "quote_age_seconds": "2",
                            "gap_count": 0},
            },
        },
        "technical_facts": None,
        "rationale": rationale(confidence=confidence, seed=seed),
    }


def excursions(now=NOW):
    return {"sampling_method": "FIRST_VALID_OBSERVATION_PER_RECEIVED_SECOND",
            "sample_count": 2874, "first_sample_at": (now - timedelta(minutes=48)).isoformat(),
            "last_sample_at": (now - timedelta(seconds=1)).isoformat(),
            "max_observation_gap_seconds": 2.004, "observed_mfe_pnl": "96.40",
            "observed_mae_pnl": "-18.80", "provenance": [{"data_provider": PROVIDER,
                                                          "data_feed": FEED}],
            "limitations": ["One first valid sample per received second."]}


def v3_dossier(*, confidence=False, seed=0, now=NOW):
    return {
        "original_research": research(confidence=confidence, seed=seed),
        "selection_judgment": selection(seed),
        "management_history": history(8, seed),
        "sampled_excursions": excursions(now),
        "retained": {"version": "FIXTURE_REFERENCES", "research_packet": {
            "setup_event_seq": 900, "evidence_hash": digest(f"evidence-{seed}")}},
    }


def v2_dossier(seed=0, now=NOW):
    """The V2 monitor's dossier shape for the same position (for the defect reproduction)."""
    original = research(seed=seed)
    return {
        "original_research": {k: original.get(k) for k in (
            "thesis", "disproof", "economic_relationship", "catalyst", "technical_context",
            "technical_facts", "levels",
        )},
        "selection_judgment": selection(seed),
        "management_history": history(8, seed),
        "sampled_excursions": excursions(now),
        "evidence_manifest": {"research_evidence_hash": digest(f"evidence-{seed}")},
    }


def trigger():
    return {"policy_id": "JEV_MONITOR_SCHEDULING_ENGINEERING_V1", "reasons": ["COMPLETED_BAR"],
            "bar_end_at": NOW.replace(second=0).isoformat(), "news_revision": 9,
            "dedup_key": "fixture"}


def production_context(policy=V3_PRODUCTION_POLICY, *, seed=0, count=64, now=NOW, market="US"):
    """One production-sized V3 context, built the way the runtime calls the builder."""
    from catalyst_lab.managed_review import build_managed_context

    window = bars(count, seed=seed, now=now)
    current, original = news(seed, now)
    return build_managed_context(
        snapshot(market=market, seed=seed, now=now), window, current, policy, now=now,
        review_deadline=now + timedelta(seconds=10), structural_bars=window,
        original_news=original, dossier=v3_dossier(seed=seed, now=now), trigger=trigger(),
    )

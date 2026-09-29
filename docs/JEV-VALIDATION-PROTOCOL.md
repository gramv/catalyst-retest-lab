# Jev validation protocol

2026-09-20. Evaluation protocol, not an activated selection policy or completed performance experiment.
The initial synthetic diagnostic has now been implemented and run; see
[measured results and remaining stages](JEV-VALIDATION-2026-09-20.md). Paper orders, model changes
and historical decision changes are not authorized by this document.

## Questions this must answer

1. Does Jev interpret the supplied evidence and the declared strategy correctly?
2. Does first-Jev selection improve prospective results compared with the same deterministic strategy without Jev?
3. Does second-Jev position management add value compared with fixed exits, independently of selection?
4. Does the application reliably execute and protect an authorized paper position?

These are separate claims. The 1,079 passing software tests do not establish model judgment accuracy or trading edge.
The previous 30 proposals are development/debugging cases already examined; they cannot be an untouched validation set.

## 1. Freeze the strategy and improve Muse input quality

Keep the current policy and receipts intact. Any revised technical-plus-news selector gets a distinct version and
initially operates without order authority. Define whether a strategy requires a fresh catalyst or permits a
technical retest with contextual news; do not apply one branch's novelty rule to another branch implicitly.

Muse should prequalify proposed setups using the existing numerical rules and full relevant observations:
observed level provenance, reward/risk at maximum entry, spread, liquidity, price ceiling, data age and declared
transaction costs. The application still performs its independent admission and risk checks. Muse gains no
execution, sizing or risk-override permission, and the app does not acquire a discovery scanner.

Retain rejected research drafts with their actual reasons. Target 20–30 well-supported contenders when the
market supplies them; if fewer qualify, report the shortage rather than padding the qualified list. The September
20 batch contained 11 below-2R proposals, 22 example-cost flags and four above-ceiling observations, with overlap.
Those known weaknesses should have been separated before describing the list as a quality shortlist.

Freeze one as-of packet per case: completed candles, quotes and venue, proposed levels, news excerpts and source
IDs, publication precision, prior disclosures, thesis, adverse evidence, source hashes, strategy version and
decision timestamp. A present-day edited webpage is not proof of what was available at an earlier decision time.

## 2. Blind reference benchmark

Start with about 60 diagnostic cases spanning approximately equal reference groups: supported, contradicted,
and genuinely insufficient evidence. This is a coverage target, not a statistically sufficient proof of trading edge.
Include both stock and crypto cases, reporting their results separately. Start execution validation with crypto.

Reference labels must be established before viewing Jev's answers and without future prices. Use deterministic
rules for arithmetic and independently checked primary evidence for factual claims. Have two independent reviewers
label semantic judgments against the written rubric, then adjudicate disagreements. An AI reviewer can assist,
but agreement between models is not ground truth. Keep unresolved cases explicitly uncertain.

Separate development and untouched validation cases by event/date/source family so near-duplicate announcements
cannot leak across the split. Use the existing 30 only for diagnosis. Do not tune prompts on validation results;
changes require a new version and fresh held-out cases.

Include controlled diagnostic pairs where exactly one material fact changes: confirmed deployment versus roadmap,
new evidence versus repeated disclosure, direct economic support versus unsupported extrapolation, and complete
versus missing technical context. Synthetic pairs are labeled engineering tests and never count as strategy returns.

For each first-Jev dimension, preserve the exact answer and probabilities. Measure false acceptance, false rejection,
uncertainty, invalid-response rate and evidence-reference accuracy separately. Report class denominators and
uncertainty intervals, not just overall accuracy: rejecting everything can score well on a bad-candidate-heavy set.

Request typed objection categories and references to supplied evidence IDs. Code verifies those references and
combines the component answers under a versioned rule. Any holistic second-stage verdict must receive the relevant
component results and identify an additional objection. Do not fabricate a narrative explanation for the existing
receipts. An overall REJECT with favorable component answers is unexplained, not automatically logically impossible.

Count malformed distributions and transport failures as service failures, not correct rejections. Test any proposed
provider tolerance or normalization independently under a new contract version. Preserve existing invalid receipts.
If reproducibility testing repeats a packet, predeclare that experiment, retain every answer and keep every repeat
non-authorizing; never select the favorable vote.

TypeSafe confidence summarizes its answer distribution; it is not a calibrated probability of trade profit.
Thresholds must be evaluated on the project's own domain and error costs. [TypeSafe confidence guidance](https://docs.typesafe.ai/confidence).

## 3. Prospective selection comparison

Before looking at outcomes, lock the universe, strategy version, entry trigger, expiry, stops/targets, fill model,
position sizing, portfolio limits, cost assumptions, sampling period and evaluation rule.

Run two shadow portfolios on the same future packets and market observations:

- Baseline: deterministic eligible setups without a Jev selection veto.
- Treatment: those same eligible setups filtered/ranked by the pinned first-Jev policy.

Simulate eligible rejected setups too. Neither portfolio bypasses a safety or eligibility failure. Keep trigger-not-met,
expired, missed/unfilled and entered outcomes distinct. Honor realistic limit fills, partial fills, available liquidity,
fees, spread and slippage; unknown costs remain unknown or explicitly sensitivity-tested. If a bar touches both exit
levels, use finer observations or flag ambiguity rather than assuming the profitable event happened first.

Keep risk budgets and portfolio constraints equal. Report both matched opportunity-level outcomes and implementable
portfolio results so capacity differences are visible. A decline in trading frequency alone is not proof of better judgment.

Measure net expectancy in risk units, aggregate net return, maximum drawdown, exposure, turnover, fill rates and costs.
Separate two meanings of error: disagreement with the evidence rubric, and economically harmful filtering. A rejected
winner alone does not prove a bad decision; a profitable accepted trade alone does not prove a sound one.

Use an initial review checkpoint after roughly 200 eligible opportunities across multiple sessions and conditions;
this is a practical starting point, not an automatic pass. Correlated coins or repeated entries from one event are not
independent samples. Evaluate paired differences with uncertainty grouped by session/event, and separate stocks from
crypto. Extend the experiment if uncertainty remains large. Prespecify the checkpoint and decision rule; do not stop at
the first attractive result. Numerical promotion margins must be frozen before the untouched evaluation, not chosen
after seeing results.

## 4. Validate execution and second Jev separately

Complete bounded paper acceptance for entry, fill, protection, stop/target exit, partial fills, restart recovery,
duplicate deliveries, stale feeds, model failure and rejected amendments. No unexplained account exposure or
unprotected position is acceptable in the acceptance scenarios. Fixture and actual paper-provider evidence remain
separately labeled; the existing owner ledger is not populated with fixtures.

For monitoring value, hold entries constant and compare the approved fixed-exit baseline with the separately versioned
Jev-managed-exit policy on the same forward paths. Record every proposed/accepted/rejected amendment and the precise
context available at that moment. Each real paper broker mutation still requires existing exact one-use risk authorization.
Model unavailability must not stop independent protective controls.

Paper acceptance demonstrates integration, while simulator assumptions limit performance conclusions. Alpaca documents
limitations including unmodeled market impact, latency slippage and queue position; keep a separate conservative cost
and fill sensitivity analysis. [Alpaca paper specification](https://docs.alpaca.markets/us/docs/paper-trading).

## Decision and immediate order of work

Jev is validated for a defined role only after it shows acceptable held-out evidence judgments, contributes measurable
out-of-sample value or a prespecified risk benefit, and passes the independent operational acceptance for that role.
If results are inconclusive or show no useful contribution, retain it as an advisory component rather than asserting
it should control selection. A forced 5–10 approvals per batch is never a success criterion.

Immediate sequence: build and adjudicate the time-frozen benchmark; implement the explicitly versioned reason-bearing
selector without order authority; evaluate its untouched cases; complete controlled paper lifecycle acceptance; then
run the locked prospective portfolio comparison. Evaluate second-Jev management separately after entry behavior is sound.

Current status (updated after implementation): the synthetic 60-case benchmark, frozen independent AI review,
non-authorizing reason-bearing diagnostic and 60 real-provider evaluations are complete. A crypto matched-opportunity
shadow ledger/evaluator is implemented. See [the bounded-run report](JEV-VALIDATION-2026-09-20.md) for measured mistakes,
reference ambiguity, accounting corrections and limitations. Human-adjudicated real-case validation, prospective
portfolio comparison, second-Jev management value and new-workflow actual fill/management acceptance remain open.

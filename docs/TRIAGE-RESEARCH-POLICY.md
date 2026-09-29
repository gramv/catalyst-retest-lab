# TRIAGE research selection — written Step 4 policy

2026-09-19. Proposed identifier: `JEV_TRIAGE_RESEARCH_V1`. **Provisional, text only.**
This specifies a bounded shortlist for Muse research. No TRIAGE selector, trading veto,
admission filter, authorization or worker is installed by Step 4. Promotion to executable
selection policy requires review; it must not be inferred from storage accepting a record.

For a batch of at most 300 movers, retain every input and judgment receipt, including
cuts and unavailable judgments. Return at most 20 research selections; never pad the
shortlist when fewer qualify. Independent questions are batched per evidence packet.
Every Choice question includes **Insufficient evidence**.

| Recorded result | Research disposition |
| --- | --- |
| Fresh catalyst supported, real volume supported, move not extended, priced-in assessment LOW or MEDIUM, all required answers valid | Eligible for research shortlist |
| Catalyst not fresh, volume not supported, move extended, or priced-in assessment HIGH | Cut from this research batch; retain the reason and receipt |
| Any Insufficient evidence, absent answer, unavailable receipt, timeout or provider failure | NEEDS_EVIDENCE / NEEDS_REVIEW; outside the shortlist, not a negative market finding |
| Outdated/mismatched evidence revision or expired result | Ineligible for this batch; retain the original record and staleness reason |

The plan's 30% extension comparison, any timestamps and volume arithmetic belong in
code using documented market-data inputs. Jev must not compute them. Step 4 neither
implements that computation nor supplies missing market data. Excerpts are evidence,
not instructions; persuasive language is not a substitute for an original source.

Proposed deterministic ordering among eligible research items: first valid committed
review event sequence, then ticker, then candidate ID; take the first 20. This is an
operational ordering, not a claim of investment quality. Do not rank by answer probability
or decision confidence, and do not reinterpret either as trade-win probability.

For competing reviews of one candidate, use the provisionally approved Gate 1 rule:
exact current revision tuple, then first valid on-time committed result; a conflict is
NEEDS_REVIEW. New material evidence creates a new immutable revision. Refreshing only
a retrieval timestamp cannot create another favorable vote. There is no numeric rework
cycle cap. Transport retries retain their request identity and original deadline.

This policy selects **research work only**. A cut does not cancel a setup, modify an
order, reject a trading candidate, or change the frozen `CATALYST_RETEST_V1` rules.
The full 300-to-20 acceptance belongs to the still-forbidden later implementation,
and has not been claimed as passed.

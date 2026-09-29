"""Frozen, authored engineering cases; none are observations of actual markets.

Only ``case['state']`` belongs in a provider request. Metadata and provisional author
references must remain outside that state. Independent reference review is required
before scoring; agreement with these labels is not evidence of trading performance.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

BENCHMARK_VERSION = "JEV_SYNTHETIC_EVIDENCE_V1"
PROVENANCE = "SYNTHETIC_ENGINEERING"
DIMENSIONS = ("novelty", "economic_support", "technical_coherence")
SUPPORTED = "SUPPORTED"
CONTRADICTED = "CONTRADICTED"
INSUFFICIENT = "Insufficient evidence"
LABELS = (SUPPORTED, CONTRADICTED, INSUFFICIENT)


@dataclass(frozen=True)
class _Family:
    number: int
    market: str
    split: str
    issuer: str
    symbol: str
    focus: str
    event: str
    previous: str
    novelty_claim: str
    economic_claim: str
    economic_detail: str
    variations: tuple[str | None, str | None, str | None]
    technical_feature: str = "support"


# Entire families are assigned before judgments. Each variation changes one material
# evidence item, never the claim or question. Variant order is hidden from case IDs.
_FAMILIES = (
    _Family(
        1, "US", "development", "Orlen Instruments", "ORI", "novelty",
        "The factory's metrology line has entered customer-accepted commercial operation.",
        "The metrology line remains in commissioning; customer acceptance has not occurred.",
        "Customer-accepted commercial operation is new relative to the prior disclosure.",
        "The operating line now permits the issuer to invoice contracted metrology shipments.",
        "Customer acceptance activates invoicing under existing purchase contracts. "
        "The issuer has begun invoicing accepted shipments from this line.",
        (
            "The metrology line remains in commissioning; customer acceptance has not occurred.",
            "The metrology line is in customer-accepted commercial operation. "
            "This status is effective and public as of this earlier disclosure.",
            None,
        ),
    ),
    _Family(
        2, "US", "development", "Maren Logistics", "MRLG", "economic_support",
        "The issuer has filed the final terms of its regional distribution agreement.",
        "Negotiations continue; final distribution agreement terms have not been filed.",
        "Final agreement terms have now been disclosed for the first time in this packet.",
        "The agreement creates committed minimum service revenue for this issuer.",
        "The signed agreement requires the customer to purchase at least $12 million "
        "of services from the issuer in the first contract year.",
        (
            "The signed agreement requires the customer to purchase at least $12 million "
            "of services from the issuer in the first contract year.",
            "The agreement is nonbinding, has no minimum purchase commitment and "
            "does not require the customer to buy any services.",
            "The contract's purchase obligations and financial schedule are withheld; "
            "the supplied filing does not include those terms.",
        ),
    ),
    _Family(
        3, "US", "development", "Tavren Components", "TVCP", "technical_coherence",
        "The issuer reports first customer acceptance of its new connector range.",
        "The connector range is undergoing acceptance tests; no customer has accepted it.",
        "First customer acceptance is a substantive new development.",
        "Acceptance enables invoicing for units already ordered from the issuer.",
        "The purchasing contract permits invoicing when acceptance occurs. "
        "The accepted units are now invoiced to the purchasing customer.",
        ("support_below", "support_above_stop", None),
    ),
    _Family(
        4, "US", "development", "Brinvale Systems", "BRVS", "novelty",
        "Management's current recurring-revenue guidance is $240 million, raised "
        "from the $210 million guidance preceding the revision.",
        "Current recurring-revenue guidance is $210 million; no revision has been issued.",
        "The $240 million recurring-revenue guidance has not appeared in the prior disclosure.",
        "The guidance revision has a stated basis in contracted recurring revenue.",
        "Management attributes the $30 million increase to signed renewals and new "
        "annual contracts, with recognition scheduled inside the guided period.",
        (
            "Current recurring-revenue guidance is $210 million; no revision has been issued.",
            "Management has raised current recurring-revenue guidance to $240 million "
            "from $210 million; that revised figure is already public.",
            None,
        ),
    ),
    _Family(
        5, "US", "development", "Sellen Materials", "SLMT", "economic_support",
        "A new operating update discloses the composition of the issuer's shipment increase.",
        "The issuer has not yet disclosed the composition of this shipment increase.",
        "The composition of this shipment increase is newly disclosed.",
        "The reported shipment increase represents additional billable external sales.",
        "All additional shipments were accepted by external paying customers; "
        "they are billable sales rather than inventory transfers.",
        (
            "All additional shipments were accepted by external paying customers; "
            "they are billable sales rather than inventory transfers.",
            "All additional shipments were internal warehouse transfers. "
            "None was sold or billable to an external customer.",
            "The update gives total units shipped without customer acceptance, "
            "sales classification or billing information.",
        ),
    ),
    _Family(
        6, "US", "holdout", "Envara Networks", "EVNW", "novelty",
        "The municipal network is operational and the municipality has signed handover.",
        "The municipal network awaits commissioning and municipal handover.",
        "Signed municipal handover is new information relative to the prior disclosure.",
        "Signed handover activates the issuer's contracted service payments.",
        "Under the signed service contract, monthly payments begin upon municipal "
        "handover. The first monthly invoice has been issued following handover.",
        (
            "The municipal network awaits commissioning and municipal handover.",
            "The municipal network is operational and municipal handover has been signed; "
            "both facts are publicly disclosed in this earlier notice.",
            None,
        ),
    ),
    _Family(
        7, "US", "holdout", "Pellora Devices", "PLDV", "economic_support",
        "The regulator has released its decision on the issuer's new device submission.",
        "The device submission is pending and no decision has been published.",
        "The regulator's decision is newly available relative to the prior filing.",
        "The decision permits commercial sale of the submitted device in this jurisdiction.",
        "The regulator grants commercial marketing authorization for the device "
        "in the requested indication, effective immediately.",
        (
            "The regulator grants commercial marketing authorization for the device "
            "in the requested indication, effective immediately.",
            "The regulator accepts an application for review only. No marketing "
            "authorization is granted and commercial sale remains prohibited.",
            "Only the decision cover page is supplied; its disposition and "
            "commercial authorization terms are not included.",
        ),
    ),
    _Family(
        8, "US", "holdout", "Avenro Services", "AVSR", "technical_coherence",
        "The issuer has reported the first invoicing milestone for a service rollout.",
        "The rollout is in acceptance testing; its invoicing milestone is not reached.",
        "First invoicing is a new operational milestone.",
        "The milestone provides evidence of billable service activity by the issuer.",
        "The issuer has delivered and invoiced the contracted service to paying "
        "customers; invoices are for external revenue-generating work.",
        ("ceiling_above_target", "ceiling_below_target", None), "ceiling",
    ),
    _Family(
        9, "US", "holdout", "Quenlow Software", "QLSW", "economic_support",
        "A new earnings release details the source of the reported operating-profit increase.",
        "The new quarter's earnings and sources of profit changes are not yet published.",
        "This quarter's profit composition is newly reported.",
        "The reported operating-profit increase comes from continuing customer operations.",
        "The entire operating-profit increase comes from higher recurring license "
        "revenue; there are no disposal gains or accounting reversals in the increase.",
        (
            "The entire operating-profit increase comes from higher recurring license "
            "revenue; there are no disposal gains or accounting reversals in the increase.",
            "The entire reported increase is an exceptional property disposal gain. "
            "Continuing customer operating profit declined from the comparison period.",
            "The release states total profit but omits the continuing-operations "
            "reconciliation and explanations of exceptional items.",
        ),
    ),
    _Family(
        10, "US", "holdout", "Nerrow Packaging", "NRPK", "technical_coherence",
        "A new report confirms customer acceptance of the issuer's packaging installation.",
        "Customer acceptance testing of the installation remains open.",
        "Completion of customer acceptance is newly confirmed.",
        "Completed acceptance releases a contractually due payment to the issuer.",
        "The signed contract makes its milestone payment due at customer acceptance; "
        "acceptance is complete and the issuer has submitted the milestone invoice.",
        ("support_below", "support_above_stop", None),
    ),
    _Family(
        11, "CRYPTO", "development", "Velin Network", "VLN/USD", "novelty",
        "Network version 3 is active on the public mainnet and the fee meter is running.",
        "Version 3 remains a roadmap item; public-mainnet activation has not occurred.",
        "Public-mainnet activation of version 3 is new relative to the prior update.",
        "Active operation creates transaction-linked demand for the network's native token.",
        "Each transaction requires the native token as its fee asset. The mainnet "
        "fee meter records fees paid in that token by executed transactions.",
        (
            "Version 3 remains a roadmap item; public-mainnet activation has not occurred.",
            "Version 3 is active on the public mainnet with its fee meter running; "
            "these facts were disclosed by this earlier update.",
            None,
        ),
    ),
    _Family(
        12, "CRYPTO", "development", "Calven Protocol", "CLVN/USD", "economic_support",
        "The protocol has published newly adopted fee-distribution terms.",
        "The proposed fee-distribution terms are pending; adoption has not been announced.",
        "Adoption of these fee-distribution terms is newly disclosed.",
        "The adopted terms route protocol fee revenue to the native token's holders.",
        "The adopted and active terms distribute collected protocol fees pro rata "
        "to native-token holders; the distributor has executed its first payment.",
        (
            "The adopted and active terms distribute collected protocol fees pro rata "
            "to native-token holders; the distributor has executed its first payment.",
            "All collected fees accrue exclusively to the operating company. "
            "Native-token holders receive no fee distribution or redemption claim.",
            "The published notice omits the recipient schedule and operative "
            "distribution contract, so the ultimate fee beneficiaries are unspecified.",
        ),
    ),
    _Family(
        13, "CRYPTO", "development", "Ravlen Chain", "RVLN/USD", "technical_coherence",
        "The mainnet's first paid settlement batch has finalized.",
        "Only unpaid test settlements have completed; no paid mainnet batch has finalized.",
        "Finalization of the first paid mainnet settlement batch is new evidence.",
        "The batch supplies evidence of native-token use for settlement fees.",
        "The finalized settlement batch paid mandatory network fees in the native "
        "token. The batch receipt records the fee asset and completed collection.",
        ("support_below", "support_above_stop", None),
    ),
    _Family(
        14, "CRYPTO", "development", "Doreva Ledger", "DRV/USD", "novelty",
        "The production settlement service has processed its first externally paid batch.",
        "Only internal trials are running; no externally paid production batch has occurred.",
        "The first externally paid production settlement is new versus prior disclosure.",
        "Paid production settlements create an actual fee use for the native token.",
        "The service requires the native token for settlement fees. The first "
        "external customer's completed batch paid those fees in the native token.",
        (
            "Only internal trials are running; no externally paid production batch has occurred.",
            "The first externally paid production batch is complete and its native-token "
            "fees are recorded; this milestone is already publicly disclosed.",
            None,
        ),
    ),
    _Family(
        15, "CRYPTO", "development", "Ovela Rollup", "OVL/USD", "economic_support",
        "The rollup has published new production fee-asset configuration details.",
        "Production fee-asset details remain undisclosed pending configuration publication.",
        "The production fee-asset configuration is newly disclosed.",
        "User transactions require purchases or holdings of this rollup's native token for fees.",
        "Every production user transaction must pay fees in the rollup's native "
        "token; no alternate fee asset is accepted.",
        (
            "Every production user transaction must pay fees in the rollup's native "
            "token; no alternate fee asset is accepted.",
            "User fees are payable only in the base chain's token. The rollup's "
            "native token is not used for fees and is not required to transact.",
            "The supplied configuration excerpt ends before the fee-asset table. "
            "No accepted fee asset is identified in the packet.",
        ),
    ),
    _Family(
        16, "CRYPTO", "holdout", "Irvon Network", "IRVN/USD", "novelty",
        "The fee-burn governance change has executed and is active on the production chain.",
        "The fee-burn proposal is approved but execution is pending; old fee handling remains.",
        "Execution of the fee-burn change, beyond proposal approval, is newly disclosed.",
        "The active change retires native tokens from already collected transaction fees.",
        "The active fee handler routes 20 percent of collected native-token fees "
        "to irreversible retirement; execution receipts record completed retirements.",
        (
            "The fee-burn proposal is approved but execution is pending; old fee handling remains.",
            "The fee-burn change has already executed and is active in production; "
            "the execution was publicly disclosed in this earlier notice.",
            None,
        ),
    ),
    _Family(
        17, "CRYPTO", "holdout", "Pelven Chain", "PLVN/USD", "economic_support",
        "A new validator report discloses the funding source for staking rewards.",
        "The reward-funding composition has not been disclosed in prior validator reports.",
        "The staking-reward funding breakdown is newly supplied.",
        "The reported rewards transfer externally paid protocol fees to native-token stakers.",
        "All reported rewards are paid from fees collected from external network "
        "users and transferred to native-token stakers; none comes from new issuance.",
        (
            "All reported rewards are paid from fees collected from external network "
            "users and transferred to native-token stakers; none comes from new issuance.",
            "All reported rewards are newly issued tokens. No external user fees "
            "were collected or transferred to stakers in this reporting period.",
            "The report states the reward rate but omits the funding-source "
            "breakdown and fee collection records.",
        ),
    ),
    _Family(
        18, "CRYPTO", "holdout", "Roveka Protocol", "RVK/USD", "technical_coherence",
        "The protocol's first fee-paying production application has launched.",
        "The application remains in testing and has not paid production fees.",
        "A fee-paying production application is newly operating.",
        "Application activity creates a demonstrated use of the native token for fees.",
        "Production calls require the native token as a fee asset. Application "
        "receipts record completed production calls and native-token fee payments.",
        ("ceiling_above_target", "ceiling_below_target", None), "ceiling",
    ),
    _Family(
        19, "CRYPTO", "holdout", "Soreli Protocol", "SRLI/USD", "economic_support",
        "A new treasury report discloses how the protocol's fee purchases are funded.",
        "The funding source of the fee-purchase program has not been disclosed.",
        "The program's funding-source disclosure adds new substantive information.",
        "The program uses externally earned fees to buy the native token in the market.",
        "The program buys the native token using stablecoin fees paid by external "
        "users; completed purchase receipts identify that fee-funded treasury account.",
        (
            "The program buys the native token using stablecoin fees paid by external "
            "users; completed purchase receipts identify that fee-funded treasury account.",
            "No market purchases occurred. The program merely transfers previously "
            "minted native tokens between treasury accounts and uses no external fees.",
            "The report mentions a token program but omits funding accounts, purchase "
            "receipts and whether any market purchase has occurred.",
        ),
    ),
    _Family(
        20, "CRYPTO", "holdout", "Talven Network", "TLVN/USD", "technical_coherence",
        "The network reports its first fee-paying production data publisher.",
        "All existing data publishers are on an unpaid test deployment.",
        "A fee-paying production data publisher is a newly disclosed milestone.",
        "The publisher provides evidence of native-token use for network service fees.",
        "Publishing production data requires payment in the native token. "
        "Completed publisher receipts record native-token service fees paid to the network.",
        ("support_below", "support_above_stop", None),
    ),
)


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _source(family: _Family, number: int, excerpt: str | None) -> dict:
    day = 14 if number == 2 else 18
    return {
        "source_id": f"E{family.number:02d}-{number}",
        "publisher": family.issuer,
        "document_kind": {1: "current_notice", 2: "prior_disclosure", 3: "operating_terms"}[number],
        "published_at": f"2026-09-{day}T12:00:00Z" if excerpt is not None else None,
        "publication_precision": "SECOND" if excerpt is not None else "UNKNOWN",
        "available_at": "2026-09-18T14:58:00Z",
        "capture_status": "CAPTURED" if excerpt is not None else "MISSING_FROM_PACKET",
        "excerpt": excerpt,
        "excerpt_sha256": sha256(excerpt.encode()).hexdigest() if excerpt is not None else None,
    }


def _technical(family: _Family, variant: str | None) -> dict:
    # Decimal results are serialized, so judges interpret context and never calculate R.
    maximum, stop, target = Decimal("102"), Decimal("98"), Decimal("110")
    risk, reward = maximum - stop, target - maximum
    context = {
        "interval": "5_MINUTES",
        "completed_bars": [
            {"end": "2026-09-18T14:45:00Z", "open": "100", "high": "101", "low": "99",
             "close": "100.5", "volume": "10000"},
            {"end": "2026-09-18T14:50:00Z", "open": "100.5", "high": "102", "low": "100",
             "close": "101", "volume": "12000"},
            {"end": "2026-09-18T14:55:00Z", "open": "101", "high": "102", "low": "100.5",
             "close": "101.5", "volume": "11000"},
        ],
        "lowest_completed_support_defense": "99",
        "nearest_prior_overhead_rejection": "112",
        "overhead_level_observed_at": "2026-09-18T14:25:00Z",
        "level_provenance": "Completed venue bars, including the preceding rejection swing.",
    }
    if variant == "support_above_stop":
        context["completed_bars"][0]["low"] = "97"
        context["lowest_completed_support_defense"] = "97"
    elif variant == "ceiling_below_target":
        context["nearest_prior_overhead_rejection"] = "106"
    elif variant is None:
        if family.technical_feature == "support":
            context["completed_bars"] = None
            context["lowest_completed_support_defense"] = None
            context["level_provenance"] = "Completed support bars were not captured."
        else:
            context["nearest_prior_overhead_rejection"] = None
            context["overhead_level_observed_at"] = None
            context["level_provenance"] = "The preceding overhead swing was not captured."
    return {
        "evidence_id": f"E{family.number:02d}-4",
        "venue": "PRIMARY_CONSOLIDATED" if family.market == "US" else "USD_SPOT_BOOK",
        "observed_at": "2026-09-18T14:59:59Z",
        "levels": {"entry_trigger": "101.5", "maximum_entry": str(maximum),
                   "stop": str(stop), "target": str(target)},
        "deterministic_facts": {
            "risk_per_unit_at_maximum_entry": str(risk),
            "reward_per_unit_at_maximum_entry": str(reward),
            "reward_risk_at_maximum_entry": str(reward / risk),
            "required_minimum_reward_risk": "2",
            "quote_age_seconds": "1",
            "maximum_quote_age_seconds": "5",
            "spread_bps": "4",
            "maximum_spread_bps": "10",
            "recent_dollar_volume": "100000000",
            "minimum_dollar_volume": "1000000",
            "quote_midpoint": "101.5",
            "round_trip_cost_per_unit": "0.08",
            "cost_basis": "Declared scenario cost assumption; not a verified venue fee.",
        },
        "context": context,
        "context_sha256": _hash(context),
    }


def _make_case(family: _Family, variation: int) -> dict:
    previous, economic_detail = family.previous, family.economic_detail
    technical_variant = "ceiling_above_target" if family.technical_feature == "ceiling" else (
        "support_below"
    )
    if family.focus == "novelty":
        previous = family.variations[variation]
    elif family.focus == "economic_support":
        economic_detail = family.variations[variation]
    else:
        technical_variant = family.variations[variation]
    technical_claim = (
        "The proposed target lies below the nearest observed overhead rejection level."
        if family.technical_feature == "ceiling" else
        "The proposed stop lies below the lowest completed-bar defense of support."
    )
    state = {
        "as_of": "2026-09-18T15:00:00Z",
        "strategy_version": "EVIDENCE_RETEST_RESEARCH_V1",
        "strategy_mode": "FRESH_CATALYST",
        "market": family.market,
        "instrument": {"issuer_or_protocol": family.issuer, "symbol": family.symbol},
        "claims": {"novelty": family.novelty_claim, "economic_support": family.economic_claim,
                   "technical_coherence": technical_claim},
        "thesis": family.economic_claim + " The proposal is a long retest around the stated level.",
        "disproof": "The operating mechanism ceases to hold, or observed price loses the "
        "support used for the setup. These are prospective invalidation conditions.",
        "technical_narrative": technical_claim + " The candidate is awaiting a retest; "
        "the packet does not claim that an entry has executed.",
        "source_scope": "Compare the stated claims only with this time-frozen packet; "
        "the two disclosures define the supplied novelty comparison window.",
        "sources": [_source(family, 1, family.event), _source(family, 2, previous),
                    _source(family, 3, economic_detail)],
        "technical": _technical(family, technical_variant),
    }
    reference = dict.fromkeys(DIMENSIONS, SUPPORTED)
    reference[family.focus] = LABELS[variation]
    return {
        "case_id": "case-" + _hash([BENCHMARK_VERSION, family.number, variation])[:12],
        "family_id": f"family-{family.number:02d}",
        "split": family.split,
        "market": family.market,
        "provenance": PROVENANCE,
        "state": state,
        "reference": reference,
    }


def get_cases() -> list[dict]:
    """Return fresh copies of 60 frozen cases, ordered by opaque case ID.

    ``reference`` contains provisional author choices for three independent dimensions,
    not adjudicated truth. Never transmit the enclosing record as model-visible state.
    No file access, randomness, credentials, runtime database or provider is used.
    """
    return sorted(
        [_make_case(family, variation) for family in _FAMILIES for variation in range(3)],
        key=lambda case: case["case_id"],
    )

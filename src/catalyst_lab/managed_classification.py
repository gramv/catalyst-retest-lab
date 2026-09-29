"""Operator-only classification import for the explicitly opted-in paper test profile."""

import re
from types import MappingProxyType

from catalyst_lab.account_risk import UNLISTED_CRYPTO_THEME
from catalyst_lab.execution import system_event
from catalyst_lab.managed_store import POLICY

CLASSIFICATION_POLICY = "MANAGED_SERVER_CLASSIFICATIONS_TEST_V1"
# Owner-supplied crypto buckets (plan 2.2): sector CRYPTO, theme = bucket name.
BUCKET_CLASSIFICATION_POLICY = "MANAGED_SERVER_CLASSIFICATIONS_V2"
# Built-in crypto sectors (package crypto-size-hold, plan 4.4): sector CRYPTO, theme = the
# coin's sector below, or CRYPTO_OTHER for a coin the list does not name. The owner's buckets
# (MANAGED_CRYPTO_BUCKETS_JSON), when supplied, override the list: the import is then exactly
# the V2 bucket import.
SECTOR_CLASSIFICATION_POLICY = "ALPACA_CRYPTO_SECTORS_V1"
CLASSIFICATION_POLICIES = frozenset(
    {CLASSIFICATION_POLICY, BUCKET_CLASSIFICATION_POLICY, SECTOR_CLASSIFICATION_POLICY}
)
# Policies that accept the owner's MANAGED_CRYPTO_BUCKETS_JSON (required only by the V2 policy).
CRYPTO_BUCKET_POLICIES = frozenset({BUCKET_CLASSIFICATION_POLICY, SECTOR_CLASSIFICATION_POLICY})
LEGACY_CRYPTO_THEME = "CRYPTO_SHARED"
DEFAULT_CRYPTO_BUCKET = "CRYPTO_OTHER"
# ALPACA_CRYPTO_SECTORS_V1: every Alpaca USD crypto pair of 2026-09-26 (33, stablecoins
# excluded) in one sector, from public category data (the categories CoinGecko and
# CoinMarketCap publish for each coin: layer-1 platforms, payments, meme, DeFi, oracle and data,
# AI and decentralized compute, layer-2 scaling, real-world assets, tokenized gold, web3
# applications). BTC, ETH and SOL share LARGE_CAP_L1, so all three may be open together under
# a three-per-sector limit. Provenance and choices: docs/packages/crypto-size-hold.md.
ALPACA_CRYPTO_SECTORS_V1 = MappingProxyType({
    "LARGE_CAP_L1": ("BTC/USD", "ETH/USD", "SOL/USD"),
    "SMART_CONTRACT_L1": ("ADA/USD", "AVAX/USD", "DOT/USD", "XTZ/USD"),
    "PAYMENTS": ("BCH/USD", "LTC/USD", "XRP/USD"),
    "MEME": ("BONK/USD", "DOGE/USD", "PEPE/USD", "SHIB/USD", "TRUMP/USD", "WIF/USD"),
    "DEFI": ("AAVE/USD", "CRV/USD", "HYPE/USD", "LDO/USD", "SKY/USD", "SUSHI/USD", "UNI/USD",
             "YFI/USD"),
    "ORACLE_DATA_INFRA": ("GRT/USD", "LINK/USD"),
    "AI_COMPUTE": ("FIL/USD", "RENDER/USD"),
    "LAYER2_SCALING": ("ARB/USD", "POL/USD"),
    "REAL_WORLD_ASSETS": ("ONDO/USD",),
    "GOLD_BACKED": ("PAXG/USD",),
    "WEB3_APPLICATIONS": ("BAT/USD",),
})
ALPACA_CRYPTO_SECTOR_OF = MappingProxyType(
    {symbol: sector for sector, symbols in ALPACA_CRYPTO_SECTORS_V1.items() for symbol in symbols}
)
# REFUSE (recommended): an unlisted symbol has no usable classification and admission refuses
# it with CORRELATION_UNKNOWN, as for an unmapped stock. CRYPTO_OTHER: the owner explicitly
# places every unlisted symbol in one shared default bucket.
UNLISTED_RULES = frozenset({"REFUSE", DEFAULT_CRYPTO_BUCKET})
_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_CRYPTO = re.compile(r"[A-Z0-9]{1,16}/USD")


def crypto_bucket_config(raw):
    """Validated ``{"buckets": {NAME: [SYMBOL/USD, ...]}, "unlisted": RULE}``.

    Each symbol belongs to exactly one bucket. Bucket names are server identifiers; the legacy
    shared theme and the refusal marker cannot be used as buckets.
    """
    try:
        if not isinstance(raw, dict) or set(raw) != {"buckets", "unlisted"}:
            raise ValueError
        buckets, rule = raw["buckets"], raw["unlisted"]
        if (
            not isinstance(rule, str)
            or rule not in UNLISTED_RULES
            or not isinstance(buckets, dict)
            or not 1 <= len(buckets) <= 64
        ):
            raise ValueError
        seen, result = set(), {}
        for name in sorted(buckets):
            symbols = buckets[name]
            if (
                not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or name in {LEGACY_CRYPTO_THEME, UNLISTED_CRYPTO_THEME}
                or not isinstance(symbols, list)
                or not 1 <= len(symbols) <= 1000
            ):
                raise ValueError
            for symbol in symbols:
                if not isinstance(symbol, str) or not _CRYPTO.fullmatch(symbol) or symbol in seen:
                    raise ValueError
                seen.add(symbol)
            result[name] = tuple(sorted(symbols))
        return {"buckets": result, "unlisted": rule}
    except (TypeError, ValueError):
        raise ValueError("CRYPTO_BUCKET_CONFIGURATION_INVALID") from None


def _crypto_row(symbol, theme):
    return {
        "ticker": symbol,
        "sector": "CRYPTO",
        "theme": theme,
        "market": "CRYPTO",
        "expected_class": "crypto",
    }


def initialize_managed_classifications(
    repository, broker, *, policy_id, us_mapping, crypto_symbols, crypto_buckets=None
):
    """Only supplied US rows; crypto shares one budget (V1 policy), owner buckets (V2) or the
    built-in sectors (ALPACA_CRYPTO_SECTORS_V1).

    Called by the executable launch configuration, never from Muse evidence or an HTTP
    policy endpoint. Identical startup imports are idempotent. Under the V1 policy every
    eligible crypto symbol is CRYPTO/CRYPTO_SHARED and a differing existing classification
    fails closed instead of silently rewriting the experiment. Under the bucket policy the
    owner's buckets explicitly supersede earlier crypto themes by appending new rows (older
    rows and open reservations keep their stored classification); a stock conflict still
    fails closed. An earlier crypto classification the buckets do not list is superseded by
    the unlisted rule: the refusal marker (REFUSE) or the CRYPTO_OTHER bucket.

    Under ALPACA_CRYPTO_SECTORS_V1 (no owner buckets) each crypto symbol classified, the
    supplied ones and every crypto symbol classified before, takes its built-in sector, or
    CRYPTO_OTHER when the list does not name it, superseding earlier crypto themes as the bucket
    import does; a listed coin that is neither supplied nor classified before is not imported,
    so a pair Alpaca no longer lists never fails the import. With owner buckets supplied the
    import is the V2 bucket import, unchanged: the owner's buckets override the built-in list.
    """
    if policy_id not in CLASSIFICATION_POLICIES:
        raise ValueError("EXPLICIT_CLASSIFICATION_POLICY_REQUIRED")
    if policy_id == SECTOR_CLASSIFICATION_POLICY and crypto_buckets is not None:
        policy_id = BUCKET_CLASSIFICATION_POLICY  # The owner's buckets override the built-in list.
    sectors = policy_id == SECTOR_CLASSIFICATION_POLICY
    bucketed = policy_id == BUCKET_CLASSIFICATION_POLICY or sectors
    if (policy_id == BUCKET_CLASSIFICATION_POLICY) != (crypto_buckets is not None):
        raise ValueError("CRYPTO_BUCKETS_REQUIRE_BUCKET_POLICY")
    config = (
        crypto_bucket_config(crypto_buckets)
        if crypto_buckets is not None
        else {"buckets": ALPACA_CRYPTO_SECTORS_V1, "unlisted": DEFAULT_CRYPTO_BUCKET}
        if sectors
        else None
    )
    repository.check_role()
    rows = []
    for row in us_mapping:
        if not isinstance(row, dict) or set(row) != {"ticker", "sector", "theme"}:
            raise ValueError("OPERATOR_US_CLASSIFICATION_REQUIRED")
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", row["ticker"]) or any(
            not isinstance(row[k], str) or not _NAME.fullmatch(row[k]) for k in ("sector", "theme")
        ):
            raise ValueError("OPERATOR_US_CLASSIFICATION_REQUIRED")
        rows.append({**row, "market": "US_STOCKS", "expected_class": "us_equity"})
    for symbol in crypto_symbols:
        if not isinstance(symbol, str) or not _CRYPTO.fullmatch(symbol):
            raise ValueError("CRYPTO_CLASSIFICATION_SYMBOL_INVALID")
    # Earlier classifications were broker-checked when imported; entry re-reads the asset live.
    unchecked = set()
    if not bucketed:
        rows.extend(_crypto_row(symbol, LEGACY_CRYPTO_THEME) for symbol in crypto_symbols)
    else:
        with repository.connect() as conn:
            classified = {
                row["ticker"]
                for row in conn.execute(
                    "SELECT ticker FROM lab.current_classifications WHERE sector='CRYPTO'"
                ).fetchall()
                if _CRYPTO.fullmatch(row["ticker"])
            }
        listed = {s: name for name, symbols in config["buckets"].items() for s in symbols}
        if sectors:  # Only coins supplied now or classified before; never a pair gone since.
            listed = {s: name for s, name in listed.items()
                      if s in set(crypto_symbols) | classified}
        rows.extend(_crypto_row(symbol, theme) for symbol, theme in sorted(listed.items()))
        earlier = classified - set(listed)
        # Earlier classifications were broker-checked when imported; so were listed ones.
        unchecked = (classified if sectors else earlier) - set(crypto_symbols)
        if config["unlisted"] == DEFAULT_CRYPTO_BUCKET:
            unlisted = (set(crypto_symbols) - set(listed)) | earlier
            rows.extend(_crypto_row(s, DEFAULT_CRYPTO_BUCKET) for s in sorted(unlisted))
        else:
            # Refusal markers only restrict; a symbol never classified needs no row at all.
            rows.extend(_crypto_row(s, UNLISTED_CRYPTO_THEME) for s in sorted(earlier))
    if len({r["ticker"] for r in rows}) != len(rows):
        raise ValueError("DUPLICATE_OPERATOR_CLASSIFICATION")
    for row in rows:
        if row["theme"] == UNLISTED_CRYPTO_THEME or row["ticker"] in unchecked:
            continue
        asset = broker.asset(row["ticker"])
        if not isinstance(asset, dict) or not (
            asset.get("symbol") == row["ticker"]
            and asset.get("class") == row["expected_class"]
            and asset.get("status") == "active"
            and asset.get("tradable") is True
        ):
            raise ValueError("CLASSIFICATION_ASSET_MISMATCH")
    inserted = 0
    with repository.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(719172026)")
        for row in rows:
            current = conn.execute(
                "SELECT * FROM lab.current_classifications WHERE ticker=%s", (row["ticker"],)
            ).fetchone()
            supersedes = None
            if current:
                if current["sector"] == row["sector"] and current["theme"] == row["theme"]:
                    continue
                if not (bucketed and row["sector"] == current["sector"] == "CRYPTO"):
                    raise ValueError("EXISTING_CLASSIFICATION_CONFLICT")
                supersedes = {"sector": current["sector"], "theme": current["theme"]}
            elif row["theme"] == UNLISTED_CRYPTO_THEME:
                continue
            body = {
                **row,
                "source": policy_id,
                "execution_profile": POLICY,
                "classification_policy": policy_id,
                "owner_configuration": True,
            }
            if bucketed:
                body.update(unlisted_rule=config["unlisted"], supersedes=supersedes)
            event = system_event(repository, conn, "CLASSIFICATION_IMPORTED", body)
            conn.execute(
                "INSERT INTO lab.risk_classifications VALUES(%s,%s,%s,%s,%s)",
                (event["seq"], row["ticker"], row["sector"], row["theme"], policy_id),
            )
            inserted += 1
    result = {"inserted": inserted, "configured": len(rows), "policy_id": policy_id}
    if bucketed:
        result["unlisted_rule"] = config["unlisted"]
    return result

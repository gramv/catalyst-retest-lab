# Contributing

Thank you for helping. This project is a **paper-trading** laboratory; its rules exist so that
results stay honest and no change can reach real money.

## Ground rules (a pull request that breaks one is not merged)

- **Paper only.** Never add a live-trading code path, a configurable broker endpoint or the live
  endpoint's literal — not in code, tests, docs, examples or compose files.
  `tests/test_safety.py` scans every tracked file for it; derive any forbidden-string assertion
  from `catalyst_lab.config.PAPER_ENDPOINT` instead of writing it.
- **No secrets.** Never commit keys, tokens, passwords or `.env` files. Tests use obvious
  fixtures (`PKFIXTURE…`, `fixture-token-…`).
- **Every broker change goes through the risk gate**: a committed, unexpired, exact-request,
  one-use authorization. No flag bypasses it.
- **Decimal arithmetic** for prices, sizes and R; whole shares for stocks.
- **Append, never rewrite.** The ledger is append-only and hash-chained; corrections are new
  events. Schema changes are new numbered migrations in `src/catalyst_lab/migrations/`; never
  edit an applied one. Application roles never own tables.
- **Rules change only as named versions.** A changed rule is a new name (`MY_RULE_V2`), never an
  edit of `MY_RULE_V1`; a trade keeps the version it was admitted under.
- **Fail closed.** Missing or stale evidence blocks an entry; it never loosens a stop.

## Strategy plug-ins

New strategies do not need core changes: write a plug-in (docs/FIRST-STRATEGY.md). A strategy
you share as an example must say plainly whether it passed a history test on real bars, and
must declare its history variants before its first run.

## Development

```
./run pytest -q              # the full suite (needs a local PostgreSQL: initdb, pg_ctl)
./run ruff check src tests
```

`./run` keeps the virtual environment outside the checkout. In a git worktree, give it its own
data folder: `XDG_DATA_HOME=~/.local/share/catalyst-wt-<name> ./run …`. Tests create disposable
PostgreSQL clusters; they never touch a ledger you run.

Please keep pull requests focused, add tests for behaviour changes, and describe what evidence
(fixture, local PostgreSQL, real broker paper account) a claim rests on.

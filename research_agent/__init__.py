"""The daily research-agent toolkit (plan phase 8, research part).

**Paper trading only.** This package makes the daily crypto research run (owner loop,
``docs/CRYPTO-AGENT-LOOP.md`` sections 1, 2 and 4.1) repeatable without a throw-away
script: read the app's research context, fetch public market data, find level setups
that pass the rules the real Jev accepted on 2026-09-27, verify news excerpts against
their live pages, build an ``AGENT_RESEARCH_REPORT_V3``, and submit it.

``research_agent`` is a standalone client of the app: it is never imported by
``catalyst_lab`` and holds no execution authority. It may import ``catalyst_lab`` to
validate its own output with the app's real models and dossier compiler (``build`` and
``validate`` do this) — the one-way dependency the app's own architecture already uses
for its research boundary (``docs/MUSE-RESEARCH-BOUNDARY.md``): research happens outside
the app; the app only ever receives a finished report over HTTP.

Modules: ``context`` (research context, offline fallback), ``market`` (Coinbase public
candles), ``levels`` (the accepted level rules, under a research profile: ``DAILY_V1`` by
default, ``INTRADAY_V2`` for the 2-hourly runs), ``sources`` (excerpt cutting and
verification), ``build`` (the report V3 builder), ``submit`` (POST to the app), ``token``
(bearer-token loading), ``run`` (the CLI, ``python -m research_agent.run``).

The learning loop (package learning-kit, 2026-09-28; ``docs/LEARNING-LOOP-PLAN.md``
sections 5, 5b and 5c): ``technicals`` (pure 1-hour-bar measurements), ``movers`` (what
moved over a New York day), ``outlook`` (the morning outlook worksheet and
``MARKET_OUTLOOK_V1``), ``postmortem`` (the evening worksheet, the knowable-before-move
helper and ``POST_MORTEM_V1``), ``checklist`` (Muse's own research checklist, kept outside
the app), ``lessons`` (the context's lessons and ordering hints for ``build``) and
``records`` (what the outlook and post-mortem sends share).

Research loop V2 (package research-loop-kit, 2026-09-29; ``docs/RESEARCH-LOOP-V2.md`` 3.5 and
3.6): ``update`` (the update run of ``RESEARCH_SCHEDULE_V2``: the review of the agent's own
WATCHING setups and ``AGENT_RESEARCH_WITHDRAWAL_V1``) and ``derivatives`` (OKX open interest
and Hyperliquid funding as cited context for the daily run's picks).

Answers (package kit-answers, 2026-09-29): ``answers`` (``MUSE_ANSWER_RULES_V1``, the agent's
answers to the app's window reviews and Jev early-exit flags, decided from Coinbase's 5-minute
candles; the ``answer`` command).
"""

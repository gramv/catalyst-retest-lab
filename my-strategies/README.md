# Your strategy plug-ins

Put your own strategy plug-ins (`*.py`) here. The local stack loads this folder beside
`examples/strategies/`: the history tester (`--plugins-dir /my-strategies`), the simulated
venue's shadow and the nightly jobs. Start with

    docker compose run --rm catalyst new-strategy MY_FIRST --dir /my-strategies

and follow [docs/FIRST-STRATEGY.md](../docs/FIRST-STRATEGY.md). Files starting with `_` are
not loaded. Paper trading only: a plug-in never places an order itself.

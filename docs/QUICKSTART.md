# Quickstart: the local stack in about 10 minutes

**PAPER TRADING ONLY.** This starts a complete local lab with one command: a disposable ledger
database, a simulated venue that runs every strategy plug-in in shadow on live public prices,
the read-only page and the learning jobs. You need no broker key and no AI key, and nothing
here can place an order.

What you need: Docker with Compose v2 (Docker Desktop, or Colima/Podman with the compose
plugin), about 2 GB of free disk and a network connection (images, and Alpaca's keyless
public crypto bars).

## 1. Start it (2–5 minutes the first time)

```
git clone <this repository> catalyst-lab
cd catalyst-lab
docker compose up --build
```

What you will see, in order (`docker compose ps -a` in another terminal):

| Service | What it does | Expected state |
| --- | --- | --- |
| `secrets` | generates the stack's database passwords (never printed) | `Exited (0)` |
| `db` | PostgreSQL on a disposable volume; not reachable from your host | `Up (healthy)` |
| `provision` | creates the ledger, its roles and grants; logs `"result": "PROVISIONED"` (later starts: `ALREADY_PROVISIONED`) | `Exited (0)` |
| `trader` | the **simulated venue** (`SIMULATED_VENUE_V1`): logs `SIMULATED_VENUE_STARTED`, then a `SIMULATED_PASS` line with the signals and simulated outcomes it recorded; repeats 90 seconds after every hour | `Up (healthy)` |
| `page` | the read-only page at **http://127.0.0.1:8080** | `Up (healthy)` |
| `jobs` | the nightly learning jobs, once; logs one `STEP` line per step, then `DONE` | `Exited (0)` |

The first trader pass reads about 40 days of hourly bars for four coins and simulates each
signal on 1-minute bars; it takes well under a minute on a normal connection. The page shows the
paper account's figures; with the simulated venue there are no paper trades, so it stays empty
on purpose — the simulated results are in the scorecard (step 3).

## 2. Run your first history test (1 minute)

On generated sample bars (offline, a tutorial run — never evidence):

```
docker compose run --rm catalyst history-test --source synthetic \
    --plugins-dir /app/examples/strategies --strategy EXAMPLE_MA_CROSS_V1 \
    --start 2026-05-01 --end 2026-09-01 --is-days 30 --oos-days 15 \
    --out /data/history-tests/first-run
```

On real public bars (keyless; cached in the `history` volume, so a re-run with `--offline`
needs no network):

```
docker compose run --rm catalyst history-test --source alpaca \
    --universe BTC/USD,ETH/USD --plugins-dir /app/examples/strategies \
    --strategy EXAMPLE_MA_CROSS_V1 --start 2026-08-15 --end 2026-09-15 \
    --is-days 14 --oos-days 7 --out /data/history-tests/public-run
```

Each run prints where its `REPORT.md` and `results.json` are (in the `history` volume). Read
one with:

```
docker compose run --rm --entrypoint cat catalyst /data/history-tests/first-run/REPORT.md
```

How to read it — and why the examples lose — is in [FIRST-STRATEGY.md](FIRST-STRATEGY.md).

## 3. Read the scorecard (30 seconds)

```
docker compose run --rm catalyst stack scorecard
```

It prints each strategy's shadow cell: signals, simulated trades, mean net R after fees and
slippage, win rate and its status (`NOT_ENOUGH_DATA` below 30 trades). Then the day's
scorecard of the paper account (empty here).

## 4. Everyday commands

```
docker compose logs -f trader              # the simulated venue's passes
docker compose run --rm jobs               # the learning jobs again
docker compose stop                        # stop; the ledger is kept
docker compose up -d                       # start again (nothing is re-created)
docker compose down -v                     # delete everything, ledger included
```

Settings live in `compose.yaml` and are listed in [CONFIGURATION.md](CONFIGURATION.md). For
example, `CATALYST_SIM_BARS: synthetic` runs the simulated venue fully offline on generated bars.

## 5. Optional: your own Alpaca **paper** account

The `alpaca-paper` profile adds the managed paper engine on your own paper account, in
`NO_AI_MODE_V1`: only strategies you have promoted trade, every trade keeps fixed exits, and every
order still needs the risk gate's one-use authorization.

```
cp deploy/local/alpaca-paper.env.example .env.paper
chmod 600 .env.paper        # then put your PAPER key id and secret in it
docker compose --profile alpaca-paper run --rm trader-alpaca \
    python -m catalyst_lab.local_stack trader --check      # validates, starts nothing
docker compose --profile alpaca-paper up -d
```

- Create the key in Alpaca's dashboard under **Paper Trading**. Only paper key ids are accepted,
  and the endpoints are fixed in code.
- `.env.paper` is ignored by git and never enters an image. Never commit it or paste it anywhere.
- Nothing trades until a strategy has climbed the ladder, been promoted on this ledger and been
  listed in `MANAGED_STRATEGIES_JSON` ([FIRST-STRATEGY.md](FIRST-STRATEGY.md), step 7).
- The engine runs one executor per paper account. Do not point two stacks at the same account.

This profile was validated in Docker (`trader --check` on the local ledger) but has not been run
against a real broker account as part of this package; treat its first session as a supervised
test.

## Troubleshooting

- **`provision` exits with `LOCAL_STACK_LEDGER_SCHEMA_BEHIND`**: you updated the code but kept an
  old ledger. The local ledger is disposable: `docker compose down -v`, then `up` again.
- **`trader` shows `DEGRADED`** in `docker compose ps`: a public-bar read failed (network or rate
  limit). It retries on the next hourly pass; `CATALYST_SIM_BARS: synthetic` needs no network.
- **Port 8080 is taken**: change the page's `ports` line in `compose.yaml` to e.g.
  `"127.0.0.1:8090:8080"`.
- **Linux, `new-strategy` cannot write to `my-strategies/`**: the container runs as uid 10001;
  run it as yourself: `docker compose run --rm --user "$(id -u):$(id -g)" catalyst new-strategy …`.

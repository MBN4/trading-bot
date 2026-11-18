# Notes for Claude (and anyone picking this repo up)

Read this first, then `README.md` and `RESEARCH_PLAN.md`. This repository is
**public**: never put secrets, passphrases, tokens or personal data in this
file or anywhere else in the repo.

## What this project is

A paper-only BTCUSDT forward test of the fixed SMA 5/20 crossover
(`sma-crossover-paper-v1`). A GitHub Actions workflow processes one
checksum-verified Binance Public Data daily candle per day. The research gate
needs **90 verified contemporaneous observations**. A candle counts as
contemporaneous only if it is committed during the first UTC calendar day
after it closes; anything later is `catch_up` and does not count.

## Hard rules (set by the owner)

- Do **not** change strategy logic, parameters, fees, slippage,
  `research_registry.json`, or the contemporaneous/catch_up counting rules.
- No live trading, exchange connections, broker/wallet access or API keys.
- Never rewrite portfolio events or data lineage. Never hand-edit state or
  event JSON.
- Run `python3 -m unittest discover -s tests` before and after any change;
  all tests must pass.
- Work on a branch and open a PR. Don't push to `main`, and don't merge,
  without the owner's explicit OK.

## Where things live

| Path | Purpose |
|---|---|
| `paper.py`, `daily_paper.py`, `research.py` | Portfolio engine, daily operator (`progress`, `update`, `auto-update`), registry tooling |
| `research_registry.json` | Source of truth for strategy versions, consumed holdouts, viewed-data dates |
| `.github/workflows/daily-btcusdt-paper-update.yml` | The daily run (header comment explains every safeguard) |
| `.github/workflows/bootstrap-forward-portfolio.yml` | One-time bootstrap; already used, refuses to overwrite existing state |
| `paper_portfolios/`, `data/real/*.csv` | Git-ignored runtime state and licensed market data; in CI they live in the Actions cache |

## How the daily workflow is set up (as of 2026-11-18)

- **Schedule:** `17 6 * * *` plus a backup at `43 10 * * *` (UTC). The
  backup run is a no-op (`"fetched": false`) if the candle is already
  committed. Before this change, scheduled 06:00 runs were starting 7–10 hours
  late (13:11–15:40 UTC). Check whether the new times start sooner.
- **State:** persisted in the Actions cache (`btcusdt-daily-paper-state-<run_id>`).
- **Encrypted backup:** every successful run uploads the
  `btcusdt-paper-state-backup` artifact (AES-256 via gpg, 90-day retention),
  encrypted with the repo secret `PAPER_STATE_BACKUP_PASSPHRASE`. The owner
  keeps the passphrase in their password manager. Never ask them to paste it
  into a chat, a file or a command line. To set it, have them paste it into a
  hidden `read -rs` prompt piped to `gh secret set`. Backups from runs
  `44260836856` and `44260902425` (2026-11-18) were encrypted with a discarded
  passphrase and cannot be decrypted. Run `44261339776` onward use the current one.
- **Restore:** run the workflow manually with `restore_from_run_id=<run id>`
  (README: "Restoring state from the encrypted Actions artifact"). Use only the
  newest successful run's backup.
- **Keep-alive:** the `keepalive` job calls the enable-workflow API every run
  so GitHub's 60-day inactivity rule doesn't disable the schedule.
- **Alerts:** the `notify` job opens or comments on an issue labelled
  `daily-run-failure` when `update` fails, and closes it on the next success.
- **Late-run guard:** a run that starts at or after 22:00 UTC still attempts
  the update, then is marked failed.

## Open decisions and caveats (owner's call, not Claude's)

1. **Two separate portfolios exist.** They are different event chains and
   cannot be merged:
   - **GitHub Actions portfolio (live):** built by the one-time bootstrap (all
     `catch_up`), then updated daily. As of the 2026-11-17 candle: **15
     verified contemporaneous observations**.
   - **Owner's laptop portfolio (stopped):** manual updates through
     2026-10-11; 7 contemporaneous, 32 catch_up, 2 unknown. Backed up on the
     owner's laptop. Its record commits (Oct 6 and Oct 7–11 metadata, README,
     RESEARCH_PLAN and registry edits) are on the branch
     `laptop-records-through-2026-10-11` and are **not** on `main`.
   - The owner has not yet decided which portfolio is the official record.
     Don't merge that branch, and don't run `daily_paper.py update` on the
     laptop portfolio, without asking: every new laptop candle would be catch_up.
2. **Records on `main` are stale.** `research_registry.json`
   (`available_fresh_data.through` = 2026-10-05), the README's forward record
   and `RESEARCH_PLAN.md` describe the laptop portfolio up to 2026-10-05, not
   the GitHub portfolio. Updating them is a research-record decision for the
   owner.
3. **Never exercised yet:** restoring from a backup artifact, a real
   `daily-run-failure` issue, the late-run guard firing, and the keep-alive's
   long-term effect.

## Checking current status (don't trust the snapshot numbers above)

`gh` must be authenticated. In cloud sessions it may not be; the owner runs
`gh` on their laptop.

```bash
gh run list --repo MBN4/trading-bot --workflow daily-btcusdt-paper-update.yml --limit 10 --json databaseId,event,createdAt,conclusion
```

```bash
gh run view RUN_ID --repo MBN4/trading-bot --log | grep -E '"verified_contemporaneous_observations"|"last_processed_date"|"committed"|"fetched"'
```

```bash
gh issue list --repo MBN4/trading-bot --label daily-run-failure --state all
```

Note on log greps: GitHub prints each step's script before running it, so
grepping for a warning's text (e.g. "State backup skipped") also matches the
script source. Check whether the artifact exists instead.

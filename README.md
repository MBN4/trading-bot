# Trading agent: offline stock and crypto research

This is an **offline, paper-only research tool**. It provides historical walk-forward backtests and persistent forward paper portfolios from local CSV files. It has no broker, exchange, wallet, API-key, network, automatic market-data download, or order-submission code.

## Forward paper portfolios

Forward mode watches a daily OHLCV CSV that you update by appending completed candles. It never sends an order anywhere. Stock and crypto state is separated under:

```text
paper_portfolios/<market>/<SYMBOL>.json
paper_portfolios/<market>/<SYMBOL>.events.jsonl
paper_portfolios/<market>/<SYMBOL>.transaction.json  # exists only during a commit/recovery
paper_portfolios/<market>/<SYMBOL>.lock              # local advisory lock
```

The JSON state stores cash, units, pending signal, fills, settings, current equity, and an exact normalized copy of every processed candle. The JSONL file is an append-only event history containing initialization, accepted or blocked signals, simulated fills, and processed-candle equity. Events have contiguous sequence numbers and a SHA-256 hash chain; state has its own checksum and records the committed event count and head hash. Startup rejects malformed JSON, altered checksums, broken event chains, and state/history checkpoint mismatches. `paper_portfolios/` is ignored by Git because it is runtime state, not a backtest report.

Forward mode requires `date,open,high,low,close,volume`, including for synthetic demonstrations. Initialization treats every existing row as observation/warm-up history: it does not pretend to trade through the past. It may create one pending signal from the final completed candle. If there is no later candle, that signal remains pending without a fill.

Every `init` and `run` must include `--completed-through YYYY-MM-DD`. This is the latest row date that you have independently confirmed is a complete daily candle according to your data provider's documented session-close rule. The tool does not monitor clocks, venues, or provider feeds. If the CSV contains a row later than `--completed-through`, the command fails before changing files. A metadata template for documenting the session-close rule is provided at `data/real/metadata.template.json`.

Mutating portfolio commands use a per-portfolio local advisory lock file. `init`, `run`, and `recover` take an exclusive non-blocking `fcntl.flock` on `paper_portfolios/<market>/<SYMBOL>.lock`; if another process already holds it, the command fails safely before state changes. The OS releases the lock when the owning process exits or is interrupted. The lock prevents two local processes from committing or recovering the same portfolio at once, but it is not a distributed lock and should not be shared across machines or unreliable network filesystems.

### Synthetic demonstration

The bundled samples are synthetic and close-only. Create deterministic, explicitly synthetic OHLCV prefixes for a safe demo:

```bash
python3 make_demo_ohlcv.py crypto_sample.csv /tmp/crypto_demo_initial.csv --rows 159
python3 make_demo_ohlcv.py crypto_sample.csv /tmp/crypto_demo_updated.csv --rows 160
```

Create matching metadata for the synthetic demo:

```bash
printf '%s\n' '{"provider":"bundled synthetic sample","usage_permission":"local repository demo only","symbol":"BTC_USD","market":"crypto","quote_currency":"USD","timezone":"UTC","session_close":"synthetic daily rows are treated as complete after the listed date","retrieval_date":"2026-09-23","adjustment_policy":"not applicable to synthetic demo data"}' > /tmp/crypto_demo_metadata.json
```

Preview initialization without creating state, then initialize:

```bash
python3 paper.py --state-root /tmp/trading-agent-paper init --market crypto --symbol BTC_USD --strategy-version sma-crossover-paper-v1 --dataset-kind synthetic --csv /tmp/crypto_demo_initial.csv --metadata /tmp/crypto_demo_metadata.json --data-source "bundled synthetic sample converted to demo OHLCV" --quote-currency USD --capital-currency USD --completed-through 2025-06-08 --initial 5000 --fee 0.002 --slippage 0.001 --allocation 0.20 --max-daily-loss 0.03 --quantity-step 0.00000001 --min-quantity 0.00000001 --min-notional 10 --preview

python3 paper.py --state-root /tmp/trading-agent-paper init --market crypto --symbol BTC_USD --strategy-version sma-crossover-paper-v1 --dataset-kind synthetic --csv /tmp/crypto_demo_initial.csv --metadata /tmp/crypto_demo_metadata.json --data-source "bundled synthetic sample converted to demo OHLCV" --quote-currency USD --capital-currency USD --completed-through 2025-06-08 --initial 5000 --fee 0.002 --slippage 0.001 --allocation 0.20 --max-daily-loss 0.03 --quantity-step 0.00000001 --min-quantity 0.00000001 --min-notional 10
```

Preview and then process the one newly appended row:

```bash
python3 paper.py --state-root /tmp/trading-agent-paper run --market crypto --symbol BTC_USD --csv /tmp/crypto_demo_updated.csv --completed-through 2025-06-09 --preview
python3 paper.py --state-root /tmp/trading-agent-paper run --market crypto --symbol BTC_USD --csv /tmp/crypto_demo_updated.csv --completed-through 2025-06-09
python3 paper.py --state-root /tmp/trading-agent-paper status --market crypto --symbol BTC_USD --events 20
```

Use `--market stock` and a distinct symbol to create an independent stock portfolio. The market is part of the state path, so identical stock and crypto symbols cannot share state. `--preview` performs full validation and calculation but creates no directories or files and changes no file contents or timestamps. A preview refuses to proceed when an interrupted transaction exists; recovery is always explicit.

### Decision and fill timing

After a completed candle is processed, its close is added to the moving-average history. A resulting buy or sell becomes pending. It can fill only on a later dated candle, using that next candle's **open** adjusted adversely for configured slippage:

- Buy fill: `next open * (1 + slippage)`.
- Sell fill: `next open * (1 - slippage)`.
- Buy budget includes fees. Raw units are `budget / (fill price * (1 + fee))`, then rounded down to the configured quantity step. The actual fee is `rounded units * fill price * fee`, so gross plus fee cannot exceed the budget or available cash.
- Sell fee: `units * fill price * fee`; the long position is sold in full.

A buy's budget is capped at `allocation * signal-close equity` and available cash. The event records the accepted budget and allocation limit. Stock portfolios default to `--quantity-step 1 --min-quantity 1`, so fractional shares are never silently assumed. Crypto defaults to eight decimal places and a minimum of `0.00000001`; set `--quantity-step`, `--min-quantity`, and `--min-notional` at initialization to match the intended venue's paper assumptions. Quantities always round down. A buy that cannot meet minimums is recorded as blocked and is not filled. Sells use exactly the held quantity; invalid zero-unit sells are rejected.

At each new completed close, the daily circuit breaker compares equity with the previous processed close. If the decline exceeds `max_daily_loss`, a newly proposed buy is recorded as `BUY_BLOCKED`; exits remain allowed. It does not liquidate positions, does not cancel a buy accepted on the prior candle, and is not a guaranteed loss cap.

### Updates and recovery

Only append complete rows with dates later than the last processed date, and pass a `--completed-through` date that is at least the CSV's latest date. Before processing anything, forward mode compares every old OHLCV value with its stored history. An unchanged rerun produces no events, fills, directory creation, or file writes. A shortened file, corrected old candle, inserted/backdated row, duplicate date, malformed value, incomplete final row, or unconfirmed final row is rejected before state changes.

Missing calendar dates and weekends are permitted: markets do not all trade every day. A pending signal fills at the open of the next **available later row**, and each candle event records the calendar-day gap. The tool cannot determine whether an absent session is expected; confirm completeness with your data provider before running.

Each commit follows this recoverable procedure:

1. Write and sync a checksummed transaction journal containing the exact new events and target state.
2. Append and sync the hash-chained events.
3. Atomically replace and sync the checksummed state.
4. Remove the transaction journal only after both checkpoints agree.

Normal `run` and `status` commands stop when a transaction journal exists; they never guess whether to keep or discard a fill. Inspect/back up all three files, then complete the exact recorded transaction with:

```bash
python3 paper.py recover --market crypto --symbol BTC_USD
```

Include the same `--state-root` before `recover` when using a non-default root. Recovery verifies the journal checksum and its common event-history prefix, appends only the missing recorded suffix, writes the recorded target state, verifies the final checkpoint, and then removes the journal. If any history diverges, recovery refuses and requires a matching backup.

When history legitimately needs correction:

1. Keep the rejected state and event files unchanged as an audit record.
2. Restore a CSV whose processed prefix exactly matches the portfolio and continue appending, or initialize a new portfolio using a different `--state-root` or symbol identifier.
3. Do not hand-edit state, history, or transaction files. Back up all files together before moving or archiving them.

Paper-state format version 2 is not silently compatible with earlier unverified state. Archive version-1 files and initialize a new portfolio baseline. The journal handles tested process interruptions, but filesystem or hardware failure can still damage multiple files; checks detect that condition, while recovery may require a known-consistent backup.

### Readiness check

Run `readiness` before initializing or updating a forward paper portfolio. It is read-only: it validates inputs and existing portfolio files without writing, recovering, initializing, or changing timestamps.

```bash
python3 paper.py --state-root /tmp/trading-agent-paper readiness --market crypto --symbol BTC_USD --dataset-kind synthetic --csv /tmp/crypto_demo_updated.csv --metadata /tmp/crypto_demo_metadata.json --quote-currency USD --capital-currency USD --completed-through 2025-06-09
```

For real data, copy `data/real/metadata.template.json` beside your CSV and fill every field yourself:

```json
{
  "provider": "",
  "usage_permission": "",
  "symbol": "",
  "market": "",
  "quote_currency": "",
  "timezone": "",
  "session_close": "",
  "retrieval_date": "",
  "adjustment_policy": ""
}
```

The readiness command checks CSV validity, currency consistency, metadata completeness and identity, existing portfolio integrity, pending transaction journals, quantity settings, candle completeness, and whether enough rows exist for the configured slow moving-average window. It exits `0` only when all checks pass and exits `2` with actionable errors when any check fails.

## Quick start with the synthetic samples

```bash
python3 agent.py --market stock --symbol SAMPLE --dataset-kind synthetic --csv stock_sample.csv --data-source "bundled synthetic sample" --quote-currency PKR --capital-currency PKR --initial 5000
python3 agent.py --market crypto --symbol BTC_USD --dataset-kind synthetic --csv crypto_sample.csv --data-source "bundled synthetic sample" --quote-currency USD --capital-currency USD --initial 5000
```

These generated samples test the workflow only. Their returns are not evidence that the strategy is profitable.

## Real historical CSV data

Export daily data that you are permitted to download and use, then normalize it to this exact, case-sensitive header and order:

```csv
date,open,high,low,close,volume
2024-01-02,100.00,104.00,99.00,103.00,125000
```

Requirements:

- At least 80 rows, oldest first, with one unique ISO `YYYY-MM-DD` date per row.
- No blank, nonnumeric, or non-finite values.
- Open, high, low, and close must be positive; volume may be zero but not negative.
- High must be at least open and close; low must be no greater than open and close.
- One consistent quote currency and one consistent daily-session convention throughout the file.

The older `date,close` format remains accepted for the bundled synthetic samples and simple regression fixtures. Use OHLCV for real-data research. The strategy currently makes decisions from close prices only; preserving OHLCV allows validation and later execution-model improvements.

Example with a CSV you supplied:

```bash
python3 agent.py --market stock --symbol YOUR_SYMBOL --dataset-kind real --csv data/real/permitted_stock_daily.csv --data-source "provider and export description" --quote-currency PKR --capital-currency PKR --initial 500000 --fee 0.002 --slippage 0.001 --folds 3

python3 agent.py --market crypto --symbol BTC_USD --dataset-kind real --csv data/real/permitted_btc_usd_daily.csv --data-source "provider and export description" --quote-currency USD --capital-currency USD --initial 5000 --fee 0.002 --slippage 0.001 --folds 3
```

`--data-source` is a report label, not a downloader. `--dataset-kind real` requires six-column OHLCV and rejects the bundled sample filenames; `--dataset-kind synthetic` is required for the samples. The report records this classification with the market, symbol, source, quote currency, capital currency, complete input date range, row count, fees, slippage, and period boundaries. Capital and quote currencies must match exactly after case normalization. The program rejects mismatches and never performs implicit FX or stablecoin conversion.

### Obtaining data lawfully

- Prefer a CSV export from your broker, exchange account, or data vendor when its terms grant you the intended personal research use. Keep the provider name in `--data-source` and retain its license/terms with your research records.
- Public download availability does not automatically grant redistribution rights. Do not commit or republish downloaded market data unless its license expressly permits that.
- Do **not** scrape the Pakistan Stock Exchange website or redistribute PSX market data. Obtain PSX data through a broker or vendor whose agreement permits your use, and provide the resulting local CSV yourself.
- Crypto exchanges and market-data vendors may require an account, a paid plan, acceptance of API terms, or availability in your country. Check fees, retention limits, redistribution terms, and Pakistan eligibility before obtaining data.

Place licensed or personally exported files in `data/real/`. CSVs in that directory are ignored by Git by default, keeping provider data separate from the bundled samples and reducing accidental redistribution. Copy `data/real/metadata.template.json` and fill in the provider, usage permission, symbol, market, quote currency, timezone, session close, retrieval date, and split/dividend adjustment policy from your source records. Do not place credentials in this project.

### Verified source review and first real dataset (checked 2026-09-23)

#### Crypto

[Binance Public Data](https://github.com/binance/binance-public-data/) is currently the usable free source for this project. Binance describes the archive as public market data that anyone can download, documents direct `curl`/`wget` and programmatic retrieval, publishes daily files the next day and monthly files on the first Monday of the month, and provides SHA-256 checksums. Its official [market-data guide](https://www.binance.com/en/academy/articles/how-to-retrieve-binance-spot-market-data-efficiently) specifically presents historical data for analysis and programmatic retrieval. The archive needs no account, API key, or payment. Pair history varies; inspect the archive for the exact symbol and interval. Pakistan users can reach the public archive without creating or funding an exchange account, but provider availability and terms can change.

The current real dataset is `data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.csv`: 1,461 completed UTC daily BTCUSDT spot candles spanning four complete calendar years. It includes the 2021 rise, the 2022 decline, the 2023 consolidation and recovery, and the 2024 rise. Its companion `data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.metadata.json` records all 48 source URLs and published archive checksums, the normalized-file checksum, retrieval date, session rule, and transformation. Every archive checksum was verified. The local copy is for personal research only; do not commit or redistribute it. No downloader, exchange connection, account, credential, or API key was added to the project.

Reproduce the fixed archive download and checksum verification outside the application:

```bash
PROJECT_ROOT="$(pwd)"
mkdir -p /tmp/trading-agent-binance-2021-2024
cd /tmp/trading-agent-binance-2021-2024
for year in 2021 2022 2023 2024; do
  for month in 01 02 03 04 05 06 07 08 09 10 11 12; do
    file="BTCUSDT-1d-${year}-${month}.zip"
    curl --fail --location --output "$file" "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/$file"
    curl --fail --location --output "$file.CHECKSUM" "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/$file.CHECKSUM"
  done
done
sha256sum -c *.CHECKSUM
```

Normalize the six required columns. Binance spot timestamps before 2025 are milliseconds; the conditional also handles the documented microsecond format introduced in 2025:

```bash
awk -F, 'BEGIN { OFS=","; print "date,open,high,low,close,volume" }
  { divisor = ($1 > 9999999999999 ? 1000000 : 1000);
    print strftime("%Y-%m-%d", $1 / divisor, 1), $2, $3, $4, $5, $6 }' \
  < <(for archive in $(find . -maxdepth 1 -name 'BTCUSDT-1d-*.zip' -printf '%f\n' | sort); do unzip -p "$archive"; done) \
  > "$PROJECT_ROOT/data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.csv"
```

Reproduce validation and the historical walk-forward run:

```bash
python3 paper.py --state-root /tmp/trading-agent-real-check readiness --market crypto --symbol BTCUSDT --dataset-kind real --csv data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.csv --metadata data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.metadata.json --quote-currency USDT --capital-currency USDT --completed-through 2024-12-31

python3 agent.py --market crypto --symbol BTCUSDT --dataset-kind real --csv data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.csv --data-source "Binance Public Data monthly spot 1d archives, 2021-01 through 2024-12" --quote-currency USDT --capital-currency USDT --initial 5000 --fee 0.002 --slippage 0.001 --folds 3
```

The `5000` in this command means **5,000 USDT**, not PKR 5,000. BTCUSDT prices and all reported portfolio values remain in USDT. No reliable historical PKR/USDT conversion series was supplied or used, and the program performs no currency conversion. A PKR budget cannot be compared directly with this simulation.

Other official crypto sources reviewed were not used. [Kraken's OHLC endpoint](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) is public and requires no key, but returns at most 720 recent candles and always includes the current incomplete candle; Kraken's general terms do not give sufficiently clear permission for automated extraction for this use. [Coinbase Market Data Terms](https://www.coinbase.com/legal/market_data) allow some personal/research use but prohibit using the data to validate or benchmark algorithms, which directly conflicts with this project. Access without credentials is not by itself permission.

#### Pakistan stocks

The official [PSX historical-data portal](https://dps.psx.com.pk/historical) is accessible in Pakistan without an account or payment and exposes equity history and daily downloads. Its terms allow one unaltered copy for personal, non-commercial use, but also prohibit editing, adapting, compiling, creating derivatives, and systematic retrieval without prior written permission. Because this project must combine and normalize rows into its OHLCV schema, the free portal is **not treated as permission** for a project-ready CSV.

PSX's official [Data Services & Vending](https://www.psx.com.pk/psx/product-and-services/data-services-vending) page says end-of-day data contains open, high, low, close, and volume, and that historical data is available from all past trading sessions for strategy backtests. It requires a PSX license or authorization; no public price is stated. Email `marketdatarequest@psx.com.pk` and request written permission for personal, local, non-commercial algorithm backtesting and forward paper trading, including permission to download, store, combine, and normalize daily rows. Ask for the available date range, delivery format, corporate-action policy, fee, and whether an individual account is required. Alternatively use a PSX-authorized vendor only after its agreement expressly grants the same uses. Do not send credentials or paid data to this repository.

Once permission is documented, place the provider's file at `data/real/permitted_stock_daily.csv`, normalize it to the required header, and fill a copy of `data/real/metadata.template.json`. Record the provider and exact permission, symbol, `stock`, `PKR`, `Asia/Karachi`, the provider's documented session close/finalization rule, retrieval date, and its split/dividend adjustment policy. This project does not scrape PSX pages or assume that displayed/downloadable data may be transformed.

## Historical backtest method and reports

The first 40% of rows is initial training history. The next 40% is divided into chronological validation folds. Each validation fold chooses a moving-average pair using only rows before that fold. All candidate pairs are also compared across the completed validation folds, and the pair with the best mean validation return is locked before the final 20% holdout is run. The holdout is never used for selection, retuning, or cost-scenario settings. Each period starts with the configured initial cash and is independent, so returns are not compounded across periods.

A signal observed after a daily close executes at the next available row's open. Adverse slippage is applied to that open: buys use `open * (1 + slippage)` and sells use `open * (1 - slippage)`. Fees apply on both sides. Close-only synthetic fixtures retain a close fallback for regression compatibility; real datasets require OHLCV and therefore use the open.

Each period reports dates, selected windows, fee and slippage rates, strategy return, cash return, cost-matched buy-and-hold return, maximum drawdowns, trade counts, trades, and daily equity. Buy-and-hold commits all capital at the first period open with the same entry fee and slippage and is marked at the final close; the strategy allocates only 20% per entry, so their risk exposure is intentionally different. Cash remains at 0% because interest and stablecoin yield are not modeled. Backtest results are written as `results/<market>_<symbol>_<dataset-kind>.json`.

### BTCUSDT 2021-2024 evaluation

Base assumptions are 5,000 USDT initial capital, 0.20% fee per side, 0.10% adverse slippage, 20% allocation per entry, and no interest on cash. These are paper assumptions, not a quote from a particular account or venue.

| Role | Dates | Windows | Fee/slip | Strategy | Cash | Buy and hold | Strategy drawdown | Trades |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Validation 1 | 2022-08-08 to 2023-02-18 | 15/40 | 0.20%/0.10% | -1.05% | 0.00% | 5.97% | -7.81% | 7 |
| Validation 2 | 2023-02-19 to 2023-09-01 | 15/40 | 0.20%/0.10% | -3.16% | 0.00% | 4.45% | -3.27% | 6 |
| Validation 3 | 2023-09-02 to 2024-03-13 | 15/40 | 0.20%/0.10% | 22.25% | 0.00% | 182.32% | -4.85% | 3 |
| Then-sealed holdout (now consumed) | 2024-03-14 to 2024-12-31 | 10/30 locked | 0.20%/0.10% | -0.82% | 0.00% | 27.68% | -8.66% | 12 |

The worst strategy return was validation period 2 at -3.16%; the largest strategy drawdown was the final holdout at -8.66%. Holdout sensitivity with the same locked 10/30 windows was -1.29% at 0.30% fee and 0.20% slippage, and -1.99% at 0.50% fee and 0.30% slippage. The strategy lost to cash in three of four reported periods and lost badly to buy-and-hold in every period. The positive validation result occurred during a strong rise and captured only a small part of it.

This is not convincing evidence that the strategy works. Continue forward paper testing; do not use these results as a reason to trade live or expect profit.

### Preregistered strategy comparison

The comparison protocol was frozen in `RESEARCH_PLAN.md` before retrieving or evaluating the new holdout. It keeps the SMA crossover baseline and adds only two alternatives: a price/SMA trend filter and a Donchian breakout. The plan fixes every parameter, cost, boundary, selection rule, and success criterion.

The later dataset is `data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.csv`, with 608 completed UTC candles. Its metadata records all 20 source archive URLs and verified SHA-256 values. Calendar year 2025 is confirmation data; `2026-01-01` through `2026-08-31` was the preregistered sealed holdout, was opened once after the plan and boundary tests were frozen, and is now consumed.

Download and verify the fixed later archives:

```bash
PROJECT_ROOT="$(pwd)"
mkdir -p /tmp/trading-agent-binance-2025-2026
cd /tmp/trading-agent-binance-2025-2026
for year_month in 2025-01 2025-02 2025-03 2025-04 2025-05 2025-06 2025-07 2025-08 2025-09 2025-10 2025-11 2025-12 2026-01 2026-02 2026-03 2026-04 2026-05 2026-06 2026-07 2026-08; do
  file="BTCUSDT-1d-${year_month}.zip"
  curl --fail --location --output "$file" "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/$file"
  curl --fail --location --output "$file.CHECKSUM" "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/$file.CHECKSUM"
done
sha256sum -c *.CHECKSUM
```

Normalize the later archives:

```bash
awk -F, 'BEGIN { OFS=","; print "date,open,high,low,close,volume" }
  { divisor = ($1 > 9999999999999 ? 1000000 : 1000);
    print strftime("%Y-%m-%d", $1 / divisor, 1), $2, $3, $4, $5, $6 }' \
  < <(for archive in $(find . -maxdepth 1 -name 'BTCUSDT-1d-*.zip' -printf '%f\n' | sort); do unzip -p "$archive"; done) \
  > "$PROJECT_ROOT/data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.csv"
```

Then reproduce readiness and comparison:

```bash
python3 paper.py --state-root /tmp/trading-agent-later-readiness readiness --market crypto --symbol BTCUSDT --dataset-kind real --csv data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.csv --metadata data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.metadata.json --quote-currency USDT --capital-currency USDT --completed-through 2026-08-31

python3 compare_strategies.py --development-csv data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.csv --development-metadata data/real/binance_btcusdt_daily_2021-01-01_2024-12-31.metadata.json --later-csv data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.csv --later-metadata data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.metadata.json
```

Every development candidate, including failures:

| Strategy | Parameters | 2022 | 2023 | 2024 | Mean | Trades | Champion |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| SMA crossover | 5/20 | -14.39% | 13.23% | 11.49% | 3.44% | 64 | No |
| SMA crossover | 10/30 | -11.34% | 14.18% | 11.29% | 4.71% | 47 | No |
| SMA crossover | 15/40 | -8.25% | 14.56% | 10.85% | 5.72% | 27 | Yes |
| Price/SMA | 100 | -4.71% | 14.49% | 12.26% | 7.35% | 40 | No |
| Price/SMA | 150 | -1.65% | 15.84% | 16.50% | 10.23% | 24 | Yes |
| Price/SMA | 200 | 0.00% | 16.28% | 14.17% | 10.15% | 16 | No |
| Donchian | 20/10 | -10.25% | 15.47% | 13.54% | 6.25% | 48 | No |
| Donchian | 55/20 | -5.84% | 13.66% | 13.05% | 6.96% | 21 | Yes |
| Donchian | 100/40 | 0.00% | 3.54% | 11.05% | 4.86% | 12 | No |

Champion confirmation and then-sealed holdout results (now consumed) at the base 0.20% fee and 0.10% slippage:

| Champion | 2025 confirmation | 2026 holdout | Cash | Buy and hold | Holdout drawdown | Trades | Moderate costs | Higher costs | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| SMA 15/40 | -0.71% | -0.21% | 0.00% | -10.61% | -4.27% | 9 | -0.57% | -1.09% | No |
| Price/SMA 150 | -2.25% | 0.97% | 0.00% | -10.61% | -2.40% | 7 | 0.69% | 0.27% | No |
| Donchian 55/20 | -4.67% | 0.59% | 0.00% | -10.61% | -2.61% | 5 | 0.39% | 0.09% | No |

All champions failed the preregistered requirement for a positive 2025 confirmation year. The positive 2026 results for price/SMA and Donchian are small and do not override that failure. No strategy earned further forward paper testing under the frozen criteria. Keep this as paper research; do not progress to live trading.

The comparison report is `results/crypto_BTCUSDT_strategy_comparison.json`. All figures are USDT results from a 5,000 USDT simulation. They are not results for PKR 5,000, and no PKR/USDT conversion was attempted.

## Controlled paper research loop

`research_registry.json` records every governed strategy version, its exact rules and parameters, development sources, periods already viewed, decisions, proposals, and fresh-data status. The 2024 and 2026 holdouts are marked `consumed_holdout`; they must never again be called untouched. The current status is deliberately conservative:

- Active paper baseline: `sma-crossover-paper-v1`, fixed at `5/20`.
- Evaluated failures: SMA `15/40`, price/SMA `150`, and Donchian `55/20` comparison champions.
- Proposals: none.
- Fresh registered data: none after the consumed boundary `2026-08-31`.
- Overall status: `paper_research_only_no_strategy_passed`.

Show the read-only research status:

```bash
python3 research.py status
```

New forward portfolios must identify the registered active version. The registry rejects failed or proposed versions and rejects parameters that differ from the fixed version:

```bash
python3 paper.py init ... --strategy-version sma-crossover-paper-v1 --fast 5 --slow 20
```

Report forward performance without changing portfolio or registry files:

```bash
python3 research.py performance --state-root paper_portfolios --market crypto --symbol BTCUSDT
```

The report uses the exact candles stored in checksummed portfolio state and identifies the latest CSV path, source metadata, strategy version, rules, parameters, fee, and slippage. Over the same first-forward-row through last-forward-row dates it reports:

- Strategy return, maximum drawdown, fees paid, and simulated fill count.
- Cash return and drawdown, both zero because interest is not modeled.
- Buy-and-hold return, maximum drawdown, entry fee, and trade count using the same fee and slippage.

Performance labels prevent overstatement:

- `demo_only_synthetic`: fixture output is a workflow demonstration, never evidence.
- `insufficient_no_forward_rows`: no rows arrived after initialization.
- `insufficient_short_forward_period`: fewer than 90 new completed rows.
- `insufficient_trade_count`: fewer than two simulated fills.
- `reused_period_exploratory_only`: dates overlap a consumed holdout.
- `unversioned_legacy_state`: the portfolio predates strategy-version tracking.
- `fresh_forward_evidence_not_profit_claim`: minimum bookkeeping gates passed on real, newly registered dates; this still does not promise profit or justify live trading.

### Recording a proposal

Record a proposed change before evaluating it. This command changes only the local registry; it does not edit strategy code, alter the active version, touch a portfolio, or deploy anything:

```bash
python3 research.py propose \
  --id example-volatility-filter-v1 \
  --base-version sma-crossover-paper-v1 \
  --rules "Keep the registered SMA rule, but permit entry only when the preregistered volatility condition is met." \
  --parameters '{"fast":5,"slow":20,"volatility_window":30,"maximum_volatility":0.04}' \
  --reasoning "Test whether a fixed volatility gate reduces whipsaw without using consumed periods for selection." \
  --criteria '{"minimum_new_rows":180,"positive_periods_required":2,"maximum_drawdown_pct":-15,"positive_under_moderate_costs":true}' \
  --future-data-start 2026-09-01
```

Proposal IDs are unique. Their future data must begin after the last consumed date, criteria must be predefined, and the base must be the active paper strategy. Proposals remain `proposed_not_active` with `automatic_deployment_allowed: false`. There is intentionally no automatic promotion command.

### Synthetic reporting demo

The synthetic demonstration earlier in this README can be followed by:

```bash
python3 research.py performance --state-root /tmp/trading-agent-paper --market crypto --symbol BTC_USD
```

After its single appended synthetic row, the report is labeled `demo_only_synthetic`, `eligible_for_performance_claim: false`, and also records that one row is below the 90-row minimum and one fill is below the two-fill minimum. Its calculated returns are useful only for checking arithmetic and command wiring.

For genuinely new evidence, supply complete permitted real candles after `2026-08-31`, preregister the question before inspecting outcomes, collect at least 90 new forward rows and two fills, and compare with cash and cost-matched buy-and-hold. These are minimum reporting gates, not guarantees. No amount of automated learning can eliminate market losses.

## Daily BTCUSDT paper checklist

This workflow is manual, local, and paper-only. It never downloads data or submits an order. Run it only after the Binance UTC daily candle has closed and the permitted public archive plus its published `.CHECKSUM` are available. Obtain those two files manually using the verified Binance Public Data process above. Never use an in-progress API response or the current UTC day's candle.

The first time only, initialize the fixed baseline from the verified data through `2026-08-31`:

```bash
python3 paper.py --state-root paper_portfolios init --market crypto --symbol BTCUSDT --strategy-version sma-crossover-paper-v1 --dataset-kind real --csv data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.csv --metadata data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.metadata.json --data-source "Binance Public Data, manually verified archives" --quote-currency USDT --capital-currency USDT --completed-through 2026-08-31 --initial 5000 --fast 5 --slow 20 --fee 0.002 --slippage 0.001 --allocation 0.20 --max-daily-loss 0.03 --quantity-step 0.00000001 --min-quantity 0.00000001 --min-notional 10
```

For each day, set these values from the manually obtained, completed Binance archive. `ARCHIVE_SHA256` must equal the official `.CHECKSUM`; all OHLCV values come from that archive. This example date is the first possible date after the current dataset, not a claim that its archive is available:

```bash
export CANDLE_DATE=2026-09-01
export RETRIEVAL_DATE=YYYY-MM-DD
export ARCHIVE_URL=https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1d/BTCUSDT-1d-2026-09-01.zip
export ARCHIVE_FILE=/path/to/BTCUSDT-1d-2026-09-01.zip
export CHECKSUM_FILE=/path/to/BTCUSDT-1d-2026-09-01.zip.CHECKSUM
export ARCHIVE_SHA256=REPLACE_WITH_PUBLISHED_SHA256
export OPEN=REPLACE_FROM_ARCHIVE
export HIGH=REPLACE_FROM_ARCHIVE
export LOW=REPLACE_FROM_ARCHIVE
export CLOSE=REPLACE_FROM_ARCHIVE
export VOLUME=REPLACE_FROM_ARCHIVE
export SOURCE_CSV=data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.csv
export SOURCE_METADATA=data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.metadata.json
export NEXT_CSV=data/forward/binance_btcusdt_daily_2025-01-01_${CANDLE_DATE}.csv
export NEXT_METADATA=data/forward/binance_btcusdt_daily_2025-01-01_${CANDLE_DATE}.metadata.json
```

Verify the archive against the separately obtained published checksum. Do not continue unless the hashes printed by these commands are exactly equal:

```bash
cat "$CHECKSUM_FILE"
sha256sum "$ARCHIVE_FILE"
```

Create a new immutable dataset version, then inspect its last rows and provenance. This synchronous helper does not download, trade, tune, or schedule anything. It requires explicit completion, rejects duplicates and date gaps, validates OHLCV, refuses to overwrite outputs, and records source and parent hashes:

```bash
python3 append_daily_candle.py --source-csv "$SOURCE_CSV" --source-metadata "$SOURCE_METADATA" --output-csv "$NEXT_CSV" --output-metadata "$NEXT_METADATA" --date "$CANDLE_DATE" --open "$OPEN" --high "$HIGH" --low "$LOW" --close "$CLOSE" --volume "$VOLUME" --source-url "$ARCHIVE_URL" --source-sha256 "$ARCHIVE_SHA256" --retrieval-date "$RETRIEVAL_DATE" --confirmed-complete
tail -n 2 "$NEXT_CSV"
python3 -m json.tool "$NEXT_METADATA"
```

Run readiness and preview with the exact confirmed date:

```bash
python3 paper.py --state-root paper_portfolios readiness --market crypto --symbol BTCUSDT --dataset-kind real --csv "$NEXT_CSV" --metadata "$NEXT_METADATA" --quote-currency USDT --capital-currency USDT --completed-through "$CANDLE_DATE" --fast 5 --slow 20 --quantity-step 0.00000001 --min-quantity 0.00000001 --min-notional 10
python3 paper.py --state-root paper_portfolios run --market crypto --symbol BTCUSDT --csv "$NEXT_CSV" --metadata "$NEXT_METADATA" --completed-through "$CANDLE_DATE" --preview
```

Continue only if readiness says `"ready": true`, preview contains exactly one new `CANDLE_PROCESSED` event, and any fill follows a signal from the prior completed close. Commit once, then inspect the append-only history and read-only comparison:

```bash
python3 paper.py --state-root paper_portfolios run --market crypto --symbol BTCUSDT --csv "$NEXT_CSV" --metadata "$NEXT_METADATA" --completed-through "$CANDLE_DATE"
python3 paper.py --state-root paper_portfolios status --market crypto --symbol BTCUSDT --events 10
python3 research.py performance --state-root paper_portfolios --market crypto --symbol BTCUSDT
```

Rerunning the same committed command is idempotent and creates no duplicate fill. A row after `--completed-through`, changed or shortened history, malformed OHLCV, duplicate date, pending transaction, or lock conflict fails before a normal commit. Real portfolios remain `dataset_kind: real`; synthetic output is always `demo_only_synthetic` and cannot support a performance claim.

### Missed days, corrections, and failures

- **Missed day:** add every completed calendar day in order, producing a new CSV/metadata pair at each step. Run readiness and preview on the final version. One commit can process the accumulated rows chronologically, but inspect every proposed event first.
- **Correction before processing:** retain the incorrect version for audit, return to its last correct parent, and create a separately named corrected version with verified provenance. Never overwrite a versioned file.
- **Correction after processing:** do not edit portfolio state, events, or processed data. The immutable-prefix check rejects the correction by design. Preserve the old portfolio, document the correction, and initialize a new clearly named state root from corrected data. Never combine both lineages as one uninterrupted record.
- **Readiness or preview failure:** stop and do not commit. Fix inputs by creating another versioned pair. If interruption leaves a `.transaction.json`, preserve it and run `python3 paper.py --state-root paper_portfolios recover --market crypto --symbol BTCUSDT`; inspect status before retrying. Never delete or hand-edit state, event, lock, or transaction files.

The 90-row gate counts genuinely new completed candles after `2026-08-31`. At one candle per calendar day, day 1 is `2026-09-01` and day 90 is **`2026-11-29`**. That candle is complete only after its UTC close, effectively at the start of `2026-11-30` UTC. This is the earliest possible date, not a claim that those candles exist. All 90 rows must actually be obtained, verified, registered, and previously unseen; meeting the count does not prove profitability.

`--max-daily-loss 0.03` only blocks a new buy on a row after the close-based equity drop is observed. It does not liquidate a held position and cannot guarantee a maximum loss. Gaps, slippage, held positions, market closures, sparse data, and delayed execution can produce larger losses.

## Verification

Run the complete standard-library test suite:

```bash
python3 -m unittest discover -v
```

This remains a small research baseline. Forward runs require your explicit completed-through date and cannot independently verify market closure, provider finalization, or missing sessions. They do not monitor time or fetch missing sessions. Neither mode models spreads beyond configured slippage, intraday fills, liquidity, partial fills, taxes, dividends, splits, delistings, funding, staking, borrow costs, exchange outages, or survivorship bias. Verify corporate-action adjustment and timezone/session definitions with your data provider. Historical, forward-paper, and synthetic results do not establish future or real-world profitability.

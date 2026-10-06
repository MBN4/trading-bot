# BTCUSDT strategy comparison research plan

Plan frozen on 2026-09-23 before downloading, opening, or evaluating the new 2026 final-holdout candles.

## Scope and safety

This is local, long-only, paper-only research. It does not connect to a broker, exchange account, wallet, live order service, or authenticated API. Results are denominated in USDT and are not a conversion of PKR 5,000. No profit claim or live-trading readiness can follow from this experiment.

## Data and boundaries

- Source: permitted Binance Public Data monthly BTCUSDT spot `1d` archives, with every archive checked against its published SHA-256 file.
- Development data: 2021-01-01 through 2024-12-31. The first year supplies indicator history; 2022, 2023, and 2024 are three chronological validation periods. Each period resets to 5,000 USDT.
- Later confirmation data: 2025-01-01 through 2025-12-31.
- Planned sealed final holdout: 2026-01-01 through 2026-08-31, the latest planned complete monthly boundary. It was unseen when this plan was frozen and is now consumed. No candidate, parameter, cost, or criterion may be changed based on its results.
- All signals use data through a completed UTC close and execute at the next available daily open. Current-row high, low, close, and volume are unavailable to that opening decision.

## Candidate strategies

1. **SMA crossover baseline.** Long when the fast close SMA is above the slow close SMA; otherwise cash. Parameter pairs: `5/20`, `10/30`, `15/40`. This preserves the existing strategy as the baseline.
2. **Price/SMA trend filter.** Long when the latest completed close is above its close SMA; otherwise cash. SMA windows: `100`, `150`, `200`. Reason: test whether a slower, simpler regime filter reduces crossover whipsaw while retaining long-only trend exposure.
3. **Donchian breakout.** Enter when the latest completed close exceeds every prior close in the entry lookback; exit when it falls below every prior close in the exit lookback; otherwise retain the prior position state. Entry/exit pairs: `20/10`, `55/20`, `100/40`. Reason: test a price-range breakout rule that is structurally different from moving-average comparisons.

No additional strategies or parameter values will be introduced after the holdout is viewed.

## Selection rule

Every parameter candidate is evaluated separately in the 2022, 2023, and 2024 validation periods. Within each strategy family, select the candidate with the highest arithmetic mean net validation return. Ties are broken by fewer trades, then by the parameter order written above. The three family champions are frozen before evaluating 2025 or 2026. The 2025 confirmation period and 2026 holdout do not select or revise parameters.

## Portfolio and costs

- Initial capital: 5,000 USDT per independent period.
- Long-only, one position at a time, 20% of signal-close equity allocated per entry.
- Base costs: 0.20% fee per side and 0.10% adverse slippage per side.
- Moderate-cost sensitivity: 0.30% fee and 0.20% slippage.
- Higher-cost sensitivity: 0.50% fee and 0.30% slippage.
- Cash benchmark: 0% return; no interest or stablecoin yield.
- Buy-and-hold benchmark: all capital enters at the period's first open with the same entry fee and slippage, then is marked at the final close. Its exposure is larger than the strategy's 20% allocation and that difference must be stated.
- The existing 3% daily-loss gate only blocks a new entry after an observed completed-close loss. It is not a stop-loss.

## Required reporting

Report every parameter candidate and all validation returns, mean return, drawdown, and trade count. For each frozen family champion, report 2025 confirmation and 2026 holdout return, cash return, cost-matched buy-and-hold return, maximum drawdown, trade count, and both higher-cost sensitivities. Failures remain in the report.

## Success criteria

A family champion earns **further forward paper testing only** if all conditions hold:

1. Mean net return across the three development validation periods is positive, with positive return in at least two of three periods.
2. The 2025 confirmation return is positive.
3. The then-sealed 2026 holdout return is positive, maximum drawdown is no worse than -15%, and trade count is between 2 and 30 sides inclusive. This criterion is retained as a historical record; that period is now consumed.
4. The 2026 holdout remains positive under moderate costs.
5. Execution-timing and boundary tests show no look-ahead or holdout use in selection.

Buy-and-hold performance is always reported but is not a pass/fail threshold because it commits 100% of capital while each strategy entry uses 20%. If no champion passes every criterion, the recommendation remains continued paper research, not live trading.

## Post-run record

The plan above was frozen before later-data retrieval. The 20 planned Binance archives were subsequently downloaded, all published checksums passed, readiness passed, and the final holdout was opened once without changing strategies, parameters, costs, selection, or criteria. The 2024 and 2026 holdouts are now explicitly **consumed** in `research_registry.json`; neither may ever be described or reused as untouched evidence.

Family champions selected from 2022-2024 were SMA crossover `15/40`, price/SMA `150`, and Donchian `55/20`. All three had negative 2025 confirmation returns: -0.71%, -2.25%, and -4.67%, respectively. Their sealed 2026 holdout returns were -0.21%, 0.97%, and 0.59%. Therefore none passed every preregistered criterion, even though the latter two remained slightly positive under moderate and higher costs. That holdout is now consumed. No strategy earned the plan's status of further forward paper testing. The project remains research-only and unsuitable for live trading.

## Controlled follow-up

`research_registry.json` is now the source of truth for strategy versions, fixed rules and parameters, development data, viewed periods, consumed holdouts, evaluated failures, proposals, and fresh-data status. The active forward-paper baseline is `sma-crossover-paper-v1` with fixed `5/20` windows. The comparison champions remain `evaluated_failed`; they are not active.

Any future change must first be appended with `research.py propose`, including fixed rules, parameters, reasoning, criteria, and a future-data start after the latest registered/viewed date. A proposal is always `proposed_not_active`, has no evaluated periods, cannot deploy automatically, and does not modify `paper.py` settings. Promotion would require a separate explicit human decision after genuinely new data meets the recorded criteria. Automated learning or repeated parameter search cannot eliminate losses.

## Daily operating protocol

The active `sma-crossover-paper-v1` baseline remains fixed at `5/20`. Daily operation uses only manually obtained, permitted, completed BTCUSDT UTC candles. `daily_paper.py update` is the guided operator: it verifies the supplied official archive and companion checksum, checks exact UTC timestamps and the strict next date, stages an immutable CSV/metadata version, runs readiness and preview, shows proposed events, and requires the human to type `COMMIT`. It then verifies the event chain and an idempotent rerun. It performs no download, strategy change, scheduling, or live trading. `daily_paper.py progress` is the read-only 90-candle counter. Exact commands, backup/restore, and recovery steps are in the README's **Daily BTCUSDT paper checklist**.

Missing days are appended and confirmed one at a time in calendar order; an unavailable or invalid day stops catch-up. A contemporaneous observation is a checksum-verified candle committed during the first UTC calendar day after its UTC close, allowing the provider's next-day publication delay. Later reconstructed decisions and fills remain in simulated accounting but are event-labeled `catch_up` and excluded from the research gate. Unknown legacy timing receives no credit. A provider correction creates a new data lineage; processed history and append-only portfolio events are never rewritten. Readiness failures stop the workflow, interrupted portfolio transactions use explicit `paper.py recover`, and known-consistent local backups cover unrecoverable corruption. Synthetic rehearsals are labeled `synthetic_demo_not_real_evidence` and remain ineligible evidence.

The earliest gate date is dynamic. 35 completed market candles: 5 verified contemporaneous observations, 28 catch-up, and 2 unknown-timing candles. There are 85 qualifying observations remaining. The earliest possible 90th qualifying candle is `2026-12-29`, with review on `2026-12-30` UTC, conditional on every remaining next-day window being met. Eligibility also requires the minimum trade count and all registry rules; it does not establish profit or live-trading readiness.

## Forward observation record

On October 6 UTC, the missing October 2, 3, 4, and 5 candles were processed individually in strict order. Each official Binance Public Data ZIP matched its companion CHECKSUM and exact completed UTC timestamps. Each guided update staged a versioned CSV and metadata, passed readiness and preview, then committed exactly once. Actual commit timestamps determine the classifications below.

| Candle | Actual commit timestamp (UTC) | Classification | Archive SHA-256 |
|---|---|---|---|
| 2026-10-02 | 2026-10-06T06:05:58.913390Z | catch_up | `97f00f9530c1ee2f091bb0fd157c2097e676cb4882c568490b2fed9d17d048da` |
| 2026-10-03 | 2026-10-06T06:06:19.596207Z | catch_up | `4fad26c8ad8e581d66200b523a7b0fc723571cfe6c760f30f16497eb920a5d1b` |
| 2026-10-04 | 2026-10-06T06:06:34.678267Z | catch_up | `7fbfe9a84ad5d0c2936b7cb489ea09f620e0813033a7d40043dd989f614175e1` |
| 2026-10-05 | 2026-10-06T06:06:55.234173Z | contemporaneous | `af132ef68300bdb34acbdab1aec6d44d8b0f54d9e6f64f5f11f6b252f67a7ec7` |

No new simulated fills occurred; the existing three fills remain unchanged. Ending cash is `3974.84865521` USDT, holdings `0.01220450` BTC, and equity `5021.59042012` USDT at close `85766.87`. All 42 events verify, with one candle event per date and no gaps or duplicates. Every update passed its identical rerun without events or portfolio file changes. The final 643-row CSV is `data/real/binance_btcusdt_daily_2025-01-01_2026-10-05.csv`, with SHA-256 `e7bfd5ea470c4e91d441a17839312cff69cb09b84e1425ed09cd79a5725a5929` and companion `.metadata.json`; the October 2–4 versions are retained.

35 completed market candles: 5 verified contemporaneous observations, 28 catch-up, and 2 unknown-timing candles. There are 85 qualifying observations remaining. The earliest possible 90th qualifying candle is `2026-12-29`, with review on `2026-12-30` UTC, conditional on every remaining next-day window being met.

The fixed SMA 5/20 strategy, fees, slippage, allocation, quantity limits, and all other paper settings are unchanged. October 6 is incomplete and was not processed. Status remains `insufficient_short_forward_period`; this is paper-only bookkeeping and no profitability claim. The guided workflow is unchanged: stop at any unavailable or invalid ZIP/CHECKSUM pair and never skip ahead.

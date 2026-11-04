# Archived orphan dataset-version metadata (2026-11-04)

These 13 `.metadata.json` files (2026-09-23 through 2026-10-05) were committed
to `data/real/` from the project's earlier narrated research log, but no
matching CSV or forward portfolio was ever actually committed alongside them
(`data/real/*.csv` and `paper_portfolios/` are both git-ignored runtime/data
paths, and no portfolio existed in this repository as of 2026-11-03). They are
documentation of a prior session's described workflow, not a committed,
reproducible dataset lineage.

They were moved here, unmodified, rather than deleted or overwritten, because
`daily_paper.py` refuses to write a new versioned dataset file over an
existing path at those same dates, and a newly bootstrapped real forward
portfolio (see `bootstrap_forward_portfolio.py`) needed to create its own
versioned files at those paths from freshly downloaded and checksum-verified
Binance archives. Moving preserves the old record instead of discarding it,
per this project's stated policy of never overwriting a versioned file.

The live, operative dataset versions for these dates after 2026-11-04 are
whatever `bootstrap_forward_portfolio.py` / `daily_paper.py` wrote to
`data/real/` as part of the actual running forward paper portfolio.

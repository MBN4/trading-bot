"""One-time bootstrap: build the real BTCUSDT base dataset and initialize the
forward paper portfolio, then catch it up to the latest completed UTC day.

This reproduces, deterministically and non-interactively, exactly the manual
steps already documented in the README ("Advanced manual reference") and
RESEARCH_PLAN.md: download the permitted Binance Public Data monthly
archives already recorded in data/real/binance_btcusdt_daily_2025-01-01_2026-08-31.metadata.json,
verify every one against its published checksum AND the checksum already
recorded in that committed metadata file (so this cannot silently diverge
from the project's existing provenance record), normalize them into the
required OHLCV CSV, verify the resulting file's hash against the metadata's
recorded normalized_csv_sha256, initialize the portfolio with the registry's
fixed active strategy settings, and then repeatedly call
`daily_paper.py auto-update --yes` to catch up one real candle at a time
until no further archive is published yet.

It performs no network access beyond the same public, documented Binance
Public Data endpoint the rest of this project already uses, no broker/
exchange/order connection, and no strategy or parameter change. Because
every candle here is being committed well after its own UTC close, all but
possibly the very last one will be classified `catch_up`, not
`contemporaneous` -- exactly like the project's own prior recorded catch-up
runs (see README, "Forward paper record"). It does not shorten the
90-observation gate; it only replaces several individual manual sessions
with one run.

Run only once, when no forward portfolio exists yet (`paper_portfolios/crypto/BTCUSDT.json`
missing). If a portfolio already exists, use `daily_paper.py auto-update` or
`daily_paper.py update` directly instead.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

from daily_paper import OperatorError, download, file_paths


ROOT = Path(__file__).resolve().parent
BASE_CSV_NAME = "binance_btcusdt_daily_2025-01-01_2026-08-31.csv"
BASE_METADATA_NAME = "binance_btcusdt_daily_2025-01-01_2026-08-31.metadata.json"
INIT_COMPLETED_THROUGH = date(2026, 8, 31)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def kline_rows(archive_bytes: bytes) -> list[list[str]]:
    import io

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
        members = [name for name in bundle.namelist() if not name.endswith("/")]
        if len(members) != 1:
            raise OperatorError("Monthly archive must contain exactly one CSV file")
        text = bundle.read(members[0]).decode("utf-8")
    rows = [line.split(",") for line in text.splitlines() if line.strip()]
    return rows


def normalized_row(row: list[str]) -> str:
    open_time = int(row[0])
    divisor = 1_000_000 if open_time > 9_999_999_999_999 else 1_000
    day = datetime.fromtimestamp(open_time / divisor, tz=timezone.utc).date()
    return ",".join([day.isoformat(), row[1], row[2], row[3], row[4], row[5]])


def build_base_dataset(metadata: dict, output_csv: Path) -> None:
    lines = ["date,open,high,low,close,volume"]
    for entry in metadata["source_archives"]:
        url = entry["url"]
        checksum_url = f"{url}.CHECKSUM"
        import tempfile

        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            archive_path = Path(temp_dir) / "archive.zip"
            checksum_path = Path(temp_dir) / "archive.CHECKSUM"
            download(url, archive_path)
            download(checksum_url, checksum_path)
            parts = checksum_path.read_text(encoding="ascii").strip().split()
            if len(parts) < 1:
                raise OperatorError(f"Malformed CHECKSUM for {url}")
            published = parts[0].lower()
            actual = sha256_file(archive_path)
            if actual != published:
                raise OperatorError(
                    f"Archive checksum mismatch for {url}: published {published}, got {actual}"
                )
            if actual != entry["sha256"].lower():
                raise OperatorError(
                    f"Archive {url} no longer matches the checksum already recorded in "
                    f"{BASE_METADATA_NAME}: recorded {entry['sha256']}, downloaded {actual}. "
                    "Stop: the official archive may have changed since this project's own "
                    "provenance record was written."
                )
            for row in kline_rows(archive_path.read_bytes()):
                lines.append(normalized_row(row))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_csv.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def run(command: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise OperatorError((result.stderr or result.stdout).strip())
    return result


def bootstrap(state_root: Path) -> dict:
    paths = file_paths(state_root)
    if paths.state.exists():
        raise OperatorError(f"Portfolio already exists at {paths.state}; bootstrap is one-time only")

    metadata_path = ROOT / "data" / "real" / BASE_METADATA_NAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    base_csv = ROOT / "data" / "real" / BASE_CSV_NAME
    if base_csv.exists():
        raise OperatorError(f"{base_csv} already exists; refusing to overwrite it")

    build_base_dataset(metadata, base_csv)
    actual_csv_hash = sha256_file(base_csv)
    if actual_csv_hash != metadata["normalized_csv_sha256"]:
        raise OperatorError(
            f"Rebuilt {BASE_CSV_NAME} does not match the project's recorded "
            f"normalized_csv_sha256 ({metadata['normalized_csv_sha256']}); got {actual_csv_hash}. "
            "Stop: do not initialize a portfolio from an unverified dataset."
        )

    run([
        sys.executable, str(ROOT / "paper.py"), "--state-root", str(state_root),
        "init", "--market", "crypto", "--symbol", "BTCUSDT",
        "--strategy-version", "sma-crossover-paper-v1", "--dataset-kind", "real",
        "--csv", str(base_csv), "--metadata", str(metadata_path),
        "--data-source", "Binance Public Data, manually verified archives (bootstrap-reproduced)",
        "--quote-currency", "USDT", "--capital-currency", "USDT",
        "--completed-through", INIT_COMPLETED_THROUGH.isoformat(), "--initial", "5000",
        "--fast", "5", "--slow", "20", "--fee", "0.002", "--slippage", "0.001",
        "--allocation", "0.20", "--max-daily-loss", "0.03",
        "--quantity-step", "0.00000001", "--min-quantity", "0.00000001", "--min-notional", "10",
    ])

    committed_days = []
    max_iterations = 800
    for _ in range(max_iterations):
        result = run([
            sys.executable, str(ROOT / "daily_paper.py"), "auto-update",
            "--state-root", str(state_root),
        ])
        payload = json.loads(result.stdout)
        if not payload.get("committed"):
            break
        committed_days.append(payload["date"])

    return {
        "base_csv_sha256": actual_csv_hash,
        "initialized_through": INIT_COMPLETED_THROUGH.isoformat(),
        "caught_up_days": committed_days,
        "caught_up_count": len(committed_days),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=ROOT / "paper_portfolios")
    args = parser.parse_args()
    try:
        summary = bootstrap(args.state_root.resolve())
    except OperatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

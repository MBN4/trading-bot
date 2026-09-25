#!/usr/bin/env python3
"""Guided, local-only BTCUSDT forward-paper operation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from append_daily_candle import append_version
from paper import PortfolioLock, load_portfolio, portfolio_paths


ROOT = Path(__file__).resolve().parent
GATE_START = date(2026, 9, 1)
GATE_ROWS = 90
OFFICIAL_BASE = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1d"


class OperatorError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise OperatorError(f"Expected a JSON object in {path}")
    return value


def archive_day(archive: Path) -> date:
    prefix = "BTCUSDT-1d-"
    if not archive.name.startswith(prefix) or not archive.name.endswith(".zip"):
        raise OperatorError("Archive name must be BTCUSDT-1d-YYYY-MM-DD.zip")
    try:
        return date.fromisoformat(archive.name[len(prefix) : -4])
    except ValueError as exc:
        raise OperatorError("Archive filename does not contain a valid ISO date") from exc


def verify_archive(archive: Path, checksum_file: Path) -> tuple[date, str, list[str]]:
    if not archive.is_file() or not checksum_file.is_file():
        raise OperatorError("Both the ZIP and its companion CHECKSUM file are required")
    day = archive_day(archive)
    parts = checksum_file.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or parts[1].lstrip("*") != archive.name:
        raise OperatorError("CHECKSUM must contain the SHA-256 and exact archive filename")
    expected = parts[0].lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise OperatorError("CHECKSUM does not contain a valid SHA-256 digest")
    actual = sha256(archive)
    if actual != expected:
        raise OperatorError(f"Archive checksum mismatch: expected {expected}, got {actual}")

    try:
        with zipfile.ZipFile(archive) as bundle:
            members = [name for name in bundle.namelist() if not name.endswith("/")]
            if len(members) != 1:
                raise OperatorError("Archive must contain exactly one CSV file")
            rows = list(csv.reader(bundle.read(members[0]).decode("utf-8").splitlines()))
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise OperatorError(f"Cannot read archive CSV: {exc}") from exc
    if len(rows) != 1 or len(rows[0]) != 12:
        raise OperatorError("Daily archive must contain exactly one 12-column kline row")
    row = rows[0]
    try:
        open_raw, close_raw = int(row[0]), int(row[6])
        for index in range(1, 6):
            float(row[index])
    except ValueError as exc:
        raise OperatorError("Archive kline contains invalid numeric values") from exc
    scale = 1_000_000 if open_raw >= 10**15 else 1_000
    expected_open = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp() * scale)
    expected_close = int(
        datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp() * scale
    ) - 1
    if open_raw != expected_open or close_raw != expected_close:
        raise OperatorError("Archive timestamps do not describe the named complete UTC daily candle")
    if datetime.now(timezone.utc).date() <= day:
        raise OperatorError(f"{day.isoformat()} is not a completed UTC candle yet")
    return day, actual, row


def file_paths(state_root: Path) -> object:
    state_path, events_path, transaction_path = portfolio_paths(state_root, "crypto", "BTCUSDT")
    return SimpleNamespace(
        state=state_path, events=events_path, transaction=transaction_path,
        lock=state_root / "crypto" / "BTCUSDT.lock",
    )


def paths_for(state_root: Path) -> tuple[dict, object]:
    paths = file_paths(state_root)
    if not paths.state.exists():
        raise OperatorError(f"Portfolio does not exist: {paths.state}")
    state, _ = load_portfolio(paths.state, paths.events, paths.transaction)
    return state, paths


def legacy_audit(registry_path: Path) -> dict[str, str]:
    registry = read_json(registry_path)
    result = {}
    for item in registry.get("forward_observation_audit", []):
        try:
            start = date.fromisoformat(item["start"])
            end = date.fromisoformat(item["end"])
            classification = item["classification"]
        except (KeyError, ValueError, TypeError) as exc:
            raise OperatorError("Registry forward_observation_audit is malformed") from exc
        if classification not in {"catch_up", "unknown"} or end < start:
            raise OperatorError("Registry legacy observation classification is invalid")
        cursor = start
        while cursor <= end:
            result[cursor.isoformat()] = classification
            cursor += timedelta(days=1)
    return result


def progress(state_root: Path, registry_path: Path = ROOT / "research_registry.json", *, now=None) -> dict:
    paths = file_paths(state_root)
    if not paths.state.exists():
        raise OperatorError(f"Portfolio does not exist: {paths.state}")
    with PortfolioLock(paths.lock, shared=True):
        state, history = load_portfolio(paths.state, paths.events, paths.transaction)
        last = date.fromisoformat(state["last_processed_date"])
        audited = legacy_audit(registry_path)
        classifications = {"contemporaneous": 0, "catch_up": 0, "unknown": 0}
        for item in history:
            if item.get("type") != "CANDLE_PROCESSED" or date.fromisoformat(item["date"]) < GATE_START:
                continue
            classification = item.get("observation_classification") or audited.get(item["date"], "unknown")
            if classification not in classifications:
                classification = "unknown"
            classifications[classification] += 1
        completed = sum(classifications.values())
        verified = classifications["contemporaneous"]
        remaining = max(0, GATE_ROWS - verified)
        today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
        next_candidate = max(last + timedelta(days=1), today - timedelta(days=1))
        earliest = None if not remaining else next_candidate + timedelta(days=remaining - 1)
        synthetic = state.get("dataset_kind") != "real"
        return {
            "read_only": True,
            "strategy_version": state["strategy_version"],
            "last_processed_date": last.isoformat(),
            "gate_start": GATE_START.isoformat(),
            "required_new_candles": GATE_ROWS,
            "completed_market_candles": completed,
            "verified_contemporaneous_observations": verified,
            "catch_up_candles": classifications["catch_up"],
            "unknown_observation_timing": classifications["unknown"],
            "remaining_verified_observations": remaining,
            "earliest_possible_gate_candle": earliest.isoformat() if earliest else "already_met_pending_review",
            "observation_rule": "A verified archive committed during the first UTC calendar day after its candle close is contemporaneous; later commits are catch-up.",
            "evidence_label": (
                "synthetic_demo_not_real_evidence"
                if synthetic
                else "insufficient_short_forward_period"
                if remaining
                else "minimum_duration_reached_requires_research_review"
            ),
        }


def run_paper(arguments: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "paper.py"), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise OperatorError(f"paper.py failed:\n{message}")
    try:
        return json.JSONDecoder().raw_decode(result.stdout)[0]
    except json.JSONDecodeError as exc:
        raise OperatorError(f"paper.py returned unexpected output:\n{result.stdout}") from exc


def run_args(state_root: Path, csv_path: Path, metadata_path: Path, day: date) -> list[str]:
    return [
        "--state-root", str(state_root), "run", "--market", "crypto", "--symbol", "BTCUSDT",
        "--csv", str(csv_path), "--metadata", str(metadata_path),
        "--completed-through", day.isoformat(),
    ]


def readiness_args(state: dict, state_root: Path, csv_path: Path, metadata_path: Path, day: date) -> list[str]:
    result = [
        "--state-root", str(state_root), "readiness", "--market", "crypto", "--symbol", "BTCUSDT",
        "--dataset-kind", state["dataset_kind"], "--quote-currency", state["quote_currency"],
        "--capital-currency", state["capital_currency"], "--csv", str(csv_path),
        "--metadata", str(metadata_path), "--completed-through", day.isoformat(),
        "--fast", str(state["fast"]), "--slow", str(state["slow"]),
        "--min-notional", str(state["min_notional"]),
    ]
    if state.get("quantity_step") is not None:
        result += ["--quantity-step", str(state["quantity_step"])]
    if state.get("min_quantity") is not None:
        result += ["--min-quantity", str(state["min_quantity"])]
    return result


def update(args: argparse.Namespace) -> int:
    state_root = args.state_root.resolve()
    state, paths = paths_for(state_root)
    before_state = sha256(paths.state)
    before_events = sha256(paths.events)
    committed = False
    final_csv: Path | None = None
    final_metadata: Path | None = None
    try:
        if state["strategy_version"] != "sma-crossover-paper-v1":
            raise OperatorError("This operator is restricted to sma-crossover-paper-v1")
        day, archive_hash, row = verify_archive(args.archive.resolve(), args.checksum.resolve())
        if args.retrieval_date <= day:
            raise OperatorError("Retrieval date must be after the completed UTC candle date")
        expected_day = date.fromisoformat(state["last_processed_date"]) + timedelta(days=1)
        if day != expected_day:
            raise OperatorError(f"Next required candle is {expected_day}; supplied archive is {day}")

        source_csv = (ROOT / state["dataset_path"]).resolve()
        source_metadata = source_csv.with_suffix(".metadata.json")
        metadata = read_json(source_metadata)
        if state.get("dataset_kind") == "real":
            if not str(metadata.get("provider", "")).startswith("Binance Public Data"):
                raise OperatorError("Real portfolio metadata provider must be Binance Public Data")
            source_url = f"{OFFICIAL_BASE}/{args.archive.name}"
        elif args.synthetic_demo:
            source_url = f"https://example.invalid/synthetic/{args.archive.name}"
        else:
            raise OperatorError("Synthetic portfolios require --synthetic-demo and never count as evidence")

        first_date = next(csv.DictReader(source_csv.open(encoding="utf-8")))["date"]
        final_csv = source_csv.parent / f"binance_btcusdt_daily_{first_date}_{day}.csv"
        final_metadata = final_csv.with_suffix(".metadata.json")
        if final_csv.exists() or final_metadata.exists():
            raise OperatorError(f"Versioned output already exists: {final_csv} (do not overwrite it)")

        with tempfile.TemporaryDirectory(prefix="daily-paper-", dir=ROOT) as temp_dir:
            temp_csv = Path(temp_dir) / final_csv.name
            temp_metadata = Path(temp_dir) / final_metadata.name
            append_version(
                source_csv, source_metadata, temp_csv, temp_metadata,
                candle_date=day.isoformat(), open_value=row[1], high=row[2], low=row[3],
                close=row[4], volume=row[5], source_url=source_url,
                source_sha256=archive_hash, retrieval_date=args.retrieval_date.isoformat(),
                confirmed_complete=True,
            )
            readiness = run_paper(readiness_args(state, state_root, temp_csv, temp_metadata, day))
            if not readiness.get("ready"):
                raise OperatorError("Readiness did not report ready")
            preview = run_paper([*run_args(state_root, temp_csv, temp_metadata, day), "--preview"])
            print(json.dumps({
                "archive_sha256": archive_hash,
                "candidate_csv_sha256": sha256(temp_csv),
                "readiness": readiness,
                "proposed_events": preview.get("new_events", []),
                "projected_portfolio": preview.get("portfolio"),
            }, indent=2))
            if input("Type COMMIT to apply this paper update: ").strip() != "COMMIT":
                print("Cancelled. Portfolio, registry, and versioned datasets were not changed.")
                return 0
            temp_csv.replace(final_csv)
            temp_metadata.replace(final_metadata)

        committed_result = run_paper(run_args(state_root, final_csv, final_metadata, day))
        committed = True
        load_portfolio(paths.state, paths.events, paths.transaction)
        after_state = sha256(paths.state)
        after_events = sha256(paths.events)
        repeat = run_paper(run_args(state_root, final_csv, final_metadata, day))
        if repeat.get("new_events") or sha256(paths.state) != after_state or sha256(paths.events) != after_events:
            raise OperatorError("Idempotence verification failed after commit")
        output = {
            "committed": True,
            "date": day.isoformat(),
            "archive_sha256": archive_hash,
            "csv_sha256": sha256(final_csv),
            "events": committed_result.get("new_events", []),
            "portfolio": committed_result.get("portfolio"),
            "event_chain_verified": True,
            "identical_rerun_changed_nothing": True,
            "progress": progress(state_root),
        }
        print(json.dumps(output, indent=2))
        return 0
    except (OperatorError, OSError, KeyError, StopIteration, ValueError, KeyboardInterrupt) as exc:
        unchanged = paths.state.exists() and paths.events.exists() and sha256(paths.state) == before_state and sha256(paths.events) == before_events
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Portfolio files unchanged: {str(unchanged).lower()}", file=sys.stderr)
        if paths.transaction.exists():
            print(f"Recovery: python3 paper.py --state-root {state_root} recover --market crypto --symbol BTCUSDT", file=sys.stderr)
        elif committed:
            print("Recovery: stop; preserve all files and inspect `daily_paper.py progress` plus the event-chain error before another update.", file=sys.stderr)
        elif final_csv is not None and final_csv.exists():
            print(
                f"Recovery: the portfolio is unchanged but validated version files remain at {final_csv} and "
                f"{final_metadata}. Preserve them, resolve the reported conflict, then run readiness and preview "
                "with those paths before a manual paper.py commit; do not rerun the append over them.",
                file=sys.stderr,
            )
        else:
            print("Recovery: correct the reported input problem and rerun this same command. Do not edit portfolio JSON or JSONL files.", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    progress_parser = subparsers.add_parser("progress", help="Read-only 90-candle gate status")
    progress_parser.add_argument("--state-root", type=Path, default=ROOT / "paper_portfolios")
    progress_parser.add_argument("--registry", type=Path, default=ROOT / "research_registry.json")
    update_parser = subparsers.add_parser("update", help="Validate, preview, confirm, and commit one candle")
    update_parser.add_argument("--archive", type=Path, required=True)
    update_parser.add_argument("--checksum", type=Path, required=True)
    update_parser.add_argument("--retrieval-date", type=date.fromisoformat, required=True)
    update_parser.add_argument("--state-root", type=Path, default=ROOT / "paper_portfolios")
    update_parser.add_argument("--synthetic-demo", action="store_true", help="Only for an explicitly synthetic portfolio")
    args = parser.parse_args()
    if args.command == "progress":
        try:
            print(json.dumps(progress(args.state_root.resolve(), args.registry.resolve()), indent=2))
            return 0
        except (OperatorError, OSError, KeyError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    return update(args)


if __name__ == "__main__":
    raise SystemExit(main())

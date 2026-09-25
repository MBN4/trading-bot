import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from agent import Bar, read_bars
from daily_paper import progress, verify_archive
from paper import (load_portfolio, observation_classification, persist,
                   portfolio_paths, process_updates)


ROOT = Path(__file__).resolve().parents[1]


class DailyPaperOperatorTests(unittest.TestCase):
    def make_archive(self, root, day, *, bad_checksum=False):
        name = f'BTCUSDT-1d-{day}.zip'
        archive = root / name
        start = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp() * 1_000_000)
        end = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp() * 1_000_000) - 1
        row = f'{start},100,112,98,108,1200,{end},0,0,0,0,0\n'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr(name.replace('.zip', '.csv'), row)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = root / f'{name}.CHECKSUM'
        checksum.write_text(f'{"0" * 64 if bad_checksum else digest}  {name}\n')
        return archive, checksum

    def test_archive_checksum_and_complete_utc_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, checksum = self.make_archive(root, date(2026, 9, 1))
            day, digest, row = verify_archive(archive, checksum)
            self.assertEqual(day, date(2026, 9, 1))
            self.assertEqual(digest, hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(row[4], '108')
            _, bad = self.make_archive(root, date(2026, 9, 2), bad_checksum=True)
            with self.assertRaisesRegex(Exception, 'checksum mismatch'):
                verify_archive(root / 'BTCUSDT-1d-2026-09-02.zip', bad)

    def set_up_portfolio(self, root):
        data = root / 'data'
        data.mkdir()
        start = date(2026, 6, 13)
        last = date(2026, 8, 31)
        csv_path = data / f'binance_btcusdt_daily_{start}_{last}.csv'
        with csv_path.open('w', newline='') as handle:
            writer = csv.writer(handle, lineterminator='\n')
            writer.writerow(['date', 'open', 'high', 'low', 'close', 'volume'])
            for offset in range((last - start).days + 1):
                writer.writerow([start + timedelta(days=offset), 100, 110, 90, 100, 1000])
        digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        metadata = csv_path.with_suffix('.metadata.json')
        metadata.write_text(json.dumps({
            'provider': 'synthetic test provider', 'usage_permission': 'test only',
            'symbol': 'BTCUSDT', 'market': 'crypto', 'quote_currency': 'USDT',
            'timezone': 'UTC', 'session_close': 'synthetic completed UTC day',
            'retrieval_date': str(last), 'adjustment_policy': 'not applicable',
            'normalized_csv_sha256': digest, 'source_archives': [],
            'forward_lineage': {'appended_start': str(last), 'appended_end': str(last), 'completed_rows': 1},
        }))
        state_root = root / 'portfolio'
        command = [sys.executable, str(ROOT / 'paper.py'), '--state-root', str(state_root),
                   'init', '--market', 'crypto', '--symbol', 'BTCUSDT',
                   '--dataset-kind', 'synthetic', '--data-source', 'synthetic operator test',
                   '--quote-currency', 'USDT', '--capital-currency', 'USDT', '--csv', str(csv_path),
                   '--metadata', str(metadata), '--completed-through', str(last),
                   '--strategy-version', 'sma-crossover-paper-v1', '--registry', str(ROOT / 'research_registry.json')]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return state_root, csv_path

    def test_cancel_is_non_mutating_and_confirmed_update_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            state_root, _ = self.set_up_portfolio(root)
            archive, checksum = self.make_archive(root, date(2026, 9, 1))
            state, events, _ = portfolio_paths(state_root, 'crypto', 'BTCUSDT')
            before = (state.read_bytes(), events.read_bytes())
            command = [sys.executable, str(ROOT / 'daily_paper.py'), 'update',
                       '--archive', str(archive), '--checksum', str(checksum),
                       '--retrieval-date', '2026-09-03', '--state-root', str(state_root),
                       '--synthetic-demo']
            cancelled = subprocess.run(command, cwd=ROOT, input='CANCEL\n', text=True, capture_output=True)
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertEqual(before, (state.read_bytes(), events.read_bytes()))
            committed = subprocess.run(command, cwd=ROOT, input='COMMIT\n', text=True, capture_output=True)
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertIn('"identical_rerun_changed_nothing": true', committed.stdout)
            report = progress(state_root)
            self.assertEqual(report['completed_market_candles'], 1)
            self.assertEqual(report['catch_up_candles'], 1)
            self.assertEqual(report['verified_contemporaneous_observations'], 0)
            self.assertEqual(report['evidence_label'], 'synthetic_demo_not_real_evidence')

    def test_next_day_catch_up_mixed_and_restart_classifications(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            state_root, csv_path = self.set_up_portfolio(root)
            registry = root / 'registry.json'
            registry.write_text('{"forward_observation_audit": []}\n')
            paths = portfolio_paths(state_root, 'crypto', 'BTCUSDT')
            bars = read_bars(csv_path)
            cases = [
                (date(2026, 9, 1), datetime(2026, 9, 2, 12, tzinfo=timezone.utc), 'contemporaneous'),
                (date(2026, 9, 2), datetime(2026, 9, 5, 8, tzinfo=timezone.utc), 'catch_up'),
                (date(2026, 9, 3), datetime(2026, 9, 5, 8, tzinfo=timezone.utc), 'catch_up'),
                (date(2026, 9, 4), datetime(2026, 9, 5, 18, tzinfo=timezone.utc), 'contemporaneous'),
            ]
            for day, observed_at, expected in cases:
                state, _ = load_portfolio(*paths)
                bars.append(Bar(day, 100, 108, 112, 98, 1200))
                state, events = process_updates(
                    state, bars, completed_through=str(day), observed_at=observed_at)
                self.assertTrue(events)
                self.assertTrue(all(item['observation_classification'] == expected for item in events))
                persist(*paths, state, events)
                load_portfolio(*paths)

            report = progress(state_root, registry, now=datetime(2026, 9, 5, 20, tzinfo=timezone.utc))
            self.assertEqual(report['completed_market_candles'], 4)
            self.assertEqual(report['verified_contemporaneous_observations'], 2)
            self.assertEqual(report['catch_up_candles'], 2)
            self.assertEqual(report['unknown_observation_timing'], 0)
            self.assertEqual(report['remaining_verified_observations'], 88)

    def test_legacy_event_without_reliable_timestamp_is_unknown(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            state_root, csv_path = self.set_up_portfolio(root)
            registry = root / 'registry.json'
            registry.write_text('{"forward_observation_audit": []}\n')
            paths = portfolio_paths(state_root, 'crypto', 'BTCUSDT')
            state, _ = load_portfolio(*paths)
            bars = read_bars(csv_path)
            bars.append(Bar(date(2026, 9, 1), 100, 108, 112, 98, 1200))
            state, events = process_updates(
                state, bars, observed_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
            for item in events:
                item.pop('observation_classification', None)
                item.pop('observed_at_utc', None)
                item.pop('observation_rule', None)
            persist(*paths, state, events)
            report = progress(state_root, registry, now=datetime(2026, 9, 2, 12, tzinfo=timezone.utc))
            self.assertEqual(report['unknown_observation_timing'], 1)
            self.assertEqual(report['verified_contemporaneous_observations'], 0)
            load_portfolio(*paths)

    def test_observation_rule_rejects_preclose_and_allows_publication_delay(self):
        day = date(2026, 9, 1)
        next_day = observation_classification(
            day, datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc))
        late = observation_classification(day, datetime(2026, 9, 3, tzinfo=timezone.utc))
        self.assertEqual(next_day['observation_classification'], 'contemporaneous')
        self.assertEqual(late['observation_classification'], 'catch_up')
        with self.assertRaisesRegex(ValueError, 'before its UTC session closes'):
            observation_classification(day, datetime(2026, 9, 1, 23, tzinfo=timezone.utc))


if __name__ == '__main__':
    unittest.main()

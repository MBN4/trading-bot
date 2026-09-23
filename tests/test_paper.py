import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from agent import Bar, read_bars
from make_demo_ohlcv import convert
from paper import (fill_pending, initialize_portfolio, load_portfolio, persist,
                   portfolio_lock_path, portfolio_paths, process_updates,
                   readiness_report, recover, validate_candle_completeness)


def ohlcv_bars(closes, start=date(2025, 1, 1)):
    return [Bar(start + timedelta(days=i), float(close), float(close),
                float(close) + 1, float(close) - 1, 1000.0)
            for i, close in enumerate(closes)]


def write_ohlcv(path, bars):
    with open(path, 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(('date', 'open', 'high', 'low', 'close', 'volume'))
        for bar in bars:
            writer.writerow((bar.day, bar.open, bar.high, bar.low, bar.close, bar.volume))


def write_metadata(path, *, symbol='TEST_USD', market='crypto', quote_currency='USD'):
    path.write_text(json.dumps({
        'provider': 'test fixture',
        'usage_permission': 'synthetic local test use',
        'symbol': symbol,
        'market': market,
        'quote_currency': quote_currency,
        'timezone': 'UTC',
        'session_close': '00:00 UTC after each synthetic daily bar',
        'retrieval_date': '2025-03-21',
        'adjustment_policy': 'not applicable to synthetic fixture',
    }))


def new_state(bars, path='paper_fixture.csv', **overrides):
    settings = {
        'market': 'crypto', 'symbol': 'TEST_USD', 'dataset_kind': 'synthetic',
        'data_source': 'test fixture', 'quote_currency': 'USD',
        'capital_currency': 'USD', 'initial': 1000, 'fast': 2, 'slow': 3,
        'fee': 0.01, 'slippage': 0.02, 'allocation': 0.5,
        'max_daily_loss': 0.03,
    }
    settings.update(overrides)
    return initialize_portfolio(bars, path, **settings)


class ForwardPaperTests(unittest.TestCase):
    def test_demo_converter_produces_explicit_ohlcv_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'sample.csv'
            destination = Path(tmp) / 'demo.csv'
            with source.open('w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(('date', 'close'))
                writer.writerows((('2025-01-01', '100'), ('2025-01-02', '101')))
            self.assertEqual(convert(source, destination, rows=1), 1)
            with destination.open(newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['open'], '100.0')
            self.assertEqual(rows[0]['volume'], '1000.0')

    def test_pending_signal_waits_and_fills_on_next_candle_open(self):
        initial_bars = ohlcv_bars([100] * 79 + [110])
        state, events = new_state(initial_bars)

        self.assertEqual(state['pending_signal']['side'], 'BUY')
        self.assertEqual(state['fills'], [])
        self.assertEqual(events[-1]['type'], 'SIGNAL_ACCEPTED')

        updated = initial_bars + ohlcv_bars([120], start=date(2025, 3, 22))
        state, events = process_updates(state, updated)
        fill = state['fills'][0]
        expected_units = int((500 / (120 * 1.02 * 1.01)) / 0.00000001) * 0.00000001

        self.assertEqual(fill['signal_date'], '2025-03-21')
        self.assertEqual(fill['fill_date'], '2025-03-22')
        self.assertAlmostEqual(fill['price'], 122.4)
        self.assertAlmostEqual(fill['fee'], expected_units * 122.4 * 0.01)
        self.assertAlmostEqual(state['units'], expected_units)
        self.assertGreaterEqual(state['cash'], 500)
        self.assertEqual([item['type'] for item in events[:2]], ['FILL', 'CANDLE_PROCESSED'])

    def test_repeat_run_is_idempotent_and_one_row_update_is_single_processed_event(self):
        initial_bars = ohlcv_bars([100] * 80)
        state, _ = new_state(initial_bars)
        state, events = process_updates(state, initial_bars)
        self.assertEqual(events, [])

        updated = initial_bars + ohlcv_bars([101], start=date(2025, 3, 22))
        state, events = process_updates(state, updated)
        self.assertEqual(sum(item['type'] == 'CANDLE_PROCESSED' for item in events), 1)
        state, repeated = process_updates(state, updated)
        self.assertEqual(repeated, [])
        self.assertEqual(len(state['processed_bars']), 81)

    def test_restart_preserves_state_and_event_history_appends(self):
        bars = ohlcv_bars([100] * 79 + [110])
        state, events = new_state(bars)
        with tempfile.TemporaryDirectory() as tmp:
            state_path, events_path, transaction_path = portfolio_paths(tmp, 'crypto', 'TEST_USD')
            persist(state_path, events_path, transaction_path, state, events)
            restarted, _ = load_portfolio(state_path, events_path, transaction_path)
            updated = bars + ohlcv_bars([120], start=date(2025, 3, 22))
            restarted, new_events = process_updates(restarted, updated)
            persist(state_path, events_path, transaction_path, restarted, new_events)

            lines = events_path.read_text().splitlines()
            self.assertEqual(len(lines), len(events) + len(new_events))
            self.assertEqual(json.loads(lines[-1])['date'], '2025-03-22')
            loaded, _ = load_portfolio(state_path, events_path, transaction_path)
            self.assertEqual(len(loaded['fills']), 1)

    def test_changed_shortened_and_backdated_history_are_rejected(self):
        bars = ohlcv_bars([100] * 80)
        state, _ = new_state(bars)
        changed = list(bars)
        changed[10] = Bar(changed[10].day, 101, 101, 102, 100, 1000)
        inserted = list(bars)
        inserted.insert(10, Bar(date(2025, 1, 10), 100, 100, 101, 99, 1000))

        with self.assertRaisesRegex(ValueError, 'history changed'):
            process_updates(state, changed)
        with self.assertRaisesRegex(ValueError, 'shortened'):
            process_updates(state, bars[:-1])
        with self.assertRaisesRegex(ValueError, 'backdated'):
            process_updates(state, inserted)

    def test_daily_loss_blocks_buy_and_allocation_limits_accepted_buy(self):
        bars = ohlcv_bars([100] * 79 + [110])
        state, _ = new_state(bars)
        self.assertEqual(state['pending_signal']['budget'], 500)

        blocked_state, _ = new_state(ohlcv_bars([100] * 80))
        blocked_state['last_equity'] = 1100
        updated = ohlcv_bars([100] * 80 + [110])
        blocked_state, events = process_updates(blocked_state, updated)
        blocked = [item for item in events if item['type'] == 'BUY_BLOCKED']
        self.assertEqual(blocked[0]['reason'], 'daily_loss_circuit_breaker')
        self.assertIsNone(blocked_state['pending_signal'])

    def test_invalid_ohlcv_is_rejected_before_processing(self):
        rows = ohlcv_bars([100] * 80)
        rows[5] = Bar(rows[5].day, 100, 90, 99, 100, 1000)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'invalid.csv'
            write_ohlcv(path, rows)
            with self.assertRaisesRegex(ValueError, 'Inconsistent OHLC'):
                read_bars(path)

    def test_stock_and_crypto_use_separate_state_paths(self):
        stock = portfolio_paths('paper_portfolios', 'stock', 'ABC')[0]
        crypto = portfolio_paths('paper_portfolios', 'crypto', 'ABC')[0]
        self.assertNotEqual(stock, crypto)
        self.assertEqual(str(stock), 'paper_portfolios/stock/ABC.json')
        self.assertEqual(str(crypto), 'paper_portfolios/crypto/ABC.json')

    def test_crash_recovery_at_each_commit_phase_is_exact(self):
        bars = ohlcv_bars([100] * 79 + [110])
        for phase in ('journal', 'events', 'state'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                paths = portfolio_paths(tmp, 'crypto', 'TEST_USD')
                state, events = new_state(bars)
                with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                    persist(*paths, state, events, fail_after=phase)
                with self.assertRaisesRegex(ValueError, 'Interrupted transaction'):
                    load_portfolio(*paths)
                recovered, appended = recover(*paths)
                loaded, history = load_portfolio(*paths)
                self.assertEqual(loaded, recovered)
                self.assertEqual(len(history), 2)
                self.assertEqual(appended, 2 if phase == 'journal' else 0)
                self.assertFalse(paths[2].exists())

    def test_corrupt_state_event_and_checkpoint_are_rejected(self):
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            paths = portfolio_paths(tmp, 'crypto', 'TEST_USD')
            state, events = new_state(bars)
            persist(*paths, state, events)
            original_state = paths[0].read_text()
            original_events = paths[1].read_text()

            damaged = json.loads(original_state)
            damaged['cash'] = 999999
            paths[0].write_text(json.dumps(damaged))
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                load_portfolio(*paths)

            paths[0].write_text(original_state)
            lines = original_events.splitlines()
            item = json.loads(lines[0])
            item['cash'] = 999999
            lines[0] = json.dumps(item)
            paths[1].write_text('\n'.join(lines) + '\n')
            with self.assertRaisesRegex(ValueError, 'event checksum mismatch'):
                load_portfolio(*paths)

            paths[1].write_text('')
            with self.assertRaisesRegex(ValueError, 'checkpoint mismatch'):
                load_portfolio(*paths)

    def test_preview_creates_nothing_and_does_not_touch_existing_files(self):
        root = Path(__file__).resolve().parents[1]
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'bars.csv'
            state_root = Path(tmp) / 'state'
            write_ohlcv(csv_path, bars)
            common = [sys.executable, 'paper.py', '--state-root', str(state_root),
                      'init', '--market', 'crypto', '--symbol', 'TEST_USD',
                      '--strategy-version', 'sma-crossover-paper-v1',
                      '--dataset-kind', 'synthetic', '--csv', str(csv_path),
                      '--data-source', 'test', '--quote-currency', 'USD',
                      '--capital-currency', 'USD',
                      '--completed-through', str(bars[-1].day)]
            result = subprocess.run(common + ['--preview'], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_root.exists())

            result = subprocess.run(common, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = portfolio_paths(state_root, 'crypto', 'TEST_USD')
            before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths[:2]]
            run = [sys.executable, 'paper.py', '--state-root', str(state_root), 'run',
                   '--market', 'crypto', '--symbol', 'TEST_USD', '--csv', str(csv_path),
                   '--completed-through', str(bars[-1].day), '--preview']
            result = subprocess.run(run, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            after = [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths[:2]]
            self.assertEqual(before, after)
            self.assertFalse(paths[2].exists())

    def test_stock_defaults_to_whole_shares_and_fractional_is_explicit(self):
        bars = ohlcv_bars([100] * 79 + [110])
        stock, _ = new_state(bars, market='stock', symbol='ABC',
                             quote_currency='PKR', capital_currency='PKR')
        self.assertEqual(stock['quantity_step'], 1)
        self.assertEqual(stock['min_quantity'], 1)
        fill_pending(stock, ohlcv_bars([120], start=date(2025, 3, 22))[0])
        self.assertEqual(stock['units'], 4)

        fractional, _ = new_state(bars, market='stock', symbol='ABC',
                                  quote_currency='PKR', capital_currency='PKR',
                                  quantity_step=0.01, min_quantity=0.01)
        fill_pending(fractional, ohlcv_bars([120], start=date(2025, 3, 22))[0])
        self.assertAlmostEqual(fractional['units'] / 0.01,
                               round(fractional['units'] / 0.01))

    def test_buy_never_overspends_and_invalid_sell_is_rejected(self):
        bars = ohlcv_bars([100] * 79 + [110])
        state, _ = new_state(bars, allocation=1, fee=0.25, slippage=0.5)
        starting_cash = state['cash']
        fill_pending(state, ohlcv_bars([1000], start=date(2025, 3, 22))[0])
        self.assertGreaterEqual(state['cash'], 0)
        fill = state['fills'][0]
        self.assertLessEqual(fill['gross'] + fill['fee'], starting_cash)

        state['pending_signal'] = {'side': 'SELL', 'signal_date': '2025-03-22'}
        state['units'] = 0
        with self.assertRaisesRegex(ValueError, 'exceeds held units'):
            fill_pending(state, ohlcv_bars([100], start=date(2025, 3, 23))[0])

    def test_weekends_and_gaps_fill_on_next_available_row(self):
        bars = ohlcv_bars([100] * 79 + [110])
        state, _ = new_state(bars)
        monday = Bar(date(2025, 3, 24), 120, 120, 121, 119, 1000)
        state, events = process_updates(state, bars + [monday])
        candle = [item for item in events if item['type'] == 'CANDLE_PROCESSED'][0]
        self.assertEqual(state['fills'][0]['fill_date'], '2025-03-24')
        self.assertEqual(candle['calendar_gap_days'], 3)

    def test_incomplete_final_csv_row_and_malformed_state_are_rejected(self):
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'incomplete.csv'
            write_ohlcv(csv_path, bars)
            with csv_path.open('a') as handle:
                handle.write('2025-03-22,100,101,99,,1000\n')
            with self.assertRaisesRegex(ValueError, 'Missing value'):
                read_bars(csv_path)

            paths = portfolio_paths(tmp, 'crypto', 'TEST_USD')
            paths[0].parent.mkdir(parents=True, exist_ok=True)
            paths[0].write_text('{not json')
            paths[1].write_text('')
            with self.assertRaisesRegex(ValueError, 'Cannot read portfolio state'):
                load_portfolio(*paths)

    def test_completed_through_is_required_to_include_latest_row(self):
        bars = ohlcv_bars([100] * 80)
        self.assertEqual(validate_candle_completeness(bars, '2025-03-21'), '2025-03-21')
        with self.assertRaisesRegex(ValueError, 'later than completed-through'):
            validate_candle_completeness(bars, '2025-03-20')

    def test_cli_rejects_unconfirmed_latest_candle_without_changing_files(self):
        root = Path(__file__).resolve().parents[1]
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'bars.csv'
            state_root = Path(tmp) / 'state'
            write_ohlcv(csv_path, bars)
            cmd = [sys.executable, 'paper.py', '--state-root', str(state_root),
                   'init', '--market', 'crypto', '--symbol', 'TEST_USD',
                   '--strategy-version', 'sma-crossover-paper-v1',
                   '--dataset-kind', 'synthetic', '--csv', str(csv_path),
                   '--data-source', 'test', '--quote-currency', 'USD',
                   '--capital-currency', 'USD', '--completed-through', '2025-03-20']
            result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('later than completed-through', result.stderr)
            self.assertFalse(portfolio_paths(state_root, 'crypto', 'TEST_USD')[0].exists())

    def test_run_records_updated_metadata_provenance(self):
        root = Path(__file__).resolve().parents[1]
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'bars.csv'
            metadata_path = Path(tmp) / 'metadata.json'
            state_root = Path(tmp) / 'state'
            write_ohlcv(csv_path, bars)
            write_metadata(metadata_path)
            init = [sys.executable, 'paper.py', '--state-root', str(state_root), 'init',
                    '--market', 'crypto', '--symbol', 'TEST_USD', '--strategy-version',
                    'sma-crossover-paper-v1', '--dataset-kind', 'synthetic', '--csv',
                    str(csv_path), '--metadata', str(metadata_path), '--data-source', 'test',
                    '--quote-currency', 'USD', '--capital-currency', 'USD',
                    '--completed-through', str(bars[-1].day)]
            initialized = subprocess.run(init, cwd=root, capture_output=True, text=True)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            updated = bars + ohlcv_bars([101], start=date(2025, 3, 22))
            write_ohlcv(csv_path, updated)
            metadata = json.loads(metadata_path.read_text())
            metadata['retrieval_date'] = '2025-03-23'
            metadata['manual_append'] = {'date': '2025-03-22', 'confirmed_complete': True}
            metadata['normalized_csv_sha256'] = __import__('hashlib').sha256(
                csv_path.read_bytes()).hexdigest()
            metadata_path.write_text(json.dumps(metadata))
            run = [sys.executable, 'paper.py', '--state-root', str(state_root), 'run',
                   '--market', 'crypto', '--symbol', 'TEST_USD', '--csv', str(csv_path),
                   '--metadata', str(metadata_path), '--completed-through', '2025-03-22']
            result = subprocess.run(run, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = load_portfolio(*portfolio_paths(state_root, 'crypto', 'TEST_USD'))[0]
            self.assertEqual(state['dataset_metadata']['manual_append']['date'], '2025-03-22')

    def test_readiness_report_is_read_only_and_actionable(self):
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'bars.csv'
            metadata_path = Path(tmp) / 'metadata.json'
            state_root = Path(tmp) / 'state'
            write_ohlcv(csv_path, bars)
            write_metadata(metadata_path)
            paths = portfolio_paths(state_root, 'crypto', 'TEST_USD')
            report = readiness_report(
                csv_path, metadata_path, market='crypto', symbol='TEST_USD',
                dataset_kind='synthetic', quote_currency='USD',
                capital_currency='USD', completed_through='2025-03-21',
                state_paths=paths)
            self.assertTrue(report['ready'], report)
            self.assertEqual(report['mode'], 'read_only_no_files_changed')
            self.assertFalse(state_root.exists())

            bad = readiness_report(
                csv_path, metadata_path, market='crypto', symbol='TEST_USD',
                dataset_kind='synthetic', quote_currency='PKR',
                capital_currency='USD', completed_through='2025-03-20',
                state_paths=paths)
            self.assertFalse(bad['ready'])
            self.assertTrue(any('currency' in error for error in bad['errors']))
            self.assertTrue(any('candle_completeness' in error for error in bad['errors']))
            self.assertFalse(state_root.exists())

    def test_readiness_checks_recorded_csv_hash(self):
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'bars.csv'
            metadata_path = Path(tmp) / 'metadata.json'
            write_ohlcv(csv_path, bars)
            write_metadata(metadata_path)
            metadata = json.loads(metadata_path.read_text())
            metadata['normalized_csv_sha256'] = __import__('hashlib').sha256(
                csv_path.read_bytes()).hexdigest()
            metadata_path.write_text(json.dumps(metadata))
            report = readiness_report(
                csv_path, metadata_path, market='crypto', symbol='TEST_USD',
                dataset_kind='synthetic', quote_currency='USD', capital_currency='USD',
                completed_through='2025-03-21')
            self.assertTrue(report['checks']['metadata_csv_hash']['ok'])
            csv_path.write_text(csv_path.read_text().replace(',1000\n', ',1001\n', 1))
            report = readiness_report(
                csv_path, metadata_path, market='crypto', symbol='TEST_USD',
                dataset_kind='synthetic', quote_currency='USD', capital_currency='USD',
                completed_through='2025-03-21')
            self.assertFalse(report['ready'])
            self.assertIn('does not match', report['checks']['metadata_csv_hash']['error'])

    def test_cli_readiness_exits_nonzero_on_pending_transaction(self):
        root = Path(__file__).resolve().parents[1]
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'bars.csv'
            metadata_path = Path(tmp) / 'metadata.json'
            state_root = Path(tmp) / 'state'
            write_ohlcv(csv_path, bars)
            write_metadata(metadata_path)
            state, events = new_state(bars)
            paths = portfolio_paths(state_root, 'crypto', 'TEST_USD')
            with self.assertRaises(RuntimeError):
                persist(*paths, state, events, fail_after='journal')
            cmd = [sys.executable, 'paper.py', '--state-root', str(state_root),
                   'readiness', '--market', 'crypto', '--symbol', 'TEST_USD',
                   '--dataset-kind', 'synthetic', '--csv', str(csv_path),
                   '--metadata', str(metadata_path), '--quote-currency', 'USD',
                   '--capital-currency', 'USD', '--completed-through', '2025-03-21']
            result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('pending transaction', result.stdout.lower())

    def test_exclusive_lock_blocks_concurrent_writer_and_recovery(self):
        root = Path(__file__).resolve().parents[1]
        bars = ohlcv_bars([100] * 80)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / 'bars.csv'
            state_root = Path(tmp) / 'state'
            write_ohlcv(csv_path, bars)
            lock_path = portfolio_lock_path(state_root, 'crypto', 'TEST_USD')
            holder = subprocess.Popen(
                [sys.executable, '-c',
                 'import fcntl, pathlib, sys, time; '
                 'p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); '
                 'f=p.open("a+"); fcntl.flock(f, fcntl.LOCK_EX); print("locked", flush=True); time.sleep(2)',
                 str(lock_path)],
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'locked')
                cmd = [sys.executable, 'paper.py', '--state-root', str(state_root),
                       'init', '--market', 'crypto', '--symbol', 'TEST_USD',
                       '--strategy-version', 'sma-crossover-paper-v1',
                       '--dataset-kind', 'synthetic', '--csv', str(csv_path),
                       '--data-source', 'test', '--quote-currency', 'USD',
                       '--capital-currency', 'USD', '--completed-through', '2025-03-21']
                result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Portfolio is locked by another process', result.stderr)

                paths = portfolio_paths(state_root, 'crypto', 'TEST_USD')
                paths[2].parent.mkdir(parents=True, exist_ok=True)
                paths[2].write_text('{}')
                recover_cmd = [sys.executable, 'paper.py', '--state-root', str(state_root),
                               'recover', '--market', 'crypto', '--symbol', 'TEST_USD']
                result = subprocess.run(recover_cmd, cwd=root, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Portfolio is locked by another process', result.stderr)
            finally:
                holder.terminate()
                holder.wait(timeout=5)
                holder.stdout.close()
                holder.stderr.close()

            portfolio_paths(state_root, 'crypto', 'TEST_USD')[2].unlink()
            cmd = [sys.executable, 'paper.py', '--state-root', str(state_root),
                   'init', '--market', 'crypto', '--symbol', 'TEST_USD',
                   '--strategy-version', 'sma-crossover-paper-v1',
                   '--dataset-kind', 'synthetic', '--csv', str(csv_path),
                   '--data-source', 'test', '--quote-currency', 'USD',
                   '--capital-currency', 'USD', '--completed-through', '2025-03-21']
            result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

import csv
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import (Bar, infer_currency, maximum_drawdown, read_bars, simulate,
                   train_then_test, validate_currencies, validate_dataset_kind,
                   walk_forward_test)


def bars_from_prices(prices):
    start = date(2025, 1, 1)
    return [Bar(start + timedelta(days=i), float(price)) for i, price in enumerate(prices)]


def write_csv(path, rows, header=('date', 'close')):
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class TradingAgentTests(unittest.TestCase):
    def test_accounting_matches_independent_calculation_for_open_position(self):
        bars = bars_from_prices([100] * 20 + [110, 120, 130])
        result = simulate(bars, initial=1000, fast=2, slow=3, fee=0.01, allocation=0.5)

        spend = 500
        expected_units = spend * 0.99 / 120
        expected_cash = 500
        expected_final = expected_cash + expected_units * 130

        self.assertEqual(result['trades'][0]['side'], 'BUY')
        self.assertEqual(result['trades'][0]['date'], '2025-01-22')
        self.assertAlmostEqual(result['trades'][0]['fee'], 5)
        self.assertAlmostEqual(result['units'], round(expected_units, 8))
        self.assertEqual(result['cash'], round(expected_cash, 2))
        self.assertEqual(result['final'], round(expected_final, 2))
        self.assertEqual(result['return_pct'], round(100 * (expected_final / 1000 - 1), 2))

    def test_accounting_matches_independent_calculation_for_round_trip(self):
        bars = bars_from_prices([100] * 20 + [110, 120, 90, 80])
        result = simulate(bars, initial=1000, fast=2, slow=3, fee=0.01, allocation=0.5)

        units = 500 * 0.99 / 120
        proceeds = units * 80 * 0.99
        expected_cash = 500 + proceeds

        self.assertEqual([t['side'] for t in result['trades']], ['BUY', 'SELL'])
        self.assertAlmostEqual(result['trades'][1]['fee'], units * 80 * 0.01)
        self.assertEqual(result['units'], 0)
        self.assertEqual(result['cash'], round(expected_cash, 2))
        self.assertEqual(result['final'], round(expected_cash, 2))

    def test_no_trades_and_repeated_signals_do_not_pyramid(self):
        flat = simulate(bars_from_prices([100] * 90), fast=5, slow=20)
        rising = simulate(bars_from_prices(list(range(100, 200))), fast=5, slow=20)

        self.assertEqual(flat['trades'], [])
        self.assertEqual([t['side'] for t in rising['trades']], ['BUY'])

    def test_train_then_test_uses_training_period_for_selection_and_buy_hold(self):
        bars = bars_from_prices([100] * 56 + [110] * 24 + [120] * 20)
        result = train_then_test(bars, {'initial': 1000, 'fee': 0, 'allocation': 1, 'max_daily_loss': 0.03})

        self.assertEqual(result['training_rows'], 80)
        self.assertEqual(result['test_rows'], 20)
        self.assertEqual(min(t['date'] for t in result['trades']), '2025-03-23')
        self.assertEqual(result['buy_hold_return_pct'], 0.0)

    def test_daily_loss_limit_is_not_a_maximum_loss_guarantee(self):
        bars = bars_from_prices([100] * 20 + [110, 120, 50])
        result = simulate(bars, initial=1000, fast=2, slow=3, fee=0, allocation=1, max_daily_loss=0.03)

        self.assertEqual([t['side'] for t in result['trades']], ['BUY'])
        self.assertLess(result['return_pct'], -3)

    def test_market_shapes_for_stock_and_crypto(self):
        shapes = {
            'rising': list(range(100, 190)),
            'falling': list(range(190, 100, -1)),
            'flat': [100] * 90,
            'volatile': ([100, 120, 95, 130, 90, 125] * 15),
            'gapped': [100] * 50 + [150] + [95] * 39,
        }
        for market in ('stock', 'crypto'):
            for name, prices in shapes.items():
                with self.subTest(market=market, shape=name):
                    result = train_then_test(bars_from_prices(prices), {
                        'initial': 1000,
                        'fee': 0.001,
                        'allocation': 0.2,
                        'max_daily_loss': 0.03,
                    })
                    self.assertEqual(result['training_rows'], 72)
                    self.assertEqual(result['test_rows'], 18)
                    self.assertIn('buy_hold_return_pct', result)
                    self.assertGreaterEqual(result['final'], 0)

    def test_read_bars_rejects_bad_inputs(self):
        cases = {
            'missing date': [['', '100']] + [[f'2025-01-{i:02d}', '100'] for i in range(2, 81)],
            'duplicate date': [['2025-01-01', '100'], ['2025-01-01', '101']] + [[f'2025-02-{i:02d}', '100'] for i in range(1, 79)],
            'unsorted': [['2025-01-02', '100'], ['2025-01-01', '101']] + [[f'2025-02-{i:02d}', '100'] for i in range(1, 79)],
            'missing price': [[str(date(2025, 1, 1) + timedelta(days=i)), '' if i == 5 else '100'] for i in range(80)],
            'nonnumeric': [[str(date(2025, 1, 1) + timedelta(days=i)), 'x' if i == 5 else '100'] for i in range(80)],
            'zero': [[str(date(2025, 1, 1) + timedelta(days=i)), '0' if i == 5 else '100'] for i in range(80)],
            'negative': [[str(date(2025, 1, 1) + timedelta(days=i)), '-1' if i == 5 else '100'] for i in range(80)],
            'too few': [[str(date(2025, 1, 1) + timedelta(days=i)), '100'] for i in range(79)],
            'empty': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, rows in cases.items():
                with self.subTest(name=name):
                    path = Path(tmp) / f'{name}.csv'
                    write_csv(path, rows)
                    with self.assertRaises(ValueError):
                        read_bars(path)

    def test_read_bars_imports_and_validates_ohlcv(self):
        header = ('date', 'open', 'high', 'low', 'close', 'volume')
        valid = [[str(date(2025, 1, 1) + timedelta(days=i)), '100', '110', '90', '105', '1000']
                 for i in range(80)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ohlcv.csv'
            write_csv(path, valid, header)
            bars = read_bars(path)
            self.assertEqual((bars[0].open, bars[0].high, bars[0].low,
                              bars[0].close, bars[0].volume), (100, 110, 90, 105, 1000))

            invalid_rows = {
                'missing': valid[:5] + [[valid[5][0], '100', '', '90', '105', '1000']] + valid[6:],
                'negative volume': valid[:5] + [[valid[5][0], '100', '110', '90', '105', '-1']] + valid[6:],
                'nonnumeric volume': valid[:5] + [[valid[5][0], '100', '110', '90', '105', 'many']] + valid[6:],
                'high below close': valid[:5] + [[valid[5][0], '100', '101', '90', '105', '1000']] + valid[6:],
                'low above open': valid[:5] + [[valid[5][0], '100', '110', '101', '105', '1000']] + valid[6:],
                'duplicate': valid[:6] + [[valid[5][0], '100', '110', '90', '105', '1000']] + valid[7:],
            }
            for name, rows in invalid_rows.items():
                with self.subTest(name=name):
                    write_csv(path, rows, header)
                    with self.assertRaises(ValueError):
                        read_bars(path)

    def test_slippage_drawdown_and_trade_count_calculations(self):
        bars = bars_from_prices([100] * 20 + [110, 120, 90, 80])
        result = simulate(bars, initial=1000, fast=2, slow=3, fee=0.01,
                          slippage=0.02, allocation=0.5)
        buy_units = (500 - 5) / (120 * 1.02)
        sell_gross = buy_units * (80 * 0.98)
        expected_final = 500 + sell_gross * 0.99

        self.assertEqual(result['trade_count'], 2)
        self.assertAlmostEqual(result['trades'][0]['price'], 122.4)
        self.assertAlmostEqual(result['trades'][1]['price'], 78.4)
        self.assertEqual(result['final'], round(expected_final, 2))
        self.assertEqual(maximum_drawdown([100, 120, 90, 95]), -25.0)

    def test_walk_forward_periods_are_disjoint_and_use_prior_training_only(self):
        bars = bars_from_prices(list(range(100, 200)))
        result = walk_forward_test(bars, {
            'initial': 1000, 'fee': 0, 'slippage': 0,
            'allocation': 1, 'max_daily_loss': 0.03,
        }, folds=3)
        periods = result['walk_forward_folds']
        self.assertEqual([p['training_rows'] for p in periods], [40, 54, 67])
        self.assertEqual([p['test_rows'] for p in periods], [14, 13, 13])
        self.assertEqual(periods[0]['test_date_range']['end'], '2025-02-23')
        self.assertEqual(periods[1]['test_date_range']['start'], '2025-02-24')
        self.assertTrue(all('buy_hold_return_pct' in p and 'max_drawdown_pct' in p
                            and 'trade_count' in p for p in periods))
        self.assertEqual(result['final_holdout']['training_rows'], 80)
        self.assertEqual(result['final_holdout']['test_rows'], 20)
        self.assertFalse(result['split_policy']['holdout_used_for_selection'])

    def test_real_ohlcv_signal_executes_at_next_open(self):
        days = bars_from_prices([100] * 20 + [110, 120, 130])
        bars = [Bar(bar.day, bar.close, 1000 + i, 1100 + i, 900 + i, 1)
                for i, bar in enumerate(days)]
        result = simulate(bars, initial=1000, fast=2, slow=3, fee=0,
                          slippage=0, allocation=1)

        self.assertEqual(result['trades'][0]['signal_date'], '2025-01-21')
        self.assertEqual(result['trades'][0]['date'], '2025-01-22')
        self.assertEqual(result['trades'][0]['price'], 1021)
        self.assertNotEqual(result['trades'][0]['price'], bars[21].close)

    def test_current_close_cannot_block_prior_signal_at_current_open(self):
        days = bars_from_prices([100] * 20 + [110, 50])
        bars = [Bar(bar.day, bar.close, 120, 130, 40, 1) for bar in days]
        result = simulate(bars, initial=1000, fast=2, slow=3, fee=0,
                          slippage=0, allocation=1, max_daily_loss=0.03)

        self.assertEqual(result['trades'][0]['side'], 'BUY')
        self.assertEqual(result['trades'][0]['signal_date'], '2025-01-21')
        self.assertEqual(result['trades'][0]['date'], '2025-01-22')
        self.assertEqual(result['trades'][0]['price'], 120)

    def test_final_holdout_does_not_affect_strategy_selection(self):
        prefix = list(range(100, 180))
        first = walk_forward_test(bars_from_prices(prefix + list(range(200, 220))), {
            'initial': 1000, 'fee': 0, 'slippage': 0,
            'allocation': 1, 'max_daily_loss': 0.03,
        }, folds=3)
        second = walk_forward_test(bars_from_prices(prefix + list(range(50, 30, -1))), {
            'initial': 1000, 'fee': 0, 'slippage': 0,
            'allocation': 1, 'max_daily_loss': 0.03,
        }, folds=3)

        self.assertEqual(first['validation_candidate_scores'], second['validation_candidate_scores'])
        self.assertEqual(first['locked_holdout_windows'], second['locked_holdout_windows'])
        self.assertNotEqual(first['final_holdout']['return_pct'],
                            second['final_holdout']['return_pct'])

    def test_invalid_settings_and_currency_guardrails(self):
        bars = bars_from_prices([100] * 80)
        for kwargs in (
            {'fast': 20, 'slow': 5},
            {'fee': 1},
            {'allocation': 0},
            {'max_daily_loss': 1},
            {'initial': 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    simulate(bars, **kwargs)

        self.assertEqual(infer_currency('crypto', 'BTC_USD', None), 'USD')
        with self.assertRaises(ValueError):
            infer_currency('crypto', 'BTC_USD', 'PKR')
        self.assertEqual(validate_currencies('pkr', 'PKR'), ('PKR', 'PKR'))
        with self.assertRaises(ValueError):
            validate_currencies('PKR', 'USD')

    def test_cli_rejects_pkr_for_usd_crypto(self):
        cmd = [
            sys.executable, 'agent.py', '--market', 'crypto', '--symbol', 'BTC_USD',
            '--csv', 'crypto_sample.csv', '--initial', '5000',
            '--dataset-kind', 'synthetic',
            '--data-source', 'synthetic sample', '--quote-currency', 'USD',
            '--capital-currency', 'PKR',
        ]
        result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('differs from price currency', result.stderr)

    def test_real_dataset_label_requires_ohlcv_and_rejects_bundled_samples(self):
        close_only = bars_from_prices([100] * 80)
        ohlcv = [Bar(bar.day, bar.close, 100, 110, 90, 1000) for bar in close_only]

        self.assertEqual(validate_dataset_kind('data/real/btc.csv', ohlcv, 'real'), 'real')
        with self.assertRaisesRegex(ValueError, 'OHLCV'):
            validate_dataset_kind('data/real/btc.csv', close_only, 'real')
        with self.assertRaisesRegex(ValueError, 'cannot be labeled real'):
            validate_dataset_kind('crypto_sample.csv', ohlcv, 'real')
        with self.assertRaises(ValueError):
            validate_dataset_kind('data/real/btc.csv', ohlcv, 'unknown')


if __name__ == '__main__':
    unittest.main()

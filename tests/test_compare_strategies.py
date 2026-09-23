import unittest
from datetime import date, timedelta

from agent import Bar
from compare_strategies import (BASE_SETTINGS, run_comparison, simulate_candidate,
                                strategy_target, validate_daily_range)


def daily_bars(start, end, close_fn):
    bars = []
    day = start
    index = 0
    while day <= end:
        close = float(close_fn(index, day))
        bars.append(Bar(day, close, close, close * 1.01, close * 0.99, 1.0))
        day += timedelta(days=1)
        index += 1
    return bars


class StrategyComparisonTests(unittest.TestCase):
    def test_strategy_signals_match_preregistered_rules(self):
        rising = list(range(1, 221))
        falling = list(range(220, 0, -1))

        self.assertTrue(strategy_target('sma_crossover', {'fast': 5, 'slow': 20}, rising, False))
        self.assertFalse(strategy_target('sma_crossover', {'fast': 5, 'slow': 20}, falling, False))
        self.assertTrue(strategy_target('price_sma_filter', {'window': 100}, rising, False))
        self.assertFalse(strategy_target('price_sma_filter', {'window': 100}, falling, False))
        self.assertTrue(strategy_target('donchian_breakout', {'entry': 20, 'exit': 10}, rising, False))
        self.assertFalse(strategy_target('donchian_breakout', {'entry': 20, 'exit': 10}, falling, True))

    def test_donchian_retains_position_between_exit_and_entry_levels(self):
        prices = [100] * 21 + [101, 100.5]
        params = {'entry': 20, 'exit': 10}

        self.assertTrue(strategy_target('donchian_breakout', params, prices[:-1], True))
        self.assertTrue(strategy_target('donchian_breakout', params, prices, True))
        self.assertFalse(strategy_target('donchian_breakout', params, prices, False))

    def test_open_fill_uses_only_prior_completed_closes(self):
        start = date(2025, 1, 1)
        closes = [100] * 20 + [110, 50]
        bars = [Bar(start + timedelta(days=i), close, 120, 130, 40, 1)
                for i, close in enumerate(closes)]
        result = simulate_candidate(
            bars, 'sma_crossover', {'fast': 2, 'slow': 3}, start=0,
            stop=len(bars), settings=dict(BASE_SETTINGS, initial=1000, fee=0,
                                           slippage=0, allocation=1))

        self.assertEqual(result['trades'][0]['signal_date'], '2025-01-21')
        self.assertEqual(result['trades'][0]['date'], '2025-01-22')
        self.assertEqual(result['trades'][0]['price'], 120)

    def test_comparison_reports_every_candidate_and_seals_holdout_selection(self):
        development = daily_bars(
            date(2021, 1, 1), date(2024, 12, 31),
            lambda i, day: 100 + (i % 200) * 0.2)
        later_a = daily_bars(
            date(2025, 1, 1), date(2026, 8, 31),
            lambda i, day: 120 + i * 0.1)
        later_b = daily_bars(
            date(2025, 1, 1), date(2026, 8, 31),
            lambda i, day: 120 + i * 0.1 if day.year == 2025 else 300 - i * 0.1)

        first = run_comparison(development, later_a)
        second = run_comparison(development, later_b)

        self.assertEqual(len(first['all_candidates']), 9)
        self.assertTrue(all(len(item['validation_periods']) == 3
                            for item in first['all_candidates']))
        self.assertEqual(
            [(item['strategy'], item['parameters']) for item in first['family_champions']],
            [(item['strategy'], item['parameters']) for item in second['family_champions']])
        self.assertEqual(first['all_candidates'], second['all_candidates'])
        self.assertNotEqual(
            [item['untouched_holdout_2026']['return_pct'] for item in first['family_champions']],
            [item['untouched_holdout_2026']['return_pct'] for item in second['family_champions']])

    def test_planned_daily_boundaries_are_mandatory(self):
        bars = daily_bars(date(2025, 1, 2), date(2026, 8, 31), lambda i, day: 100)
        with self.assertRaisesRegex(ValueError, 'cover exactly'):
            validate_daily_range(bars, date(2025, 1, 1), date(2026, 8, 31), 'Later data')

        bars = daily_bars(date(2025, 1, 1), date(2026, 8, 31), lambda i, day: 100)
        bars.pop(100)
        with self.assertRaisesRegex(ValueError, 'every UTC calendar day'):
            validate_daily_range(bars, date(2025, 1, 1), date(2026, 8, 31), 'Later data')


if __name__ == '__main__':
    unittest.main()

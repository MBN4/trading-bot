"""Preregistered, offline BTCUSDT strategy comparison. Paper research only.

rsi_mean_reversion and macd_crossover were added after the original
2026-09-23 plan was frozen and its 2024/2026 holdouts were consumed (see
RESEARCH_PLAN.md). Running them against the 2022-2024 development/validation
periods for candidate selection is consistent with the existing protocol
(those are not holdouts). But their results over the 2025 confirmation and
2026 holdout periods are NOT a valid preregistered confirmation test: that
data was already viewed before these strategies existed, so any outcome
there is descriptive/exploratory only and must not be reported as evidence
toward promotion. A real confirmation for these candidates requires
genuinely new data dated after they were added, selected and frozen,
exactly like the original plan required for its own candidates.
"""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from agent import buy_and_hold_benchmark, maximum_drawdown, read_bars, validate_currencies
from paper import read_metadata


ORIGINAL_FROZEN_PLAN_STRATEGIES = {'sma_crossover', 'price_sma_filter', 'donchian_breakout'}

STRATEGY_GRIDS = {
    'sma_crossover': [
        {'fast': 5, 'slow': 20},
        {'fast': 10, 'slow': 30},
        {'fast': 15, 'slow': 40},
    ],
    'price_sma_filter': [
        {'window': 100},
        {'window': 150},
        {'window': 200},
    ],
    'donchian_breakout': [
        {'entry': 20, 'exit': 10},
        {'entry': 55, 'exit': 20},
        {'entry': 100, 'exit': 40},
    ],
    'rsi_mean_reversion': [
        {'period': 14, 'oversold': 30, 'exit_level': 50},
        {'period': 14, 'oversold': 25, 'exit_level': 55},
        {'period': 21, 'oversold': 30, 'exit_level': 50},
    ],
    'macd_crossover': [
        {'fast': 12, 'slow': 26, 'signal': 9},
        {'fast': 8, 'slow': 17, 'signal': 9},
        {'fast': 12, 'slow': 26, 'signal': 12},
    ],
}


def ema_series(values, period):
    """Exponential moving average over the full series, oldest first."""
    if not values:
        return []
    multiplier = 2 / (period + 1)
    series = [values[0]]
    for value in values[1:]:
        series.append((value - series[-1]) * multiplier + series[-1])
    return series


def rsi_value(closes, period):
    """Simple-average RSI over the most recent `period` changes, or None if not enough history."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for index in range(len(closes) - period, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def macd_value(closes, fast, slow, signal_period):
    """Return (macd_line, signal_line) at the latest close, or (None, None) if not enough history."""
    if len(closes) < slow + signal_period:
        return None, None
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = ema_series(macd_line, signal_period)
    return macd_line[-1], signal_line[-1]

BASE_SETTINGS = {
    'initial': 5000.0,
    'fee': 0.002,
    'slippage': 0.001,
    'allocation': 0.20,
    'max_daily_loss': 0.03,
}


def strategy_target(strategy, params, completed_closes, currently_long):
    """Return desired position using completed closes only."""
    if strategy == 'sma_crossover':
        fast, slow = params['fast'], params['slow']
        if len(completed_closes) < slow:
            return False
        return (sum(completed_closes[-fast:]) / fast
                > sum(completed_closes[-slow:]) / slow)
    if strategy == 'price_sma_filter':
        window = params['window']
        if len(completed_closes) < window:
            return False
        return completed_closes[-1] > sum(completed_closes[-window:]) / window
    if strategy == 'donchian_breakout':
        lookback = params['exit'] if currently_long else params['entry']
        if len(completed_closes) < lookback + 1:
            return currently_long
        latest = completed_closes[-1]
        prior = completed_closes[-lookback - 1:-1]
        if currently_long:
            return not (latest < min(prior))
        return latest > max(prior)
    if strategy == 'rsi_mean_reversion':
        value = rsi_value(completed_closes, params['period'])
        if value is None:
            return False
        if currently_long:
            return value < params['exit_level']
        return value < params['oversold']
    if strategy == 'macd_crossover':
        macd_line, signal_line = macd_value(
            completed_closes, params['fast'], params['slow'], params['signal'])
        if macd_line is None:
            return False
        return macd_line > signal_line
    raise ValueError(f'Unknown strategy: {strategy}')


def simulate_candidate(bars, strategy, params, *, start, stop, settings):
    """Signal after a completed close and fill at the next row's open."""
    cash, units = settings['initial'], 0.0
    trades, curve, raw_curve = [], [], []
    prior_equity = settings['initial']
    paused_for_next_open = False
    closes = [bar.close for bar in bars]
    for index in range(start, stop):
        bar = bars[index]
        target = strategy_target(strategy, params, closes[:index], units > 0)
        if units > 0 and not target:
            price = bar.open * (1 - settings['slippage'])
            gross = units * price
            fee = gross * settings['fee']
            cash += gross - fee
            trades.append({'date': str(bar.day), 'signal_date': str(bars[index - 1].day),
                           'side': 'SELL', 'open': bar.open, 'price': price,
                           'units': units, 'fee': fee})
            units = 0.0
        elif units == 0 and target and not paused_for_next_open:
            spend = min(cash, prior_equity * settings['allocation'])
            fee = spend * settings['fee']
            price = bar.open * (1 + settings['slippage'])
            units = (spend - fee) / price
            cash -= spend
            trades.append({'date': str(bar.day), 'signal_date': str(bars[index - 1].day),
                           'side': 'BUY', 'open': bar.open, 'price': price,
                           'units': units, 'fee': fee})
        equity = cash + units * bar.close
        paused_for_next_open = equity < prior_equity * (1 - settings['max_daily_loss'])
        raw_curve.append(equity)
        curve.append({'date': str(bar.day), 'equity': round(equity, 2)})
        prior_equity = equity
    final = cash + units * bars[stop - 1].close
    return {
        'return_pct': round(100 * (final / settings['initial'] - 1), 2),
        'max_drawdown_pct': maximum_drawdown([settings['initial']] + raw_curve),
        'trade_count': len(trades),
        'final': round(final, 2),
        'cash': round(cash, 2),
        'units': round(units, 8),
        'trades': trades,
        'equity': curve,
    }


def period_result(bars, strategy, params, start, stop, settings):
    result = simulate_candidate(
        bars, strategy, params, start=start, stop=stop, settings=settings)
    benchmark = buy_and_hold_benchmark(
        bars, initial=settings['initial'], fee=settings['fee'],
        slippage=settings['slippage'], start=start, stop=stop)
    result.update({
        'date_range': {'start': str(bars[start].day), 'end': str(bars[stop - 1].day)},
        'fee_rate': settings['fee'],
        'slippage_rate': settings['slippage'],
        'cash_return_pct': 0.0,
        'buy_hold_return_pct': benchmark['return_pct'],
        'buy_hold_max_drawdown_pct': benchmark['max_drawdown_pct'],
        'buy_hold_trade_count': benchmark['trade_count'],
    })
    return result


def exact_period(bars, start_day, end_day):
    lookup = {bar.day: index for index, bar in enumerate(bars)}
    try:
        return lookup[start_day], lookup[end_day] + 1
    except KeyError as exc:
        raise ValueError(f'Dataset is missing planned boundary date {exc.args[0]}') from exc


def validate_daily_range(bars, expected_start, expected_end, name):
    if bars[0].day != expected_start or bars[-1].day != expected_end:
        raise ValueError(f'{name} must cover exactly {expected_start} through {expected_end}')
    if any(right.day - left.day != timedelta(days=1)
           for left, right in zip(bars, bars[1:])):
        raise ValueError(f'{name} must contain every UTC calendar day')


def compact_period(result):
    return {key: value for key, value in result.items() if key not in ('trades', 'equity')}


def run_comparison(development_bars, later_bars):
    validate_daily_range(development_bars, date(2021, 1, 1), date(2024, 12, 31),
                         'Development data')
    validate_daily_range(later_bars, date(2025, 1, 1), date(2026, 8, 31),
                         'Later data')
    combined = development_bars + later_bars
    validation_boundaries = [
        exact_period(development_bars, date(year, 1, 1), date(year, 12, 31))
        for year in (2022, 2023, 2024)
    ]
    confirmation = exact_period(combined, date(2025, 1, 1), date(2025, 12, 31))
    holdout = exact_period(combined, date(2026, 1, 1), date(2026, 8, 31))

    all_candidates = []
    champions = []
    for strategy, grid in STRATEGY_GRIDS.items():
        family = []
        for order, params in enumerate(grid):
            periods = [period_result(development_bars, strategy, params, start, stop,
                                     BASE_SETTINGS)
                       for start, stop in validation_boundaries]
            candidate = {
                'strategy': strategy,
                'parameters': params,
                'validation_periods': [compact_period(period) for period in periods],
                'mean_validation_return_pct': round(
                    sum(period['return_pct'] for period in periods) / len(periods), 2),
                'positive_validation_periods': sum(period['return_pct'] > 0 for period in periods),
                'total_validation_trades': sum(period['trade_count'] for period in periods),
                '_order': order,
            }
            family.append(candidate)
            all_candidates.append(candidate)
        champion = max(family, key=lambda item: (
            item['mean_validation_return_pct'],
            -item['total_validation_trades'],
            -item['_order']))
        champions.append({'strategy': strategy, 'parameters': champion['parameters'],
                          'development_summary': champion})

    champion_results = []
    for champion in champions:
        strategy, params = champion['strategy'], champion['parameters']
        confirmation_result = period_result(
            combined, strategy, params, *confirmation, BASE_SETTINGS)
        holdout_result = period_result(combined, strategy, params, *holdout, BASE_SETTINGS)
        sensitivities = []
        for label, fee, slippage in (
                ('moderate_costs', 0.003, 0.002),
                ('higher_costs', 0.005, 0.003)):
            scenario = dict(BASE_SETTINGS, fee=fee, slippage=slippage)
            result = period_result(combined, strategy, params, *holdout, scenario)
            sensitivities.append({
                'scenario': label,
                'fee_rate': fee,
                'slippage_rate': slippage,
                'return_pct': result['return_pct'],
                'max_drawdown_pct': result['max_drawdown_pct'],
                'trade_count': result['trade_count'],
                'buy_hold_return_pct': result['buy_hold_return_pct'],
            })
        development = champion['development_summary']
        criteria = {
            'positive_mean_validation': development['mean_validation_return_pct'] > 0,
            'positive_at_least_two_validation_periods': development['positive_validation_periods'] >= 2,
            'positive_2025_confirmation': confirmation_result['return_pct'] > 0,
            'positive_2026_holdout': holdout_result['return_pct'] > 0,
            'holdout_drawdown_no_worse_than_minus_15_pct': holdout_result['max_drawdown_pct'] >= -15,
            'holdout_trade_count_between_2_and_30': 2 <= holdout_result['trade_count'] <= 30,
            'positive_holdout_under_moderate_costs': sensitivities[0]['return_pct'] > 0,
        }
        in_original_plan = strategy in ORIGINAL_FROZEN_PLAN_STRATEGIES
        champion_results.append({
            'strategy': strategy,
            'parameters': params,
            'development_summary': {key: value for key, value in development.items()
                                    if key != '_order'},
            'confirmation_2025': compact_period(confirmation_result),
            'untouched_holdout_2026': compact_period(holdout_result),
            'holdout_cost_sensitivity': sensitivities,
            'success_criteria': criteria,
            'part_of_original_frozen_plan': in_original_plan,
            'confirmation_and_holdout_are_valid_preregistered_tests': in_original_plan,
            'earned_further_paper_testing': all(criteria.values()) and in_original_plan,
        })
        if not in_original_plan:
            champion_results[-1]['warning'] = (
                'This strategy was added after the 2025/2026 periods above were already '
                'viewed and consumed as holdouts for the original plan. Its confirmation_2025 '
                'and untouched_holdout_2026 figures are descriptive/exploratory only, not a '
                'valid preregistered confirmation, so earned_further_paper_testing is forced '
                'to false regardless of the numbers. A real test requires genuinely new data.'
            )

    for candidate in all_candidates:
        candidate.pop('_order')
    return {
        'mode': 'offline_paper_only',
        'plan': 'RESEARCH_PLAN.md frozen before later-data evaluation',
        'currency': 'USDT',
        'pk_budget_conversion_performed': False,
        'development_date_range': {'start': '2021-01-01', 'end': '2024-12-31'},
        'confirmation_date_range': {'start': '2025-01-01', 'end': '2025-12-31'},
        'untouched_holdout_date_range': {'start': '2026-01-01', 'end': '2026-08-31'},
        'base_settings': BASE_SETTINGS,
        'all_candidates': all_candidates,
        'family_champions': champion_results,
        'any_strategy_earned_further_paper_testing': any(
            result['earned_further_paper_testing'] for result in champion_results),
    }


def main():
    parser = argparse.ArgumentParser(description='Preregistered offline strategy comparison.')
    parser.add_argument('--development-csv', required=True, type=Path)
    parser.add_argument('--development-metadata', required=True, type=Path)
    parser.add_argument('--later-csv', required=True, type=Path)
    parser.add_argument('--later-metadata', required=True, type=Path)
    parser.add_argument('--out', type=Path,
                        default=Path('results/crypto_BTCUSDT_strategy_comparison.json'))
    args = parser.parse_args()
    try:
        validate_currencies('USDT', 'USDT')
        read_metadata(args.development_metadata, market='crypto', symbol='BTCUSDT',
                      quote_currency='USDT')
        read_metadata(args.later_metadata, market='crypto', symbol='BTCUSDT',
                      quote_currency='USDT')
        report = run_comparison(read_bars(args.development_csv), read_bars(args.later_csv))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    summary = dict(report)
    summary['all_candidates'] = [
        {key: value for key, value in candidate.items() if key != 'validation_periods'}
        for candidate in report['all_candidates']
    ]
    print(json.dumps(summary, indent=2))
    print(f'Saved {args.out}')


if __name__ == '__main__':
    main()

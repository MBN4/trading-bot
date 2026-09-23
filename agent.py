"""Offline CSV-based walk-forward research. Never connects to or trades on a venue."""
import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    day: date
    close: float
    open: float = None
    high: float = None
    low: float = None
    volume: float = None


def read_bars(path):
    """Read strict daily close-only or OHLCV data without filling or sorting rows."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        close_only = fields == ['date', 'close']
        ohlcv = fields == ['date', 'open', 'high', 'low', 'close', 'volume']
        if not (close_only or ohlcv):
            raise ValueError('CSV header must be exactly date,close or date,open,high,low,close,volume')
        rows = list(reader)
    if not rows:
        raise ValueError('CSV is empty')

    bars = []
    numeric_fields = ['close'] if close_only else ['open', 'high', 'low', 'close', 'volume']
    for line_no, row in enumerate(rows, start=2):
        raw_day = (row.get('date') or '').strip()
        if not raw_day or any(not (row.get(name) or '').strip() for name in numeric_fields):
            raise ValueError(f'Missing value on line {line_no}')
        try:
            day = date.fromisoformat(raw_day)
            values = {name: float(row[name]) for name in numeric_fields}
        except ValueError as exc:
            raise ValueError(f'Invalid date or numeric value on line {line_no}') from exc
        if any(not isfinite(value) for value in values.values()):
            raise ValueError(f'Non-finite numeric value on line {line_no}')
        if values['close'] <= 0:
            raise ValueError(f'Prices must be positive on line {line_no}')
        if ohlcv:
            if any(values[name] <= 0 for name in ('open', 'high', 'low')):
                raise ValueError(f'Prices must be positive on line {line_no}')
            if values['volume'] < 0:
                raise ValueError(f'Volume must be non-negative on line {line_no}')
            if (values['low'] > min(values['open'], values['close'])
                    or values['high'] < max(values['open'], values['close'])
                    or values['low'] > values['high']):
                raise ValueError(f'Inconsistent OHLC prices on line {line_no}')
            bars.append(Bar(day, values['close'], values['open'], values['high'],
                            values['low'], values['volume']))
        else:
            bars.append(Bar(day, values['close']))
    if len(bars) < 80:
        raise ValueError('Need at least 80 daily rows')
    if any(a.day >= b.day for a, b in zip(bars, bars[1:])):
        raise ValueError('Dates must be unique and strictly ascending')
    return bars


def signal(prices, fast, slow):
    if len(prices) < slow:
        return False
    return sum(prices[-fast:]) / fast > sum(prices[-slow:]) / slow


def maximum_drawdown(equity_values):
    peak = equity_values[0]
    worst = 0.0
    for value in equity_values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return round(100 * worst, 2)


def simulate(bars, *, initial=5000.0, fast=5, slow=20, fee=0.002,
             slippage=0.0, allocation=0.20, max_daily_loss=0.03, start=0, stop=None):
    """Long-only; signal after close, execute at the next row's open."""
    if not (initial > 0 and 0 < fast < slow and 0 <= fee < 1 and 0 <= slippage < 1
            and 0 < allocation <= 1 and 0 < max_daily_loss < 1):
        raise ValueError('Invalid strategy or risk settings')
    stop = len(bars) if stop is None else stop
    if start < 0 or stop > len(bars) or start >= stop:
        raise ValueError('Invalid simulation range')
    cash, units = initial, 0.0
    trades, curve, raw_curve = [], [], []
    prior_equity = initial
    paused_for_next_open = False
    prices = [b.close for b in bars]
    for i in range(start, stop):
        bar = bars[i]
        close = prices[i]
        execution_reference = bar.open if bar.open is not None else close
        target = signal(prices[:i], fast, slow) if i > 0 else False
        if units and not target:
            execution_price = execution_reference * (1 - slippage)
            gross = units * execution_price
            trade_fee = gross * fee
            trades.append({'date': str(bar.day), 'signal_date': str(bars[i - 1].day),
                           'side': 'SELL', 'open': bar.open, 'close': close,
                           'price': execution_price, 'units': units, 'fee': trade_fee})
            cash += gross - trade_fee
            units = 0.0
        elif not units and target and not paused_for_next_open:
            spend = min(cash, prior_equity * allocation)
            trade_fee = spend * fee
            execution_price = execution_reference * (1 + slippage)
            bought = (spend - trade_fee) / execution_price
            if bought > 0:
                cash -= spend
                units = bought
                trades.append({'date': str(bar.day), 'signal_date': str(bars[i - 1].day),
                               'side': 'BUY', 'open': bar.open, 'close': close,
                               'price': execution_price, 'units': bought, 'fee': trade_fee})
        equity = cash + units * close
        paused_for_next_open = equity < prior_equity * (1 - max_daily_loss)
        raw_curve.append(equity)
        curve.append({'date': str(bars[i].day), 'equity': round(equity, 2)})
        prior_equity = equity
    final = cash + units * prices[stop - 1]
    return {'initial': initial, 'final': round(final, 2),
            'return_pct': round(100 * (final / initial - 1), 2),
            'max_drawdown_pct': maximum_drawdown([initial] + raw_curve),
            'cash': round(cash, 2), 'units': round(units, 8),
            'trade_count': len(trades), 'trades': trades, 'equity': curve}


def validate_currencies(capital_currency, quote_currency):
    capital = capital_currency.strip().upper()
    quote = quote_currency.strip().upper()
    if not capital or not quote:
        raise ValueError('Capital and quote currencies are required')
    if capital != quote:
        raise ValueError(f'Capital currency {capital} differs from price currency {quote}; no conversion is performed')
    return capital, quote


def validate_dataset_kind(path, bars, dataset_kind):
    """Keep bundled fixtures out of reports represented as real-market evaluations."""
    if dataset_kind not in {'synthetic', 'real'}:
        raise ValueError('Dataset kind must be synthetic or real')
    if dataset_kind == 'real':
        if Path(path).name in {'stock_sample.csv', 'crypto_sample.csv'}:
            raise ValueError('Bundled sample files are synthetic and cannot be labeled real')
        if any(bar.open is None for bar in bars):
            raise ValueError('Real datasets must use date,open,high,low,close,volume OHLCV rows')
    return dataset_kind


def infer_currency(market, symbol, requested):
    """Compatibility helper; explicit CLI runs require both currencies."""
    currency = requested.strip().upper() if requested else None
    normalized = symbol.upper().replace('-', '_')
    quote = normalized.rsplit('_', 1)[-1] if '_' in normalized else None
    if market == 'crypto' and quote in {'USD', 'USDT', 'USDC'}:
        if currency and currency != 'USD':
            raise ValueError(f'{symbol} appears USD-quoted; currency must be USD or prices must be converted')
        return 'USD'
    return currency or 'input currency (no FX conversion)'


def select_windows(bars, stop, settings):
    candidates = [(5, 20), (10, 30), (15, 40)]
    scores = [(simulate(bars, fast=f, slow=s, stop=stop, **settings)['final'], f, s)
              for f, s in candidates]
    _, fast, slow = max(scores)
    return fast, slow


def buy_and_hold_benchmark(bars, *, initial, fee, slippage, start, stop):
    """Buy at the first period open and mark to the final close, with entry costs."""
    reference = bars[start].open if bars[start].open is not None else bars[start].close
    execution_price = reference * (1 + slippage)
    entry_fee = initial * fee
    units = (initial - entry_fee) / execution_price
    values = [units * bar.close for bar in bars[start:stop]]
    final = values[-1]
    return {
        'return_pct': round(100 * (final / initial - 1), 2),
        'max_drawdown_pct': maximum_drawdown([initial] + values),
        'trade_count': 1,
        'entry_price': execution_price,
        'entry_fee': entry_fee,
    }


def evaluated_period(bars, settings, *, start, stop, fast, slow, role, number=None):
    result = simulate(bars, fast=fast, slow=slow, start=start, stop=stop, **settings)
    benchmark = buy_and_hold_benchmark(
        bars, initial=settings['initial'], fee=settings['fee'],
        slippage=settings['slippage'], start=start, stop=stop)
    result.update({
        'role': role, 'period': number,
        'training_date_range': {'start': str(bars[0].day), 'end': str(bars[start - 1].day)},
        'test_date_range': {'start': str(bars[start].day), 'end': str(bars[stop - 1].day)},
        'training_rows': start, 'test_rows': stop - start,
        'selected_windows': {'fast': fast, 'slow': slow},
        'fee_rate': settings['fee'], 'slippage_rate': settings['slippage'],
        'cash_return_pct': 0.0, 'cash_max_drawdown_pct': 0.0,
        'buy_hold_return_pct': benchmark['return_pct'],
        'buy_hold_max_drawdown_pct': benchmark['max_drawdown_pct'],
        'buy_hold_trade_count': benchmark['trade_count'],
    })
    return result


def walk_forward_test(bars, settings, folds=3):
    """Use chronological validation folds, then one untouched final holdout."""
    settings = dict({'initial': 5000.0, 'fee': 0.002, 'slippage': 0.0,
                     'allocation': 0.20, 'max_daily_loss': 0.03}, **settings)
    if not 1 <= folds <= 10:
        raise ValueError('Folds must be between 1 and 10')
    holdout_start = int(len(bars) * 0.8)
    validation_start = int(len(bars) * 0.40)
    validation_rows = holdout_start - validation_start
    if folds > validation_rows or holdout_start >= len(bars):
        raise ValueError('More folds than validation rows or no final holdout rows')
    base, extra = divmod(validation_rows, folds)
    validation_periods, boundaries, start = [], [], validation_start
    for fold in range(folds):
        stop = start + base + (1 if fold < extra else 0)
        fast, slow = select_windows(bars, start, settings)
        validation_periods.append(evaluated_period(
            bars, settings, start=start, stop=stop, fast=fast, slow=slow,
            role='validation', number=fold + 1))
        boundaries.append((start, stop))
        start = stop

    candidates = [(5, 20), (10, 30), (15, 40)]
    candidate_scores = []
    for fast, slow in candidates:
        returns = [simulate(bars, fast=fast, slow=slow, start=start, stop=stop,
                            **settings)['return_pct'] for start, stop in boundaries]
        candidate_scores.append({'fast': fast, 'slow': slow,
                                 'mean_validation_return_pct': round(sum(returns) / len(returns), 2),
                                 'period_returns_pct': returns})
    winner = max(candidate_scores,
                 key=lambda item: (item['mean_validation_return_pct'], -item['slow'], -item['fast']))
    holdout = evaluated_period(
        bars, settings, start=holdout_start, stop=len(bars),
        fast=winner['fast'], slow=winner['slow'], role='final_holdout')

    sensitivity = []
    for label, fee, slippage in (
            ('base', settings['fee'], settings['slippage']),
            ('moderate_costs', settings['fee'] + 0.001, settings['slippage'] + 0.001),
            ('higher_costs', settings['fee'] + 0.003, settings['slippage'] + 0.002)):
        scenario = dict(settings, fee=fee, slippage=slippage)
        result = evaluated_period(
            bars, scenario, start=holdout_start, stop=len(bars),
            fast=winner['fast'], slow=winner['slow'], role='sensitivity')
        sensitivity.append({
            'scenario': label, 'fee_rate': fee, 'slippage_rate': slippage,
            'strategy_return_pct': result['return_pct'],
            'buy_hold_return_pct': result['buy_hold_return_pct'],
            'max_drawdown_pct': result['max_drawdown_pct'],
            'trade_count': result['trade_count'],
        })
    return {
        'walk_forward_folds': validation_periods, 'fold_count': folds,
        'final_holdout': holdout, 'validation_candidate_scores': candidate_scores,
        'locked_holdout_windows': {'fast': winner['fast'], 'slow': winner['slow']},
        'sensitivity_analysis': sensitivity,
        'split_policy': {
            'initial_training_fraction': 0.40,
            'validation_end_fraction': 0.80,
            'final_holdout_fraction': 0.20,
            'holdout_used_for_selection': False,
        },
        'risk_note': 'max_daily_loss only blocks new buys after an observed close-to-close equity drop; it is not a stop-loss or maximum-loss guarantee',
    }


def train_then_test(bars, settings):
    """Compatibility wrapper returning the sealed holdout from one validation fold."""
    result = walk_forward_test(bars, settings, folds=1)
    period = result['final_holdout']
    period['risk_note'] = result['risk_note']
    return period


def main():
    p = argparse.ArgumentParser(description='Offline stock and crypto paper research using CSV input.')
    p.add_argument('--market', choices=['stock', 'crypto'], required=True)
    p.add_argument('--symbol', required=True)
    p.add_argument('--dataset-kind', choices=['synthetic', 'real'], required=True)
    p.add_argument('--data-source', required=True, help='Provider or origin of the permitted CSV')
    p.add_argument('--quote-currency', required=True, help='Currency of all prices in the CSV')
    p.add_argument('--capital-currency', required=True, help='Currency of the starting capital')
    p.add_argument('--csv', required=True, type=Path)
    p.add_argument('--initial', type=float, default=5000)
    p.add_argument('--fee', type=float, default=0.002, help='Estimated fee per side')
    p.add_argument('--slippage', type=float, default=0.0, help='Adverse execution slippage per side')
    p.add_argument('--allocation', type=float, default=0.20)
    p.add_argument('--max-daily-loss', type=float, default=0.03)
    p.add_argument('--folds', type=int, default=3)
    p.add_argument('--out', type=Path, default=Path('results'))
    args = p.parse_args()
    if args.initial <= 0 or not args.symbol.replace('-', '').replace('_', '').replace('.', '').isalnum():
        p.error('Initial amount must be positive; symbol may contain letters, digits, -, _, and .')
    if not (0 <= args.fee < 1 and 0 <= args.slippage < 1 and 0 < args.allocation <= 1
            and 0 < args.max_daily_loss < 1 and 1 <= args.folds <= 10):
        p.error('Invalid fee, slippage, allocation, max daily loss, or folds')
    try:
        capital_currency, quote_currency = validate_currencies(args.capital_currency, args.quote_currency)
        bars = read_bars(args.csv)
        dataset_kind = validate_dataset_kind(args.csv, bars, args.dataset_kind)
        result = walk_forward_test(bars, {'initial': args.initial, 'fee': args.fee,
                                   'slippage': args.slippage, 'allocation': args.allocation,
                                   'max_daily_loss': args.max_daily_loss}, args.folds)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    result.update({
        'mode': 'offline_paper_only', 'market': args.market, 'symbol': args.symbol,
        'dataset_kind': dataset_kind,
        'data_source': args.data_source, 'quote_currency': quote_currency,
        'capital_currency': capital_currency,
        'data_date_range': {'start': str(bars[0].day), 'end': str(bars[-1].day)},
        'input_rows': len(bars),
        'data_format': 'daily_ohlcv' if bars[0].open is not None else 'daily_close_only',
        'fee_rate': args.fee, 'slippage_rate': args.slippage,
    })
    args.out.mkdir(parents=True, exist_ok=True)
    output = args.out / f'{args.market}_{args.symbol}_{dataset_kind}.json'
    output.write_text(json.dumps(result, indent=2) + '\n')
    summary = {key: value for key, value in result.items()
               if key not in ('walk_forward_folds', 'final_holdout')}
    summary['periods'] = [{key: value for key, value in period.items() if key not in ('trades', 'equity')}
                          for period in result['walk_forward_folds']]
    summary['final_holdout'] = {key: value for key, value in result['final_holdout'].items()
                                if key not in ('trades', 'equity')}
    print(json.dumps(summary, indent=2))
    print(f'Saved {output}')


if __name__ == '__main__':
    main()

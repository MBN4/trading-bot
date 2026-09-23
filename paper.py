"""Persistent offline forward paper trading from local daily OHLCV files."""
import argparse
from contextlib import nullcontext
import errno
import fcntl
import hashlib
import json
import os
import tempfile
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from agent import read_bars, signal, validate_currencies, validate_dataset_kind


STATE_VERSION = 2
GENESIS_HASH = '0' * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


def symbol_key(symbol):
    if not symbol.replace('-', '').replace('_', '').replace('.', '').isalnum():
        raise ValueError('Symbol may contain only letters, digits, -, _, and .')
    return symbol.upper()


def portfolio_paths(root, market, symbol):
    base = Path(root) / market / symbol_key(symbol)
    return (base.with_suffix('.json'), base.with_suffix('.events.jsonl'),
            base.with_suffix('.transaction.json'))


def portfolio_lock_path(root, market, symbol):
    return Path(root) / market / (symbol_key(symbol) + '.lock')


class PortfolioLock:
    """Process-scoped advisory lock; the OS releases it if the process exits."""
    def __init__(self, path, *, shared=False, timeout=0.0, create=False):
        self.path = Path(path)
        self.shared = shared
        self.timeout = timeout
        self.create = create
        self.handle = None

    def __enter__(self):
        if self.create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = self.path.open('a+' if self.create else 'r')
        except OSError as exc:
            raise ValueError(f'Portfolio lock file is missing or unreadable: {self.path}') from exc
        operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.handle.fileno(), operation | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self.handle.close()
                    raise
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise ValueError(f'Portfolio is locked by another process: {self.path}')
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def __exit__(self, exc_type, exc_value, traceback):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def parse_completed_through(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('--completed-through must be an ISO date (YYYY-MM-DD)') from exc


def validate_candle_completeness(bars, completed_through):
    confirmed = parse_completed_through(completed_through)
    if bars[-1].day > confirmed:
        raise ValueError(f'CSV includes {bars[-1].day}, later than completed-through {confirmed}; remove the incomplete row or confirm it after session close')
    return str(confirmed)


METADATA_FIELDS = ('provider', 'usage_permission', 'symbol', 'market',
                   'quote_currency', 'timezone', 'session_close',
                   'retrieval_date', 'adjustment_policy')


def read_metadata(path, *, market=None, symbol=None, quote_currency=None):
    try:
        metadata = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read dataset metadata: {exc}') from exc
    missing = [field for field in METADATA_FIELDS
               if not isinstance(metadata.get(field), str) or not metadata[field].strip()]
    if missing:
        raise ValueError('Dataset metadata has blank or missing fields: ' + ', '.join(missing))
    try:
        date.fromisoformat(metadata['retrieval_date'])
    except ValueError as exc:
        raise ValueError('Metadata retrieval_date must be YYYY-MM-DD') from exc
    expected = {'market': market, 'symbol': symbol_key(symbol) if symbol else None,
                'quote_currency': quote_currency.upper() if quote_currency else None}
    observed = {'market': metadata['market'], 'symbol': symbol_key(metadata['symbol']),
                'quote_currency': metadata['quote_currency'].upper()}
    for field, value in expected.items():
        if value is not None and observed[field] != value:
            raise ValueError(f'Metadata {field} {observed[field]} does not match {value}')
    return metadata


def verify_metadata_csv_hash(metadata, csv_path):
    expected = metadata.get('normalized_csv_sha256')
    if expected is None:
        return 'not recorded'
    if not isinstance(expected, str) or len(expected) != 64 or any(
            character not in '0123456789abcdefABCDEF' for character in expected):
        raise ValueError('Metadata normalized_csv_sha256 is not a valid SHA-256')
    observed = hashlib.sha256(Path(csv_path).read_bytes()).hexdigest()
    if observed != expected.lower():
        raise ValueError(
            f'CSV SHA-256 {observed} does not match metadata normalized_csv_sha256 {expected.lower()}')
    return observed


def same_dataset_identity(first, second):
    return all(first.get(field) == second.get(field) for field in METADATA_FIELDS
               if field not in ('retrieval_date',))


def validate_registered_paper_strategy(path, version_id, fast, slow):
    try:
        registry = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read research registry: {exc}') from exc
    matches = [item for item in registry.get('strategy_versions', [])
               if item.get('id') == version_id]
    if not matches:
        raise ValueError(f'Unknown strategy version: {version_id}')
    version = matches[0]
    if version.get('status') != 'active_paper_baseline':
        raise ValueError(f'Strategy version {version_id} is not active for forward paper use')
    if version.get('parameters') != {'fast': fast, 'slow': slow}:
        raise ValueError(
            f'Paper settings fast={fast}, slow={slow} do not match registered {version_id} parameters {version.get("parameters")}')
    return version


def bar_record(bar):
    return {'date': str(bar.day), 'open': bar.open, 'high': bar.high,
            'low': bar.low, 'close': bar.close, 'volume': bar.volume}


def event(kind, day=None, **details):
    item = {'type': kind}
    if day is not None:
        item['date'] = str(day)
    item.update(details)
    return item


def positive_decimal(value, name, allow_zero=False):
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be numeric') from exc
    if not parsed.is_finite() or (parsed < 0 if allow_zero else parsed <= 0):
        raise ValueError(f'{name} must be {"non-negative" if allow_zero else "positive"}')
    return parsed


def quantity_defaults(market):
    return (1.0, 1.0) if market == 'stock' else (0.00000001, 0.00000001)


def round_quantity_down(value, step):
    value_decimal = positive_decimal(value, 'Quantity', allow_zero=True)
    step_decimal = positive_decimal(step, 'Quantity step')
    return float((value_decimal / step_decimal).to_integral_value(rounding=ROUND_DOWN) * step_decimal)


def validate_settings(initial, fast, slow, fee, slippage, allocation, max_daily_loss,
                      quantity_step, min_quantity, min_notional):
    if not (initial > 0 and 0 < fast < slow and 0 <= fee < 1 and 0 <= slippage < 1
            and 0 < allocation <= 1 and 0 < max_daily_loss < 1):
        raise ValueError('Invalid portfolio or risk settings')
    positive_decimal(quantity_step, 'Quantity step')
    positive_decimal(min_quantity, 'Minimum quantity')
    positive_decimal(min_notional, 'Minimum notional', allow_zero=True)


def proposed_signal(state, bars, index, equity, daily_loss_triggered):
    target = signal([bar.close for bar in bars[:index + 1]], state['fast'], state['slow'])
    day = bars[index].day
    if state['units'] > 0 and not target:
        pending = {'side': 'SELL', 'signal_date': str(day)}
        return pending, event('SIGNAL_ACCEPTED', day, side='SELL', reason='moving_average_exit')
    if state['units'] == 0 and target:
        if daily_loss_triggered:
            return None, event('BUY_BLOCKED', day, side='BUY',
                               reason='daily_loss_circuit_breaker',
                               max_daily_loss=state['max_daily_loss'])
        budget = min(state['cash'], equity * state['allocation'])
        if budget < state['min_notional']:
            return None, event('BUY_BLOCKED', day, side='BUY', reason='below_minimum_notional',
                               budget=budget, minimum=state['min_notional'])
        pending = {'side': 'BUY', 'signal_date': str(day), 'budget': budget}
        return pending, event('SIGNAL_ACCEPTED', day, side='BUY',
                              reason='moving_average_entry', budget=budget,
                              allocation_limit=state['allocation'])
    return None, None


def fill_pending(state, bar):
    pending = state['pending_signal']
    if not pending:
        return None
    if str(bar.day) <= pending['signal_date']:
        raise ValueError('Pending signal cannot fill on or before its signal date')
    side = pending['side']
    if side == 'BUY':
        budget = min(state['cash'], pending['budget'])
        price = bar.open * (1 + state['slippage'])
        raw_units = budget / (price * (1 + state['fee']))
        units = round_quantity_down(raw_units, state['quantity_step'])
        gross = units * price
        fee = gross * state['fee']
        total = gross + fee
        if units < state['min_quantity'] or gross < state['min_notional'] or total > state['cash'] + 1e-9:
            state['pending_signal'] = None
            return event('FILL_BLOCKED', bar.day, signal_date=pending['signal_date'], side='BUY',
                         reason='quantity_or_notional_minimum', raw_units=raw_units,
                         quantity_step=state['quantity_step'], minimum_quantity=state['min_quantity'],
                         minimum_notional=state['min_notional'])
        state['cash'] -= total
        if state['cash'] < 0 and state['cash'] > -1e-9:
            state['cash'] = 0.0
        state['units'] += units
    elif side == 'SELL':
        units = state['units']
        if units <= 0:
            raise ValueError('Pending sell exceeds held units')
        price = bar.open * (1 - state['slippage'])
        gross = units * price
        fee = gross * state['fee']
        total = gross - fee
        state['cash'] += total
        state['units'] = 0.0
    else:
        raise ValueError('Invalid pending signal side')
    state['pending_signal'] = None
    fill = {'signal_date': pending['signal_date'], 'fill_date': str(bar.day),
            'side': side, 'open': bar.open, 'price': price, 'units': units,
            'gross': gross, 'fee': fee, 'cash_change': -total if side == 'BUY' else total}
    state['fills'].append(fill)
    return event('FILL', bar.day, **fill)


def initialize_portfolio(bars, csv_path, *, market, symbol, dataset_kind, data_source,
                         quote_currency, capital_currency, initial=5000.0, fast=5,
                         slow=20, fee=0.002, slippage=0.0, allocation=0.20,
                         max_daily_loss=0.03, quantity_step=None, min_quantity=None,
                         min_notional=0.0, metadata=None, completed_through=None):
    default_step, default_min = quantity_defaults(market)
    quantity_step = default_step if quantity_step is None else quantity_step
    min_quantity = default_min if min_quantity is None else min_quantity
    validate_settings(initial, fast, slow, fee, slippage, allocation, max_daily_loss,
                      quantity_step, min_quantity, min_notional)
    capital_currency, quote_currency = validate_currencies(capital_currency, quote_currency)
    validate_dataset_kind(csv_path, bars, dataset_kind)
    if any(bar.open is None for bar in bars):
        raise ValueError('Forward paper mode requires daily OHLCV rows')
    state = {
        'version': STATE_VERSION, 'mode': 'forward_paper_only', 'market': market,
        'symbol': symbol_key(symbol), 'dataset_kind': dataset_kind,
        'data_source': data_source, 'dataset_path': str(Path(csv_path).resolve()),
        'strategy_version': 'unversioned_legacy', 'quote_currency': quote_currency,
        'capital_currency': capital_currency, 'initial': initial, 'cash': initial,
        'units': 0.0, 'pending_signal': None, 'fills': [], 'equity': initial,
        'last_equity': initial, 'last_processed_date': str(bars[-1].day),
        'processed_bars': [bar_record(bar) for bar in bars],
        'fast': fast, 'slow': slow, 'fee': fee, 'slippage': slippage,
        'allocation': allocation, 'max_daily_loss': max_daily_loss,
        'quantity_step': float(quantity_step), 'min_quantity': float(min_quantity),
        'min_notional': float(min_notional), 'event_count': 0,
        'event_head': GENESIS_HASH, 'dataset_metadata': metadata or {},
        'last_completed_through': completed_through or str(bars[-1].day),
    }
    events = [event('PORTFOLIO_INITIALIZED', bars[-1].day, observed_rows=len(bars),
                    cash=initial, equity=initial, quantity_step=state['quantity_step'],
                    minimum_quantity=state['min_quantity'],
                    minimum_notional=state['min_notional'])]
    pending, decision = proposed_signal(state, bars, len(bars) - 1, initial, False)
    state['pending_signal'] = pending
    if decision:
        events.append(decision)
    return state, events


def process_updates(state, bars, completed_through=None):
    stored = state['processed_bars']
    incoming = [bar_record(bar) for bar in bars]
    if len(incoming) < len(stored):
        raise ValueError('CSV history was shortened; restore the exact processed file or initialize a new portfolio')
    if incoming[:len(stored)] != stored:
        raise ValueError('Previously processed history changed or a backdated row was added; restore the exact old prefix or initialize a new portfolio')
    events = []
    for index in range(len(stored), len(bars)):
        bar = bars[index]
        fill_event = fill_pending(state, bar)
        if fill_event:
            events.append(fill_event)
        equity = state['cash'] + state['units'] * bar.close
        prior_equity = state['last_equity']
        daily_loss = prior_equity > 0 and equity < prior_equity * (1 - state['max_daily_loss'])
        previous_day = date.fromisoformat(state['last_processed_date'])
        events.append(event('CANDLE_PROCESSED', bar.day, close=bar.close, equity=equity,
                            calendar_gap_days=(bar.day - previous_day).days,
                            daily_loss_circuit_breaker=daily_loss))
        pending, decision = proposed_signal(state, bars, index, equity, daily_loss)
        state['pending_signal'] = pending
        if decision:
            events.append(decision)
        state['equity'] = equity
        state['last_equity'] = equity
        state['last_processed_date'] = str(bar.day)
        state['processed_bars'].append(incoming[index])
    if completed_through is not None:
        state['last_completed_through'] = completed_through
    return state, events


def state_checksum(state):
    content = dict(state)
    content.pop('state_checksum', None)
    return digest(content)


def validate_state(state):
    required = {'version', 'mode', 'market', 'symbol', 'cash', 'units', 'fills',
                'pending_signal', 'processed_bars', 'event_count', 'event_head',
                'quantity_step', 'min_quantity', 'min_notional', 'state_checksum'}
    if not isinstance(state, dict) or not required.issubset(state):
        raise ValueError('Portfolio state is malformed or incomplete')
    if state['version'] != STATE_VERSION or state['mode'] != 'forward_paper_only':
        raise ValueError('Unsupported portfolio state version; initialize a new portfolio')
    if state_checksum(state) != state['state_checksum']:
        raise ValueError('Portfolio state checksum mismatch; do not trade or edit files manually')
    if state['cash'] < -1e-9 or state['units'] < 0:
        raise ValueError('Portfolio state has impossible negative cash or units')
    if state['event_count'] < 0 or len(state['event_head']) != 64:
        raise ValueError('Portfolio event checkpoint is invalid')


def read_history(path):
    path = Path(path)
    if not path.exists():
        return []
    events, previous = [], GENESIS_HASH
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        for expected, line in enumerate(lines, start=1):
            if not line.strip():
                raise ValueError(f'blank event line {expected}')
            item = json.loads(line)
            if item.get('sequence') != expected or item.get('previous_hash') != previous:
                raise ValueError(f'event chain mismatch at line {expected}')
            supplied = item.get('event_hash')
            unhashed = dict(item)
            unhashed.pop('event_hash', None)
            if supplied != digest(unhashed):
                raise ValueError(f'event checksum mismatch at line {expected}')
            previous = supplied
            events.append(item)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f'Event history is corrupt: {exc}') from exc
    return events


def verify_checkpoint(state, history):
    head = history[-1]['event_hash'] if history else GENESIS_HASH
    if state['event_count'] != len(history) or state['event_head'] != head:
        raise ValueError('State/event checkpoint mismatch; run the explicit recover command if a transaction journal exists')


def load_state_file(path):
    try:
        state = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read portfolio state: {exc}') from exc
    validate_state(state)
    return state


def load_portfolio(state_path, events_path, transaction_path):
    if Path(transaction_path).exists():
        raise ValueError(f'Interrupted transaction detected at {transaction_path}; run paper.py recover explicitly')
    state = load_state_file(state_path)
    history = read_history(events_path)
    verify_checkpoint(state, history)
    return state, history


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False,
                                     encoding='utf-8') as temporary:
        json.dump(value, temporary, indent=2, sort_keys=True)
        temporary.write('\n')
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def finalized_events(state, events):
    previous = state['event_head']
    sequence = state['event_count']
    result = []
    for raw in events:
        sequence += 1
        item = dict(raw)
        item['recorded_at_utc'] = datetime.now(timezone.utc).isoformat()
        item['sequence'] = sequence
        item['previous_hash'] = previous
        item['event_hash'] = digest(item)
        previous = item['event_hash']
        result.append(item)
    return result


def build_transaction(state, events):
    completed = finalized_events(state, events)
    target = dict(state)
    target['event_count'] += len(completed)
    target['event_head'] = completed[-1]['event_hash'] if completed else target['event_head']
    target['state_checksum'] = state_checksum(target)
    body = {'base_event_count': state['event_count'], 'base_event_head': state['event_head'],
            'events': completed, 'state': target}
    body['transaction_checksum'] = digest(body)
    return body


def validate_transaction(transaction):
    if not isinstance(transaction, dict) or 'transaction_checksum' not in transaction:
        raise ValueError('Transaction journal is malformed')
    supplied = transaction['transaction_checksum']
    body = dict(transaction)
    body.pop('transaction_checksum')
    if supplied != digest(body):
        raise ValueError('Transaction journal checksum mismatch')
    validate_state(transaction['state'])


def append_events(path, events):
    with Path(path).open('a', encoding='utf-8') as history:
        for item in events:
            history.write(canonical(item) + '\n')
        history.flush()
        os.fsync(history.fileno())


def persist(state_path, events_path, transaction_path, state, events, fail_after=None):
    state_path, events_path = Path(state_path), Path(events_path)
    transaction_path = Path(transaction_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    transaction = build_transaction(state, events)
    atomic_json(transaction_path, transaction)
    if fail_after == 'journal':
        raise RuntimeError('simulated interruption after journal')
    append_events(events_path, transaction['events'])
    if fail_after == 'events':
        raise RuntimeError('simulated interruption after events')
    atomic_json(state_path, transaction['state'])
    if fail_after == 'state':
        raise RuntimeError('simulated interruption after state')
    transaction_path.unlink()
    return transaction['state']


def recover(state_path, events_path, transaction_path):
    transaction_path = Path(transaction_path)
    if not transaction_path.exists():
        raise ValueError('No transaction journal exists; nothing can be recovered automatically')
    try:
        transaction = json.loads(transaction_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read transaction journal: {exc}') from exc
    validate_transaction(transaction)
    history = read_history(events_path)
    base_count = transaction['base_event_count']
    base_head = transaction['base_event_head']
    if len(history) < base_count:
        raise ValueError('Event history is shorter than the transaction base; restore from backup')
    observed_base_head = history[base_count - 1]['event_hash'] if base_count else GENESIS_HASH
    if observed_base_head != base_head:
        raise ValueError('Event history does not match the transaction base; restore from backup')
    expected = transaction['events']
    already = len(history) - base_count
    if already > len(expected) or history[base_count:] != expected[:already]:
        raise ValueError('Event history diverges from the transaction journal; restore from backup')
    if already < len(expected):
        append_events(events_path, expected[already:])
    atomic_json(state_path, transaction['state'])
    verify_checkpoint(transaction['state'], read_history(events_path))
    transaction_path.unlink()
    return transaction['state'], len(expected) - already


def status_view(state):
    view = {key: value for key, value in state.items()
            if key not in ('processed_bars', 'state_checksum')}
    view['processed_row_count'] = len(state['processed_bars'])
    return view


def readiness_report(csv_path, metadata_path, *, market, symbol, dataset_kind,
                     quote_currency, capital_currency, completed_through,
                     fast=5, slow=20, quantity_step=None, min_quantity=None,
                     min_notional=0.0, state_paths=None):
    checks, errors = {}, []

    def check(name, operation):
        try:
            value = operation()
            checks[name] = {'ok': True, 'value': value}
            return value
        except (OSError, ValueError) as exc:
            checks[name] = {'ok': False, 'error': str(exc)}
            errors.append(f'{name}: {exc}')
            return None

    currencies = check('currency', lambda: validate_currencies(capital_currency, quote_currency))
    metadata = check('metadata', lambda: read_metadata(
        metadata_path, market=market, symbol=symbol, quote_currency=quote_currency))
    if metadata:
        check('metadata_csv_hash', lambda: verify_metadata_csv_hash(metadata, csv_path))
    else:
        checks['metadata_csv_hash'] = {'ok': False, 'error': 'Metadata could not be validated'}
    try:
        bars = read_bars(csv_path)
        checks['csv'] = {'ok': True, 'value': {
            'rows': len(bars),
            'start': str(bars[0].day),
            'end': str(bars[-1].day),
            'format': 'daily_ohlcv' if bars[0].open is not None else 'daily_close_only',
        }}
    except (OSError, ValueError) as exc:
        bars = None
        checks['csv'] = {'ok': False, 'error': str(exc)}
        errors.append(f'csv: {exc}')
    if bars:
        check('dataset_kind', lambda: validate_dataset_kind(csv_path, bars, dataset_kind))
        check('candle_completeness', lambda: validate_candle_completeness(bars, completed_through))
        checks['history'] = {
            'ok': len(bars) >= slow,
            'value': {'rows': len(bars), 'required_for_slow_window': slow},
        }
        if not checks['history']['ok']:
            errors.append(f'history: need at least {slow} rows for the configured slow window')
    else:
        checks['dataset_kind'] = {'ok': False, 'error': 'CSV could not be validated'}
        checks['candle_completeness'] = {'ok': False, 'error': 'CSV could not be validated'}
        checks['history'] = {'ok': False, 'error': 'CSV could not be validated'}
    default_step, default_min = quantity_defaults(market)
    step = default_step if quantity_step is None else quantity_step
    minimum = default_min if min_quantity is None else min_quantity
    settings = check('quantity_settings', lambda: (
        validate_settings(1, fast, slow, 0, 0, 1, 0.03, step, minimum, min_notional)
        or {'quantity_step': step, 'min_quantity': minimum,
            'min_notional': min_notional}))
    if state_paths:
        state_path, events_path, transaction_path = state_paths
        if transaction_path.exists():
            checks['portfolio_integrity'] = {
                'ok': False,
                'error': f'Pending transaction exists: {transaction_path}; run recover explicitly',
            }
            errors.append('portfolio_integrity: pending transaction requires recovery')
        elif state_path.exists():
            state = check('portfolio_integrity', lambda: load_portfolio(*state_paths)[0])
            if state:
                checks['portfolio_integrity']['value'] = status_view(state)
                if state['market'] != market or state['symbol'] != symbol_key(symbol):
                    errors.append('portfolio_integrity: portfolio identity does not match request')
                    checks['portfolio_integrity'] = {'ok': False, 'error': errors[-1]}
                elif currencies and state['quote_currency'] != currencies[1]:
                    errors.append('portfolio_integrity: portfolio quote currency differs')
                    checks['portfolio_integrity'] = {'ok': False, 'error': errors[-1]}
                elif metadata and not same_dataset_identity(state.get('dataset_metadata', {}), metadata):
                    errors.append('portfolio_integrity: portfolio dataset identity differs from supplied metadata')
                    checks['portfolio_integrity'] = {'ok': False, 'error': errors[-1]}
        else:
            checks['portfolio_integrity'] = {'ok': True, 'value': 'not initialized'}
    return {'ready': not errors, 'checks': checks, 'errors': errors,
            'mode': 'read_only_no_files_changed'}


def build_parser():
    parser = argparse.ArgumentParser(description='Offline forward paper trading from local OHLCV CSVs.')
    parser.add_argument('--state-root', type=Path, default=Path('paper_portfolios'))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--market', choices=['stock', 'crypto'], required=True)
    init.add_argument('--symbol', required=True)
    init.add_argument('--dataset-kind', choices=['synthetic', 'real'], required=True)
    init.add_argument('--data-source', required=True)
    init.add_argument('--quote-currency', required=True)
    init.add_argument('--capital-currency', required=True)
    init.add_argument('--csv', type=Path, required=True)
    init.add_argument('--initial', type=float, default=5000)
    init.add_argument('--fast', type=int, default=5)
    init.add_argument('--slow', type=int, default=20)
    init.add_argument('--strategy-version', required=True,
                      help='Active paper strategy ID from the research registry')
    init.add_argument('--registry', type=Path, default=Path('research_registry.json'))
    init.add_argument('--fee', type=float, default=0.002)
    init.add_argument('--slippage', type=float, default=0.0)
    init.add_argument('--allocation', type=float, default=0.20)
    init.add_argument('--max-daily-loss', type=float, default=0.03)
    init.add_argument('--quantity-step', type=float)
    init.add_argument('--min-quantity', type=float)
    init.add_argument('--min-notional', type=float, default=0.0)
    init.add_argument('--completed-through', required=True,
                      help='Latest ISO date whose daily candle is confirmed complete')
    init.add_argument('--metadata', type=Path,
                      help='JSON metadata file for real datasets')
    init.add_argument('--preview', action='store_true')
    run = sub.add_parser('run')
    run.add_argument('--market', choices=['stock', 'crypto'], required=True)
    run.add_argument('--symbol', required=True)
    run.add_argument('--csv', type=Path, required=True)
    run.add_argument('--metadata', type=Path,
                     help='Updated metadata/provenance for this dataset version')
    run.add_argument('--completed-through', required=True,
                     help='Latest ISO date whose daily candle is confirmed complete')
    run.add_argument('--preview', action='store_true')
    status = sub.add_parser('status')
    status.add_argument('--market', choices=['stock', 'crypto'], required=True)
    status.add_argument('--symbol', required=True)
    status.add_argument('--events', type=int, default=10)
    recovery = sub.add_parser('recover')
    recovery.add_argument('--market', choices=['stock', 'crypto'], required=True)
    recovery.add_argument('--symbol', required=True)
    ready = sub.add_parser('readiness')
    ready.add_argument('--market', choices=['stock', 'crypto'], required=True)
    ready.add_argument('--symbol', required=True)
    ready.add_argument('--dataset-kind', choices=['synthetic', 'real'], required=True)
    ready.add_argument('--quote-currency', required=True)
    ready.add_argument('--capital-currency', required=True)
    ready.add_argument('--csv', type=Path, required=True)
    ready.add_argument('--metadata', type=Path, required=True)
    ready.add_argument('--completed-through', required=True)
    ready.add_argument('--fast', type=int, default=5)
    ready.add_argument('--slow', type=int, default=20)
    ready.add_argument('--quantity-step', type=float)
    ready.add_argument('--min-quantity', type=float)
    ready.add_argument('--min-notional', type=float, default=0.0)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        state_path, events_path, transaction_path = portfolio_paths(
            args.state_root, args.market, args.symbol)
        if args.command == 'recover':
            with PortfolioLock(portfolio_lock_path(args.state_root, args.market, args.symbol),
                               timeout=0.0, create=True):
                state, appended = recover(state_path, events_path, transaction_path)
            print(json.dumps({'recovered': True, 'events_appended': appended,
                              'portfolio': status_view(state)}, indent=2))
            return
        if args.command == 'readiness':
            with PortfolioLock(portfolio_lock_path(args.state_root, args.market, args.symbol),
                               shared=True, timeout=0.0, create=True):
                report = readiness_report(
                    args.csv, args.metadata, market=args.market, symbol=args.symbol,
                    dataset_kind=args.dataset_kind, quote_currency=args.quote_currency,
                    capital_currency=args.capital_currency,
                    completed_through=args.completed_through, fast=args.fast,
                    slow=args.slow, quantity_step=args.quantity_step,
                    min_quantity=args.min_quantity, min_notional=args.min_notional,
                    state_paths=(state_path, events_path, transaction_path))
            print(json.dumps(report, indent=2, default=str))
            if not report['ready']:
                raise SystemExit(2)
            return
        if args.command == 'init':
            lock = nullcontext() if args.preview else PortfolioLock(
                portfolio_lock_path(args.state_root, args.market, args.symbol),
                timeout=0.0, create=True)
            with lock:
                if state_path.exists() or events_path.exists() or transaction_path.exists():
                    raise ValueError('Portfolio files already exist; use run/recover or choose a new state root')
                bars = read_bars(args.csv)
                completed = validate_candle_completeness(bars, args.completed_through)
                metadata = None
                if args.metadata:
                    metadata = read_metadata(args.metadata, market=args.market,
                                             symbol=args.symbol,
                                             quote_currency=args.quote_currency)
                validate_registered_paper_strategy(
                    args.registry, args.strategy_version, args.fast, args.slow)
                state, events = initialize_portfolio(
                    bars, args.csv, market=args.market, symbol=args.symbol,
                    dataset_kind=args.dataset_kind, data_source=args.data_source,
                    quote_currency=args.quote_currency, capital_currency=args.capital_currency,
                    initial=args.initial, fast=args.fast, slow=args.slow, fee=args.fee,
                    slippage=args.slippage, allocation=args.allocation,
                    max_daily_loss=args.max_daily_loss, quantity_step=args.quantity_step,
                    min_quantity=args.min_quantity, min_notional=args.min_notional,
                    metadata=metadata, completed_through=completed)
                state['strategy_version'] = args.strategy_version
                events[0]['strategy_version'] = args.strategy_version
                if not args.preview and events:
                    state = persist(state_path, events_path, transaction_path, state, events)
                    print(json.dumps({'preview': False, 'new_events': events,
                                      'portfolio': status_view(state)}, indent=2))
                    print(f'Committed {state_path} and {events_path}')
                    return
                if args.preview:
                    print(json.dumps({'preview': True, 'new_events': events,
                                      'portfolio': status_view(state)}, indent=2))
                    print('Preview only; no directories or files changed')
                    return
        else:
            shared = args.command == 'status'
            with PortfolioLock(portfolio_lock_path(args.state_root, args.market, args.symbol),
                               shared=shared, timeout=0.0, create=True):
                state, history = load_portfolio(state_path, events_path, transaction_path)
                if args.command == 'status':
                    if args.events < 0:
                        raise ValueError('--events must be non-negative')
                    recent = history[-args.events:] if args.events else []
                    print(json.dumps({'portfolio': status_view(state),
                                      'recent_events': recent}, indent=2))
                    return
                bars = read_bars(args.csv)
                completed = validate_candle_completeness(bars, args.completed_through)
                validate_dataset_kind(args.csv, bars, state['dataset_kind'])
                if any(bar.open is None for bar in bars):
                    raise ValueError('Forward paper mode requires daily OHLCV rows')
                updated_metadata = None
                if args.metadata:
                    updated_metadata = read_metadata(
                        args.metadata, market=state['market'], symbol=state['symbol'],
                        quote_currency=state['quote_currency'])
                    verify_metadata_csv_hash(updated_metadata, args.csv)
                    if not same_dataset_identity(state.get('dataset_metadata', {}), updated_metadata):
                        raise ValueError('Updated metadata dataset identity differs from portfolio')
                state, events = process_updates(state, bars, completed_through=completed)
                state['dataset_path'] = str(args.csv.resolve())
                if updated_metadata is not None:
                    state['dataset_metadata'] = updated_metadata
                if not args.preview and events:
                    state = persist(state_path, events_path, transaction_path, state, events)
                    print(json.dumps({'preview': False, 'new_events': events,
                                      'portfolio': status_view(state)}, indent=2))
                    print(f'Committed {state_path} and {events_path}')
                    return
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not args.preview and events:
        state = persist(state_path, events_path, transaction_path, state, events)
        print(json.dumps({'preview': False, 'new_events': events,
                          'portfolio': status_view(state)}, indent=2))
        print(f'Committed {state_path} and {events_path}')
    elif not args.preview:
        print(json.dumps({'preview': False, 'new_events': [],
                          'portfolio': status_view(state)}, indent=2))
        print('No new rows; no state or event files changed')
    else:
        print(json.dumps({'preview': True, 'new_events': events,
                          'portfolio': status_view(state)}, indent=2))
        print('Preview only; no directories or files changed')


if __name__ == '__main__':
    main()

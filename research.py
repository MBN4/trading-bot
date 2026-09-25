"""Local governance and read-only reporting for paper strategy research."""
import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from agent import Bar, buy_and_hold_benchmark, maximum_drawdown
from paper import (PortfolioLock, atomic_json, load_portfolio, portfolio_lock_path,
                   portfolio_paths)


def read_registry(path):
    try:
        registry = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read research registry: {exc}') from exc
    required = {'version', 'mode', 'decision_policy', 'data_periods',
                'strategy_versions', 'proposals', 'available_fresh_data'}
    if not isinstance(registry, dict) or not required.issubset(registry):
        raise ValueError('Research registry is malformed or incomplete')
    ids = [item.get('id') for item in registry['strategy_versions']]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError('Research registry strategy version IDs must be unique and nonblank')
    proposal_ids = [item.get('id') for item in registry['proposals']]
    if any(not value for value in proposal_ids) or len(proposal_ids) != len(set(proposal_ids)):
        raise ValueError('Research proposal IDs must be unique and nonblank')
    return registry


def strategy_version(registry, version_id):
    matches = [item for item in registry['strategy_versions'] if item['id'] == version_id]
    if not matches:
        raise ValueError(f'Unknown strategy version: {version_id}')
    return matches[0]


def validate_active_paper_strategy(registry, version_id, fast, slow):
    version = strategy_version(registry, version_id)
    if version['status'] != 'active_paper_baseline':
        raise ValueError(f'Strategy version {version_id} is not active for forward paper use')
    expected = version.get('parameters', {})
    if expected != {'fast': fast, 'slow': slow}:
        raise ValueError(
            f'Paper settings fast={fast}, slow={slow} do not match registered {version_id} parameters {expected}')
    return version


def overlaps_consumed_period(registry, start_day, end_day):
    overlaps = []
    for period in registry['data_periods']:
        if period['status'] != 'consumed_holdout':
            continue
        period_start = date.fromisoformat(period['start'])
        period_end = date.fromisoformat(period['end'])
        if start_day <= period_end and end_day >= period_start:
            overlaps.append(period['id'])
    return overlaps


def forward_performance(state, history, registry):
    version_id = state.get('strategy_version', 'unversioned_legacy')
    version = None
    if version_id != 'unversioned_legacy':
        version = strategy_version(registry, version_id)
    candle_events = [item for item in history if item.get('type') == 'CANDLE_PROCESSED']
    limitations = []
    if state.get('dataset_kind') == 'synthetic':
        limitations.append('synthetic_fixture_demo_only')
    if not candle_events:
        limitations.append('no_forward_rows_after_initialization')
        return {
            'mode': 'read_only_no_files_changed',
            'claim_status': 'insufficient_no_forward_rows',
            'eligible_for_performance_claim': False,
            'limitations': limitations,
            'strategy_version': version_id,
            'strategy': version,
            'dataset': {
                'kind': state.get('dataset_kind'),
                'path': state.get('dataset_path', 'not recorded in legacy state'),
                'source': state.get('data_source'),
                'metadata': state.get('dataset_metadata', {}),
            },
            'forward_rows': 0,
        }

    start_day = date.fromisoformat(candle_events[0]['date'])
    end_day = date.fromisoformat(candle_events[-1]['date'])
    period_bars = [record for record in state['processed_bars']
                   if start_day <= date.fromisoformat(record['date']) <= end_day]
    if len(period_bars) != len(candle_events):
        raise ValueError('Forward candle events do not match stored processed bars')
    minimum_rows = registry['decision_policy']['minimum_forward_rows']
    minimum_trades = registry['decision_policy']['minimum_forward_trades']
    fills = [fill for fill in state['fills']
             if start_day <= date.fromisoformat(fill['fill_date']) <= end_day]
    reused = overlaps_consumed_period(registry, start_day, end_day)
    if len(period_bars) < minimum_rows:
        limitations.append(f'short_forward_period_{len(period_bars)}_rows_below_{minimum_rows}')
    if len(fills) < minimum_trades:
        limitations.append(f'trade_count_{len(fills)}_below_{minimum_trades}')
    if reused:
        limitations.append('overlaps_consumed_holdout:' + ','.join(reused))
    if version is None:
        limitations.append('unversioned_legacy_strategy')

    initial = state['initial']
    equity_values = [initial] + [item['equity'] for item in candle_events]
    final_equity = candle_events[-1]['equity']
    bars = [Bar(date.fromisoformat(item['date']), item['close'], item['open'],
                item['high'], item['low'], item['volume']) for item in period_bars]
    benchmark = buy_and_hold_benchmark(
        bars, initial=initial, fee=state['fee'], slippage=state['slippage'],
        start=0, stop=len(bars))

    if state.get('dataset_kind') == 'synthetic':
        claim_status = 'demo_only_synthetic'
    elif len(period_bars) < minimum_rows:
        claim_status = 'insufficient_short_forward_period'
    elif reused:
        claim_status = 'reused_period_exploratory_only'
    elif len(fills) < minimum_trades:
        claim_status = 'insufficient_trade_count'
    elif version is None:
        claim_status = 'unversioned_legacy_state'
    else:
        claim_status = 'fresh_forward_evidence_not_profit_claim'
    return {
        'mode': 'read_only_no_files_changed',
        'claim_status': claim_status,
        'eligible_for_performance_claim': claim_status == 'fresh_forward_evidence_not_profit_claim',
        'limitations': limitations,
        'strategy_version': version_id,
        'strategy': version,
        'dataset': {
            'kind': state['dataset_kind'],
            'path': state.get('dataset_path', 'not recorded in legacy state'),
            'source': state['data_source'],
            'metadata': state.get('dataset_metadata', {}),
            'date_range': {'start': str(start_day), 'end': str(end_day)},
            'forward_rows': len(period_bars),
        },
        'costs': {'fee_rate': state['fee'], 'slippage_rate': state['slippage']},
        'strategy_performance': {
            'initial': initial,
            'final': round(final_equity, 2),
            'return_pct': round(100 * (final_equity / initial - 1), 2),
            'max_drawdown_pct': maximum_drawdown(equity_values),
            'fees_paid': round(sum(fill['fee'] for fill in fills), 8),
            'trade_count': len(fills),
        },
        'cash_benchmark': {
            'return_pct': 0.0,
            'max_drawdown_pct': 0.0,
            'fees_paid': 0.0,
            'trade_count': 0,
        },
        'buy_hold_benchmark': {
            'return_pct': benchmark['return_pct'],
            'max_drawdown_pct': benchmark['max_drawdown_pct'],
            'fees_paid': round(benchmark['entry_fee'], 8),
            'trade_count': benchmark['trade_count'],
        },
    }


def status_report(registry):
    active = [item for item in registry['strategy_versions']
              if item['status'] == 'active_paper_baseline']
    failed = [item for item in registry['strategy_versions']
              if item['status'] == 'evaluated_failed']
    return {
        'mode': registry['mode'],
        'overall_status': 'paper_research_only_no_strategy_passed',
        'current_strategy': active,
        'proposals': registry['proposals'],
        'evaluated_failures': failed,
        'consumed_holdouts': [item for item in registry['data_periods']
                              if item['status'] == 'consumed_holdout'],
        'available_fresh_data': registry['available_fresh_data'],
        'evidence_still_needed': {
            'new_completed_data_after': registry['available_fresh_data']['through'],
            'minimum_forward_rows': registry['decision_policy']['minimum_forward_rows'],
            'minimum_forward_trades': registry['decision_policy']['minimum_forward_trades'],
            'requirements': [
                'preregister a proposal and criteria before evaluation',
                'use real completed candles not previously viewed',
                'keep the proposal isolated from the active paper strategy',
                'compare against cash and cost-matched buy-and-hold',
                'observe positive evidence across predefined periods and higher costs',
            ],
        },
        'warning': 'Automated learning or parameter search cannot eliminate losses or establish live-trading safety.',
    }


def record_proposal(registry, *, proposal_id, base_version, rules, parameters,
                    reasoning, criteria, future_data_start):
    if any(item['id'] == proposal_id for item in registry['proposals']):
        raise ValueError(f'Proposal already exists: {proposal_id}')
    base = strategy_version(registry, base_version)
    if base['status'] != 'active_paper_baseline':
        raise ValueError('A proposal must name the active paper strategy as its base')
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError('Proposal parameters must be a non-empty JSON object')
    if not isinstance(criteria, dict) or not criteria:
        raise ValueError('Proposal criteria must be a non-empty JSON object')
    try:
        fresh_start = date.fromisoformat(future_data_start)
    except ValueError as exc:
        raise ValueError('--future-data-start must be YYYY-MM-DD') from exc
    viewed_through = date.fromisoformat(registry['available_fresh_data']['through'])
    if fresh_start <= viewed_through:
        raise ValueError(f'Proposal future data must begin after registered/viewed data {viewed_through}')
    proposal = {
        'id': proposal_id,
        'status': 'proposed_not_active',
        'base_strategy_version': base_version,
        'rules': rules,
        'parameters': parameters,
        'reasoning': reasoning,
        'predefined_criteria': criteria,
        'future_data_start': str(fresh_start),
        'evaluated_periods': [],
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'is_active': False,
        'automatic_deployment_allowed': False,
    }
    registry['proposals'].append(proposal)
    return proposal


def parse_json_object(value, option):
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{option} must be valid JSON') from exc
    if not isinstance(parsed, dict):
        raise ValueError(f'{option} must be a JSON object')
    return parsed


def build_parser():
    parser = argparse.ArgumentParser(description='Controlled local paper-research registry.')
    parser.add_argument('--registry', type=Path, default=Path('research_registry.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    performance = sub.add_parser('performance')
    performance.add_argument('--state-root', type=Path, default=Path('paper_portfolios'))
    performance.add_argument('--market', choices=['stock', 'crypto'], required=True)
    performance.add_argument('--symbol', required=True)
    proposal = sub.add_parser('propose')
    proposal.add_argument('--id', required=True)
    proposal.add_argument('--base-version', required=True)
    proposal.add_argument('--rules', required=True)
    proposal.add_argument('--parameters', required=True, help='JSON object')
    proposal.add_argument('--reasoning', required=True)
    proposal.add_argument('--criteria', required=True, help='JSON object')
    proposal.add_argument('--future-data-start', required=True)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        registry = read_registry(args.registry)
        if args.command == 'status':
            print(json.dumps(status_report(registry), indent=2))
            return
        if args.command == 'performance':
            paths = portfolio_paths(args.state_root, args.market, args.symbol)
            with PortfolioLock(portfolio_lock_path(args.state_root, args.market, args.symbol),
                               shared=True, timeout=0.0, create=False):
                state, history = load_portfolio(*paths)
            print(json.dumps(forward_performance(state, history, registry), indent=2))
            return
        parameters = parse_json_object(args.parameters, '--parameters')
        criteria = parse_json_object(args.criteria, '--criteria')
        lock_path = Path(str(args.registry) + '.lock')
        with PortfolioLock(lock_path, timeout=0.0, create=True):
            registry = read_registry(args.registry)
            proposal = record_proposal(
                registry, proposal_id=args.id, base_version=args.base_version,
                rules=args.rules, parameters=parameters, reasoning=args.reasoning,
                criteria=criteria, future_data_start=args.future_data_start)
            atomic_json(args.registry, registry)
        print(json.dumps({'recorded': True, 'proposal': proposal,
                          'active_strategy_changed': False}, indent=2))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()

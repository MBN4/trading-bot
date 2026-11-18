import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from research import (forward_performance, read_registry, record_proposal,
                      status_report, validate_active_paper_strategy)


ROOT = Path(__file__).resolve().parents[1]


def registry_copy():
    return read_registry(ROOT / 'research_registry.json')


# Dates are derived from the live registry rather than hardcoded, because the
# daily operating protocol advances `available_fresh_data.through` over time.
def viewed_through(registry=None):
    registry = registry or registry_copy()
    return date.fromisoformat(registry['available_fresh_data']['through'])


def first_fresh_day(registry=None):
    return viewed_through(registry) + timedelta(days=1)


def fresh_day(offset=0):
    return str(first_fresh_day() + timedelta(days=offset))


def consumed_holdout_end(period_id):
    for period in registry_copy()['data_periods']:
        if period['id'] == period_id:
            assert period['status'] == 'consumed_holdout', period
            return date.fromisoformat(period['end'])
    raise AssertionError(f'{period_id} not found in research_registry.json')


def bar(day, open_price, close):
    return {'date': day, 'open': open_price, 'high': max(open_price, close),
            'low': min(open_price, close), 'close': close, 'volume': 1.0}


def performance_state(kind='real', version='sma-crossover-paper-v1'):
    return {
        'strategy_version': version,
        'dataset_kind': kind,
        'dataset_path': '/tmp/demo.csv',
        'data_source': 'test fixture',
        'dataset_metadata': {'provider': 'test'},
        'initial': 1000.0,
        'fee': 0.01,
        'slippage': 0.0,
        'processed_bars': [
            bar(fresh_day(0), 100, 100),
            bar(fresh_day(1), 100, 110),
        ],
        'fills': [
            {'fill_date': fresh_day(0), 'fee': 10.0},
            {'fill_date': fresh_day(1), 'fee': 11.0},
        ],
    }


def performance_history():
    return [
        {'type': 'CANDLE_PROCESSED', 'date': fresh_day(0), 'equity': 1000.0,
         'observation_classification': 'contemporaneous'},
        {'type': 'CANDLE_PROCESSED', 'date': fresh_day(1), 'equity': 1100.0,
         'observation_classification': 'contemporaneous'},
    ]


class ResearchControlTests(unittest.TestCase):
    def test_consumed_holdouts_are_explicit_and_never_untouched(self):
        report = status_report(registry_copy())
        consumed = {item['id']: item for item in report['consumed_holdouts']}

        self.assertEqual(consumed['btc-holdout-2024']['status'], 'consumed_holdout')
        self.assertEqual(consumed['btc-holdout-2026']['status'], 'consumed_holdout')
        self.assertEqual(report['overall_status'], 'paper_research_only_no_strategy_passed')
        registry = registry_copy()
        fresh = report['available_fresh_data']
        # The count word advances as the forward test progresses; check the
        # shape and that the report passes the registry value through.
        self.assertRegex(fresh['status'], r'^forward_observation_gate_[a-z_]+_of_90$')
        self.assertEqual(fresh['status'], registry['available_fresh_data']['status'])
        self.assertEqual(fresh['through'], str(viewed_through(registry)))

    def test_proposal_is_isolated_from_active_strategy(self):
        registry = registry_copy()
        active_before = copy.deepcopy(status_report(registry)['current_strategy'])
        proposal = record_proposal(
            registry, proposal_id='demo-proposal-v1',
            base_version='sma-crossover-paper-v1',
            rules='Demo rule, never deployed.', parameters={'window': 60},
            reasoning='Exercise proposal isolation.',
            criteria={'minimum_return_pct': 1, 'minimum_rows': 90},
            future_data_start=str(first_fresh_day(registry)))

        self.assertEqual(proposal['status'], 'proposed_not_active')
        self.assertFalse(proposal['is_active'])
        self.assertFalse(proposal['automatic_deployment_allowed'])
        self.assertEqual(status_report(registry)['current_strategy'], active_before)
        self.assertEqual(registry['proposals'][0]['evaluated_periods'], [])

    def test_proposal_rejects_consumed_data_and_failed_base(self):
        registry = registry_copy()
        with self.assertRaisesRegex(ValueError, 'after registered/viewed data'):
            record_proposal(
                registry, proposal_id='bad-date', base_version='sma-crossover-paper-v1',
                rules='x', parameters={'x': 1}, reasoning='x', criteria={'x': 1},
                future_data_start=str(viewed_through(registry)))
        with self.assertRaisesRegex(ValueError, 'active paper strategy'):
            record_proposal(
                registry, proposal_id='bad-base', base_version='price-sma-150-eval-v1',
                rules='x', parameters={'x': 1}, reasoning='x', criteria={'x': 1},
                future_data_start=str(first_fresh_day(registry)))

    def test_performance_calculation_matches_cash_and_buy_hold(self):
        registry = registry_copy()
        registry['decision_policy']['minimum_forward_rows'] = 2
        registry['decision_policy']['minimum_forward_trades'] = 2
        report = forward_performance(performance_state(), performance_history(), registry)

        self.assertEqual(report['claim_status'], 'fresh_forward_evidence_not_profit_claim')
        self.assertEqual(report['strategy_performance']['return_pct'], 10.0)
        self.assertEqual(report['strategy_performance']['max_drawdown_pct'], 0.0)
        self.assertEqual(report['strategy_performance']['fees_paid'], 21.0)
        self.assertEqual(report['strategy_performance']['trade_count'], 2)
        self.assertEqual(report['cash_benchmark']['return_pct'], 0.0)
        self.assertEqual(report['buy_hold_benchmark']['return_pct'], 8.9)
        self.assertEqual(report['buy_hold_benchmark']['fees_paid'], 10.0)
        self.assertEqual(report['strategy_version'], 'sma-crossover-paper-v1')

    def test_synthetic_short_and_reused_periods_block_claims(self):
        registry = registry_copy()
        synthetic = forward_performance(
            performance_state(kind='synthetic'), performance_history(), registry)
        self.assertEqual(synthetic['claim_status'], 'demo_only_synthetic')
        self.assertFalse(synthetic['eligible_for_performance_claim'])
        self.assertIn('synthetic_fixture_demo_only', synthetic['limitations'])
        self.assertTrue(any(item.startswith('contemporaneous_observations_')
                            for item in synthetic['limitations']))

        holdout_end = consumed_holdout_end('btc-holdout-2026')
        day1 = str(holdout_end - timedelta(days=1))
        day2 = str(holdout_end)
        reused_state = performance_state()
        reused_state['processed_bars'] = [
            bar(day1, 100, 100), bar(day2, 100, 110)]
        reused_state['fills'] = [
            {'fill_date': day1, 'fee': 10},
            {'fill_date': day2, 'fee': 10},
        ]
        reused_history = [
            {'type': 'CANDLE_PROCESSED', 'date': day1, 'equity': 1000,
             'observation_classification': 'contemporaneous'},
            {'type': 'CANDLE_PROCESSED', 'date': day2, 'equity': 1100,
             'observation_classification': 'contemporaneous'},
        ]
        registry['decision_policy']['minimum_forward_rows'] = 2
        registry['decision_policy']['minimum_forward_trades'] = 2
        reused = forward_performance(reused_state, reused_history, registry)
        self.assertEqual(reused['claim_status'], 'reused_period_exploratory_only')
        self.assertIn('overlaps_consumed_holdout:btc-holdout-2026', reused['limitations'])

    def test_only_registered_active_parameters_can_initialize(self):
        registry = registry_copy()
        version = validate_active_paper_strategy(
            registry, 'sma-crossover-paper-v1', 5, 20)
        self.assertEqual(version['status'], 'active_paper_baseline')
        with self.assertRaisesRegex(ValueError, 'do not match'):
            validate_active_paper_strategy(
                registry, 'sma-crossover-paper-v1', 10, 30)
        with self.assertRaisesRegex(ValueError, 'not active'):
            validate_active_paper_strategy(
                registry, 'price-sma-150-eval-v1', 5, 20)

    def test_registry_round_trip_preserves_proposal_status(self):
        registry = registry_copy()
        record_proposal(
            registry, proposal_id='round-trip-demo', base_version='sma-crossover-paper-v1',
            rules='demo', parameters={'window': 60}, reasoning='demo',
            criteria={'minimum_rows': 90},
            future_data_start=str(first_fresh_day(registry)))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'registry.json'
            path.write_text(json.dumps(registry), encoding='utf-8')
            loaded = read_registry(path)
        self.assertEqual(loaded['proposals'][0]['status'], 'proposed_not_active')


if __name__ == '__main__':
    unittest.main()

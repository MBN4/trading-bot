import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from append_daily_candle import append_version, file_sha256
from agent import read_bars


class DailyCandleAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.csv = root / 'source.csv'
        self.metadata = root / 'source.metadata.json'
        self.output = root / 'next.csv'
        self.output_metadata = root / 'next.metadata.json'
        rows = ['date,open,high,low,close,volume']
        start = date(2026, 6, 13)
        rows.extend(f'{start + timedelta(days=index)},100,110,90,105,1000'
                    for index in range(80))
        self.csv.write_text('\n'.join(rows) + '\n')
        self.metadata.write_text(json.dumps({
            'provider': 'synthetic test provider', 'usage_permission': 'test only',
            'symbol': 'TEST_USD', 'market': 'crypto', 'quote_currency': 'USD',
            'timezone': 'UTC', 'session_close': 'synthetic completed daily rows',
            'retrieval_date': '2026-08-31', 'adjustment_policy': 'not applicable',
            'source_archives': [],
            'forward_lineage': {
                'appended_start': '2026-08-31', 'appended_end': '2026-08-31',
                'completed_rows': 1
            }
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, **changes):
        values = dict(candle_date='2026-09-01', open_value='105', high='112',
                      low='101', close='108', volume='1200',
                      source_url='https://example.test/archive.zip',
                      source_sha256='a' * 64, retrieval_date='2026-09-02',
                      confirmed_complete=True)
        values.update(changes)
        return append_version(self.csv, self.metadata, self.output,
                              self.output_metadata, **values)

    def test_creates_new_valid_version_and_provenance(self):
        before_csv = self.csv.read_bytes()
        before_metadata = self.metadata.read_bytes()
        report = self.append()
        self.assertEqual(report['appended_date'], '2026-09-01')
        self.assertEqual(len(read_bars(self.output)), 81)
        provenance = json.loads(self.output_metadata.read_text())
        self.assertEqual(provenance['manual_append']['date'], '2026-09-01')
        self.assertTrue(provenance['manual_append']['confirmed_complete'])
        self.assertEqual(provenance['forward_lineage']['appended_end'], '2026-09-01')
        self.assertEqual(provenance['forward_lineage']['completed_rows'], 2)
        self.assertEqual(provenance['normalized_csv_sha256'], file_sha256(self.output))
        self.assertEqual(self.csv.read_bytes(), before_csv)
        self.assertEqual(self.metadata.read_bytes(), before_metadata)

    def test_rejects_incomplete_duplicate_gap_bad_ohlc_and_overwrite(self):
        cases = [
            {'confirmed_complete': False},
            {'candle_date': '2026-08-31'},
            {'candle_date': '2026-09-02'},
            {'high': '100'},
            {'source_sha256': 'bad'},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.append(**changes)
            self.assertFalse(self.output.exists())
            self.assertFalse(self.output_metadata.exists())
        self.output.write_text('do not replace')
        with self.assertRaisesRegex(ValueError, 'already exist'):
            self.append()


if __name__ == '__main__':
    unittest.main()

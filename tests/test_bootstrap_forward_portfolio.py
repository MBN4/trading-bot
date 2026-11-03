import io
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone

import bootstrap_forward_portfolio as bootstrap


class BootstrapNormalizationTests(unittest.TestCase):
    def make_archive(self, rows):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as bundle:
            bundle.writestr('BTCUSDT-1d.csv', '\n'.join(rows))
        return buffer.getvalue()

    def test_millisecond_and_microsecond_open_times_normalize_to_the_same_date(self):
        day = date(2025, 1, 15)
        open_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        millisecond_row = f'{int(open_dt.timestamp() * 1000)},1,2,0.5,1.5,10,0,0,0,0,0,0'
        microsecond_row = f'{int(open_dt.timestamp() * 1_000_000)},1,2,0.5,1.5,10,0,0,0,0,0,0'
        for raw in (millisecond_row, microsecond_row):
            rows = bootstrap.kline_rows(self.make_archive([raw]))
            self.assertEqual(len(rows), 1)
            self.assertEqual(bootstrap.normalized_row(rows[0]), '2025-01-15,1,2,0.5,1.5,10')

    def test_archive_with_more_than_one_member_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as bundle:
            bundle.writestr('a.csv', 'x')
            bundle.writestr('b.csv', 'y')
        with self.assertRaises(bootstrap.OperatorError):
            bootstrap.kline_rows(buffer.getvalue())


if __name__ == '__main__':
    unittest.main()

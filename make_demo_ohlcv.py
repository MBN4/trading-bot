"""Convert bundled close-only samples into explicitly synthetic OHLCV demo data."""
import argparse
import csv
from pathlib import Path


def convert(source, destination, rows=None):
    with Path(source).open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ['date', 'close']:
            raise ValueError('Demo source header must be exactly date,close')
        source_rows = list(reader)
    selected = source_rows if rows is None else source_rows[:rows]
    if not selected:
        raise ValueError('No rows selected')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(('date', 'open', 'high', 'low', 'close', 'volume'))
        for row in selected:
            close = float(row['close'])
            writer.writerow((row['date'], close, close * 1.001, close * 0.999,
                             close, 1000.0))
    return len(selected)


def main():
    parser = argparse.ArgumentParser(description='Create synthetic OHLCV for local paper-mode demos.')
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--rows', type=int)
    args = parser.parse_args()
    if args.rows is not None and args.rows <= 0:
        parser.error('--rows must be positive')
    try:
        count = convert(args.source, args.destination, args.rows)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f'Wrote {count} explicitly synthetic OHLCV rows to {args.destination}')


if __name__ == '__main__':
    main()

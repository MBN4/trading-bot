"""Create a new local OHLCV dataset version with one confirmed daily candle."""
import argparse
import csv
import hashlib
import json
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from agent import read_bars
from paper import read_metadata


def file_sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def validated_number(value, name, *, positive=True):
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be numeric') from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise ValueError(f'{name} must be {"positive" if positive else "non-negative"}')
    return value


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False,
                                     encoding='utf-8', newline='') as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def append_version(source_csv, source_metadata, output_csv, output_metadata, *,
                   candle_date, open_value, high, low, close, volume,
                   source_url, source_sha256, retrieval_date, confirmed_complete):
    if not confirmed_complete:
        raise ValueError('--confirmed-complete is required after the UTC daily session has closed')
    source_csv, source_metadata = Path(source_csv), Path(source_metadata)
    output_csv, output_metadata = Path(output_csv), Path(output_metadata)
    if output_csv.exists() or output_metadata.exists():
        raise ValueError('Output files already exist; choose new versioned paths')
    if output_csv.resolve() == source_csv.resolve() or output_metadata.resolve() == source_metadata.resolve():
        raise ValueError('Outputs must be new files; the source dataset and metadata are immutable')

    bars = read_bars(source_csv)
    metadata = read_metadata(source_metadata)
    try:
        day = date.fromisoformat(candle_date)
        retrieved = date.fromisoformat(retrieval_date)
    except ValueError as exc:
        raise ValueError('Candle and retrieval dates must be YYYY-MM-DD') from exc
    expected = bars[-1].day + timedelta(days=1)
    if day != expected:
        raise ValueError(f'Next candle must be {expected}; got {day}. Add missed days in order')
    values = [validated_number(open_value, 'open'), validated_number(high, 'high'),
              validated_number(low, 'low'), validated_number(close, 'close'),
              validated_number(volume, 'volume', positive=False)]
    parsed = [Decimal(item) for item in values]
    if parsed[1] < max(parsed[0], parsed[3]) or parsed[2] > min(parsed[0], parsed[3]) or parsed[1] < parsed[2]:
        raise ValueError('OHLC values are inconsistent')
    checksum = source_sha256.lower()
    if len(checksum) != 64 or any(character not in '0123456789abcdef' for character in checksum):
        raise ValueError('--source-sha256 must be a 64-character hexadecimal SHA-256')
    parsed_url = urlparse(source_url)
    if parsed_url.scheme != 'https' or not parsed_url.netloc:
        raise ValueError('--source-url must be an HTTPS provenance URL')

    with source_csv.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.reader(handle))
    rows.append([str(day), *values])
    with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8', newline='') as candidate:
        writer = csv.writer(candidate, lineterminator='\n')
        writer.writerows(rows)
        candidate_path = Path(candidate.name)
    try:
        candidate_bars = read_bars(candidate_path)
        if candidate_bars[-1].day != day:
            raise ValueError('Candidate CSV did not preserve the requested candle date')
        csv_content = candidate_path.read_text(encoding='utf-8')
        candidate_hash = file_sha256(candidate_path)
    finally:
        candidate_path.unlink(missing_ok=True)

    updated = dict(metadata)
    archives = list(updated.get('source_archives', []))
    archive = {'url': source_url, 'sha256': checksum}
    if archive in archives:
        raise ValueError('This exact source archive and checksum are already recorded')
    archives.append(archive)
    updated['source_archives'] = archives
    updated['retrieval_date'] = str(retrieved)
    updated['normalized_csv_sha256'] = candidate_hash
    updated['derived_from'] = {
        'csv': str(source_csv),
        'csv_sha256': file_sha256(source_csv),
        'metadata': str(source_metadata),
        'metadata_sha256': file_sha256(source_metadata),
    }
    updated['manual_append'] = {
        'date': str(day),
        'confirmed_complete': True,
        'source_url': source_url,
        'source_sha256': checksum,
    }
    if isinstance(updated.get('forward_lineage'), dict):
        lineage = dict(updated['forward_lineage'])
        lineage['appended_end'] = str(day)
        lineage['completed_rows'] = int(lineage.get('completed_rows', 0)) + 1
        updated['forward_lineage'] = lineage

    atomic_text(output_csv, csv_content)
    atomic_text(output_metadata, json.dumps(updated, indent=2, sort_keys=True) + '\n')
    return {'created_csv': str(output_csv), 'created_metadata': str(output_metadata),
            'appended_date': str(day), 'rows': len(candidate_bars),
            'normalized_csv_sha256': candidate_hash, 'source_url': source_url,
            'source_sha256': checksum, 'confirmed_complete': True}


def build_parser():
    parser = argparse.ArgumentParser(
        description='Manually create a new local dataset version with one completed candle; never downloads or trades.')
    parser.add_argument('--source-csv', type=Path, required=True)
    parser.add_argument('--source-metadata', type=Path, required=True)
    parser.add_argument('--output-csv', type=Path, required=True)
    parser.add_argument('--output-metadata', type=Path, required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--open', required=True)
    parser.add_argument('--high', required=True)
    parser.add_argument('--low', required=True)
    parser.add_argument('--close', required=True)
    parser.add_argument('--volume', required=True)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--retrieval-date', required=True)
    parser.add_argument('--confirmed-complete', action='store_true')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = append_version(
            args.source_csv, args.source_metadata, args.output_csv, args.output_metadata,
            candle_date=args.date, open_value=args.open, high=args.high, low=args.low,
            close=args.close, volume=args.volume, source_url=args.source_url,
            source_sha256=args.source_sha256, retrieval_date=args.retrieval_date,
            confirmed_complete=args.confirmed_complete)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

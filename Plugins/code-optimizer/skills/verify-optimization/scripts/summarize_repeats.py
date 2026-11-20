#!/usr/bin/env python3
"""Describe independent-run observations; no benchmark execution or verdicts."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import statistics


def describe(values):
    if not values or any(isinstance(v, bool) or not math.isfinite(v) for v in values):
        raise ValueError('values must be nonempty finite numbers')
    mean = statistics.mean(values)
    median = statistics.median(values)
    sd = statistics.stdev(values) if len(values) > 1 else None
    result = dict(n=len(values), mean=mean, sample_sd=sd, median=median,
                  min=min(values), max=max(values),
                  mad=statistics.median(abs(v - median) for v in values) if len(values) > 1 else None,
                  cv=sd / abs(mean) if sd is not None and mean != 0 else None,
                  variability_available=len(values) > 1)
    if any(isinstance(v, float) and not math.isfinite(v) for v in result.values()):
        raise ValueError('statistics overflow; rescale input units')
    return result


def summarize(paths, dimensions=()):
    reserved = {'group', 'run_id', 'metric', 'unit', 'value', 'statistics', 'observations'}
    if len(set(dimensions)) != len(dimensions) or reserved.intersection(dimensions):
        raise ValueError('dimensions must be unique and distinct from required/output columns')
    groups, sources, seen_paths = {}, [], set()
    for name in paths:
        path = Path(name).resolve()
        if path in seen_paths:
            raise ValueError('duplicate input file: ' + str(path))
        seen_paths.add(path)
        raw = path.read_bytes()
        content = raw.decode('utf-8-sig')
        if path.suffix.lower() == '.json':
            rows = json.loads(content)
            if not isinstance(rows, list):
                raise ValueError('JSON input must be a record array')
        elif path.suffix.lower() in ('.csv', '.tsv'):
            reader = csv.DictReader(io.StringIO(content), delimiter='\t' if path.suffix.lower() == '.tsv' else ',', strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError('missing or duplicate column names')
            rows = list(reader)
            if any(None in row or any(v is None for v in row.values()) for row in rows):
                raise ValueError('CSV/TSV row width does not match header')
        else:
            raise ValueError('reshape input to CSV, TSV or JSON first')
        if not rows:
            raise ValueError('empty input: ' + str(path))
        sources.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest()))
        for index, row in enumerate(rows, 1):
            required = ('group', 'run_id', 'metric', 'unit', *dimensions)
            if not isinstance(row, dict) or any(k not in row or row[k] is None or str(row[k]).strip() == '' for k in (*required, 'value')):
                raise ValueError(f'{path}: record {index}: missing required value')
            if any(isinstance(row[k], (list, dict, bool)) for k in required):
                raise ValueError('identifiers and dimensions must be scalar strings or numbers')
            if isinstance(row['value'], bool):
                raise ValueError('boolean metric value')
            value = float(row['value'])
            if not math.isfinite(value):
                raise ValueError('metric value must be finite')
            keys = ('group', 'metric', 'unit', *dimensions)
            key = tuple(str(row[k]) for k in keys)
            bucket = groups.setdefault(key, dict(labels=dict(zip(keys, key)), observations=[], ids=set()))
            run_id = str(row['run_id'])
            if run_id in bucket['ids']:
                raise ValueError('duplicate independent run_id within group: ' + run_id)
            bucket['ids'].add(run_id)
            bucket['observations'].append(dict(run_id=run_id, value=value, source_file=str(path), source_record=index))
    if not groups:
        raise ValueError('no observations')
    return dict(schema_version=1, statistic_scope='descriptive_only', sources=sources,
                groups=[dict(**b['labels'], statistics=describe([r['value'] for r in b['observations']]),
                             observations=b['observations']) for b in groups.values()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+')
    parser.add_argument('--dimensions', default='')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        result = summarize(args.inputs, tuple(s.strip() for s in args.dimensions.split(',') if s.strip()))
        # Exclusive creation protects raw inputs and earlier summaries.
        with open(args.output, 'x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    except (OSError, ValueError, TypeError, OverflowError, csv.Error) as exc:
        parser.exit(2, f'error: {exc}\n')


if __name__ == '__main__':
    main()

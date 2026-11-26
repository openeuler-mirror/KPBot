#!/usr/bin/env python3
"""Configuration-driven, offline performance charts. No metric names are reserved."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import statistics as stats
import sys
import uuid
import textwrap


def number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a measurement')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('measurement must be finite')
    return result


def quantile(values, q):
    if not 0 <= q <= 1:
        raise ValueError('quantile must be between 0 and 1')
    a = sorted(values)
    position = (len(a) - 1) * q
    lo = int(position)
    hi = min(lo + 1, len(a) - 1)
    return a[lo] + (a[hi] - a[lo]) * (position - lo)


def aggregate(values, method):
    methods = {'mean': stats.mean, 'median': stats.median, 'min': min,
               'max': max, 'count': len, 'mad': lambda a: stats.median(
                   abs(x - stats.median(a)) for x in a)}
    if method == 'std':
        if len(values) < 2:
            raise ValueError('sample standard deviation needs at least two observations')
        return stats.stdev(values)
    if isinstance(method, str) and re.fullmatch(r'p\d+(\.\d+)?', method):
        return quantile(values, float(method[1:]) / 100)
    if method not in methods:
        raise ValueError(f'unsupported aggregation: {method}')
    return methods[method](values)


def columns(value):
    return [value] if isinstance(value, str) else list(value or [])


def load_records(path, spec):
    fmt = spec.get('format', path.suffix.lstrip('.'))
    if fmt in ('csv', 'tsv'):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream, delimiter=spec.get('delimiter', '\t' if fmt == 'tsv' else ',')))
    payload = json.loads(path.read_text(encoding='utf-8-sig'))
    if fmt == 'vector':
        records = []
        metric = payload['performance_field']
        for dataset, sides in payload['datasets'].items():
            for side in ('baseline', 'candidate'):
                for index, row in enumerate(sides[side]):
                    records.append(dict(row, dataset=dataset, side=side,
                                        source_index=index, metric_name=metric))
        return records
    for key in spec.get('records_path', []):
        payload = payload[key]
    if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
        raise ValueError('JSON must contain records; declare records_path for nested arrays')
    return payload


def select(records, chart):
    selected = []
    filters = chart.get('filter', {})
    for index, record in enumerate(records):
        if all(record.get(key) in (value if isinstance(value, list) else [value])
               for key, value in filters.items()):
            selected.append((index, record))
    if not selected:
        raise ValueError(f"chart {chart['id']}: no records match filter")
    return selected


def prepare(records, chart, metrics):
    metric = metrics[chart['metric']]
    field = metric['column']
    dimensions = columns(chart.get('series')) + columns(chart.get('facet'))
    xfield = chart.get('x')
    points = []
    for index, record in select(records, chart):
        try:
            if any(key not in record for key in dimensions):
                raise ValueError('missing grouping dimension')
            x = record[xfield] if xfield else 'all'
            if chart.get('x_type') == 'numeric':
                x = number(x)
            value = number(record[field]) * number(metric.get('scale', 1))
            if not math.isfinite(value):
                raise ValueError('scaled value is not finite')
            points.append({'source_row': index, 'source_file': record.get('_source_file'),
                           'source_record': record.get('_source_record', index), 'x': x, 'value': value,
                           'series': tuple(record[k] for k in columns(chart.get('series'))),
                           'facet': tuple(record[k] for k in columns(chart.get('facet'))),
                           'unit_id': record.get(chart.get('unit_id', ''))})
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"chart {chart['id']}, record {index}: {error}") from error
    if chart.get('aggregate') and metric.get('sample_level') == 'summary' and not chart.get('aggregate_summary'):
        raise ValueError('aggregating existing summaries requires aggregate_summary=true')
    # Repeated timestamps within a run are not independent experiments.
    if chart.get('unit_id'):
        seen = set()
        for p in points:
            if p['unit_id'] is None:
                raise ValueError('unit_id missing')
            key = (p['facet'], p['series'], p['x'], p['unit_id'])
            if key in seen:
                raise ValueError('multiple observations per experimental unit and x; aggregate upstream first')
            seen.add(key)
    return points


def summarize(points, chart):
    groups = {}
    for p in points:
        groups.setdefault((p['facet'], p['series'], p['x']), []).append(p)
    output = []
    for (facet, series, x), group in groups.items():
        values = [p['value'] for p in group]
        method = chart.get('aggregate')
        if not method and len(values) > 1 and chart['type'] in ('line', 'bar', 'heatmap', 'point'):
            raise ValueError('repeated x requires explicit aggregate or a series dimension separating runs')
        center = aggregate(values, method) if method else values[0] if len(values) == 1 else None
        spread = chart.get('spread')
        low = high = None
        if spread:
            if not method:
                raise ValueError('spread requires explicit aggregation')
            if spread == 'minmax':
                low, high = min(values), max(values)
            elif spread == 'iqr':
                low, high = quantile(values, .25), quantile(values, .75)
            elif spread == 'sd':
                if method != 'mean':
                    raise ValueError('sd range requires mean aggregation')
                if len(values) >= 2:
                    low, high = center - stats.stdev(values), center + stats.stdev(values)
            elif spread == 'mad':
                if method != 'median':
                    raise ValueError('mad range requires median aggregation')
                low, high = center - aggregate(values, 'mad'), center + aggregate(values, 'mad')
            else:
                raise ValueError(f'unsupported spread: {spread}')
        output.append(dict(facet=facet, series=series, x=x, center=center,
                           low=low, high=high, n=len(values),
                           source_rows=[p['source_row'] for p in group]))
    return output


def render(points, summary, chart, metric, directory, formats):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    import numpy as np

    fonts = {f.name for f in font_manager.fontManager.ttflist}
    for name in ('Microsoft YaHei', 'Noto Sans CJK SC', 'SimHei'):
        if name in fonts:
            plt.rcParams['font.sans-serif'] = [name, 'DejaVu Sans']
            break
    plt.rcParams['axes.unicode_minus'] = False
    facets = list(dict.fromkeys(p['facet'] for p in points))
    series = list(dict.fromkeys(p['series'] for p in points))
    ncols = min(int(chart.get('columns', 2)), len(facets))
    if ncols < 1:
        raise ValueError('columns must be positive')
    nrows = math.ceil(len(facets) / ncols)
    fig, axes = plt.subplots(nrows, ncols, squeeze=False,
                             figsize=chart.get('figsize', [7 * ncols, 4.5 * nrows]),
                             constrained_layout=True)
    palette = chart.get('colors', plt.rcParams['axes.prop_cycle'].by_key()['color'])
    label_map = chart.get('label_map', {})
    def display(x):
        return label_map.get(str(x), str(x))
    kind = chart['type']
    for facet, ax in zip(facets, axes.flat):
        local = [p for p in points if p['facet'] == facet]
        xs = list(dict.fromkeys(p['x'] for p in local))
        numeric = chart.get('x_type') == 'numeric'
        if numeric:
            xs.sort()
        if 'order' in chart:
            if set(chart['order']) != set(xs):
                raise ValueError('order must include every x category exactly once')
            xs = chart['order']
        position = {x: x if numeric else i for i, x in enumerate(xs)}
        bins = chart.get('bins', 20)
        if kind == 'hist':
            bins = np.histogram_bin_edges([p['value'] for p in local], bins=bins)
        for si, group in enumerate(series):
            raw = [p for p in local if p['series'] == group]
            if not raw:
                continue
            items = [r for r in summary if r['facet'] == facet and r['series'] == group]
            by_x = {r['x']: r for r in items}
            color = palette[si % len(palette)]
            label = ' / '.join(map(str, group)) or metric.get('label', chart['metric'])
            vals = [p['value'] for p in raw]
            xp = [position[p['x']] for p in raw]
            if kind == 'hist':
                ax.hist(vals, bins=bins, alpha=.45, label=label, color=color,
                        density=chart.get('density', False))
            elif kind == 'ecdf':
                values = sorted(vals)
                ax.step(values, np.arange(1, len(values) + 1) / len(values),
                        where='post', label=label, color=color)
            elif kind == 'box':
                width = .7 / len(series)
                offset = (si - (len(series) - 1) / 2) * width
                arrays = [[p['value'] for p in raw if p['x'] == x] for x in xs]
                valid = [(x, a) for x, a in zip(xs, arrays) if a]
                box = ax.boxplot([a for _, a in valid], positions=[position[x] + offset for x, _ in valid],
                                 widths=width * .8, patch_artist=True, manage_ticks=False)
                for patch in box['boxes']:
                    patch.set_facecolor(color)
                ax.plot([], [], color=color, label=label)
            elif kind == 'scatter':
                ax.scatter(xp, vals, label=label, color=color, alpha=chart.get('alpha', .65),
                           s=chart.get('point_size', 24))
                if chart.get('aggregate'):
                    ax.scatter([position[r['x']] for r in items], [r['center'] for r in items],
                               marker='_', s=200, color='black')
            elif kind == 'heatmap':
                # A series tuple defines a row; arbitrary dimensions can compose that tuple.
                matrix = [[next((r['center'] for r in summary if r['facet'] == facet and
                                  r['series'] == g and r['x'] == x), float('nan'))
                           for x in xs] for g in series]
                mesh = ax.imshow(np.ma.masked_invalid(matrix), aspect='auto',
                                 cmap=chart.get('cmap', 'viridis'),
                                 vmin=chart.get('vmin'), vmax=chart.get('vmax'))
                ax.set_xticks(range(len(xs)), [display(x) for x in xs])
                ax.set_yticks(range(len(series)), [' / '.join(map(str, g)) for g in series])
                fig.colorbar(mesh, ax=ax, label=metric.get('unit', ''))
                break
            else:
                xx = [position[x] for x in xs]
                yy = [by_x[x]['center'] if x in by_x else float('nan') for x in xs]
                if kind == 'line':
                    ax.plot(xx, yy, marker=chart.get('marker', 'o'), label=label, color=color,
                            linewidth=chart.get('linewidth', 1.5))
                elif kind == 'point':
                    ax.scatter(xx, yy, label=label, color=color)
                elif kind == 'bar':
                    width = .8 / len(series)
                    xx = [i + (si - (len(series) - 1) / 2) * width for i in range(len(xs))]
                    ax.bar(xx, yy, width=width, label=label, color=color)
                else:
                    raise ValueError(f'unsupported chart type: {kind}')
                if chart.get('spread'):
                    lows = [by_x[x]['low'] if x in by_x and by_x[x]['low'] is not None else float('nan') for x in xs]
                    highs = [by_x[x]['high'] if x in by_x and by_x[x]['high'] is not None else float('nan') for x in xs]
                    if kind == 'line':
                        ax.fill_between(xx, lows, highs, alpha=.18, color=color)
                    else:
                        ax.vlines(xx, lows, highs, color='black')
            if chart.get('show_samples') and kind in ('line', 'point', 'box', 'bar'):
                if kind in ('box', 'bar'):
                    offset = (si - (len(series) - 1) / 2) * (.7 if kind == 'box' else .8) / len(series)
                    xp = [(position[p['x']] if kind == 'box' else xs.index(p['x'])) + offset for p in raw]
                ax.scatter(xp, vals, color=color, alpha=.35, s=12)
        if kind not in ('hist', 'ecdf', 'heatmap') and (not numeric or kind == 'bar'):
            ax.set_xticks(range(len(xs)), [display(x) for x in xs], rotation=chart.get('rotation', 0))
        ax.set_title(' / '.join(map(str, facet)) if facet else chart.get('title', chart['id']))
        unit = metric.get('unit', '')
        value_label = metric.get('label', chart['metric']) + (f' ({unit})' if unit else '')
        ax.set_xlabel(chart.get('xlabel', value_label if kind in ('hist', 'ecdf') else chart.get('x', 'group')))
        ax.set_ylabel(chart.get('ylabel', ('ECDF' if kind == 'ecdf' else 'Density' if chart.get('density') else 'Count')
                                   if kind in ('hist', 'ecdf') else ' / '.join(columns(chart.get('series'))) if kind == 'heatmap' else value_label))
        for axis in ('x', 'y'):
            scale = chart.get(axis + 'scale', 'linear')
            if scale == 'log':
                values = ([p['value'] for p in local] if axis == 'x' else [1]) if kind in ('hist', 'ecdf') else ([p['value'] for p in local] if axis == 'y' else list(position.values()))
                if any(v <= 0 for v in values):
                    raise ValueError('log axis requires positive values; use linear or symlog')
            if scale != 'linear':
                getattr(ax, 'set_' + axis + 'scale')(scale)
            if axis + 'lim' in chart:
                limits = chart[axis + 'lim']
                if len(limits) != 2 or (all(v is not None for v in limits) and limits[0] >= limits[1]):
                    raise ValueError('axis limits must be [lower, upper], lower < upper')
                getattr(ax, 'set_' + axis + 'lim')(*limits)
        if chart.get('grid', kind != 'heatmap'):
            ax.grid(True, alpha=.2)
        else:
            ax.grid(False)
        if kind != 'heatmap':
            ax.legend(fontsize=chart.get('legend_fontsize', 9), loc=chart.get('legend_loc', 'best'))
    for ax in list(axes.flat)[len(facets):]:
        ax.set_visible(False)
    if len(facets) > 1:
        fig.suptitle(chart.get('title', chart['id']))
    if label_map:
        fig.get_layout_engine().set(rect=(0, 0, .74, 1))
        legend = '\n\n'.join(textwrap.fill(f'{code}: {full}', 38)
                             for full, code in list(label_map.items())[:12])
        if len(label_map) > 12:
            legend += '\n\nFull mapping: summary JSON / index Markdown'
        fig.text(.76, .95, legend, va='top', fontsize=8)
        width, height = fig.get_size_inches()
        fig.set_size_inches(width + 3, max(height, min(20, len(legend.splitlines()) * .16)))
    paths = []
    try:
        for fmt in formats:
            if fmt not in ('svg', 'png', 'pdf'):
                raise ValueError('formats must be svg, png or pdf')
            target = directory / f"{chart['id']}.{fmt}"
            fig.savefig(target, dpi=chart.get('dpi', 160))
            paths.append(target.name)
    finally:
        plt.close(fig)
    return paths


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def run(spec_path, input_path=None, output_dir=None):
    spec_path = Path(spec_path).resolve()
    spec = json.loads(spec_path.read_text(encoding='utf-8-sig'))
    inputs = [{'path': str(Path(input_path).resolve()), 'format': spec.get('input', {}).get('format', Path(input_path).suffix.lstrip('.'))}] if input_path else spec.get('inputs', [spec.get('input')])
    records, sources = [], []
    for item in inputs:
        source = (spec_path.parent / item['path']).resolve()
        loaded = load_records(source, item)
        for row_index, record in enumerate(loaded):
            record = dict(record)
            for key, value in item.get('dimensions', {}).items():
                if key in record and record[key] != value:
                    raise ValueError(f'file dimension conflicts with record: {key}')
                record[key] = value
            record['_source_file'] = str(source)
            record['_source_record'] = row_index
            records.append(record)
        sources.append(dict(item, path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    output = Path(output_dir) if output_dir else spec_path.parent / spec['output']['directory']
    name = spec.get('output', {}).get('run_name', '')
    if name and not re.fullmatch('[a-zA-Z0-9_-]+', name):
        raise ValueError('run_name must be a safe filename')
    if not name and output.exists():
        name = 'run-' + uuid.uuid4().hex[:10]
    prefix = name + '-' if name else ''
    if (output / (prefix + 'summary.json')).exists():
        raise ValueError('run_name already exists; choose another name')
    metrics = {m['name']: m for m in spec['mapping']['metrics']}
    if len(metrics) != len(spec['mapping']['metrics']):
        raise ValueError('metric names must be unique')
    for metric in metrics.values():
        if not all(k in metric for k in ('column', 'unit', 'sample_level')):
            raise ValueError('metrics require column, unit and sample_level')
    allowed = set('id type metric x x_type series facet filter aggregate aggregate_summary unit_id spread show_samples order xlim ylim xscale yscale xlabel ylabel title figsize dpi columns colors marker linewidth point_size alpha rotation grid legend_loc legend_fontsize bins density cmap vmin vmax label_max_length label_map'.split())
    prepared = []
    ids = set()
    for chart in spec['charts']:
        unknown = set(chart) - allowed
        if unknown:
            raise ValueError(f'unknown chart options: {sorted(unknown)}')
        if chart['type'] not in ('scatter', 'line', 'point', 'bar', 'box', 'hist', 'ecdf', 'heatmap'):
            raise ValueError('unsupported chart type')
        if chart['type'] in ('box', 'hist', 'ecdf') and (chart.get('aggregate') or chart.get('spread')):
            raise ValueError('distribution charts use observations directly; remove aggregate/spread')
        name = chart['id']
        if not re.fullmatch('[a-zA-Z0-9_-]+', name) or name in ids:
            raise ValueError('chart IDs must be unique safe filenames')
        ids.add(name)
        points = prepare(records, chart, metrics)
        labels = list(dict.fromkeys(str(p['x']) for p in points))
        if chart.get('x_type') != 'numeric' and any(len(x) > chart.get('label_max_length', 24) for x in labels) and not chart.get('label_map'):
            def code(i):
                result = ''
                while i >= 0:
                    result = chr(65 + i % 26) + result
                    i = i // 26 - 1
                return result
            chart['label_map'] = {full: code(i) for i, full in enumerate(labels)}
        summary = summarize(points, chart)
        repeated = any(r['n'] > 1 for r in summary)
        if repeated and chart['type'] in ('line', 'point', 'bar') and not chart.get('spread'):
            chart['spread'] = 'minmax'
            summary = summarize(points, chart)
        prepared.append((chart, points, summary))
        if repeated and chart['type'] == 'heatmap':
            companion = dict(chart, id=chart['id']+'-samples', type='scatter', spread=None,
                             title=chart.get('title', chart['id'])+' — repeated observations')
            prepared.append((companion, points, summary))
    if not prepared:
        raise ValueError('at least one chart is required')
    formats = spec.get('output', {}).get('formats', ['svg', 'png'])
    if not formats or any(fmt not in ('svg', 'png', 'pdf') for fmt in formats):
        raise ValueError('formats must be a nonempty list of svg/png/pdf')
    filenames = [c['id'] for c, _, _ in prepared]
    if len(set(filenames)) != len(filenames):
        raise ValueError('generated chart ID conflicts with another chart')
    for filename in filenames:
        if any((output / 'figures' / f'{prefix}{filename}.{fmt}').exists() for fmt in formats) or (output / 'data' / f'{prefix}{filename}.csv').exists():
            raise ValueError('output filename exists; choose another run_name')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'figures').mkdir(exist_ok=True)
    (output / 'data').mkdir(exist_ok=True)
    write_json(output / (prefix+'summary.json'), {'status': 'incomplete'})
    entries = []
    markdown = ['# Performance charts', '', 'Charts describe supplied measurements; they do not certify optimization acceptance.', '']
    for chart, points, summary in prepared:
        paths = render(points, summary, dict(chart, id=prefix+chart['id']), metrics[chart['metric']], output / 'figures',
                       formats)
        with (output / 'data' / (prefix+chart['id'] + '.csv')).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(points[0]))
            writer.writeheader()
            writer.writerows(points)
        entries.append({'id': chart['id'], 'statistics': summary, 'figures': paths, 'label_map': chart.get('label_map', {}),
                        'selected_records': len(points), 'input_records': len(records),
                        'sample_level': metrics[chart['metric']]['sample_level']})
        markdown += [f"## {chart['id']}", '', f"Metric: {chart['metric']}; sample level: {metrics[chart['metric']]['sample_level']}; aggregation: {chart.get('aggregate')}; range: {chart.get('spread')}. Selected {len(points)} / {len(records)} records.", '']
        markdown += [f'![{chart["id"]}](figures/{paths[0]})', '']
        markdown += [f'- {code}: {full}' for full, code in chart.get('label_map', {}).items()]
    write_json(output / (prefix+'summary.json'), {'status': 'complete', 'charts': entries})
    spec.pop('input', None)
    spec['inputs'] = [{k:v for k,v in item.items() if k != 'sha256'} for item in sources]
    write_json(output / (prefix+'chart-spec.resolved.json'), spec)
    import matplotlib
    write_json(output / (prefix+'provenance.json'), {'inputs': sources,
               'source_row_definition': 'zero-based record index after adapter; CSV excludes header',
               'python': sys.version, 'matplotlib': matplotlib.__version__,
               'quantile_method': 'linear interpolation', 'std_ddof': 1,
               'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    markdown += ['', 'Visual review: if the agent cannot read images, user review is required. Rendering success is not visual approval.']
    (output / (prefix+'index.md')).write_text('\n'.join(markdown), encoding='utf-8')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--input')
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    try:
        print(run(args.spec, args.input, args.output_dir))
    except (ValueError, KeyError, TypeError, OSError, ImportError) as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(2)

"""Report writers: CSV, Markdown, JSON and one offline HTML page."""
import csv
import html
import json

from . import diff, fields


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def csv_cell(value):
    """Keep spreadsheets from treating a value as a formula."""
    if value is None:
        return ''
    text = str(value).replace('\x00', '')
    if text[:1] in ('=', '+', '@', '\t', '\r') or (
            text[:1] == '-' and len(text) > 1 and not text[1].isdigit()):
        return "'" + text
    return text


def write_csv(path, header, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow([csv_cell(v) for v in row])


def overview_rows(entries):
    header = ['标签', '角色', '宿主机', '容器', '采集时间(UTC)', '采集状态', '画像来源',
              'Python版本', 'vllm', 'vllm-ascend', 'torch', 'torch-npu', 'CANN版本']
    rows = []
    for e in entries:
        def get(category, name):
            return e['items'].get((category, name), fields.MISSING)
        rows.append([
            e['id'], e['role'], e['node'], e['container'], e['collected_at'],
            ('采集失败: ' + e['error']) if e.get('error') else
            ('成功' if e['profile_ok'] else '部分失败'),
            e['profile_source'],
            get(fields.CATEGORY_BASE, 'Python版本'),
            get(fields.CATEGORY_CORE, 'vllm'), get(fields.CATEGORY_CORE, 'vllm-ascend'),
            get(fields.CATEGORY_CORE, 'torch'), get(fields.CATEGORY_CORE, 'torch-npu'),
            get(fields.CATEGORY_BASE, 'CANN版本'),
        ])
    return header, rows


def write_matrix_csv(path, entries, rows):
    header = ['类别', '项目'] + [e['id'] for e in entries] + ['比较结果', '性能相关']
    write_csv(path, header, [
        [r['category'], r['name']] + list(r['values']) + [r['status'], '是' if r['perf'] else '']
        for r in rows])


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def _md_cell(value, limit=90):
    text = str(value).replace('|', '\\|').replace('\n', ' ')
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _md_table(header, rows):
    out = ['| ' + ' | '.join(_md_cell(h) for h in header) + ' |',
           '|' + '|'.join(['---'] * len(header)) + '|']
    for row in rows:
        out.append('| ' + ' | '.join(_md_cell(v) for v in row) + ' |')
    return out


def markdown_report(entries, matrix, comparisons, problems):
    lines = ['# P/D 环境对比摘要', '']
    lines.append('对比环境：' + '、'.join(e['id'] for e in entries))
    lines.append('')
    if problems:
        lines += ['## 采集问题', '']
        for p in problems:
            lines.append('- ' + p)
        lines.append('')
    header, rows = overview_rows(entries)
    lines += ['## 环境简表', ''] + _md_table(header, rows) + ['']
    for a, b, rows_ab, summary in comparisons:
        lines.append('## %s ↔ %s' % (a['id'], b['id']))
        lines.append('')
        lines.append('共 %d 项，差异 %d 项，其中性能相关 %d 项%s。'
                     % (summary['total_items'], summary['diff_count'],
                        summary['perf_diff_count'],
                        '，无法比较 %d 项' % summary['not_comparable_count']
                        if summary['not_comparable_count'] else ''))
        if not summary['comparable']:
            lines.append('')
            lines.append('> 该组合无法比较：' + summary['note'])
        elif summary['note']:
            lines.append('')
            lines.append('> 注意：' + summary['note'])
        lines.append('')
        perf = [r for r in rows_ab if r['diff'] and r['perf']]
        other = [r for r in rows_ab if r['diff'] and not r['perf']]
        if perf:
            lines += ['### 性能相关差异', ''] + _md_table(
                ['类别', '项目', a['id'], b['id'], '比较结果'],
                [[r['category'], r['name'], r['left'], r['right'], r['status']] for r in perf]) + ['']
        else:
            lines += ['### 性能相关差异', '', '无。', '']
        if other:
            shown = other[:60]
            lines += ['### 其他差异（共 %d 项%s）' % (
                len(other), '，此处显示前 60 项' if len(other) > 60 else ''), ''] + _md_table(
                ['类别', '项目', a['id'], b['id'], '比较结果'],
                [[r['category'], r['name'], r['left'], r['right'], r['status']] for r in shown]) + ['']
    missing = sum(1 for r in matrix if r['status'] == diff.BOTH_MISSING)
    lines += ['---', '',
              '说明：`未采集`/`未报告`/`未设置` 表示没有采到对应信息，不等于没有安装或没有设置；'
              '`无法比较` 表示某个环境的这一项采集失败，不计入差异。'
              '全表共 %d 项，其中 %d 项所有环境都没有数据。'
              % (len(matrix), missing)]
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def json_report(entries, matrix, comparisons, problems, generated_at):
    return {
        'schema': 'pdenv-compare/1',
        'generated_at': generated_at,
        'environments': [{
            'id': e['id'], 'label': e['label'], 'role': e['role'], 'node': e['node'],
            'container': e['container'], 'source': e['source'],
            'collected_at': e['collected_at'], 'error': e['error'],
            'profile_ok': e['profile_ok'], 'packages_ok': e['packages_ok'],
            'profile_source': e['profile_source'],
        } for e in entries],
        'problems': problems,
        'items': [{
            'category': r['category'], 'name': r['name'],
            'values': dict(zip([e['id'] for e in entries], r['values'])),
            'status': r['status'], 'perf_relevant': r['perf'],
        } for r in matrix],
        'comparisons': [{
            **summary,
            'differences': [{
                'category': r['category'], 'name': r['name'],
                'left': r['left'], 'right': r['right'],
                'status': r['status'], 'perf_relevant': r['perf'],
            } for r in rows if r['diff']],
        } for _, _, rows, summary in comparisons],
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#f4f7fb;--fg:#17304a;--card:#fff;--line:#dfe6ed;--muted:#8b98a6;
--diff-bg:#fff1f0;--diff-fg:#9a312a;--same-bg:#eaf7f0;--same-fg:#22734e;
--warn-bg:#faf4dc;--warn-fg:#75631f;--perf-bg:#fff6e5;--perf-fg:#8a5a10}
@media(prefers-color-scheme:dark){:root{--bg:#121a24;--fg:#e6edf5;--card:#1a2431;
--line:#2b3a4c;--muted:#8fa0b4;--diff-bg:#3a1f1d;--diff-fg:#ffb4ac;
--same-bg:#16301f;--same-fg:#8fe0b0;--warn-bg:#352e14;--warn-fg:#ecd68a;
--perf-bg:#3a2d12;--perf-fg:#f5c97a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1600px;margin:auto;padding:24px 20px 64px}
h1{font-size:25px;margin:0 0 6px}h2{font-size:18px;margin:0 0 8px}
.sub{color:var(--muted);margin:0 0 14px;line-height:1.6}
nav a{display:inline-block;margin:4px 12px 4px 0;color:#2f7fd0;text-decoration:none}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
margin:16px 0;padding:18px}
.note{padding:10px 12px;background:var(--warn-bg);color:var(--warn-fg);
border-left:3px solid currentColor;border-radius:4px;margin:0 0 12px;line-height:1.6}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:10px 0}
input[type=search]{flex:1 1 260px;min-width:0;padding:8px 11px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--fg)}
label{font-size:13px;display:flex;align-items:center;gap:5px}
.count{font-size:12px;color:var(--muted)}
.table-wrap{overflow:auto;max-height:620px;border:1px solid var(--line);border-radius:7px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:left;
vertical-align:top;max-width:460px;overflow-wrap:anywhere}
th{position:sticky;top:0;background:var(--bg);z-index:2;white-space:nowrap}
td:nth-child(2){font-weight:600}
tr.perf td:nth-child(2){border-left:3px solid var(--perf-fg);padding-left:6px}
.s{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11.5px;white-space:nowrap}
.s-diff{background:var(--diff-bg);color:var(--diff-fg);font-weight:600}
.s-same{background:var(--same-bg);color:var(--same-fg)}
.s-unknown{background:var(--warn-bg);color:var(--warn-fg)}
.muted{color:var(--muted)}
.footer{color:var(--muted);font-size:12px;line-height:1.7}
@media(max-width:680px){.wrap{padding:12px}section{padding:12px}th,td{max-width:200px}}
"""

SCRIPT = """
function applyFilter(section){
  var q=(section.querySelector('input[type=search]').value||'').toLowerCase();
  var onlyDiff=section.querySelector('[data-only-diff]').checked;
  var onlyPerf=section.querySelector('[data-only-perf]').checked;
  var shown=0;
  var rows=section.querySelectorAll('tbody tr');
  for(var i=0;i<rows.length;i++){
    var row=rows[i];
    var ok=(!onlyDiff||row.dataset.diff==='1')&&(!onlyPerf||row.dataset.perf==='1')
      &&(!q||row.textContent.toLowerCase().indexOf(q)>=0);
    row.style.display=ok?'':'none';
    if(ok)shown++;
  }
  section.querySelector('[data-shown]').textContent=shown;
}
document.addEventListener('input',function(e){
  var s=e.target.closest('section'); if(s&&s.querySelector('table'))applyFilter(s);});
document.addEventListener('change',function(e){
  var s=e.target.closest('section'); if(s&&s.querySelector('table'))applyFilter(s);});
"""


def _esc(value):
    return html.escape(str(value), quote=True)


def _status_class(status):
    if diff.is_difference(status):
        return 's s-diff'
    if status in (diff.SAME, diff.ALL_SAME):
        return 's s-same'
    return 's s-unknown'


def _value_cell(value):
    cls = ' class="muted"' if fields.is_missing(value) else ''
    return '<td%s>%s</td>' % (cls, _esc(value))


def _table_section(anchor, title, subtitle, header, rows, render_row, single=False):
    total = len(rows)
    diffs = sum(1 for r in rows if r['diff'])
    perfs = sum(1 for r in rows if r['diff'] and r['perf'])
    out = ['<section id="%s"><h2>%s</h2><p class="sub">%s</p>' % (anchor, _esc(title), _esc(subtitle))]
    if single:
        # 只有一个环境时没有「差异」可言，只留搜索和性能相关筛选
        out.append(
            '<div class="controls">'
            '<input type="search" placeholder="搜索类别、项目或取值">'
            '<label><input type="checkbox" data-only-perf> 仅看性能相关项</label>'
            '<label hidden><input type="checkbox" data-only-diff></label>'
            '<span class="count">共 %d 项 · 当前显示 <b data-shown>%d</b> 项</span>'
            '</div>' % (total, total))
    else:
        out.append(
            '<div class="controls">'
            '<input type="search" placeholder="搜索类别、项目或取值">'
            '<label><input type="checkbox" data-only-diff> 仅看差异</label>'
            '<label><input type="checkbox" data-only-perf> 仅看性能相关</label>'
            '<span class="count">共 %d 项 · 差异 %d 项（性能相关 %d 项）· 当前显示 <b data-shown>%d</b> 项</span>'
            '</div>' % (total, diffs, perfs, total))
    out.append('<div class="table-wrap"><table><thead><tr>')
    out.extend('<th>%s</th>' % _esc(h) for h in header)
    out.append('</tr></thead><tbody>')
    out.extend(render_row(r) for r in rows)
    out.append('</tbody></table></div></section>')
    return ''.join(out)


def html_report(entries, matrix, comparisons, problems, generated_at):
    ids = [e['id'] for e in entries]
    single = len(entries) == 1
    body = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<title>P/D 环境对比</title><style>', CSS, '</style></head><body><div class="wrap">']
    body.append('<h1>%s</h1>' % ('P/D 环境报告' if single else 'P/D 环境对比'))
    body.append('<p class="sub">生成时间 %s · %s：%s · 全部内容离线生成，默认显示所有项目</p>'
                % (_esc(generated_at), '环境' if single else '对比环境',
                   _esc('、'.join(ids))))
    for p in problems:
        body.append('<p class="note">%s</p>' % _esc(p))
    body.append('<nav><a href="#overview">环境简表</a><a href="#all">%s</a>'
                % ('全部采集项' if single else '完整总表'))
    for i, (a, b, _, _) in enumerate(comparisons):
        body.append('<a href="#cmp%d">%s ↔ %s</a>' % (i, _esc(a['id']), _esc(b['id'])))
    body.append('</nav>')

    header, rows = overview_rows(entries)
    body.append('<section id="overview"><h2>环境简表</h2><div class="table-wrap"><table><thead><tr>'
                + ''.join('<th>%s</th>' % _esc(h) for h in header) + '</tr></thead><tbody>'
                + ''.join('<tr>' + ''.join(_value_cell(v) for v in row) + '</tr>' for row in rows)
                + '</tbody></table></div></section>')

    def matrix_row(r):
        head = ('<tr data-diff="%s" data-perf="%s"%s><td>%s</td><td>%s</td>%s'
                % ('1' if r['diff'] else '0', '1' if r['perf'] else '0',
                   ' class="perf"' if r['perf'] else '',
                   _esc(r['category']), _esc(r['name']),
                   ''.join(_value_cell(v) for v in r['values'])))
        if single:
            return head + '</tr>'
        return head + ('<td><span class="%s">%s</span></td></tr>'
                       % (_status_class(r['status']), _esc(r['status'])))

    body.append(_table_section(
        'all',
        '全部采集项' if single else '完整总表',
        '这次采到的全部项目。' if single
        else '所有环境的全部采集项目，包含相同项、缺失项与无法比较项。',
        ['类别', '项目'] + ids + ([] if single else ['比较结果']),
        matrix, matrix_row, single=single))

    for i, (a, b, rows_ab, summary) in enumerate(comparisons):
        def pair_row(r):
            return ('<tr data-diff="%s" data-perf="%s"%s><td>%s</td><td>%s</td>%s%s<td><span class="%s">%s</span></td></tr>'
                    % ('1' if r['diff'] else '0', '1' if r['perf'] else '0',
                       ' class="perf"' if r['perf'] else '',
                       _esc(r['category']), _esc(r['name']),
                       _value_cell(r['left']), _value_cell(r['right']),
                       _status_class(r['status']), _esc(r['status'])))
        subtitle = '逐项比较；差异 %d 项，其中性能相关 %d 项%s。%s' % (
            summary['diff_count'], summary['perf_diff_count'],
            '，无法比较 %d 项' % summary['not_comparable_count']
            if summary['not_comparable_count'] else '',
            ('该组合无法比较：' + summary['note']) if not summary['comparable']
            else ('注意：' + summary['note']) if summary['note'] else '')
        body.append(_table_section('cmp%d' % i, '%s ↔ %s' % (a['id'], b['id']), subtitle,
                                   ['类别', '项目', a['id'], b['id'], '比较结果'],
                                   rows_ab, pair_row))

    body.append('<p class="footer">左侧带竖线的行是性能相关项（核心包、启动参数，以及并行度、'
                '线程数、显存与缓存相关的环境变量和系统参数）。<br>'
                '“未采集 / 未报告 / 未设置”表示没有采到对应信息，不能据此认定没有安装或没有设置；'
                '“仅 X 有”表示只有一方存在该项，这算作差异并会出现在“仅看差异”里；'
                '“无法比较”表示有一方这项采集失败，不计入差异。'
                '<br>CSV 为 UTF-8 BOM，可直接用 Excel 打开。</p>')
    body.append('<script>' + SCRIPT + '</script></div></body></html>')
    return ''.join(body)

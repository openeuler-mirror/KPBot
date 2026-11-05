#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比两个或多个采集包。

    bash scripts/collect.sh ./p-env.tar.gz ./d-env.tar.gz --out ./报告

列名自动取采集包里的容器名；同名时在后面加上包名区分。只读采集结果，不连网。
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from pdenv import bundle as bundle_mod   # noqa: E402
from pdenv import diff, fields, report   # noqa: E402


def bundle_label(source):
    """采集包的名字，用来在容器重名时区分。"""
    name = pathlib.Path(str(source)).name
    for suffix in ('.tar.gz', '.tgz', '.tar'):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name or 'bundle'


def load_entries(sources, workdir, problems, failed=None):
    """加载所有采集包；单个包失败只记录问题，不中断。

    每个采集对象（容器）成为一列，列名就是容器名；两个包里有同名容器时，
    列名加上包名区分。

    `failed` 是调用方给的列表：显式点名却没读进来的包会记在这里，
    调用方据此在最前面提示并返回非零退出码。
    """
    loaded = []
    for index, source in enumerate(sources):
        try:
            data = bundle_mod.load_bundle(bundle_label(source), source,
                                          workdir / ('b%d' % index))
        except (bundle_mod.LoadError, OSError, ValueError) as e:
            reason = str(e)
            if '#' in str(source):
                reason += '（路径里带 # 的写法已经取消，直接给采集包路径）'
            message = '采集包 %s 读取失败，已跳过：%s' % (source, reason)
            problems.append(message)
            if failed is not None:
                failed.append((source, reason, message))
            continue
        if data.get('incomplete'):
            problems.append('采集包 %s：%s' % (source, data['incomplete']))
        for container in data['containers']:
            loaded.append((data, container))

    counts = {}
    for _, container in loaded:
        counts[container['container']] = counts.get(container['container'], 0) + 1

    entries, used = [], set()
    for data, container in loaded:
        name = container['container']
        label = name if counts[name] == 1 else '%s(%s)' % (name, data['label'])
        while label in used:
            label += '+'
        used.add(label)
        try:
            entry = fields.build_entry(label, data, container)
        except Exception as e:      # 解析任何一份采集都不应拖垮整体
            problems.append('%s 解析失败，已跳过：%s: %s' % (label, type(e).__name__, e))
            continue
        entry['id'] = label
        if entry.get('error'):
            problems.append('%s：这份采集不完整（%s），对比中显示为「采集失败」'
                            % (label, entry['error']))
        entries.append(entry)
    return entries


def run(args):
    """比较端的主体。scripts/collect.sh 和本文件的 main() 都走这里。"""
    out = pathlib.Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        print('输出目录不是空的：%s' % out, file=sys.stderr)
        print('请换一个 --out，或先把它清空。', file=sys.stderr)
        return 1
    # 报告里带着进程的完整环境变量和启动参数，目录和文件都只给本人可读
    os.umask(0o077)
    out.mkdir(parents=True, exist_ok=True)
    try:
        out.chmod(0o700)
    except OSError:
        pass

    problems = []
    failed = []
    generated_at = dt.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    with tempfile.TemporaryDirectory(prefix='pdenv_') as tmp:
        entries = load_entries(args.sources, pathlib.Path(tmp), problems, failed)
        # 先输出，保证失败提示排在成功汇总之前
        if failed:
            print('有 %d 个采集包没有读进来：' % len(failed), file=sys.stderr)
            for source, reason, _ in failed:
                print('  - %s：%s' % (source, reason), file=sys.stderr)
        shown = set(message for _, _, message in failed)
        if len(entries) < 2:
            for problem in problems:
                if problem not in shown:
                    print('  - ' + problem, file=sys.stderr)
            if entries:
                print('只读到 1 个环境，没法对比。要看单份采集的内容，'
                      '直接打开那次 collect 输出目录里的 环境报告.html。', file=sys.stderr)
            else:
                print('没有一份采集包能读出来。', file=sys.stderr)
            # 显式给的包没读进来、但还有环境可报时按 1（文档里 1 就是这个意思）；
            # 一个环境都没剩下才是 3。
            return 1 if (failed and entries) else 3

        matrix = diff.matrix_rows(entries)
        keys = [(r['category'], r['name']) for r in matrix]
        comparisons = []
        for left, right in diff.choose_pairs(entries):
            rows = diff.pair_rows(left, right, keys)
            comparisons.append((left, right, rows, diff.summarize_pair(left, right, rows)))

        header, rows = report.overview_rows(entries)
        report.write_csv(out / '环境简表.csv', header, rows)
        report.write_matrix_csv(out / '完整总表.csv', entries, matrix)
        (out / '对比摘要.md').write_text(
            report.markdown_report(entries, matrix, comparisons, problems), encoding='utf-8')
        (out / '对比结果.json').write_text(
            json.dumps(report.json_report(entries, matrix, comparisons, problems, generated_at),
                       ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (out / '环境对比报告.html').write_text(
            report.html_report(entries, matrix, comparisons, problems, generated_at),
            encoding='utf-8')

    if failed:
        print('下面的报告只包含读进来的那些。', file=sys.stderr)
    comparable = sum(1 for row in matrix if row['status'] != diff.NOT_COMPARABLE)
    print('环境 %d 个，对比 %d 组，可比较项目 %d 项。'
          % (len(entries), len(comparisons), comparable))
    for left, right, _, summary in comparisons:
        blocked = ('，无法比较 %d 项' % summary['not_comparable_count']
                   if summary['not_comparable_count'] else '')
        if not summary['comparable']:
            tail = '  ⚠ 该组合无法比较：' + summary['note']
        elif summary['note']:
            tail = '  ⚠ ' + summary['note']
        else:
            tail = ''
        print('  %s ↔ %s：差异 %d 项（性能相关 %d 项）%s%s'
              % (left['id'], right['id'], summary['diff_count'],
                 summary['perf_diff_count'], blocked, tail))
    rest = [problem for problem in problems if problem not in shown]
    if rest:
        print('采集问题 %d 条：' % len(rest))
        for problem in rest:
            print('  - ' + problem)
    print('报告: %s' % (out / '环境对比报告.html'))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='compare.py', description='对比两个或多个采集包。',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('sources', nargs='+', metavar='采集包',
                        help='采集目录或 .tar.gz，至少两个')
    parser.add_argument('--out', required=True, metavar='目录', help='报告输出目录')
    args = parser.parse_args(argv)
    return run(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已中断', file=sys.stderr)
        sys.exit(130)

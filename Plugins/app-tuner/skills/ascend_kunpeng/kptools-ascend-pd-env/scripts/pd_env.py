#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vLLM-Ascend P/D 环境采集与对比的统一入口。

对外只有这一个入口，不分「采集」和「对比」两步：

    bash scripts/collect.sh vllm-p                      # 采一个，出单环境报告
    bash scripts/collect.sh vllm-p vllm-d               # 采两个，采完自动比
    bash scripts/collect.sh vllm-p /data/上周-d.tar.gz  # 现采一个 + 历史包，自动比
    bash scripts/collect.sh /data/a.tar.gz /data/b.tar.gz   # 只比历史包，不采集

对象是容器名还是采集包，按「这个路径存不存在」自动区分：
存在的目录或 .tar.gz 当作采集包，其余当作本机容器名。

采集与对比的实现在 pdenv/ 和 compare.py，本文件只做分流、编排和退出码汇总。
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

EPILOG = """\
例子:
  docker ps --format 'table {{.Names}}\\t{{.ID}}\\t{{.Image}}'   # 先看容器真名
  collect.sh vllm-p                            # 采一个
  collect.sh vllm-p vllm-d                     # 采两个并自动比
  collect.sh vllm-p /data/上周-d.tar.gz        # 现采 + 历史包
  collect.sh /data/a.tar.gz /data/b.tar.gz     # 只比历史包

输出目录里（采集/ 和 采集.tar.gz 只在采了本机容器时才有）:
  采集/环境报告.html      这次采到的环境，单个也能直接看
  采集/SUMMARY.txt        文本摘要
  采集.tar.gz             打包好的采集包，可拷到其他机器上继续对比
  对比/环境对比报告.html  凑够两个及以上环境时才有
  对比/环境简表.csv  完整总表.csv  对比摘要.md  对比结果.json

退出码: 0 正常（采成功一个容器也算，会出单环境报告）;
       1 参数错误、输出目录问题，或给的采集包有没读进来的（还剩环境可报时）;
       2 环境不可用（没有 python3，或要采容器却连不上 docker）;
       3 一个对象都没采成功、采集包全都读不出来，或对象本身凑不出两个环境;
       130 被 Ctrl-C 中断。
"""


BUNDLE_SUFFIXES = ('.tar.gz', '.tgz', '.tar')


def looks_like_path(text):
    """判断参数写的是不是路径：写错的路径不应被当成容器名送去 docker inspect。"""
    return '/' in text or text.startswith('.') or text.endswith(BUNDLE_SUFFIXES)


def split_targets(targets):
    """把对象分成「本机容器」和「已有采集包」两堆。

    判据和文档写的一致：只有已存在的目录或已存在的 .tar.gz 算采集包，
    其余一律当容器名。一个名字同时又是本地路径时给出提示，避免被静默当成
    采集包而少采一个环境。
    """
    containers, bundles, bad, notes = [], [], [], []
    for target in targets:
        path = pathlib.Path(target).expanduser()
        bare = '/' not in target          # 不带路径分隔符的名字才可能是容器名
        if path.is_dir() or (path.is_file() and target.endswith(BUNDLE_SUFFIXES)):
            bundles.append(str(path))
            if bare:
                notes.append('%s 是本地已存在的%s，已按采集包处理，不会去采同名容器；'
                             '要采容器就换个工作目录再跑。'
                             % (target, '目录' if path.is_dir() else ' .tar.gz 文件'))
        elif path.exists():
            if bare:
                containers.append(target)
                notes.append('%s 本地有同名文件，但只有目录和 .tar.gz 算采集包，'
                             '已按容器名处理。' % target)
            else:
                bad.append((target, '存在，但既不是目录也不是 .tar.gz，不能当采集包'))
        elif looks_like_path(target):
            bad.append((target, '找不到'))
        else:
            containers.append(target)
    return containers, bundles, bad, notes


def collected_count(collect_dir):
    """这次采集真正采到了几个对象。读不到就当 0。"""
    try:
        meta = json.loads((collect_dir / 'collection_meta.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    return len(meta.get('targets') or [])


class _Parser(argparse.ArgumentParser):
    """参数错误返回 1。argparse 默认返回 2，而 2 在文档里是「环境不可用」。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, '%s: error: %s\n' % (self.prog, message))


def build_parser():
    parser = _Parser(
        prog=os.environ.get('PD_ENV_PROG', 'pd_env.py'),
        description='vLLM-Ascend P/D 环境采集与对比（只读采集，不连网）。'
                    '给一个对象就采集并分析，给多个就自动比。',
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('targets', nargs='+', metavar='对象',
                        help='本机容器名，或已有的采集包（目录 / .tar.gz），可以混着写')
    parser.add_argument('--out', metavar='目录', help='输出目录，默认 pdenv_<时间>')
    parser.add_argument('--role', choices=['P', 'D', 'PD', 'other'],
                        help='这台机器的角色，只作标记')
    parser.add_argument('--python', metavar='路径',
                        help='容器里 Python 的路径，默认自动找')
    return parser


def run(args):
    containers, bundles, bad, notes = split_targets(args.targets)
    if bad:
        print('下面这些写成了路径，但不能当采集包用：', file=sys.stderr)
        for item, reason in bad:
            print('  - %s：%s' % (item, reason), file=sys.stderr)
        print('如果想采本机容器，容器名里不要带 / 和 .tar.gz。', file=sys.stderr)
        return 1
    for note in notes:
        print('提示：%s' % note, file=sys.stderr)
    if args.role and not containers:
        print('提示：--role 只在采集容器时有意义，这次没有要采的容器，已忽略。',
              file=sys.stderr)
    if containers:
        # 连不上 docker 就不往下走：否则会写出一份「成功 0 项」的空报告
        from pdenv import collect as collect_mod
        _, docker_error = collect_mod.docker_cmd(
            ['version', '--format', '{{.Server.Version}}'], timeout=20)
        if docker_error:
            print('连不上 docker，本机容器采不了：%s' % docker_error, file=sys.stderr)
            print('先跑 discover.sh 看环境。只对比已有采集包时不需要 docker。',
                  file=sys.stderr)
            return 2

    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = (pathlib.Path(args.out).expanduser().resolve() if args.out
           else pathlib.Path('pdenv_%s' % stamp).resolve())
    if out.exists() and any(out.iterdir()):
        print('输出目录不是空的：%s' % out, file=sys.stderr)
        print('请换一个 --out，或先把它清空。', file=sys.stderr)
        return 1

    collect_dir = out / '采集'
    compare_dir = out / '对比'
    sources = []
    collect_code = 0
    fresh_count = 0

    if containers:
        print('第 1 步：采集 %d 个容器（%s）' % (len(containers), '、'.join(containers)),
              flush=True)
        collect_code = collect_mod.run(argparse.Namespace(
            containers=containers, role=args.role,
            python=args.python, out=str(collect_dir)))
        if collect_code == 130:
            print('采集被中断，已采到的部分留在 %s。' % collect_dir, file=sys.stderr)
            return 130
        if collect_code == 1:
            return 1
        fresh_count = collected_count(collect_dir)
        if fresh_count:
            sources.append(str(collect_dir))
    sources.extend(bundles)

    # 只有一个环境就没得比：这种情况下采集端已经出了 环境报告.html
    only_fresh_single = (len(sources) == 1 and not bundles and fresh_count < 2)
    if not sources:
        print('没有任何对象可以分析。', file=sys.stderr)
        return 3
    if only_fresh_single:
        print('只有 1 个环境，不做对比。报告在 %s。' % (collect_dir / '环境报告.html'),
              flush=True)
        return 3 if collect_code else 0

    import compare
    print('第 %d 步：对比 %d 份采集' % (2 if containers else 1, len(sources)), flush=True)
    compare_code = compare.run(argparse.Namespace(sources=sources, out=str(compare_dir)))
    if compare_code == 3 and fresh_count:
        print('没凑够两个能比的环境，单份结果看 %s。' % (collect_dir / '环境报告.html'),
              file=sys.stderr)
    return compare_code or (3 if collect_code else 0)


def main(argv=None):
    return run(build_parser().parse_args(argv))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('已中断', file=sys.stderr)
        sys.exit(130)

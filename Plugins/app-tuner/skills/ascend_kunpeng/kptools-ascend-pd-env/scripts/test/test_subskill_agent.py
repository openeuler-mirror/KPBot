#!/usr/bin/env python3
"""用 Codex CLI 验证父 skill 路由。需要登录，只使用临时目录和离线样例。

用法：python3 scripts/test/test_subskill_agent.py
可附带用例名，只运行指定用例。在六个子 skill 齐备的目录运行；
缺失子 skill 的场景由用例自行构造。
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


SKILL = pathlib.Path(__file__).resolve().parents[2]
ENTRY = '.agents/skills/' + SKILL.name
SUMMARY = 'summarize-environment-findings'
HISTORY = SKILL / 'subskills' / SUMMARY / 'evals/fixtures/p-history.json'


def bundle(work, name, container, role, day=24):
    root = work / name
    subprocess.run([sys.executable, str(SKILL / 'scripts/test/make_fixture.py'),
                    str(root), container], check=True, stdout=subprocess.DEVNULL)
    path = root / 'collection_meta.json'
    meta = json.loads(path.read_text(encoding='utf-8'))
    meta.update(role=role, timestamp_utc='202610%02dT000000Z' % day,
                timestamp_local='2026-10-%02d 08:00:00 +0800' % day)
    path.write_text(json.dumps(meta), encoding='utf-8')


def run(work, prompt, child=None):
    result = subprocess.run(
        ['codex', '-a', 'never', 'exec', '--ephemeral', '--ignore-user-config',
         '--skip-git-repo-check', '-s', 'workspace-write', '--json', '-C', str(work),
         '从 ' + ENTRY + '/SKILL.md 开始，按该 skill 处理以下请求。'
         '只使用此测试目录及其中的工具。\n' + prompt],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
        env=dict(os.environ, DOCKER_HOST='unix:///no-such-pdenv-docker.sock'))
    if result.returncode:
        raise RuntimeError(result.stderr)
    items = [event['item'] for event in map(json.loads, result.stdout.splitlines())
             if event.get('type') == 'item.completed']
    executions = [item for item in items if item['type'] == 'command_execution']
    commands = [item['command'] for item in executions]
    messages = [item['text'] for item in items if item['type'] == 'agent_message']
    assert messages, items
    if child:
        assert any('name: ' + child + '\n' in item.get('aggregated_output', '')
                   for item in executions), (child, commands, messages[-1])
    return messages[-1], commands


def check_no_action(commands):
    executed = r'(?:\b(?:bash|sh|python3?|docker)\b[^;|&\n]*[/ ](?:collect\.sh|compare\.py)\b|\bdocker\s+(?:ps|inspect|exec)\b)'
    assert all(not re.search(executed, command) for command in commands), commands


def collect_role(work):
    answer, commands = run(work, '只给容器 engine-2 留档，角色我还没确认。', 'collect-environment')
    check_no_action(commands)
    assert 'engine-2' in answer and '角色' in answer and re.search(r'确认|请问|是 P', answer), answer


def collect_only(work):
    scripts = work / ENTRY / 'scripts'
    (scripts / 'discover.sh').write_text("printf '%s\\n' DOCKER_OK\n", encoding='utf-8')
    (scripts / 'collect.sh').write_text(
        'set -eu\n'
        'test "$#" = 5 && test "$2" = --role && test "$4" = --out || exit 1\n'
        'mkdir -p "$5/采集"\n'
        'printf \'{"targets":["%s"],"role":"%s"}\\n\' "$1" "$3" > "$5/采集/collection_meta.json"\n'
        'printf "采集完成\\n" > "$5/采集/SUMMARY.txt"\n'
        'tar -czf "$5/采集.tar.gz" -C "$5" 采集\n', encoding='utf-8')
    _, commands = run(work, '同一宿主机上，prefill-1 是 P，decode-1 是 D。请只采集留档，'
                      'P 输出到 p-out/，D 输出到 d-out/（相对于当前测试目录）。',
                      'collect-environment')
    assert all('compare.py' not in cmd for cmd in commands), commands
    for prefix, role, name in [('p', 'P', 'prefill-1'), ('d', 'D', 'decode-1')]:
        out = work / (prefix + '-out')
        meta = json.loads((out / '采集/collection_meta.json').read_text(encoding='utf-8'))
        assert meta == {'targets': [name], 'role': role}, meta
        assert (out / '采集.tar.gz').is_file()


def analysis_gap(work):
    bundle(work, 'decode', 'engine-nopkg', 'D')
    _, commands = run(work, '分析 decode/ 这份 D 快照，不连容器。'
                      '请把报告保存为 D-环境分析.md。', 'analyze-environment')
    check_no_action(commands)
    report = (work / 'D-环境分析.md').read_text(encoding='utf-8')
    assert 'engine-nopkg' in report and 'torch_npu' in report, report
    assert re.search(r'未采到|缺失|无法确认', report), report


def role_conflict(work):
    bundle(work, 'old', 'vllm-d', 'P')
    bundle(work, 'new', 'vllm-d', 'D')
    answer, commands = run(work, '请对比旧 D（old/）和新 D（new/），把结果放到 report/。',
                           'compare-d-over-time')
    check_no_action(commands)
    assert 'old' in answer and re.search(r'角色|标记|标为', answer), answer
    assert not (work / 'report' / '对比结果.json').exists()


def check_pair(work, child, prompt, roles):
    run(work, prompt + '请把对比产物保存到 report/（相对于当前测试目录）。', child)
    report = json.loads((work / 'report/对比结果.json').read_text(encoding='utf-8'))
    environments = report['environments']
    assert [(entry['role'], pathlib.Path(entry['source']).name) for entry in environments] == roles, report
    assert [(pair['left'], pair['right']) for pair in report['comparisons']] == [
        (environments[0]['id'], environments[1]['id'])], report


def p_history_pair(work):
    bundle(work, 'old', 'engine', 'P', day=23)
    bundle(work, 'new', 'engine', 'P')
    check_pair(work, 'compare-p-over-time', '以旧 P（old/）为基线对比新 P（new/）。',
               [('P', 'old'), ('P', 'new')])


def pd_pair(work):
    bundle(work, 'p', 'engine', 'P')
    bundle(work, 'd', 'engine', 'D')
    check_pair(work, 'compare-p-and-d', '只对比 P（p/）和 D（d/）两份现有快照。',
               [('P', 'p'), ('D', 'd')])


def retained_result(work):
    shutil.copyfile(HISTORY, work / HISTORY.name)
    _, commands = run(work, '只有旧 P 和新 P 的对比结果 ' + HISTORY.name +
                      '，原包已经清理，其他组没有跑。请在当前目录生成环境汇总报告。', SUMMARY)
    check_no_action(commands)
    report = (work / '环境汇总报告.md').read_text(encoding='utf-8')
    assert all(term in report for term in ('p-history.json', '8.0.0', '8.1.0', '未执行')), report
    assert all('p-history.json' in line or '未执行' not in line
               for line in report.splitlines() if '旧 P' in line and '新 P' in line and '|' in line), report


def missing_child(work):
    shutil.rmtree(work / ENTRY / 'subskills/compare-p-over-time')
    answer, commands = run(work, '按旧 P 为基线和新 P 比较，给我前后变化。')
    check_no_action(commands)
    assert re.search(r'不可用|未安装|未启用|缺少|不存在', answer), answer


def main():
    if not shutil.which('codex'):
        sys.exit('需要 Codex CLI')
    for child in ('collect-environment', 'analyze-environment', 'compare-p-over-time',
                  'compare-d-over-time', 'compare-p-and-d', SUMMARY):
        if not (SKILL / 'subskills' / child / 'SKILL.md').is_file():
            sys.exit('测试需要已装配的子 skill：' + child)
    scenarios = {scenario.__name__: scenario for scenario in (
        collect_role, collect_only, analysis_gap, role_conflict, p_history_pair,
        pd_pair, retained_result, missing_child)}
    failed = 0
    for name in sys.argv[1:] or scenarios:
        if name not in scenarios:
            sys.exit('未知用例：' + name)
        with tempfile.TemporaryDirectory(prefix='pdenv-agent-') as temp:
            work = pathlib.Path(temp)
            shutil.copytree(SKILL, work / ENTRY, ignore=shutil.ignore_patterns('__pycache__', 'test', 'evals'))
            try:
                scenarios[name](work)
            except (AssertionError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                print('FAIL %s: %s' % (name, exc), flush=True)
                failed += 1
            else:
                print('PASS ' + name, flush=True)
    return int(failed > 0)


if __name__ == '__main__':
    sys.exit(main())

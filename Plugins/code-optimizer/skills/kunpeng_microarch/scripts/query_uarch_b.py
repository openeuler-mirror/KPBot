#!/usr/bin/env python3
"""
0xd03 指令性能查询脚本

用法:
    python query_uarch_b.py <指令名>           # 查询该指令所有版本
    python query_uarch_b.py <指令名> --json    # JSON格式输出
    python query_uarch_b.py --list             # 列出所有指令
"""

import json
import sys
import os

DEFAULT_JSON_PATH = os.path.join(os.path.dirname(__file__), '[REDACTED]_full.json')

def load_data(json_path=DEFAULT_JSON_PATH):
    with open(json_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def build_lookup(data):
    """构建指令名到性能数据的映射"""
    lookup = {}
    for t in data:
        for inst in t['instructions']:
            for name in inst['instructions']:
                if name not in lookup:
                    lookup[name] = []
                lookup[name].append({
                    'table': t['title'],
                    'group': inst['instruction_group'],
                    'latency': inst['exec_latency'],
                    'throughput': inst['exec_throughput'],
                    'pipeline': inst['utilized_pipelines'],
                    'notes': inst['notes']
                })
    return lookup

def query(lookup, name):
    """查询指令性能"""
    name_upper = name.upper()
    if name_upper in lookup:
        return lookup[name_upper]
    # 模糊匹配
    matches = {k: v for k, v in lookup.items() if name_upper in k}
    return matches

def format_results(name, results):
    """格式化输出"""
    if isinstance(results, dict):
        print(f"\n指令 '{name}' 模糊匹配结果:")
        for k, v in results.items():
            print(f"  {k}: {len(v)} 个版本")
        return
    
    if not results:
        print(f"\n未找到指令 '{name}'")
        return
    
    print(f"\n{'=' * 70}")
    print(f"指令: {name}")
    print(f"版本数: {len(results)}")
    print(f"{'=' * 70}")
    
    for i, r in enumerate(results, 1):
        print(f"\n[版本 {i}] {r['table']}")
        print(f"  Group:    {r['group']}")
        print(f"  Latency:  {r['latency'] or 'N/A'}")
        print(f"  Throughput: {r['throughput'].replace(chr(10), ' | ')}")
        print(f"  Pipeline: {r['pipeline'] or 'N/A'}")
        if r['notes']:
            print(f"  Notes:    {r['notes']}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description='0xd03 指令性能查询')
    parser.add_argument('instruction', nargs='?', help='指令名')
    parser.add_argument('--json', '-j', action='store_true', help='JSON格式输出')
    parser.add_argument('--list', '-l', action='store_true', help='列出所有指令')
    parser.add_argument('--data', '-d', default=DEFAULT_JSON_PATH, 
                        help=f'JSON数据文件路径')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.data):
        print(f"错误: 数据文件不存在: {args.data}")
        sys.exit(1)
    
    lookup = build_lookup(load_data(args.data))
    
    if args.list:
        all_insts = sorted(lookup.keys())
        print(f"\n共 {len(all_insts)} 条指令:")
        for i, name in enumerate(all_insts, 1):
            versions = len(lookup[name])
            print(f"  {name:15} ({versions} 版本)")
        return
    
    if not args.instruction:
        print("用法: query_uarch_b.py <指令名>")
        print("示例: query_uarch_b.py ADD")
        print("      query_uarch_b.py TBL --json")
        print("      query_uarch_b.py --list")
        return
    
    results = query(lookup, args.instruction)
    
    if args.json and isinstance(results, list):
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        format_results(args.instruction, results)

if __name__ == '__main__':
    main()

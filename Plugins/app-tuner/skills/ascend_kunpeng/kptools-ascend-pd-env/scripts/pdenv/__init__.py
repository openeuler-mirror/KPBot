"""P/D 环境采集与对比工具。

采集端：
- probe:   12 个采集项，每项单独超时和兜底，结果按行输出
- collect: 通过 docker exec 采集指定容器，汇总成 bundle，写人读摘要

对比端：
- bundle: 读取采集包（目录或 .tar.gz）
- fields: 把一份采集摊平成可比较的项
- diff:   比较逻辑，单边存在算差异
- report: HTML / CSV / Markdown / JSON 四种输出

入口是 scripts/collect.sh：采集、分析、自动对比都走它。
"""
__all__ = ['probe', 'collect', 'bundle', 'fields', 'diff', 'report']
VERSION = '5.0'

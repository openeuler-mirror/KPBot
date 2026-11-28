#!/usr/bin/env python3
"""从 Excel 生成可离线打开的里程碑甘特图。"""

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile


def iso_day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("请使用有效的 YYYY-MM-DD 日期")


def save_html(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        tmp = Path(temp.name)
        try:
            temp.write(content)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="用户填写的 .xlsx 工作簿")
    parser.add_argument("-o", "--output", type=Path, help="输出 HTML，默认在输入文件旁生成「文件名-甘特图.html」")
    parser.add_argument("--title", help="图表标题，默认使用输入文件名")
    parser.add_argument("--as-of", type=iso_day, metavar="YYYY-MM-DD", help="固定复盘基准日，默认使用浏览器当地的今天")
    args = parser.parse_args()

    source = args.input.expanduser().resolve()
    target = (args.output or source.with_name(source.stem + "-甘特图.html")).expanduser().resolve()
    if source.suffix.lower() != ".xlsx" or not source.is_file():
        parser.error(f"需要一个存在的 .xlsx 文件：{source}")
    if target.suffix.lower() not in (".htm", ".html") or target == source:
        parser.error("请使用与输入工作簿不同的 .html 输出文件")
    try:
        from read_workbook import read_workbook
        from chart import render_html
        from openpyxl.utils.exceptions import InvalidFileException
    except ModuleNotFoundError as exc:
        if exc.name == "openpyxl":
            parser.exit(2, "需要 openpyxl。请用已安装该库的 Python，或在虚拟环境中安装技能目录的 requirements.txt。\n")
        raise

    try:
        tasks, milestones = read_workbook(source)
    except (BadZipFile, KeyError, ParseError, InvalidFileException) as exc:
        parser.exit(2, f"无法读取 Excel 工作簿：{exc}\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"无法读取输入：{exc}\n")
    try:
        chart = render_html(tasks, milestones, (args.title or source.stem).strip() or source.stem, args.as_of)
        save_html(target, chart)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"生成失败：{exc}\n")
    print(json.dumps({"output": str(target), "tasks": len(tasks), "milestones": len(milestones),
                      "as_of": args.as_of}, ensure_ascii=False))


if __name__ == "__main__":
    main()

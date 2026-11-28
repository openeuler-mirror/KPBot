"""读取里程碑甘特图的 Excel 输入。日期按自然日，完成率输出为 0–100。"""

from contextlib import closing
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter
from openpyxl.utils.datetime import CALENDAR_MAC_1904, from_excel


STATES = {"未开始", "进行中", "受阻", "待验收", "已完成", "待更新"}


def _text(value):
    return "" if value is None else str(value).strip()


def _date(value, epoch):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        try:
            value = from_excel(value, epoch)
        except (ValueError, OverflowError):
            raise ValueError("Excel 日期超出有效范围") from None
    if isinstance(value, time) and value == time(0) and epoch == CALENDAR_MAC_1904:
        return epoch.date().isoformat()
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
    raise ValueError("请填写有效的 Excel 日期或 YYYY-MM-DD")


def _progress(value, number_format):
    raw = _text(value).replace("％", "%")
    if not raw:
        return None
    suffix = raw.endswith("%")
    try:
        n = Decimal(raw[:-1].strip() if suffix else raw)
    except InvalidOperation:
        raise ValueError("请填写 0% 到 100% 之间的完成率") from None
    if not n.is_finite() or n < 0 or n > 100:
        raise ValueError("完成率须在 0% 到 100% 之间")
    native_percent = not isinstance(value, str) and "%" in (number_format or "")
    if not suffix and (native_percent or n <= 1):
        n *= 100
    if n > 100:
        raise ValueError("完成率须在 0% 到 100% 之间")
    return int(n) if n == n.to_integral() else float(n)


def _records(values, formulas, sheet, required, optional=()):
    """在第一至十行找表头，保留单元格格式和位置以便报告错误。"""
    ws, raw = values[sheet], formulas[sheet]
    # 某些第三方导出器的 dimension 小于实有区域，按 sheet XML 读取。
    ws.reset_dimensions()
    raw.reset_dimensions()
    fields = (*required, *optional)
    indexes = {}
    for rownum, (row, original) in enumerate(zip(ws.rows, raw.rows), 1):
        if not indexes:
            names = [_text(cell.value) for cell in row]
            if all(field in names for field in required):
                for field in fields:
                    if names.count(field) > 1:
                        raise ValueError(f"「{sheet}」第 {rownum} 行：表头「{field}」重复")
                    if field in names:
                        indexes[field] = names.index(field)
            elif rownum >= 10:
                break
            continue
        record = {}
        for field, index in indexes.items():
            cell = row[index] if index < len(row) else None
            value = cell.value if cell is not None else None
            where = f"「{sheet}」{get_column_letter(index + 1)}{rownum}（{field}）"
            if index < len(original) and original[index].data_type == "f" and value is None:
                raise ValueError(f"{where}：公式没有已保存的计算结果。请在 Excel 中重算并保存，或粘贴为值。")
            if cell is not None and cell.data_type == "e":
                raise ValueError(f"{where}：Excel 返回 {value}")
            record[field] = value, cell.number_format if cell is not None else None
        if any(_text(v) for v, _ in record.values()):
            yield rownum, record
    if not indexes:
        raise ValueError(f"「{sheet}」前 10 行需要表头：{'、'.join(required)}")


def read_workbook(path):
    """返回可直接传给 render_html 的 (tasks, milestones)；只读输入文件。"""
    path = Path(path)
    opts = {"read_only": True, "keep_links": False}
    tasks, milestones = [], []
    with closing(load_workbook(path, data_only=True, **opts)) as values, \
         closing(load_workbook(path, data_only=False, **opts)) as formulas:
        if "事项" not in values.sheetnames:
            raise ValueError(f"找不到「事项」工作表，当前有：{'、'.join(values.sheetnames)}")
        schema = [("事项", ("事项名称", "开始日期", "截止日期"), ("负责人", "状态", "完成率"))]
        if "里程碑" in values.sheetnames:
            schema.append(("里程碑", ("里程碑名称", "日期"), ()))
        else:
            for ws in values.worksheets:
                if ws.title == "事项":
                    continue
                for row in ws.iter_rows(max_row=10, values_only=True):
                    if {"里程碑名称", "日期"}.issubset({_text(v) for v in row}):
                        raise ValueError(f"「{ws.title}」含里程碑表头，请将此工作表命名为「里程碑」")
        for sheet, required, optional in schema:
            for rownum, record in _records(values, formulas, sheet, required, optional):
                entry = {}
                for field, (value, fmt) in record.items():
                    try:
                        if field in required and not _text(value):
                            raise ValueError("此项必填")
                        if field in ("日期", "开始日期", "截止日期"):
                            entry[field] = _date(value, values.epoch)
                        elif field == "完成率":
                            entry[field] = _progress(value, fmt)
                        else:
                            entry[field] = _text(value)
                    except ValueError as exc:
                        raise ValueError(f"「{sheet}」第 {rownum} 行「{field}」：{exc}") from None
                if sheet == "里程碑":
                    milestones.append({"name": entry["里程碑名称"], "date": entry["日期"]})
                    continue
                if entry["截止日期"] < entry["开始日期"]:
                    raise ValueError(f"「事项」第 {rownum} 行「截止日期」早于开始日期")
                state = entry.get("状态") or "待更新"
                if state not in STATES:
                    raise ValueError(f"「事项」第 {rownum} 行「状态」不支持「{state}」，可填：{'、'.join(sorted(STATES))}")
                tasks.append({"name": entry["事项名称"], "start": entry["开始日期"],
                              "end": entry["截止日期"], "owner": entry.get("负责人", ""),
                              "status": state, "progress": entry.get("完成率")})
    if not tasks and not milestones:
        raise ValueError("工作簿尚无记录，请添加至少一条事项或里程碑")
    return tasks, milestones

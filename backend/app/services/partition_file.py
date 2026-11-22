"""分区策略文件解析与逐行校验。

文件格式支持 CSV / TSV / 分号或竖线分隔文本 / JSON 数组 / JSONL。
解析结果保留真实行号（CSV 为物理行号，JSON 数组为元素序号），
供后续登记结果追溯到「已接收的文件 + 行号」。
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import re
from typing import Any

GRANULARITY_ALIASES: dict[str, set[str]] = {
    "day": {"day", "d", "dd", "1d", "日", "天", "按天", "按日", "每日"},
    "month": {"month", "m", "mo", "mon", "1m", "1mo", "1月", "月", "按月", "每月"},
    "hour": {"hour", "h", "hh", "1h", "小时", "时", "按小时", "每小时"},
    "year": {"year", "y", "yyyy", "1y", "1年", "年", "按年", "每年"},
}
GRANULARITY_LABELS = {"day": "天", "month": "月", "hour": "小时", "year": "年"}
GRANULARITY_KEY_FIELDS = {"day": "dt", "month": "month", "hour": "hour", "year": "year"}
GRANULARITY_EXPECT = {
    "day": "YYYY-MM-DD",
    "month": "YYYY-MM",
    "hour": "YYYY-MM-DDTHH",
    "year": "YYYY",
}

HEADER_ALIASES: dict[str, set[str]] = {
    "table": {
        "目标表", "目标湖表", "表名", "湖表", "table", "targettable",
        "tablename", "target", "target_table", "表",
    },
    "date": {"日期", "分区日期", "dt", "date", "partitiondate", "分区", "数据日期"},
    "granularity": {
        "分区粒度", "粒度", "granularity", "partitiongranularity",
        "partgranularity", "granularitylevel", "分区级别",
    },
    "key_field": {
        "分区键", "分区字段", "partitionkey", "partitionfield", "key", "keyfield",
    },
}

MAX_FILE_BYTES = 10 * 1024 * 1024
_DATE_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "day": [
        ("%Y-%m-%d", r"^\d{4}-\d{2}-\d{2}$"),
        ("%Y/%m/%d", r"^\d{4}/\d{2}/\d{2}$"),
        ("%Y%m%d", r"^\d{8}$"),
    ],
    "month": [
        ("%Y-%m", r"^\d{4}-\d{2}$"),
        ("%Y/%m", r"^\d{4}/\d{2}$"),
        ("%Y%m", r"^\d{6}$"),
    ],
    "hour": [
        ("%Y-%m-%dT%H", r"^\d{4}-\d{2}-\d{2}T\d{2}$"),
        ("%Y-%m-%d %H", r"^\d{4}-\d{2}-\d{2} \d{2}$"),
        ("%Y/%m/%d %H", r"^\d{4}/\d{2}/\d{2} \d{2}$"),
        ("%Y-%m-%dT%H:%M", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$"),
        ("%Y-%m-%d %H:%M", r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"),
    ],
    "year": [
        ("%Y", r"^\d{4}$"),
    ],
}


def _norm_header(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "")).casefold()


_NORM_ALIASES = {
    field: {_norm_header(alias) for alias in aliases}
    for field, aliases in HEADER_ALIASES.items()
}


def normalize_granularity(raw: Any) -> str | None:
    """把文件里的粒度写法归一化；不支持的粒度返回 None。"""
    text = _norm_header(raw)
    if not text:
        return None
    for canonical, aliases in GRANULARITY_ALIASES.items():
        if text in {_norm_header(alias) for alias in aliases}:
            return canonical
    return None


def normalize_date(raw: Any, granularity: str) -> str | None:
    """按粒度校验并归一化日期；与粒度不匹配时返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt, pattern in _DATE_PATTERNS.get(granularity, []):
        if not re.match(pattern, text):
            continue
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            return None
        if granularity == "day":
            return parsed.strftime("%Y-%m-%d")
        if granularity == "month":
            return parsed.strftime("%Y-%m")
        if granularity == "hour":
            return parsed.strftime("%Y-%m-%dT%H")
        return parsed.strftime("%Y")
    return None


def _clean_cell(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _decode_rows_csv(text: str) -> list[tuple[int, list[str]]]:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = ","
    for candidate in ("\t", ";", "|", ","):
        if candidate in first_line:
            delimiter = candidate
            break
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[tuple[int, list[str]]] = []
    for cells in reader:
        if not any(_clean_cell(cell) for cell in cells):
            continue
        rows.append((reader.line_num, [_clean_cell(cell) for cell in cells]))
    return rows


def _decode_rows_json(text: str) -> list[tuple[int, dict[str, Any]]]:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        # 按 JSONL 解析，行号就是物理行号。
        rows: list[tuple[int, dict[str, Any]]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_no} 行不是合法 JSON：{exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"第 {line_no} 行必须是 JSON 对象")
            rows.append((line_no, item))
        return rows
    if isinstance(payload, dict):
        for key in ("rows", "rules", "items", "data", "records"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("JSON 文件必须是对象数组或包含 rows/rules/items 数组")
    rows = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个元素必须是 JSON 对象")
        rows.append((index, item))
    return rows


def _map_headers(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        normalized = _norm_header(cell)
        for field, aliases in _NORM_ALIASES.items():
            if normalized in aliases and field not in mapping:
                mapping[field] = index
    return mapping


def _looks_like_header(cells: list[str]) -> bool:
    return any(_norm_header(cell) in aliases for cell in cells
               for aliases in _NORM_ALIASES.values())


def _row_dict(cells: list[str], mapping: dict[str, int] | None,
               header: list[str] | None) -> dict[str, Any]:
    """把一行转成 {table,date,granularity,key_field,原始行}。"""
    if mapping is not None:
        def pick(field: str) -> str:
            index = mapping.get(field)
            return cells[index] if index is not None and index < len(cells) else ""
        raw = {
            _clean_cell(header[index]) if header and index < len(header) else f"col{index}":
            cells[index]
            for index in range(len(cells))
        }
        return {
            "table": pick("table"),
            "date": pick("date"),
            "granularity": pick("granularity"),
            "key_field": pick("key_field"),
            "原始行": raw,
        }
    values = [*cells, "", "", ""]
    return {
        "table": values[0],
        "date": values[1],
        "granularity": values[2],
        "key_field": values[3],
        "原始行": {f"col{index}": cell for index, cell in enumerate(cells)},
    }


def parse_and_validate(text: str, default_table: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """解析文件并逐行校验。

    返回 (行结果列表, 文件级错误列表)。行结果里 ``有效`` 为 True 的行才会
    进入规则包并可被湖表登记领取；失败行保留 ``失败原因`` 供追查，
    但永远不会产生湖表分区记录。
    """
    if not text or not text.strip():
        return [], ["文件内容为空"]
    if len(text.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
        return [], [f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限"]

    stripped = text.lstrip("﻿").strip()
    json_mode = stripped.startswith(("{", "["))
    header: list[str] | None = None
    mapping: dict[str, int] | None = None
    results: list[dict[str, Any]] = []

    try:
        if json_mode:
            raw_rows: list[tuple[int, Any]] = _decode_rows_json(stripped)
            for line_no, item in raw_rows:
                results.append(_validate_row(line_no, {
                    "table": _clean_cell(item.get("目标表") or item.get("表名")
                                         or item.get("table") or item.get("target_table")),
                    "date": _clean_cell(item.get("日期") or item.get("dt")
                                        or item.get("date") or item.get("partition_date")),
                    "granularity": _clean_cell(item.get("分区粒度") or item.get("粒度")
                                               or item.get("granularity")),
                    "key_field": _clean_cell(item.get("分区字段") or item.get("分区键")
                                             or item.get("partition_key")),
                    "原始行": item,
                }, default_table))
            return results, []
        raw_rows = _decode_rows_csv(stripped)
        if not raw_rows:
            return [], ["文件没有可解析的数据行"]
        first_line_no, first_cells = raw_rows[0]
        if _looks_like_header(first_cells):
            header = first_cells
            mapping = _map_headers(first_cells)
            data_rows = raw_rows[1:]
            missing = [name for name in ("table", "date", "granularity")
                       if name not in mapping]
            if missing:
                label = {"table": "目标表", "date": "日期", "granularity": "分区粒度"}
                reason = "表头缺少列：" + "、".join(label[name] for name in missing)
                return [dict(_validate_row(line_no, {"table": "", "date": "",
                                                     "granularity": "", "key_field": "",
                                                     "原始行": cells}, default_table),
                             **{"有效": False, "失败原因": reason})
                        for line_no, cells in data_rows], []
        else:
            data_rows = raw_rows
        for line_no, cells in data_rows:
            results.append(_validate_row(line_no, _row_dict(cells, mapping, header),
                                         default_table))
        if not results:
            return [], ["文件没有数据行"]
        return results, []
    except ValueError as exc:
        return [], [str(exc)]
    except csv.Error as exc:
        return [], [f"文件解析失败：{exc}"]


def _validate_row(line_no: int, values: dict[str, Any],
                  default_table: str = "") -> dict[str, Any]:
    table = _clean_cell(values.get("table")) or _clean_cell(default_table)
    raw_date = _clean_cell(values.get("date"))
    raw_granularity = _clean_cell(values.get("granularity"))
    key_field = _clean_cell(values.get("key_field"))

    result: dict[str, Any] = {
        "行号": line_no,
        "目标湖表": table,
        "日期": raw_date,
        "分区粒度": raw_granularity,
        "规范化日期": "",
        "规范化粒度": "",
        "分区字段": "",
        "分区键": "",
        "原始行": values.get("原始行", {}),
        "有效": True,
        "失败原因": "",
    }
    missing = []
    if not table:
        missing.append("目标表")
    if not raw_date:
        missing.append("日期")
    if not raw_granularity:
        missing.append("分区粒度")
    if missing:
        result["有效"] = False
        result["失败原因"] = "缺少必填字段：" + "、".join(missing)
        return result

    granularity = normalize_granularity(raw_granularity)
    if granularity is None:
        result["有效"] = False
        result["失败原因"] = (
            f"不支持的分区粒度「{raw_granularity}」，"
            "仅支持 day/month/hour/year（天/月/小时/年）"
        )
        return result

    normalized_date = normalize_date(raw_date, granularity)
    if normalized_date is None:
        result["有效"] = False
        result["失败原因"] = (
            f"日期「{raw_date}」与分区粒度「{granularity}」不匹配，"
            f"应形如 {GRANULARITY_EXPECT[granularity]}"
        )
        return result

    field = key_field or GRANULARITY_KEY_FIELDS[granularity]
    result.update({
        "规范化日期": normalized_date,
        "规范化粒度": granularity,
        "分区字段": field,
        "分区键": f"{field}={normalized_date}",
    })
    return result

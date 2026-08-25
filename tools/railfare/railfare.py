#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""解析中国铁路电子客票（报销凭证）并自动检测版式变化。

用法示例：
    bin/railfare parse --pdf "票据.pdf"
    bin/railfare parse --text "发票号码:... ￥174.00"
    bin/railfare parse --stdin --format text

--pdf 模式会调用项目内 bin/pdftext 提取 PDF 原生文本层，再执行解析；
解析本身只依赖 Python 标准库，不发起任何网络请求。

设计要点：
- 字段解析：用正则从全文提取关键字段，不依赖行顺序（PDF 提取的行序
  可能与视觉顺序不一致）。
- 结构校验：把每一行与预定义的行模板逐一匹配，报告缺失的必需模板、
  缺失的可选模板和无法识别的行，从而在铁路方改版式时自动报警。
- 交叉校验：开票日期与乘车日期先后关系、文件名中的金额/日期/站点
  与票面内容的一致性（文件校验收到的文件名可能不规范，仅作参考）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RailfareError(Exception):
    """可预期且适合直接展示给用户的错误。"""


# ---------------------------------------------------------------- 行模板


@dataclass(frozen=True)
class LineTemplate:
    template_id: str
    description: str
    regex: re.Pattern[str]
    required: bool


# 依据两张真实样本归纳的行级模板。required 为 True 表示该行属于
# 票面主体结构，缺失即视为版式变化或内容缺失（错误级别）；
# required 为 False 表示装饰性/辅助行，缺失只给警告。
TEMPLATES: tuple[LineTemplate, ...] = (
    LineTemplate(
        "invoice",
        "发票号码与开票日期行",
        re.compile(
            r"^发票号码\s*[:：]\s*\d{15,25}\s+开票日期\s*[:：]\s*\d{4}年\d{1,2}月\d{1,2}日$"
        ),
        True,
    ),
    LineTemplate(
        "stations",
        "中文站名与车次行（出发站 车次 到达站）",
        re.compile(
            r"^[\u4e00-\u9fa5]{2,6}\s+[A-Z]{1,2}\d{1,5}\s+[\u4e00-\u9fa5]{2,6}$"
        ),
        True,
    ),
    LineTemplate(
        "stations_en",
        "英文站名行",
        re.compile(r"^[A-Za-z][A-Za-z\s]{2,}$"),
        False,
    ),
    LineTemplate(
        "depart",
        "乘车日期、开车时间与座位行",
        re.compile(
            r"^\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}开\s+\d{1,2}车\d{1,3}[A-Z]?号$"
        ),
        True,
    ),
    LineTemplate(
        "price_label",
        "票价标签行",
        re.compile(r"^票价\s*[:：]?$"),
        False,
    ),
    LineTemplate(
        "seat_class",
        "座位等级行",
        re.compile(
            r"^(商务座|特等座|一等座|二等座|硬座|软座|硬卧|软卧|高级软卧|动卧|无座)$"
        ),
        False,
    ),
    LineTemplate(
        "passenger",
        "乘车人证件号（脱敏）与姓名行",
        re.compile(r"^\d{6,10}\*{4,8}\d{4}\s+[\u4e00-\u9fa5]{2,4}$"),
        True,
    ),
    LineTemplate(
        "ticket_no",
        "电子客票号行",
        re.compile(r"^电子客票号\s*[:：]\s*\d{15,30}$"),
        True,
    ),
    LineTemplate(
        "buyer",
        "购买方名称与统一社会信用代码行",
        re.compile(
            r"^购买方名称\s*[:：]\S+\s+统一社会信用代码\s*[:：][0-9A-Z]{15,20}$"
        ),
        True,
    ),
    LineTemplate(
        "footer_12306",
        "12306 提示行",
        re.compile(r"^买票请到12306\s+发货请到95306$"),
        False,
    ),
    LineTemplate(
        "footer_wish",
        "铁路祝福语行",
        re.compile(r"^中国铁路祝您旅途愉快$"),
        False,
    ),
    LineTemplate(
        "title",
        "票据标题行（电子发票（铁路电子客票））",
        re.compile(r"^电子发票（铁路电子客票）$"),
        True,
    ),
    LineTemplate(
        "amount",
        "票面金额行（￥xx.xx）",
        re.compile(r"^￥\d+\.\d{2}$"),
        True,
    ),
)

TEMPLATE_BY_ID = {t.template_id: t for t in TEMPLATES}

# ---------------------------------------------------------------- 字段正则

RE_INVOICE_NO = re.compile(r"发票号码\s*[:：]\s*(\d{15,25})")
RE_ISSUE_DATE = re.compile(r"开票日期\s*[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日")
RE_STATIONS = re.compile(
    r"^([\u4e00-\u9fa5]{2,6})\s+([A-Z]{1,2}\d{1,5})\s+([\u4e00-\u9fa5]{2,6})$",
    re.MULTILINE,
)
RE_RIDE_DATETIME = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})\s*开"
)
RE_SEAT = re.compile(r"(\d{1,2}车\d{1,3}[A-Z]?号)")
RE_CLASS = re.compile(
    r"(商务座|特等座|一等座|二等座|硬座|软座|硬卧|软卧|高级软卧|动卧|无座)"
)
RE_PASSENGER = re.compile(r"(\d{6,10}\*{4,8}\d{4})\s+([\u4e00-\u9fa5]{2,4})")
RE_TICKET_NO = re.compile(r"电子客票号\s*[:：]\s*(\d{15,30})")
RE_BUYER = re.compile(
    r"购买方名称\s*[:：]\s*(\S+?)\s+统一社会信用代码\s*[:：]\s*([0-9A-Z]{15,20})"
)
RE_AMOUNT = re.compile(r"￥\s*(\d+\.\d{2})")

# 文件名对照正则（只用于 --pdf 模式的辅助校验，匹配不到就跳过）
RE_FN_AMOUNT = re.compile(r"(\d+\.\d{2})\s*元")
RE_FN_DATE = re.compile(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})")
RE_FN_STATIONS = re.compile(r"([\u4e00-\u9fa5]{2,6})\s*-\s*([\u4e00-\u9fa5]{2,6})")


def patternize(line: str) -> str:
    """把一行内容归一化为模式串：数字变 ⟨N⟩、字母串变 ⟨X⟩、中文保留。

    用于向用户展示整篇凭证的“结构签名”，版式变化时逐行对比即可发现。
    """
    return re.sub(r"\d+", "⟨N⟩", re.sub(r"[A-Za-z]+", "⟨X⟩", line))


def normalize_date(year: str, month: str, day: str) -> str:
    """把"年/月/日"三段数字归一化为 YYYY-MM-DD；非法日期抛 ValueError。"""
    value = int(year), int(month), int(day)
    if not (1 <= value[1] <= 12) or not (1 <= value[2] <= 31):
        raise ValueError("日期超出合理范围")
    return f"{value[0]:04d}-{value[1]:02d}-{value[2]:02d}"


# ---------------------------------------------------------------- 解析


def parse_fields(text: str) -> tuple[dict[str, str], list[str]]:
    """从全文提取字段。返回 (字段字典, 字段级错误列表)。

    字段级正则不依赖行顺序，容忍 PDF 文本层行序与视觉顺序不一致。
    """
    fields: dict[str, str] = {}
    errors: list[str] = []

    def need(key: str, value: str | None, label: str) -> None:
        if value is None:
            errors.append(f"无法解析字段：{label}")
        else:
            fields[key] = value

    match = RE_INVOICE_NO.search(text)
    need("invoice_no", match.group(1) if match else None, "发票号码")

    match = RE_ISSUE_DATE.search(text)
    if match:
        try:
            fields["issue_date"] = normalize_date(*match.groups())
        except ValueError:
            errors.append("开票日期超出合理范围")
    else:
        errors.append("无法解析字段：开票日期")

    match = RE_STATIONS.search(text)
    if match:
        fields["departure_station"], fields["train_no"], fields["arrival_station"] = (
            match.group(1),
            match.group(2),
            match.group(3),
        )
    else:
        errors.append("无法解析字段：出发站/车次/到达站")

    match = RE_RIDE_DATETIME.search(text)
    if match:
        year, month, day, hour, minute = match.groups()
        try:
            fields["ride_date"] = normalize_date(year, month, day)
        except ValueError:
            errors.append("乘车日期超出合理范围")
        fields["departure_time"] = f"{int(hour):02d}:{minute}"
    else:
        errors.append("无法解析字段：乘车日期/开车时间")

    match = RE_SEAT.search(text)
    need("seat", match.group(1) if match else None, "座位号")

    match = RE_CLASS.search(text)
    if match:
        fields["seat_class"] = match.group(1)

    match = RE_PASSENGER.search(text)
    if match:
        fields["passport_masked"], fields["passenger"] = match.group(1), match.group(2)
    else:
        errors.append("无法解析字段：乘车人证件号/姓名")

    match = RE_TICKET_NO.search(text)
    need("ticket_no", match.group(1) if match else None, "电子客票号")

    match = RE_BUYER.search(text)
    if match:
        fields["buyer_name"], fields["buyer_tax_id"] = match.group(1), match.group(2)
    else:
        errors.append("无法解析字段：购买方名称/统一社会信用代码")

    match = RE_AMOUNT.search(text)
    need("amount", match.group(1) if match else None, "票面金额")

    return fields, errors


def check_structure(lines: list[str]) -> dict[str, Any]:
    """逐行与模板匹配，报告匹配情况、缺失模板与无法识别的行。"""
    matched: list[dict[str, Any]] = []
    unrecognized: list[dict[str, Any]] = []
    patterns: list[dict[str, Any]] = []
    for index, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        hit = next((t for t in TEMPLATES if t.regex.match(line)), None)
        if hit is not None:
            matched.append({"template": hit.template_id, "line": index})
        else:
            unrecognized.append({"line": index, "text": line})
        patterns.append({"line": index, "pattern": patternize(line)})

    matched_ids = {item["template"] for item in matched}
    return {
        "matched": matched,
        "missing_required": [
            {"template": t.template_id, "description": t.description}
            for t in TEMPLATES
            if t.required and t.template_id not in matched_ids
        ],
        "missing_optional": [
            {"template": t.template_id, "description": t.description}
            for t in TEMPLATES
            if not t.required and t.template_id not in matched_ids
        ],
        "unrecognized_lines": unrecognized,
        "line_patterns": patterns,
    }


def cross_checks(fields: dict[str, str], file_name: str | None) -> list[str]:
    """交叉校验，返回警告列表。"""
    warnings: list[str] = []

    issue_date = fields.get("issue_date")
    ride_date = fields.get("ride_date")
    if issue_date and ride_date and issue_date < ride_date:
        warnings.append(f"开票日期 {issue_date} 早于乘车日期 {ride_date}，请人工核对")

    if not file_name:
        return warnings

    match = RE_FN_AMOUNT.search(file_name)
    if match and fields.get("amount") and match.group(1) != fields["amount"]:
        warnings.append(
            f"文件名金额 {match.group(1)} 元与票面金额 {fields['amount']} 元不一致"
        )

    match = RE_FN_DATE.search(file_name)
    if match:
        try:
            file_date = normalize_date(match.group(1), match.group(2), match.group(3))
        except ValueError:
            file_date = None
        if file_date and ride_date and file_date != ride_date:
            warnings.append(f"文件名日期 {file_date} 与乘车日期 {ride_date} 不一致")

    match = RE_FN_STATIONS.search(file_name)
    if match and fields.get("departure_station"):
        file_dep, file_arr = match.group(1), match.group(2)
        if (
            file_dep != fields["departure_station"]
            or file_arr != fields["arrival_station"]
        ):
            warnings.append(
                f"文件名站点 {file_dep}-{file_arr} 与票面 "
                f"{fields['departure_station']}-{fields['arrival_station']} 不一致"
            )

    return warnings


def analyze(
    text: str, file_name: str | None = None, strict: bool = False
) -> dict[str, Any]:
    """解析全文并做结构校验与交叉校验，返回完整结果字典。"""
    lines = text.splitlines()
    fields, field_errors = parse_fields(text)
    structure = check_structure(lines)

    errors: list[str] = list(field_errors)
    errors.extend(
        f"版式变化或内容缺失：缺少必需行模板 {item['template']}（{item['description']}）"
        for item in structure["missing_required"]
    )

    warnings: list[str] = [
        f"无法识别的行（第 {item['line']} 行）：{item['text']}"
        for item in structure["unrecognized_lines"]
    ]
    warnings.extend(
        f"缺少可选行模板 {item['template']}（{item['description']}），请留意版式是否变化"
        for item in structure["missing_optional"]
    )
    warnings.extend(cross_checks(fields, file_name))

    if strict:
        errors.extend(warnings)
        warnings = []

    return {
        "schema_version": 1,
        "ok": not errors,
        "command": "parse",
        "fields": fields,
        "structure": structure,
        "warnings": warnings,
        "errors": errors,
    }


# ---------------------------------------------------------------- PDF 模式


def run_pdftext(pdf_path: str) -> str:
    """调用项目内 bin/pdftext 提取 PDF 第 1 页文本层。"""
    pdftext_bin = Path(__file__).resolve().parents[2] / "bin" / "pdftext"
    if not pdftext_bin.is_file():
        raise RailfareError(f"找不到 pdftext 入口：{pdftext_bin}")
    proc = subprocess.run(
        [str(pdftext_bin), "extract", pdf_path, "--format", "json", "--pages", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RailfareError(f"pdftext 提取失败（退出码 {proc.returncode}）：{detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RailfareError("pdftext 输出不是有效 JSON") from exc
    if not payload.get("ok"):
        raise RailfareError(f"pdftext 报告错误：{payload}")
    text = "".join(page.get("text", "") for page in payload.get("pages", []))
    if not text.strip():
        raise RailfareError("PDF 无原生文本层（可能是扫描件），请改用视觉读取")
    return text


# ---------------------------------------------------------------- 输出


def render_text(result: dict[str, Any]) -> str:
    """把结果渲染为人类可读文本。"""
    lines: list[str] = []
    fields = result["fields"]
    labels = [
        ("invoice_no", "发票号码"),
        ("issue_date", "开票日期"),
        ("train_no", "车次"),
        ("departure_station", "出发站"),
        ("arrival_station", "到达站"),
        ("ride_date", "乘车日期"),
        ("departure_time", "开车时间"),
        ("seat", "座位"),
        ("seat_class", "座位等级"),
        ("passenger", "乘车人"),
        ("passport_masked", "证件号（脱敏）"),
        ("ticket_no", "电子客票号"),
        ("buyer_name", "购买方名称"),
        ("buyer_tax_id", "统一社会信用代码"),
        ("amount", "票面金额（元）"),
    ]
    for key, label in labels:
        if key in fields:
            lines.append(f"{label:<14}: {fields[key]}")

    lines.append("")
    structure = result["structure"]
    lines.append(
        f"结构检查：匹配 {len(structure['matched'])} 个模板行，"
        f"无法识别 {len(structure['unrecognized_lines'])} 行"
    )
    for item in structure["line_patterns"]:
        lines.append(f"  第 {item['line']} 行  {item['pattern']}")

    for warning in result["warnings"]:
        lines.append(f"警告：{warning}")
    for error in result["errors"]:
        lines.append(f"错误：{error}")
    lines.append("结论：解析成功" if result["ok"] else "结论：解析失败，请人工复核")
    return "\n".join(lines)


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="railfare",
        description="解析中国铁路电子客票（报销凭证）并自动检测版式变化",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    parse = sub.add_parser("parse", help="解析高铁报销凭证")
    source = parse.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--pdf", metavar="PATH", help="PDF 报销凭证路径（调用 pdftext 提取文本）"
    )
    source.add_argument("--text", metavar="TEXT", help="直接传入已提取的文本")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取文本")
    parse.add_argument(
        "--format", choices=("json", "text"), default="json", help="输出格式"
    )
    parse.add_argument("--strict", action="store_true", help="把警告提升为错误")
    parse.add_argument(
        "--no-file-check",
        action="store_true",
        help="关闭文件名与票面内容的对照校验（仅 --pdf 模式有效）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command != "parse":
        return 2

    file_name: str | None = None
    try:
        if args.pdf:
            pdf_path = Path(args.pdf).expanduser()
            if not pdf_path.is_file():
                raise RailfareError(f"输入文件不存在：{pdf_path}")
            file_name = None if args.no_file_check else pdf_path.name
            text = run_pdftext(str(pdf_path))
        elif args.text is not None:
            text = args.text
        else:
            text = sys.stdin.read()

        result = analyze(text, file_name=file_name, strict=args.strict)
    except RailfareError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.format == "text":
        print(render_text(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())

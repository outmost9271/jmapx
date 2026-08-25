#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""使用 pypdfium2/PDFium 在本地提取 PDF 文本层。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from pypdfium2.version import PDFIUM_INFO, PYPDFIUM_INFO

DEFAULT_MAX_INPUT_MB = 200.0
DEFAULT_MAX_PAGES = 500
DEFAULT_MAX_PAGE_CHARS = 1_000_000
DEFAULT_MAX_TOTAL_CHARS = 5_000_000
DEFAULT_MAX_OUTPUT_MB = 50.0
MAX_PASSWORD_CHARS = 4096


class PdfTextError(Exception):
    """可预期且适合直接展示给用户的错误。"""


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是数字") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的有限数字")
    return parsed


def validate_input(path_value: str, max_input_mb: float) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise PdfTextError(f"输入文件不存在：{path}")
    if not path.is_file():
        raise PdfTextError(f"输入路径不是普通文件：{path}")

    size = path.stat().st_size
    if size == 0:
        raise PdfTextError("输入 PDF 是空文件")
    if size > max_input_mb * 1024 * 1024:
        raise PdfTextError(
            f"输入文件为 {size / 1024 / 1024:.2f} MiB，超过 {max_input_mb:g} MiB 限制"
        )

    try:
        with path.open("rb") as stream:
            header = stream.read(1024)
    except OSError as exc:
        raise PdfTextError(f"无法读取输入文件：{path}") from exc
    if b"%PDF-" not in header:
        raise PdfTextError("输入文件头中未发现 PDF 标识")
    return path


def validate_password(value: str, source: str) -> str:
    if len(value) > MAX_PASSWORD_CHARS:
        raise PdfTextError(f"{source}中的 PDF 密码超过 {MAX_PASSWORD_CHARS} 个字符限制")
    return value


def resolve_password(args: argparse.Namespace) -> str | None:
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env)
        if value is None:
            raise PdfTextError(f"环境变量 {args.password_env} 未设置")
        return validate_password(value, "环境变量")

    if getattr(args, "password_file", None):
        password_path = Path(args.password_file).expanduser()
        try:
            if not password_path.is_file():
                raise PdfTextError(f"密码路径不是普通文件：{password_path}")
            if (
                os.name == "posix"
                and stat.S_IMODE(password_path.stat().st_mode) & 0o077
            ):
                raise PdfTextError(
                    "密码文件权限必须禁止组用户和其他用户访问，建议设置为 600 或 400"
                )
            with password_path.open(encoding="utf-8") as stream:
                value = stream.readline(MAX_PASSWORD_CHARS + 2).rstrip("\r\n")
            return validate_password(value, "密码文件")
        except PdfTextError:
            raise
        except (OSError, UnicodeError) as exc:
            raise PdfTextError(f"无法读取密码文件：{password_path}") from exc

    value = os.environ.get("PDF_PASSWORD")
    if value is None:
        return None
    return validate_password(value, "环境变量 PDF_PASSWORD")


def open_document(path: Path, password: str | None) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(path, password=password)
    except pdfium.PdfiumError as exc:
        message = str(exc)
        if "password" in message.lower():
            if password is None:
                raise PdfTextError(
                    "PDF 已加密；请通过 PDF_PASSWORD、--password-env 或 --password-file 提供密码"
                ) from exc
            raise PdfTextError("PDF 密码不正确") from exc
        raise PdfTextError(f"PDFium 无法打开 PDF：{message}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_record(path: Path) -> dict[str, Any]:
    return {
        "file": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def version_record() -> dict[str, Any]:
    return {
        "pypdfium2": PYPDFIUM_INFO.tag,
        "pdfium_build": PDFIUM_INFO.build,
    }


def parse_page_selection(value: str, page_count: int) -> list[int]:
    selection = value.strip().lower()
    if selection == "all":
        return list(range(1, page_count + 1))
    if selection == "odd":
        return list(range(1, page_count + 1, 2))
    if selection == "even":
        return list(range(2, page_count + 1, 2))
    if not selection:
        raise PdfTextError("页码选择不能为空")

    pages: list[int] = []
    seen: set[int] = set()
    for token in selection.split(","):
        token = token.strip()
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise PdfTextError(f"无效页码表达式：{token!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise PdfTextError(f"无效页码范围：{token!r}")
        if end > page_count:
            raise PdfTextError(f"页码 {end} 超出 PDF 总页数 {page_count}")
        for page_number in range(start, end + 1):
            if page_number not in seen:
                pages.append(page_number)
                seen.add(page_number)
    return pages


def parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        values = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise PdfTextError("--bbox 应为 left,bottom,right,top") from exc
    if len(values) != 4 or not all(math.isfinite(item) for item in values):
        raise PdfTextError("--bbox 应包含四个有限数字：left,bottom,right,top")
    left, bottom, right, top = values
    if not (left < right and bottom < top):
        raise PdfTextError("--bbox 必须满足 left < right 且 bottom < top")
    return left, bottom, right, top


def rounded_box(box: tuple[float, float, float, float]) -> list[float]:
    return [round(float(value), 3) for value in box]


def page_geometry(page: Any, page_number: int) -> dict[str, Any]:
    width, height = page.get_size()
    bbox = page.get_bbox()
    return {
        "page": page_number,
        "width_points": round(float(width), 3),
        "height_points": round(float(height), 3),
        "rotation": int(page.get_rotation()),
        "page_bbox_points": rounded_box(bbox),
    }


def count_page_text(document: pdfium.PdfDocument, page_number: int) -> dict[str, Any]:
    page = document[page_number - 1]
    textpage = None
    try:
        record = page_geometry(page, page_number)
        textpage = page.get_textpage()
        char_count = int(textpage.count_chars())
        record.update(
            {
                "pdfium_char_count": char_count,
                "has_text": char_count > 0,
            }
        )
        return record
    except pdfium.PdfiumError as exc:
        raise PdfTextError(f"检查第 {page_number} 页文本层失败：{exc}") from exc
    finally:
        if textpage is not None:
            textpage.close()
        page.close()


def normalize_text_with_stats(
    text: str, trim: bool, collapse_blank_lines: bool
) -> tuple[str, int]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    safe_characters: list[str] = []
    removed_controls = 0
    for character in normalized:
        codepoint = ord(character)
        if character in ("\n", "\t") or (
            codepoint >= 32 and not 127 <= codepoint <= 159
        ):
            safe_characters.append(character)
        else:
            removed_controls += 1
    normalized = "".join(safe_characters)
    if collapse_blank_lines:
        normalized = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", normalized)
    if trim:
        normalized = normalized.strip()
    return normalized, removed_controls


def normalize_text(text: str, trim: bool, collapse_blank_lines: bool) -> str:
    return normalize_text_with_stats(text, trim, collapse_blank_lines)[0]


def extract_page_text(
    document: pdfium.PdfDocument,
    page_number: int,
    *,
    method: str,
    bbox: tuple[float, float, float, float] | None,
    decode_errors: str,
    trim: bool,
    collapse_blank_lines: bool,
    max_page_chars: int,
) -> dict[str, Any]:
    page = document[page_number - 1]
    textpage = None
    try:
        record = page_geometry(page, page_number)
        textpage = page.get_textpage()
        char_count = int(textpage.count_chars())
        if char_count > max_page_chars:
            raise PdfTextError(
                f"第 {page_number} 页包含 {char_count} 个 PDFium 字符，超过 {max_page_chars} 个限制"
            )

        if method == "range":
            text = textpage.get_text_range(errors=decode_errors)
            extraction_bbox = None
        else:
            if bbox is None:
                text = textpage.get_text_bounded(errors=decode_errors)
                extraction_bbox = record["page_bbox_points"]
            else:
                text = textpage.get_text_bounded(*bbox, errors=decode_errors)
                extraction_bbox = rounded_box(bbox)

        text, removed_controls = normalize_text_with_stats(
            text, trim, collapse_blank_lines
        )
        record.update(
            {
                "pdfium_char_count": char_count,
                "text_char_count": len(text),
                "replacement_character_count": text.count("\ufffd"),
                "removed_control_character_count": removed_controls,
                "has_text": bool(text),
                "extraction_method": method,
                "extraction_bbox_points": extraction_bbox,
                "text": text,
            }
        )
        return record
    except PdfTextError:
        raise
    except (pdfium.PdfiumError, UnicodeError) as exc:
        raise PdfTextError(f"提取第 {page_number} 页文本失败：{exc}") from exc
    finally:
        if textpage is not None:
            textpage.close()
        page.close()


def enforce_document_limits(document: pdfium.PdfDocument, max_pages: int) -> int:
    page_count = len(document)
    if page_count == 0:
        raise PdfTextError("PDF 不包含页面")
    if page_count > max_pages:
        raise PdfTextError(f"PDF 共 {page_count} 页，超过 {max_pages} 页限制")
    return page_count


def base_payload(
    path: Path, command: str, page_count: int, pages: list[int]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": True,
        "command": command,
        "backend": "pypdfium2/pdfium",
        "versions": version_record(),
        "source": source_record(path),
        "page_count": page_count,
        "selected_pages": pages,
    }


def emit_json(payload: dict[str, Any], *, stream: Any | None = None) -> None:
    destination = sys.stdout if stream is None else stream
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=destination)


def inspect_pdf(args: argparse.Namespace) -> int:
    path = validate_input(args.pdf, args.max_input_mb)
    password = resolve_password(args)
    with open_document(path, password) as document:
        page_count = enforce_document_limits(document, args.max_pages)
        selected_pages = parse_page_selection(args.pages, page_count)
        records = [count_page_text(document, page) for page in selected_pages]

    payload = base_payload(path, "inspect", page_count, selected_pages)
    total_chars = sum(record["pdfium_char_count"] for record in records)
    payload.update(
        {
            "total_pdfium_char_count": total_chars,
            "textless_pages": [
                record["page"] for record in records if not record["has_text"]
            ],
            "pages": records,
        }
    )
    emit_json(payload)
    return 0


def format_plain_text(records: list[dict[str, Any]], page_markers: bool) -> str:
    if page_markers:
        chunks = [
            f"===== PDF 第 {record['page']} 页 =====\n{record['text']}"
            for record in records
        ]
        output = "\n\n".join(chunks)
    else:
        output = "\n\f\n".join(record["text"] for record in records)
    if not output.endswith("\n"):
        output += "\n"
    return output


def format_json_payload(payload: dict[str, Any], compact: bool) -> str:
    if compact:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def format_json_lines(payload: dict[str, Any]) -> str:
    document_record = {key: value for key, value in payload.items() if key != "pages"}
    document_record["type"] = "document"
    lines = [json.dumps(document_record, ensure_ascii=False, separators=(",", ":"))]
    for page in payload["pages"]:
        page_record = {"type": "page", **page}
        lines.append(json.dumps(page_record, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n"


def prepare_output_path(path_value: str, source: Path, overwrite: bool) -> Path:
    requested = Path(path_value).expanduser()
    if requested.exists() and requested.is_symlink():
        raise PdfTextError(f"拒绝写入符号链接：{requested}")
    target = requested.resolve()
    if target == source:
        raise PdfTextError("输出路径不能与输入 PDF 相同")
    if target.exists() and not target.is_file():
        raise PdfTextError(f"输出路径不是普通文件：{target}")
    if target.exists() and not overwrite:
        raise PdfTextError(f"输出文件已存在；如需覆盖请使用 --overwrite：{target}")
    return target


def atomic_write_text(target: Path, content: str, overwrite: bool) -> None:
    parent_existed = target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        target.parent.chmod(0o700)

    if target.exists() and not overwrite:
        raise PdfTextError(f"输出文件已存在；如需覆盖请使用 --overwrite：{target}")

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists() and not overwrite:
            raise PdfTextError(f"输出文件已存在；如需覆盖请使用 --overwrite：{target}")
        os.replace(temporary, target)
        target.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def extract_pdf(args: argparse.Namespace) -> int:
    path = validate_input(args.pdf, args.max_input_mb)
    bbox = parse_bbox(args.bbox)
    if args.method == "range" and bbox is not None:
        raise PdfTextError("--bbox 只能与 --method bounded 一起使用")

    password = resolve_password(args)
    with open_document(path, password) as document:
        page_count = enforce_document_limits(document, args.max_pages)
        selected_pages = parse_page_selection(args.pages, page_count)
        records: list[dict[str, Any]] = []
        total_pdfium_chars = 0
        for page_number in selected_pages:
            record = extract_page_text(
                document,
                page_number,
                method=args.method,
                bbox=bbox,
                decode_errors=args.decode_errors,
                trim=args.trim,
                collapse_blank_lines=args.collapse_blank_lines,
                max_page_chars=args.max_page_chars,
            )
            total_pdfium_chars += record["pdfium_char_count"]
            if total_pdfium_chars > args.max_total_chars:
                raise PdfTextError(
                    f"所选页面累计包含 {total_pdfium_chars} 个 PDFium 字符，"
                    f"超过 {args.max_total_chars} 个限制"
                )
            records.append(record)

    textless_pages = [record["page"] for record in records if not record["has_text"]]
    if args.fail_on_empty and textless_pages:
        pages = ",".join(str(page) for page in textless_pages)
        raise PdfTextError(f"以下页面未提取到文本：{pages}")

    payload = base_payload(path, "extract", page_count, selected_pages)
    payload.update(
        {
            "method": args.method,
            "requested_bbox_points": rounded_box(bbox) if bbox else None,
            "total_pdfium_char_count": total_pdfium_chars,
            "total_text_char_count": sum(
                record["text_char_count"] for record in records
            ),
            "replacement_character_count": sum(
                record["replacement_character_count"] for record in records
            ),
            "removed_control_character_count": sum(
                record["removed_control_character_count"] for record in records
            ),
            "textless_pages": textless_pages,
            "pages": records,
        }
    )

    if args.format == "text":
        content = format_plain_text(records, args.page_markers)
    elif args.format == "json":
        content = format_json_payload(payload, args.compact)
    else:
        content = format_json_lines(payload)

    output_bytes = len(content.encode("utf-8"))
    if output_bytes > args.max_output_mb * 1024 * 1024:
        raise PdfTextError(
            f"输出预计为 {output_bytes / 1024 / 1024:.2f} MiB，"
            f"超过 {args.max_output_mb:g} MiB 限制"
        )

    if args.output and args.output != "-":
        target = prepare_output_path(args.output, path, args.overwrite)
        atomic_write_text(target, content, args.overwrite)
        emit_json(
            {
                "ok": True,
                "command": "extract",
                "format": args.format,
                "output": str(target),
                "size_bytes": output_bytes,
                "selected_pages": selected_pages,
                "textless_pages": textless_pages,
                "source_sha256": payload["source"]["sha256"],
            }
        )
    else:
        sys.stdout.write(content)
    return 0


def add_password_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--password-env", metavar="NAME", help="从指定环境变量读取 PDF 密码"
    )
    group.add_argument(
        "--password-file",
        metavar="PATH",
        help="从权限为 600 或 400 的文件首行读取 PDF 密码",
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pdf", help="输入 PDF 文件")
    parser.add_argument("--pages", default="all", help="all、odd、even、1 或 1,3-5")
    parser.add_argument(
        "--max-input-mb", type=positive_float, default=DEFAULT_MAX_INPUT_MB
    )
    parser.add_argument("--max-pages", type=positive_int, default=DEFAULT_MAX_PAGES)
    add_password_arguments(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdftext",
        description="使用 pypdfium2/PDFium 在本地检查并提取 PDF 文本层",
    )
    parser.add_argument("--debug", action="store_true", help="错误时输出本地调试堆栈")
    subparsers = parser.add_subparsers(dest="command")

    inspect_parser = subparsers.add_parser("inspect", help="检查页数和每页文本字符数量")
    add_common_arguments(inspect_parser)

    extract_parser = subparsers.add_parser("extract", help="提取指定页面的文本")
    add_common_arguments(extract_parser)
    extract_parser.add_argument(
        "--format", choices=("text", "json", "jsonl"), default="text"
    )
    extract_parser.add_argument(
        "--method",
        choices=("bounded", "range"),
        default="bounded",
        help="bounded 支持完整 Unicode 和区域提取；range 用于兼容性对照",
    )
    extract_parser.add_argument(
        "--bbox",
        metavar="L,B,R,T",
        help="PDF 坐标区域，单位为点，原点位于左下角；仅适用于 bounded",
    )
    extract_parser.add_argument(
        "--decode-errors",
        choices=("strict", "replace", "ignore"),
        default="replace",
        help="Unicode 解码错误处理方式，默认以替换字符显式标记",
    )
    extract_parser.add_argument("--trim", action="store_true", help="去除每页首尾空白")
    extract_parser.add_argument(
        "--collapse-blank-lines",
        action="store_true",
        help="将连续空白行压缩为一个空白行",
    )
    extract_parser.add_argument(
        "--page-markers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="文本格式中加入页码标记；关闭后使用换页符分隔",
    )
    extract_parser.add_argument("--compact", action="store_true", help="压缩 JSON 输出")
    extract_parser.add_argument(
        "--output", metavar="PATH", help="原子写入文件；使用 - 表示标准输出"
    )
    extract_parser.add_argument(
        "--overwrite", action="store_true", help="允许覆盖已有输出文件"
    )
    extract_parser.add_argument(
        "--fail-on-empty", action="store_true", help="任一所选页面没有文本时返回失败"
    )
    extract_parser.add_argument(
        "--max-page-chars", type=positive_int, default=DEFAULT_MAX_PAGE_CHARS
    )
    extract_parser.add_argument(
        "--max-total-chars", type=positive_int, default=DEFAULT_MAX_TOTAL_CHARS
    )
    extract_parser.add_argument(
        "--max-output-mb", type=positive_float, default=DEFAULT_MAX_OUTPUT_MB
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "inspect":
            return inspect_pdf(args)
        return extract_pdf(args)
    except BrokenPipeError:
        return 0
    except PdfTextError as exc:
        emit_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转换未知异常
        emit_json({"ok": False, "error": f"未预期错误：{exc}"}, stream=sys.stderr)
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

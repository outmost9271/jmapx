# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import pdf_to_text as tool


def escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def create_pdf(
    path: Path, pages: list[list[str]], rotations: dict[int, int] | None = None
) -> None:
    rotations = rotations or {}
    page_count = len(pages)
    font_number = 3 + page_count * 2
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Count {page_count} /Kids "
            f"[{' '.join(f'{3 + index * 2} 0 R' for index in range(page_count))}] >>"
        ).encode("ascii"),
        font_number: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }

    for index, lines in enumerate(pages):
        page_number = 3 + index * 2
        content_number = page_number + 1
        rotation = rotations.get(index + 1)
        rotation_entry = f" /Rotate {rotation}" if rotation is not None else ""
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            f"{rotation_entry} /Resources << /Font << /F1 {font_number} 0 R >> >> "
            f"/Contents {content_number} 0 R >>"
        ).encode("ascii")

        if lines:
            operators = ["BT", "/F1 18 Tf", "72 720 Td"]
            for line_index, line in enumerate(lines):
                if line_index:
                    operators.append("0 -36 Td")
                operators.append(f"({escape_pdf_text(line)}) Tj")
            operators.append("ET")
            stream = "\n".join(operators).encode("ascii")
        else:
            stream = b"q\nQ"
        objects[content_number] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number in range(1, font_number + 1):
        offsets.append(len(body))
        body.extend(f"{object_number} 0 obj\n".encode("ascii"))
        body.extend(objects[object_number])
        body.extend(b"\nendobj\n")

    xref_offset = len(body)
    body.extend(f"xref\n0 {font_number + 1}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(
        (
            f"trailer\n<< /Size {font_number + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(body)


class PdfToTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "sample.pdf"
        create_pdf(
            self.pdf,
            [
                ["First page", "Amount 123.45"],
                ["Second page", "Date 2026-08-19"],
                ["Third page"],
            ],
            rotations={3: 90},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = tool.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_json(self, arguments: list[str]) -> tuple[int, dict[str, object], str]:
        code, stdout, stderr = self.invoke(arguments)
        content = stdout if code == 0 else stderr
        return code, json.loads(content), stderr

    def test_selection_bbox_and_normalization_helpers(self) -> None:
        self.assertEqual(tool.parse_page_selection("1,3-4,3", 4), [1, 3, 4])
        self.assertEqual(tool.parse_page_selection("odd", 5), [1, 3, 5])
        self.assertEqual(tool.parse_page_selection("even", 5), [2, 4])
        self.assertEqual(tool.parse_bbox("1,2,3,4"), (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(
            tool.normalize_text("a\r\n\r\nb\x00\x1b", False, False), "a\n\nb"
        )
        self.assertEqual(
            tool.normalize_text_with_stats("a\x00\x1b\t", False, False),
            ("a\t", 2),
        )
        self.assertEqual(tool.normalize_text("\n a\n\n\n b \n", True, True), "a\n\n b")
        with self.assertRaises(tool.PdfTextError):
            tool.parse_page_selection("4-2", 4)
        with self.assertRaises(tool.PdfTextError):
            tool.parse_bbox("0,4,3,2")

    def test_inspect_reports_text_counts_geometry_and_rotation(self) -> None:
        code, payload, _ = self.invoke_json(["inspect", str(self.pdf)])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backend"], "pypdfium2/pdfium")
        self.assertEqual(payload["page_count"], 3)
        self.assertEqual(payload["selected_pages"], [1, 2, 3])
        self.assertGreater(payload["total_pdfium_char_count"], 0)
        self.assertEqual(payload["textless_pages"], [])
        pages = payload["pages"]
        self.assertEqual(pages[0]["width_points"], 612.0)
        self.assertEqual(pages[2]["rotation"], 90)
        self.assertTrue(all(page["has_text"] for page in pages))

    def test_plain_text_respects_selection_and_markers(self) -> None:
        code, stdout, stderr = self.invoke(
            ["extract", str(self.pdf), "--pages", "2", "--format", "text"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("===== PDF 第 2 页 =====", stdout)
        self.assertIn("Second page", stdout)
        self.assertNotIn("First page", stdout)

        code, stdout, stderr = self.invoke(
            [
                "extract",
                str(self.pdf),
                "--pages",
                "1,3",
                "--format",
                "text",
                "--no-page-markers",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("\f", stdout)
        self.assertNotIn("=====", stdout)

    def test_json_and_range_methods(self) -> None:
        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--pages", "1", "--format", "json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["method"], "bounded")
        self.assertIn("Amount 123.45", payload["pages"][0]["text"])
        self.assertEqual(payload["replacement_character_count"], 0)
        self.assertEqual(payload["removed_control_character_count"], 0)
        self.assertEqual(payload["textless_pages"], [])

        code, payload, _ = self.invoke_json(
            [
                "extract",
                str(self.pdf),
                "--pages",
                "1",
                "--format",
                "json",
                "--method",
                "range",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["method"], "range")
        self.assertIn("First page", payload["pages"][0]["text"])
        self.assertIsNone(payload["pages"][0]["extraction_bbox_points"])

    def test_bounded_region_extracts_only_matching_line(self) -> None:
        code, payload, stderr = self.invoke_json(
            [
                "extract",
                str(self.pdf),
                "--pages",
                "1",
                "--format",
                "json",
                "--bbox",
                "0,700,612,750",
            ]
        )
        self.assertEqual(code, 0, stderr)
        text = payload["pages"][0]["text"]
        self.assertIn("First page", text)
        self.assertNotIn("Amount 123.45", text)
        self.assertEqual(payload["requested_bbox_points"], [0.0, 700.0, 612.0, 750.0])

    def test_json_lines_contains_document_and_page_records(self) -> None:
        code, stdout, stderr = self.invoke(
            ["extract", str(self.pdf), "--pages", "2-3", "--format", "jsonl"]
        )
        self.assertEqual(code, 0, stderr)
        records = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(
            [record["type"] for record in records], ["document", "page", "page"]
        )
        self.assertEqual(records[1]["page"], 2)
        self.assertEqual(records[2]["page"], 3)

    def test_output_is_private_atomic_and_requires_overwrite(self) -> None:
        output = self.root / "private" / "result.json"
        code, payload, _ = self.invoke_json(
            [
                "extract",
                str(self.pdf),
                "--format",
                "json",
                "--output",
                str(output),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(Path(payload["output"]), output)
        self.assertTrue(output.is_file())
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["page_count"], 3)
        if os.name == "posix":
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)

        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--output", str(output)]
        )
        self.assertEqual(code, 1)
        self.assertIn("已存在", payload["error"])

        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--output", str(output), "--overwrite"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["format"], "text")

    def test_textless_page_and_fail_on_empty(self) -> None:
        blank = self.root / "blank.pdf"
        create_pdf(blank, [[]])
        code, payload, _ = self.invoke_json(["inspect", str(blank)])
        self.assertEqual(code, 0)
        self.assertEqual(payload["textless_pages"], [1])

        code, payload, _ = self.invoke_json(
            ["extract", str(blank), "--format", "json", "--fail-on-empty"]
        )
        self.assertEqual(code, 1)
        self.assertIn("未提取到文本", payload["error"])

    def test_resource_and_argument_limits(self) -> None:
        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--max-page-chars", "1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("超过", payload["error"])

        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--max-total-chars", "1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("累计", payload["error"])

        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--max-output-mb", "0.000001"]
        )
        self.assertEqual(code, 1)
        self.assertIn("输出预计", payload["error"])

        code, payload, _ = self.invoke_json(
            ["inspect", str(self.pdf), "--max-pages", "2"]
        )
        self.assertEqual(code, 1)
        self.assertIn("超过", payload["error"])

        code, payload, _ = self.invoke_json(
            ["inspect", str(self.pdf), "--max-input-mb", "0.000001"]
        )
        self.assertEqual(code, 1)
        self.assertIn("输入文件", payload["error"])

        code, payload, _ = self.invoke_json(["extract", str(self.pdf), "--pages", "4"])
        self.assertEqual(code, 1)
        self.assertIn("超出", payload["error"])

        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--method", "range", "--bbox", "0,0,1,1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("只能", payload["error"])

    def test_password_sources_and_file_permissions(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            code, payload, _ = self.invoke_json(
                ["inspect", str(self.pdf), "--password-env", "MISSING_PDF_PASSWORD"]
            )
        self.assertEqual(code, 1)
        self.assertIn("未设置", payload["error"])

        password_file = self.root / "password"
        password_file.write_text("test-password\n", encoding="utf-8")
        if os.name == "posix":
            password_file.chmod(0o644)
            code, payload, _ = self.invoke_json(
                ["inspect", str(self.pdf), "--password-file", str(password_file)]
            )
            self.assertEqual(code, 1)
            self.assertIn("权限", payload["error"])
            password_file.chmod(0o600)

        code, payload, _ = self.invoke_json(
            ["inspect", str(self.pdf), "--password-file", str(password_file)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_invalid_and_empty_inputs_are_rejected(self) -> None:
        empty = self.root / "empty.pdf"
        empty.touch()
        code, payload, _ = self.invoke_json(["inspect", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("空文件", payload["error"])

        invalid = self.root / "invalid.pdf"
        invalid.write_text("not a pdf", encoding="utf-8")
        code, payload, _ = self.invoke_json(["inspect", str(invalid)])
        self.assertEqual(code, 1)
        self.assertIn("PDF 标识", payload["error"])

    def test_output_cannot_replace_source_or_follow_symlink(self) -> None:
        code, payload, _ = self.invoke_json(
            ["extract", str(self.pdf), "--output", str(self.pdf), "--overwrite"]
        )
        self.assertEqual(code, 1)
        self.assertIn("输入 PDF 相同", payload["error"])

        if os.name == "posix":
            target = self.root / "target.txt"
            target.write_text("existing", encoding="utf-8")
            link = self.root / "linked.txt"
            link.symlink_to(target)
            code, payload, _ = self.invoke_json(
                ["extract", str(self.pdf), "--output", str(link), "--overwrite"]
            )
            self.assertEqual(code, 1)
            self.assertIn("符号链接", payload["error"])


if __name__ == "__main__":
    unittest.main()

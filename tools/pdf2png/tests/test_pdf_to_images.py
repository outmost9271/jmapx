# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pymupdf
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf_to_images as tool


class PdfToImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pdf2png-tests-")
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "sample.pdf"
        document = pymupdf.open()
        page = document.new_page(width=200, height=300)
        page.insert_text((20, 40), "Synthetic page one - 123.45")
        page.draw_rect(pymupdf.Rect(15, 55, 180, 250), color=(0, 0, 1), width=2)
        page = document.new_page(width=300, height=180)
        page.insert_text((20, 40), "Synthetic page two - 2026-08-25")
        page.draw_circle((150, 100), 45, color=(1, 0, 0), width=2)
        document.save(self.pdf)
        document.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, arguments: list[str]) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = tool.main(arguments)
        if code == 0:
            payload = json.loads(stdout.getvalue())
        else:
            payload = json.loads(stderr.getvalue())
        return code, payload, stderr.getvalue()

    def test_page_selection(self) -> None:
        self.assertEqual(tool.parse_page_selection("all", 5), [1, 2, 3, 4, 5])
        self.assertEqual(tool.parse_page_selection("odd", 5), [1, 3, 5])
        self.assertEqual(tool.parse_page_selection("even", 5), [2, 4])
        self.assertEqual(tool.parse_page_selection("3,1-2,3", 5), [3, 1, 2])
        with self.assertRaises(tool.PdfToImagesError):
            tool.parse_page_selection("0", 5)
        with self.assertRaises(tool.PdfToImagesError):
            tool.parse_page_selection("2-8", 5)

    def test_color_crop_and_tile_helpers(self) -> None:
        self.assertEqual(tool.parse_background("#112233"), (17, 34, 51))
        self.assertEqual(tool.parse_background("1, 2, 3"), (1, 2, 3))
        self.assertEqual(tool.parse_crop_percent("0,50,100,100"), (0, 50, 100, 100))
        self.assertEqual(tool.tile_positions(200, 120, 20), [0, 80])
        self.assertEqual(tool.tile_positions(300, 120, 20), [0, 100, 180])
        self.assertLessEqual(
            len(tool.safe_prefix(self.pdf, "票" * 200).encode("utf-8")), 120
        )

    def test_inspect_reports_pages_and_dimensions(self) -> None:
        code, payload, _ = self.invoke(["inspect", str(self.pdf), "--dpi", "72"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backend"], "pymupdf")
        self.assertEqual(payload["page_count"], 2)
        self.assertEqual(payload["pages"][0]["estimated_pixel_width"], 200)
        self.assertEqual(payload["pages"][0]["estimated_pixel_height"], 300)
        self.assertEqual(len(payload["source_sha256"]), 64)

    def test_render_separate_and_manifest(self) -> None:
        output = self.root / "separate"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(output),
                "--mode",
                "separate",
                "--dpi",
                "72",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["selected_pages"], [1, 2])
        self.assertEqual(len(payload["outputs"]), 2)
        self.assertTrue(Path(payload["manifest"]).exists())
        with Image.open(payload["outputs"][0]["file"]) as image:
            self.assertEqual(image.size, (200, 300))
        with Image.open(payload["outputs"][1]["file"]) as image:
            self.assertEqual(image.size, (300, 180))
        if os.name == "posix":
            mode = stat.S_IMODE(Path(payload["outputs"][0]["file"]).stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_single_page_and_crop(self) -> None:
        output = self.root / "crop"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(output),
                "--pages",
                "1",
                "--dpi",
                "72",
                "--crop-percent",
                "0,50,100,100",
                "--no-manifest",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["selected_pages"], [1])
        with Image.open(payload["outputs"][0]["file"]) as image:
            self.assertEqual(image.size, (200, 150))

    def test_vertical_and_vertical_chunks(self) -> None:
        vertical = self.root / "vertical"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(vertical),
                "--mode",
                "vertical",
                "--dpi",
                "72",
                "--gap",
                "10",
                "--no-page-labels",
                "--no-manifest",
            ]
        )
        self.assertEqual(code, 0)
        with Image.open(payload["outputs"][0]["file"]) as image:
            self.assertEqual(image.size, (300, 490))

        chunks = self.root / "chunks"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(chunks),
                "--mode",
                "vertical-chunks",
                "--chunk-pages",
                "1",
                "--dpi",
                "72",
                "--no-manifest",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["outputs"]), 2)
        self.assertEqual(payload["outputs"][0]["pages"], [1])
        self.assertEqual(payload["outputs"][1]["pages"], [2])

        grayscale = self.root / "grayscale"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(grayscale),
                "--mode",
                "vertical",
                "--pages",
                "1-2",
                "--dpi",
                "72",
                "--grayscale",
                "--no-manifest",
            ]
        )
        self.assertEqual(code, 0)
        with Image.open(payload["outputs"][0]["file"]) as image:
            self.assertEqual(image.mode, "L")

    def test_grid_and_tiles(self) -> None:
        grid = self.root / "grid"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(grid),
                "--mode",
                "grid",
                "--dpi",
                "72",
                "--grid-columns",
                "2",
                "--grid-cell-width",
                "100",
                "--grid-cell-height",
                "120",
                "--no-manifest",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["outputs"]), 1)
        self.assertEqual(len(payload["outputs"][0]["page_regions"]), 2)

        tiles = self.root / "tiles"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(tiles),
                "--pages",
                "1",
                "--mode",
                "tiles",
                "--dpi",
                "72",
                "--tile-width",
                "120",
                "--tile-height",
                "120",
                "--tile-overlap",
                "20",
                "--no-manifest",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["outputs"]), 6)
        self.assertEqual(
            payload["outputs"][-1]["source_pixel_bbox"], [80, 180, 200, 300]
        )

    def test_pdfium_backend(self) -> None:
        output = self.root / "pdfium"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(output),
                "--pages",
                "2",
                "--backend",
                "pdfium",
                "--dpi",
                "72",
                "--no-manifest",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["backend"], "pdfium")
        with Image.open(payload["outputs"][0]["file"]) as image:
            self.assertEqual(image.size, (300, 180))

    def test_existing_output_requires_overwrite(self) -> None:
        output = self.root / "existing"
        arguments = [
            "render",
            str(self.pdf),
            "--output-dir",
            str(output),
            "--pages",
            "1",
            "--dpi",
            "72",
            "--no-manifest",
        ]
        self.assertEqual(self.invoke(arguments)[0], 0)
        code, payload, _ = self.invoke(arguments)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("已存在", payload["error"])
        self.assertEqual(self.invoke([*arguments, "--overwrite"])[0], 0)

    def test_megapixel_limit_fails_before_output(self) -> None:
        output = self.root / "limited"
        code, payload, _ = self.invoke(
            [
                "render",
                str(self.pdf),
                "--output-dir",
                str(output),
                "--dpi",
                "72",
                "--max-page-megapixels",
                "0.01",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("超过", payload["error"])
        self.assertEqual(list(output.glob("*.png")), [])

    def test_non_pdf_is_rejected(self) -> None:
        not_pdf = self.root / "not-a-pdf.pdf"
        not_pdf.write_text("not a PDF", encoding="utf-8")
        code, payload, _ = self.invoke(["inspect", str(not_pdf)])
        self.assertEqual(code, 1)
        self.assertIn("PDF 标识", payload["error"])

    def test_encrypted_pdf_uses_password_environment(self) -> None:
        encrypted = self.root / "encrypted.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "No sensitive content")
        document.save(
            encrypted,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner-password",
            user_pw="user-password",
        )
        document.close()

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PDF_PASSWORD", None)
            code, payload, _ = self.invoke(["inspect", str(encrypted)])
            self.assertEqual(code, 1)
            self.assertIn("加密", payload["error"])

        password_file = self.root / "pdf-password"
        password_file.write_text("user-password\n", encoding="utf-8")
        if os.name == "posix":
            password_file.chmod(0o644)
            code, payload, _ = self.invoke(
                ["inspect", str(encrypted), "--password-file", str(password_file)]
            )
            self.assertEqual(code, 1)
            self.assertIn("权限", payload["error"])
            password_file.chmod(0o600)

        code, payload, _ = self.invoke(
            ["inspect", str(encrypted), "--password-file", str(password_file)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["encrypted"])

        with mock.patch.dict(os.environ, {"TEST_PDF_PASSWORD": "user-password"}):
            code, payload, _ = self.invoke(
                ["inspect", str(encrypted), "--password-env", "TEST_PDF_PASSWORD"]
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["encrypted"])


if __name__ == "__main__":
    unittest.main()

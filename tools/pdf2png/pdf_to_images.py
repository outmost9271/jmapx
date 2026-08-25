#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render PDF pages to PNG files for local visual inspection."""

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
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pymupdf
import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw

DEFAULT_DPI = 200
DEFAULT_MAX_INPUT_MB = 200.0
DEFAULT_MAX_PAGES = 500
DEFAULT_MAX_PAGE_MEGAPIXELS = 40.0
DEFAULT_MAX_OUTPUT_MEGAPIXELS = 80.0
DEFAULT_MAX_TOTAL_MEGAPIXELS = 500.0
DEFAULT_MAX_OUTPUTS = 1000
DEFAULT_MAX_LONG_HEIGHT = 16_000
LABEL_HEIGHT = 28


class PdfToImagesError(Exception):
    """Expected user-facing error."""


@dataclass(frozen=True)
class PageInfo:
    page: int
    width_points: float
    height_points: float
    rotation: int

    def estimated_pixels(
        self,
        dpi: int,
        extra_rotation: int = 0,
        crop_percent: tuple[float, float, float, float] | None = None,
    ) -> tuple[int, int]:
        width = max(1, math.ceil(self.width_points * dpi / 72.0))
        height = max(1, math.ceil(self.height_points * dpi / 72.0))
        if extra_rotation in (90, 270):
            width, height = height, width
        if crop_percent is not None:
            left, top, right, bottom = crop_percent
            width = max(1, math.ceil(width * (right - left) / 100.0))
            height = max(1, math.ceil(height * (bottom - top) / 100.0))
        return width, height


class PdfDocument:
    backend_name = "unknown"

    def __init__(self, path: Path, password: str | None):
        self.path = path
        self.encrypted: bool | None = None
        self.repaired: bool | None = None

    @property
    def page_count(self) -> int:
        raise NotImplementedError

    def page_info(self, index: int) -> PageInfo:
        raise NotImplementedError

    def render_page(
        self,
        index: int,
        dpi: int,
        grayscale: bool,
        background: tuple[int, int, int],
        annotations: bool,
    ) -> Image.Image:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class MuPdfDocument(PdfDocument):
    backend_name = "pymupdf"

    def __init__(self, path: Path, password: str | None):
        super().__init__(path, password)
        try:
            self.document = pymupdf.open(path)
        except Exception as exc:
            raise PdfToImagesError(f"PyMuPDF 无法打开 PDF：{exc}") from exc

        self.encrypted = bool(self.document.needs_pass or self.document.is_encrypted)
        self.repaired = bool(self.document.is_repaired)
        if self.document.needs_pass:
            if not password:
                self.document.close()
                raise PdfToImagesError(
                    "PDF 已加密；请通过 PDF_PASSWORD、--password-env 或 --password-file 提供密码"
                )
            if self.document.authenticate(password) <= 0:
                self.document.close()
                raise PdfToImagesError("PDF 密码不正确")

    @property
    def page_count(self) -> int:
        return self.document.page_count

    def page_info(self, index: int) -> PageInfo:
        page = self.document.load_page(index)
        rect = page.rect
        return PageInfo(
            page=index + 1,
            width_points=round(float(rect.width), 3),
            height_points=round(float(rect.height), 3),
            rotation=int(page.rotation),
        )

    def render_page(
        self,
        index: int,
        dpi: int,
        grayscale: bool,
        background: tuple[int, int, int],
        annotations: bool,
    ) -> Image.Image:
        page = self.document.load_page(index)
        custom_background = background != (255, 255, 255)
        try:
            pixmap = page.get_pixmap(
                dpi=dpi,
                colorspace=pymupdf.csRGB,
                alpha=custom_background,
                annots=annotations,
            )
        except Exception as exc:
            raise PdfToImagesError(f"PyMuPDF 渲染第 {index + 1} 页失败：{exc}") from exc

        mode = "RGBA" if pixmap.alpha else "RGB"
        image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
        if image.mode == "RGBA":
            canvas = Image.new("RGBA", image.size, (*background, 255))
            canvas.alpha_composite(image)
            image.close()
            image = canvas.convert("RGB")
            canvas.close()
        if grayscale:
            converted = image.convert("L")
            image.close()
            image = converted
        return image

    def close(self) -> None:
        self.document.close()


class PdfiumDocument(PdfDocument):
    backend_name = "pdfium"

    def __init__(self, path: Path, password: str | None):
        super().__init__(path, password)
        try:
            self.document = pdfium.PdfDocument(path, password=password)
        except Exception as exc:
            message = str(exc)
            if "password" in message.lower():
                raise PdfToImagesError("PDF 已加密或密码不正确") from exc
            raise PdfToImagesError(f"PDFium 无法打开 PDF：{exc}") from exc
        self.encrypted = None
        self.repaired = None
        try:
            self.document.init_forms()
        except pdfium.PdfiumError:
            # Most documents do not contain interactive forms. Rendering normal
            # annotations still works without an initialized form environment.
            pass

    @property
    def page_count(self) -> int:
        return len(self.document)

    def page_info(self, index: int) -> PageInfo:
        page = self.document[index]
        try:
            width, height = page.get_size()
            return PageInfo(
                page=index + 1,
                width_points=round(float(width), 3),
                height_points=round(float(height), 3),
                rotation=int(page.get_rotation()),
            )
        finally:
            page.close()

    def render_page(
        self,
        index: int,
        dpi: int,
        grayscale: bool,
        background: tuple[int, int, int],
        annotations: bool,
    ) -> Image.Image:
        page = self.document[index]
        bitmap = None
        try:
            bitmap = page.render(
                scale=dpi / 72.0,
                grayscale=grayscale,
                fill_color=(*background, 255),
                draw_annots=annotations,
                rev_byteorder=True,
                limit_image_cache=True,
            )
            return bitmap.to_pil().copy()
        except Exception as exc:
            raise PdfToImagesError(f"PDFium 渲染第 {index + 1} 页失败：{exc}") from exc
        finally:
            if bitmap is not None:
                bitmap.close()
            page.close()

    def close(self) -> None:
        self.document.close()


def open_document(path: Path, backend: str, password: str | None) -> PdfDocument:
    if backend == "pymupdf":
        return MuPdfDocument(path, password)
    if backend == "pdfium":
        return PdfiumDocument(path, password)

    errors: list[str] = []
    for document_type in (MuPdfDocument, PdfiumDocument):
        try:
            return document_type(path, password)
        except PdfToImagesError as exc:
            errors.append(str(exc))
    raise PdfToImagesError("自动后端均无法打开 PDF：" + "；".join(errors))


def validate_input(path_value: str, max_input_mb: float) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise PdfToImagesError(f"输入文件不存在：{path}")
    if not path.is_file():
        raise PdfToImagesError(f"输入路径不是普通文件：{path}")
    size = path.stat().st_size
    if size == 0:
        raise PdfToImagesError("输入 PDF 是空文件")
    if size > max_input_mb * 1024 * 1024:
        raise PdfToImagesError(
            f"输入文件为 {size / 1024 / 1024:.2f} MiB，超过 {max_input_mb:g} MiB 限制"
        )
    try:
        with path.open("rb") as stream:
            header = stream.read(1024)
    except OSError as exc:
        raise PdfToImagesError(f"无法读取输入文件：{path}") from exc
    if b"%PDF-" not in header:
        raise PdfToImagesError("输入文件头中未发现 PDF 标识")
    return path


def resolve_password(args: argparse.Namespace) -> str | None:
    if getattr(args, "password_env", None):
        value = os.environ.get(args.password_env)
        if value is None:
            raise PdfToImagesError(f"环境变量 {args.password_env} 未设置")
        return value
    if getattr(args, "password_file", None):
        password_path = Path(args.password_file).expanduser()
        try:
            if (
                os.name == "posix"
                and stat.S_IMODE(password_path.stat().st_mode) & 0o077
            ):
                raise PdfToImagesError(
                    "密码文件权限必须禁止组用户和其他用户访问，建议设置为 600 或 400"
                )
            return password_path.read_text(encoding="utf-8").splitlines()[0]
        except PdfToImagesError:
            raise
        except (OSError, IndexError) as exc:
            raise PdfToImagesError(f"无法读取密码文件：{password_path}") from exc
    return os.environ.get("PDF_PASSWORD")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_page_selection(value: str, page_count: int) -> list[int]:
    selection = value.strip().lower()
    if selection == "all":
        return list(range(1, page_count + 1))
    if selection == "odd":
        return list(range(1, page_count + 1, 2))
    if selection == "even":
        return list(range(2, page_count + 1, 2))
    if not selection:
        raise PdfToImagesError("页码选择不能为空")

    pages: list[int] = []
    seen: set[int] = set()
    for token in selection.split(","):
        token = token.strip()
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise PdfToImagesError(f"无效页码表达式：{token!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise PdfToImagesError(f"无效页码范围：{token!r}")
        if end > page_count:
            raise PdfToImagesError(f"页码 {end} 超出 PDF 总页数 {page_count}")
        for page in range(start, end + 1):
            if page not in seen:
                pages.append(page)
                seen.add(page)
    return pages


def parse_background(value: str) -> tuple[int, int, int]:
    named = {"white": (255, 255, 255), "black": (0, 0, 0)}
    lowered = value.strip().lower()
    if lowered in named:
        return named[lowered]
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if match:
        raw = match.group(1)
        return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    match = re.fullmatch(r"\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*", value)
    if match:
        color = tuple(int(item) for item in match.groups())
        if all(0 <= item <= 255 for item in color):
            return color  # type: ignore[return-value]
    raise PdfToImagesError("背景色应为 white、black、#RRGGBB 或 R,G,B")


def parse_crop_percent(value: str | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        left, top, right, bottom = [float(item.strip()) for item in value.split(",")]
    except (TypeError, ValueError) as exc:
        raise PdfToImagesError("--crop-percent 应为 left,top,right,bottom") from exc
    if not (0 <= left < right <= 100 and 0 <= top < bottom <= 100):
        raise PdfToImagesError(
            "裁剪百分比必须满足 0 <= left < right <= 100 且 0 <= top < bottom <= 100"
        )
    return left, top, right, bottom


def pixel_megapixels(width: int, height: int) -> float:
    return width * height / 1_000_000.0


def enforce_dimensions(
    width: int,
    height: int,
    limit_megapixels: float,
    label: str,
    max_height: int | None = None,
) -> None:
    megapixels = pixel_megapixels(width, height)
    if megapixels > limit_megapixels:
        raise PdfToImagesError(
            f"{label} 预计为 {width}×{height}（{megapixels:.2f} MP），超过 {limit_megapixels:g} MP 限制"
        )
    if max_height is not None and height > max_height:
        raise PdfToImagesError(f"{label} 高度 {height} 超过 {max_height} 像素限制")


def image_background(
    mode: str, rgb: tuple[int, int, int]
) -> int | tuple[int, int, int]:
    if mode == "L":
        return round(0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2])
    return rgb


def transform_image(
    image: Image.Image,
    extra_rotation: int,
    crop_percent: tuple[float, float, float, float] | None,
    trim_whitespace: bool,
    trim_padding: int,
    background: tuple[int, int, int],
) -> Image.Image:
    fill = image_background(image.mode, background)
    if extra_rotation:
        rotated = image.rotate(-extra_rotation, expand=True, fillcolor=fill)
        image.close()
        image = rotated

    if crop_percent is not None:
        left, top, right, bottom = crop_percent
        box = (
            math.floor(image.width * left / 100.0),
            math.floor(image.height * top / 100.0),
            math.ceil(image.width * right / 100.0),
            math.ceil(image.height * bottom / 100.0),
        )
        cropped = image.crop(box)
        image.close()
        image = cropped

    if trim_whitespace:
        solid = Image.new(image.mode, image.size, fill)
        difference = ImageChops.difference(image, solid)
        solid.close()
        bbox = difference.getbbox()
        difference.close()
        if bbox is not None:
            left = max(0, bbox[0] - trim_padding)
            top = max(0, bbox[1] - trim_padding)
            right = min(image.width, bbox[2] + trim_padding)
            bottom = min(image.height, bbox[3] + trim_padding)
            trimmed = image.crop((left, top, right, bottom))
            image.close()
            image = trimmed
    return image


def truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_prefix(path: Path, override: str | None) -> str:
    value = (override or path.stem).strip()
    value = re.sub(r"[\x00-\x1f/\\]+", "_", value)
    value = value.strip(" .") or "document"
    return truncate_utf8(value, 120).rstrip(" .") or "document"


def ensure_output_dir(value: str) -> Path:
    output_dir = Path(value).expanduser().resolve()
    existed = output_dir.exists()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PdfToImagesError(f"无法创建输出目录：{output_dir}") from exc
    if not output_dir.is_dir():
        raise PdfToImagesError(f"输出路径不是目录：{output_dir}")
    if not existed:
        try:
            output_dir.chmod(0o700)
        except OSError:
            pass
    return output_dir


def atomic_save_image(
    image: Image.Image,
    target: Path,
    compression: int,
    dpi: int,
    overwrite: bool,
) -> None:
    if target.exists() and not overwrite:
        raise PdfToImagesError(f"输出文件已存在；如需覆盖请使用 --overwrite：{target}")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp.png", dir=target.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        image.save(temporary, format="PNG", compress_level=compression, dpi=(dpi, dpi))
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        if overwrite:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise PdfToImagesError(f"输出文件已存在：{target}") from exc
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def atomic_save_json(data: dict[str, Any], target: Path, overwrite: bool) -> None:
    if target.exists() and not overwrite:
        raise PdfToImagesError(f"清单文件已存在；如需覆盖请使用 --overwrite：{target}")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp.json", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        if overwrite:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise PdfToImagesError(f"清单文件已存在：{target}") from exc
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def render_selected_page(
    document: PdfDocument,
    page_number: int,
    args: argparse.Namespace,
    background: tuple[int, int, int],
    crop_percent: tuple[float, float, float, float] | None,
) -> Image.Image:
    image = document.render_page(
        page_number - 1,
        dpi=args.dpi,
        grayscale=args.grayscale,
        background=background,
        annotations=args.annotations,
    )
    image = transform_image(
        image,
        extra_rotation=args.rotate,
        crop_percent=crop_percent,
        trim_whitespace=args.trim_whitespace,
        trim_padding=args.trim_padding,
        background=background,
    )
    enforce_dimensions(
        image.width,
        image.height,
        args.max_page_megapixels,
        f"第 {page_number} 页",
    )
    return image


def output_record(
    target: Path,
    mode: str,
    pages: Sequence[int],
    image: Image.Image,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "file": str(target),
        "mode": mode,
        "pages": list(pages),
        "width": image.width,
        "height": image.height,
        "megapixels": round(pixel_megapixels(image.width, image.height), 3),
    }
    result.update(extra)
    return result


def render_separate(
    document: PdfDocument,
    selected_pages: Sequence[int],
    output_dir: Path,
    prefix: str,
    args: argparse.Namespace,
    background: tuple[int, int, int],
    crop_percent: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    padding = max(3, len(str(document.page_count)))
    outputs: list[dict[str, Any]] = []
    for page_number in selected_pages:
        target = output_dir / f"{prefix}-p{page_number:0{padding}d}.png"
        image = render_selected_page(
            document, page_number, args, background, crop_percent
        )
        try:
            atomic_save_image(
                image, target, args.png_compression, args.dpi, args.overwrite
            )
            outputs.append(
                output_record(
                    target,
                    "separate",
                    [page_number],
                    image,
                    page_regions=[
                        {
                            "page": page_number,
                            "canvas_bbox": [0, 0, image.width, image.height],
                        }
                    ],
                )
            )
        finally:
            image.close()
    return outputs


def text_color(background: tuple[int, int, int]) -> tuple[int, int, int]:
    luminance = 0.299 * background[0] + 0.587 * background[1] + 0.114 * background[2]
    return (0, 0, 0) if luminance >= 140 else (255, 255, 255)


def compose_vertical(
    images: Sequence[tuple[int, Image.Image]],
    background: tuple[int, int, int],
    gap: int,
    page_labels: bool,
    max_output_megapixels: float,
    max_long_height: int,
) -> tuple[Image.Image, list[dict[str, Any]]]:
    mode = images[0][1].mode
    width = max(image.width for _, image in images)
    height = sum(image.height for _, image in images)
    height += gap * max(0, len(images) - 1)
    if page_labels:
        height += LABEL_HEIGHT * len(images)
    enforce_dimensions(
        width, height, max_output_megapixels, "纵向长图", max_long_height
    )

    canvas = Image.new(mode, (width, height), image_background(mode, background))
    draw = ImageDraw.Draw(canvas)
    label_fill: int | tuple[int, int, int] = text_color(background)
    if mode == "L":
        label_fill = image_background(mode, label_fill)
    y = 0
    regions: list[dict[str, Any]] = []
    for index, (page_number, image) in enumerate(images):
        if index:
            y += gap
        if page_labels:
            label = f"Page {page_number}"
            draw.text((8, y + 6), label, fill=label_fill)
            y += LABEL_HEIGHT
        x = (width - image.width) // 2
        canvas.paste(image, (x, y))
        regions.append(
            {
                "page": page_number,
                "canvas_bbox": [x, y, x + image.width, y + image.height],
            }
        )
        y += image.height
    return canvas, regions


def estimated_vertical_height(
    infos: dict[int, PageInfo],
    pages: Sequence[int],
    args: argparse.Namespace,
    crop_percent: tuple[float, float, float, float] | None,
) -> int:
    height = sum(
        infos[page].estimated_pixels(args.dpi, args.rotate, crop_percent)[1]
        for page in pages
    )
    height += args.gap * max(0, len(pages) - 1)
    if args.page_labels:
        height += LABEL_HEIGHT * len(pages)
    return height


def group_vertical_chunks(
    infos: dict[int, PageInfo],
    pages: Sequence[int],
    args: argparse.Namespace,
    crop_percent: tuple[float, float, float, float] | None,
) -> list[list[int]]:
    groups: list[list[int]] = []
    current: list[int] = []
    for page in pages:
        candidate = [*current, page]
        candidate_height = estimated_vertical_height(
            infos, candidate, args, crop_percent
        )
        if current and (
            len(candidate) > args.chunk_pages or candidate_height > args.max_long_height
        ):
            groups.append(current)
            current = [page]
        else:
            current = candidate
        if (
            estimated_vertical_height(infos, current, args, crop_percent)
            > args.max_long_height
        ):
            raise PdfToImagesError(
                f"第 {page} 页单独生成长图仍超过 {args.max_long_height} 像素；请降低 DPI、裁剪或使用 separate/tiles"
            )
    if current:
        groups.append(current)
    return groups


def render_vertical_groups(
    document: PdfDocument,
    groups: Sequence[Sequence[int]],
    output_dir: Path,
    prefix: str,
    args: argparse.Namespace,
    background: tuple[int, int, int],
    crop_percent: tuple[float, float, float, float] | None,
    chunked: bool,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for group_index, pages in enumerate(groups, 1):
        rendered: list[tuple[int, Image.Image]] = []
        try:
            for page_number in pages:
                rendered.append(
                    (
                        page_number,
                        render_selected_page(
                            document, page_number, args, background, crop_percent
                        ),
                    )
                )
            canvas, regions = compose_vertical(
                rendered,
                background=background,
                gap=args.gap,
                page_labels=args.page_labels,
                max_output_megapixels=args.max_output_megapixels,
                max_long_height=args.max_long_height,
            )
            try:
                suffix = f"-vertical-{group_index:03d}" if chunked else "-vertical"
                target = output_dir / f"{prefix}{suffix}.png"
                atomic_save_image(
                    canvas, target, args.png_compression, args.dpi, args.overwrite
                )
                outputs.append(
                    output_record(
                        target,
                        "vertical-chunks" if chunked else "vertical",
                        pages,
                        canvas,
                        page_regions=regions,
                    )
                )
            finally:
                canvas.close()
        finally:
            for _, image in rendered:
                image.close()
    return outputs


def render_grid(
    document: PdfDocument,
    selected_pages: Sequence[int],
    output_dir: Path,
    prefix: str,
    args: argparse.Namespace,
    background: tuple[int, int, int],
    crop_percent: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    thumbnails: list[tuple[int, Image.Image]] = []
    try:
        for page_number in selected_pages:
            image = render_selected_page(
                document, page_number, args, background, crop_percent
            )
            image.thumbnail(
                (args.grid_cell_width, args.grid_cell_height), Image.Resampling.LANCZOS
            )
            thumbnails.append((page_number, image))

        columns = min(args.grid_columns, len(thumbnails))
        rows = math.ceil(len(thumbnails) / columns)
        cell_width = args.grid_cell_width
        cell_height = args.grid_cell_height + LABEL_HEIGHT
        width = columns * cell_width + (columns + 1) * args.gap
        height = rows * cell_height + (rows + 1) * args.gap
        enforce_dimensions(width, height, args.max_output_megapixels, "缩略图网格")
        mode = "L" if args.grayscale else "RGB"
        canvas = Image.new(mode, (width, height), image_background(mode, background))
        draw = ImageDraw.Draw(canvas)
        regions: list[dict[str, Any]] = []
        label_fill = text_color(background)
        if mode == "L":
            label_fill = image_background(mode, label_fill)
        for index, (page_number, image) in enumerate(thumbnails):
            row, column = divmod(index, columns)
            cell_x = args.gap + column * cell_width
            cell_y = args.gap + row * cell_height
            draw.text((cell_x + 4, cell_y + 5), f"Page {page_number}", fill=label_fill)
            image_x = cell_x + (cell_width - image.width) // 2
            image_y = (
                cell_y + LABEL_HEIGHT + (args.grid_cell_height - image.height) // 2
            )
            canvas.paste(image, (image_x, image_y))
            regions.append(
                {
                    "page": page_number,
                    "canvas_bbox": [
                        image_x,
                        image_y,
                        image_x + image.width,
                        image_y + image.height,
                    ],
                }
            )
        try:
            target = output_dir / f"{prefix}-grid.png"
            atomic_save_image(
                canvas, target, args.png_compression, args.dpi, args.overwrite
            )
            return [
                output_record(
                    target, "grid", selected_pages, canvas, page_regions=regions
                )
            ]
        finally:
            canvas.close()
    finally:
        for _, image in thumbnails:
            image.close()


def tile_positions(length: int, tile_size: int, overlap: int) -> list[int]:
    if length <= tile_size:
        return [0]
    step = tile_size - overlap
    positions = list(range(0, length - tile_size + 1, step))
    last = length - tile_size
    if positions[-1] != last:
        positions.append(last)
    return positions


def render_tiles(
    document: PdfDocument,
    selected_pages: Sequence[int],
    output_dir: Path,
    prefix: str,
    args: argparse.Namespace,
    background: tuple[int, int, int],
    crop_percent: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    padding = max(3, len(str(document.page_count)))
    outputs: list[dict[str, Any]] = []
    for page_number in selected_pages:
        image = render_selected_page(
            document, page_number, args, background, crop_percent
        )
        try:
            xs = tile_positions(image.width, args.tile_width, args.tile_overlap)
            ys = tile_positions(image.height, args.tile_height, args.tile_overlap)
            if len(outputs) + len(xs) * len(ys) > args.max_outputs:
                raise PdfToImagesError(f"切片输出数量将超过 {args.max_outputs} 个限制")
            for row, y in enumerate(ys, 1):
                for column, x in enumerate(xs, 1):
                    right = min(x + args.tile_width, image.width)
                    bottom = min(y + args.tile_height, image.height)
                    tile = image.crop((x, y, right, bottom))
                    try:
                        target = output_dir / (
                            f"{prefix}-p{page_number:0{padding}d}-r{row:02d}-c{column:02d}.png"
                        )
                        atomic_save_image(
                            tile, target, args.png_compression, args.dpi, args.overwrite
                        )
                        outputs.append(
                            output_record(
                                target,
                                "tiles",
                                [page_number],
                                tile,
                                source_pixel_bbox=[x, y, right, bottom],
                                row=row,
                                column=column,
                            )
                        )
                    finally:
                        tile.close()
        finally:
            image.close()
    return outputs


def collect_page_infos(document: PdfDocument, max_pages: int) -> list[PageInfo]:
    if document.page_count <= 0:
        raise PdfToImagesError("PDF 没有可渲染页面")
    if document.page_count > max_pages:
        raise PdfToImagesError(
            f"PDF 共 {document.page_count} 页，超过 {max_pages} 页限制"
        )
    return [document.page_info(index) for index in range(document.page_count)]


def inspect_pdf(args: argparse.Namespace) -> dict[str, Any]:
    path = validate_input(args.pdf, args.max_input_mb)
    password = resolve_password(args)
    with open_document(path, args.backend, password) as document:
        infos = collect_page_infos(document, args.max_pages)
        pages: list[dict[str, Any]] = []
        total_pixels = 0
        for info in infos:
            width, height = info.estimated_pixels(args.dpi)
            total_pixels += width * height
            pages.append(
                {
                    **asdict(info),
                    "estimated_pixel_width": width,
                    "estimated_pixel_height": height,
                    "estimated_megapixels": round(pixel_megapixels(width, height), 3),
                    "estimated_rgb_bytes": width * height * 3,
                }
            )
        return {
            "ok": True,
            "source": str(path),
            "source_size": path.stat().st_size,
            "source_sha256": sha256_file(path),
            "backend": document.backend_name,
            "encrypted": document.encrypted,
            "repaired": document.repaired,
            "page_count": document.page_count,
            "estimate_dpi": args.dpi,
            "estimated_total_megapixels": round(total_pixels / 1_000_000.0, 3),
            "pages": pages,
        }


def render_pdf(args: argparse.Namespace) -> dict[str, Any]:
    path = validate_input(args.pdf, args.max_input_mb)
    output_dir = ensure_output_dir(args.output_dir)
    password = resolve_password(args)
    background = parse_background(args.background)
    crop_percent = parse_crop_percent(args.crop_percent)

    with open_document(path, args.backend, password) as document:
        infos_list = collect_page_infos(document, args.max_pages)
        infos = {info.page: info for info in infos_list}
        selected_pages = parse_page_selection(args.pages, document.page_count)
        if not selected_pages:
            raise PdfToImagesError("没有选中任何页面")
        if args.mode == "separate" and len(selected_pages) > args.max_outputs:
            raise PdfToImagesError(f"独立页面输出数量超过 {args.max_outputs} 个限制")

        estimated_total = 0
        for page_number in selected_pages:
            width, height = infos[page_number].estimated_pixels(
                args.dpi, args.rotate, crop_percent
            )
            enforce_dimensions(
                width, height, args.max_page_megapixels, f"第 {page_number} 页"
            )
            estimated_total += width * height
        if estimated_total / 1_000_000.0 > args.max_total_megapixels:
            raise PdfToImagesError(
                f"所选页面预计共 {estimated_total / 1_000_000.0:.2f} MP，超过 "
                f"{args.max_total_megapixels:g} MP 总量限制"
            )

        prefix = safe_prefix(path, args.prefix)
        if args.mode == "separate":
            outputs = render_separate(
                document,
                selected_pages,
                output_dir,
                prefix,
                args,
                background,
                crop_percent,
            )
        elif args.mode == "vertical":
            estimated_height = estimated_vertical_height(
                infos, selected_pages, args, crop_percent
            )
            if estimated_height > args.max_long_height:
                raise PdfToImagesError(
                    f"长图预计高度 {estimated_height} 超过 {args.max_long_height}；"
                    "请降低 DPI、减少页面或使用 vertical-chunks"
                )
            outputs = render_vertical_groups(
                document,
                [selected_pages],
                output_dir,
                prefix,
                args,
                background,
                crop_percent,
                chunked=False,
            )
        elif args.mode == "vertical-chunks":
            groups = group_vertical_chunks(infos, selected_pages, args, crop_percent)
            if len(groups) > args.max_outputs:
                raise PdfToImagesError(f"分段长图数量将超过 {args.max_outputs} 个限制")
            outputs = render_vertical_groups(
                document,
                groups,
                output_dir,
                prefix,
                args,
                background,
                crop_percent,
                chunked=True,
            )
        elif args.mode == "grid":
            outputs = render_grid(
                document,
                selected_pages,
                output_dir,
                prefix,
                args,
                background,
                crop_percent,
            )
        elif args.mode == "tiles":
            estimated_tiles = 0
            for page_number in selected_pages:
                width, height = infos[page_number].estimated_pixels(
                    args.dpi, args.rotate, crop_percent
                )
                estimated_tiles += len(
                    tile_positions(width, args.tile_width, args.tile_overlap)
                ) * len(tile_positions(height, args.tile_height, args.tile_overlap))
            if estimated_tiles > args.max_outputs:
                raise PdfToImagesError(
                    f"切片输出数量预计为 {estimated_tiles}，超过 {args.max_outputs} 个限制"
                )
            outputs = render_tiles(
                document,
                selected_pages,
                output_dir,
                prefix,
                args,
                background,
                crop_percent,
            )
        else:
            raise PdfToImagesError(f"未知输出模式：{args.mode}")

        if len(outputs) > args.max_outputs:
            raise PdfToImagesError(
                f"输出数量 {len(outputs)} 超过 {args.max_outputs} 个限制"
            )

        result: dict[str, Any] = {
            "ok": True,
            "source": str(path),
            "source_size": path.stat().st_size,
            "source_sha256": sha256_file(path),
            "backend": document.backend_name,
            "page_count": document.page_count,
            "selected_pages": selected_pages,
            "dpi": args.dpi,
            "mode": args.mode,
            "grayscale": args.grayscale,
            "background": "#{:02X}{:02X}{:02X}".format(*background),
            "crop_percent": list(crop_percent) if crop_percent is not None else None,
            "outputs": outputs,
        }

    if not args.no_manifest:
        manifest = (
            Path(args.manifest).expanduser().resolve()
            if args.manifest
            else output_dir / f"{prefix}-{args.mode}-manifest.json"
        )
        if manifest == path:
            raise PdfToImagesError("JSON 清单路径不能与输入 PDF 相同")
        if manifest.parent != output_dir:
            ensure_output_dir(str(manifest.parent))
        result["manifest"] = str(manifest)
        atomic_save_json(result, manifest, args.overwrite)
    return result


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正数")
    return number


def add_password_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--password-env", metavar="NAME", help="从指定环境变量读取 PDF 密码"
    )
    group.add_argument(
        "--password-file", metavar="PATH", help="从仅含密码首行的文件读取 PDF 密码"
    )


def add_limit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-input-mb", type=positive_float, default=DEFAULT_MAX_INPUT_MB
    )
    parser.add_argument("--max-pages", type=positive_int, default=DEFAULT_MAX_PAGES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2png",
        description="将 PDF 页面安全地渲染为适合视觉模型读取的 PNG 图片",
    )
    parser.add_argument("--debug", action="store_true", help="发生异常时输出调用栈")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="查看页数、页面尺寸和渲染规模"
    )
    inspect_parser.add_argument("pdf", help="输入 PDF 文件")
    inspect_parser.add_argument(
        "--backend", choices=("auto", "pymupdf", "pdfium"), default="auto"
    )
    inspect_parser.add_argument(
        "--dpi", type=positive_int, default=DEFAULT_DPI, help="估算使用的 DPI"
    )
    add_password_arguments(inspect_parser)
    add_limit_arguments(inspect_parser)

    render_parser = subparsers.add_parser("render", help="将指定页面渲染为 PNG")
    render_parser.add_argument("pdf", help="输入 PDF 文件")
    render_parser.add_argument(
        "--output-dir", required=True, help="输出目录；不存在时自动创建并设为 700"
    )
    render_parser.add_argument(
        "--pages", default="all", help="all、odd、even、1 或 1,3-5"
    )
    render_parser.add_argument(
        "--mode",
        choices=("separate", "vertical", "vertical-chunks", "grid", "tiles"),
        default="separate",
    )
    render_parser.add_argument(
        "--backend", choices=("auto", "pymupdf", "pdfium"), default="auto"
    )
    render_parser.add_argument("--dpi", type=positive_int, default=DEFAULT_DPI)
    render_parser.add_argument("--prefix", help="覆盖输出文件名前缀")
    render_parser.add_argument("--grayscale", action="store_true", help="输出灰度图片")
    render_parser.add_argument(
        "--background", default="#FFFFFF", help="white、black、#RRGGBB 或 R,G,B"
    )
    render_parser.add_argument(
        "--rotate",
        type=int,
        choices=(0, 90, 180, 270),
        default=0,
        help="额外顺时针旋转",
    )
    render_parser.add_argument(
        "--annotations",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否渲染注释和表单标注",
    )
    render_parser.add_argument(
        "--crop-percent",
        help="按渲染图百分比裁剪：left,top,right,bottom，例如 0,50,100,100",
    )
    render_parser.add_argument(
        "--trim-whitespace", action="store_true", help="裁掉与背景色相同的外边缘"
    )
    render_parser.add_argument("--trim-padding", type=nonnegative_int, default=8)
    render_parser.add_argument(
        "--png-compression", type=int, choices=range(10), default=6
    )
    render_parser.add_argument(
        "--overwrite", action="store_true", help="允许覆盖已有输出"
    )
    render_parser.add_argument("--manifest", help="自定义 JSON 清单路径")
    render_parser.add_argument(
        "--no-manifest", action="store_true", help="不保存 JSON 清单"
    )

    render_parser.add_argument(
        "--gap", type=nonnegative_int, default=24, help="拼图页面间距"
    )
    render_parser.add_argument(
        "--page-labels",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="在长图中添加 Page N 标签",
    )
    render_parser.add_argument("--chunk-pages", type=positive_int, default=4)
    render_parser.add_argument(
        "--max-long-height", type=positive_int, default=DEFAULT_MAX_LONG_HEIGHT
    )
    render_parser.add_argument("--grid-columns", type=positive_int, default=3)
    render_parser.add_argument("--grid-cell-width", type=positive_int, default=480)
    render_parser.add_argument("--grid-cell-height", type=positive_int, default=680)
    render_parser.add_argument("--tile-width", type=positive_int, default=1600)
    render_parser.add_argument("--tile-height", type=positive_int, default=1600)
    render_parser.add_argument("--tile-overlap", type=nonnegative_int, default=100)

    add_password_arguments(render_parser)
    add_limit_arguments(render_parser)
    render_parser.add_argument(
        "--max-page-megapixels",
        type=positive_float,
        default=DEFAULT_MAX_PAGE_MEGAPIXELS,
    )
    render_parser.add_argument(
        "--max-output-megapixels",
        type=positive_float,
        default=DEFAULT_MAX_OUTPUT_MEGAPIXELS,
    )
    render_parser.add_argument(
        "--max-total-megapixels",
        type=positive_float,
        default=DEFAULT_MAX_TOTAL_MEGAPIXELS,
    )
    render_parser.add_argument(
        "--max-outputs", type=positive_int, default=DEFAULT_MAX_OUTPUTS
    )
    return parser


def validate_render_arguments(args: argparse.Namespace) -> None:
    if args.tile_overlap >= args.tile_width or args.tile_overlap >= args.tile_height:
        raise PdfToImagesError(
            "--tile-overlap 必须同时小于 --tile-width 和 --tile-height"
        )
    if args.manifest and args.no_manifest:
        raise PdfToImagesError("--manifest 与 --no-manifest 不能同时使用")


def emit_json(data: dict[str, Any], stream: Any | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    json.dump(data, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_pdf(args)
        else:
            validate_render_arguments(args)
            result = render_pdf(args)
        emit_json(result)
        return 0
    except (PdfToImagesError, MemoryError) as exc:
        emit_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts unknown failures to JSON
        emit_json({"ok": False, "error": f"未预期错误：{exc}"}, stream=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

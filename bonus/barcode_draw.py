"""Штрихкод Code128 только из цифр — для сканеров на кассе и печати."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from barcode import Code128
from barcode.writer import ImageWriter, SVGWriter
from PIL import Image, ImageDraw, ImageFont

_SIZE_MM = re.compile(
    r'\swidth="(?P<width>[\d.]+)mm"\s+height="(?P<height>[\d.]+)mm"',
)

_FONT_CANDIDATES = (
    Path(__file__).resolve().parent / "assets" / "fonts" / "DejaVuSans.ttf",
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
)


def barcode_svg(digits: str) -> str:
    """Рисует Code128 по цифровому коду сертификата."""
    payload = _digits_only(digits)
    if payload == "":
        return ""
    buffer = BytesIO()
    Code128(payload, writer=SVGWriter()).write(
        buffer,
        options={
            "module_width": 0.5,
            "module_height": 24.0,
            "quiet_zone": 0.8,
            "font_size": 0,
            "text_distance": 1.0,
            "write_text": False,
            "background": "white",
            "foreground": "black",
        },
    )
    text = buffer.getvalue().decode("utf-8")
    start = text.find("<svg")
    if start > 0:
        text = text[start:]
    return _fit_svg_to_card(text)


def barcode_png(
    digits: str,
    *,
    code_label: str,
    certificate_number: str,
) -> bytes:
    """PNG Code128 с подписью кода и номером сертификата, без фона.

    Полоски, цифры кода и public_number в одном файле на прозрачном фоне —
    для печати на бумажных сертификатах.
    """
    payload = _digits_only(digits)
    if payload == "":
        return b""
    bars = _render_bars_rgba(payload)
    return _compose_print_png(
        bars,
        code_label=code_label.strip(),
        certificate_number=certificate_number.strip(),
    )


def _digits_only(digits: str) -> str:
    """Оставляет только цифры из кода сертификата."""
    return "".join(char for char in digits if char.isdigit())


def _render_bars_rgba(payload: str) -> Image.Image:
    """Рисует полоски Code128 на прозрачном RGBA без текста."""
    buffer = BytesIO()
    writer = ImageWriter(format="PNG", mode="RGBA")
    Code128(payload, writer=writer).write(
        buffer,
        options={
            "module_width": 0.4,
            "module_height": 18.0,
            "quiet_zone": 1.5,
            "font_size": 0,
            "text_distance": 0,
            "write_text": False,
            "background": (255, 255, 255, 0),
            "foreground": "black",
        },
    )
    return Image.open(BytesIO(buffer.getvalue())).convert("RGBA")


def _compose_print_png(
    bars: Image.Image,
    *,
    code_label: str,
    certificate_number: str,
) -> bytes:
    """Склеивает полоски и две подписи в один прозрачный PNG."""
    lines = [line for line in (code_label, certificate_number) if line]
    code_font = _label_font(42)
    number_font = _label_font(32)
    fonts = [code_font, number_font][: len(lines)]
    draw_probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    gap = 18
    line_gap = 10
    text_heights: list[int] = []
    text_widths: list[int] = []
    for line, font in zip(lines, fonts, strict=True):
        box = draw_probe.textbbox((0, 0), line, font=font)
        text_widths.append(box[2] - box[0])
        text_heights.append(box[3] - box[1])
    text_block = sum(text_heights) + line_gap * max(0, len(lines) - 1)
    width = max(bars.width, max(text_widths, default=0)) + 24
    height = bars.height + (gap + text_block if lines else 0) + 12
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    canvas.paste(bars, ((width - bars.width) // 2, 0), bars)
    draw = ImageDraw.Draw(canvas)
    y = bars.height + gap
    for line, font, line_height, line_width in zip(
        lines, fonts, text_heights, text_widths, strict=True
    ):
        x = (width - line_width) // 2
        draw.text((x, y), line, font=font, fill=(0, 0, 0, 255))
        y += line_height + line_gap
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _label_font(size: int) -> ImageFont.ImageFont:
    """Подбирает TTF для подписи; иначе bitmap-шрифт Pillow."""
    for path in _FONT_CANDIDATES:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit_svg_to_card(svg: str) -> str:
    """Растягивает SVG штрихкода на всю ширину белого блока карточки.

    python-barcode рисует полоски в мм без viewBox. Браузер не сопоставляет
    абсолютные mm с unitless viewBox — полоски остаются слева. Убираем суффикс
    mm, чтобы координаты совпали с viewBox, и растягиваем на 100% ширины.
    """
    if not svg.startswith("<svg"):
        return svg
    size = _SIZE_MM.search(svg)
    view_box = ""
    if size is not None:
        view_box = (
            f' viewBox="0 0 {size.group("width")} {size.group("height")}"'
        )
    svg = re.sub(r'\swidth="[^"]*"', "", svg, count=1)
    svg = re.sub(r'\sheight="[^"]*"', "", svg, count=1)
    svg = re.sub(r'\spreserveAspectRatio="[^"]*"', "", svg, count=1)
    svg = re.sub(r'\sviewBox="[^"]*"', "", svg, count=1)
    svg = re.sub(r"(?<=\d)mm", "", svg)
    return svg.replace(
        "<svg",
        (
            '<svg width="100%" height="56"'
            f'{view_box} preserveAspectRatio="none"'
        ),
        1,
    )

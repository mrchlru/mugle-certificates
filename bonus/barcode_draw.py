"""Штрихкод Code128 только из цифр — для сканеров на кассе."""

import re
from io import BytesIO

from barcode import Code128
from barcode.writer import SVGWriter

_SIZE_MM = re.compile(
    r'\swidth="(?P<width>[\d.]+)mm"\s+height="(?P<height>[\d.]+)mm"',
)


def barcode_svg(digits: str) -> str:
    """Рисует Code128 по цифровому коду сертификата."""
    payload = "".join(char for char in digits if char.isdigit())
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


def _fit_svg_to_card(svg: str) -> str:
    """Растягивает SVG штрихкода на всю ширину белого блока карточки.

    python-barcode рисует полоски в мм без viewBox. Без viewBox при width=100%
    координаты остаются абсолютными и штрихкод «прилипает» влево.
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
    return svg.replace(
        "<svg",
        (
            '<svg width="100%" height="56"'
            f'{view_box} preserveAspectRatio="none"'
        ),
        1,
    )

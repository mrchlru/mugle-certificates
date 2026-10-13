"""Штрихкод Code128 только из цифр — для сканеров на кассе."""

from io import BytesIO

from barcode import Code128
from barcode.writer import SVGWriter


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
    # Растягиваем штрихкод на всю ширину белого блока карточки.
    if 'preserveAspectRatio="' in text:
        text = text.replace('preserveAspectRatio="xMidYMid meet"', 'preserveAspectRatio="none"', 1)
        text = text.replace('preserveAspectRatio="xMinYMid meet"', 'preserveAspectRatio="none"', 1)
    elif text.startswith("<svg"):
        text = text.replace("<svg", '<svg preserveAspectRatio="none"', 1)
    return text

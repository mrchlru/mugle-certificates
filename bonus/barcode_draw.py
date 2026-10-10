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
            "module_width": 0.35,
            "module_height": 18.0,
            "quiet_zone": 2.0,
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
    return text

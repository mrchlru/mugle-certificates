"""QR-код карточки. Внутри ссылка, которую открывает камера телефона."""

import io

import qrcode
from qrcode.image.svg import SvgPathImage


def qr_svg(payload: str) -> str:
    """Собирает SVG без чужих данных: на вход только наша ссылка."""
    code = qrcode.QRCode(border=2, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(payload)
    code.make(fit=True)
    image = code.make_image(image_factory=SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    text = buffer.getvalue().decode("utf-8")
    start = text.find("<svg")
    if start > 0:
        text = text[start:]
    return text

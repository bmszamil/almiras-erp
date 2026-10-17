"""The ZATCA QR code of a printed invoice as inline SVG (invoicing R47; zatca R-15).

The QR payload is the TLV base64 string ZATCA's data carries (``SalesDocument.zatca_qr``, from the cleared XML or the
zatca module); invoicing never builds it (R45). Encoding the payload as a QR matrix uses the pure-Python QR encoder
of ReportLab (BSD licence) when it is installed; it is not declared in requirements.txt, so without it the printed
document shows the payload as text and a bilingual notice instead of the image.
"""
from html import escape


def qr_svg(payload, *, module=4, quiet=4):
    """SVG markup of ``payload`` as a QR code (error correction M), or None when no encoder is available."""
    if not payload:
        return None
    try:
        from reportlab.graphics.barcode import qrencoder
    except ImportError:
        return None
    code = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    code.addData(payload)
    code.make()
    count = code.getModuleCount()
    size = (count + 2 * quiet) * module
    cells = []
    for row in range(count):
        for col in range(count):
            if code.isDark(row, col):
                cells.append(f'<rect x="{(col + quiet) * module}" y="{(row + quiet) * module}" '
                             f'width="{module}" height="{module}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
            f'role="img" aria-label="{escape("ZATCA QR / رمز الاستجابة السريعة")}">'
            f'<rect width="{size}" height="{size}" fill="#fff"/><g fill="#000">{"".join(cells)}</g></svg>')

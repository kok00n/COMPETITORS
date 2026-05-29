"""OCR-based PDF text extraction dla PDFow gdzie pdfplumber nie radzi sobie z
ekstrakcja tabel (Goldman Sachs - bitmap watermark mask, Pekao - extremely
fragmented tables).

Pipeline:
  pdf2image: PDF -> PIL Images per page (wymaga poppler binaries)
  pytesseract: PIL Image -> text (wymaga Tesseract installation + polish lang)

Cost saving vs. Claude vision:
  - Goldman vision tokens: ~420K per PDF -> $1.40 per call
  - Goldman OCR text tokens: ~12K -> $0.04 per call (Claude text prompt)
  - Per backfill 4 lata = $25 vs $620

Wymaga:
  Windows: Tesseract installer https://github.com/UB-Mannheim/tesseract/wiki
           + Polish language pack
           + dodanie do PATH
  Poppler dla pdf2image: https://github.com/oschwartz10612/poppler-windows
           lub pip install pdf2image (przylacza poppler-utils dla Linux)
  Python: pip install pytesseract pdf2image pillow
"""

from __future__ import annotations

import shutil


def is_tesseract_available() -> bool:
    """Sprawdz czy Tesseract jest w PATH."""
    return shutil.which("tesseract") is not None


def is_poppler_available() -> bool:
    """Sprawdz czy poppler (pdfinfo) jest w PATH - wymagany przez pdf2image."""
    return shutil.which("pdfinfo") is not None or shutil.which("pdfinfo.exe") is not None


def ocr_pdf_to_text(
    pdf_bytes: bytes,
    *,
    lang: str = "pol+eng",
    dpi: int = 200,
    max_pages: int | None = None,
) -> str:
    """Convert PDF bytes -> tekst przez OCR.

    Args:
        pdf_bytes: PDF jako bytes
        lang: jezyki Tesseract, np. 'pol+eng' (polski + angielski)
        dpi: rozdzielczosc konwersji (200 = balans jakosc/szybkosc; 300 = lepsze ale wolniejsze)
        max_pages: opcjonalny limit liczby stron (do testow)

    Returns:
        Tekst calego PDFa (strony oddzielone '\\n\\n=== PAGE N ===\\n\\n').

    Raises:
        ImportError jesli pdf2image / pytesseract nie zainstalowane.
        RuntimeError jesli Tesseract lub poppler brak w PATH.
    """
    if not is_tesseract_available():
        raise RuntimeError(
            "Tesseract not found in PATH. Install: "
            "https://github.com/UB-Mannheim/tesseract/wiki"
        )

    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as e:
        raise ImportError(
            "OCR requires: pip install pdf2image pytesseract pillow"
        ) from e

    # Convert PDF pages to PIL Images
    try:
        images = convert_from_bytes(pdf_bytes, dpi=dpi)
    except Exception as e:
        # pdf2image typowo failuje gdy brak poppler
        raise RuntimeError(
            f"pdf2image failed (poppler brak?): {e}. "
            "Pobierz poppler: https://github.com/oschwartz10612/poppler-windows"
        ) from e

    if max_pages is not None:
        images = images[:max_pages]

    page_texts: list[str] = []
    for i, img in enumerate(images):
        text = pytesseract.image_to_string(img, lang=lang)
        page_texts.append(f"=== PAGE {i+1} ===\n{text}")

    return "\n\n".join(page_texts)


def check_setup() -> dict:
    """Diagnostic: sprawdz dostepne komponenty OCR pipeline."""
    out = {
        "tesseract_in_path": is_tesseract_available(),
        "poppler_in_path": is_poppler_available(),
    }
    try:
        import pdf2image  # noqa: F401
        out["pdf2image_installed"] = True
    except ImportError:
        out["pdf2image_installed"] = False
    try:
        import pytesseract  # noqa: F401
        out["pytesseract_installed"] = True
    except ImportError:
        out["pytesseract_installed"] = False

    if out["tesseract_in_path"]:
        try:
            import pytesseract
            out["tesseract_version"] = str(pytesseract.get_tesseract_version())
            out["tesseract_languages"] = pytesseract.get_languages()
        except Exception as e:
            out["tesseract_error"] = str(e)
    return out

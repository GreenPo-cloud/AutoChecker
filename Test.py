"""Diagnostic OCR output for the last four pages of one Label PDF.

This script does not create or modify JSON files. It uses the same rendering,
RapidOCR model, confidence threshold and parsing functions as pdf_label_ocr.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pdf_label_ocr import (
    _label_type_from_text,
    _load_ocr_dependencies,
    _normalize_packeta_tracking_number,
    parse_label_text,
)


DEFAULT_PDF = (
    Path.home()
    / "Downloads"
    / "31.08.2026 Part 1 (Label).pdf"
)
LAST_PAGE_COUNT = 4
RENDER_SCALE = 3.0
MIN_CONFIDENCE = 0.45


def diagnostic_ocr(pdf_path: Path) -> None:
    """Print raw and filtered OCR output for the PDF's last four pages."""
    np, pymupdf, RapidOCR = _load_ocr_dependencies()
    source = pdf_path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF not found: {source}")

    document = pymupdf.open(source)
    try:
        total_pages = len(document)
        if total_pages == 0:
            raise ValueError("PDF contains no pages")
        first_page = max(1, total_pages - LAST_PAGE_COUNT + 1)
        selected_pages = range(first_page, total_pages + 1)
        ocr = RapidOCR()

        print(f"PDF: {source}")
        print(f"All pages: {total_pages}")
        print(f"Testing pages: {first_page}-{total_pages}")
        print(
            f"Render scale: {RENDER_SCALE}; "
            f"minimum confidence: {MIN_CONFIDENCE}"
        )

        for page_number in selected_pages:
            page = document[page_number - 1]
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(RENDER_SCALE, RENDER_SCALE),
                alpha=False,
            )
            image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height,
                pixmap.width,
                pixmap.n,
            )
            result, elapsed = ocr(image)

            print("\n" + "=" * 80)
            print(f"PAGE {page_number} OF {total_pages}")
            print(f"RapidOCR elapsed: {elapsed!r}")
            print("-" * 80)
            print("RAW OCR LINES (confidence | USED/SKIPPED | text)")

            accepted_lines: list[str] = []
            for index, item in enumerate(result or [], start=1):
                text = str(item[1]).strip()
                confidence = float(item[2])
                accepted = bool(text) and confidence >= MIN_CONFIDENCE
                marker = "USED" if accepted else "SKIPPED"
                packeta_tracking = _normalize_packeta_tracking_number(text)
                tracking_note = (
                    f" | PACKETA TRACKING -> {packeta_tracking}"
                    if packeta_tracking is not None
                    else ""
                )
                print(
                    f"{index:03d}. {confidence:.4f} | "
                    f"{marker:<7} | {text!r}{tracking_note}"
                )
                if accepted:
                    accepted_lines.append(text)

            full_text = "\n".join(accepted_lines)
            print("-" * 80)
            print("FILTERED TEXT PASSED TO THE CURRENT PARSER")
            print(full_text or "<EMPTY>")
            print("-" * 80)
            print(f"DETECTED LABEL TYPE: {_label_type_from_text(full_text)!r}")
            print("PARSED FIELDS:")
            print(
                json.dumps(
                    parse_label_text(full_text),
                    ensure_ascii=False,
                    indent=4,
                )
            )
    finally:
        document.close()


def main() -> int:
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    try:
        diagnostic_ocr(pdf_path)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

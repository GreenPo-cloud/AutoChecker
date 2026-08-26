"""Local OCR extraction for RETAIL UPS label PDFs."""

from __future__ import annotations

import argparse
import datetime
from difflib import SequenceMatcher
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable


DOWNLOADS_DIR = Path.home() / "Downloads"
LABEL_PDF_PATTERN = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4}) Part (\d+) \(Label\)\.pdf$",
    re.IGNORECASE,
)


def _load_ocr_dependencies() -> tuple[Any, Any, Any]:
    """Import optional OCR dependencies and provide a useful setup error."""
    try:
        import numpy as np
        import pymupdf
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "Не установлены библиотеки для OCR. Выполните:\n"
            "python -m pip install PyMuPDF rapidocr-onnxruntime"
        ) from exc
    return np, pymupdf, RapidOCR


def _normalize_tracking_number(text: str) -> str | None:
    """Find a UPS 1Z number and remove spaces/punctuation from it."""
    tracking_lines = [line for line in text.splitlines() if "TRACK" in line.upper()]
    candidates = tracking_lines + [text]
    for candidate in candidates:
        for match in re.finditer(r"1Z(?:[\s-]*[A-Z0-9]){16}", candidate.upper()):
            value = re.sub(r"[^A-Z0-9]", "", match.group(0))
            if len(value) == 18:
                return value
    return None


def _ship_to_score(line: str) -> float:
    """Return a 0..100 fuzzy score for OCR variants of the 'SHIP TO' heading."""
    normalized = re.sub(r"[^A-Z]", "", line.upper())
    # Only compare the heading-sized prefix, so a stray suffix does not matter.
    candidate = normalized[: len("SHIPTO")]
    return SequenceMatcher(None, candidate, "SHIPTO").ratio() * 100


def _looks_like_phone(line: str) -> bool:
    """Accept a phone line made of digits and common phone punctuation."""
    value = line.strip()
    digits = re.sub(r"\D", "", value)
    non_phone_characters = re.sub(r"[\d\s+()./-]", "", value)
    return len(digits) >= 6 and not non_phone_characters


def _extract_customer_name(lines: list[str], *, ship_to_threshold: float = 80.0) -> str | None:
    """Extract a name only from the sequence: fuzzy SHIP TO, name, phone."""
    for index, line in enumerate(lines[:-2]):
        if _ship_to_score(line) < ship_to_threshold:
            continue

        name = lines[index + 1].strip(" :-")
        phone = lines[index + 2].strip()
        if name and not _looks_like_phone(name) and _looks_like_phone(phone):
            return name
    return None


def _extract_order_number(text: str) -> str | None:
    """Extract the order number after DESC and restore its leading '#'."""
    match = re.search(r"\bDESC\s*:\s*#?\s*(\d+)", text, flags=re.IGNORECASE)
    return f"#{match.group(1)}" if match else None


def parse_label_text(text: str) -> dict[str, str | None]:
    """Parse the fields needed by AutoChecker from recognized label text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "tracking_number": _normalize_tracking_number(text),
        "customer_name": _extract_customer_name(lines),
        "order_number": _extract_order_number(text),
    }


def extract_pdf_labels(
    pdf_path: str | Path,
    *,
    page_numbers: Iterable[int] | None = None,
    render_scale: float = 3.0,
    min_confidence: float = 0.45,
) -> list[dict[str, Any]]:
    """OCR label pages and return full text plus parsed fields.

    ``page_numbers`` uses human-friendly 1-based page numbers. If it is omitted,
    every page is processed. One RapidOCR model is reused for the whole PDF.
    """
    np, pymupdf, RapidOCR = _load_ocr_dependencies()
    source = Path(pdf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF не найден: {source}")
    if render_scale <= 0:
        raise ValueError("render_scale должен быть больше нуля")

    document = pymupdf.open(source)
    selected = list(page_numbers) if page_numbers is not None else list(range(1, len(document) + 1))
    invalid = [number for number in selected if number < 1 or number > len(document)]
    if invalid:
        raise ValueError(f"Страницы вне диапазона 1..{len(document)}: {invalid}")

    ocr = RapidOCR()
    pages: list[dict[str, Any]] = []
    try:
        for position, page_number in enumerate(selected, start=1):
            print(
                f"OCR: страница {page_number}/{len(document)} "
                f"({position} из {len(selected)})...",
                file=sys.stderr,
            )
            page = document[page_number - 1]
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(render_scale, render_scale), alpha=False
            )
            image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            )
            result, _elapsed = ocr(image)
            recognized = [
                item[1].strip()
                for item in (result or [])
                if item[1].strip() and float(item[2]) >= min_confidence
            ]
            full_text = "\n".join(recognized)
            pages.append(
                {
                    "page": page_number,
                    **parse_label_text(full_text),
                    "full_text": full_text,
                }
            )
    finally:
        document.close()
    return pages


def is_label_pdf_name(filename: str) -> bool:
    """Return whether a name follows DD.MM.YYYY Part N (Label).pdf."""
    match = LABEL_PDF_PATTERN.fullmatch(filename)
    if not match:
        return False
    try:
        datetime.datetime.strptime(match.group(1), "%d.%m.%Y")
    except ValueError:
        return False
    return int(match.group(2)) >= 1


def find_pending_label_pdfs(
    downloads_dir: str | Path = DOWNLOADS_DIR,
    label_date: datetime.date | None = None,
) -> list[Path]:
    """Find correctly named label PDFs which do not have sibling JSON files."""
    directory = Path(downloads_dir).expanduser().resolve()
    if not directory.is_dir():
        return []
    pending = []
    for path in directory.iterdir():
        if (
            not path.is_file()
            or not is_label_pdf_name(path.name)
            or path.with_suffix(".json").exists()
        ):
            continue
        match = LABEL_PDF_PATTERN.fullmatch(path.name)
        if (
            label_date is not None
            and match is not None
            and datetime.datetime.strptime(
                match.group(1), "%d.%m.%Y"
            ).date() != label_date
        ):
            continue
        pending.append(path)
    return sorted(pending, key=lambda path: path.name.lower())


def _json_document(pages: list[dict[str, Any]]) -> dict[str, dict[str, str | int | None]]:
    """Convert internal OCR results to the compact AutoChecker JSON schema."""
    return {
        page["tracking_number"]: {
            "PageNumber": page["page"],
            "Order Number": page["order_number"],
            "CustomerName": page["customer_name"],
        }
        for page in pages
        if page["tracking_number"]
    }


def process_label_pdf(pdf_path: str | Path) -> Path | None:
    """OCR one eligible label PDF and atomically create its sibling JSON.

    Returns the JSON path, or ``None`` when it already exists.
    """
    source = Path(pdf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF не найден: {source}")
    if not is_label_pdf_name(source.name):
        raise ValueError(
            "Неверное имя PDF. Ожидается: DD.MM.YYYY Part N (Label).pdf"
        )

    destination = source.with_suffix(".json")
    if destination.exists():
        return None

    pages = extract_pdf_labels(source)
    document = _json_document(pages)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        # Do not overwrite a JSON that may have appeared while OCR was running.
        if destination.exists():
            return None
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def process_pending_label_pdfs(
    department: str,
    downloads_dir: str | Path = DOWNLOADS_DIR,
    label_date: datetime.date | None = None,
) -> list[Path]:
    """Process pending label PDFs for RETAIL workflows."""
    if department.strip().upper() not in {"RETAIL", "RETAIL_UP"}:
        return []

    created: list[Path] = []
    for pdf_path in find_pending_label_pdfs(downloads_dir, label_date=label_date):
        result = process_label_pdf(pdf_path)
        if result is not None:
            created.append(result)
    return created


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Создать JSON для новых RETAIL PDF с UPS-этикетками"
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        type=Path,
        help="Один Label PDF; без аргумента сканируется папка Downloads",
    )
    parser.add_argument("--department", default="RETAIL")
    args = parser.parse_args()

    try:
        if args.department.strip().upper() not in {"RETAIL", "RETAIL_UP"}:
            print("OCR этикеток пропущен: department не RETAIL/RETAIL_UP")
            return 0
        if args.pdf is not None:
            result = process_label_pdf(args.pdf)
            created = [result] if result is not None else []
        else:
            created = process_pending_label_pdfs(args.department)
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    if created:
        for path in created:
            print(f"JSON создан: {path}")
    else:
        print("Новых PDF с этикетками для обработки нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

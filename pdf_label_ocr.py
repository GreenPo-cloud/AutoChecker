"""Local OCR extraction for RETAIL UPS and Packeta label PDFs."""

from __future__ import annotations

import argparse
import datetime
from difflib import SequenceMatcher
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

import portalocker


DOWNLOADS_DIR = Path.home() / "Downloads"
OCR_LOCK_FILE = Path(__file__).resolve().parent / ".pdf_label_ocr.lock"
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


def _is_packeta_label(lines: list[str]) -> bool:
    """Identify the second label layout by its standalone service line."""
    return any(line.casefold() == "packeta.com" for line in lines)


def _normalize_packeta_tracking_number(text: str) -> str | None:
    """Accept Packeta Z numbers with optional OCR whitespace after the Z."""
    normalized = re.sub(r"\s+", "", text).upper()
    return normalized if re.fullmatch(r"Z\d{3,}", normalized) else None


def _label_type_from_text(text: str) -> str | None:
    """Classify a page using the mutually exclusive label markers."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if _is_packeta_label(lines):
        return "Packeta"
    if "UPS " in text.upper():
        return "UPS"
    return None


def _parse_packeta_label(lines: list[str]) -> dict[str, str | None]:
    """Parse obj/order and the validated Packeta tracking/name sequence."""
    order_number = None
    for line in lines:
        match = re.fullmatch(r"obj\.\s*(\d+)", line, flags=re.IGNORECASE)
        if match:
            order_number = f"#{match.group(1)}"
            break

    tracking_number = None
    customer_name = None
    for index, line in enumerate(lines[:-2]):
        normalized_tracking = _normalize_packeta_tracking_number(line)
        if normalized_tracking is None:
            continue
        if lines[index + 1].casefold() != "crassula group s":
            continue
        tracking_number = normalized_tracking
        customer_name = lines[index + 2]
        break

    return {
        "tracking_number": tracking_number,
        "customer_name": customer_name,
        "order_number": order_number,
    }


def parse_label_text(text: str) -> dict[str, str | None]:
    """Parse the fields needed by AutoChecker from recognized label text."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if _is_packeta_label(lines):
        return _parse_packeta_label(lines)
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
    page_callback: Callable[[dict[str, Any]], None] | None = None,
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
            page_result = {
                "page": page_number,
                **parse_label_text(full_text),
                "full_text": full_text,
            }
            pages.append(page_result)
            if page_callback is not None:
                page_callback(page_result)
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
    """Find label PDFs whose JSON is absent, incomplete or inconsistent."""
    directory = Path(downloads_dir).expanduser().resolve()
    if not directory.is_dir():
        return []
    pending = []
    for path in directory.iterdir():
        if (
            not path.is_file()
            or not is_label_pdf_name(path.name)
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
        if not label_pdf_is_complete(path):
            pending.append(path)
    return sorted(pending, key=lambda path: path.name.lower())


def _pdf_page_count(pdf_path: Path) -> int:
    """Return a label PDF's page count without initializing the OCR model."""
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("Не установлена библиотека PyMuPDF") from exc
    document = pymupdf.open(pdf_path)
    try:
        return len(document)
    finally:
        document.close()


def _read_label_document(destination: Path) -> dict[str, Any]:
    """Read a progressive Label JSON under its writer-compatible lock."""
    if not destination.is_file():
        return {}
    with portalocker.Lock(
        str(destination),
        mode="r",
        encoding="utf-8",
        timeout=3600,
    ) as file:
        try:
            document = json.load(file)
        except json.JSONDecodeError:
            return {}
    return document if isinstance(document, dict) else {}


def _clean_label_document(
    document: dict[str, Any],
    page_count: int,
) -> dict[str, dict[str, Any]]:
    """Keep exactly one structurally valid JSON entry for each page."""
    entries_by_page: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for tracking_number, label_data in document.items():
        if not isinstance(label_data, dict):
            continue
        try:
            page_number = int(label_data.get("PageNumber"))
        except (TypeError, ValueError):
            continue
        if not 1 <= page_number <= page_count:
            continue
        entries_by_page.setdefault(page_number, []).append(
            (str(tracking_number), label_data)
        )

    cleaned: dict[str, dict[str, Any]] = {}
    for entries in entries_by_page.values():
        # Duplicate PageNumber values are ambiguous: OCR that page again.
        if len(entries) != 1:
            continue
        tracking_number, label_data = entries[0]
        if tracking_number:
            cleaned[tracking_number] = label_data
    return cleaned


def label_pdf_missing_pages(pdf_path: str | Path) -> list[int]:
    """Return 1-based pages absent from the PDF's valid Label JSON entries."""
    source = Path(pdf_path).expanduser().resolve()
    page_count = _pdf_page_count(source)
    document = _read_label_document(source.with_suffix(".json"))
    cleaned = _clean_label_document(document, page_count)
    completed_pages = {
        int(label_data["PageNumber"])
        for label_data in cleaned.values()
    }
    return [
        page_number
        for page_number in range(1, page_count + 1)
        if page_number not in completed_pages
    ]


def label_pdf_is_complete(pdf_path: str | Path) -> bool:
    """Require one and only one valid JSON entry for every PDF page."""
    source = Path(pdf_path).expanduser().resolve()
    page_count = _pdf_page_count(source)
    document = _read_label_document(source.with_suffix(".json"))
    cleaned = _clean_label_document(document, page_count)
    completed_pages = {
        int(label_data["PageNumber"])
        for label_data in cleaned.values()
    }
    return (
        len(document) == page_count
        and document == cleaned
        and completed_pages == set(range(1, page_count + 1))
    )


def _json_document(pages: list[dict[str, Any]]) -> dict[str, dict[str, str | int | None]]:
    """Convert internal OCR results to the compact AutoChecker JSON schema."""
    return {
        page["tracking_number"]: {
            "PageNumber": page["page"],
            "Order Number": page["order_number"],
            "CustomerName": page["customer_name"],
            "LabelType": _label_type_from_text(page["full_text"]),
        }
        for page in pages
        if page["tracking_number"]
    }


def _save_label_page(
    destination: Path,
    page: dict[str, Any],
) -> bool:
    """Create or update one Label JSON after a page has been recognized."""
    page_document = _json_document([page])
    if not page_document:
        return False
    if not destination.exists():
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(page_document, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
            # The first visible version is already a complete, valid JSON file.
            if not destination.exists():
                temporary.replace(destination)
                return True
        finally:
            temporary.unlink(missing_ok=True)

    # Later pages merge into the existing document under an exclusive lock.
    with portalocker.Lock(
        str(destination),
        mode="r+",
        encoding="utf-8",
        timeout=3600,
    ) as file:
        try:
            document = json.load(file)
        except json.JSONDecodeError:
            document = {}
        if not isinstance(document, dict):
            document = {}
        page_number = int(page["page"])
        document = {
            tracking_number: label_data
            for tracking_number, label_data in document.items()
            if not (
                isinstance(label_data, dict)
                and str(label_data.get("PageNumber", "")) == str(page_number)
            )
        }
        document.update(page_document)
        file.seek(0)
        file.truncate()
        json.dump(document, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    return True


def _replace_label_document(destination: Path, document: dict[str, Any]) -> None:
    """Replace an existing progressive JSON while holding its file lock."""
    with portalocker.Lock(
        str(destination),
        mode="r+",
        encoding="utf-8",
        timeout=3600,
    ) as file:
        file.seek(0)
        file.truncate()
        json.dump(document, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())


def process_label_pdf(
    pdf_path: str | Path,
    *,
    page_saved_callback: Callable[[Path, int], None] | None = None,
) -> Path | None:
    """OCR one eligible label PDF and publish each completed page immediately.

    Returns the JSON path when pages were processed, or ``None`` when complete.
    """
    source = Path(pdf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF не найден: {source}")
    if not is_label_pdf_name(source.name):
        raise ValueError(
            "Неверное имя PDF. Ожидается: DD.MM.YYYY Part N (Label).pdf"
        )

    destination = source.with_suffix(".json")
    page_count = _pdf_page_count(source)
    existing_document = _read_label_document(destination)
    cleaned_document = _clean_label_document(
        existing_document,
        page_count,
    )
    if destination.exists() and existing_document != cleaned_document:
        _replace_label_document(destination, cleaned_document)
    completed_pages = {
        int(label_data["PageNumber"])
        for label_data in cleaned_document.values()
    }
    missing_pages = [
        page_number
        for page_number in range(1, page_count + 1)
        if page_number not in completed_pages
    ]
    if not missing_pages:
        return None

    def save_page(page: dict[str, Any]) -> None:
        saved = _save_label_page(destination, page)
        if saved and page_saved_callback is not None:
            page_saved_callback(destination, int(page["page"]))

    extract_pdf_labels(
        source,
        page_numbers=missing_pages,
        page_callback=save_page,
    )
    return destination


def process_pending_label_pdfs(
    department: str,
    downloads_dir: str | Path = DOWNLOADS_DIR,
    label_date: datetime.date | None = None,
    page_saved_callback: Callable[[Path, int], None] | None = None,
) -> list[Path]:
    """Process pending label PDFs for RETAIL workflows."""
    if department.strip().upper() not in {"RETAIL", "RETAIL_UP"}:
        return []

    created: list[Path] = []
    # Multiple RETAIL_UP processes may request OCR simultaneously. Keep one
    # OCR model/process active and let later requests re-check completeness.
    with portalocker.Lock(
        str(OCR_LOCK_FILE),
        mode="a+",
        encoding="utf-8",
        timeout=3600,
    ):
        for pdf_path in find_pending_label_pdfs(
            downloads_dir,
            label_date=label_date,
        ):
            result = process_label_pdf(
                pdf_path,
                page_saved_callback=page_saved_callback,
            )
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
    parser.add_argument(
        "--downloads-dir",
        action="append",
        type=Path,
        help="Папка Downloads; параметр можно повторить",
    )
    parser.add_argument(
        "--label-date",
        type=lambda value: datetime.datetime.strptime(value, "%Y-%m-%d").date(),
        help="Обрабатывать только дату YYYY-MM-DD",
    )
    args = parser.parse_args()

    try:
        if args.department.strip().upper() not in {"RETAIL", "RETAIL_UP"}:
            print("OCR этикеток пропущен: department не RETAIL/RETAIL_UP")
            return 0
        if args.pdf is not None:
            result = process_label_pdf(args.pdf)
            created = [result] if result is not None else []
        else:
            created = []
            for downloads_dir in args.downloads_dir or [DOWNLOADS_DIR]:
                created.extend(process_pending_label_pdfs(
                    args.department,
                    downloads_dir=downloads_dir,
                    label_date=args.label_date,
                ))
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

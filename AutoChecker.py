"""AutoChecker v2: dynamic discovery of displays, cameras, printers and scanners.

Protocol received from a display (one command per line):
    Start:<name>;<department>;<camera>;<printer>;<scanner>\n
    Stop:<name>;<department>;<camera>;<printer>;<scanner>\n
The printer and scanner fields are optional for compatibility with old displays.
"""

from __future__ import annotations

import importlib
import importlib.util
import base64
import copy
import ctypes
import getpass
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import winsound
import xml.etree.ElementTree as ET


PACKAGES_INSTALLED = False
DEPENDENCY_RESTART_MARKER = "AUTOCHECKER_DEPENDENCIES_RESTARTED"


def ensure_package(module_name: str, pip_name: str | None = None) -> None:
    """Install a missing third-party module for the current Python."""
    global PACKAGES_INSTALLED
    if pip_name is None:
        pip_name = module_name

    try:
        # These modules start hardware/audio runtimes during import. Detect
        # them without starting those runtimes until the feature is used.
        if module_name in {"dymo_sdk", "pygame"}:
            if importlib.util.find_spec(module_name) is None:
                raise ImportError(module_name)
        else:
            importlib.import_module(module_name)
    except ImportError:
        print(f"Installing missing package: {pip_name}")
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            pip_name,
        ])
        print(f"{pip_name} installed")
        PACKAGES_INSTALLED = True


REQUIRED_PACKAGES = [
    ("cv2", "opencv-python"),
    ("serial", "pyserial"),
    ("pdfplumber", "pdfplumber"),
    ("send2trash", "send2trash"),
    ("numpy", "numpy"),
    ("requests", "requests"),
    ("keyboard", "keyboard"),
    ("portalocker", "portalocker"),
    ("comtypes", "comtypes>=1.4,<2"),
    ("pygrabber", "pygrabber"),
    ("pymupdf", "PyMuPDF>=1.28,<2"),
    ("rapidocr_onnxruntime", "rapidocr-onnxruntime>=1.4,<2"),
    ("esptool", "esptool>=4.8,<6"),
    ("PIL", "Pillow>=10"),
    ("qrcode", "qrcode[pil]>=7.4,<9"),
    ("dymo_sdk", "dymo-sdk"),
    ("pypdf", "pypdf>=5,<7"),
    ("pygame", "pygame>=2.6,<3"),
    ("rapidfuzz", "rapidfuzz>=3,<4"),
    ("win32print", "pywin32>=306"),
]

for required_module, required_pip_name in REQUIRED_PACKAGES:
    ensure_package(required_module, required_pip_name)

if PACKAGES_INSTALLED:
    if os.environ.get(DEPENDENCY_RESTART_MARKER) == "1":
        raise RuntimeError(
            "Installed packages are still unavailable after restart; "
            "restart Python manually"
        )
    print("* Restarting AutoChecker after package installation")
    os.environ[DEPENDENCY_RESTART_MARKER] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])
else:
    os.environ.pop(DEPENDENCY_RESTART_MARKER, None)


import json
import math
import multiprocessing
import queue
import re
import statistics
import time
import datetime
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path

import cv2
import comtypes.client
import portalocker
import pymupdf
import pywintypes
import qrcode
import requests
import serial
import win32con
import win32api
import win32event
import win32gui
import win32print
import win32process
import win32ui
from win32com.shell import shell, shellcon
from PIL import Image, ImageWin
from pypdf import PdfReader, PdfWriter
from rapidfuzz import fuzz
from send2trash import send2trash
from serial.tools import list_ports

# pygrabber otherwise asks comtypes to generate DirectShow wrappers inside
# site-packages. That directory can be read-only on a newly installed PC.
# In-memory generation is sufficient and avoids a startup PermissionError.
comtypes.client.gen_dir = None
from pygrabber.dshow_graph import FilterGraph


BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "Settings.json"
DESKTOP_DIR = Path.home() / "Desktop"

DISPLAY_BAUDRATE = 115200
SCANNER_BAUDRATE = 115200
DISPLAY_TIMEOUT = 0.1
DISPLAY_WRITE_TIMEOUT = 5.0
HANDSHAKE_TIMEOUT = 1.0
DISPLAY_BOOT_DELAY = 1.5
ESP_ROM_PREFIX = "ESP-ROM:"
DISPLAY_BOOT_TOKEN = "ESP_ROM_PREFIX"
PORT_SCAN_INTERVAL = 1.5
CAMERA_SCAN_INTERVAL = 1.5
CAMERA_STABLE_POLLS = 2
PRINTER_SCAN_INTERVAL = 1.5
PRINTER_STABLE_POLLS = 2
SCANNER_IDENTITY_TIMEOUT = 15
SCANNER_IDENTITY_RETRY_INTERVAL = 10.0
SCAN_INTERVAL = 0.2
CHANGE_PDF_SCAN_INTERVAL = 2.0
CHANGE_PDF_RETRY_INTERVAL = 10.0
ORDER_SOURCE_SCAN_INTERVAL = 2.0
ORDER_SOURCE_RETRY_INTERVAL = 10.0
PRODUCT_COUNT_MAX_AGE_DAYS = 90
PHOTO_DELAY = 2.5
B2B_INDEX_FILENAME = "B2B_Order_Index.txt"
RETAIL_ORDER_CACHE_SUFFIX = " (Order).json"
RETAIL_DELIVERY_ORDER = ("UPS", "Zasilkovna", "Postal")
RAW_CHANGE_LABEL_PATTERN = re.compile(
    r"^change\s+(#?\d+(?:\s*,\s*#?\d+)*)\.pdf$",
    re.IGNORECASE,
)
OTHER_SLOT_COUNT = 4
DEFAULT_OTHER_COLOUR = "#ffffff"
BONUS_COLOUR = "#ff8000"
CURRENT_VERSION = "3.29"

DEFAULT_MAIN_COMPUTER_NAME = "GREENPO"
SUPPORT_SHARE_NAMES = {
    "downloads": "Downloads",
    "retail": "AutoChecker_RETAIL",
    "b2b": "AutoChecker_B2B",
    "settings": "AutoChecker_Settings",
}
LEGACY_SUPPORT_SHARES = {
    "AutoChecker_Photo": DESKTOP_DIR / "RETAIL" / "Photo",
    "AutoChecker_Statistik": DESKTOP_DIR / "RETAIL" / "Statistik",
}
DEFAULT_SUPPORT_PATHS = {
    "downloads": rf"\\{DEFAULT_MAIN_COMPUTER_NAME}\{SUPPORT_SHARE_NAMES['downloads']}",
    "retail": rf"\\{DEFAULT_MAIN_COMPUTER_NAME}\{SUPPORT_SHARE_NAMES['retail']}",
    "b2b": rf"\\{DEFAULT_MAIN_COMPUTER_NAME}\{SUPPORT_SHARE_NAMES['b2b']}",
    "photos": (
        rf"\\{DEFAULT_MAIN_COMPUTER_NAME}\{SUPPORT_SHARE_NAMES['retail']}"
        r"\Photo"
    ),
    "statistics": (
        rf"\\{DEFAULT_MAIN_COMPUTER_NAME}\{SUPPORT_SHARE_NAMES['retail']}"
        r"\Statistik"
    ),
    "settings": (
        rf"\\{DEFAULT_MAIN_COMPUTER_NAME}\{SUPPORT_SHARE_NAMES['settings']}"
        r"\Settings.json"
    ),
}
SUPPORT_CATALOG_KEYS = (
    "PRODUCTS",
    "Merch",
    "OTHER",
    "B2B_ALIASES",
    "AUTO_COMPLETE_ITEMS",
    "FEM_BONUS",
    "AUTO_BONUS",
    "OTHER_BONUS",
    "REMOVE_ITEMS",
    "REMOVE_PHRASES",
    "PRODUCT_NAME_EXCEPTIONS",
    "CHECK",
)
DEFAULT_ORDER_LABEL_SETTINGS = {
    "width_mm": 70.0,
    "height_mm": 35.0,
    "offset_x_mm": 0.0,
    "offset_y_mm": 0.0,
}
SETTINGS_SCHEMA_DEFAULTS = {
    "NAME": [],
    "DEPARTMENT": [],
    "FOCUS": {},
    "DISPLAY": {},
    "SCANNER": {},
    "OTHER": {},
    "B2B_ALIASES": {},
    "AUTO_COMPLETE_ITEMS": [],
    "FEM_BONUS": {},
    "AUTO_BONUS": {},
    "OTHER_BONUS": {},
    "REMOVE_ITEMS": [],
    "REMOVE_PHRASES": [],
    "PRODUCT_NAME_EXCEPTIONS": [],
    "PRODUCTS": {},
    "Merch": {},
    "CHECK": {},
    "ORDER_LABEL": DEFAULT_ORDER_LABEL_SETTINGS,
    "support": False,
    "support_username": "",
    "main_computer_name": DEFAULT_MAIN_COMPUTER_NAME,
    "support_paths": DEFAULT_SUPPORT_PATHS,
}

SUPPORT_NETWORK_AUTH_BLOCKED = False

VERSION_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/version.txt"

PYTHON_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/AutoChecker.py"

PDF_LABEL_OCR_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/pdf_label_ocr.py"
UPS_SOUND_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
    "main/ups.mp3"
)
ORDER_LABEL_SVG_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
    "main/Etiketka.svg"
)
STEALTH_ORDER_LABEL_SVG_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
    "main/STEALTH%20Etiketka.svg"
)
DYMO_ORDER_LABEL_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
    "main/DYMO%20Etiketka.dymo"
)
DYMO_STEALTH_ORDER_LABEL_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
    "main/DYMO%20STEALTH%20Etiketka.dymo"
)

DISPLAY_FLASH_BAUDRATE = 460800
DISPLAY_UPDATE_TIMEOUT = 180
DISPLAY_RESTART_TIMEOUT = 12.0
RETAIL_UP_DEPARTMENT = "RETAIL_UP"
RETAIL_UP_PRINTER_DPI = 200
RETAIL_UP_PAPER_WIDTH_TENTHS_MM = 1016
RETAIL_UP_PAPER_HEIGHT_TENTHS_MM = 1524
RETAIL_UP_PRINT_SCALE = 1.0
ORDER_LABEL_SVG_NS = "http://www.w3.org/2000/svg"
ORDER_LABEL_TEMPLATE = BASE_DIR / "Etiketka.svg"
STEALTH_ORDER_LABEL_TEMPLATE = BASE_DIR / "STEALTH Etiketka.svg"
DYMO_ORDER_LABEL_TEMPLATE = BASE_DIR / "DYMO Etiketka.dymo"
DYMO_STEALTH_ORDER_LABEL_TEMPLATE = BASE_DIR / "DYMO STEALTH Etiketka.dymo"
DYMO_ROLL_SELECTED = 2
DYMO_PRINT_DELAY = 1.0
EXPECTED_CYD_APP_SLOTS = (
    (0x10000, 0x140000, "app0"),
    (0x150000, 0x140000, "app1"),
)
EXPECTED_BYD_APP_SLOTS = (
    (0x10000, 0x300000, "app0"),
    (0x310000, 0x300000, "app1"),
)
DISPLAY_FIRMWARE_CONFIG = {
    "CYD": {
        "version_url": (
            "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
            "main/CYD/Display_version.txt"
        ),
        "build_url": (
            "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
            "main/CYD/build/esp32.esp32.esp32s3"
        ),
        "app_filename": "Display.ino.bin",
        "partitions_filename": "Display.ino.partitions.bin",
        "chip": "esp32s3",
        "expected_app_slots": EXPECTED_CYD_APP_SLOTS,
    },
    "BYD": {
        "version_url": (
            "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
            "main/BYD/Display_version.txt"
        ),
        "build_url": (
            "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
            "main/BYD/build/esp32.esp32.esp32p4"
        ),
        "app_filename": "P4_V2_Test.ino.bin",
        "partitions_filename": "P4_V2_Test.ino.partitions.bin",
        "chip": "esp32p4",
        "expected_app_slots": EXPECTED_BYD_APP_SLOTS,
    },
}
DISPLAY_HANDSHAKE_PATTERN = re.compile(
    r"Display:([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5});"
    r"([^;\s]+)(?:;(CYD|BYD))?",
    re.IGNORECASE,
)

# The cursor makes repeated `previous=True` calls move backwards through all
# PDF parts, including parts from previous days.
PDF_HISTORY_CURSOR: Path | None = None
OTHER_CACHE_SIGNATURE: tuple[int, int] | None = None
OTHER_CACHE_RULES: list[tuple[str, str]] = []
DISPLAY_FIRMWARE_CACHE: dict[str, tuple[bytes, bytes]] = {}
SCANNER_IDENTITY_RETRY_PORTS: set[str] = set()


def _version_key(version: str) -> tuple[int, ...] | None:
    """Convert versions such as 1.5 or v2.0.1 into comparable tuples."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip(), re.IGNORECASE)
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def check_for_updates() -> None:
    """Download and install a newer AutoChecker version when available."""
    try:
        response = requests.get(VERSION_URL, timeout=5)
        if response.status_code != 200:
            return

        latest_version = response.text.strip()
        current_key = _version_key(CURRENT_VERSION)
        latest_key = _version_key(latest_version)
        if latest_key is None:
            print(f"XXX Invalid update version: {latest_version!r}")
            return
        if current_key is not None and latest_key <= current_key:
            print("* Latest version")
            return

        print(f"* New version found: {latest_version}")
        update_program()
    except Exception as error:
        print(f"XXX Update check failed: {error}")


def update_program() -> None:
    """Download and install one validated AutoChecker code/asset bundle."""
    try:
        current_file = Path(__file__).resolve()
        update_files = (
            (current_file, PYTHON_URL, True),
            (BASE_DIR / "pdf_label_ocr.py", PDF_LABEL_OCR_URL, True),
            (ORDER_LABEL_TEMPLATE, ORDER_LABEL_SVG_URL, False),
            (
                STEALTH_ORDER_LABEL_TEMPLATE,
                STEALTH_ORDER_LABEL_SVG_URL,
                False,
            ),
        )
        downloaded_files: list[tuple[Path, bytes]] = []
        for target_file, url, validate_python in update_files:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                print(f"XXX Cannot download update file: {target_file.name}")
                return
            downloaded_data = response.content
            if validate_python:
                downloaded_code = downloaded_data.decode("utf-8-sig")
                # Validate the complete bundle before writing any temporary
                # file, so incompatible partial downloads are never installed.
                compile(downloaded_code, url, "exec")
                downloaded_data = downloaded_code.encode("utf-8")
            elif target_file.suffix.casefold() == ".svg":
                # Do not install a broken or incomplete label template.
                ET.fromstring(downloaded_data)
            downloaded_files.append((target_file, downloaded_data))

        temp_files = []
        for target_file, downloaded_data in downloaded_files:
            temp_file = Path(str(target_file) + ".new")
            temp_file.write_bytes(downloaded_data)
            temp_files.append((temp_file, target_file))

        bat_path = Path(str(current_file) + ".bat")
        commands = ["@echo off", "timeout /t 2 >nul"]
        commands.extend(
            f'move /Y "{temp_file}" "{target_file}"'
            for temp_file, target_file in temp_files
        )
        commands.extend((
            f'start "" "{sys.executable}" "{current_file}"',
            'del "%~f0"',
        ))
        bat_path.write_text("\n".join(commands) + "\n", encoding="utf-8")

        print("* Updating program...")
        os.startfile(str(bat_path))
        raise SystemExit
    except SystemExit:
        raise
    except Exception as error:
        print(f"XXX Update failed: {error}")


def sync_order_label_templates() -> None:
    """Keep SVG and published DYMO templates in sync after every update."""
    templates = (
        (ORDER_LABEL_TEMPLATE, ORDER_LABEL_SVG_URL, False),
        (STEALTH_ORDER_LABEL_TEMPLATE, STEALTH_ORDER_LABEL_SVG_URL, False),
        (DYMO_ORDER_LABEL_TEMPLATE, DYMO_ORDER_LABEL_URL, True),
        (
            DYMO_STEALTH_ORDER_LABEL_TEMPLATE,
            DYMO_STEALTH_ORDER_LABEL_URL,
            True,
        ),
    )
    temporary_files: list[Path] = []
    try:
        downloaded_templates: list[tuple[Path, bytes]] = []
        for target_file, url, optional in templates:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                if optional:
                    if not target_file.is_file():
                        print(
                            f"? Optional DYMO template is not published: "
                            f"{target_file.name}"
                        )
                    continue
                raise RuntimeError(
                    f"cannot download {target_file.name}: HTTP "
                    f"{response.status_code}"
                )
            downloaded_data = response.content
            ET.fromstring(downloaded_data)
            downloaded_templates.append((target_file, downloaded_data))

        changed_templates = [
            (target_file, downloaded_data)
            for target_file, downloaded_data in downloaded_templates
            if (
                not target_file.is_file()
                or target_file.read_bytes() != downloaded_data
            )
        ]
        for target_file, downloaded_data in changed_templates:
            temporary_file = target_file.with_name(
                target_file.name + ".sync"
            )
            temporary_file.write_bytes(downloaded_data)
            temporary_files.append(temporary_file)

        for temporary_file, (target_file, _downloaded_data) in zip(
            temporary_files,
            changed_templates,
        ):
            os.replace(temporary_file, target_file)

        if changed_templates:
            names = ", ".join(
                target_file.name for target_file, _data in changed_templates
            )
            print(f"* Order label templates synchronized: {names}")
    except Exception as error:
        print(f"XXX Order label template synchronization failed: {error}")
    finally:
        for temporary_file in temporary_files:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass


def load_label_ocr_processor():
    """Import the OCR module lazily, after the launcher update check."""
    from pdf_label_ocr import process_pending_label_pdfs

    return process_pending_label_pdfs


def get_latest_display_versions() -> dict[str, str]:
    """Return published firmware versions keyed by CYD/BYD display type."""
    versions: dict[str, str] = {}
    for display_type, config in DISPLAY_FIRMWARE_CONFIG.items():
        try:
            response = requests.get(str(config["version_url"]), timeout=5)
            response.raise_for_status()
            version = response.text.strip()
            if _version_key(version) is None:
                print(
                    f"XXX Invalid {display_type} display update version: "
                    f"{version!r}"
                )
                continue
            versions[display_type] = version
            print(f"* Latest {display_type} display firmware version: {version}")
        except requests.RequestException as error:
            print(f"XXX {display_type} display update check failed: {error}")
    return versions


def normalise_display_mac(value: str) -> str | None:
    """Return a stable uppercase colon-separated ESP32 MAC address."""
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != 12:
        return None
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2)).upper()


def _download_display_firmware(display_type: str) -> tuple[bytes, bytes]:
    """Download and validate one display type's firmware artifacts once."""
    display_type = display_type.upper()
    config = DISPLAY_FIRMWARE_CONFIG.get(display_type)
    if config is None:
        raise RuntimeError(f"Unsupported display type: {display_type}")
    if display_type in DISPLAY_FIRMWARE_CACHE:
        return DISPLAY_FIRMWARE_CACHE[display_type]

    downloaded: list[bytes] = []
    filenames = (
        str(config["app_filename"]),
        str(config["partitions_filename"]),
    )
    for filename in filenames:
        response = requests.get(f"{config['build_url']}/{filename}", timeout=30)
        response.raise_for_status()
        if not response.content:
            raise RuntimeError(f"Downloaded firmware file is empty: {filename}")
        downloaded.append(response.content)

    application, partitions = downloaded
    if application[0] != 0xE9:
        raise RuntimeError(
            f"{config['app_filename']} is not an ESP application image"
        )
    if len(partitions) < 32 or partitions[:2] != b"\xAA\x50":
        raise RuntimeError("Display partition table has an invalid format")

    DISPLAY_FIRMWARE_CACHE[display_type] = application, partitions
    return DISPLAY_FIRMWARE_CACHE[display_type]


def _application_partitions(partitions: bytes) -> list[tuple[int, int, str]]:
    """Extract application offsets and sizes from an ESP partition-table BIN."""
    result: list[tuple[int, int, str]] = []
    for position in range(0, len(partitions) - 31, 32):
        entry = partitions[position:position + 32]
        if entry[:2] != b"\xAA\x50":
            break
        if entry[2] != 0x00:
            continue
        offset = int.from_bytes(entry[4:8], "little")
        size = int.from_bytes(entry[8:12], "little")
        label = entry[12:28].split(b"\0", 1)[0].decode("ascii", errors="replace")
        if offset > 0 and size > 0:
            result.append((offset, size, label))
    return result


def update_display_firmware(port: str, display_mac: str, display_type: str,
                            latest_version: str) -> bool:
    """Flash the application into every app slot without erasing NVS."""
    try:
        display_type = display_type.upper()
        config = DISPLAY_FIRMWARE_CONFIG.get(display_type)
        if config is None:
            raise RuntimeError(f"Unsupported display type: {display_type}")

        application, partition_data = _download_display_firmware(display_type)
        app_slots = _application_partitions(partition_data)
        if not app_slots:
            raise RuntimeError("No application slots found in partition table")
        expected_app_slots = config["expected_app_slots"]
        if expected_app_slots is not None and tuple(app_slots) != expected_app_slots:
            raise RuntimeError(
                f"{display_type} display partition layout changed; perform one "
                "manual full flash before enabling automatic updates"
            )
        oversized = [label for _, size, label in app_slots if len(application) > size]
        if oversized:
            raise RuntimeError(
                "Display firmware does not fit app slot(s): " + ", ".join(oversized)
            )

        with tempfile.TemporaryDirectory(prefix="AutoChecker-display-") as temp_dir:
            firmware_path = Path(temp_dir) / str(config["app_filename"])
            firmware_path.write_bytes(application)
            command = [
                sys.executable,
                "-m",
                "esptool",
                "--chip",
                str(config["chip"]),
                "--port",
                port,
                "--baud",
                str(DISPLAY_FLASH_BAUDRATE),
                "--before",
                "default-reset",
                "--after",
                "hard-reset",
                "write-flash",
            ]
            for offset, _size, _label in app_slots:
                command.extend((hex(offset), str(firmware_path)))

            print(
                f"* Updating {display_type} display {display_mac} on {port} "
                f"to firmware {latest_version}"
            )
            subprocess.run(command, check=True, timeout=DISPLAY_UPDATE_TIMEOUT)
        print(f"* {display_type} display {display_mac} firmware update completed")
        return True
    except (requests.RequestException, OSError, subprocess.SubprocessError,
            RuntimeError) as error:
        print(
            f"XXX {display_type} display {display_mac} firmware update failed: "
            f"{error}"
        )
        return False


def run_pending_label_ocr(
    department: str,
    ready_event=None,
    downloads_dirs=None,
    label_date: datetime.date | None = None,
) -> None:
    """Create missing label JSON files in an isolated, serialized process."""
    try:
        process_pending_label_pdfs = load_label_ocr_processor()

        def page_saved(_json_path: Path, _page_number: int) -> None:
            if ready_event is not None:
                ready_event.set()

        created_json_files = []
        if downloads_dirs is None:
            created_json_files.extend(process_pending_label_pdfs(
                department,
                label_date=label_date,
                page_saved_callback=page_saved,
            ))
        else:
            for downloads_dir in downloads_dirs:
                created_json_files.extend(process_pending_label_pdfs(
                    department,
                    downloads_dir=downloads_dir,
                    label_date=label_date,
                    page_saved_callback=page_saved,
                ))
        if created_json_files:
            print(
                "* Label OCR created: "
                + ", ".join(path.name for path in created_json_files)
            )
        else:
            print(f"* Label OCR: no pending PDFs for {department}")
    except Exception as error:
        # OCR errors must never stop the launcher or an active checker.
        print(f"XXX Label OCR failed for {department}: {error}")


def start_label_ocr_process(
    department: str,
    ready_event=None,
    downloads_dirs=None,
    label_date: datetime.date | None = None,
) -> subprocess.Popen:
    """Start OCR independently so its checker process may safely exit."""
    command = [
        sys.executable,
        str(BASE_DIR / "pdf_label_ocr.py"),
        "--department",
        department,
    ]
    for downloads_dir in downloads_dirs or []:
        command.extend(("--downloads-dir", str(downloads_dir)))
    if label_date is not None:
        command.extend(("--label-date", label_date.isoformat()))

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        start_new_session = True
    log_path = Path(tempfile.gettempdir()) / "AutoChecker_pdf_label_ocr.log"
    log_file = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    finally:
        log_file.close()
    print(f"* Label OCR process {process.pid} started; log: {log_path}")
    return process


def label_ocr_process_is_alive(process) -> bool:
    """Return whether a detached OCR subprocess is still running."""
    return process is not None and process.poll() is None


def join_finished_label_ocr_process(process) -> None:
    """Release OS handles only after OCR has finished; never terminate it."""
    if process is None:
        return
    if process.poll() is not None:
        process.wait()


@dataclass(frozen=True)
class CheckerIdentity:
    name: str
    department: str
    camera_number: int | None
    printer_name: str | None = None
    scanner_name: str | None = None


def load_settings() -> dict:
    with portalocker.Lock(str(SETTINGS_FILE), mode="r", encoding="utf-8", timeout=30) as file:
        return json.load(file)


def save_settings(settings: dict) -> None:
    """Write Settings.json while excluding concurrent child-process updates."""
    with portalocker.Lock(str(SETTINGS_FILE), mode="r+", encoding="utf-8", timeout=30) as file:
        file.seek(0)
        file.truncate()
        json.dump(settings, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()


def close_serial_safely(display) -> None:
    """Close a serial object without propagating errors from a detached USB device."""
    if display is None:
        return
    try:
        display.close()
    except (serial.SerialException, OSError):
        pass


def update_display_last_identity(display_mac: str, identity: CheckerIdentity) -> None:
    """Persist the last name, department, camera, printer and scanner."""
    with portalocker.Lock(str(SETTINGS_FILE), mode="r+", encoding="utf-8", timeout=30) as file:
        file.seek(0)
        settings = json.load(file)
        settings.setdefault("DISPLAY", {})[display_mac] = [
            identity.name,
            identity.department,
            identity.camera_number if identity.camera_number is not None else "",
            identity.printer_name or "",
            identity.scanner_name or "",
        ]
        file.seek(0)
        file.truncate()
        json.dump(settings, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()


def ensure_other_slots(settings: dict) -> tuple[dict, bool]:
    """Return four stable OTHER slots, migrating a legacy colour:text mapping."""
    original = settings.get("OTHER")
    legacy_values: list[tuple[str, str]] = []
    if isinstance(original, dict):
        for colour, text in original.items():
            if re.fullmatch(r"#[0-9a-fA-F]{6}", str(colour)):
                legacy_values.append((str(colour).lower(), str(text)))

    slots = {}
    for index in range(1, OTHER_SLOT_COUNT + 1):
        slot_key = str(index)
        current = original.get(slot_key) if isinstance(original, dict) else None
        if isinstance(current, dict):
            colour = str(current.get("color", DEFAULT_OTHER_COLOUR)).strip().lower()
            text = str(current.get("text", ""))
        elif index <= len(legacy_values):
            colour, text = legacy_values[index - 1]
        else:
            colour, text = DEFAULT_OTHER_COLOUR, ""

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
            colour = DEFAULT_OTHER_COLOUR
        slots[slot_key] = {"color": colour, "text": text}

    changed = original != slots
    settings["OTHER"] = slots
    return slots, changed


def migrate_trial_packs_to_standard_products(settings: dict) -> bool:
    """Remove the legacy Trial Pack rule and package size from catalog names."""
    changed = False
    exceptions = settings.get("PRODUCT_NAME_EXCEPTIONS")
    if isinstance(exceptions, list):
        filtered = [
            fragment
            for fragment in exceptions
            if str(fragment).strip().casefold() != "trial pack"
        ]
        if filtered != exceptions:
            settings["PRODUCT_NAME_EXCEPTIONS"] = filtered
            changed = True

    products = settings.get("PRODUCTS")
    if isinstance(products, dict):
        for product_code, product_name in list(products.items()):
            if "trial pack" not in str(product_name).casefold():
                continue
            normalized_name = re.sub(
                r"\s*-\s*\d+(?:\+\d+)?\s*fem\s*$",
                "",
                str(product_name),
                flags=re.IGNORECASE,
            ).strip()
            if normalized_name != product_name:
                products[product_code] = normalized_name
                changed = True
    return changed


def ensure_settings_schema() -> dict:
    """Add settings introduced by newer releases without replacing user data."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(
        str(SETTINGS_FILE), mode="a+", encoding="utf-8", timeout=30
    ) as file:
        file.seek(0)
        raw = file.read().strip()
        settings = json.loads(raw) if raw else {}
        if not isinstance(settings, dict):
            raise ValueError("Settings.json must contain a JSON object")

        changed = False
        # Sound files are local replaceable assets. Older releases stored
        # their paths in Settings.json; remove that obsolete setting once.
        if "SOUNDS" in settings:
            del settings["SOUNDS"]
            changed = True
        for key, default_value in SETTINGS_SCHEMA_DEFAULTS.items():
            if key not in settings:
                settings[key] = copy.deepcopy(default_value)
                changed = True

        main_computer_name = normalized_main_computer_name(
            settings.get("main_computer_name", DEFAULT_MAIN_COMPUTER_NAME)
        )
        if settings.get("main_computer_name") != main_computer_name:
            settings["main_computer_name"] = main_computer_name
            changed = True

        expected_support_paths = support_paths_for_computer(main_computer_name)
        support_paths = settings.get("support_paths")
        if not isinstance(support_paths, dict):
            settings["support_paths"] = expected_support_paths
            changed = True
        else:
            for key, expected_path in expected_support_paths.items():
                if support_paths.get(key) != expected_path:
                    support_paths[key] = expected_path
                    changed = True

        order_label = settings.get("ORDER_LABEL")
        if not isinstance(order_label, dict):
            order_label = copy.deepcopy(DEFAULT_ORDER_LABEL_SETTINGS)
            settings["ORDER_LABEL"] = order_label
            changed = True
        else:
            for key, default_value in DEFAULT_ORDER_LABEL_SETTINGS.items():
                if key not in order_label:
                    order_label[key] = default_value
                    changed = True

        _slots, other_changed = ensure_other_slots(settings)
        changed = changed or other_changed

        trial_pack_changed = migrate_trial_packs_to_standard_products(settings)
        changed = changed or trial_pack_changed

        departments = settings.get("DEPARTMENT")
        if isinstance(departments, list) and RETAIL_UP_DEPARTMENT not in departments:
            departments.append(RETAIL_UP_DEPARTMENT)
            changed = True

        if changed:
            file.seek(0)
            file.truncate()
            json.dump(settings, file, ensure_ascii=False, indent=4)
            file.write("\n")
            file.flush()
        return settings


def get_other_colour_rules() -> list[tuple[str, str]]:
    """Read OTHER rules only when Settings.json has changed."""
    global OTHER_CACHE_SIGNATURE, OTHER_CACHE_RULES

    try:
        stat = SETTINGS_FILE.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != OTHER_CACHE_SIGNATURE:
            settings = load_settings()
            slots, _ = ensure_other_slots(settings)
            OTHER_CACHE_RULES = [
                (slots[str(index)]["color"], slots[str(index)]["text"])
                for index in range(1, OTHER_SLOT_COUNT + 1)
            ]
            OTHER_CACHE_SIGNATURE = signature
    except (OSError, ValueError, json.JSONDecodeError, portalocker.exceptions.LockException):
        return []

    return OTHER_CACHE_RULES


def add_setting_value(selector: str, value: str, key: str) -> tuple[bool, str]:
    """Apply one ADD:<selector>;<value>;<key> command to Settings.json."""
    # The display uses `/` as a convenient input character, while settings
    # names use `|`. Apply this to every ADD parameter in one common place so
    # launcher and worker commands behave identically. A Merch key beginning
    # with https: is a URL, so its slashes must remain untouched.
    keep_url_key = (
        selector.strip().casefold() == "merch"
        and key.strip().casefold().startswith("https:")
    )
    selector = selector.replace("/", "|").strip()
    raw_value = value.replace("/", "|")
    value = raw_value.strip()
    key = key.strip() if keep_url_key else key.replace("/", "|").strip()

    with portalocker.Lock(str(SETTINGS_FILE), mode="r+", encoding="utf-8", timeout=30) as file:
        file.seek(0)
        settings = json.load(file)

        camera_match = re.fullmatch(
            r"Camera\s+(\d+)\s+focus:\s*.+",
            selector,
            re.IGNORECASE,
        )
        if camera_match:
            if not key:
                return False, "Focus value is empty"
            try:
                new_focus = float(key) if "." in key else int(key)
            except ValueError:
                return False, f"Invalid focus value: {key}"
            camera_number = camera_match.group(1)
            settings.setdefault("FOCUS", {})[camera_number] = new_focus

        elif other_match := re.fullmatch(r"OTHER\s+([1-4])", selector, re.IGNORECASE):
            slots, _ = ensure_other_slots(settings)
            # Spaces can be a meaningful part of a matching fragment, e.g. " AK ".
            slots[other_match.group(1)]["text"] = raw_value

        elif selector == "NAME":
            if not value:
                return False, "Name is empty"
            names = settings.setdefault("NAME", [])
            if not isinstance(names, list):
                return False, "NAME is not a list"
            if value not in names:
                names.append(value)

        else:
            actual_name = next(
                (name for name in settings if name.casefold() == selector.casefold()),
                None,
            )
            if actual_name is None:
                return False, f"Unknown settings dictionary: {selector}"
            target = settings[actual_name]
            if not isinstance(target, dict):
                return False, f"{actual_name} is not a dictionary"
            if not key:
                return False, "Key is empty"

            # Numeric PRODUCTS scan codes consist of a five-digit product ID
            # followed by package size. Store only the product ID and remove
            # that size from the catalog name. Textual product keys retain
            # their complete key and value.
            if actual_name.casefold() == "products" and key.isdigit():
                key = key[:5]
                value = re.sub(
                    r"\s+-\s+\d+\s+fem\s*$",
                    "",
                    value,
                    flags=re.IGNORECASE,
                ).rstrip()
            target[key] = value

        file.seek(0)
        file.truncate()
        json.dump(settings, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
        return True, ""


def apply_other_colours(colours: list[str]) -> tuple[bool, str]:
    """Replace the four OTHER colours in slot order while preserving their texts."""
    if len(colours) != OTHER_SLOT_COUNT:
        return False, "APPLY command must contain exactly four colours"

    normalised = [colour.strip().lower() for colour in colours]
    invalid = next(
        (colour for colour in normalised if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour)),
        None,
    )
    if invalid is not None:
        return False, f"Invalid colour: {invalid or '<empty>'}"

    with portalocker.Lock(str(SETTINGS_FILE), mode="r+", encoding="utf-8", timeout=30) as file:
        file.seek(0)
        settings = json.load(file)
        slots, _ = ensure_other_slots(settings)
        for index, colour in enumerate(normalised, start=1):
            slots[str(index)]["color"] = colour

        file.seek(0)
        file.truncate()
        json.dump(settings, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
    return True, ""


def ensure_detected_settings(cameras: dict[int, int], displays: dict[str, str]) -> dict:
    """Create default settings for every newly discovered camera/display."""
    settings = ensure_settings_schema()
    focus = settings.setdefault("FOCUS", {})
    display_settings = settings.setdefault("DISPLAY", {})
    _, changed = ensure_other_slots(settings)
    if not isinstance(settings.get("SCANNER"), dict):
        settings["SCANNER"] = {}
        changed = True

    # Migrate the former global PRINTER.RETAIL_UP value to the per-display
    # last selection, then remove the obsolete dictionary.
    legacy_printers = settings.pop("PRINTER", None)
    legacy_printer = ""
    if isinstance(legacy_printers, dict):
        configured = legacy_printers.get(RETAIL_UP_DEPARTMENT, [])
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, list):
            legacy_printer = next(
                (
                    str(printer).strip()
                    for printer in configured
                    if str(printer).strip()
                ),
                "",
            )
        changed = True

    departments = settings.setdefault("DEPARTMENT", [])
    if (
        isinstance(departments, list)
        and RETAIL_UP_DEPARTMENT not in departments
    ):
        departments.append(RETAIL_UP_DEPARTMENT)
        changed = True

    if settings.get("CHECK") is None:
        settings["CHECK"] = {}
        changed = True

    for camera_number in cameras:
        if str(camera_number) not in focus:
            focus[str(camera_number)] = 540
            changed = True

    for display_mac, values in list(display_settings.items()):
        if not isinstance(values, list):
            values = ["", "", "", "", ""]
        else:
            values = (values + ["", "", "", "", ""])[:5]
        if (
            not values[3]
            and str(values[1]).upper() == RETAIL_UP_DEPARTMENT
            and legacy_printer
        ):
            values[3] = legacy_printer
        if display_settings.get(display_mac) != values:
            display_settings[display_mac] = values
            changed = True

    for display_mac in displays:
        if display_mac not in display_settings:
            display_settings[display_mac] = ["", "", "", "", ""]
            changed = True

    if changed:
        save_settings(settings)
    return settings


def is_display_boot_message(line: str) -> bool:
    """Return True for a real ESP-ROM line or the BYD reset token."""
    return line == DISPLAY_BOOT_TOKEN or line.startswith(ESP_ROM_PREFIX)


def _probe_autochecker_display(port: str) -> tuple[str, str, str] | None:
    """Ask one COM device for its MAC, firmware version, and CYD/BYD type."""
    display = None
    try:
        print(f"? Checking display port: {port}")
        display = serial.Serial(
            port=port,
            baudrate=DISPLAY_BAUDRATE,
            timeout=HANDSHAKE_TIMEOUT,
            write_timeout=HANDSHAKE_TIMEOUT,
        )
        # Opening an Arduino COM port often toggles DTR and restarts it.
        # Wait for its sketch to start before sending the identification request.
        time.sleep(DISPLAY_BOOT_DELAY)
        display.reset_input_buffer()
        display.write(b"<WHO?>\n")
        display.flush()
        who_attempt = 1
        max_who_attempts = 3

        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        answers: list[str] = []
        while time.monotonic() < deadline:
            answer = display.readline().decode("utf-8", errors="ignore").strip()
            if not answer:
                continue
            answers.append(answer)
            if is_display_boot_message(answer):
                if who_attempt >= max_who_attempts:
                    break
                print(
                    f"? {port} restarted during <WHO?>; waiting and "
                    "asking again"
                )
                time.sleep(DISPLAY_BOOT_DELAY)
                display.reset_input_buffer()
                display.write(b"<WHO?>\n")
                display.flush()
                who_attempt += 1
                deadline = time.monotonic() + HANDSHAKE_TIMEOUT
                continue
            match = DISPLAY_HANDSHAKE_PATTERN.fullmatch(answer)
            if match:
                display_mac = normalise_display_mac(match.group(1))
                if display_mac is None:
                    break
                version = match.group(2).strip()
                if _version_key(version) is None:
                    print(f"XXX {port} returned invalid firmware version: {version!r}")
                    return None
                display_type = (match.group(3) or "CYD").upper()
                if match.group(3) is None:
                    print(
                        f"? Display {display_mac} uses the two-field legacy "
                        "handshake; treating it as CYD"
                    )
                print(
                    f"* {display_type} display found: {display_mac} ({port}), "
                    f"firmware {version}"
                )
                return display_mac, version, display_type

            legacy = re.fullmatch(r"AutoChecker\s+(\d+)", answer, re.IGNORECASE)
            if legacy:
                print(
                    f"XXX Display on {port} uses the old AutoChecker "
                    f"{legacy.group(1)} handshake; flash firmware that replies "
                    "Display:{MAC};{version};{CYD|BYD}"
                )
                return None
        if answers:
            print(f"? {port} replied: {answers[-1]!r}")
        else:
            print(f"? {port} did not answer <WHO?>")
    except (serial.SerialException, OSError) as error:
        print(f"? Cannot check {port}: {error}")
    finally:
        close_serial_safely(display)
    return None


def identify_autochecker_display(
    port: str,
    latest_versions: dict[str, str],
) -> str | None:
    """Identify a display, update old firmware, and return its stable MAC."""
    identity = _probe_autochecker_display(port)
    if identity is None:
        return None

    display_mac, installed_version, display_type = identity
    latest_version = latest_versions.get(display_type)
    installed_key = _version_key(installed_version)
    latest_key = _version_key(latest_version) if latest_version is not None else None
    if (
        installed_key is not None
        and latest_key is not None
        and latest_key > installed_key
    ):
        if update_display_firmware(
            port,
            display_mac,
            display_type,
            latest_version,
        ):
            # esptool resets the board. Confirm both its identity and the version
            # before the launcher hands the port to normal serial processing.
            deadline = time.monotonic() + DISPLAY_RESTART_TIMEOUT
            updated_identity = None
            while time.monotonic() < deadline and updated_identity is None:
                try:
                    updated_identity = _probe_autochecker_display(port)
                except Exception:
                    updated_identity = None
                if updated_identity is None:
                    time.sleep(0.5)
            if updated_identity is None:
                print(
                    f"XXX Display {display_mac} did not answer after firmware update"
                )
                return None
            updated_mac, updated_version, updated_type = updated_identity
            if updated_mac != display_mac:
                print(
                    f"XXX Display MAC changed after update: "
                    f"{display_mac} -> {updated_mac}"
                )
                return None
            if updated_type != display_type:
                print(
                    f"XXX Display type changed after update: "
                    f"{display_type} -> {updated_type}"
                )
                return None
            if _version_key(updated_version) < latest_key:
                print(
                    f"XXX Display {display_mac} still reports firmware "
                    f"{updated_version}; expected {latest_version}"
                )
            else:
                print(
                    f"* Display {display_mac} now uses firmware {updated_version}"
                )
    return display_mac


def find_autochecker_displays(
    latest_versions: dict[str, str],
) -> dict[str, str]:
    """Return {display MAC: COM port} from the <WHO?> handshake."""
    displays: dict[str, str] = {}
    for port_info in list_ports.comports():
        display_mac = identify_autochecker_display(
            port_info.device,
            latest_versions,
        )
        if display_mac is not None:
            displays[display_mac] = port_info.device
    return displays


def read_windows_port_identity(port_info) -> tuple[str, str]:
    """Return (bus-reported description, serial number) for one COM device."""
    description = str(
        getattr(port_info, "product", "")
        or getattr(port_info, "description", "")
        or ""
    ).strip()
    serial_number = str(getattr(port_info, "serial_number", "") or "").strip()
    if os.name != "nt":
        return description, serial_number
    description_key = description.casefold()
    if serial_number and all(
        token in description_key for token in ("scanner", "bar", "code")
    ):
        # pyserial already supplied everything needed. Avoid a comparatively
        # expensive PowerShell/PnP walk on the common path.
        return description, serial_number

    port_name = str(port_info.device)
    script = r"""
$portName = $env:AUTOCHECKER_SCANNER_PORT
$device = Get-PnpDevice -Class Ports -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object { $_.FriendlyName -like "*($portName)*" } |
    Select-Object -First 1
if ($null -eq $device) { exit 2 }
$leafInstanceId = ($device.InstanceId -split '\\')[-1]
$current = $device.InstanceId
$busDescription = ""
$serialNumber = ""
for ($index = 0; $index -lt 6 -and $current; $index++) {
    if (-not $busDescription) {
        $value = Get-PnpDeviceProperty -InstanceId $current `
            -KeyName 'DEVPKEY_Device_BusReportedDeviceDesc' `
            -ErrorAction SilentlyContinue
        if ($null -ne $value.Data) { $busDescription = [string]$value.Data }
    }
    if (-not $serialNumber) {
        $value = Get-PnpDeviceProperty -InstanceId $current `
            -KeyName 'DEVPKEY_Device_SerialNumber' `
            -ErrorAction SilentlyContinue
        if ($null -ne $value.Data) { $serialNumber = [string]$value.Data }
    }
    if ($busDescription -and $serialNumber) { break }
    $parent = Get-PnpDeviceProperty -InstanceId $current `
        -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue
    $current = if ($null -ne $parent.Data) { [string]$parent.Data } else { "" }
}
if (-not $serialNumber -and $leafInstanceId) {
    $candidate = [string]$leafInstanceId
    if ($candidate -match '^S/N:(.+)$') {
        $candidate = [string]$Matches[1]
        $candidate = $candidate -replace ':[A-Za-z]$', ''
    }
    # Values such as 5&315b279c&0&0 are USB topology paths, not stable
    # device serial numbers, and must not be persisted as scanner IDs.
    if ($candidate -and $candidate -notmatch '^\d+&') {
        $serialNumber = $candidate
    }
}
[PSCustomObject]@{
    description = $busDescription
    serial_number = $serialNumber
} | ConvertTo-Json -Compress
"""
    environment = os.environ.copy()
    environment["AUTOCHECKER_SCANNER_PORT"] = port_name
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCANNER_IDENTITY_TIMEOUT,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=environment,
        )
        SCANNER_IDENTITY_RETRY_PORTS.discard(port_name)
        if result.returncode == 0 and result.stdout.strip():
            properties = json.loads(result.stdout.strip().lstrip("\ufeff"))
            description = str(
                properties.get("description") or description
            ).strip()
            serial_number = str(
                properties.get("serial_number") or serial_number
            ).strip()
    except subprocess.TimeoutExpired:
        SCANNER_IDENTITY_RETRY_PORTS.add(port_name)
        print(
            f"? PnP query for {port_name} timed out after "
            f"{SCANNER_IDENTITY_TIMEOUT} seconds; scanner detection will retry"
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        SCANNER_IDENTITY_RETRY_PORTS.add(port_name)
        print(f"? Cannot read PnP properties for {port_name}: {error}")
    return description, serial_number


def identify_external_scanner(port_info, *, announce: bool = True) -> str | None:
    """Return/register Scanner N when a COM device is a configured scanner."""
    description, serial_number = read_windows_port_identity(port_info)
    if not serial_number:
        return None

    with portalocker.Lock(
        str(SETTINGS_FILE), mode="r+", encoding="utf-8", timeout=30
    ) as file:
        file.seek(0)
        settings = json.load(file)
        scanner_settings = settings.setdefault("SCANNER", {})
        if not isinstance(scanner_settings, dict):
            scanner_settings = {}
            settings["SCANNER"] = scanner_settings

        stored_serial = next(
            (
                key for key in scanner_settings
                if str(key).casefold() == serial_number.casefold()
            ),
            None,
        )
        changed = False
        if stored_serial is not None and str(scanner_settings[stored_serial]).strip():
            scanner_name = str(scanner_settings[stored_serial]).strip()
        else:
            description_key = description.casefold()
            if not all(
                token in description_key for token in ("scanner", "bar", "code")
            ):
                return None
            used_numbers = [
                int(match.group(1))
                for value in scanner_settings.values()
                if (
                    match := re.fullmatch(
                        r"Scanner\s+(\d+)", str(value).strip(), re.IGNORECASE
                    )
                )
            ]
            scanner_name = f"Scanner {max(used_numbers, default=0) + 1}"
            scanner_settings[serial_number] = scanner_name
            changed = True

        if changed:
            file.seek(0)
            file.truncate()
            json.dump(settings, file, ensure_ascii=False, indent=4)
            file.write("\n")
            file.flush()

    if announce:
        print(
            f"* Scanner found: {scanner_name} ({port_info.device}), "
            f"serial {serial_number}, {description or 'no description'}"
        )
    SCANNER_IDENTITY_RETRY_PORTS.discard(str(port_info.device))
    return scanner_name


def find_external_scanners(
    excluded_ports: set[str] | None = None,
    *,
    announce: bool = True,
) -> dict[str, str]:
    """Return {Scanner N: COM port}, excluding identified displays."""
    excluded = excluded_ports or set()
    scanners: dict[str, str] = {}
    for port_info in list_ports.comports():
        if port_info.device in excluded:
            continue
        scanner_name = identify_external_scanner(port_info, announce=announce)
        if scanner_name is None:
            continue
        duplicate = next(
            (name for name in scanners if name.casefold() == scanner_name.casefold()),
            None,
        )
        if duplicate is not None:
            print(
                f"XXX Duplicate {scanner_name} on {port_info.device}; "
                f"already using {scanners[duplicate]}"
            )
            continue
        scanners[scanner_name] = port_info.device
    return scanners


def find_autochecker_cameras(*, announce: bool = True) -> dict[int, int]:
    """Return {AutoChecker number: OpenCV DirectShow index}."""
    graph = FilterGraph()
    cameras: dict[int, int] = {}

    for camera_id, camera_name in enumerate(graph.get_input_devices()):
        match = re.search(r"AutoChecker\s*(\d+)", camera_name, re.IGNORECASE)
        if match:
            camera_number = int(match.group(1))
            cameras[camera_number] = camera_id
            if announce:
                print(
                    f"* Camera found: AutoChecker {camera_number} "
                    f"(OpenCV ID {camera_id})"
                )

    return cameras


def find_available_printers(*, announce: bool = False) -> list[str]:
    """Return connected USB printer queues that are not marked offline."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    offline_status = (
        getattr(win32print, "PRINTER_STATUS_OFFLINE", 0x80)
        | getattr(win32print, "PRINTER_STATUS_NOT_AVAILABLE", 0x1000)
    )
    work_offline = getattr(
        win32print,
        "PRINTER_ATTRIBUTE_WORK_OFFLINE",
        0x400,
    )
    printers: dict[str, str] = {}
    for printer in win32print.EnumPrinters(flags):
        printer_name = str(printer[2]).strip()
        if not printer_name:
            continue
        handle = None
        try:
            handle = win32print.OpenPrinter(printer_name)
            printer_info = win32print.GetPrinter(handle, 2)
            port_name = str(
                printer_info.get("pPortName", printer_info.get("PortName", "")) or ""
            ).strip()
            status = int(printer_info.get("Status", 0) or 0)
            attributes = int(printer_info.get("Attributes", 0) or 0)
            if (
                "USB" not in port_name.upper()
                or status & offline_status
                or attributes & work_offline
            ):
                continue
        except pywintypes.error:
            continue
        finally:
            if handle is not None:
                try:
                    win32print.ClosePrinter(handle)
                except pywintypes.error:
                    pass
        printers.setdefault(printer_name.casefold(), printer_name)

    result = sorted(printers.values(), key=str.casefold)
    if announce:
        for printer_name in result:
            print(f"* Printer found: {printer_name}")
    return result


def _prioritise(values: list, preferred) -> list:
    """Move a stored value to the front, only when it is still available."""
    for index, value in enumerate(values):
        if str(value) == str(preferred):
            return [value, *values[:index], *values[index + 1:]]
    return values


def upload_display_data(
    display: serial.Serial,
    display_mac: str,
    settings: dict,
    cameras: dict[int, int],
    printers: list[str],
    scanners: list[str],
) -> None:
    """Upload menu options, placing the display's last selection first."""
    last_values = settings.get("DISPLAY", {}).get(
        display_mac,
        ["", "", "", "", ""],
    )
    last_values = (list(last_values) + ["", "", "", "", ""])[:5]
    names = _prioritise(list(settings.get("NAME", [])), last_values[0])
    departments = _prioritise(list(settings.get("DEPARTMENT", [])), last_values[1])
    camera_numbers = _prioritise(sorted(cameras), last_values[2])
    printer_names = _prioritise(list(printers), last_values[3])
    scanner_names = _prioritise(list(scanners), last_values[4])

    fields = [
        ",".join(map(str, names)),
        ",".join(map(str, departments)),
        ",".join(map(str, camera_numbers)),
        ",".join(printer_names),
        ",".join(scanner_names),
        "*ORDER*",
    ]
    for setting_name, setting_value in settings.items():
        if setting_name in {
            "DEPARTMENT", "DISPLAY", "OTHER", "PRINTER", "SCANNER",
            "FEM_BONUS", "AUTO_BONUS", "support", "support_paths",
            "support_username", "main_computer_name",
        }:
            continue
        if setting_name == "FOCUS":
            if isinstance(setting_value, dict):
                fields.extend(
                    f"Camera {camera_number} focus: {focus}"
                    for camera_number, focus in setting_value.items()
                )
            continue
        fields.append(setting_name)

    other_slots, _ = ensure_other_slots(settings)
    fields.extend(
        f"{other_slots[str(index)]['color']}:{other_slots[str(index)]['text']}"
        for index in range(1, OTHER_SLOT_COUNT + 1)
    )

    packet = "uploadData:" + ";".join(fields) + "\n"
    display.write(packet.encode("utf-8"))
    Printer(packet)
    display.flush()


def cameras_available_for_selection(
        cameras: dict[int, int],
        worker_identities: dict[str, CheckerIdentity],
) -> dict[int, int]:
    """Exclude cameras currently owned by running checker processes."""
    reserved = {
        identity.camera_number
        for identity in worker_identities.values()
        if identity.camera_number is not None
    }
    return {
        camera_number: camera_id
        for camera_number, camera_id in cameras.items()
        if camera_number not in reserved
    }


def printers_available_for_selection(
    printers: list[str],
    worker_identities: dict[str, CheckerIdentity],
) -> list[str]:
    """Exclude printers currently owned by running checker processes."""
    reserved = {
        identity.printer_name.casefold()
        for identity in worker_identities.values()
        if identity.printer_name
    }
    return [
        printer_name
        for printer_name in printers
        if printer_name.casefold() not in reserved
    ]


def scanners_available_for_selection(
    scanners: dict[str, str],
    worker_identities: dict[str, CheckerIdentity],
) -> list[str]:
    """Exclude external scanners currently owned by running workers."""
    reserved = {
        identity.scanner_name.casefold()
        for identity in worker_identities.values()
        if identity.scanner_name
    }
    return [
        scanner_name
        for scanner_name in scanners
        if scanner_name.casefold() not in reserved
    ]


def refresh_idle_display_data(
    settings: dict,
    cameras: dict[int, int],
    printers: list[str],
    scanners: list[str],
    idle_displays: dict[str, serial.Serial],
) -> None:
    """Send current menu data to every display still owned by the launcher."""
    for display_number, display in idle_displays.items():
        try:
            upload_display_data(
                display,
                display_number,
                settings,
                cameras,
                printers,
                scanners,
            )
        except (serial.SerialException, OSError) as error:
            print(f"XXX Cannot upload data to AutoChecker {display_number}: {error}")


def broadcast_display_data(
        settings: dict,
        cameras: dict[int, int],
        printers: list[str],
        scanners: dict[str, str],
        idle_displays: dict[str, serial.Serial],
        worker_update_queues: dict[str, multiprocessing.Queue],
        worker_identities: dict[str, CheckerIdentity],
) -> None:
    """Refresh displays using devices not reserved by active workers."""
    available_cameras = cameras_available_for_selection(
        cameras,
        worker_identities,
    )
    available_printers = printers_available_for_selection(
        printers,
        worker_identities,
    )
    available_scanners = scanners_available_for_selection(
        scanners,
        worker_identities,
    )
    refresh_idle_display_data(
        settings,
        available_cameras,
        available_printers,
        available_scanners,
        idle_displays,
    )

    for display_number, update_queue in worker_update_queues.items():
        try:
            update_queue.put_nowait((
                "uploadData",
                dict(available_cameras),
                list(available_printers),
                list(available_scanners),
            ))
        except (OSError, ValueError, queue.Full) as error:
            print(f"XXX Cannot notify AutoChecker {display_number}: {error}")


def broadcast_RETAIL_orders_updated(
    worker_update_queues: dict[str, multiprocessing.Queue],
    worker_identities: dict[str, CheckerIdentity],
    order_ids: set[str] | list[str] | tuple[str, ...],
) -> None:
    """Tell all live RETAIL workers to reload the authoritative order state."""
    payload = tuple(sorted(set(order_ids)))
    for display_number, update_queue in worker_update_queues.items():
        identity = worker_identities.get(display_number)
        if (
            identity is None
            or identity.department.upper()
            not in {"RETAIL", RETAIL_UP_DEPARTMENT}
        ):
            continue
        try:
            update_queue.put_nowait(("retail_orders_updated", payload))
        except (OSError, ValueError, queue.Full) as error:
            print(
                f"XXX Cannot refresh RETAIL state on AutoChecker "
                f"{display_number}: {error}"
            )


def broadcast_RETAIL_UP_header(
    worker_update_queues: dict[str, multiprocessing.Queue],
    worker_identities: dict[str, CheckerIdentity],
) -> None:
    """Refresh progress on every packer's display without replacing its tail."""
    for display_number, update_queue in worker_update_queues.items():
        identity = worker_identities.get(display_number)
        if identity is None or identity.department.upper() != RETAIL_UP_DEPARTMENT:
            continue
        try:
            update_queue.put_nowait(("retail_up_header",))
        except (OSError, ValueError, queue.Full) as error:
            print(
                f"XXX Cannot refresh packing progress on AutoChecker "
                f"{display_number}: {error}"
            )


def parse_command(line: str) -> tuple[str, CheckerIdentity] | None:
    """Parse one Start or Stop line; reject malformed data."""
    match = re.fullmatch(
        r"(Start|Stop):([^;\r\n]+);([^;\r\n]+);(\d*)"
        r"(?:;([^;\r\n]*))?"
        r"(?:;([^;\r\n]*))?",
        line.strip(),
    )
    if not match:
        return None

    (
        action,
        name,
        department,
        camera_number,
        printer_name,
        scanner_name,
    ) = match.groups()
    identity = CheckerIdentity(
        name.strip(),
        department.strip(),
        int(camera_number) if camera_number else None,
        printer_name.strip() if printer_name and printer_name.strip() else None,
        scanner_name.strip() if scanner_name and scanner_name.strip() else None,
    )

    if not identity.name or not identity.department:
        return None

    return action, identity


def stop_matches_identity(
    stop_identity: CheckerIdentity,
    running_identity: CheckerIdentity,
) -> bool:
    """Match legacy Stop commands and optional printer/scanner fields."""
    return (
        stop_identity.name == running_identity.name
        and stop_identity.department == running_identity.department
        and stop_identity.camera_number == running_identity.camera_number
        and (
            stop_identity.printer_name is None
            or (
                running_identity.printer_name is not None
                and stop_identity.printer_name.casefold()
                == running_identity.printer_name.casefold()
            )
        )
        and (
            stop_identity.scanner_name is None
            or (
                running_identity.scanner_name is not None
                and stop_identity.scanner_name.casefold()
                == running_identity.scanner_name.casefold()
            )
        )
    )


def department_paths(department: str) -> tuple[Path, Path]:
    """Create and return Desktop/<department>/Photo and /Statistik safely."""
    # department is a directory name received over serial; do not permit paths.
    if Path(department).name != department or department in {".", ".."}:
        raise ValueError(f"Invalid department name: {department!r}")

    # RETAIL_UP uses the same orders and storage as RETAIL. Do not create a
    # separate Desktop/RETAIL_UP directory tree for the packing process.
    storage_department = (
        "RETAIL"
        if department.upper() == RETAIL_UP_DEPARTMENT
        else department
    )
    department_dir = DESKTOP_DIR / storage_department
    photo_dir = department_dir / "Photo"
    statistics_dir = department_dir / "Statistik"
    photo_dir.mkdir(parents=True, exist_ok=True)
    statistics_dir.mkdir(parents=True, exist_ok=True)
    return photo_dir, statistics_dir


def support_mode_enabled(settings: dict) -> bool:
    """Return True only for an explicit JSON boolean true."""
    return settings.get("support") is True


def configured_support_paths(settings: dict) -> dict[str, Path]:
    """Validate and return the fixed shared resources used in support mode."""
    # Always rebuild the paths from one sanitized computer name.  This makes
    # startup self-healing when an older Settings.json contains a stale share
    # name, an extra slash, or whitespace around a UNC component.
    expected = support_paths_for_computer(
        settings.get("main_computer_name", DEFAULT_MAIN_COMPUTER_NAME)
    )
    result = {key: Path(path) for key, path in expected.items()}

    def require_directory(key: str, create: bool = False) -> None:
        path = result[key]
        if create:
            path.mkdir(parents=True, exist_ok=True)
        # stat() preserves the real WinError (for example 1909).  Path.is_dir
        # can turn that useful authentication error into a misleading False.
        try:
            path.stat()
        except FileNotFoundError as error:
            raise FileNotFoundError(
                f"Support {key} folder is unavailable: {path}"
            ) from error
        if not path.is_dir():
            raise NotADirectoryError(
                f"Support {key} folder is unavailable: {path}"
            )

    require_directory("downloads")
    require_directory("retail")
    require_directory("b2b")
    require_directory("photos", create=True)
    require_directory("statistics", create=True)
    try:
        result["settings"].stat()
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Main Settings.json is unavailable: {result['settings']}"
        ) from error
    if not result["settings"].is_file():
        raise FileNotFoundError(
            f"Main Settings.json is unavailable: {result['settings']}"
        )
    return result


def configure_worker_storage(worker: dict, settings: dict) -> None:
    """Select local or shared RETAIL storage and retain local mirror paths."""
    department = str(worker.get("DEPARTMENT", "")).upper()
    local_retail_statistics = DESKTOP_DIR / "RETAIL" / "Statistik"
    local_retail_statistics.mkdir(parents=True, exist_ok=True)
    local_B2B_downloads = B2B_download_directories()
    if department == "B2B":
        worker["B2B_LOCAL_HISTORY"] = DESKTOP_DIR / "B2B" / "History"
        worker["B2B_LOCAL_DOWNLOAD_DIRECTORIES"] = local_B2B_downloads
        worker["B2B_HISTORY"] = worker["B2B_LOCAL_HISTORY"]

    worker["DOWNLOAD_DIRECTORIES"] = (
        [Path.home() / "Downloads"]
        if department == "RETAIL"
        else local_B2B_downloads
    )
    worker["RETAIL_STATISTICS"] = local_retail_statistics
    worker["RETAIL_LOCAL_STATISTICS"] = local_retail_statistics
    if not support_mode_enabled(settings):
        return

    shared = configured_support_paths(settings)
    worker["SUPPORT"] = True
    worker["DOWNLOAD_DIRECTORIES"] = [shared["downloads"]]
    if department == "B2B":
        worker["B2B_NETWORK_HISTORY"] = shared["b2b"] / "History"
        worker["B2B_NETWORK_DOWNLOAD_DIRECTORIES"] = [shared["downloads"]]
        # Retain the combined list for older helpers. prepare_B2B_order_history
        # uses the explicit four-stage order stored above.
        worker["DOWNLOAD_DIRECTORIES"] = [
            *local_B2B_downloads,
            shared["downloads"],
        ]
        return
    if department not in {"RETAIL", RETAIL_UP_DEPARTMENT}:
        return
    worker["SUPPORT_FOTO"] = shared["photos"]
    worker["RETAIL_STATISTICS"] = shared["statistics"]
    worker["STATISTICS_MIRROR_DIR"] = local_retail_statistics
    if department == "RETAIL":
        worker["STATISTICS"] = shared["statistics"]


def retail_statistics_file(worker: dict) -> Path:
    """Return today's RETAIL file for both assembly and packing workers."""
    statistics_dir = worker.get("RETAIL_STATISTICS")
    if statistics_dir is None:
        statistics_dir = DESKTOP_DIR / "RETAIL" / "Statistik"
    return Path(statistics_dir) / f"{datetime.date.today():%d.%m.%Y}.txt"


def safe_worker_filename(name: str) -> str:
    """Return a Windows-safe filename while keeping the worker name readable."""
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return safe_name or "Unknown"


class WorkerPerformanceStats:
    """Collect per-worker scan timings and merge them atomically into JSON."""

    def __init__(self, name: str, department: str):
        self.name = name
        self.department = department
        self.storage_department = (
            "RETAIL"
            if department.upper() == RETAIL_UP_DEPARTMENT
            else department
        )
        self.storage_section = (
            RETAIL_UP_DEPARTMENT
            if department.upper() == RETAIL_UP_DEPARTMENT
            else ""
        )
        self.pending: dict[str, dict] = {}
        self.persisted_counts: dict[str, int] = {}
        self.active_order: str | None = None
        self.last_scan_time: float | None = None

    @staticmethod
    def _date_key(day: datetime.date | None = None) -> str:
        return f"{day or datetime.date.today():%d.%m.%Y}"

    def _bucket(self, day: datetime.date | None = None) -> dict:
        return self.pending.setdefault(
            self._date_key(day),
            {"intervals": [], "mistakes": 0, "orders": set()},
        )

    def record_scan(self, order_id: str | None = None, *,
                    moment: float | None = None,
                    day: datetime.date | None = None) -> None:
        """Record one Scan command and update the current order timer."""
        now = time.monotonic() if moment is None else moment
        normalized_order = order_id.lstrip("#") if order_id else None

        if self.active_order is None:
            if normalized_order is not None:
                self.active_order = normalized_order
                self.last_scan_time = now
            return

        if self.last_scan_time is not None:
            elapsed = max(0.0, now - self.last_scan_time)
            self._bucket(day)["intervals"].append(round(elapsed, 3))

        if normalized_order is not None and normalized_order != self.active_order:
            self.active_order = normalized_order
        self.last_scan_time = now

    def record_mistake(self, day: datetime.date | None = None) -> None:
        self._bucket(day)["mistakes"] += 1

    def record_photo(self, order_id: str | None,
                     day: datetime.date | None = None) -> None:
        if not order_id:
            return
        normalized_order = order_id.lstrip("#")
        self._bucket(day)["orders"].add(normalized_order)

    def stop_tracking(self) -> None:
        self.active_order = None
        self.last_scan_time = None

    def change_name(self, new_name: str) -> None:
        if new_name == self.name:
            return
        self.flush()
        self.name = new_name
        self.persisted_counts.clear()
        self.flush(force=True)
        # Do not assign a cross-worker partial interval to either person.
        if self.active_order is not None:
            self.last_scan_time = time.monotonic()

    def _file_path(self, day: datetime.date | None = None) -> Path:
        day = day or datetime.date.today()
        statistics_root = (
            DESKTOP_DIR
            / self.storage_department
            / "Worker_Statistics"
        )
        if self.storage_section:
            statistics_root /= self.storage_section
        return (
            statistics_root
            / safe_worker_filename(self.name)
            / f"{day:%m.%Y}.json"
        )

    @staticmethod
    def _day_from_key(date_key: str) -> datetime.date:
        return datetime.datetime.strptime(date_key, "%d.%m.%Y").date()

    def completed_count(self, day: datetime.date | None = None) -> int:
        """Return persisted plus current-process completed orders for one day."""
        day = day or datetime.date.today()
        date_key = self._date_key(day)
        if date_key not in self.persisted_counts:
            path = self._file_path(day)
            stored_count = 0
            if path.is_file():
                try:
                    with portalocker.Lock(
                        str(path), mode="r", encoding="utf-8", timeout=30
                    ) as file:
                        document = json.load(file)
                    stored_count = int(
                        document.get("days", {}).get(date_key, {}).get("count", 0)
                    )
                except (
                    OSError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    portalocker.exceptions.LockException,
                ):
                    stored_count = 0
            self.persisted_counts[date_key] = max(0, stored_count)
        return self.persisted_counts[date_key] + len(
            self.pending.get(date_key, {}).get("orders", set())
        )

    def flush(self, *, force: bool = False) -> None:
        if not self.pending and not force:
            return

        monthly_pending: dict[Path, list[tuple[str, dict]]] = defaultdict(list)
        for date_key, pending in self.pending.items():
            monthly_pending[self._file_path(self._day_from_key(date_key))].append(
                (date_key, pending)
            )
        if force:
            monthly_pending.setdefault(self._file_path(), [])

        saved_date_keys = []
        for path, entries in monthly_pending.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            try:
                with portalocker.Lock(
                    str(path), mode="r+", encoding="utf-8", timeout=30
                ) as file:
                    file.seek(0)
                    raw = file.read().strip()
                    document = json.loads(raw) if raw else {
                        "name": self.name,
                        "department": self.department,
                        "month": path.stem,
                        "days": {},
                    }
                    days = document.setdefault("days", {})
                    for stored_day in days.values():
                        if isinstance(stored_day, dict):
                            stored_day.pop("completed_orders", None)

                    for date_key, pending in entries:
                        stored = days.setdefault(date_key, {})
                        stored_intervals = [
                            float(value)
                            for value in stored.get("scan_intervals_seconds", [])
                            if isinstance(value, (int, float))
                        ]
                        stored_intervals.extend(pending["intervals"])

                        stored["count"] = max(0, int(stored.get("count", 0))) + len(
                            pending["orders"]
                        )
                        stored["median_seconds"] = (
                            round(float(statistics.median(stored_intervals)), 3)
                            if stored_intervals else 0.0
                        )
                        stored["mistakes"] = (
                            int(stored.get("mistakes", 0))
                            + int(pending["mistakes"])
                        )
                        stored["scan_intervals_seconds"] = stored_intervals
                        # Remove order identifiers written by the old format.
                        stored.pop("completed_orders", None)
                        self.persisted_counts[date_key] = stored["count"]
                        saved_date_keys.append(date_key)

                    today_key = self._date_key()
                    if path == self._file_path() and today_key in days:
                        self.persisted_counts[today_key] = max(
                            0, int(days[today_key].get("count", 0))
                        )
                    document["name"] = self.name
                    document["department"] = self.department
                    document["month"] = path.stem
                    file.seek(0)
                    file.truncate()
                    json.dump(document, file, ensure_ascii=False, indent=4)
                    file.write("\n")
                    file.flush()
            except (OSError, TypeError, ValueError, json.JSONDecodeError,
                    portalocker.exceptions.LockException) as error:
                print(f"XXX Cannot save worker statistics for {self.name}: {error}")

        for date_key in saved_date_keys:
            self.pending.pop(date_key, None)


def ensure_worker_statistics_files(settings: dict) -> None:
    """Create an empty JSON file for every configured NAME/DEPARTMENT pair."""
    names = settings.get("NAME", [])
    departments = settings.get("DEPARTMENT", [])
    if not isinstance(names, list) or not isinstance(departments, list):
        return
    for department in departments:
        department = str(department).strip()
        if (
            not department
            or Path(department).name != department
            or department in {".", ".."}
        ):
            continue
        for name in names:
            name = str(name).strip()
            if not name:
                continue
            WorkerPerformanceStats(name, department).flush(force=True)


def send(display: serial.Serial, message: str) -> None:
    """Send coloured display lines in the existing BEGIN/END packet format."""
    def colour_tag(line: str) -> str:
        if line.startswith(("XXX", "!")) or "!!!" in line:
            return "#ff0000"
        if re.search(r"\bBonus\b", line, re.IGNORECASE):
            return BONUS_COLOUR
        if line.startswith("+") or re.match(r"^\d+\s+\+", line):
            return "#00ff00"
        if line.startswith("*"):
            return "#00ffff"
        if line.startswith("Order"):
            return "#ff00d9"
        if line.startswith("?") or re.match(r"^\d+\s+\?", line):
            return "#ffff00"
        for colour, fragment in get_other_colour_rules():
            if not fragment:
                continue
            if fragment in line:
                return colour
            if "N" in fragment:
                numeric_pattern = re.escape(fragment).replace("N", r"\d+")
                if re.search(numeric_pattern, line):
                    return colour
        return "#ffffff"

    coloured_lines = []
    for line in message.splitlines():
        if not line:
            continue
        line = bonus_display_name(line)
        # Keep the catalog name unchanged internally, but omit the redundant
        # single-unit suffix in every line shown on the display.
        line = re.sub(
            r"\s+-\s+1\s+unit\b",
            "",
            line,
            flags=re.IGNORECASE,
        )
        explicit_colour = re.match(r"^(#[0-9a-fA-F]{6})\s+(.*)$", line)
        if explicit_colour:
            tag, line = explicit_colour.groups()
        else:
            tag = colour_tag(line)
        # Keep a leading # intact, but remove # embedded in product names.
        display_line = re.sub(r"(?<!^)#", "", line)
        coloured_lines.append(f"{tag} {display_line}")
    packet = "<BEGIN>\n" + "\n".join(coloured_lines) + "\n<END>\n"
    try:
        display.write(packet.encode("utf-8"))
        display.flush()
    except (serial.SerialException, OSError) as error:
        print(f"XXX Display write error: {error}")


def Printer(message: str) -> None:
    """Keep a console log; messages for the display are sent separately."""
    print(message)


def print_console_red(message: str) -> None:
    """Print one high-priority console warning in red when interactive."""
    if sys.stdout and sys.stdout.isatty():
        print(f"\033[91m{message}\033[0m")
    else:
        print(message)


def cleanup_old_photos(folder: Path, older_days: int = 120,
                       extensions: set[str] | None = None) -> int:
    """Move old files (optionally filtered by extension) to the Recycle Bin."""
    if not folder.is_dir():
        return 0
    cutoff = time.time() - older_days * 24 * 60 * 60
    removed = 0
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        if extensions is not None and entry.suffix.lower() not in extensions:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                send2trash(str(entry))
                removed += 1
        except OSError as error:
            print(f"XXX Cannot clean {entry}: {error}")
    if removed:
        print(f"* Old files moved to Recycle Bin from {folder}: {removed}")
    return removed


def cleanup_launcher_photos() -> None:
    for department in ("B2B", "RETAIL"):
        cleanup_old_photos(DESKTOP_DIR / department / "Photo")
    cleanup_old_photos(Path.home() / "Downloads", extensions={".pdf"})


def is_bonus_item(name: str) -> bool:
    """Return whether an order position contains the Bonus marker."""
    return bool(re.search(r"\bBonus\b", str(name), re.IGNORECASE))


def bonus_barcode_name(name: str) -> str:
    """Remove Bonus only for catalogue/barcode matching."""
    without_bonus = re.sub(
        r"\(\s*Bonus\s*\)|\bBonus\b",
        "",
        str(name),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", without_bonus).strip()


def bonus_display_name(line: str) -> str:
    """Move the Bonus marker directly behind the first product-category |."""
    if not is_bonus_item(line):
        return line
    without_bonus = bonus_barcode_name(line)
    if "|" not in without_bonus:
        return re.sub(
            r"\(\s*Bonus\s*\)|\bBonus\b",
            "(Bonus)",
            line,
            count=1,
            flags=re.IGNORECASE,
        )
    left, right = without_bonus.split("|", 1)
    return f"{left.rstrip()} | (Bonus) {right.strip()}"


def normalize(name: str) -> str:
    name = re.sub(r"^Outlet \|\s*", "", name)
    name = re.sub(r"\(\+\d+\)", "", name)
    return re.sub(r"\s+", " ", name).strip()


def product_name_keeps_fem_suffix(name: str, config: dict | None) -> bool:
    """Whether a configured name fragment makes the trailing N fem part literal."""
    if not config:
        return False
    exceptions = config.get("PRODUCT_NAME_EXCEPTIONS", [])
    if not isinstance(exceptions, list):
        return False
    normalized_name = normalize(name).casefold()
    return any(
        str(fragment).strip()
        and str(fragment).strip().casefold() in normalized_name
        for fragment in exceptions
    )


def product_base_name(name: str, config: dict | None = None) -> str:
    """Remove package size unless the whole suffix is part of an exception name."""
    normalized_name = normalize(bonus_barcode_name(name))
    if product_name_keeps_fem_suffix(normalized_name, config):
        return normalized_name
    return re.sub(
        r"\s*-\s*\d+(?:\+\d+)?\s*fem\s*$",
        "",
        normalized_name,
        flags=re.IGNORECASE,
    )


def has_barcode_for_item(item_name: str, config: dict) -> bool:
    """Whether an order line has a matching product or merchandise entry."""
    base_name = product_base_name(item_name, config)
    products = config["PRODUCTS"].values()
    merchandise = config.get("Merch", {}).values()
    return base_name in products or base_name in merchandise


def resolve_scanned_product(code: str, config: dict) -> str | None:
    """Turn Scan:<five digit product id><package size> into a PDF item name."""
    # Some catalogue keys are scanner text rather than a numeric barcode,
    # for example "Super Mix #1".
    product_name = config["PRODUCTS"].get(code)
    if product_name is not None:
        return product_name

    # Merchandise scanners can send the full text key, for example
    # "Hoodie Off-white 2025 x1 S", rather than a numeric barcode.
    merchandise_name = config.get("Merch", {}).get(code)
    if merchandise_name is not None:
        return merchandise_name

    # Seeds use a numeric code: five-digit product ID + package size.
    if len(code) < 6 or not code.isdigit():
        return None

    product_id = code[:5]
    size_text = code[5:]
    package_size = str(int(size_text))  # 01 -> 1, 001000 -> 1000

    product_name = config["PRODUCTS"].get(product_id)
    if product_name is not None:
        return f"{product_name} - {package_size} fem"

    # Merch may also use the same five-digit barcode key.
    return config.get("Merch", {}).get(product_id)


def _B2B_name_signature(name: str) -> tuple[str | None, frozenset[str]]:
    """Return product category and order-independent significant words."""
    words = re.findall(r"[a-z0-9]+", name.casefold())
    if "auto" in words:
        category = "auto"
    elif "fem" in words:
        category = "fem"
    elif "ff" in words:
        category = "ff"
    else:
        category = None
    ignored = {"auto", "fem", "ff", "original"}
    return category, frozenset(word for word in words if word not in ignored)


def resolve_B2B_catalog_product(B2B_name: str, config: dict) -> tuple[str, str] | None:
    """Resolve a B2B name to (catalog value, PRODUCTS/Merch) without guessing."""
    aliases = config.get("B2B_ALIASES", {})
    alias_target = aliases.get(B2B_name)
    if alias_target is not None:
        if alias_target in config["PRODUCTS"].values():
            return alias_target, "PRODUCTS"
        if alias_target in config.get("Merch", {}).values():
            return alias_target, "Merch"
        return None

    B2B_signature = _B2B_name_signature(B2B_name)
    product_matches = [
        value for value in config["PRODUCTS"].values()
        if _B2B_name_signature(value) == B2B_signature
    ]
    if len(product_matches) == 1:
        return product_matches[0], "PRODUCTS"
    if product_matches:
        return None

    merch_matches = [
        value for value in config.get("Merch", {}).values()
        if _B2B_name_signature(product_base_name(value)) == B2B_signature
    ]
    if len(merch_matches) == 1:
        return merch_matches[0], "Merch"
    return None


def scan_match_name(name: str) -> str:
    """Ignore the Bonus marker and free B2B +X when matching a scan."""
    return re.sub(
        r"(\s*-\s*\d+)\+\d+(\s*fem\s*)$",
        r"\1\2",
        normalize(bonus_barcode_name(name)),
        flags=re.IGNORECASE,
    )


def sort_key(item: tuple[str, int], config: dict) -> tuple[int, str, int]:
    name, _ = item
    normalized = product_base_name(name, config)
    is_bonus = is_bonus_item(name)
    is_no_barcode = not has_barcode_for_item(name, config)
    fem_match = re.search(r"(\d+)\s*fem", name, re.IGNORECASE)
    fem_count = int(fem_match.group(1)) if fem_match else 0
    base_name = re.sub(r"\s*-\s*\d+\s*fem", "", normalized, flags=re.IGNORECASE).strip().lower()
    category = 0 if not is_bonus and not is_no_barcode else 1 if is_bonus else 2
    return category, base_name, fem_count


def _available_order_pdfs(
    download_directories: list[Path] | None = None,
) -> list[tuple[datetime.date, int, Path]]:
    pattern = re.compile(r"^(\d{2}\.\d{2}\.\d{4}) Part (\d+)\.pdf$", re.IGNORECASE)
    found = []
    seen_paths = set()
    directories = download_directories or [Path.home() / "Downloads"]
    for downloads in directories:
        if not downloads.is_dir():
            continue
        for path in downloads.iterdir():
            match = pattern.fullmatch(path.name)
            if not match:
                continue
            path_key = str(path).casefold()
            if path_key in seen_paths:
                continue
            try:
                date = datetime.datetime.strptime(
                    match.group(1), "%d.%m.%Y"
                ).date()
                found.append((date, int(match.group(2)), path))
                seen_paths.add(path_key)
            except ValueError:
                continue
    return sorted(
        found,
        key=lambda item: (item[0], item[1], str(item[2]).casefold()),
    )


def _available_RETAIL_order_sources(
    download_directories: list[Path] | None = None,
) -> list[tuple[datetime.date, int, Path, Path | None, Path | None]]:
    """Return every normal RETAIL part with its PDF and/or Order JSON.

    The canonical PDF path is returned even for a JSON-only part.  It is used
    as the stable section name in statistics and does not have to exist.
    """
    pdf_pattern = re.compile(
        r"^(\d{2}\.\d{2}\.\d{4}) Part (\d+)\.pdf$",
        re.IGNORECASE,
    )
    json_pattern = re.compile(
        r"^(\d{2}\.\d{2}\.\d{4}) Part (\d+) \(Order\)\.json$",
        re.IGNORECASE,
    )
    sources: dict[
        tuple[datetime.date, int, str], dict[str, Path | None]
    ] = {}
    directories = download_directories or [Path.home() / "Downloads"]
    for directory_value in directories:
        directory = Path(directory_value)
        if not directory.is_dir():
            continue
        try:
            paths = list(directory.iterdir())
        except OSError:
            continue
        for path in paths:
            if not path.is_file():
                continue
            match = pdf_pattern.fullmatch(path.name)
            source_kind = "pdf"
            if match is None:
                match = json_pattern.fullmatch(path.name)
                source_kind = "json"
            if match is None:
                continue
            try:
                source_date = datetime.datetime.strptime(
                    match.group(1), "%d.%m.%Y"
                ).date()
            except ValueError:
                continue
            part_number = int(match.group(2))
            key = (
                source_date,
                part_number,
                str(directory).casefold(),
            )
            slot = sources.setdefault(key, {"pdf": None, "json": None})
            slot[source_kind] = path

    result = []
    for (source_date, part_number, directory_key), paths in sources.items():
        pdf_path = paths["pdf"]
        cache_path = paths["json"]
        if pdf_path is not None:
            canonical_pdf = pdf_path
        else:
            assert cache_path is not None
            cache_name = cache_path.name[:-len(RETAIL_ORDER_CACHE_SUFFIX)]
            canonical_pdf = cache_path.with_name(cache_name + ".pdf")
        result.append((
            source_date,
            part_number,
            canonical_pdf,
            pdf_path,
            cache_path,
        ))
    return sorted(
        result,
        key=lambda item: (
            item[0],
            item[1],
            str(item[2].parent).casefold(),
        ),
    )


def launcher_local_downloads(worker: dict) -> Path:
    """Return the local browser-download folder used by Admin-style inputs."""
    downloads = Path(worker.get("LOCAL_DOWNLOADS", Path.home() / "Downloads"))
    downloads.mkdir(parents=True, exist_ok=True)
    return downloads


def raw_download_is_stable(worker: dict, path: Path) -> bool:
    """Confirm a browser download stayed unchanged across two launcher polls."""
    states = worker.setdefault("RAW_DOWNLOAD_STABILITY", {})
    key = str(path).casefold()
    try:
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size <= 0:
            states.pop(key, None)
            return False
        previous = states.get(key)
        if previous is not None and previous[0] == signature:
            states[key] = (signature, previous[1] + 1)
        else:
            states[key] = (signature, 0)
        return states[key][1] >= 1
    except OSError:
        states.pop(key, None)
        return False


def launcher_order_part_numbers(worker: dict) -> set[int]:
    """Return today's Part numbers across local and authoritative Downloads."""
    directories = [launcher_local_downloads(worker)]
    directories.extend(Path(path) for path in worker.get("DOWNLOAD_DIRECTORIES", []))
    return {
        part_number
        for source_date, part_number, _path in _available_order_pdfs(directories)
        if source_date == datetime.date.today()
    }


def launcher_current_label_paths(worker: dict) -> tuple[Path, Path | None]:
    """Return local and optional shared Label paths for the newest order Part."""
    parts = launcher_order_part_numbers(worker)
    if not parts:
        raise FileNotFoundError("Today's order PDF was not found")
    filename = (
        f"{datetime.date.today():%d.%m.%Y} Part {max(parts)} (Label).pdf"
    )
    local_path = launcher_local_downloads(worker) / filename
    shared_path = None
    if worker.get("SUPPORT"):
        shared_path = Path(worker["DOWNLOAD_DIRECTORIES"][0]) / filename
    return local_path, shared_path


def copy_file_atomically(source: Path, destination: Path) -> None:
    """Copy one completed file without exposing a partial destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.AutoChecker-{os.getpid()}-{threading.get_ident()}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_pdf_without_leading_junk(path: Path) -> PdfReader:
    """Open a PDF after removing browser bytes before its %PDF- header."""
    data = path.read_bytes()
    header_position = data.find(b"%PDF-")
    if header_position < 0:
        raise ValueError(f"PDF header was not found in {path.name}")
    return PdfReader(io.BytesIO(data[header_position:]), strict=False)


def contains_ups_access_point(text: str) -> bool:
    """Match the two known spellings plus close OCR/text-extraction variants."""
    normalized = " ".join(text.casefold().split())
    spellings = ("ups access point", "ups acces point")
    return any(
        spelling in normalized
        or fuzz.partial_ratio(spelling, normalized) >= 90
        for spelling in spellings
    )


def ensure_default_sound_file() -> None:
    """Download the default UPS sound once, without replacing user audio."""
    sound_path = BASE_DIR / "ups.mp3"
    if sound_path.is_file():
        return
    temporary_path = sound_path.with_name(sound_path.name + ".download")
    try:
        response = requests.get(UPS_SOUND_URL, timeout=10)
        if response.status_code != 200 or not response.content:
            raise RuntimeError(f"HTTP {response.status_code}")
        temporary_path.write_bytes(response.content)
        os.replace(temporary_path, sound_path)
        print(f"* Default sound installed: {sound_path.name}")
    except Exception as error:
        temporary_path.unlink(missing_ok=True)
        print(f"? Cannot install default ups.mp3: {error}")


def play_launcher_sound(name: str) -> None:
    """Play the configured Admin-compatible sound, with a Windows fallback."""
    sound_path = BASE_DIR / f"{name}.mp3"
    try:
        if not sound_path.is_file():
            raise FileNotFoundError(sound_path)
        import pygame

        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.stop()
        pygame.mixer.Sound(str(sound_path)).play()
    except Exception as error:
        print(f"? Cannot play {name} sound ({error}); using system alert")
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except RuntimeError:
            pass


def notify_ups_access_points(pdf_path: Path) -> list[str]:
    """Report and sound every unique UPS Access Point order in one PDF."""
    import pdfplumber

    pattern = re.compile(
        r"(?m)^(#\d+)(?=[^#]*☐).*?type( STEALTH)?\s*\n(.*?)\s*☐"
        r".*?(UPS|Zasilkovna|Postal)(.*?)(?=^#\d+|\Z)",
        re.DOTALL,
    )
    detected = []
    seen = set()
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for order_id, _stealth, _name, _delivery, tail in pattern.findall(text):
                if order_id in seen or not contains_ups_access_point(tail):
                    continue
                seen.add(order_id)
                detected.append(order_id)
                print(f"! UPS Access Point detected for order {order_id}")
                play_launcher_sound("ups")
    return detected


def extract_RETAIL_pdf_order_ids(pdf_path: Path) -> list[str]:
    """Extract unique #OrderNumber values in their PDF appearance order."""
    import pdfplumber

    result = []
    seen = set()
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for order_id in re.findall(r"(?<![\w#])#\d+(?!\d)", text):
                if order_id not in seen:
                    seen.add(order_id)
                    result.append(order_id)
    return result


def queue_or_copy_order_pdf_to_main(worker: dict, source: Path) -> None:
    """Send an Admin-style PDF to main Downloads in support mode."""
    if not worker.get("SUPPORT"):
        return
    destination = Path(worker["DOWNLOAD_DIRECTORIES"][0]) / source.name
    try:
        copy_file_atomically(source, destination)
        worker.setdefault("PENDING_MAIN_PDFS", {}).pop(str(source), None)
        print(f"* PDF sent to main computer: {destination}")
    except Exception as error:
        worker.setdefault("PENDING_MAIN_PDFS", {})[str(source)] = str(destination)
        print(f"XXX Cannot send {source.name} to main computer: {error}")


def retry_pending_main_pdfs(worker: dict) -> None:
    for source_text, destination_text in list(
        worker.setdefault("PENDING_MAIN_PDFS", {}).items()
    ):
        source = Path(source_text)
        try:
            if not source.is_file():
                raise FileNotFoundError(source)
            copy_file_atomically(source, Path(destination_text))
            del worker["PENDING_MAIN_PDFS"][source_text]
            print(f"* Pending PDF sent to main computer: {source.name}")
        except Exception:
            # Keep it queued. The periodic launcher pass will try again.
            continue


def mark_launcher_change_pdf_stable(
    worker: dict,
    path: Path,
    *,
    label: bool,
) -> None:
    """Allow an atomically completed raw Change PDF to process immediately."""
    try:
        stat = path.stat()
    except OSError:
        return
    state_name = (
        "RETAIL_CHANGE_LABEL_OBSERVED"
        if label
        else "RETAIL_CHANGE_OBSERVED"
    )
    worker.setdefault(state_name, {})[str(path).casefold()] = (
        stat.st_mtime_ns,
        stat.st_size,
    )


def process_pending_admin_download_files(worker: dict) -> bool:
    """Process Admin-style raw PDF downloads without filesystem events."""
    downloads = launcher_local_downloads(worker)
    retry_pending_main_pdfs(worker)
    processed_any = False
    failures = worker.setdefault("RAW_DOWNLOAD_FAILURES", {})

    candidates: list[tuple[Path, str, list[str]]] = [
        (downloads / "mpdf.pdf", "orders", []),
        (downloads / "qwe.pdf", "labels", []),
        (downloads / "qwez.pdf", "label_fragment", []),
        (downloads / "change.pdf", "change_order", []),
    ]
    try:
        for path in downloads.iterdir():
            if not path.is_file():
                continue
            match = RAW_CHANGE_LABEL_PATTERN.fullmatch(path.name)
            if match is None:
                continue
            order_ids = [
                f"#{value.strip().lstrip('#')}"
                for value in match.group(1).split(",")
            ]
            candidates.append((path, "change_label", order_ids))
    except OSError as error:
        print(f"XXX Cannot inspect raw Downloads files: {error}")

    for source, source_kind, filename_order_ids in candidates:
        raw_name = source.name.casefold()
        if not source.is_file() or not raw_download_is_stable(worker, source):
            continue
        try:
            signature = (source.stat().st_size, source.stat().st_mtime_ns)
        except OSError:
            continue
        failed = failures.get(raw_name)
        if (
            failed is not None
            and failed[0] == signature
            and time.monotonic() - failed[1] < ORDER_SOURCE_RETRY_INTERVAL
        ):
            continue

        try:
            lock_path = downloads / ".AutoChecker_raw_downloads.lock"
            with portalocker.Lock(
                str(lock_path), mode="a+", encoding="utf-8", timeout=5
            ):
                if not source.is_file():
                    continue
                if source_kind == "orders":
                    naming_directory = (
                        Path(worker["DOWNLOAD_DIRECTORIES"][0])
                        if worker.get("SUPPORT")
                        else downloads
                    )
                    naming_lock = naming_directory / ".AutoChecker_orders.lock"
                    with portalocker.Lock(
                        str(naming_lock), mode="a+", encoding="utf-8", timeout=30
                    ):
                        part_number = max(
                            launcher_order_part_numbers(worker), default=0
                        ) + 1
                        destination = downloads / (
                            f"{datetime.date.today():%d.%m.%Y} "
                            f"Part {part_number}.pdf"
                        )
                        os.replace(source, destination)
                        # Publish the reserved Part name before another
                        # support launcher can choose the same number.
                        queue_or_copy_order_pdf_to_main(worker, destination)
                    print(f"* Order PDF renamed: {destination.name}")
                    notify_ups_access_points(destination)
                elif source_kind == "change_order":
                    order_ids = extract_RETAIL_pdf_order_ids(source)
                    if not order_ids:
                        raise ValueError(
                            "No order numbers were found in change.pdf"
                        )
                    destination = downloads / (
                        f"Change {', '.join(order_ids)} "
                        f"({datetime.date.today():%d.%m.%Y}).pdf"
                    )
                    notify_ups_access_points(source)
                    os.replace(source, destination)
                    print(f"* Change Order PDF renamed: {destination.name}")
                    report_launcher_order_progress(worker, order_ids)
                    queue_or_copy_order_pdf_to_main(worker, destination)
                    monitor_path = (
                        Path(worker["DOWNLOAD_DIRECTORIES"][0])
                        / destination.name
                        if worker.get("SUPPORT")
                        else destination
                    )
                    mark_launcher_change_pdf_stable(
                        worker, monitor_path, label=False
                    )
                elif source_kind == "change_label":
                    destination = downloads / (
                        f"Change Label {', '.join(filename_order_ids)} "
                        f"({datetime.date.today():%d.%m.%Y}).pdf"
                    )
                    os.replace(source, destination)
                    print(f"* Change Label PDF renamed: {destination.name}")
                    report_launcher_order_progress(
                        worker, filename_order_ids
                    )
                    queue_or_copy_order_pdf_to_main(worker, destination)
                    monitor_path = (
                        Path(worker["DOWNLOAD_DIRECTORIES"][0])
                        / destination.name
                        if worker.get("SUPPORT")
                        else destination
                    )
                    mark_launcher_change_pdf_stable(
                        worker, monitor_path, label=True
                    )
                elif source_kind == "labels":
                    destination, _shared = launcher_current_label_paths(worker)
                    os.replace(source, destination)
                    print(f"* Label PDF renamed: {destination.name}")
                    print(
                        "* Sending is waiting for command: send part "
                        f"{destination.stem.split(' Part ')[-1].split(' ')[0]}"
                    )
                else:
                    destination, shared = launcher_current_label_paths(worker)
                    if not destination.is_file() and shared is not None and shared.is_file():
                        copy_file_atomically(shared, destination)
                    if not destination.is_file():
                        raise FileNotFoundError(
                            "Current Label PDF was not found; download qwe.pdf first"
                        )
                    temporary = destination.with_name(
                        f".{destination.stem}.merging.pdf"
                    )
                    writer = PdfWriter()
                    try:
                        for pdf_source in (destination, source):
                            reader = read_pdf_without_leading_junk(pdf_source)
                            for page in reader.pages:
                                writer.add_page(page)
                        with temporary.open("wb") as output:
                            writer.write(output)
                        os.replace(temporary, destination)
                        source.unlink()
                    finally:
                        writer.close()
                        temporary.unlink(missing_ok=True)
                    print(f"* Label fragment appended: {destination.name}")
                    print(
                        "* Sending is waiting for command: send part "
                        f"{destination.stem.split(' Part ')[-1].split(' ')[0]}"
                    )
            worker.setdefault("RAW_DOWNLOAD_STABILITY", {}).pop(
                str(source).casefold(), None
            )
            failures.pop(raw_name, None)
            processed_any = True
        except Exception as error:
            failures[raw_name] = (signature, time.monotonic())
            print(f"XXX Cannot process {raw_name}: {error}")

    return processed_any


def find_latest_pdf(
    previous: bool = False,
    download_directories: list[Path] | None = None,
) -> tuple[Path | None, int | None]:
    """Find latest PDF, or step back one part/date when previous is requested."""
    global PDF_HISTORY_CURSOR
    files = _available_order_pdfs(download_directories)
    if not files:
        return None, None
    today_files = [item for item in files if item[0] == datetime.date.today()]
    if not previous:
        selected = today_files[-1] if today_files else files[-1]
    else:
        paths = [item[2] for item in files]
        if PDF_HISTORY_CURSOR in paths:
            selected_index = paths.index(PDF_HISTORY_CURSOR) - 1
        else:
            current = today_files[-1] if today_files else files[-1]
            selected_index = paths.index(current[2]) - 1
        if selected_index < 0:
            return None, None
        selected = files[selected_index]
    PDF_HISTORY_CURSOR = selected[2]
    return selected[2], selected[1]


def statistics_file(worker: dict) -> Path:
    if worker.get("DEPARTMENT", "").upper() == "B2B" and worker.get("ACTIVE_STAT_FILE"):
        return worker["ACTIVE_STAT_FILE"]
    return worker["STATISTICS"] / f"{datetime.date.today():%d.%m.%Y}.txt"


def _locked_statistics_file(stat_file: Path):
    """Return an exclusive OS/SMB lock for a statistics file."""
    stat_file.parent.mkdir(parents=True, exist_ok=True)
    stat_file.touch(exist_ok=True)
    return portalocker.Lock(str(stat_file), mode="r+", encoding="utf-8", timeout=30)


def mirror_statistics_lines(
    worker: dict,
    source_file: Path,
    lines: list[str],
) -> None:
    """Write a locked local mirror while the authoritative shared lock is held."""
    mirror_dir = worker.get("STATISTICS_MIRROR_DIR")
    if mirror_dir is None:
        return
    mirror_file = Path(mirror_dir) / source_file.name
    try:
        if mirror_file.resolve() == source_file.resolve():
            return
    except OSError:
        if str(mirror_file).casefold() == str(source_file).casefold():
            return
    with _locked_statistics_file(mirror_file) as mirror:
        mirror.seek(0)
        mirror.truncate()
        mirror.writelines(lines)
        mirror.flush()


def synchronize_support_statistics(worker: dict) -> None:
    """Refresh today's local mirror from the authoritative network file."""
    if (
        not worker.get("SUPPORT")
        or str(worker.get("DEPARTMENT", "")).upper()
        not in {"RETAIL", RETAIL_UP_DEPARTMENT}
    ):
        return
    shared_file = retail_statistics_file(worker)
    if not shared_file.is_file():
        return
    with _locked_statistics_file(shared_file) as file:
        file.seek(0)
        mirror_statistics_lines(worker, shared_file, file.readlines())


def read_stat_lines(stat_file: Path) -> list[str]:
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        return file.readlines()


def write_stat_file(stat_file: Path, lines: list[str]) -> None:
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        file.truncate()
        file.writelines(lines)
        file.flush()


def B2B_index_file(worker: dict) -> Path:
    return worker["STATISTICS"] / B2B_INDEX_FILENAME


def B2B_order_name(pdf_reference: str) -> str | None:
    """Return a safe order name without its leading # or trailing .pdf."""
    reference = pdf_reference.strip()
    if not reference.startswith("#"):
        return None
    name = reference[1:].strip()
    if name.casefold().endswith(".pdf"):
        name = name[:-4]
    if (
        not name
        or name in {".", ".."}
        or name.rstrip(" .") != name
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
    ):
        return None
    return name


def _normalise_B2B_disk_name(value: object) -> str:
    """Normalise an order folder/PDF name for tolerant SMB lookup."""
    name = str(value).strip()
    if name.startswith("#"):
        name = name[1:].strip()
    if name.casefold().endswith(".pdf"):
        name = name[:-4].strip()
    return name.casefold()


def _B2B_pdf_in_folder(folder: Path, order_name: str) -> Path | None:
    """Find one B2B PDF despite case, a leading #, or surrounding spaces."""
    expected = _normalise_B2B_disk_name(order_name)
    try:
        entries = list(folder.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return None
    for path in entries:
        if (
            path.suffix.casefold() == ".pdf"
            and _normalise_B2B_disk_name(path.name) == expected
            and path.is_file()
        ):
            return path
    return None


def _B2B_order_folder(history_dir: Path, order_name: str) -> Path | None:
    """Find an existing History order folder using the same tolerant name."""
    expected = _normalise_B2B_disk_name(order_name)
    try:
        entries = list(history_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return None
    for path in entries:
        if (
            _normalise_B2B_disk_name(path.name) == expected
            and path.is_dir()
        ):
            return path
    return None


def _case_insensitive_file(folder: Path, filename: str) -> Path | None:
    if not folder.is_dir():
        return None
    expected = filename.casefold()
    return next(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.name.casefold() == expected
        ),
        None,
    )


def B2B_download_directories() -> list[Path]:
    """Return Windows' configured Downloads folder plus safe fallbacks."""
    candidates: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                configured, _ = winreg.QueryValueEx(
                    key,
                    "{374DE290-123F-4565-9164-39C4925E467B}",
                )
            candidates.append(
                Path(os.path.expandvars(str(configured))).expanduser()
            )
        except (OSError, ValueError):
            pass

    candidates.append(Path.home() / "Downloads")
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Downloads")

    unique = []
    seen = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def current_RETAIL_UP_label_files(
    day: datetime.date | None = None,
    download_directories: list[Path] | None = None,
) -> list[tuple[Path, Path, int]]:
    """Return today's existing (JSON, PDF, part number) label file pairs."""
    day = day or datetime.date.today()
    pattern = re.compile(
        rf"^{re.escape(day.strftime('%d.%m.%Y'))} Part (\d+) "
        r"\(Label\)\.json$",
        re.IGNORECASE,
    )
    result: list[tuple[Path, Path, int]] = []
    for directory in download_directories or B2B_download_directories():
        if not directory.is_dir():
            continue
        for json_path in directory.iterdir():
            if not json_path.is_file():
                continue
            match = pattern.fullmatch(json_path.name)
            if match is None:
                continue
            pdf_path = _case_insensitive_file(
                directory,
                json_path.with_suffix(".pdf").name,
            )
            if pdf_path is not None:
                result.append((json_path, pdf_path, int(match.group(1))))
    return sorted(result, key=lambda item: item[2], reverse=True)


def pending_RETAIL_UP_label_pdfs(
    day: datetime.date | None = None,
    download_directories: list[Path] | None = None,
) -> list[Path]:
    """Return today's Label PDFs with an absent or incomplete JSON document."""
    from pdf_label_ocr import find_pending_label_pdfs

    today = datetime.date.today()
    if day is not None:
        today = day
    pending = []
    for directory in download_directories or B2B_download_directories():
        pending.extend(find_pending_label_pdfs(directory, label_date=today))
    return pending


def wait_for_RETAIL_UP_label_data(
    process,
    ready_event=None,
    day: datetime.date | None = None,
    download_directories: list[Path] | None = None,
) -> list[tuple[Path, Path, int]]:
    """Wait only for the first usable page, not for the complete OCR run."""
    day = day or datetime.date.today()
    while True:
        label_files = current_RETAIL_UP_label_files(
            day, download_directories
        )
        if label_files:
            return label_files
        if ready_event is not None and ready_event.wait(timeout=0.1):
            label_files = current_RETAIL_UP_label_files(
                day, download_directories
            )
            if label_files:
                return label_files
        else:
            time.sleep(0.1)
        if not label_ocr_process_is_alive(process):
            join_finished_label_ocr_process(process)
            raise FileNotFoundError(
                f"Label PDF/JSON for {day:%d.%m.%Y} was not found in Downloads"
            )


def read_RETAIL_UP_label_document(json_path: Path) -> dict:
    """Wait for the writer lock, then read a progressive Label JSON."""
    while True:
        try:
            with portalocker.Lock(
                str(json_path),
                mode="r",
                encoding="utf-8",
                timeout=1,
            ) as file:
                document = json.load(file)
            break
        except portalocker.exceptions.LockException:
            # Keep the original scan pending until OCR finishes updating JSON.
            continue
    if not isinstance(document, dict):
        raise ValueError(f"Invalid Label JSON document: {json_path.name}")
    return document


def RETAIL_UP_statistics_line(worker: dict, order_id: str) -> str:
    """Return one RETAIL statistics line without checking its order status."""
    normalized_order = order_id.strip()
    if not re.fullmatch(r"#\d+", normalized_order):
        raise ValueError(f"Invalid order number {order_id}")

    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        raise FileNotFoundError("Today's RETAIL statistics file was not found")

    matching_line = None
    for line in read_stat_lines(stat_file):
        stored_order = retail_order_id_from_stat_line(line)
        if stored_order == normalized_order:
            matching_line = line.rstrip("\r\n")
            break
    if matching_line is None:
        raise LookupError(f"Unknown order {normalized_order}")

    return matching_line


def RETAIL_UP_ready_statistics_line(worker: dict, order_id: str) -> str:
    """Return one completed, non-cancelled RETAIL statistics line."""
    normalized_order = order_id.strip()
    matching_line = RETAIL_UP_statistics_line(worker, normalized_order)

    status = retail_stat_effective_suffix(matching_line)
    if retail_stat_is_cancelled(matching_line):
        raise ValueError(f"Cancelled order {normalized_order}")
    # Change Label keeps an existing assembly '+', while Change Order removes
    # everything before its own boundary from the effective assembly status.
    if "+" not in status:
        raise ValueError(f"Order is not completed {normalized_order}")

    return matching_line


class RETAILTrackingNumberEmptyError(LookupError):
    """A non-Postal order has no tracking number in RETAIL statistics."""


def RETAIL_UP_tracking_from_order_line(order_id: str, line: str) -> str:
    """Extract tracking while treating the explicit delivery field as truth."""
    normalized_order = order_id.strip()
    if retail_order_id_from_stat_line(line) != normalized_order:
        raise LookupError(
            f"Order {normalized_order} is missing from its statistics line"
        )
    delivery = RETAIL_delivery_from_stat_line(line)
    tracking_number = RETAIL_tracking_from_stat_line(line)
    if tracking_number:
        return tracking_number
    if delivery == "Postal":
        raise LookupError(f"Order {normalized_order} is Postal")
    raise RETAILTrackingNumberEmptyError(
        f"Order {normalized_order} tracking number is empty"
    )


def RETAIL_UP_tracking_from_statistics(
    worker: dict,
    order_id: str,
    *,
    require_ready: bool = True,
) -> str:
    """Return an order's tracking number, optionally requiring ready status."""
    normalized_order = order_id.strip()
    matching_line = (
        RETAIL_UP_ready_statistics_line(worker, normalized_order)
        if require_ready
        else RETAIL_UP_statistics_line(worker, normalized_order)
    )
    return RETAIL_UP_tracking_from_order_line(normalized_order, matching_line)


def RETAIL_UP_today_orders(worker: dict) -> dict[str, str]:
    """Return today's RETAIL statistics lines indexed by order number."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        raise FileNotFoundError("Today's RETAIL statistics file was not found")

    orders: dict[str, str] = {}
    for line in read_stat_lines(stat_file):
        order_id = retail_order_id_from_stat_line(line)
        if order_id is not None and order_id not in orders:
            orders[order_id] = line.rstrip("\r\n")
    return orders


def parse_RETAIL_UP_print_label_command(
    line: str,
) -> tuple[str, str | None]:
    """Parse Print_Label:first[;last], keeping an empty last value optional."""
    prefix = "Print_Label:"
    if not line.startswith(prefix):
        raise ValueError("Invalid Print_Label command")

    parameters = [value.strip() for value in line[len(prefix):].split(";")]
    if len(parameters) not in {1, 2} or not parameters[0]:
        raise ValueError(
            "Print_Label command must contain one or two order numbers"
        )

    first_order = parameters[0]
    last_order = parameters[1] if len(parameters) == 2 else ""
    for order_id in (first_order, last_order):
        if order_id and not re.fullmatch(r"#\d+", order_id):
            raise ValueError(f"Invalid order number {order_id}")
    return first_order, last_order or None


def append_RETAIL_UP_print_name(worker: dict, order_id: str, name: str) -> bool:
    """Append a packing name and return whether one was already recorded."""
    normalized_order = order_id.strip()
    stat_file = retail_statistics_file(worker)
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        for index, line in enumerate(lines):
            if retail_order_id_from_stat_line(line) != normalized_order:
                continue
            if retail_stat_is_cancelled(line):
                raise ValueError(f"Cancelled order {normalized_order}")
            duplicate = retail_stat_packing_name(line) is not None
            lines[index] = line.rstrip("\r\n") + f" {name}\n"
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, stat_file, lines)
            return duplicate
    raise LookupError(f"Unknown order {normalized_order}")


def RETAIL_UP_shipping_progress_from_lines(
    lines: list[str],
) -> tuple[int, int, int]:
    """Count orders and those having a printer name after + or (+)."""
    total = 0
    completed = 0
    for line in lines:
        if retail_order_id_from_stat_line(line) is None:
            continue
        total += 1
        if (
            not retail_stat_is_cancelled(line)
            and retail_stat_packing_name(line) is not None
        ):
            completed += 1
    return completed, total - completed, total


def RETAIL_UP_printed_name(status: str) -> str | None:
    """Return text recorded after +/(+), or None before the first packing."""
    match = re.search(r"(?:\(\+\)|\+)\s+(.+?)\s*$", status)
    if match is None or not match.group(1).strip():
        return None
    return match.group(1).strip()


def RETAIL_UP_shipping_progress_by_delivery_from_lines(
    lines: list[str],
) -> tuple[int, int, int, int, int]:
    """Return completed UPS/Zasilkovna/Postal counts, left and total."""
    completed_ups = 0
    completed_zasilkovna = 0
    completed_postal = 0
    total = 0

    for line in lines:
        order_id = retail_order_id_from_stat_line(line)
        if order_id is None:
            continue
        total += 1
        if (
            retail_stat_is_cancelled(line)
            or retail_stat_packing_name(line) is None
        ):
            continue

        delivery = RETAIL_delivery_from_stat_line(line)
        if delivery == "UPS":
            completed_ups += 1
        elif delivery == "Zasilkovna":
            completed_zasilkovna += 1
        else:
            completed_postal += 1

    completed = completed_ups + completed_zasilkovna + completed_postal
    return (
        completed_ups,
        completed_zasilkovna,
        completed_postal,
        total - completed,
        total,
    )


def RETAIL_UP_shipping_progress(worker: dict) -> tuple[int, int, int]:
    """Return completed-for-shipping, left and all RETAIL orders for today."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        return 0, 0, 0
    return RETAIL_UP_shipping_progress_from_lines(read_stat_lines(stat_file))


def RETAIL_UP_shipping_progress_by_delivery(
    worker: dict,
) -> tuple[int, int, int, int, int]:
    """Read today's detailed packing progress from RETAIL statistics."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        return 0, 0, 0, 0, 0
    return RETAIL_UP_shipping_progress_by_delivery_from_lines(
        read_stat_lines(stat_file)
    )


def RETAIL_UP_left_by_delivery_from_lines(
    lines: list[str],
) -> tuple[int, int, int]:
    """Return unpacked, non-cancelled UPS, Packeta and Postal counts."""
    left = {delivery: 0 for delivery in RETAIL_DELIVERY_ORDER}
    for line in lines:
        if retail_order_id_from_stat_line(line) is None:
            continue
        if retail_stat_is_cancelled(line):
            continue
        if retail_stat_packing_name(line) is not None:
            continue
        left[RETAIL_delivery_from_stat_line(line)] += 1
    return left["UPS"], left["Zasilkovna"], left["Postal"]


def RETAIL_UP_left_and_total_by_delivery_from_lines(
    lines: list[str],
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Return remaining/total active orders for each delivery type."""
    left = {delivery: 0 for delivery in RETAIL_DELIVERY_ORDER}
    total = {delivery: 0 for delivery in RETAIL_DELIVERY_ORDER}
    for line in lines:
        if retail_order_id_from_stat_line(line) is None:
            continue
        # Cancelled orders are not work and therefore are not part of either
        # side of LEFT n/x. If Cancelled is removed, the next refresh includes
        # that order again.
        if retail_stat_is_cancelled(line):
            continue
        delivery = RETAIL_delivery_from_stat_line(line)
        total[delivery] += 1
        if retail_stat_packing_name(line) is None:
            left[delivery] += 1
    return (
        (left["UPS"], total["UPS"]),
        (left["Zasilkovna"], total["Zasilkovna"]),
        (left["Postal"], total["Postal"]),
    )


def RETAIL_UP_left_by_delivery(worker: dict) -> tuple[int, int, int]:
    """Read today's remaining packing work grouped by delivery type."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        return 0, 0, 0
    return RETAIL_UP_left_by_delivery_from_lines(read_stat_lines(stat_file))


def RETAIL_UP_shipping_header(worker: dict) -> str:
    """Build the compact first display line with remaining work counts."""
    stat_file = retail_statistics_file(worker)
    if stat_file.is_file():
        grouped = RETAIL_UP_left_and_total_by_delivery_from_lines(
            read_stat_lines(stat_file)
        )
    else:
        grouped = ((0, 0), (0, 0), (0, 0))
    (ups_left, ups_total), (packeta_left, packeta_total), (
        postal_left,
        postal_total,
    ) = grouped
    return (
        f"* LEFT UPS: {ups_left}/{ups_total}, "
        f"Packeta: {packeta_left}/{packeta_total}, "
        f"Postal: {postal_left}/{postal_total}"
    )


def send_RETAIL_UP_display_state(
    worker: dict,
    tail_lines: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Refresh the shared progress header while preserving local details."""
    if tail_lines is not None:
        worker["RETAIL_UP_LAST_TAIL"] = [
            str(line) for line in tail_lines if str(line)
        ]
    remembered_tail = worker.get("RETAIL_UP_LAST_TAIL", [])
    message = "\n".join((
        RETAIL_UP_shipping_header(worker),
        *remembered_tail,
    ))
    Printer(message)
    send(worker["DISPLAY"], message)
    return message


def RETAIL_check_fragments(settings: dict) -> list[str]:
    """Read CHECK fragments from either a dictionary, list or single string."""
    configured = settings.get("CHECK", {})
    if isinstance(configured, dict):
        values = [
            value.strip()
            for value in configured.values()
            if isinstance(value, str) and value.strip()
        ]
        candidates = values or [str(key).strip() for key in configured]
    elif isinstance(configured, list):
        candidates = [str(value).strip() for value in configured]
    elif isinstance(configured, str):
        candidates = [configured.strip()]
    else:
        candidates = []

    unique = []
    seen = set()
    for candidate in candidates:
        key = candidate.casefold()
        if candidate and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def RETAIL_UP_is_product_item(name: str, settings: dict) -> bool:
    """Return whether an order name resolves to a PRODUCTS catalogue value."""
    products = settings.get("PRODUCTS", {})
    if not isinstance(products, dict):
        return False
    product_names = {
        normalize(value).casefold()
        for value in products.values()
        if isinstance(value, str) and value.strip()
    }
    return product_base_name(name, settings).casefold() in product_names


def RETAIL_UP_check_lines(
    worker: dict,
    order_id: str,
    settings: dict,
) -> list[str]:
    """Return packing warnings, excluding every Bonus position."""
    fragments = RETAIL_check_fragments(settings)
    fragment_keys = [fragment.casefold() for fragment in fragments]
    today = datetime.date.today()
    for pdf_date, _part, pdf_path in reversed(
        _available_order_pdfs(worker.get("DOWNLOAD_DIRECTORIES"))
    ):
        if pdf_date != today:
            continue
        try:
            orders, _metadata = load_or_create_RETAIL_order_cache(
                pdf_path, settings
            )
        except Exception as error:
            Printer(f"XXX Cannot load {pdf_path.name} for CHECK: {error}")
            continue
        matched_order = next(
            (
                items
                for stored_order, items in orders.items()
                if stored_order.lstrip("#") == order_id.lstrip("#")
            ),
            None,
        )
        if matched_order is None:
            continue
        result = []
        for name, quantity in matched_order:
            if is_bonus_item(name):
                continue
            is_product = RETAIL_UP_is_product_item(name, settings)
            matches_check = any(
                fragment in name.casefold() for fragment in fragment_keys
            )
            if is_product and not matches_check:
                continue
            line = f"{quantity} {name}"
            # Every position outside PRODUCTS is a packing warning, including
            # Merch and completely unknown items. An explicit tag keeps it
            # yellow even if OTHER colour rules contain a matching fragment.
            result.append(f"#ffff00 {line}" if not is_product else line)
        return result
    Printer(f"? Order data for packing checks not found: {order_id}")
    return []


def normalize_RETAIL_UP_tracking_number(tracking_number: str) -> str:
    """Normalize a tracking value for statistics/Label JSON comparisons."""
    return re.sub(r"[^A-Za-z0-9]", "", str(tracking_number)).upper()


def RETAIL_UP_label_tracking_sequence(worker: dict) -> list[str]:
    """Return today's Label tracking numbers in PDF part/page order."""
    sequence = []
    label_files = sorted(
        current_RETAIL_UP_label_files(
            download_directories=worker.get("DOWNLOAD_DIRECTORIES")
        ),
        key=lambda item: item[2],
    )
    if not label_files:
        raise FileNotFoundError("Today's Label PDF/JSON was not found")

    for json_path, _pdf_path, _part_number in label_files:
        document = read_RETAIL_UP_label_document(json_path)

        page_entries = []
        for position, (tracking_number, label_data) in enumerate(
            document.items()
        ):
            if not isinstance(label_data, dict):
                raise ValueError(
                    f"Invalid Label JSON entry for {tracking_number}"
                )
            try:
                page_number = int(label_data["PageNumber"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid PageNumber for {tracking_number}"
                ) from error
            if page_number < 1:
                raise ValueError(f"Invalid PageNumber for {tracking_number}")
            normalized_tracking = normalize_RETAIL_UP_tracking_number(
                tracking_number
            )
            if not normalized_tracking:
                raise ValueError(
                    f"Empty tracking number in {json_path.name}"
                )
            page_entries.append(
                (page_number, position, normalized_tracking)
            )

        sequence.extend(
            tracking_number
            for _page, _position, tracking_number in sorted(page_entries)
        )
    return sequence


def find_RETAIL_UP_label_page(
    worker: dict,
    tracking_number: str,
) -> tuple[Path, int, str]:
    """Find a tracking number and its label type in today's Label JSON."""
    expected = normalize_RETAIL_UP_tracking_number(tracking_number)
    for json_path, pdf_path, _part_number in current_RETAIL_UP_label_files(
        download_directories=worker.get("DOWNLOAD_DIRECTORIES")
    ):
        document = read_RETAIL_UP_label_document(json_path)
        actual_key = next(
            (
                key
                for key in document
                if normalize_RETAIL_UP_tracking_number(key) == expected
            ),
            None,
        )
        if actual_key is None:
            continue
        label_data = document.get(actual_key)
        if not isinstance(label_data, dict):
            raise ValueError(f"Invalid Label JSON entry for {tracking_number}")
        try:
            page_number = int(label_data["PageNumber"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"Invalid PageNumber for {tracking_number}"
            ) from error
        if page_number < 1:
            raise ValueError(f"Invalid PageNumber for {tracking_number}")
        label_type = str(label_data.get("LabelType", "")).strip().casefold()
        if label_type == "packeta":
            normalized_label_type = "Packeta"
        elif label_type == "ups":
            normalized_label_type = "UPS"
        else:
            # JSON files created by older OCR versions have no LabelType.
            # Their tracking-number formats still distinguish both layouts.
            normalized_label_type = (
                "Packeta" if expected.startswith("Z") else "UPS"
            )
        return pdf_path, page_number, normalized_label_type
    raise LookupError(f"Tracking number not found in Label JSON: {tracking_number}")


def change_label_page_type(pdf_path: Path, page_number: int) -> str:
    """Distinguish Packeta replacement labels for print orientation."""
    import pdfplumber

    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_number < 1 or page_number > len(pdf.pages):
                raise ValueError(
                    f"Invalid page {page_number} in {pdf_path.name}"
                )
            text = pdf.pages[page_number - 1].extract_text() or ""
    except Exception as error:
        raise ValueError(
            f"Cannot inspect page {page_number} in {pdf_path.name}: {error}"
        ) from error
    return "Packeta" if "packeta.com" in text.casefold() else "UPS"


def find_RETAIL_UP_change_label_page(
    worker: dict,
    order_id: str,
) -> tuple[Path, int, str] | None:
    """Find the newest replacement label by its order position in filename."""
    normalized_order = f"#{order_id.strip().lstrip('#')}"
    for pdf_path in reversed(
        available_RETAIL_change_label_pdfs(
            worker.get("DOWNLOAD_DIRECTORIES")
        )
    ):
        order_ids = RETAIL_change_label_pdf_order_ids(pdf_path)
        if normalized_order not in order_ids:
            continue
        page_number = order_ids.index(normalized_order) + 1
        reader = read_pdf_without_leading_junk(pdf_path)
        if page_number > len(reader.pages):
            raise ValueError(
                f"{pdf_path.name} has no page {page_number} for "
                f"{normalized_order}"
            )
        return (
            pdf_path,
            page_number,
            change_label_page_type(pdf_path, page_number),
        )
    return None


def find_order_label_browser() -> Path:
    """Find a Chromium browser capable of rendering the SVG label."""
    candidates = []
    for variable, relative_paths in (
        (
            "ProgramFiles",
            (
                Path("Google/Chrome/Application/chrome.exe"),
                Path("Microsoft/Edge/Application/msedge.exe"),
            ),
        ),
        (
            "ProgramFiles(x86)",
            (
                Path("Google/Chrome/Application/chrome.exe"),
                Path("Microsoft/Edge/Application/msedge.exe"),
            ),
        ),
        (
            "LOCALAPPDATA",
            (Path("Google/Chrome/Application/chrome.exe"),),
        ),
    ):
        base = os.environ.get(variable)
        if base:
            candidates.extend(Path(base) / relative for relative in relative_paths)
    browser = next((path for path in candidates if path.is_file()), None)
    if browser is None:
        raise FileNotFoundError("Google Chrome or Microsoft Edge was not found")
    return browser


def order_label_settings(settings: dict | None = None) -> dict[str, float]:
    """Return validated order-label dimensions and offsets in millimetres."""
    settings = load_settings() if settings is None else settings
    configured = settings.get("ORDER_LABEL", {})
    if not isinstance(configured, dict):
        raise ValueError("ORDER_LABEL must be a dictionary")

    result = {}
    for key, default_value in DEFAULT_ORDER_LABEL_SETTINGS.items():
        raw_value = configured.get(key, default_value)
        if isinstance(raw_value, bool):
            raise ValueError(f"ORDER_LABEL.{key} must be a number")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"ORDER_LABEL.{key} must be a number"
            ) from error
        if not math.isfinite(value):
            raise ValueError(f"ORDER_LABEL.{key} must be finite")
        if key in {"width_mm", "height_mm"} and value <= 0:
            raise ValueError(f"ORDER_LABEL.{key} must be greater than zero")
        result[key] = value
    return result


def order_label_size_text(width_mm: float, height_mm: float) -> str:
    return f"{width_mm:g}x{height_mm:g}-mm"


def open_order_label_printer(
    printer_name: str,
    width_mm: float,
    height_mm: float,
):
    """Create a Windows printer DC using configured label dimensions."""
    size_text = order_label_size_text(width_mm, height_mm)
    handle = win32print.OpenPrinter(printer_name)
    try:
        mode = win32print.GetPrinter(handle, 2).get("pDevMode")
        if mode is None:
            raise RuntimeError(f"Cannot read printer settings: {printer_name}")
        mode.Orientation = win32con.DMORIENT_PORTRAIT
        mode.Copies = 1
        mode.PaperSize = win32con.DMPAPER_USER
        # DEVMODE custom paper dimensions are expressed in tenths of a
        # millimetre, regardless of whether Windows shows inches in its UI.
        mode.PaperWidth = round(width_mm * 10)
        mode.PaperLength = round(height_mm * 10)
        mode.Fields &= ~win32con.DM_FORMNAME
        mode.Fields |= (
            win32con.DM_ORIENTATION
            | win32con.DM_COPIES
            | win32con.DM_PAPERSIZE
            | win32con.DM_PAPERWIDTH
            | win32con.DM_PAPERLENGTH
        )
        result = win32print.DocumentProperties(
            0,
            handle,
            printer_name,
            mode,
            mode,
            win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
        )
        if result != win32con.IDOK:
            raise RuntimeError(
                f"Printer rejected {size_text} settings: {result}"
            )
        dc = win32ui.CreateDCFromHandle(
            win32gui.CreateDC("WINSPOOL", printer_name, mode)
        )
    finally:
        win32print.ClosePrinter(handle)

    dpi_x = dc.GetDeviceCaps(88)
    dpi_y = dc.GetDeviceCaps(90)
    actual_width = dc.GetDeviceCaps(110) * 25.4 / dpi_x
    actual_height = dc.GetDeviceCaps(111) * 25.4 / dpi_y
    if (
        abs(actual_width - width_mm) > 1
        or abs(actual_height - height_mm) > 1
    ):
        dc.DeleteDC()
        raise RuntimeError(
            f"Printer did not accept {size_text} paper "
            f"({actual_width:.2f}x{actual_height:.2f} mm)"
        )
    return dc, dpi_x, dpi_y


def render_order_label(
    template_path: Path,
    data: dict[str, str],
    dpi_x: int,
    dpi_y: int,
    width_mm: float,
    height_mm: float,
) -> Image.Image:
    """Fill one SVG template and return its printer-resolution bitmap."""
    if not template_path.is_file():
        raise FileNotFoundError(f"Order label template not found: {template_path.name}")
    root = ET.parse(template_path).getroot()
    elements = {}
    expected_ids = {*data, "QR-code"}
    for node in root.iter():
        node_id = node.get("id")
        if node_id not in expected_ids:
            continue
        if node_id in elements:
            raise ValueError(f"Duplicate SVG field: {node_id}")
        elements[node_id] = node
    missing = expected_ids - elements.keys()
    if missing:
        raise ValueError("Missing SVG fields: " + ", ".join(sorted(missing)))

    for key, value in data.items():
        node = elements[key]
        spans = node.findall(f"{{{ORDER_LABEL_SVG_NS}}}tspan")
        if len(spans) != 1:
            raise ValueError(f"Expected one SVG text span in {key}")
        spans[0].text = value
        node.set("font-family", "Arial")

    qr_group = elements["QR-code"]
    qr_box = qr_group.find(f"{{{ORDER_LABEL_SVG_NS}}}rect")
    if qr_box is None:
        raise ValueError("QR-code must contain a rectangle")
    transform_match = re.fullmatch(
        r"translate\(([-\d.]+)[ ,]+([-\d.]+)\)",
        qr_box.get("transform", ""),
    )
    if transform_match is None:
        raise ValueError("Unexpected QR rectangle transform")
    qr_x, qr_y = map(float, transform_match.groups())
    qr_width = float(qr_box.get("width"))
    qr_height = float(qr_box.get("height"))
    qr_group.remove(qr_box)

    view_box = root.get("viewBox", "").split()
    if len(view_box) != 4:
        raise ValueError("SVG template has no valid viewBox")
    _, _, view_width, view_height = map(float, view_box)
    width = round(width_mm * dpi_x / 25.4)
    height = round(height_mm * dpi_y / 25.4)
    ET.register_namespace("", ORDER_LABEL_SVG_NS)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    root.set("width", str(width))
    root.set("height", str(height))

    browser = find_order_label_browser()
    with tempfile.TemporaryDirectory(prefix="AutoChecker-order-label-") as temporary:
        folder = Path(temporary)
        html_path = folder / "label.html"
        image_path = folder / "label.png"
        html_path.write_text(
            '<!doctype html><meta charset="utf-8"><style>'
            "html,body{margin:0;background:white;overflow:hidden}"
            "svg{display:block}</style>"
            + ET.tostring(root, encoding="unicode"),
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(browser),
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--no-proxy-server",
                f"--user-data-dir={folder / 'profile'}",
                f"--window-size={max(width, 800)},{max(height, 600)}",
                f"--screenshot={image_path}",
                html_path.as_uri(),
            ],
            check=True,
            timeout=45,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        with Image.open(image_path) as rendered:
            image = rendered.convert("RGB").crop((0, 0, width, height))

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=4,
    )
    qr.add_data(data["OrderNumber"])
    qr.make(fit=True)
    modules = len(qr.get_matrix())
    scale = int(
        min(
            qr_width / view_width * width,
            qr_height / view_height * height,
        ) // modules
    )
    if scale < 3:
        raise ValueError("QR area is too small for reliable printing")
    qr.box_size = scale
    qr_image = qr.make_image(
        fill_color="black",
        back_color="white",
    ).convert("RGB")
    left = round(
        qr_x / view_width * width
        + (qr_width / view_width * width - qr_image.width) / 2
    )
    top = round(
        qr_y / view_height * height
        + (qr_height / view_height * height - qr_image.height) / 2
    )
    image.paste(qr_image, (left, top))
    return image


def load_DYMO_sdk():
    """Start the DYMO .NET bridge only when a DYMO printer is selected."""
    try:
        return importlib.import_module("dymo_sdk")
    except Exception as error:
        raise RuntimeError(f"Cannot start DYMO SDK: {error}") from error


def connected_DYMO_printer(printer_name: str):
    """Return the connected DYMO SDK printer matching a Windows name."""
    dsdk = load_DYMO_sdk()
    selected = printer_name.casefold()
    for printer in dsdk.get_printers():
        candidate = str(getattr(printer, "name", "")).strip()
        candidate_folded = candidate.casefold()
        if not getattr(printer, "is_connected", False):
            continue
        if (
            selected == candidate_folded
            or selected in candidate_folded
            or candidate_folded in selected
        ):
            return printer
    raise RuntimeError(f"Connected DYMO printer not found: {printer_name}")


def print_DYMO_order_label(
    printer_name: str,
    template_path: Path,
    data: dict[str, str],
) -> str:
    """Fill and print one native DYMO order-label template."""
    if not template_path.is_file():
        raise FileNotFoundError(
            f"DYMO order label template not found: {template_path.name}"
        )
    dsdk = load_DYMO_sdk()
    printer = connected_DYMO_printer(printer_name)
    label = dsdk.DymoLabel(filepath=str(template_path))
    values = {
        **data,
        "QR-code": data["OrderNumber"],
    }
    missing = []
    for object_name, value in values.items():
        label_object = label.get_label_object(object_name)
        if label_object is None:
            missing.append(object_name)
            continue
        label_object.update_data(str(value))
    if missing:
        raise ValueError(
            f"Missing DYMO fields in {template_path.name}: "
            + ", ".join(missing)
        )
    printer.print_label(
        label,
        roll_selected=DYMO_ROLL_SELECTED,
        barcode_graphics_quality=True,
    )
    time.sleep(DYMO_PRINT_DELAY)
    return "DYMO"


def print_RETAIL_order_label(worker: dict, order_id: str) -> int | str:
    """Render and submit one normal or STEALTH RETAIL order label."""
    printer_name = str(worker.get("PRINTER", "")).strip()
    if not printer_name:
        raise ValueError("Printer is not selected")
    metadata = worker.get("RETAIL_ORDER_META", {}).get(order_id)
    if not isinstance(metadata, dict):
        raise LookupError(f"Order metadata not found: {order_id}")
    is_stealth = str(metadata.get("stealth", "NO")).upper() == "YES"
    customer_name = re.sub(
        r"\s+", " ", str(metadata.get("customer_name", ""))
    ).strip()
    if not customer_name:
        raise LookupError(f"Customer name not found: {order_id}")
    data = {
        "NAME": customer_name,
        "OrderNumber": order_id,
        "Delivery": normalize_RETAIL_delivery(metadata.get("delivery", "")),
        "Date": f"{datetime.date.today():%d.%m.%Y}",
    }

    if "dymo" in printer_name.casefold():
        template_path = (
            DYMO_STEALTH_ORDER_LABEL_TEMPLATE
            if is_stealth
            else DYMO_ORDER_LABEL_TEMPLATE
        )
        return print_DYMO_order_label(printer_name, template_path, data)

    template_path = (
        STEALTH_ORDER_LABEL_TEMPLATE if is_stealth else ORDER_LABEL_TEMPLATE
    )

    label_settings = order_label_settings()
    width_mm = label_settings["width_mm"]
    height_mm = label_settings["height_mm"]
    dc, dpi_x, dpi_y = open_order_label_printer(
        printer_name,
        width_mm,
        height_mm,
    )
    document_started = False
    try:
        image = render_order_label(
            template_path,
            data,
            dpi_x,
            dpi_y,
            width_mm,
            height_mm,
        )
        job_id = dc.StartDoc(f"AutoChecker order {order_id}")
        document_started = True
        dc.StartPage()
        physical_offset_x = dc.GetDeviceCaps(112)
        physical_offset_y = dc.GetDeviceCaps(113)
        configured_offset_x = round(
            label_settings["offset_x_mm"] * dpi_x / 25.4
        )
        configured_offset_y = round(
            label_settings["offset_y_mm"] * dpi_y / 25.4
        )
        left = -physical_offset_x + configured_offset_x
        top = -physical_offset_y + configured_offset_y
        ImageWin.Dib(image).draw(
            dc.GetHandleOutput(),
            (
                left,
                top,
                left + image.width,
                top + image.height,
            ),
        )
        dc.EndPage()
        dc.EndDoc()
        document_started = False
        return job_id
    except BaseException:
        if document_started:
            dc.AbortDoc()
        raise
    finally:
        dc.DeleteDC()


def print_pdf_page(
    pdf_path: Path,
    page_number: int,
    printer_name: str,
    label_type: str,
) -> None:
    """Print UPS unchanged or rotate a Packeta page 90 degrees clockwise."""
    document = pymupdf.open(pdf_path)
    printer_handle = None
    printer_dc = None
    document_started = False
    try:
        if page_number < 1 or page_number > len(document):
            raise ValueError(
                f"Page {page_number} is outside PDF range 1..{len(document)}"
            )
        page = document[page_number - 1]
        pixmap = page.get_pixmap(
            dpi=RETAIL_UP_PRINTER_DPI,
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        image = Image.frombytes(
            "RGB",
            (pixmap.width, pixmap.height),
            pixmap.samples,
        )
        is_packeta = label_type.casefold() == "packeta"
        if is_packeta:
            image = image.transpose(Image.Transpose.ROTATE_270)

        printer_handle = win32print.OpenPrinter(printer_name)
        printer_info = win32print.GetPrinter(printer_handle, 2)
        devmode_size = win32print.DocumentProperties(
            0,
            printer_handle,
            printer_name,
            None,
            None,
            0,
        )
        if devmode_size <= 0:
            raise RuntimeError("Cannot read printer DEVMODE size")
        driver_extra = max(
            0,
            devmode_size - pywintypes.DEVMODEType().Size,
        )
        devmode = pywintypes.DEVMODEType(driver_extra)
        devmode.Fields |= (
            win32con.DM_ORIENTATION
            | win32con.DM_PAPERSIZE
            | win32con.DM_PAPERWIDTH
            | win32con.DM_PAPERLENGTH
        )
        devmode.Orientation = win32con.DMORIENT_PORTRAIT
        devmode.PaperSize = getattr(win32con, "DMPAPER_USER", 256)
        devmode.PaperWidth = RETAIL_UP_PAPER_WIDTH_TENTHS_MM
        devmode.PaperLength = RETAIL_UP_PAPER_HEIGHT_TENTHS_MM
        properties_result = win32print.DocumentProperties(
            0,
            printer_handle,
            printer_name,
            devmode,
            devmode,
            win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
        )
        if properties_result < 0:
            raise RuntimeError("Printer rejected 4x6-inch paper settings")

        printer_dc = win32gui.CreateDC(
            printer_info["pPrintProcessor"],
            printer_name,
            devmode,
        )
        printable_width = win32print.GetDeviceCaps(
            printer_dc,
            win32con.HORZRES,
        )
        printable_height = win32print.GetDeviceCaps(
            printer_dc,
            win32con.VERTRES,
        )
        if printable_width <= 0 or printable_height <= 0:
            raise RuntimeError("Printer returned an invalid printable area")

        scale = min(
            printable_width / image.width,
            printable_height / image.height,
        ) * RETAIL_UP_PRINT_SCALE
        target_width = max(1, round(image.width * scale))
        target_height = max(1, round(image.height * scale))
        print_rectangle = (0, 0, target_width, target_height)

        win32print.StartDoc(
            printer_dc,
            (
                f"AutoChecker {pdf_path.name} page {page_number}",
                None,
                None,
                0,
            ),
        )
        document_started = True
        win32print.StartPage(printer_dc)
        ImageWin.Dib(image).draw(
            printer_dc,
            print_rectangle,
        )
        win32print.EndPage(printer_dc)
        win32print.EndDoc(printer_dc)
        document_started = False
    except Exception:
        if document_started and printer_dc is not None:
            try:
                win32print.AbortDoc(printer_dc)
            except Exception:
                pass
        raise
    finally:
        if printer_dc is not None:
            win32gui.DeleteDC(printer_dc)
        if printer_handle is not None:
            win32print.ClosePrinter(printer_handle)
        document.close()


def submit_RETAIL_UP_label(
    worker: dict,
    settings: dict,
    order_id: str,
    *,
    parenthesize_print_name: bool = False,
) -> tuple[str, bool]:
    """Validate one order, print non-Postal labels, and record the worker."""
    printer_name = str(worker.get("PRINTER", "")).strip()
    ready_line = RETAIL_UP_ready_statistics_line(worker, order_id)
    changed_label = find_RETAIL_UP_change_label_page(worker, order_id)

    postal_order = False
    tracking_missing = False
    try:
        tracking_number = RETAIL_UP_tracking_from_order_line(
            order_id, ready_line
        )
    except RETAILTrackingNumberEmptyError:
        tracking_number = ""
        tracking_missing = True
    except LookupError as error:
        if str(error) != f"Order {order_id.strip()} is Postal":
            raise
        tracking_number = ""
        postal_order = True

    if (
        not tracking_missing
        and changed_label is None
        and retail_stat_latest_change_kind(ready_line) == "Label"
    ):
        raise FileNotFoundError(
            f"Change Label PDF not found for {order_id.strip()}"
        )

    if tracking_missing:
        # The explicit UPS/Zasilkovna field is authoritative. Record the
        # packer, but never pretend that a missing label is a Postal order.
        changed_label = None
    elif changed_label is not None:
        pdf_path, page_number, label_type = changed_label
    elif printer_name and not postal_order:
        pdf_path, page_number, label_type = find_RETAIL_UP_label_page(
            worker, tracking_number
        )

    if printer_name and not postal_order and not tracking_missing:
        print_pdf_page(pdf_path, page_number, printer_name, label_type)
        physically_printed = True
    else:
        physically_printed = False
    recorded_name = worker["NAME"]
    if physically_printed and parenthesize_print_name:
        recorded_name = f"({recorded_name})"
    duplicate_print = append_RETAIL_UP_print_name(
        worker, order_id, recorded_name
    )
    if tracking_missing:
        status_message = "XXX tracking number is empty"
    elif physically_printed:
        status_message = (
            f"? Dublicate print label {order_id.lstrip('#')} "
            f"page {page_number}"
            if duplicate_print
            else f"+ Label printed {order_id.lstrip('#')} page {page_number}"
        )
    elif postal_order:
        status_message = (
            f"+ Postal order {order_id.lstrip('#')} recorded without printing"
        )
    else:
        status_message = (
            f"+ Order {order_id.lstrip('#')} recorded without printing"
        )
    return status_message, physically_printed


def process_RETAIL_UP_scan(
    worker: dict,
    settings: dict,
    order_id: str,
    *,
    parenthesize_print_name: bool = False,
) -> bool:
    """Validate an order, print its label and show configured CHECK items."""
    check_lines = []
    try:
        status_message, _physically_printed = submit_RETAIL_UP_label(
            worker,
            settings,
            order_id,
            parenthesize_print_name=parenthesize_print_name,
        )
        check_lines = RETAIL_UP_check_lines(worker, order_id, settings)
        success = True
    except Exception as error:
        status_message = f"XXX {error}"
        success = False
    send_RETAIL_UP_display_state(
        worker,
        (status_message, *check_lines),
    )
    return success


def process_RETAIL_UP_label_range(
    worker: dict,
    settings: dict,
    first_order: str,
    last_order: str,
) -> int:
    """Print an inclusive range in Label JSON part/page order."""
    try:
        orders = RETAIL_UP_today_orders(worker)
        first_tracking = normalize_RETAIL_UP_tracking_number(
            RETAIL_UP_tracking_from_statistics(
                worker, first_order,
                require_ready=False,
            )
        )
        last_tracking = normalize_RETAIL_UP_tracking_number(
            RETAIL_UP_tracking_from_statistics(
                worker, last_order,
                require_ready=False,
            )
        )

        label_sequence = RETAIL_UP_label_tracking_sequence(worker)
        try:
            first_position = label_sequence.index(first_tracking)
        except ValueError as error:
            raise LookupError(
                f"Tracking number not found in Label JSON: {first_tracking}"
            ) from error
        try:
            last_position = label_sequence.index(last_tracking)
        except ValueError as error:
            raise LookupError(
                f"Tracking number not found in Label JSON: {last_tracking}"
            ) from error

        range_start, range_end = sorted((first_position, last_position))
        selected_trackings = label_sequence[range_start:range_end + 1]
        orders_by_tracking = {}
        for order_id, order_line in orders.items():
            try:
                tracking_number = RETAIL_UP_tracking_from_order_line(
                    order_id,
                    order_line,
                )
            except LookupError:
                continue
            orders_by_tracking.setdefault(
                normalize_RETAIL_UP_tracking_number(tracking_number),
                order_id,
            )

        status_lines = []
        processed_count = 0
        printed_count = 0
        for tracking_number in selected_trackings:
            order_id = orders_by_tracking.get(tracking_number)
            if order_id is None:
                status_lines.append(
                    f"XXX Tracking {tracking_number} not found in statistics"
                )
                continue
            if retail_stat_is_cancelled(orders[order_id]):
                status_lines.append(f"XXX {order_id} is Cancelled")
                continue
            if "+" not in retail_stat_effective_suffix(orders[order_id]):
                status_lines.append(f"XXX {order_id} not ready for packing")
                continue
            try:
                _status_message, physically_printed = submit_RETAIL_UP_label(
                    worker,
                    settings,
                    order_id,
                    parenthesize_print_name=True,
                )
                processed_count += 1
                if physically_printed:
                    printed_count += 1
            except Exception as error:
                status_lines.append(f"XXX {order_id}: {error}")
    except Exception as error:
        processed_count = 0
        printed_count = 0
        status_lines = [f"XXX {error}"]

    result_line = (
        f"+ Labels printed: {printed_count}"
        if worker.get("PRINTER")
        else f"+ Orders recorded: {processed_count}"
    )
    send_RETAIL_UP_display_state(
        worker,
        (result_line, *status_lines),
    )
    return processed_count


def find_downloaded_B2B_pdf(
    filename: str,
    download_directories: list[Path] | None = None,
) -> tuple[Path | None, list[Path]]:
    """Find a B2B PDF in the configured Windows Downloads locations."""
    directories = download_directories or B2B_download_directories()
    order_name = _normalise_B2B_disk_name(filename)
    # A newly copied SMB file can take a fraction of a second to become
    # visible to another computer. Retry briefly before falling back to
    # History or reporting an unknown order.
    for attempt in range(3):
        for directory in directories:
            found = _B2B_pdf_in_folder(Path(directory), order_name)
            if found is not None:
                return found, directories
        if attempt < 2:
            time.sleep(0.25)
    return None, directories


def prepare_B2B_order_history(
    worker: dict,
    order_id: str,
) -> tuple[Path | None, str | None]:
    """Find a B2B PDF in the required local-then-network order."""
    order_name = B2B_order_name(order_id)
    if order_name is None:
        return None, "invalid_order"

    pdf_filename = f"{order_name}.pdf"
    local_history = Path(
        worker.get("B2B_LOCAL_HISTORY", DESKTOP_DIR / "B2B" / "History")
    )
    local_downloads = [
        Path(path)
        for path in worker.get(
            "B2B_LOCAL_DOWNLOAD_DIRECTORIES",
            B2B_download_directories(),
        )
    ]
    network_history_value = worker.get("B2B_NETWORK_HISTORY")
    network_history = (
        Path(network_history_value)
        if network_history_value is not None
        else None
    )
    network_downloads = [
        Path(path)
        for path in worker.get("B2B_NETWORK_DOWNLOAD_DIRECTORIES", [])
    ]

    def path_key(path: Path) -> str:
        return str(path).rstrip("\\/").casefold()

    local_download_keys = {path_key(path) for path in local_downloads}
    if network_history is not None and path_key(network_history) == path_key(local_history):
        network_history = None
    network_downloads = [
        path
        for path in network_downloads
        if path_key(path) not in local_download_keys
    ]

    stages: list[tuple[str, str, Path | list[Path], bool]] = [
        ("local History", "history", local_history, False),
        ("local Downloads", "downloads", local_downloads, False),
    ]
    if network_history is not None:
        stages.append(("network History", "history", network_history, True))
    if network_downloads:
        stages.append((
            "network Downloads",
            "downloads",
            network_downloads,
            True,
        ))

    checked_locations: list[str] = []
    access_errors: list[str] = []
    archived_pdf: Path | None = None
    order_dir: Path | None = None

    for stage_name, stage_kind, source, is_network_source in stages:
        source_pdf: Path | None = None
        if stage_kind == "history":
            history_dir = Path(source)
            checked_locations.append(str(history_dir))
            try:
                existing_order_dir = _B2B_order_folder(
                    history_dir,
                    order_name,
                )
                if existing_order_dir is None:
                    continue
                existing_pdf = _B2B_pdf_in_folder(
                    existing_order_dir,
                    order_name,
                )
            except OSError as error:
                access_errors.append(f"{history_dir}: {error}")
                continue
            if existing_pdf is None:
                continue
            if not is_network_source:
                order_dir = existing_order_dir
                archived_pdf = existing_pdf
                print(f"* B2B PDF found in {stage_name}: {archived_pdf}")
                break
            source_pdf = existing_pdf
        else:
            download_dirs = [Path(path) for path in source]
            checked_locations.extend(str(path) for path in download_dirs)
            try:
                source_pdf, _checked_downloads = find_downloaded_B2B_pdf(
                    pdf_filename,
                    download_dirs,
                )
            except OSError as error:
                access_errors.append(f"{stage_name}: {error}")
                continue
            if source_pdf is None:
                continue

        # The network is only a source of the order PDF.  Every active B2B
        # order uses the local History folder for its PDF copy, progress file,
        # photos and index entry.
        history_dir = local_history
        try:
            history_dir.mkdir(parents=True, exist_ok=True)
            order_dir = (
                _B2B_order_folder(history_dir, order_name)
                or history_dir / order_name
            )
            # Two displays may scan the same newly downloaded order at once.
            # Lock the destination History while checking and moving the PDF.
            history_lock = history_dir / ".history.lock"
            with portalocker.Lock(
                str(history_lock), mode="a+", encoding="utf-8", timeout=30
            ):
                order_dir.mkdir(parents=True, exist_ok=True)
                existing_pdf = _B2B_pdf_in_folder(order_dir, order_name)
                if existing_pdf is not None:
                    archived_pdf = existing_pdf
                    print(
                        f"* B2B order {order_name} already has an archived "
                        f"PDF in {history_dir}; duplicate {stage_name} PDF "
                        "was left unchanged"
                    )
                else:
                    archived_pdf = order_dir / pdf_filename
                    if is_network_source:
                        shutil.copy2(str(source_pdf), str(archived_pdf))
                        operation = "copied"
                    else:
                        shutil.move(str(source_pdf), str(archived_pdf))
                        operation = "moved"
                    print(
                        f"* B2B PDF {operation} from {stage_name} to "
                        f"{archived_pdf}"
                    )
        except (OSError, shutil.Error) as error:
            print(f"XXX Cannot archive B2B PDF {source_pdf}: {error}")
            return None, "move_failed"
        break

    if archived_pdf is None or order_dir is None:
        print(
            f"? B2B PDF {pdf_filename} was not found; checked in order: "
            + " -> ".join(checked_locations)
        )
        if access_errors:
            print("XXX B2B storage access errors: " + " | ".join(access_errors))
            return None, "storage_unavailable"
        return None, "unknown_order"

    progress_file = order_dir / f"{order_name}_Sborka.txt"
    try:
        photo_dir = order_dir / "Photo"
        photo_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"XXX Cannot prepare B2B order folder {order_dir}: {error}")
        return None, "storage_unavailable"

    worker["ACTIVE_STAT_FILE"] = progress_file
    worker["B2B_PDF_NAME"] = archived_pdf.name
    worker["B2B_PDF_PATH"] = archived_pdf
    worker["B2B_ORDER_DIR"] = order_dir
    worker["FOTO"] = photo_dir
    return archived_pdf, None


def activate_B2B_order(
    worker: dict,
    order_id: str,
    pdf_path: Path,
) -> tuple[bool, str | None]:
    """Register a B2B order while using its per-order History progress file."""
    index_path = B2B_index_file(worker)
    normalized_id = order_id if order_id.startswith("#") else "#" + order_id
    with _locked_statistics_file(index_path) as file:
        file.seek(0)
        lines = file.readlines()
        for line in lines:
            parts = [part.strip() for part in line.strip().split("|")]
            if not parts or parts[0].lstrip("#") != normalized_id.lstrip("#"):
                continue
            if any(part.casefold() == "cancelled" for part in parts[3:]):
                return False, "cancelled"
            return True, None

        started = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        progress_file = worker["ACTIVE_STAT_FILE"]
        relative_progress = progress_file.relative_to(
            worker["STATISTICS"].parent
        )
        file.seek(0, 2)
        file.write(
            f"{normalized_id} | {started} | {relative_progress}\n"
        )
        file.flush()
        return True, None


def B2B_order_is_cancelled(worker: dict, order_id: str) -> bool:
    """Check cancellation before attempting to open the order PDF."""
    with _locked_statistics_file(B2B_index_file(worker)) as file:
        file.seek(0)
        lines = file.readlines()
        for line in lines:
            parts = [part.strip() for part in line.strip().split("|")]
            if parts and parts[0].lstrip("#") == order_id.lstrip("#"):
                return any(part.casefold() == "cancelled" for part in parts[3:])
    return False


def _B2B_section_bounds(lines: list[str], header: str) -> tuple[int, int] | None:
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return None
    end = next(
        (index for index in range(start + 1, len(lines))
         if lines[index].strip().startswith("----------")),
        len(lines),
    )
    return start, end


def _format_B2B_progress_section(
    header: str,
    items: list[tuple[str, int]],
    counts: defaultdict,
    extras: defaultdict,
    no_barcode_items: list[tuple[str, int]],
    contributors: dict[str, list[str]],
    errors: set[str],
    extra_contributors: dict[str, list[str]] | None = None,
    error_contributors: dict[str, list[str]] | None = None,
) -> list[str]:
    """Build aligned B2B position rows followed by recorded scan errors."""
    extra_contributors = extra_contributors or {}
    error_contributors = error_contributors or {}
    rows = []
    for name, quantity in items:
        if (name, quantity) in no_barcode_items:
            status = f"{quantity} ?"
        elif counts.get(name, 0) >= quantity:
            status = "+"
        else:
            status = f"{counts.get(name, 0)}/{quantity}"
        names = ", ".join(contributors.get(name, []))
        rows.append((status, name, names, False))
        extra_quantity = extras.get(name, 0)
        if extra_quantity:
            extra_names = ", ".join(extra_contributors.get(name, []))
            rows.append((f"{extra_quantity} !!! EXTRA", "", extra_names, True))

    status_width = max(
        (len(status) for status, _, _, is_extra in rows if not is_extra),
        default=1,
    )
    status_and_names = [
        status if is_extra else f"{status:<{status_width}} {name}"
        for status, name, _, is_extra in rows
    ]
    name_column_end = max((len(text) for text in status_and_names), default=0)

    section = [header + "\n"]
    for left_text, (_, _, names, _) in zip(status_and_names, rows):
        section.append(f"{left_text:<{name_column_end}}    NAME: {names}\n")
    for error in sorted(errors, key=str.casefold):
        names = ", ".join(error_contributors.get(error, []))
        section.append(f"XXX {error}    NAME: {names}\n")
    section.append("\n")
    return section


def load_B2B_progress(worker: dict, items: list[tuple[str, int]], counts: defaultdict,
                      extras: defaultdict,
                      errors: set[str],
                      no_barcode_items: list[tuple[str, int]]) -> None:
    """Restore counts, contributors and scan errors, creating the section if needed."""
    stat_file = worker["ACTIVE_STAT_FILE"]
    header = f"----------{worker['B2B_PDF_NAME']}----------"
    contributors: dict[str, list[str]] = {}
    extra_contributors: dict[str, list[str]] = {}
    error_contributors: dict[str, list[str]] = {}
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        bounds = _B2B_section_bounds(lines, header)
        if bounds is None:
            file.seek(0, 2)
            if lines and lines[-1].strip():
                file.write("\n")
            file.writelines(
                _format_B2B_progress_section(
                    header, items, counts, extras, no_barcode_items, contributors,
                    errors, extra_contributors, error_contributors,
                )
            )
            file.flush()
        else:
            start, end = bounds
            item_quantities = dict(items)
            match_names = worker.get("B2B_MATCH_NAMES", {})
            last_item_name = None
            for line in lines[start + 1:end]:
                stripped_line = line.strip()
                if stripped_line.startswith("XXX "):
                    error_payload = stripped_line[4:].strip()
                    if "NAME:" in error_payload:
                        error_text, error_names = error_payload.rsplit(
                            "NAME:", 1
                        )
                        error_text = error_text.strip()
                        stored_error_names = [
                            part.strip()
                            for part in error_names.split(",")
                            if part.strip()
                        ]
                    else:
                        error_text = error_payload
                        stored_error_names = []
                    if error_text:
                        errors.add(error_text)
                        error_contributors[error_text] = stored_error_names
                    continue
                if "NAME:" not in line:
                    continue
                status_text, names_text = line.rstrip("\n").split("NAME:", 1)
                extra_match = re.fullmatch(
                    r"(\d+)\s+!!!\s+EXTRA\s*",
                    status_text.strip(),
                    re.IGNORECASE,
                )
                if extra_match:
                    if last_item_name in item_quantities:
                        extra_quantity = int(extra_match.group(1))
                        extras[last_item_name] = extra_quantity
                        counts[last_item_name] = (
                            item_quantities[last_item_name] + extra_quantity
                        )
                        extra_contributors[last_item_name] = [
                            part.strip()
                            for part in names_text.split(",")
                            if part.strip()
                        ]
                    continue
                status_match = re.fullmatch(
                    r"(\+|\?|\d+\s+\?|\d+/\d+)\s+(.+?)\s*",
                    status_text.strip(),
                )
                if not status_match:
                    last_item_name = None
                    continue
                status, name = status_match.groups()
                if name not in item_quantities:
                    # Migrate progress written by versions that stored the
                    # normalized PRODUCTS name instead of the original PDF name.
                    name = next(
                        (
                            item_name for item_name, comparison_name in match_names.items()
                            if scan_match_name(comparison_name) == scan_match_name(name)
                        ),
                        name,
                    )
                    if name not in item_quantities:
                        last_item_name = None
                        continue
                last_item_name = name
                if status == "+":
                    counts[name] = item_quantities[name]
                elif "/" in status:
                    counts[name] = int(status.split("/", 1)[0])
                contributors[name] = [part.strip() for part in names_text.split(",") if part.strip()]
    worker["B2B_POSITION_NAMES"] = contributors
    worker["B2B_EXTRA_NAMES"] = extra_contributors
    worker["B2B_ERROR_NAMES"] = error_contributors


def save_B2B_progress(worker: dict, items: list[tuple[str, int]], counts: defaultdict,
                      extras: defaultdict,
                      errors: set[str],
                      no_barcode_items: list[tuple[str, int]],
                      changed_item: str | None = None) -> None:
    """Atomically rewrite one B2B order section with progress and scan errors."""
    contributors = worker.setdefault("B2B_POSITION_NAMES", {})
    extra_contributors = worker.setdefault("B2B_EXTRA_NAMES", {})
    error_contributors = worker.setdefault("B2B_ERROR_NAMES", {})
    if changed_item is not None:
        item_quantity = dict(items).get(changed_item, 0)
        target = (
            extra_contributors
            if counts.get(changed_item, 0) > item_quantity
            else contributors
        )
        names = target.setdefault(changed_item, [])
        if worker["NAME"] not in names:
            names.append(worker["NAME"])

    stat_file = worker["ACTIVE_STAT_FILE"]
    header = f"----------{worker['B2B_PDF_NAME']}----------"
    section = _format_B2B_progress_section(
        header, items, counts, extras, no_barcode_items, contributors, errors,
        extra_contributors, error_contributors,
    )

    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        bounds = _B2B_section_bounds(lines, header)
        if bounds is None:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend(section)
        else:
            start, end = bounds
            lines[start:end] = section
        file.seek(0)
        file.truncate()
        file.writelines(lines)
        file.flush()


def retail_order_id_from_stat_line(text: str) -> str | None:
    """Extract a RETAIL #OrderNumber from either old or metadata-prefixed lines."""
    match = re.search(r"(?<!\S)(#\d+)(?=\s|$)", text)
    return match.group(1) if match else None


def RETAIL_delivery_from_tracking_number(tracking_number: object) -> str:
    """Infer delivery only for legacy lines that have no explicit field."""
    normalized = normalize_RETAIL_UP_tracking_number(str(tracking_number))
    if re.fullmatch(r"1ZR[A-Z0-9]+", normalized):
        return "UPS"
    if re.fullmatch(r"Z\d{5,}", normalized):
        return "Zasilkovna"
    return "Postal"


def RETAIL_stat_prefix_values(line: str) -> tuple[str, str]:
    """Return explicit delivery and tracking from old or new stat formats."""
    order_id = retail_order_id_from_stat_line(line)
    order_position = line.find(order_id) if order_id is not None else -1
    prefix = line[:order_position].strip() if order_position >= 0 else ""
    values = prefix.split()
    explicit_delivery = (
        normalize_RETAIL_delivery(values[0]) if values else ""
    )
    tracking_index = 1 if explicit_delivery else 0
    tracking_number = ""
    if len(values) > tracking_index:
        candidate = normalize_RETAIL_UP_tracking_number(
            values[tracking_index]
        )
        if RETAIL_delivery_from_tracking_number(candidate) != "Postal":
            tracking_number = candidate
    return explicit_delivery, tracking_number


def RETAIL_tracking_from_stat_line(line: str) -> str:
    """Return the normalized tracking number without guessing from names."""
    return RETAIL_stat_prefix_values(line)[1]


def retail_stat_order_suffix(text: str) -> str:
    """Return only assembler/status text following the RETAIL order number."""
    match = re.search(r"(?<!\S)#\d+(?=\s|$)", text)
    return text[match.end():] if match else ""


def retail_stat_change_matches(text: str) -> list[re.Match]:
    """Return complete legacy/Order/Label Change markers in status text."""
    return list(
        re.finditer(
            r"(?<!\S)Change(?:\s+(Order|Label))?(?=\s|$)",
            retail_stat_order_suffix(text),
            re.IGNORECASE,
        )
    )


def retail_stat_change_kind(change_match: re.Match) -> str:
    """Normalize a legacy bare Change to the compatible Order kind."""
    explicit_kind = change_match.group(1)
    return explicit_kind.title() if explicit_kind else "Order"


def retail_stat_change_match(text: str) -> re.Match | None:
    """Return the latest complete Change marker of either kind."""
    matches = retail_stat_change_matches(text)
    return matches[-1] if matches else None


def retail_stat_order_change_match(text: str) -> re.Match | None:
    """Return the latest Change Order (including legacy bare Change)."""
    matches = [
        match
        for match in retail_stat_change_matches(text)
        if retail_stat_change_kind(match) == "Order"
    ]
    return matches[-1] if matches else None


def retail_stat_latest_change_kind(text: str) -> str | None:
    change_match = retail_stat_change_match(text)
    return retail_stat_change_kind(change_match) if change_match else None


def retail_stat_has_change(text: str) -> bool:
    """Return whether a RETAIL line contains a Change history boundary."""
    return retail_stat_change_match(text) is not None


def retail_stat_has_order_change(text: str) -> bool:
    """Return whether an order-content history boundary exists."""
    return retail_stat_order_change_match(text) is not None


def retail_stat_has_label_change(text: str) -> bool:
    """Return whether a replacement-label history boundary exists."""
    return any(
        retail_stat_change_kind(match) == "Label"
        for match in retail_stat_change_matches(text)
    )


def retail_stat_is_cancelled(text: str) -> bool:
    """Treat Cancelled anywhere in the order history as authoritative."""
    return bool(
        re.search(
            r"(?<!\S)Cancelled(?=\s|$)",
            retail_stat_order_suffix(text),
            re.IGNORECASE,
        )
    )


def retail_stat_effective_suffix(text: str) -> str:
    """Return live assembly status after the latest Change Order marker.

    Change Label does not invalidate assembly. A legacy bare Change retains
    its old meaning and is treated as Change Order.
    """
    suffix = retail_stat_order_suffix(text)
    change_match = retail_stat_order_change_match(text)
    effective = (
        suffix[change_match.end():]
        if change_match is not None
        else suffix
    )
    # A label-only boundary is packaging history, not assembly status. Remove
    # the marker so that a not-yet-assembled order remains claimable.
    return re.sub(
        r"(?<!\S)Change\s+Label(?=\s|$)",
        "",
        effective,
        flags=re.IGNORECASE,
    )


def retail_stat_packing_suffix(text: str) -> str:
    """Return packing status after the latest Order or Label change."""
    suffix = retail_stat_order_suffix(text)
    change_match = retail_stat_change_match(text)
    return suffix[change_match.end():] if change_match is not None else suffix


def retail_stat_packing_name(text: str) -> str | None:
    """Return the current packer according to the latest change kind."""
    latest_change = retail_stat_change_match(text)
    if (
        latest_change is not None
        and retail_stat_change_kind(latest_change) == "Label"
    ):
        order_suffix = retail_stat_order_suffix(text)
        latest_order_change = retail_stat_order_change_match(text)
        live_start = (
            latest_order_change.end()
            if latest_order_change is not None
            else 0
        )
        assembly_before_label = order_suffix[
            live_start:latest_change.start()
        ]
        candidate = order_suffix[latest_change.end():]
        if not re.search(
            r"(?<!\S)(?:\(\+\)|\+)(?=\s|$)",
            assembly_before_label,
        ):
            # The label changed before assembly finished. In this case text
            # before the first later '+' belongs to the assembler, and only
            # text after that completion marker can be a packer name.
            completion = re.search(
                r"(?<!\S)(?:\(\+\)|\+)(?=\s|$)",
                candidate,
            )
            if completion is None:
                return None
            candidate = candidate[completion.end():]
        candidate = re.sub(
            r"(?<!\S)Cancelled(?=\s|$)",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()
        return candidate or None
    return RETAIL_UP_printed_name(retail_stat_packing_suffix(text))


def retail_stat_metadata_values(metadata: dict) -> tuple[str, str]:
    tracking_number = re.sub(
        r"\s+", " ", str(metadata.get("tracking_number", ""))
    ).strip()
    customer_name = re.sub(
        r"\s+", " ", str(metadata.get("customer_name", ""))
    ).strip()
    return tracking_number, customer_name


def retail_stat_shipping_value(metadata: dict) -> str:
    """Return the visible `Delivery TrackNumber` statistics field."""
    tracking_number, _customer_name = retail_stat_metadata_values(metadata)
    delivery = normalize_RETAIL_delivery(metadata.get("delivery", ""))
    if not delivery:
        delivery = RETAIL_delivery_from_tracking_number(tracking_number)
    return f"{delivery} {tracking_number}".rstrip()


def normalize_RETAIL_delivery(value: object) -> str:
    """Return the canonical supported delivery name, or an empty string."""
    normalized = str(value).strip().casefold()
    return next(
        (
            delivery
            for delivery in RETAIL_DELIVERY_ORDER
            if delivery.casefold() == normalized
        ),
        "",
    )


def sorted_RETAIL_order_ids(
    parsed_orders: dict,
    metadata_by_order: dict[str, dict],
) -> list[str]:
    """Group orders as UPS, Zasilkovna, Postal while preserving PDF order."""
    delivery_rank = {
        delivery.casefold(): rank
        for rank, delivery in enumerate(RETAIL_DELIVERY_ORDER)
    }
    return sorted(
        parsed_orders,
        key=lambda order_id: delivery_rank.get(
            normalize_RETAIL_delivery(
                metadata_by_order.get(order_id, {}).get("delivery", "")
            ).casefold(),
            len(RETAIL_DELIVERY_ORDER),
        ),
    )


def RETAIL_part_totals(
    parsed_orders: dict,
    config: dict,
) -> tuple[int, int]:
    """Return (PRODUCTS/Bonus packs, all non-removed position volume)."""
    product_names = set(config.get("PRODUCTS", {}).values())
    remove_items = config.get("REMOVE_ITEMS", [])
    pack = 0
    volume = 0
    for items in parsed_orders.values():
        for item_name, quantity in items:
            if any(remove in item_name for remove in remove_items):
                continue
            volume += quantity
            if (
                product_base_name(item_name, config) in product_names
                or "bonus" in item_name.casefold()
            ):
                pack += quantity
    return pack, volume


def retail_stat_order_prefix(order_id: str, metadata: dict, *,
                             shipping_width: int | None = None,
                             customer_width: int | None = None) -> str:
    _tracking_number, customer_name = retail_stat_metadata_values(metadata)
    shipping_value = retail_stat_shipping_value(metadata)
    shipping_width = (
        len(shipping_value) if shipping_width is None else shipping_width
    )
    customer_width = (
        len(customer_name) if customer_width is None else customer_width
    )
    return (
        f"{shipping_value:<{shipping_width}}   "
        f"{customer_name:<{customer_width}}   {order_id}"
    )


def save_part_to_statistics(
    worker: dict,
    pdf_path: Path,
    parsed_orders: dict,
    config: dict,
) -> None:
    stat_file = statistics_file(worker)
    header_prefix = f"----------{pdf_path.name}----------"
    pack, volume = RETAIL_part_totals(parsed_orders, config)
    metadata_by_order = worker.get("RETAIL_ORDER_META", {})
    metadata_values = {
        order_id: retail_stat_metadata_values(
            metadata_by_order.get(order_id, {})
        )
        for order_id in parsed_orders
    }
    ordered_order_ids = sorted_RETAIL_order_ids(
        parsed_orders,
        metadata_by_order,
    )
    shipping_width = max(
        (
            len(retail_stat_shipping_value(
                metadata_by_order.get(order_id, {})
            ))
            for order_id in parsed_orders
        ),
        default=0,
    )
    customer_width = max(
        (len(values[1]) for values in metadata_values.values()),
        default=0,
    )
    natural_order_column = shipping_width + 3 + customer_width + 3
    order_column = max(natural_order_column, len(header_prefix) + 3)
    customer_width += order_column - natural_order_column
    header = (
        f"{header_prefix:<{order_column}}"
        f"Pack: {pack}, Volume: {volume}"
    )
    # Keep the lock across the complete read-modify-write operation.
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        header_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip().startswith(header_prefix)
            ),
            None,
        )
        if header_index is None:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.append(header + "\n")
            lines.extend(
                retail_stat_order_prefix(
                    order_id,
                    metadata_by_order.get(order_id, {}),
                    shipping_width=shipping_width,
                    customer_width=customer_width,
                ) + "\n"
                for order_id in ordered_order_ids
            )
            lines.append("\n")
        else:
            lines[header_index] = header + "\n"
            end_index = next(
                (
                    index
                    for index in range(header_index + 1, len(lines))
                    if lines[index].strip().startswith("----------")
                ),
                len(lines),
            )
            existing_indexes = {}
            for index in range(header_index + 1, end_index):
                order_id = retail_order_id_from_stat_line(lines[index].strip())
                if order_id:
                    existing_indexes[order_id] = index

            insertion_index = end_index
            while (
                insertion_index > header_index + 1
                and not lines[insertion_index - 1].strip()
            ):
                insertion_index -= 1

            missing_lines = []
            for order_id in ordered_order_ids:
                prefix = retail_stat_order_prefix(
                    order_id,
                    metadata_by_order.get(order_id, {}),
                    shipping_width=shipping_width,
                    customer_width=customer_width,
                )
                existing_index = existing_indexes.get(order_id)
                if existing_index is None:
                    missing_lines.append(prefix + "\n")
                    continue
                old_text = lines[existing_index].strip()
                order_match = re.search(
                    rf"(?<!\S){re.escape(order_id)}(?=\s|$)", old_text
                )
                suffix = old_text[order_match.end():] if order_match else ""
                lines[existing_index] = prefix + suffix + "\n"
            if missing_lines:
                lines[insertion_index:insertion_index] = missing_lines

            # Reorder the complete existing section too, while retaining every
            # assembler/status suffix which was already stored on its line.
            end_index = next(
                (
                    index
                    for index in range(header_index + 1, len(lines))
                    if lines[index].strip().startswith("----------")
                ),
                len(lines),
            )
            order_lines = {}
            unrecognized_lines = []
            for line in lines[header_index + 1:end_index]:
                order_id = retail_order_id_from_stat_line(line.strip())
                if order_id in parsed_orders:
                    order_lines[order_id] = line
                elif line.strip():
                    unrecognized_lines.append(line)
            sorted_section = [
                order_lines[order_id]
                for order_id in ordered_order_ids
                if order_id in order_lines
            ]
            sorted_section.extend(unrecognized_lines)
            sorted_section.append("\n")
            lines[header_index + 1:end_index] = sorted_section

        file.seek(0)
        file.truncate()
        file.writelines(lines)
        file.flush()
        mirror_statistics_lines(worker, stat_file, lines)


def migrate_RETAIL_statistics_delivery_fields(
    worker: dict,
    metadata_by_order: dict[str, dict],
) -> bool:
    """Add the explicit Delivery field to existing RETAIL order lines."""
    if not metadata_by_order:
        return False
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        return False

    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        order_ids = [
            order_id
            for line in lines
            if (order_id := retail_order_id_from_stat_line(line))
            in metadata_by_order
        ]
        if not order_ids:
            return False

        shipping_width = max(
            len(retail_stat_shipping_value(metadata_by_order[order_id]))
            for order_id in order_ids
        )
        customer_width = max(
            len(retail_stat_metadata_values(metadata_by_order[order_id])[1])
            for order_id in order_ids
        )
        header_prefixes = [
            match.group(1)
            for line in lines
            if (
                match := re.match(
                    r"^(----------.*?\.pdf----------)",
                    line.rstrip("\r\n"),
                    re.IGNORECASE,
                )
            )
        ]
        natural_order_column = shipping_width + 3 + customer_width + 3
        order_column = max(
            natural_order_column,
            max((len(prefix) + 3 for prefix in header_prefixes), default=0),
        )
        customer_width += order_column - natural_order_column

        changed = False
        for index, line in enumerate(lines):
            raw_text = line.rstrip("\r\n")
            order_id = retail_order_id_from_stat_line(raw_text)
            if order_id in metadata_by_order:
                order_match = re.search(
                    rf"(?<!\S){re.escape(order_id)}(?=\s|$)",
                    raw_text,
                )
                suffix = raw_text[order_match.end():] if order_match else ""
                replacement = retail_stat_order_prefix(
                    order_id,
                    metadata_by_order[order_id],
                    shipping_width=shipping_width,
                    customer_width=customer_width,
                ) + suffix
                if replacement != raw_text:
                    lines[index] = replacement + "\n"
                    changed = True
                continue

            header_match = re.match(
                r"^(----------.*?\.pdf----------)\s*(Pack:.*)$",
                raw_text,
                re.IGNORECASE,
            )
            if header_match is not None:
                replacement = (
                    f"{header_match.group(1):<{order_column}}"
                    f"{header_match.group(2)}"
                )
                if replacement != raw_text:
                    lines[index] = replacement + "\n"
                    changed = True

        if changed:
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, stat_file, lines)
        return changed


def find_RETAIL_pdf_statistics_section(
    statistics_dir: Path,
    pdf_path: Path,
) -> tuple[Path, list[str]] | None:
    """Return the newest daily statistics section belonging to one PDF."""
    header_prefix = f"----------{pdf_path.name}----------"
    dated_files: list[tuple[datetime.date, Path]] = []
    if statistics_dir.is_dir():
        for stat_path in statistics_dir.iterdir():
            match = re.fullmatch(
                r"(\d{2}\.\d{2}\.\d{4})\.txt",
                stat_path.name,
                re.IGNORECASE,
            )
            if match is None or not stat_path.is_file():
                continue
            try:
                stat_date = datetime.datetime.strptime(
                    match.group(1),
                    "%d.%m.%Y",
                ).date()
            except ValueError:
                continue
            dated_files.append((stat_date, stat_path))

    for _, stat_path in sorted(
        dated_files,
        key=lambda item: item[0],
        reverse=True,
    ):
        lines = read_stat_lines(stat_path)
        header_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip().startswith(header_prefix)
            ),
            None,
        )
        if header_index is None:
            continue
        end_index = next(
            (
                index
                for index in range(header_index + 1, len(lines))
                if lines[index].strip().startswith("----------")
            ),
            len(lines),
        )
        return stat_path, lines[header_index + 1:end_index]
    return None


def RETAIL_orders_for_current_day(
    worker: dict,
    pdf_path: Path,
    parsed_orders: dict,
) -> OrderedDict[str, list[tuple[str, int]]]:
    """Select all new orders or carry only free orders from an older day."""
    source = find_RETAIL_pdf_statistics_section(
        worker["STATISTICS"],
        pdf_path,
    )
    if source is None:
        Printer(f"* New RETAIL PDF | {pdf_path.name}")
        return OrderedDict(parsed_orders)

    source_path, section_lines = source
    today_path = statistics_file(worker)
    source_is_today = source_path == today_path
    selected_ids: list[str] = []
    seen_ids: set[str] = set()
    for line in section_lines:
        raw_text = line.rstrip("\r\n")
        order_id = retail_order_id_from_stat_line(raw_text)
        if (
            order_id is None
            or order_id not in parsed_orders
            or order_id in seen_ids
        ):
            continue
        if (
            not source_is_today
            and (
                retail_stat_is_cancelled(raw_text)
                or retail_stat_effective_suffix(raw_text).strip()
            )
        ):
            continue
        selected_ids.append(order_id)
        seen_ids.add(order_id)

    if source_is_today:
        Printer(
            f"* RETAIL PDF already prepared today | {pdf_path.name} | "
            f"{len(selected_ids)} order(s)"
        )
    else:
        Printer(
            f"* RETAIL PDF continued from {source_path.name} | "
            f"{len(selected_ids)}/{len(parsed_orders)} free order(s)"
        )
    return OrderedDict(
        (order_id, parsed_orders[order_id])
        for order_id in selected_ids
    )


def restore_RETAIL_wrapped_lines(text: str, tracking_pattern: re.Pattern) -> str:
    """Remove delivery prefixes and reattach their product-name continuations."""
    restored_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        cut_positions = []

        flight_index = line.rfind("✈")
        if flight_index >= 0:
            cut_positions.append(flight_index + 1)

        tracking_matches = list(tracking_pattern.finditer(line))
        if tracking_matches:
            cut_positions.append(tracking_matches[-1].end())

        if cut_positions:
            # Everything through the rightmost delivery marker belongs to the
            # PDF label/address prefix, not to an order position.
            # Preserve a leading "- xN" quantity continuation. Removing its
            # dash makes the product regex consume the next item's quantity.
            line = line[max(cut_positions):].lstrip()
            if not line:
                continue
            if "☐" not in line and restored_lines:
                restored_lines[-1] = f"{restored_lines[-1].rstrip()} {line}"
                continue

        if line:
            restored_lines.append(line)

    return "\n".join(restored_lines)


def RETAIL_order_cache_path(pdf_path: Path) -> Path:
    """Return the sibling `PDF stem (Order).json` cache path."""
    return pdf_path.with_name(pdf_path.stem + RETAIL_ORDER_CACHE_SUFFIX)


def parse_RETAIL_pdf_document(
    pdf_path: Path,
    config: dict,
) -> tuple[OrderedDict[str, list[tuple[str, int]]], dict[str, dict]]:
    """Extract RETAIL orders and metadata from one source PDF."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages).replace("\r", "\n")

    tracking_pattern = re.compile(
        r"(?<!\S)(?:1ZR[^\u2610\s-]*|Z\d{3,})(?=[\u2610\s-]|$)",
        re.IGNORECASE,
    )
    metadata_pattern = re.compile(
        r"^(?![^\n]*→)(#\d+)(?=[^#]*\u2610)"
        r".*?type( STEALTH)?"
        r"\s*\n(.*?)\s*\u2610"
        r".*?(UPS|Zasilkovna|Postal)"
        r"(.*?)"
        r"(?=^(?![^\n]*→)#\d+|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    metadata_by_order = {}
    seen_orders = set()
    for match in metadata_pattern.finditer(text):
        order_id = match.group(1)
        if order_id in seen_orders:
            continue
        seen_orders.add(order_id)
        full_order_block = match.group(0)
        tracking_match = tracking_pattern.search(full_order_block)
        metadata_by_order[order_id] = {
            "tracking_number": tracking_match.group(0) if tracking_match else "",
            "customer_name": re.sub(r"\s+", " ", match.group(3)).strip(),
            "stealth": "YES" if match.group(2) else "NO",
            "delivery": normalize_RETAIL_delivery(match.group(4)),
        }

    text = restore_RETAIL_wrapped_lines(text, tracking_pattern)
    for phrase in config["REMOVE_PHRASES"]:
        text = text.replace(phrase, "")
    blocks = re.split(
        r"(?m)^(?![^\n]*[\u2610→])[^\n]*?(#\S+)",
        text,
    )
    contents: OrderedDict[str, str] = OrderedDict()
    for index in range(1, len(blocks), 2):
        order_id = blocks[index].strip()
        contents[order_id] = contents.get(order_id, "") + " " + (blocks[index + 1] if index + 1 < len(blocks) else "")
    parsed: OrderedDict[str, list[tuple[str, int]]] = OrderedDict()
    for order_id, block in contents.items():
        matches = re.findall(r"\u2610\s+(.*?)\s*[-–]\s*x(\d+)", block, flags=re.DOTALL)
        aggregated: OrderedDict[str, int] = OrderedDict()
        for name, quantity in matches:
            cleaned = re.sub(r"\s+", " ", name).strip()
            aggregated[cleaned] = aggregated.get(cleaned, 0) + int(quantity)
        items = [(name, qty) for name, qty in aggregated.items() if not any(remove in name for remove in config["REMOVE_ITEMS"])]
        items.sort(key=lambda item: sort_key(item, config))
        parsed[order_id] = items
        metadata_by_order.setdefault(
            order_id,
            {
                "tracking_number": "",
                "customer_name": "",
                "stealth": "NO",
                "delivery": "",
            },
        )
    return parsed, metadata_by_order


def RETAIL_order_cache_document(
    parsed: dict[str, list[tuple[str, int]]],
    metadata_by_order: dict[str, dict],
) -> OrderedDict[str, dict]:
    """Build the public Order JSON schema from internal RETAIL structures."""
    document: OrderedDict[str, dict] = OrderedDict()
    for order_id, items in parsed.items():
        metadata = metadata_by_order.get(order_id, {})
        stealth = str(metadata.get("stealth", "NO")).strip().upper()
        document[order_id] = {
            "TrackNumber": str(metadata.get("tracking_number", "")),
            "CustomerName": str(metadata.get("customer_name", "")),
            "Stealth": "YES" if stealth == "YES" else "NO",
            "Delivery": normalize_RETAIL_delivery(
                metadata.get("delivery", "")
            ),
            "Order": [
                {"Name": name, "Quantity": quantity}
                for name, quantity in items
            ],
        }
    return document


def decode_RETAIL_order_cache(
    document: object,
) -> tuple[OrderedDict[str, list[tuple[str, int]]], dict[str, dict]]:
    """Validate and convert one Order JSON document to internal structures."""
    if not isinstance(document, dict):
        raise ValueError("Order JSON root must be an object")

    parsed: OrderedDict[str, list[tuple[str, int]]] = OrderedDict()
    metadata_by_order: dict[str, dict] = {}
    for order_id, entry in document.items():
        if not re.fullmatch(r"#\d+", str(order_id)) or not isinstance(entry, dict):
            raise ValueError("Invalid order entry in Order JSON")
        if not {
            "TrackNumber",
            "CustomerName",
            "Stealth",
            "Delivery",
            "Order",
        } <= entry.keys():
            raise ValueError(f"Incomplete Order JSON entry: {order_id}")
        stealth = str(entry["Stealth"]).strip().upper()
        if stealth not in {"YES", "NO"} or not isinstance(entry["Order"], list):
            raise ValueError(f"Invalid Order JSON entry: {order_id}")

        items = []
        for item in entry["Order"]:
            if (
                not isinstance(item, dict)
                or not {"Name", "Quantity"} <= item.keys()
            ):
                raise ValueError(f"Invalid item in Order JSON: {order_id}")
            name = str(item["Name"]).strip()
            quantity = item["Quantity"]
            if not name or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise ValueError(f"Invalid item in Order JSON: {order_id}")
            items.append((name, quantity))

        normalized_order_id = str(order_id)
        parsed[normalized_order_id] = items
        metadata_by_order[normalized_order_id] = {
            "tracking_number": str(entry["TrackNumber"]).strip(),
            "customer_name": str(entry["CustomerName"]).strip(),
            "stealth": stealth,
            "delivery": normalize_RETAIL_delivery(entry["Delivery"]),
        }
    return parsed, metadata_by_order


def warn_RETAIL_orders_without_tracking(
    metadata_by_order: dict[str, dict],
) -> list[str]:
    """Sound and report UPS/Packeta orders missing their tracking number."""
    missing_orders = [
        order_id
        for order_id, metadata in metadata_by_order.items()
        if normalize_RETAIL_delivery(metadata.get("delivery", ""))
        in {"UPS", "Zasilkovna"}
        and not normalize_RETAIL_UP_tracking_number(
            metadata.get("tracking_number", "")
        )
    ]
    if not missing_orders:
        return []

    play_launcher_sound("ups")
    for order_id in missing_orders:
        delivery = normalize_RETAIL_delivery(
            metadata_by_order[order_id].get("delivery", "")
        )
        print_console_red(
            f"XXX Order without tracking number: {order_id} ({delivery})"
        )
    return missing_orders


def load_or_create_RETAIL_order_cache(
    pdf_path: Path,
    config: dict,
) -> tuple[OrderedDict[str, list[tuple[str, int]]], dict[str, dict]]:
    """Load a validated Order JSON or rebuild it once from its sibling PDF."""
    cache_path = RETAIL_order_cache_path(pdf_path)
    with portalocker.Lock(
        str(cache_path), mode="a+", encoding="utf-8", timeout=120
    ) as file:
        file.seek(0)
        raw = file.read().strip()
        if raw:
            try:
                result = decode_RETAIL_order_cache(json.loads(raw))
                Printer(f"* Order JSON loaded | {cache_path.name}")
                return result
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                Printer(f"? Rebuilding invalid Order JSON {cache_path.name}: {error}")

        parsed, metadata_by_order = parse_RETAIL_pdf_document(pdf_path, config)
        if not parsed:
            raise ValueError(f"No RETAIL orders found in {pdf_path.name}")
        warn_RETAIL_orders_without_tracking(metadata_by_order)
        document = RETAIL_order_cache_document(parsed, metadata_by_order)
        file.seek(0)
        file.truncate()
        json.dump(document, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
        Printer(f"* Order JSON created | {cache_path.name}")
        return parsed, metadata_by_order


def load_RETAIL_order_cache_file(
    cache_path: Path,
) -> tuple[OrderedDict[str, list[tuple[str, int]]], dict[str, dict]]:
    """Read one existing Order JSON while waiting for its writer's lock."""
    with portalocker.Lock(
        str(cache_path), mode="r", encoding="utf-8", timeout=120
    ) as file:
        return decode_RETAIL_order_cache(json.load(file))


def product_count_catalog(config: dict) -> list[str]:
    """Return unique PRODUCTS values in their Settings.json order."""
    products = config.get("PRODUCTS", {})
    if not isinstance(products, dict):
        return []
    result = []
    seen = set()
    for value in products.values():
        name = str(value).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def product_count_base_name(name: object) -> str:
    """Remove Bonus and package size for demand-frequency statistics."""
    normalized = normalize(bonus_barcode_name(str(name)))
    return re.sub(
        r"\s*-\s*\d+(?:\+\d+)?\s*fem\s*$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()


def product_count_item_key(name: object) -> str:
    """Normalize a complete order-row name without removing package size."""
    return re.sub(r"\s+", " ", str(name)).strip().casefold()


def product_count_exclusion_keys(exclusions: object) -> set[str]:
    """Return normalized full position names stored as EXCEPTIONS keys."""
    if not isinstance(exclusions, dict):
        return set()
    return {
        product_count_item_key(name)
        for name in exclusions
        if str(name).strip()
    }


def counted_product_name(
    item_name: object,
    config: dict,
    *,
    B2B: bool = False,
) -> str | None:
    """Resolve an order row to its canonical PRODUCTS value."""
    catalog = product_count_catalog(config)
    catalog_by_name = {
        normalize(name).casefold(): name
        for name in catalog
    }
    catalog_by_base = {
        product_count_base_name(name).casefold(): name
        for name in catalog
    }
    base_name = product_count_base_name(item_name)
    direct = (
        catalog_by_name.get(normalize(base_name).casefold())
        or catalog_by_base.get(base_name.casefold())
    )
    if direct is not None or not B2B:
        return direct

    resolved = resolve_B2B_catalog_product(base_name, config)
    if resolved is None or resolved[1] != "PRODUCTS":
        return None
    resolved_name = resolved[0]
    return (
        catalog_by_name.get(normalize(resolved_name).casefold())
        or catalog_by_base.get(product_count_base_name(resolved_name).casefold())
    )


def RETAIL_product_count_contribution(
    cache_path: Path,
    config: dict,
    exclusions: set[str] | None = None,
) -> dict[str, int]:
    """Count PRODUCTS rows in both current and legacy Order JSON schemas.

    Operational loading deliberately validates every metadata field, but old
    cache files can predate fields such as Delivery. Demand statistics only
    need the Order list, so do not reject an otherwise usable historical file.
    """
    with portalocker.Lock(
        str(cache_path), mode="r", encoding="utf-8", timeout=120
    ) as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError("Order JSON root must be an object")

    counts: dict[str, int] = defaultdict(int)
    for entry in document.values():
        if not isinstance(entry, dict):
            continue
        order = entry.get("Order")
        if isinstance(order, dict):
            raw_items = list(order)
        elif isinstance(order, list):
            raw_items = []
            for item in order:
                if isinstance(item, dict):
                    item_name = item.get("Name", item.get("name"))
                elif isinstance(item, (list, tuple)) and item:
                    item_name = item[0]
                elif isinstance(item, str):
                    item_name = item
                else:
                    item_name = None
                if item_name is not None:
                    raw_items.append(item_name)
        else:
            continue

        for item_name in raw_items:
            if (
                is_bonus_item(str(item_name))
                or product_count_item_key(item_name) in (exclusions or set())
            ):
                continue
            product_name = counted_product_name(item_name, config)
            if product_name is not None:
                counts[product_name] += 1
    return dict(counts)


def B2B_product_count_contribution(
    progress_path: Path,
    config: dict,
    exclusions: set[str] | None = None,
) -> dict[str, int]:
    """Count PRODUCTS rows stored in one B2B _Sborka text file."""
    counts: dict[str, int] = defaultdict(int)
    for line in read_stat_lines(progress_path):
        if "NAME:" not in line:
            continue
        left_text = line.split("NAME:", 1)[0].strip()
        match = re.fullmatch(
            r"(?:\+|\d+/\d+|\d+\s+\?)\s+(.+?)\s*",
            left_text,
        )
        if match is None:
            continue
        item_name = match.group(1)
        if (
            is_bonus_item(item_name)
            or product_count_item_key(item_name) in (exclusions or set())
        ):
            continue
        product_name = counted_product_name(item_name, config, B2B=True)
        if product_name is not None:
            counts[product_name] += 1
    return dict(counts)


def RETAIL_product_count_sources(
    worker: dict,
) -> list[tuple[str, datetime.date, Path]]:
    """Return one recent Order JSON per RETAIL part name."""
    cutoff = datetime.date.today() - datetime.timedelta(
        days=PRODUCT_COUNT_MAX_AGE_DAYS
    )
    by_name: dict[str, tuple[str, datetime.date, Path]] = {}
    for source_date, _part, _canonical, _pdf, cache_path in (
        _available_RETAIL_order_sources(worker.get("DOWNLOAD_DIRECTORIES"))
    ):
        if source_date < cutoff or cache_path is None:
            continue
        source_id = cache_path.name
        key = source_id.casefold()
        candidate = (source_id, source_date, cache_path)
        current = by_name.get(key)
        if current is None:
            by_name[key] = candidate
            continue
        try:
            if cache_path.stat().st_mtime_ns > current[2].stat().st_mtime_ns:
                by_name[key] = candidate
        except OSError:
            pass
    return sorted(by_name.values(), key=lambda item: (item[1], item[0].casefold()))


def B2B_product_count_sources() -> list[tuple[str, datetime.date, Path]]:
    """Return recent local B2B progress files; network text is never read."""
    history = DESKTOP_DIR / "B2B" / "History"
    if not history.is_dir():
        return []
    cutoff = datetime.date.today() - datetime.timedelta(
        days=PRODUCT_COUNT_MAX_AGE_DAYS
    )
    result = []
    try:
        paths = list(history.rglob("*_Sborka.txt"))
    except OSError:
        return []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        source_date = datetime.date.fromtimestamp(stat.st_mtime)
        if source_date < cutoff:
            continue
        try:
            source_id = path.relative_to(history).as_posix()
        except ValueError:
            source_id = path.name
        result.append((source_id, source_date, path))
    return sorted(result, key=lambda item: (item[1], item[0].casefold()))


def refresh_product_count_file(
    department: str,
    sources: list[tuple[str, datetime.date, Path]],
    config: dict,
) -> bool:
    """Incrementally rebuild a 90-day PRODUCTS frequency snapshot."""
    department = department.upper()
    count_path = DESKTOP_DIR / department / "Count.json"
    count_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = product_count_catalog(config)
    parser = (
        RETAIL_product_count_contribution
        if department == "RETAIL"
        else B2B_product_count_contribution
    )

    with portalocker.Lock(
        str(count_path), mode="a+", encoding="utf-8", timeout=120
    ) as file:
        file.seek(0)
        raw = file.read().strip()
        try:
            previous = json.loads(raw) if raw else {}
        except (TypeError, json.JSONDecodeError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        previous_products = previous.get("PRODUCTS", {})
        previous_files = previous.get("PROCESSED_FILES", {})
        previous_exclusions = previous.get("EXCEPTIONS", {})
        if not isinstance(previous_products, dict):
            previous_products = {}
        if not isinstance(previous_files, dict):
            previous_files = {}
        exclusions = (
            previous_exclusions
            if isinstance(previous_exclusions, dict)
            else {}
        )
        exclusion_keys = product_count_exclusion_keys(exclusions)
        exclusion_signature = sorted(exclusion_keys)
        # Count.json is ordered by frequency, so its key order normally
        # differs from Settings.json. Only catalogue membership determines
        # whether cached per-file contributions need to be recalculated.
        catalog_changed = set(previous_products) != set(catalog)

        processed_files: OrderedDict[str, dict] = OrderedDict()
        total: dict[str, int] = defaultdict(int)
        catalog_set = set(catalog)
        for source_id, source_date, source_path in sources:
            try:
                stat = source_path.stat()
            except OSError:
                continue
            old_record = previous_files.get(source_id, {})
            can_reuse = (
                not catalog_changed
                and isinstance(old_record, dict)
                and old_record.get("date") == source_date.isoformat()
                and old_record.get("modified_ns") == stat.st_mtime_ns
                and old_record.get("size") == stat.st_size
                and old_record.get("EXCEPTIONS") == exclusion_signature
                and isinstance(old_record.get("PRODUCTS"), dict)
            )
            if can_reuse:
                contribution = {
                    str(name): int(value)
                    for name, value in old_record["PRODUCTS"].items()
                    if name in catalog_set
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                }
            else:
                try:
                    contribution = parser(
                        source_path, config, exclusion_keys
                    )
                except Exception as error:
                    print(
                        f"XXX Cannot count products in {source_path}: {error}"
                    )
                    if (
                        not catalog_changed
                        and isinstance(old_record, dict)
                        and old_record.get("EXCEPTIONS") == exclusion_signature
                        and isinstance(old_record.get("PRODUCTS"), dict)
                    ):
                        contribution = {
                            str(name): int(value)
                            for name, value in old_record["PRODUCTS"].items()
                            if name in catalog_set
                            and isinstance(value, int)
                            and not isinstance(value, bool)
                            and value >= 0
                        }
                    else:
                        continue
            contribution = {
                name: value
                for name, value in contribution.items()
                if name in catalog_set and value > 0
            }
            processed_files[source_id] = {
                "date": source_date.isoformat(),
                "modified_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "EXCEPTIONS": exclusion_signature,
                "PRODUCTS": contribution,
            }
            for name, value in contribution.items():
                total[name] += value

        catalog_order = {name: index for index, name in enumerate(catalog)}
        sorted_catalog = sorted(
            catalog,
            key=lambda name: (
                -total.get(name, 0),
                catalog_order[name],
            ),
        )
        product_counts = OrderedDict(
            (name, total.get(name, 0)) for name in sorted_catalog
        )
        unchanged = (
            "EXCEPTIONS" in previous
            and previous_exclusions == exclusions
            and previous_products == product_counts
            and previous_files == processed_files
        )
        if unchanged:
            return False

        document = OrderedDict((
            ("EXCEPTIONS", exclusions),
            ("PRODUCTS", product_counts),
            ("PROCESSED_FILES", processed_files),
            ("UPDATED_AT", datetime.datetime.now().isoformat(timespec="seconds")),
        ))
        file.seek(0)
        file.truncate()
        json.dump(document, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
    print(
        f"* {department} Count.json updated | "
        f"{len(processed_files)} file(s) | {sum(product_counts.values())} position(s)"
    )
    return True


def refresh_RETAIL_product_count(worker: dict, config: dict) -> bool:
    return refresh_product_count_file(
        "RETAIL", RETAIL_product_count_sources(worker), config
    )


def refresh_B2B_product_count(config: dict) -> bool:
    return refresh_product_count_file(
        "B2B", B2B_product_count_sources(), config
    )


def RETAIL_statistics_part_names(worker: dict) -> set[str]:
    """Return PDF part names already represented in today's statistics."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        return set()
    result = set()
    for line in read_stat_lines(stat_file):
        match = re.match(r"^-{10}(.+?\.pdf)-{10}", line.strip(), re.IGNORECASE)
        if match is not None:
            result.add(match.group(1).casefold())
    return result


def load_RETAIL_orders_from_statistics(
    worker: dict,
    config: dict,
) -> OrderedDict[str, list[tuple[str, int]]]:
    """Load every order part referenced by today's shared statistics file."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        return OrderedDict()
    stat_lines = read_stat_lines(stat_file)
    ordered_ids = []
    seen_ids = set()
    part_names = set()
    for line in stat_lines:
        header_match = re.match(
            r"^-{10}(.+?\.pdf)-{10}", line.strip(), re.IGNORECASE
        )
        if header_match is not None:
            part_names.add(header_match.group(1).casefold())
        order_id = retail_order_id_from_stat_line(line)
        if order_id is not None and order_id not in seen_ids:
            ordered_ids.append(order_id)
            seen_ids.add(order_id)

    loaded_orders: dict[str, list[tuple[str, int]]] = {}
    loaded_metadata: dict[str, dict] = {}
    for (
        _source_date,
        _part_number,
        canonical_pdf,
        pdf_path,
        cache_path,
    ) in _available_RETAIL_order_sources(worker.get("DOWNLOAD_DIRECTORIES")):
        if canonical_pdf.name.casefold() not in part_names:
            continue
        try:
            if cache_path is not None:
                parsed, metadata = load_RETAIL_order_cache_file(cache_path)
            elif pdf_path is not None:
                parsed, metadata = load_or_create_RETAIL_order_cache(
                    pdf_path, config
                )
            else:
                continue
        except Exception as error:
            Printer(f"XXX Cannot load {canonical_pdf.name}: {error}")
            continue
        loaded_orders.update(parsed)
        loaded_metadata.update(metadata)

    worker["RETAIL_ORDER_META"] = {
        order_id: loaded_metadata.get(order_id, {})
        for order_id in ordered_ids
        if order_id in loaded_orders
    }
    migrate_RETAIL_statistics_delivery_fields(
        worker,
        worker["RETAIL_ORDER_META"],
    )
    return OrderedDict(
        (order_id, loaded_orders[order_id])
        for order_id in ordered_ids
        if order_id in loaded_orders
    )


def process_pending_RETAIL_order_sources(
    worker: dict,
    config: dict,
) -> set[str]:
    """Import stable new current-day PDF/Order JSON parts into statistics."""
    processed = worker.setdefault("RETAIL_SOURCE_PROCESSED", {})
    failures = worker.setdefault("RETAIL_SOURCE_FAILURES", {})
    imported_orders: set[str] = set()
    existing_parts = RETAIL_statistics_part_names(worker)
    now = time.time()

    for (
        source_date,
        part_number,
        canonical_pdf,
        pdf_path,
        cache_path,
    ) in _available_RETAIL_order_sources(worker.get("DOWNLOAD_DIRECTORIES")):
        if source_date != datetime.date.today():
            continue
        source_paths = [
            path for path in (pdf_path, cache_path) if path is not None
        ]
        try:
            source_stats = [path.stat() for path in source_paths]
            signature = tuple(
                (stat.st_mtime_ns, stat.st_size)
                for stat in source_stats
            )
        except OSError:
            continue
        source_key = (
            source_date.isoformat(),
            part_number,
            str(canonical_pdf.parent).casefold(),
        )
        if processed.get(source_key) == signature:
            continue
        if any(
            now - stat.st_mtime < ORDER_SOURCE_SCAN_INTERVAL
            for stat in source_stats
        ):
            continue
        if canonical_pdf.name.casefold() in existing_parts:
            processed[source_key] = signature
            continue
        failed = failures.get(source_key)
        if (
            failed is not None
            and failed[0] == signature
            and time.monotonic() - failed[1] < ORDER_SOURCE_RETRY_INTERVAL
        ):
            continue

        try:
            import_lock = canonical_pdf.parent / ".AutoChecker_orders.lock"
            with portalocker.Lock(
                str(import_lock), mode="a+", encoding="utf-8", timeout=120
            ):
                # Another launcher may have imported the part while this one
                # waited for the network lock.
                existing_parts = RETAIL_statistics_part_names(worker)
                if canonical_pdf.name.casefold() in existing_parts:
                    processed[source_key] = signature
                    continue
                if cache_path is not None:
                    try:
                        parsed, metadata = load_RETAIL_order_cache_file(
                            cache_path
                        )
                    except Exception:
                        if pdf_path is None:
                            raise
                        parsed, metadata = load_or_create_RETAIL_order_cache(
                            pdf_path, config
                        )
                elif pdf_path is not None:
                    parsed, metadata = load_or_create_RETAIL_order_cache(
                        pdf_path, config
                    )
                else:
                    continue
                selected = RETAIL_orders_for_current_day(
                    worker, canonical_pdf, parsed
                )
                worker["RETAIL_ORDER_META"] = metadata
                save_part_to_statistics(
                    worker, canonical_pdf, selected, config
                )
        except Exception as error:
            failures[source_key] = (signature, time.monotonic())
            Printer(f"XXX Cannot import {canonical_pdf.name}: {error}")
            continue

        processed[source_key] = signature
        failures.pop(source_key, None)
        existing_parts.add(canonical_pdf.name.casefold())
        imported_orders.update(selected)
        Printer(
            f"* RETAIL part imported | {canonical_pdf.name} | "
            f"{len(selected)} order(s)"
        )

    try:
        refresh_RETAIL_product_count(worker, config)
    except Exception as error:
        Printer(f"XXX Cannot update RETAIL Count.json: {error}")
    return imported_orders


def RETAIL_change_pdf_order_ids(pdf_path: Path) -> list[str]:
    """Return IDs from a dated order-content Change PDF filename."""
    match = re.fullmatch(
        r"Change(?:\s+Order)?\s+(?!Label\b).+\s+"
        r"\(\d{2}\.\d{2}\.\d{4}\)\.pdf",
        pdf_path.name,
        re.IGNORECASE,
    )
    if match is None:
        return []
    result = []
    seen = set()
    for order_id in re.findall(r"#\d+", pdf_path.stem):
        if order_id not in seen:
            seen.add(order_id)
            result.append(order_id)
    return result


def RETAIL_change_label_pdf_order_ids(pdf_path: Path) -> list[str]:
    """Return IDs in page order from a dated Change Label PDF name."""
    match = re.fullmatch(
        r"Change\s+Label\s+.+\s+\(\d{2}\.\d{2}\.\d{4}\)\.pdf",
        pdf_path.name,
        re.IGNORECASE,
    )
    if match is None:
        return []
    result = []
    seen = set()
    for order_id in re.findall(r"#\d+", pdf_path.stem):
        if order_id not in seen:
            seen.add(order_id)
            result.append(order_id)
    return result


def available_RETAIL_change_pdfs(
    download_directories: list[Path] | None,
) -> list[Path]:
    """List manager Change PDFs in chronological and filesystem order."""
    result: list[tuple[datetime.date, int, str, Path]] = []
    seen = set()
    for directory in download_directories or [Path.home() / "Downloads"]:
        if not directory.is_dir():
            continue
        try:
            paths = list(directory.iterdir())
        except OSError:
            continue
        for path in paths:
            if not path.is_file() or not RETAIL_change_pdf_order_ids(path):
                continue
            path_key = str(path).casefold()
            if path_key in seen:
                continue
            seen.add(path_key)
            date_match = re.search(
                r"\((\d{2}\.\d{2}\.\d{4})\)\.pdf$",
                path.name,
                re.IGNORECASE,
            )
            try:
                change_date = datetime.datetime.strptime(
                    date_match.group(1), "%d.%m.%Y"
                ).date()
            except (AttributeError, ValueError):
                change_date = datetime.date.min
            try:
                modified_ns = path.stat().st_mtime_ns
            except OSError:
                modified_ns = 0
            result.append((change_date, modified_ns, path.name.casefold(), path))
    return [entry[-1] for entry in sorted(result)]


def available_RETAIL_change_label_pdfs(
    download_directories: list[Path] | None,
) -> list[Path]:
    """List dated replacement-label PDFs in chronological order."""
    result: list[tuple[datetime.date, int, str, Path]] = []
    seen = set()
    for directory in download_directories or [Path.home() / "Downloads"]:
        if not directory.is_dir():
            continue
        try:
            paths = list(directory.iterdir())
        except OSError:
            continue
        for path in paths:
            if not path.is_file() or not RETAIL_change_label_pdf_order_ids(path):
                continue
            path_key = str(path).casefold()
            if path_key in seen:
                continue
            seen.add(path_key)
            date_match = re.search(
                r"\((\d{2}\.\d{2}\.\d{4})\)\.pdf$",
                path.name,
                re.IGNORECASE,
            )
            try:
                change_date = datetime.datetime.strptime(
                    date_match.group(1), "%d.%m.%Y"
                ).date()
            except (AttributeError, ValueError):
                change_date = datetime.date.min
            try:
                modified_ns = path.stat().st_mtime_ns
            except OSError:
                modified_ns = 0
            result.append((change_date, modified_ns, path.name.casefold(), path))
    return [entry[-1] for entry in sorted(result)]


def merge_RETAIL_change_cache(
    cache_path: Path,
    source_pdf: Path | None,
    replacements: dict[str, list[tuple[str, int]]],
    replacement_metadata: dict[str, dict],
    config: dict,
) -> set[str]:
    """Replace matching order entries in one locked normal Order cache."""
    with portalocker.Lock(
        str(cache_path), mode="a+", encoding="utf-8", timeout=120
    ) as file:
        file.seek(0)
        raw = file.read().strip()
        parsed = None
        metadata_by_order = None
        if raw:
            try:
                parsed, metadata_by_order = decode_RETAIL_order_cache(
                    json.loads(raw)
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                parsed = None
                metadata_by_order = None
        if parsed is None:
            if source_pdf is None or not source_pdf.is_file():
                return set()
            parsed, metadata_by_order = parse_RETAIL_pdf_document(
                source_pdf, config
            )

        matched = set(parsed).intersection(replacements)
        if not matched:
            return set()
        for order_id in matched:
            parsed[order_id] = replacements[order_id]
            metadata_by_order[order_id] = replacement_metadata[order_id]

        document = RETAIL_order_cache_document(parsed, metadata_by_order)
        file.seek(0)
        file.truncate()
        json.dump(document, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
        return matched


def normalized_main_computer_name(value: object) -> str:
    """Validate the DNS/Windows name used to build support UNC paths."""
    # Accept either a plain host name or an accidentally pasted UNC prefix,
    # but never retain whitespace next to a path separator.  A value such as
    # ``"\\\\GREENPO\\ "`` previously produced invalid paths on support PCs.
    raw_name = str(value).strip().replace("/", "\\")
    name_parts = [part.strip() for part in raw_name.split("\\") if part.strip()]
    name = name_parts[0] if name_parts else ""
    if (
        not name
        or len(name) > 63
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", name)
    ):
        raise ValueError(f"Invalid main_computer_name: {value!r}")
    return name


def support_paths_for_computer(computer_name: object) -> dict[str, str]:
    """Build the fixed support paths from one main-computer name."""
    name = normalized_main_computer_name(computer_name)
    root = rf"\\{name}"
    retail_root = root + "\\" + SUPPORT_SHARE_NAMES["retail"]
    return {
        "downloads": root + "\\" + SUPPORT_SHARE_NAMES["downloads"],
        "retail": retail_root,
        "b2b": root + "\\" + SUPPORT_SHARE_NAMES["b2b"],
        "photos": retail_root + "\\Photo",
        "statistics": retail_root + "\\Statistik",
        "settings": (
            root
            + "\\"
            + SUPPORT_SHARE_NAMES["settings"]
            + "\\Settings.json"
        ),
    }


SUPPORT_AUTH_ERROR_CODES = {
    5,     # Access denied
    13,    # Python's EACCES wrapper for a Windows access-denied error
    86,    # Invalid password
    1219,  # Conflicting credentials already connected to the server
    1326,  # Unknown user name or bad password
    1330,  # Password expired
    1331,  # Account disabled
    1907,  # Password must be changed
    1909,  # Account locked out
}


def exception_windows_error_codes(error: BaseException) -> set[int]:
    """Collect Windows error codes from one exception and its causes."""
    codes: set[int] = set()
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        for attribute in ("winerror", "errno"):
            value = getattr(current, attribute, None)
            if isinstance(value, int):
                codes.add(value)
        if current.args and isinstance(current.args[0], int):
            codes.add(current.args[0])
        current = current.__cause__ or current.__context__
    return codes


def is_support_authentication_error(error: BaseException) -> bool:
    """Return whether an SMB failure requires user/account intervention."""
    if exception_windows_error_codes(error).intersection(
        SUPPORT_AUTH_ERROR_CODES
    ):
        return True
    message = str(error).casefold()
    markers = (
        "account is currently locked",
        "account has been locked",
        "учетная запись пользователя заблокирована",
        "user account has expired",
        "logon failure",
        "неверное имя пользователя или пароль",
    )
    return any(marker in message for marker in markers)


def support_account_is_locked(error: BaseException) -> bool:
    """Return whether Windows explicitly reports account lockout."""
    return (
        1909 in exception_windows_error_codes(error)
        or "заблокирована" in str(error).casefold()
        or "locked" in str(error).casefold()
    )


def probe_support_network(settings: dict) -> None:
    """Read metadata from the two shares required before launcher startup."""
    paths = support_paths_for_computer(
        settings.get("main_computer_name", DEFAULT_MAIN_COMPUTER_NAME)
    )
    Path(paths["downloads"]).stat()
    Path(paths["settings"]).stat()


def connect_support_network_session(settings: dict) -> bool:
    """Prompt once for a Windows password when SMB authentication fails."""
    global SUPPORT_NETWORK_AUTH_BLOCKED
    try:
        probe_support_network(settings)
        SUPPORT_NETWORK_AUTH_BLOCKED = False
        return True
    except OSError as error:
        if not is_support_authentication_error(error):
            print(f"XXX Cannot access main AutoChecker computer: {error}")
            return False
        if support_account_is_locked(error):
            SUPPORT_NETWORK_AUTH_BLOCKED = True
            print(
                "XXX The Windows account used for the main AutoChecker "
                "computer is locked. Unlock it on the main computer, then "
                "restart AutoChecker; automatic network retries are paused"
            )
            return False

        computer_name = normalized_main_computer_name(
            settings.get("main_computer_name", DEFAULT_MAIN_COMPUTER_NAME)
        )
        if not sys.stdin or not sys.stdin.isatty():
            SUPPORT_NETWORK_AUTH_BLOCKED = True
            print(
                f"XXX Windows credentials are required for {computer_name}; "
                "run AutoChecker in an interactive console"
            )
            return False

        default_user = str(settings.get("support_username", "")).strip()
        prompt = f"Windows user for {computer_name}"
        if default_user:
            prompt += f" [{default_user}]"
        username = input(prompt + " (blank cancels): ").strip() or default_user
        if not username:
            SUPPORT_NETWORK_AUTH_BLOCKED = True
            print("? Network sign-in cancelled; automatic retries are paused")
            return False
        password = getpass.getpass(
            "Windows account password (Windows Hello PIN is not accepted): "
        )
        if not password:
            SUPPORT_NETWORK_AUTH_BLOCKED = True
            print("? Empty password was not submitted; automatic retries are paused")
            return False
        connection_username = username
        if "\\" not in username and "@" not in username:
            connection_username = f"{computer_name}\\{username}"

        try:
            import win32netcon
            import win32wnet

            win32wnet.WNetAddConnection2(
                win32netcon.RESOURCETYPE_DISK,
                None,
                rf"\\{computer_name}\IPC$",
                None,
                connection_username,
                password,
                0,
            )
            probe_support_network(settings)
        except Exception as connection_error:
            SUPPORT_NETWORK_AUTH_BLOCKED = True
            if 1219 in exception_windows_error_codes(connection_error):
                print(
                    f"XXX Windows already has a connection to {computer_name} "
                    "with different credentials. Disconnect that Windows "
                    "network connection and restart AutoChecker"
                )
            else:
                print(
                    f"XXX Cannot sign in to {computer_name}: "
                    f"{connection_error}"
                )
            return False

        if settings.get("support_username") != connection_username:
            settings["support_username"] = connection_username
            save_settings(settings)
        SUPPORT_NETWORK_AUTH_BLOCKED = False
        print(f"* Connected to main AutoChecker computer {computer_name}")
        return True


def read_json_snapshot(path: Path, attempts: int = 5) -> dict:
    """Read a stable JSON snapshot without requesting an SMB write lock."""
    last_error: Exception | None = None
    previous_text = None
    for _attempt in range(attempts):
        try:
            text = path.read_text(encoding="utf-8-sig")
            if previous_text is None:
                previous_text = text
                time.sleep(0.1)
                continue
            if text != previous_text:
                previous_text = text
                time.sleep(0.1)
                continue
            document = json.loads(text)
            if not isinstance(document, dict):
                raise ValueError(f"{path.name} root is not an object")
            return document
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            last_error = error
            previous_text = None
            time.sleep(0.1)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Cannot read {path}")


def synchronize_support_catalog_settings(settings: dict) -> dict:
    """Replace only product-related local settings from the main computer."""
    if not support_mode_enabled(settings):
        return settings
    remote_path = Path(
        support_paths_for_computer(settings["main_computer_name"])["settings"]
    )
    try:
        # AutoChecker_Settings is intentionally read-only for support PCs.
        # Windows/SMB can reject portalocker's byte-range lock on a read-only
        # UNC handle with Errno 22, so use a stable double-read snapshot.
        remote_settings = read_json_snapshot(remote_path)
    except Exception as error:
        print(f"XXX Cannot update product settings from {remote_path}: {error}")
        return settings

    changed_keys = []
    missing_keys = []
    with portalocker.Lock(
        str(SETTINGS_FILE), mode="r+", encoding="utf-8", timeout=30
    ) as local_file:
        local_file.seek(0)
        local_settings = json.load(local_file)
        for key in SUPPORT_CATALOG_KEYS:
            if key not in remote_settings:
                missing_keys.append(key)
                continue
            remote_value = copy.deepcopy(remote_settings[key])
            if local_settings.get(key) != remote_value:
                local_settings[key] = remote_value
                changed_keys.append(key)
        if changed_keys:
            local_file.seek(0)
            local_file.truncate()
            json.dump(local_settings, local_file, ensure_ascii=False, indent=4)
            local_file.write("\n")
            local_file.flush()

    if changed_keys:
        print("* Product settings updated: " + ", ".join(changed_keys))
    else:
        print("* Product settings are current")
    if missing_keys:
        print(
            "? Main Settings.json has no product settings: "
            + ", ".join(missing_keys)
        )
    return ensure_settings_schema()


def main_share_directories() -> dict[str, Path]:
    """Return local folders exposed by the main AutoChecker computer."""
    return {
        "downloads": Path.home() / "Downloads",
        # Share the department roots so all current and future subfolders are
        # immediately available without adding more SMB shares.
        "retail": DESKTOP_DIR / "RETAIL",
        "b2b": DESKTOP_DIR / "B2B",
        # Windows shares directories, not individual files. The project
        # folder is therefore exposed read-only and Settings.json stays at
        # its root.
        "settings": BASE_DIR,
    }


def main_network_share_is_usable(
    share_name: str,
    directory: Path,
    read_only: bool,
) -> bool:
    """Verify the share mapping and its required access through SMB itself."""
    unc_root = Path(rf"\\localhost\{share_name}")
    if read_only:
        try:
            return (
                (unc_root / SETTINGS_FILE.name).read_bytes()
                == SETTINGS_FILE.read_bytes()
            )
        except OSError:
            return False

    probe_name = (
        f".autochecker-share-probe-{os.getpid()}-{time.time_ns()}.tmp"
    )
    local_probe = directory / probe_name
    unc_probe = unc_root / probe_name
    probe_text = f"AutoChecker {time.time_ns()}"
    try:
        unc_probe.write_text(probe_text, encoding="utf-8")
        return local_probe.read_text(encoding="utf-8") == probe_text
    except OSError:
        return False
    finally:
        for probe in (unc_probe, local_probe):
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def missing_main_network_shares() -> list[tuple[str, Path, bool]]:
    """Return shares that do not expose the expected folder and access."""
    missing: list[tuple[str, Path, bool]] = []
    for key, directory in main_share_directories().items():
        directory.mkdir(parents=True, exist_ok=True)
        share_name = SUPPORT_SHARE_NAMES[key]
        read_only = key == "settings"
        if main_network_share_is_usable(share_name, directory, read_only):
            print(f"* Network share ready: {share_name} -> {directory}")
        else:
            missing.append((share_name, directory, read_only))
    return missing


def removable_legacy_network_shares() -> list[tuple[str, Path]]:
    """Return only obsolete shares that still point to our old folders."""
    import win32net

    removable = []
    for share_name, expected_path in LEGACY_SUPPORT_SHARES.items():
        try:
            share = win32net.NetShareGetInfo(None, share_name, 2)
        except pywintypes.error as error:
            if exception_windows_error_codes(error).intersection({2, 2310}):
                continue
            raise
        actual_path = Path(str(share.get("path", "")))
        try:
            matches = actual_path.resolve() == expected_path.resolve()
        except OSError:
            matches = str(actual_path).casefold() == str(expected_path).casefold()
        if matches:
            removable.append((share_name, expected_path))
        else:
            print(
                f"? Legacy share {share_name} points to {actual_path}; "
                "it was not removed"
            )
    return removable


def _powershell_single_quoted(value: object) -> str:
    """Quote one value for a PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def network_share_setup_script(
    shares: list[tuple[str, Path, bool]],
    legacy_shares: list[tuple[str, Path]],
) -> str:
    """Build an idempotent elevated script for SMB and NTFS permissions."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        (
            "$everyone = ([System.Security.Principal.SecurityIdentifier]"
            "'S-1-1-0').Translate("
            "[System.Security.Principal.NTAccount]).Value"
        ),
    ]
    for share_name, directory, read_only in shares:
        share_literal = _powershell_single_quoted(share_name)
        path_literal = _powershell_single_quoted(directory)
        access_parameter = "ReadAccess" if read_only else "ChangeAccess"
        access_right = "Read" if read_only else "Change"
        ntfs_right = "(OI)(CI)RX" if read_only else "(OI)(CI)M"
        lines.extend((
            f"$shareName = {share_literal}",
            f"$sharePath = {path_literal}",
            (
                "$existing = Get-SmbShare -Name $shareName "
                "-ErrorAction SilentlyContinue"
            ),
            (
                "if ($null -eq $existing) { New-SmbShare -Name $shareName "
                f"-Path $sharePath -{access_parameter} $everyone "
                "-CachingMode None | Out-Null }"
            ),
            (
                "elseif ([IO.Path]::GetFullPath($existing.Path).TrimEnd('\\') "
                "-ne [IO.Path]::GetFullPath($sharePath).TrimEnd('\\')) "
                "{ throw \"Share $shareName points to $($existing.Path), "
                "expected $sharePath\" }"
            ),
            (
                "Grant-SmbShareAccess -Name $shareName -AccountName "
                f"$everyone -AccessRight {access_right} -Force | Out-Null"
            ),
            (
                "& icacls.exe $sharePath /grant "
                f"'*S-1-1-0:{ntfs_right}' /C | Out-Null"
            ),
            "if ($LASTEXITCODE -ne 0) { throw \"icacls failed: $sharePath\" }",
        ))
    for share_name, expected_path in legacy_shares:
        share_literal = _powershell_single_quoted(share_name)
        path_literal = _powershell_single_quoted(expected_path)
        lines.extend((
            f"$legacyName = {share_literal}",
            f"$legacyPath = {path_literal}",
            (
                "$legacy = Get-SmbShare -Name $legacyName "
                "-ErrorAction SilentlyContinue"
            ),
            (
                "if ($null -ne $legacy -and "
                "[IO.Path]::GetFullPath($legacy.Path).TrimEnd('\\') -eq "
                "[IO.Path]::GetFullPath($legacyPath).TrimEnd('\\')) { "
                "Remove-SmbShare -Name $legacyName -Force }"
            ),
        ))
    return "\n".join(lines)


def run_elevated_powershell(script: str) -> None:
    """Run an elevated PowerShell script and preserve its real error text."""
    result_handle, result_name = tempfile.mkstemp(
        prefix="AutoChecker-elevated-",
        suffix=".json",
    )
    os.close(result_handle)
    result_file = Path(result_name)
    result_file.unlink(missing_ok=True)
    result_path = _powershell_single_quoted(str(result_file))
    wrapped_script = "\n".join((
        "$ErrorActionPreference = 'Stop'",
        "try {",
        "    & {",
        "        " + script.replace("\n", "\n        "),
        "    }",
        "    $result = @{ ok = $true; error = ''; details = '' }",
        "} catch {",
        "    $result = @{",
        "        ok = $false",
        "        error = $_.Exception.Message",
        "        details = ($_ | Out-String)",
        "    }",
        "}",
        (
            "$result | ConvertTo-Json -Compress | Set-Content "
            f"-LiteralPath {result_path} -Encoding UTF8"
        ),
        "if (-not $result.ok) { exit 1 }",
    ))
    encoded = base64.b64encode(
        wrapped_script.encode("utf-16-le")
    ).decode("ascii")
    arguments = (
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {encoded}"
    )
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                check=False,
            )
            exit_code = completed.returncode
        else:
            process_info = shell.ShellExecuteEx(
                fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
                lpVerb="runas",
                lpFile="powershell.exe",
                lpParameters=arguments,
                nShow=win32con.SW_HIDE,
            )
            process_handle = process_info["hProcess"]
            try:
                win32event.WaitForSingleObject(
                    process_handle,
                    win32event.INFINITE,
                )
                exit_code = win32process.GetExitCodeProcess(process_handle)
            finally:
                win32api.CloseHandle(process_handle)

        if exit_code:
            detail = ""
            if result_file.is_file():
                try:
                    result = json.loads(
                        result_file.read_text(encoding="utf-8-sig")
                    )
                    detail = str(
                        result.get("details") or result.get("error") or ""
                    ).strip()
                except (OSError, ValueError, TypeError):
                    detail = ""
            raise RuntimeError(
                detail
                or f"elevated PowerShell finished with code {exit_code}"
            )
    finally:
        result_file.unlink(missing_ok=True)


NETWORK_DISCOVERY_FIREWALL_GROUP = "@FirewallAPI.dll,-32752"
FILE_SHARING_FIREWALL_GROUP = "@FirewallAPI.dll,-28502"
NETWORK_DISCOVERY_CHECK_TIMEOUT = 45


def windows_network_discovery_state() -> dict:
    """Return language-independent state for active Windows networks."""
    discovery_group = _powershell_single_quoted(
        NETWORK_DISCOVERY_FIREWALL_GROUP
    )
    sharing_group = _powershell_single_quoted(FILE_SHARING_FIREWALL_GROUP)
    script = "\n".join((
        "$ErrorActionPreference = 'Stop'",
        (
            "$profiles = @(Get-NetConnectionProfile | Where-Object { "
            "$_.IPv4Connectivity -ne 'Disconnected' -or "
            "$_.IPv6Connectivity -ne 'Disconnected' })"
        ),
        "function Test-PrivateFirewallGroup([string]$Group) {",
        (
            "    $rules = @(Get-NetFirewallRule -Group $Group "
            "-PolicyStore ActiveStore -ErrorAction SilentlyContinue)"
        ),
        "    if ($rules.Count -eq 0) { return $true }",
        "    $privateRules = @($rules | Where-Object {",
        "        $profileValue = [int]$_.Profile",
        (
            "        $profileValue -eq 0 -or "
            "(($profileValue -band 2) -ne 0)"
        ),
        "    })",
        "    if ($privateRules.Count -eq 0) { return $true }",
        (
            "    return @($privateRules | Where-Object { "
            "[string]$_.Enabled -ne 'True' }).Count -eq 0"
        ),
        "}",
        "$state = [PSCustomObject]@{",
        "    connected = $profiles.Count",
        (
            "    public = @($profiles | Where-Object { "
            "$_.NetworkCategory -eq 'Public' }).Count"
        ),
        (
            "    private = @($profiles | Where-Object { "
            "$_.NetworkCategory -eq 'Private' }).Count"
        ),
        (
            "    discovery = Test-PrivateFirewallGroup "
            f"{discovery_group}"
        ),
        (
            "    sharing = Test-PrivateFirewallGroup "
            f"{sharing_group}"
        ),
        "}",
        "$state | ConvertTo-Json -Compress",
    ))
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NETWORK_DISCOVERY_CHECK_TIMEOUT,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(
            "Windows network discovery check timed out after "
            f"{NETWORK_DISCOVERY_CHECK_TIMEOUT} seconds"
        ) from None
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            detail or f"PowerShell finished with code {result.returncode}"
        )
    state = json.loads(result.stdout.strip().lstrip("\ufeff"))
    if not isinstance(state, dict):
        raise ValueError("invalid network discovery state")
    return state


def network_discovery_setup_script() -> str:
    """Build the elevated, idempotent private-network setup script."""
    discovery_group = _powershell_single_quoted(
        NETWORK_DISCOVERY_FIREWALL_GROUP
    )
    sharing_group = _powershell_single_quoted(FILE_SHARING_FIREWALL_GROUP)
    return "\n".join((
        "$ErrorActionPreference = 'Stop'",
        (
            "$profiles = @(Get-NetConnectionProfile | Where-Object { "
            "$_.IPv4Connectivity -ne 'Disconnected' -or "
            "$_.IPv6Connectivity -ne 'Disconnected' })"
        ),
        "$profiles | Where-Object { $_.NetworkCategory -eq 'Public' } |",
        (
            "    ForEach-Object { Set-NetConnectionProfile "
            "-InterfaceIndex $_.InterfaceIndex -NetworkCategory Private }"
        ),
        "function Enable-PrivateFirewallGroup([string]$Group) {",
        (
            "    $rules = @(Get-NetFirewallRule -Group $Group "
            "-PolicyStore PersistentStore -ErrorAction SilentlyContinue)"
        ),
        "    $privateRules = @($rules | Where-Object {",
        "        $profileValue = [int]$_.Profile",
        (
            "        $profileValue -eq 0 -or "
            "(($profileValue -band 2) -ne 0)"
        ),
        "    })",
        "    foreach ($rule in $privateRules) {",
        (
            "        Set-NetFirewallRule -PolicyStore PersistentStore "
            "-Name $rule.Name -Enabled True"
        ),
        "    }",
        "}",
        f"Enable-PrivateFirewallGroup {discovery_group}",
        f"Enable-PrivateFirewallGroup {sharing_group}",
        "foreach ($serviceName in @('fdPHost', 'FDResPub', 'LanmanServer')) {",
        (
            "    $service = Get-Service -Name $serviceName "
            "-ErrorAction SilentlyContinue"
        ),
        "    if ($null -ne $service) {",
        "        Set-Service -Name $serviceName -StartupType Automatic",
        "        Start-Service -Name $serviceName -ErrorAction SilentlyContinue",
        "    }",
        "}",
    ))


def ensure_windows_network_discovery_enabled() -> None:
    """Make active Windows networks private and enable SMB discovery rules."""
    if sys.platform != "win32":
        return
    try:
        state = windows_network_discovery_state()
    except TimeoutError as error:
        print(f"? {error}; launcher will continue")
        return
    except Exception as error:
        print(f"XXX Cannot check Windows network discovery: {error}")
        return

    if int(state.get("connected", 0)) == 0:
        print("? No active network; network discovery check skipped")
        return
    if (
        int(state.get("public", 0)) == 0
        and bool(state.get("discovery"))
        and bool(state.get("sharing"))
    ):
        print("* Windows network discovery is enabled")
        return

    print("? Windows network discovery is disabled; enabling it")
    try:
        run_elevated_powershell(network_discovery_setup_script())
        verified = windows_network_discovery_state()
    except Exception as error:
        print(f"XXX Cannot enable Windows network discovery: {error}")
        return
    if (
        int(verified.get("public", 0)) == 0
        and bool(verified.get("discovery"))
        and bool(verified.get("sharing"))
    ):
        print("* Windows network discovery enabled")
    else:
        print(
            "XXX Windows network discovery is still unavailable; "
            "check Windows network or group policy settings"
        )


WINDOWS_PROTECTED_PRINT_REGISTRY = (
    r"SOFTWARE\Policies\Microsoft\Windows NT\Printers\WPP"
)


def windows_protected_print_enabled() -> bool:
    """Read the Windows 11 WPP state without requesting administrator rights."""
    if sys.platform != "win32":
        return False
    try:
        if sys.getwindowsversion().build < 26100:
            return False
    except AttributeError:
        return False

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            WINDOWS_PROTECTED_PRINT_REGISTRY,
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            values = {}
            for value_name in (
                "EnabledBy",
                "WindowsProtectedPrintGroupPolicyState",
                "WindowsProtectedPrintMode",
            ):
                try:
                    values[value_name] = int(winreg.QueryValueEx(key, value_name)[0])
                except FileNotFoundError:
                    values[value_name] = 0
    except FileNotFoundError:
        return False
    return (
        values.get("WindowsProtectedPrintMode") == 1
        or values.get("WindowsProtectedPrintGroupPolicyState") == 1
        or values.get("EnabledBy") == 2
    )


def ensure_windows_protected_print_disabled() -> None:
    """Disable WPP only when Windows reports that it is currently enabled."""
    try:
        enabled = windows_protected_print_enabled()
    except OSError as error:
        print(f"XXX Cannot check Windows protected print mode: {error}")
        return
    if not enabled:
        print("* Windows protected print mode is disabled")
        return

    print("? Windows protected print mode is enabled; disabling it")
    registry_path = (
        r"HKLM:\SOFTWARE\Policies\Microsoft\Windows NT\Printers\WPP"
    )
    script = "\n".join((
        "$ErrorActionPreference = 'Stop'",
        f"$path = '{registry_path}'",
        "New-Item -Path $path -Force | Out-Null",
        (
            "New-ItemProperty -Path $path -Name "
            "'WindowsProtectedPrintMode' -PropertyType DWord "
            "-Value 0 -Force | Out-Null"
        ),
        (
            "New-ItemProperty -Path $path -Name "
            "'WindowsProtectedPrintGroupPolicyState' -PropertyType DWord "
            "-Value 0 -Force | Out-Null"
        ),
        (
            "Remove-ItemProperty -Path $path -Name 'EnabledBy' "
            "-ErrorAction SilentlyContinue"
        ),
        (
            "Remove-ItemProperty -Path $path -Name "
            "'WindowsProtectedPrintOobeConfigComplete' "
            "-ErrorAction SilentlyContinue"
        ),
    ))
    try:
        run_elevated_powershell(script)
    except Exception as error:
        print(f"XXX Cannot disable Windows protected print mode: {error}")
        return
    if windows_protected_print_enabled():
        print(
            "XXX Windows protected print mode is enforced by system policy; "
            "contact the administrator"
        )
        return
    print(
        "* Windows protected print mode disabled; restart Windows if printer "
        "drivers are not available yet"
    )


def ensure_main_network_shares(settings: dict) -> None:
    """Create the main computer's fixed SMB shares when they are absent."""
    if support_mode_enabled(settings):
        return
    try:
        missing = missing_main_network_shares()
        legacy_shares = removable_legacy_network_shares()
    except Exception as error:
        print(f"XXX Cannot check network shares: {error}")
        return
    if not missing and not legacy_shares:
        return
    if missing:
        names = ", ".join(name for name, _path, _read_only in missing)
        print(f"* Configuring network shares: {names}")
    if legacy_shares:
        print(
            "* Removing obsolete network shares: "
            + ", ".join(name for name, _path in legacy_shares)
        )
    try:
        run_elevated_powershell(
            network_share_setup_script(missing, legacy_shares)
        )
    except Exception as error:
        print(f"XXX Cannot configure network shares: {error}")
        return
    # Verify that Windows registered every requested share.
    try:
        remaining = missing_main_network_shares()
    except Exception as error:
        print(f"XXX Cannot verify network shares: {error}")
        return
    if remaining:
        print(
            "XXX Network shares are still unavailable: "
            + ", ".join(name for name, _path, _read_only in remaining)
        )
    remaining_legacy = removable_legacy_network_shares()
    if remaining_legacy:
        print(
            "XXX Obsolete network shares are still available: "
            + ", ".join(name for name, _path in remaining_legacy)
        )


def update_RETAIL_change_caches(
    worker: dict,
    replacements: dict[str, list[tuple[str, int]]],
    replacement_metadata: dict[str, dict],
    config: dict,
) -> set[str]:
    """Update every normal cache containing one of the changed orders."""
    cache_sources: OrderedDict[str, tuple[Path, Path | None]] = OrderedDict()
    for _pdf_date, _part, pdf_path in _available_order_pdfs(
        worker.get("DOWNLOAD_DIRECTORIES")
    ):
        cache_path = RETAIL_order_cache_path(pdf_path)
        cache_sources[str(cache_path).casefold()] = (cache_path, pdf_path)

    for directory in worker.get("DOWNLOAD_DIRECTORIES") or []:
        if not Path(directory).is_dir():
            continue
        try:
            paths = list(Path(directory).iterdir())
        except OSError:
            continue
        for cache_path in paths:
            if (
                cache_path.is_file()
                and not cache_path.name.casefold().startswith("change ")
                and cache_path.name.casefold().endswith(
                    RETAIL_ORDER_CACHE_SUFFIX.casefold()
                )
            ):
                cache_sources.setdefault(
                    str(cache_path).casefold(), (cache_path, None)
                )

    matched: set[str] = set()
    for cache_path, source_pdf in cache_sources.values():
        matched.update(
            merge_RETAIL_change_cache(
                cache_path,
                source_pdf,
                replacements,
                replacement_metadata,
                config,
            )
        )
    return matched


def append_RETAIL_change_markers(
    worker: dict,
    order_ids: list[str],
    marker_kind: str = "Order",
) -> tuple[set[str], set[str]]:
    """Append one typed Change boundary to every found statistics line."""
    if marker_kind not in {"Order", "Label"}:
        raise ValueError(f"Invalid Change marker kind: {marker_kind}")
    target_ids = set(order_ids)
    stat_file = retail_statistics_file(worker)
    found: set[str] = set()
    added: set[str] = set()
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        for index, line in enumerate(lines):
            raw_text = line.rstrip("\r\n")
            order_id = retail_order_id_from_stat_line(raw_text)
            if order_id not in target_ids:
                continue
            found.add(order_id)
            marker_exists = (
                retail_stat_has_order_change(raw_text)
                if marker_kind == "Order"
                else retail_stat_has_label_change(raw_text)
            )
            if marker_exists:
                continue
            lines[index] = raw_text + f" Change {marker_kind}\n"
            added.add(order_id)
        if added:
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, stat_file, lines)
    return found, added


def process_RETAIL_change_pdf(
    worker: dict,
    config: dict,
    change_pdf: Path,
) -> tuple[
    OrderedDict[str, list[tuple[str, int]]],
    dict[str, dict],
    set[str],
]:
    """Apply one Change PDF to normal caches and current statistics."""
    expected_ids = RETAIL_change_pdf_order_ids(change_pdf)
    if not expected_ids:
        raise ValueError(f"Invalid Change PDF filename: {change_pdf.name}")

    lock_path = change_pdf.parent / ".AutoChecker_change_orders.lock"
    with portalocker.Lock(
        str(lock_path), mode="a+", encoding="utf-8", timeout=120
    ):
        parsed, metadata_by_order = parse_RETAIL_pdf_document(
            change_pdf, config
        )
        missing_in_pdf = [
            order_id for order_id in expected_ids if order_id not in parsed
        ]
        if missing_in_pdf:
            raise ValueError(
                "Change PDF does not contain: " + ", ".join(missing_in_pdf)
            )

        replacements = OrderedDict(
            (order_id, parsed[order_id]) for order_id in expected_ids
        )
        replacement_metadata = {
            order_id: metadata_by_order[order_id]
            for order_id in expected_ids
        }
        cached_ids = update_RETAIL_change_caches(
            worker,
            replacements,
            replacement_metadata,
            config,
        )
        missing_caches = set(expected_ids) - cached_ids
        if missing_caches:
            raise LookupError(
                "Normal Order JSON not found for: "
                + ", ".join(sorted(missing_caches))
            )

        _found_ids, added_ids = append_RETAIL_change_markers(
            worker, expected_ids, "Order"
        )
        missing_statistics = set(expected_ids) - found_ids
        if missing_statistics:
            raise LookupError(
                "Orders not found in today's statistics: "
                + ", ".join(sorted(missing_statistics))
            )
    return replacements, replacement_metadata, added_ids


def process_pending_RETAIL_change_pdfs(
    worker: dict,
    config: dict,
) -> tuple[
    OrderedDict[str, list[tuple[str, int]]],
    dict[str, dict],
    set[str],
]:
    """Process stable new Change PDFs once per signature in this worker."""
    observed = worker.setdefault("RETAIL_CHANGE_OBSERVED", {})
    processed = worker.setdefault("RETAIL_CHANGE_PROCESSED", {})
    failures = worker.setdefault("RETAIL_CHANGE_FAILURES", {})
    merged_orders: OrderedDict[str, list[tuple[str, int]]] = OrderedDict()
    merged_metadata: dict[str, dict] = {}
    newly_changed: set[str] = set()
    now = time.time()

    for change_pdf in available_RETAIL_change_pdfs(
        worker.get("DOWNLOAD_DIRECTORIES")
    ):
        path_key = str(change_pdf).casefold()
        try:
            stat = change_pdf.stat()
        except OSError:
            continue
        signature = (stat.st_mtime_ns, stat.st_size)
        previous_signature = observed.get(path_key)
        observed[path_key] = signature
        if processed.get(path_key) == signature:
            continue
        # Do not open a PDF while another program may still be copying it.
        if (
            previous_signature != signature
            and now - stat.st_mtime < CHANGE_PDF_SCAN_INTERVAL
        ):
            continue
        failed = failures.get(path_key)
        if (
            failed is not None
            and failed[0] == signature
            and time.monotonic() - failed[1] < CHANGE_PDF_RETRY_INTERVAL
        ):
            continue
        try:
            changed_orders, changed_metadata, added_ids = (
                process_RETAIL_change_pdf(worker, config, change_pdf)
            )
        except Exception as error:
            failures[path_key] = (signature, time.monotonic())
            message = f"XXX Cannot apply {change_pdf.name}: {error}"
            Printer(message)
            send(worker["DISPLAY"], message)
            continue

        processed[path_key] = signature
        failures.pop(path_key, None)
        merged_orders.update(changed_orders)
        merged_metadata.update(changed_metadata)
        newly_changed.update(added_ids)
        if added_ids:
            message = (
                f"* Orders changed: {', '.join(sorted(added_ids))}"
            )
            Printer(message)
            send(worker["DISPLAY"], message)

    return merged_orders, merged_metadata, newly_changed


def process_RETAIL_change_label_pdf(
    worker: dict,
    change_pdf: Path,
) -> set[str]:
    """Validate a replacement-label PDF and mark its statistics orders."""
    expected_ids = RETAIL_change_label_pdf_order_ids(change_pdf)
    if not expected_ids:
        raise ValueError(f"Invalid Change Label PDF filename: {change_pdf.name}")

    lock_path = change_pdf.parent / ".AutoChecker_change_labels.lock"
    with portalocker.Lock(
        str(lock_path), mode="a+", encoding="utf-8", timeout=120
    ):
        page_count = len(read_pdf_without_leading_junk(change_pdf).pages)
        if page_count < len(expected_ids):
            raise ValueError(
                f"{change_pdf.name} has {page_count} page(s), but its name "
                f"contains {len(expected_ids)} order(s)"
            )
        found_ids, added_ids = append_RETAIL_change_markers(
            worker,
            expected_ids,
            "Label",
        )
        # A replacement label may belong to an order that was completed on a
        # previous day and therefore was not carried into today's statistics.
        # The PDF remains available for historical lookup, but there is no
        # current order line to reopen.  Treat that case as already handled so
        # the launcher does not retry and report it every monitor cycle.
    return added_ids


def process_pending_RETAIL_change_label_pdfs(
    worker: dict,
) -> set[str]:
    """Process stable dated Change Label PDFs once per file signature."""
    observed = worker.setdefault("RETAIL_CHANGE_LABEL_OBSERVED", {})
    processed = worker.setdefault("RETAIL_CHANGE_LABEL_PROCESSED", {})
    failures = worker.setdefault("RETAIL_CHANGE_LABEL_FAILURES", {})
    newly_changed: set[str] = set()
    now = time.time()

    for change_pdf in available_RETAIL_change_label_pdfs(
        worker.get("DOWNLOAD_DIRECTORIES")
    ):
        path_key = str(change_pdf).casefold()
        try:
            stat = change_pdf.stat()
        except OSError:
            continue
        signature = (stat.st_mtime_ns, stat.st_size)
        previous_signature = observed.get(path_key)
        observed[path_key] = signature
        if processed.get(path_key) == signature:
            continue
        if (
            previous_signature != signature
            and now - stat.st_mtime < CHANGE_PDF_SCAN_INTERVAL
        ):
            continue
        failed = failures.get(path_key)
        if (
            failed is not None
            and failed[0] == signature
            and time.monotonic() - failed[1] < CHANGE_PDF_RETRY_INTERVAL
        ):
            continue
        try:
            added_ids = process_RETAIL_change_label_pdf(worker, change_pdf)
        except Exception as error:
            failures[path_key] = (signature, time.monotonic())
            message = f"XXX Cannot apply {change_pdf.name}: {error}"
            Printer(message)
            send(worker["DISPLAY"], message)
            continue

        processed[path_key] = signature
        failures.pop(path_key, None)
        newly_changed.update(added_ids)
        if added_ids:
            message = (
                f"* Order labels changed: {', '.join(sorted(added_ids))}"
            )
            Printer(message)
            send(worker["DISPLAY"], message)

    return newly_changed


def parse_orders_RETAIL(worker: dict, config: dict, previous: bool = False) -> dict:
    pdf_path, _ = find_latest_pdf(
        previous,
        worker.get("DOWNLOAD_DIRECTORIES"),
    )
    if pdf_path is None:
        message = "XXX Orders PDF not found in Downloads"
        Printer(message)
        send(worker["DISPLAY"], message)
        return {}
    message = f"* Update PDF | {pdf_path.name}"
    Printer(message)
    send(worker["DISPLAY"], message)
    try:
        parsed, metadata_by_order = load_or_create_RETAIL_order_cache(
            pdf_path, config
        )
    except Exception as error:
        message = f"XXX Cannot load orders from {pdf_path.name}: {error}"
        Printer(message)
        send(worker["DISPLAY"], message)
        return {}
    worker["RETAIL_ORDER_META"] = metadata_by_order
    parsed = RETAIL_orders_for_current_day(worker, pdf_path, parsed)
    save_part_to_statistics(worker, pdf_path, parsed, config)
    return parsed


def find_B2B_pdf(pdf_reference: str) -> Path | None:
    """Find Downloads/<order without #>.pdf using an exact case-insensitive name."""
    order_name = B2B_order_name(pdf_reference)
    if order_name is None:
        return None
    found, _ = find_downloaded_B2B_pdf(f"{order_name}.pdf")
    return found


def parse_orders_B2B(
    worker: dict,
    config: dict,
    pdf_reference: str,
    pdf_path: Path | None = None,
) -> dict:
    """Parse one archived B2B order PDF selected by Scan:#<name>."""
    pdf_path = pdf_path or worker.get("B2B_PDF_PATH")
    if pdf_path is None:
        message = f"XXX B2B PDF not found: {pdf_reference}"
        Printer(message)
        send(worker["DISPLAY"], message)
        return {}

    import pdfplumber
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages).replace("\r", "\n")
    except Exception as error:
        message = f"XXX Cannot open B2B PDF {pdf_path.name}: {error}"
        Printer(message)
        send(worker["DISPLAY"], message)
        return {}

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    table_header = "Description Price Discount Discount price Quantity Amount"
    lines = [
        line for line in lines
        if not re.fullmatch(r"\d+\s*/\s*\d+", line)
        and line.casefold() != table_header.casefold()
    ]

    # Spain counts only inside the Ship To block ending at Delivery Note.
    ship_to_indexes = [index for index, line in enumerate(lines) if line.casefold() == "ship to"]
    delivery_indexes = [index for index, line in enumerate(lines)
                        if line.casefold().startswith("delivery note")]
    spain_indexes = [index for index, line in enumerate(lines) if line.casefold() == "spain"]
    is_spain = any(
        any(ship_index < spain_index < delivery_index
            for ship_index in ship_to_indexes for delivery_index in delivery_indexes)
        for spain_index in spain_indexes
    )
    worker["DISPLAY"].write(b"<Spain>\n" if is_spain else b"</Spain>\n")
    worker["DISPLAY"].flush()

    start_index = next(
        (index + 1 for index, line in enumerate(lines)
         if line.casefold().startswith("payment date")),
        None,
    )
    end_index = next(
        (index for index, line in enumerate(lines)
         if start_index is not None and index >= start_index
         and line.casefold().startswith("company name")),
        None,
    )
    if start_index is None or end_index is None or start_index >= end_index:
        message = f"XXX B2B order table not found in {pdf_path.name}"
        Printer(message)
        send(worker["DISPLAY"], message)
        return {}

    positions = []
    B2B_match_names: dict[str, str] = {}
    for line in lines[start_index:end_index]:
        match = re.fullmatch(r"(.+?)\s+(\d+(?:\+\d+)?)\s+(\d+)", line)
        gift_match = re.fullmatch(r"(.+?)\s+GIFT\s+(\d+)", line, re.IGNORECASE)
        if match:
            name, package_size, quantity = match.groups()
            display_name = f"{name} - {package_size} fem"
        elif gift_match:
            # B2B invoices omit the package-size column for GIFT rows.  These
            # are individual one-seed promotional packs, so keep them as a
            # Bonus item (last-priority scan) and match the normal 1-fem code.
            name, quantity = gift_match.groups()
            package_size = "1"
            display_name = f"{name} - Bonus {package_size} fem"
        else:
            message = f"XXX Cannot parse B2B position: {line}"
            Printer(message)
            send(worker["DISPLAY"], message)
            return {}
        resolved = resolve_B2B_catalog_product(name, config)
        if resolved is not None:
            catalog_name, catalog = resolved
            if catalog == "PRODUCTS":
                match_name = f"{catalog_name} - {package_size} fem"
            else:
                match_name = catalog_name
            B2B_match_names[display_name] = match_name
        positions.append((display_name, int(quantity)))

    if not positions:
        message = f"XXX No B2B positions found in {pdf_path.name}"
        Printer(message)
        send(worker["DISPLAY"], message)
        return {}

    # Keep the command's leading # as the order identifier even when the
    # physical PDF filename does not contain it.
    order_id = Path(pdf_reference).stem
    parsed = {order_id: positions}
    worker["B2B_MATCH_NAMES"] = B2B_match_names
    return parsed


def find_original_item(order_items: list[tuple[str, int]], scanned_name: str,
                       match_names: dict[str, str] | None = None,
                       counts: dict | None = None,
                       allow_bonus: bool = True) -> tuple[str | None, int | None]:
    """Find an incomplete normal match before an incomplete Bonus match."""
    match_names = match_names or {}
    counts = counts or {}
    matches = []
    for item_name, quantity in order_items:
        if is_bonus_item(item_name) and not allow_bonus:
            continue
        comparison_name = match_names.get(item_name, item_name)
        if scan_match_name(comparison_name) == scan_match_name(scanned_name):
            matches.append((item_name, quantity))
    matches.sort(key=lambda item: is_bonus_item(item[0]))
    for item_name, quantity in matches:
        if counts.get(item_name, 0) < quantity:
            return item_name, quantity
    if matches:
        return matches[0]
    return None, None


def get_order_status(worker: dict, order_id: str) -> str:
    for line in read_stat_lines(statistics_file(worker)):
        text = line.strip()
        stored_order_id = retail_order_id_from_stat_line(text)
        if (
            stored_order_id is None
            or stored_order_id.lstrip("#") != order_id.lstrip("#")
        ):
            continue
        status_suffix = retail_stat_effective_suffix(text)
        cancelled = retail_stat_is_cancelled(text)
        completed = "+" in status_suffix
        if cancelled and completed:
            return "completed_cancelled"
        if cancelled:
            return "cancelled"
        if completed:
            return "completed"
        return "active"
    return "not_found"


def claim_RETAIL_order(worker: dict, order_id: str) -> str:
    """Atomically claim one free RETAIL order across local/SMB workers."""
    stat_file = statistics_file(worker)
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        for index, line in enumerate(lines):
            raw_text = line.rstrip("\r\n")
            stored_order = retail_order_id_from_stat_line(raw_text)
            if (
                stored_order is None
                or stored_order.lstrip("#") != order_id.lstrip("#")
            ):
                continue

            status_suffix = retail_stat_effective_suffix(raw_text).strip()
            if retail_stat_is_cancelled(raw_text):
                return "cancelled"
            if "+" in status_suffix:
                return "completed"
            if not status_suffix:
                lines[index] = f"{raw_text} {worker['NAME']}\n"
                file.seek(0)
                file.truncate()
                file.writelines(lines)
                file.flush()
                mirror_statistics_lines(worker, stat_file, lines)
                return "claimed"
            if status_suffix == worker["NAME"]:
                return "active"
            return "assigned"
    return "not_found"


def update_order_in_statistics(worker: dict, order_id: str, *, add_name: bool = False,
                               add_plus: bool = False, manual_plus: bool = False) -> bool:
    stat_file = statistics_file(worker)
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        updated = False
        result = []
        for line in lines:
            raw_text = line.rstrip("\r\n")
            text = raw_text.strip()
            department = worker.get("DEPARTMENT", "").upper()
            if department == "RETAIL":
                stored_order_id = retail_order_id_from_stat_line(text)
                matches_order = bool(
                    stored_order_id
                    and stored_order_id.lstrip("#") == order_id.lstrip("#")
                )
            else:
                matches_order = text.startswith(order_id)
            status_suffix = (
                retail_stat_effective_suffix(raw_text)
                if department == "RETAIL" else text
            )
            order_cancelled = (
                retail_stat_is_cancelled(raw_text)
                if department == "RETAIL" else "Cancelled" in status_suffix
            )
            if matches_order and not order_cancelled:
                if add_name and worker["NAME"] not in status_suffix:
                    raw_text += f" {worker['NAME']}"
                    status_suffix += f" {worker['NAME']}"
                if manual_plus and "(+)" not in status_suffix:
                    raw_text += " (+)"
                elif (
                    add_plus
                    and " +" not in status_suffix
                    and "(+)" not in status_suffix
                ):
                    raw_text += " +"
                line = raw_text + "\n"
                updated = True
            result.append(line)
        if updated:
            file.seek(0)
            file.truncate()
            file.writelines(result)
            file.flush()
            mirror_statistics_lines(worker, stat_file, result)
        return updated


def RETAIL_delivery_from_stat_line(line: str) -> str:
    """Classify by the explicit first field, with legacy-track fallback."""
    explicit_delivery, tracking_number = RETAIL_stat_prefix_values(line)
    if explicit_delivery:
        return explicit_delivery
    return RETAIL_delivery_from_tracking_number(tracking_number)


def reserve_next_RETAIL_order(worker: dict, orders: dict) -> str | None:
    """Assign one free order without crossing the active delivery priority."""
    available_order_ids = set(orders)
    stat_file = statistics_file(worker)
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        reserved_order = None
        unfinished_by_rank: dict[int, list[tuple[int, str, str]]] = defaultdict(list)
        delivery_rank = {
            delivery: rank
            for rank, delivery in enumerate(RETAIL_DELIVERY_ORDER)
        }
        for index, line in enumerate(lines):
            raw_text = line.rstrip("\r\n")
            order_id = retail_order_id_from_stat_line(raw_text)
            if order_id is None or retail_stat_is_cancelled(raw_text):
                continue
            effective_status = retail_stat_effective_suffix(raw_text)
            if "+" in effective_status:
                continue
            delivery = RETAIL_delivery_from_stat_line(raw_text)
            unfinished_by_rank[delivery_rank[delivery]].append(
                (index, order_id, raw_text)
            )

        if not unfinished_by_rank:
            worker.pop("RETAIL_RESERVATION_WAITING_DELIVERY", None)
            return None

        active_rank = min(unfinished_by_rank)
        active_delivery = RETAIL_DELIVERY_ORDER[active_rank]
        active_orders = unfinished_by_rank[active_rank]
        # Change orders remain first inside their delivery class, but can no
        # longer make Postal or Packeta jump ahead of unfinished UPS orders.
        for changed_order_only in (True, False):
            for index, order_id, raw_text in active_orders:
                if order_id not in available_order_ids:
                    continue
                if retail_stat_has_order_change(raw_text) != changed_order_only:
                    continue
                if retail_stat_effective_suffix(raw_text).strip():
                    continue
                lines[index] = f"{raw_text} {worker['NAME']}\n"
                reserved_order = order_id
                break
            if reserved_order is not None:
                break
        if reserved_order is not None:
            worker.pop("RETAIL_RESERVATION_WAITING_DELIVERY", None)
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, stat_file, lines)
        else:
            # At least one order of the highest-priority class is still being
            # assembled (or its new part has not reached this worker yet).
            # Never advance to the next delivery class in that state.
            worker["RETAIL_RESERVATION_WAITING_DELIVERY"] = active_delivery
        return reserved_order


def release_RETAIL_order_reservation(worker: dict, order_id: str) -> bool:
    """Remove this worker's untouched reservation after a printing failure."""
    stat_file = statistics_file(worker)
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        changed = False
        for index, line in enumerate(lines):
            raw_text = line.rstrip("\r\n")
            stored_order = retail_order_id_from_stat_line(raw_text)
            if stored_order != order_id:
                continue
            if retail_stat_is_cancelled(raw_text):
                break
            if retail_stat_effective_suffix(raw_text).strip() != worker["NAME"]:
                break
            order_match = re.search(
                rf"(?<!\S){re.escape(order_id)}(?=\s|$)",
                raw_text,
            )
            if order_match is not None:
                # Preserve the latest history boundary. This can be either an
                # order-content change or a label-only change that happened
                # before the temporary reservation.
                change_match = retail_stat_change_match(raw_text)
                keep_until = (
                    order_match.end() + change_match.end()
                    if change_match is not None
                    else order_match.end()
                )
                lines[index] = raw_text[:keep_until] + "\n"
                changed = True
            break
        if changed:
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, stat_file, lines)
        return changed


def cancel_order(worker: dict, order_id: str) -> bool:
    """Mark a RETAIL daily order or B2B index entry as Cancelled."""
    department = worker.get("DEPARTMENT", "").upper()
    is_retail = department in {"RETAIL", RETAIL_UP_DEPARTMENT}
    if department == "B2B":
        target = B2B_index_file(worker)
    elif is_retail:
        target = retail_statistics_file(worker)
    else:
        target = worker["STATISTICS"] / f"{datetime.date.today():%d.%m.%Y}.txt"

    with _locked_statistics_file(target) as file:
        file.seek(0)
        lines = file.readlines()
        changed = False
        for index, line in enumerate(lines):
            raw_text = line.rstrip("\r\n")
            text = raw_text.strip()
            if department == "B2B":
                identifier = text.split("|", 1)[0].strip() if text else ""
            else:
                identifier = retail_order_id_from_stat_line(text) or ""
            if identifier.lstrip("#") != order_id.strip().lstrip("#"):
                continue
            status_text = (
                text
                if department == "B2B"
                else retail_stat_order_suffix(raw_text)
            )
            already_cancelled = (
                "cancelled" in status_text.casefold()
                if department == "B2B"
                else retail_stat_is_cancelled(raw_text)
            )
            if not already_cancelled:
                lines[index] = (
                    text + " | Cancelled\n"
                    if department == "B2B"
                    else raw_text + " Cancelled\n"
                )
            changed = True
            break

        if changed:
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, target, lines)
        return changed


def cancel_order_from_launcher(display_mac: str, order_id: str, settings: dict) -> bool:
    """Cancel an order while its display is idle, preferring its last department."""
    last_values = settings.get("DISPLAY", {}).get(display_mac, ["", "", ""])
    preferred_department = (
        str(last_values[1]).upper()
        if isinstance(last_values, (list, tuple)) and len(last_values) > 1
        else ""
    )
    if preferred_department == RETAIL_UP_DEPARTMENT:
        preferred_department = "RETAIL"
    candidates = [
        preferred_department,
        *(str(value).upper() for value in settings.get("DEPARTMENT", [])),
        "B2B",
        "RETAIL",
    ]

    checked = set()
    for department in candidates:
        if department not in {"B2B", "RETAIL"} or department in checked:
            continue
        checked.add(department)

        statistics_dir = DESKTOP_DIR / department / "Statistik"
        mirror_dir = None
        if support_mode_enabled(settings):
            shared_paths = configured_support_paths(settings)
            if department == "RETAIL":
                statistics_dir = shared_paths["statistics"]
                mirror_dir = DESKTOP_DIR / "RETAIL" / "Statistik"
        target = (
            statistics_dir / B2B_INDEX_FILENAME
            if department == "B2B"
            else statistics_dir / f"{datetime.date.today():%d.%m.%Y}.txt"
        )
        if not target.is_file():
            continue

        worker = {
            "DEPARTMENT": department,
            "STATISTICS": statistics_dir,
        }
        if mirror_dir is not None:
            worker["STATISTICS_MIRROR_DIR"] = mirror_dir
            worker["RETAIL_STATISTICS"] = statistics_dir
        if cancel_order(worker, order_id):
            return True
    return False


def show_status(order_id: str, items: list[tuple[str, int]], counts: defaultdict, extras: defaultdict,
                errors: set, no_barcode_items: list[tuple[str, int]], send_fn,
                show_completed_quantity: bool = False,
                highlighted_item: str | None = None) -> None:
    message = [f"Order status {order_id.lstrip('#')}"]
    for name, quantity in items:
        if (name, quantity) in no_barcode_items:
            continue
        collected = counts.get(name, 0)
        if collected == quantity:
            prefix = f"{quantity} + " if show_completed_quantity else "+ "
            message.append(f"{prefix}{name}")
        elif collected < quantity:
            line = f"{collected}/{quantity} {name}"
            if name == highlighted_item:
                line = "#ff78eb " + line
            message.append(line)
        else:
            prefix = f"{quantity} + " if show_completed_quantity else "+ "
            message.extend((
                f"{prefix}{name}",
                f"{extras.get(name, 0)} !!! EXTRA",
            ))
    message.extend(
        f"{quantity} ? {name}"
        for name, quantity in no_barcode_items
    )
    message.extend(f"XXX {error}" for error in errors)
    text = "\n".join(message)
    Printer(text)
    send_fn(text)


def order_ready(items: list[tuple[str, int]], counts: defaultdict, extras: defaultdict,
                errors: set, no_barcode_items: list[tuple[str, int]]) -> bool:
    if errors or extras or no_barcode_items:
        return False
    return all(counts.get(name, 0) == quantity for name, quantity in items)


def save_photo(
    worker: dict,
    order_id: str | None,
    frame,
    *,
    manually: bool = False,
) -> tuple[bool, bool]:
    """Save a photo and return (saved, first photo for this order)."""
    if not worker.get("HAS_CAMERA", True):
        message = "XXX Process without camera"
        Printer(message)
        send(worker["DISPLAY"], message)
        return False, False
    if frame is None:
        message = "XXX Camera frame unavailable"
        Printer(message)
        send(worker["DISPLAY"], message)
        return False, False
    if not order_id:
        message = "XXX Order not selected"
        Printer(message)
        send(worker["DISPLAY"], message)
        return False, False
    photo_directories = [Path(worker["FOTO"])]
    support_photo_dir = worker.get("SUPPORT_FOTO")
    if support_photo_dir is not None:
        support_photo_dir = Path(support_photo_dir)
        if str(support_photo_dir).casefold() != str(photo_directories[0]).casefold():
            photo_directories.append(support_photo_dir)

    number = 1
    while True:
        suffix = "" if number == 1 else f"_{number}"
        filename = f"{order_id}{suffix}.jpg"
        if not any((directory / filename).exists() for directory in photo_directories):
            break
        number += 1
    path = photo_directories[0] / filename
    if not cv2.imwrite(str(path), frame):
        message = f"XXX Cannot save photo {path.name}"
        saved = False
    else:
        try:
            for directory in photo_directories[1:]:
                directory.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, directory / filename)
        except OSError as error:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            message = f"XXX Cannot save support photo {filename}: {error}"
            saved = False
        else:
            display_filename = path.name.lstrip("#")
            message = (
                f"* {display_filename} Photo taken manually"
                if manually
                else f"* {display_filename} Photo saved"
            )
            update_order_in_statistics(
                worker,
                order_id,
                add_plus=not manually,
                manual_plus=manually,
            )
            saved = True
    Printer(message)
    send(worker["DISPLAY"], message)
    return saved, saved and number == 1


def process_scan(worker: dict, config: dict, code: str, orders: dict, current_order: str | None,
                 counts: defaultdict, extras: defaultdict, errors: set,
                 no_barcode_items: list[tuple[str, int]], photo_pending: bool,
                 photo_start: float, automatic_assignment: bool = False,
                 record_performance: bool = True,
                 ) -> tuple[str | None, bool, float]:
    """Apply one scanner barcode/order code using the previous AutoChecker rules."""
    send_fn = lambda message: send(worker["DISPLAY"], message)
    is_B2B = worker.get("DEPARTMENT", "").upper() == "B2B"
    show_completed_quantity = not is_B2B
    B2B_match_names = worker.get("B2B_MATCH_NAMES", {})
    performance_stats: WorkerPerformanceStats | None = worker.get(
        "PERFORMANCE_STATS"
    )
    if not orders:
        if performance_stats is not None:
            performance_stats.record_scan()
            performance_stats.record_mistake()
        message = "XXX Orders not loaded"
        Printer(message)
        send_fn(message)
        return current_order, photo_pending, photo_start

    if code.startswith("#"):
        matched = next((order for order in orders if order.lstrip("#") == code.lstrip("#")), None)
        if not matched:
            if performance_stats is not None:
                performance_stats.record_scan()
                performance_stats.record_mistake()
            message = f"XXX Unknown order {code}"
            Printer(message)
            send_fn(message)
            return current_order, photo_pending, photo_start
        if worker.get("DEPARTMENT", "").upper() != "B2B":
            status = (
                claim_RETAIL_order(worker, matched)
                if worker.get("SUPPORT")
                else get_order_status(worker, matched)
            )
            available_statuses = (
                {"claimed", "active"}
                if worker.get("SUPPORT")
                else {"active", "not_found"}
            )
            if status not in available_statuses:
                if performance_stats is not None:
                    performance_stats.record_scan()
                    performance_stats.record_mistake()
                message = f"XXX Order unavailable {matched}: {status}"
                Printer(message)
                send_fn(message)
                return current_order, photo_pending, photo_start
        if performance_stats is not None and record_performance:
            performance_stats.record_scan(matched)
        if worker.get("DEPARTMENT", "").upper() == "RETAIL":
            if automatic_assignment:
                worker["RETAIL_AUTOMATIC_ORDER"] = matched
            else:
                # A valid Scan:#order explicitly selected this order, so its
                # first photo must not start the automatic assignment chain.
                worker.pop("RETAIL_AUTOMATIC_ORDER", None)
        current_order = matched
        counts.clear(); extras.clear(); errors.clear(); no_barcode_items.clear()
        for item, quantity in orders[current_order]:
            has_barcode = (
                item in B2B_match_names if is_B2B
                else has_barcode_for_item(item, config)
            )
            if not has_barcode:
                if quantity == 1 and item in config["AUTO_COMPLETE_ITEMS"]:
                    counts[item] = 1
                else:
                    no_barcode_items.append((item, quantity))
        if worker.get("DEPARTMENT", "").upper() == "B2B":
            load_B2B_progress(
                worker, orders[current_order], counts, extras, errors,
                no_barcode_items,
            )
            save_B2B_progress(
                worker, orders[current_order], counts, extras, errors,
                no_barcode_items,
            )
            try:
                refresh_B2B_product_count(config)
            except Exception as error:
                Printer(f"XXX Cannot update B2B Count.json: {error}")
        else:
            update_order_in_statistics(worker, current_order, add_name=True)
        show_status(
            current_order, orders[current_order], counts, extras, errors,
            no_barcode_items, send_fn, show_completed_quantity,
        )
        return current_order, False, 0.0

    if performance_stats is not None:
        performance_stats.record_scan()

    if current_order is None:
        if performance_stats is not None:
            performance_stats.record_mistake()
        message = "XXX Scan order number first"
        Printer(message)
        send_fn(message)
        return current_order, photo_pending, photo_start

    order_items = dict(orders[current_order])
    product_name = resolve_scanned_product(code, config)
    regular_ready = all(
        is_bonus_item(name)
        or (name not in B2B_match_names if is_B2B else not has_barcode_for_item(name, config))
        or counts.get(name, 0) >= quantity
        for name, quantity in orders[current_order]
    )

    if (is_B2B or regular_ready) and code in config.get("OTHER_BONUS", {}):
        product_name = config["OTHER_BONUS"][code]

    item_name, quantity = (
        find_original_item(
            orders[current_order],
            product_name,
            B2B_match_names,
            counts,
            # RETAIL deliberately keeps Bonus positions until all normal
            # positions are complete. B2B has no scan-order restriction.
            allow_bonus=is_B2B or regular_ready,
        )
        if product_name else (None, None)
    )
    changed_item = None
    if item_name is None or item_name not in order_items:
        if performance_stats is not None:
            performance_stats.record_mistake()
        error_text = product_name or f"Unknown barcode {code}"
        errors.add(error_text)
        if is_B2B:
            error_names = worker.setdefault("B2B_ERROR_NAMES", {}).setdefault(
                error_text, []
            )
            if worker["NAME"] not in error_names:
                error_names.append(worker["NAME"])
        photo_pending = False
    else:
        counts[item_name] += 1
        changed_item = item_name
        if counts[item_name] > quantity:
            if performance_stats is not None:
                performance_stats.record_mistake()
            extras[item_name] = counts[item_name] - quantity
            photo_pending = False

    if worker.get("DEPARTMENT", "").upper() == "B2B":
        save_B2B_progress(
            worker, orders[current_order], counts, extras, errors,
            no_barcode_items, changed_item,
        )

    show_status(
        current_order, orders[current_order], counts, extras, errors,
        no_barcode_items, send_fn, show_completed_quantity, changed_item,
    )
    if order_ready(orders[current_order], counts, extras, errors, no_barcode_items):
        message = "+ Order complete"
        Printer(message)
        send_fn(message)
        if not is_B2B:
            photo_pending = True
            photo_start = time.time()
        else:
            photo_pending = False
    return current_order, photo_pending, photo_start


def automatically_assign_next_RETAIL_order(
    worker: dict,
    config: dict,
    orders: dict,
    counts: defaultdict,
    extras: defaultdict,
    errors: set,
    no_barcode_items: list[tuple[str, int]],
) -> tuple[str | None, bool, float]:
    """Reserve and activate the next RETAIL order, printing when possible."""
    performance_stats: WorkerPerformanceStats | None = worker.get(
        "PERFORMANCE_STATS"
    )
    order_id = reserve_next_RETAIL_order(worker, orders)
    if order_id is None:
        if performance_stats is not None:
            performance_stats.stop_tracking()
        waiting_delivery = worker.get("RETAIL_RESERVATION_WAITING_DELIVERY")
        message = (
            f"* Waiting for unfinished {waiting_delivery} orders"
            if waiting_delivery
            else "* All orders compeleted"
        )
        Printer(message)
        send(worker["DISPLAY"], message)
        return None, False, 0.0

    printer_name = str(worker.get("PRINTER", "")).strip()
    if printer_name:
        try:
            job_id = print_RETAIL_order_label(worker, order_id)
        except Exception as error:
            release_RETAIL_order_reservation(worker, order_id)
            if performance_stats is not None:
                performance_stats.stop_tracking()
            message = f"XXX Cannot print order label {order_id}: {error}"
            Printer(message)
            send(worker["DISPLAY"], message)
            return None, False, 0.0
        message = (
            f"* Order label printed {order_id.lstrip('#')} | Job {job_id}"
        )
    else:
        message = f"* Order {order_id.lstrip('#')} assigned without printing"
    Printer(message)
    send(worker["DISPLAY"], message)
    return process_scan(
        worker,
        config,
        order_id,
        orders,
        None,
        counts,
        extras,
        errors,
        no_barcode_items,
        False,
        0.0,
        automatic_assignment=True,
    )


def draw_RETAIL_order_counters(
    frame,
    completed: int,
    total: int,
    cancelled: int,
) -> None:
    """Draw completed, remaining and total order counts on a camera preview."""
    height, width = frame.shape[:2]
    remaining = max(0, total - completed - cancelled)
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.7, min(width, height) / 450.0)
    thickness = max(2, round(font_scale * 2))
    top = max(28, round(34 * font_scale))
    padding = max(10, round(width * 0.025))

    counters = (
        (str(completed), padding, (0, 255, 0)),
        (str(remaining), None, (75, 75, 75)),
        (str(total), width - padding, (0, 165, 255)),
    )
    for index, (text, anchor, colour) in enumerate(counters):
        (text_width, _), _ = cv2.getTextSize(
            text, font, font_scale, thickness
        )
        if index == 0:
            x = anchor
        elif index == 1:
            x = (width - text_width) // 2
        else:
            x = anchor - text_width
        cv2.putText(
            frame,
            text,
            (max(0, x), top),
            font,
            font_scale,
            colour,
            thickness,
            cv2.LINE_AA,
        )


def RETAIL_daily_order_counts(worker: dict) -> tuple[int, int, int]:
    """Count completed, total and cancelled RETAIL orders for today."""
    stat_file = statistics_file(worker)
    try:
        stat = stat_file.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        worker.pop("RETAIL_ORDER_COUNTER_CACHE", None)
        return 0, 0, 0

    cached = worker.get("RETAIL_ORDER_COUNTER_CACHE")
    if cached and cached[0] == signature:
        return cached[1], cached[2], cached[3]

    completed = 0
    total = 0
    cancelled = 0
    for line in read_stat_lines(stat_file):
        if retail_order_id_from_stat_line(line) is None:
            continue
        total += 1
        if retail_stat_is_cancelled(line):
            cancelled += 1
        elif "+" in retail_stat_effective_suffix(line):
            completed += 1

    worker["RETAIL_ORDER_COUNTER_CACHE"] = (
        signature,
        completed,
        total,
        cancelled,
    )
    return completed, total, cancelled
































def main(identity: CheckerIdentity, camera_id: int | None, focus: int | float | None,
         display_port: str, scanner_port: str | None, display_number: str,
         cameras: dict[int, int], printers: list[str], scanners: list[str],
         settings_update_requests: multiprocessing.Queue,
         display_update_queue: multiprocessing.Queue,
         display_reset_event: multiprocessing.Event,
         display_disconnect_event: multiprocessing.Event) -> None:
    """Run one checker and own its COM port until its matching Stop command."""
    cap = None
    display = None
    external_scanner = None
    performance_stats = None
    label_ocr_process = None
    label_ocr_rerun_requested = False

    try:
        config = load_settings()
        photo_dir, statistics_dir = department_paths(identity.department)

        display = serial.Serial(
            display_port,
            baudrate=DISPLAY_BAUDRATE,
            timeout=DISPLAY_TIMEOUT,
            write_timeout=DISPLAY_WRITE_TIMEOUT,
        )

        if scanner_port is not None:
            try:
                external_scanner = serial.Serial(
                    scanner_port,
                    baudrate=SCANNER_BAUDRATE,
                    timeout=DISPLAY_TIMEOUT,
                    write_timeout=DISPLAY_WRITE_TIMEOUT,
                )
                external_scanner.reset_input_buffer()
                print(
                    f"* {identity.scanner_name} opened on {scanner_port} "
                    f"at {SCANNER_BAUDRATE} baud"
                )
            except (serial.SerialException, OSError) as error:
                message = f"XXX Cannot open {identity.scanner_name}: {error}"
                Printer(message)
                send(display, message)
                external_scanner = None

        if camera_id is not None:
            cap = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open camera ID {camera_id}")

            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("M", "J", "P", "G"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 4656)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 3496)
            if focus is not None:
                cap.set(cv2.CAP_PROP_FOCUS, focus)

        performance_stats = WorkerPerformanceStats(
            identity.name,
            identity.department,
        )
        performance_stats.flush(force=True)
        worker = {
            "NAME": identity.name,
            "DEPARTMENT": identity.department,
            "DISPLAY": display,
            "FOTO": photo_dir,
            "STATISTICS": statistics_dir,
            "HAS_CAMERA": cap is not None,
            "PRINTER": identity.printer_name or "",
            "PERFORMANCE_STATS": performance_stats,
        }
        try:
            configure_worker_storage(worker, config)
            synchronize_support_statistics(worker)
        except (OSError, ValueError) as error:
            message = f"XXX Support storage unavailable: {error}"
            Printer(message)
            send(display, message)
            raise RuntimeError(message) from error

        print(f"* Photo folder: {photo_dir}")
        print(f"* Statistics folder: {worker['STATISTICS']}")
        if worker.get("SUPPORT"):
            support_photo_dir = worker.get("SUPPORT_FOTO")
            if support_photo_dir is not None:
                print(f"* Support photo folder: {support_photo_dir}")
            print(
                "* Support downloads folder: "
                f"{worker['DOWNLOAD_DIRECTORIES'][0]}"
            )
        if identity.department.upper() == RETAIL_UP_DEPARTMENT:
            label_date = datetime.date.today()
            downloads_dirs = worker["DOWNLOAD_DIRECTORIES"]
            label_files = current_RETAIL_UP_label_files(
                label_date, downloads_dirs
            )
            pending_label_pdfs = pending_RETAIL_UP_label_pdfs(
                label_date, downloads_dirs
            )
            if pending_label_pdfs:
                label_ocr_ready_event = None
                label_ocr_process = start_label_ocr_process(
                    identity.department,
                    label_ocr_ready_event,
                    downloads_dirs,
                    label_date,
                )
                message = "* Label OCR started"
                Printer(message)
                send(display, message)
            try:
                if not label_files and label_ocr_process is not None:
                    label_files = wait_for_RETAIL_UP_label_data(
                        label_ocr_process,
                        label_ocr_ready_event,
                        label_date,
                        downloads_dirs,
                    )
                if not label_files:
                    raise FileNotFoundError(
                        "Today's Label PDF/JSON was not found in Downloads"
                    )
                message = f"* Label data ready: {len(label_files)} file(s)"
            except Exception as error:
                message = f"XXX {error}"
            Printer(message)
            send(display, message)

        if identity.department.upper() == "B2B":
            B2B_history = Path(worker["STATISTICS"]).parent / "History"
            B2B_history.mkdir(parents=True, exist_ok=True)
            worker["B2B_HISTORY"] = B2B_history
        # RETAIL uses the PDF order list. B2B will load its order PDF when it
        # receives Scan:<pdf filename>; it never parses a retail PDF here.
        department = identity.department.upper()
        if department == "RETAIL":
            initial_orders = (
                parse_orders_RETAIL(worker, config)
                if _available_order_pdfs(worker.get("DOWNLOAD_DIRECTORIES"))
                else OrderedDict()
            )
            orders = load_RETAIL_orders_from_statistics(worker, config)
            if not orders:
                orders = initial_orders
        else:
            orders = {}
        current_order = None
        counts = defaultdict(int)
        extras = defaultdict(int)
        errors = set()
        no_barcode_items: list[tuple[str, int]] = []
        photo_pending = False
        photo_start = 0.0

        if (
            identity.department.upper() == "RETAIL"
            and identity.printer_name
        ):
            current_order, photo_pending, photo_start = (
                automatically_assign_next_RETAIL_order(
                    worker,
                    config,
                    orders,
                    counts,
                    extras,
                    errors,
                    no_barcode_items,
                )
            )

        if cap is not None:
            print(
                f"* Started {identity}; camera ID {camera_id}, focus {focus}, "
                f"resolution {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
            )
        else:
            print(f"* Started {identity} without camera")
        display.write(b"<Ready>\n")
        display.flush()

        window_name = f"AutoChecker - {identity.name} - {identity.department} - {identity.camera_number}"
        while True:
            if (
                label_ocr_process is not None
                and not label_ocr_process_is_alive(label_ocr_process)
            ):
                join_finished_label_ocr_process(label_ocr_process)
                label_ocr_process = None
                if label_ocr_rerun_requested:
                    label_ocr_rerun_requested = False
                    label_ocr_process = start_label_ocr_process(
                        identity.department,
                        downloads_dirs=worker.get("DOWNLOAD_DIRECTORIES"),
                        label_date=datetime.date.today(),
                    )

            display_update_requested = False
            updated_cameras = None
            updated_printers = None
            updated_scanners = None
            camera_stop_reason = None
            retail_state_update = False
            retail_changed_order_ids: set[str] = set()
            retail_up_header_requested = False
            while True:
                try:
                    update_message = display_update_queue.get_nowait()
                    if isinstance(update_message, tuple) and update_message:
                        if (
                            update_message[0] == "uploadData"
                            and len(update_message) >= 2
                            and isinstance(update_message[1], dict)
                        ):
                            display_update_requested = True
                            updated_cameras = update_message[1]
                            if (
                                len(update_message) >= 3
                                and isinstance(update_message[2], list)
                            ):
                                updated_printers = update_message[2]
                            if (
                                len(update_message) >= 4
                                and isinstance(update_message[3], list)
                            ):
                                updated_scanners = update_message[3]
                        elif update_message[0] == "stop" and len(update_message) >= 2:
                            camera_stop_reason = str(update_message[1])
                        elif update_message[0] == "retail_orders_updated":
                            retail_state_update = True
                            if len(update_message) >= 2:
                                retail_changed_order_ids.update(
                                    str(order_id)
                                    for order_id in update_message[1]
                                )
                        elif update_message[0] == "retail_up_header":
                            retail_up_header_requested = True
                except queue.Empty:
                    break

            if camera_stop_reason is not None:
                stop_packet = f"Stop:{camera_stop_reason}\n"
                display.write(stop_packet.encode("utf-8"))
                display.flush()
                print(
                    f"* AutoChecker {display_number} worker stopped: "
                    f"{camera_stop_reason}"
                )
                break

            if display_update_requested:
                if updated_cameras is not None:
                    cameras = updated_cameras
                if updated_printers is not None:
                    printers = updated_printers
                if updated_scanners is not None:
                    scanners = updated_scanners
                config = load_settings()
                try:
                    upload_display_data(
                        display,
                        display_number,
                        config,
                        cameras,
                        printers,
                        scanners,
                    )
                except serial.SerialTimeoutException as error:
                    # A settings refresh must not stop order processing merely
                    # because the display consumed a large packet too slowly.
                    print(
                        f"XXX AutoChecker {display_number} uploadData timeout: {error}"
                    )

            if retail_state_update:
                if department == "RETAIL":
                    refreshed_orders = load_RETAIL_orders_from_statistics(
                        worker, config
                    )
                    if refreshed_orders:
                        orders = refreshed_orders
                    if (
                        current_order is not None
                        and current_order in retail_changed_order_ids
                        and current_order in orders
                    ):
                        if get_order_status(worker, current_order) in {
                            "cancelled",
                            "completed_cancelled",
                        }:
                            message = f"XXX Cancelled order {current_order}"
                            Printer(message)
                            send(worker["DISPLAY"], message)
                            current_order = None
                            worker.pop("RETAIL_AUTOMATIC_ORDER", None)
                            counts.clear()
                            extras.clear()
                            errors.clear()
                            no_barcode_items.clear()
                            photo_pending = False
                            performance_stats.stop_tracking()
                        else:
                            # Reclaim a manager-reopened order before another
                            # automatic worker can reserve the same Change.
                            update_order_in_statistics(
                                worker, current_order, add_name=True
                            )
                            was_automatic = (
                                worker.get("RETAIL_AUTOMATIC_ORDER")
                                == current_order
                            )
                            message = f"? Order {current_order} was changed"
                            Printer(message)
                            send(worker["DISPLAY"], message)
                            current_order, photo_pending, photo_start = process_scan(
                                worker,
                                config,
                                current_order,
                                orders,
                                current_order,
                                counts,
                                extras,
                                errors,
                                no_barcode_items,
                                False,
                                0.0,
                                automatic_assignment=was_automatic,
                                record_performance=False,
                            )
                elif department == RETAIL_UP_DEPARTMENT:
                    retail_up_header_requested = True

            if retail_up_header_requested and department == RETAIL_UP_DEPARTMENT:
                send_RETAIL_UP_display_state(worker)

            ok = False
            frame = None
            if cap is not None:
                ok, frame = cap.read()
            if cap is not None and ok:
                preview = cv2.resize(frame, None, fx=0.15, fy=0.15)
                if identity.department.upper() == "RETAIL":
                    (
                        completed_orders,
                        total_orders,
                        cancelled_orders,
                    ) = RETAIL_daily_order_counts(worker)
                    draw_RETAIL_order_counters(
                        preview,
                        completed_orders,
                        total_orders,
                        cancelled_orders,
                    )
                cv2.imshow(window_name, preview)

            line = display.readline().decode("utf-8", errors="ignore").strip()
            if not line and external_scanner is not None:
                try:
                    scanner_line = external_scanner.readline().decode(
                        "utf-8",
                        errors="ignore",
                    ).strip()
                except (serial.SerialException, OSError) as error:
                    message = (
                        f"XXX {identity.scanner_name} disconnected: {error}"
                    )
                    Printer(message)
                    send(display, message)
                    close_serial_safely(external_scanner)
                    external_scanner = None
                else:
                    if scanner_line:
                        print(f"* {identity.scanner_name}: {scanner_line}")
                        line = (
                            scanner_line
                            if scanner_line.startswith("Scan:")
                            else f"Scan:{scanner_line}"
                        )
            if is_display_boot_message(line):
                print(f"? AutoChecker {display_number} reset detected")
                display_reset_event.set()
                break

            command = parse_command(line) if line else None
            if (
                command is not None
                and command[0] == "Stop"
                and stop_matches_identity(command[1], identity)
            ):
                print(f"* Stop received for {identity}")
                break

            if line.startswith("changeName:"):
                new_name = line[len("changeName:"):].strip()
                if not new_name:
                    message = "XXX Name is empty"
                    Printer(message)
                    send(worker["DISPLAY"], message)
                else:
                    performance_stats.change_name(new_name)
                    identity = CheckerIdentity(
                        new_name,
                        identity.department,
                        identity.camera_number,
                        identity.printer_name,
                        identity.scanner_name,
                    )
                    worker["NAME"] = new_name
                    update_display_last_identity(display_number, identity)
                    display.write(b"<ok>\n")
                    display.flush()

            if line.startswith("CancelOrder:"):
                cancelled_order = line[len("CancelOrder:"):].strip()
                if cancelled_order and cancel_order(worker, cancelled_order):
                    display.write(b"<ok>\n")
                    display.flush()
                    if current_order and current_order.lstrip("#") == cancelled_order.lstrip("#"):
                        current_order = None
                        counts.clear()
                        extras.clear()
                        errors.clear()
                        no_barcode_items.clear()
                        photo_pending = False
                        performance_stats.stop_tracking()
                else:
                    message = f"XXX Order not found {cancelled_order}"
                    Printer(message)
                    send(worker["DISPLAY"], message)
                    display.write(b"</ok>\n")

            if line.startswith("ADD:"):
                parameters = line[len("ADD:"):].split(";")
                if len(parameters) != 3:
                    message = "XXX ADD command must contain exactly three parameters"
                    Printer(message)
                    send(worker["DISPLAY"], message)
                else:
                    previous_config = load_settings()
                    added, error = add_setting_value(*parameters)
                    if added:
                        config = load_settings()
                        if cap is not None and identity.camera_number is not None:
                            current_focus = config.get("FOCUS", {}).get(
                                str(identity.camera_number)
                            )
                            if current_focus is not None:
                                cap.set(cv2.CAP_PROP_FOCUS, current_focus)
                        display.write(b"<ok>\n")
                        display.flush()
                        if config != previous_config:
                            settings_update_requests.put(display_number)
                    else:
                        message = f"XXX {error}"
                        Printer(message)
                        send(worker["DISPLAY"], message)

            if line.startswith("APPLY:"):
                parameters = line[len("APPLY:"):].split(";")
                previous_config = load_settings()
                applied, error = apply_other_colours(parameters)
                if applied:
                    config = load_settings()
                    display.write(b"<ok>\n")
                    display.flush()
                    if config != previous_config:
                        settings_update_requests.put(display_number)
                else:
                    message = f"XXX {error}"
                    Printer(message)
                    send(worker["DISPLAY"], message)

            if line in {"LastPDF", "NextPDF"}:
                if identity.department.upper() != "RETAIL":
                    message = "XXX PDF commands are available only for RETAIL"
                    Printer(message)
                    send(worker["DISPLAY"], message)
                else:
                    if line == "NextPDF":
                        if (
                            label_ocr_process is not None
                            and label_ocr_process_is_alive(label_ocr_process)
                        ):
                            # Coalesce repeated requests into one follow-up run.
                            label_ocr_rerun_requested = True
                        else:
                            if label_ocr_process is not None:
                                join_finished_label_ocr_process(
                                    label_ocr_process
                                )
                            label_ocr_process = start_label_ocr_process(
                                identity.department,
                                downloads_dirs=worker.get(
                                    "DOWNLOAD_DIRECTORIES"
                                ),
                            )
                    loaded_orders = parse_orders_RETAIL(
                        worker, config, previous=(line == "LastPDF")
                    )
                    # Keep the currently loaded list if no previous PDF exists.
                    if loaded_orders:
                        orders = loaded_orders
                        current_order = None
                        worker.pop("RETAIL_AUTOMATIC_ORDER", None)
                        counts.clear()
                        extras.clear()
                        errors.clear()
                        no_barcode_items.clear()
                        photo_pending = False
                        performance_stats.stop_tracking()
                        display.write(b"<Ready>\n")
                        display.flush()

            if line.startswith("Print_Label:"):
                if identity.department.upper() != RETAIL_UP_DEPARTMENT:
                    message = (
                        "XXX Print_Label is available only for RETAIL_UP"
                    )
                    Printer(message)
                    send(worker["DISPLAY"], message)
                else:
                    try:
                        first_order, last_order = (
                            parse_RETAIL_UP_print_label_command(line)
                        )
                    except ValueError as error:
                        message = f"XXX {error}"
                        Printer(message)
                        send(worker["DISPLAY"], message)
                    else:
                        if last_order is None:
                            label_processed = process_RETAIL_UP_scan(
                                worker,
                                config,
                                first_order,
                                parenthesize_print_name=True,
                            )
                        else:
                            label_processed = process_RETAIL_UP_label_range(
                                worker,
                                config,
                                first_order,
                                last_order,
                            ) > 0
                        if label_processed:
                            settings_update_requests.put((
                                "retail_up_progress",
                                display_number,
                            ))

            if line == "NextOrder":
                if identity.department.upper() != "RETAIL":
                    message = "XXX NextOrder is available only for RETAIL"
                    Printer(message)
                    send(worker["DISPLAY"], message)
                else:
                    next_state = automatically_assign_next_RETAIL_order(
                        worker,
                        config,
                        orders,
                        counts,
                        extras,
                        errors,
                        no_barcode_items,
                    )
                    if next_state[0] is not None:
                        current_order, photo_pending, photo_start = next_state

            is_direct_B2B_order = (
                identity.department.upper() == "B2B"
                and line.startswith("#")
            )
            if line.startswith("Scan:") or is_direct_B2B_order:
                code = (
                    line[len("Scan:"):].strip()
                    if line.startswith("Scan:")
                    else line.strip()
                )
                if code:
                    if identity.department.upper() == RETAIL_UP_DEPARTMENT:
                        performance_stats.record_scan()
                        label_processed = process_RETAIL_UP_scan(
                            worker, config, code
                        )
                        if not label_processed:
                            performance_stats.record_mistake()
                        else:
                            settings_update_requests.put((
                                "retail_up_progress",
                                display_number,
                            ))
                    elif identity.department.upper() == "B2B" and code.startswith("#"):
                        is_cancelled = B2B_order_is_cancelled(worker, code)
                        if is_cancelled:
                            performance_stats.record_scan()
                            performance_stats.record_mistake()
                            message = f"XXX Cancelled order {code}"
                            Printer(message)
                            send(worker["DISPLAY"], message)
                        else:
                            pdf_path, history_error = prepare_B2B_order_history(
                                worker, code
                            )
                            if pdf_path is None:
                                performance_stats.record_scan()
                                performance_stats.record_mistake()
                                if history_error == "move_failed":
                                    message = (
                                        f"XXX Cannot move order PDF to History: {code}"
                                    )
                                elif history_error == "storage_unavailable":
                                    message = (
                                        "XXX B2B network storage is unavailable; "
                                        "check the console"
                                    )
                                else:
                                    message = f"XXX Unknown order {code}"
                                Printer(message)
                                send(worker["DISPLAY"], message)
                            else:
                                activated, reason = activate_B2B_order(
                                    worker, code, pdf_path
                                )
                                if not activated:
                                    performance_stats.record_scan()
                                    performance_stats.record_mistake()
                                    if reason == "cancelled":
                                        message = f"XXX Cancelled order {code}"
                                    else:
                                        message = f"XXX Invalid B2B index entry for {code}"
                                    Printer(message)
                                    send(worker["DISPLAY"], message)
                                else:
                                    loaded_orders = parse_orders_B2B(
                                        worker, config, code, pdf_path
                                    )
                                    if loaded_orders:
                                        orders = loaded_orders
                                        current_order, photo_pending, photo_start = process_scan(
                                            worker, config, code, orders, current_order, counts, extras,
                                            errors, no_barcode_items, photo_pending, photo_start,
                                        )
                                    else:
                                        performance_stats.record_scan()
                                        performance_stats.record_mistake()
                    else:
                        current_order, photo_pending, photo_start = process_scan(
                            worker, config, code, orders, current_order, counts, extras,
                            errors, no_barcode_items, photo_pending, photo_start,
                        )

            if line == "Photo":
                photographed_order = current_order
                saved, _first_photo = save_photo(
                    worker,
                    current_order,
                    frame if ok else None,
                    manually=True,
                )
                if saved:
                    performance_stats.record_photo(photographed_order)
                    if identity.department.upper() == "RETAIL":
                        # A photo requested by the display ends the automatic
                        # assignment chain. Only NextOrder can start it again.
                        worker.pop("RETAIL_AUTOMATIC_ORDER", None)
                photo_pending = False

            if photo_pending and time.time() - photo_start >= PHOTO_DELAY:
                photographed_order = current_order
                continue_automatic_assignment = (
                    photographed_order is not None
                    and worker.get("RETAIL_AUTOMATIC_ORDER")
                    == photographed_order
                )
                saved, first_photo = save_photo(
                    worker,
                    current_order,
                    frame if ok else None,
                )
                if saved:
                    performance_stats.record_photo(photographed_order)
                photo_pending = False
                if (
                    saved
                    and first_photo
                    and identity.department.upper() == "RETAIL"
                    and continue_automatic_assignment
                ):
                    worker.pop("RETAIL_AUTOMATIC_ORDER", None)
                    next_state = automatically_assign_next_RETAIL_order(
                        worker,
                        config,
                        orders,
                        counts,
                        extras,
                        errors,
                        no_barcode_items,
                    )
                    if next_state[0] is not None:
                        current_order, photo_pending, photo_start = next_state

            if cap is not None:
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    print(f"* Stopped locally: {identity}")
                    break

    except (serial.SerialException, OSError) as error:
        display_disconnect_event.set()
        print(f"XXX Checker {identity} lost display connection: {error}")
    except Exception as error:
        print(f"XXX Checker {identity} stopped with error: {error}")
    finally:
        if performance_stats is not None:
            performance_stats.flush()
        if cap is not None:
            cap.release()
        if cap is not None:
            cv2.destroyAllWindows()
        close_serial_safely(display)
        close_serial_safely(external_scanner)
        join_finished_label_ocr_process(label_ocr_process)


































def open_display(port: str) -> serial.Serial:
    return serial.Serial(port, baudrate=DISPLAY_BAUDRATE, timeout=DISPLAY_TIMEOUT)


def notify_launcher_ready(display: serial.Serial) -> None:
    """Tell the display that the launcher is ready to accept commands."""
    display.write(b"</Ready>\n")
    display.flush()


class _LauncherDisplaySink:
    """Serial-like sink used by launcher's background order maintenance."""

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None


def create_launcher_RETAIL_worker(settings: dict) -> dict:
    """Build the launcher's RETAIL storage context without opening hardware."""
    photo_dir, statistics_dir = department_paths("RETAIL")
    worker = {
        "NAME": "Launcher",
        "DEPARTMENT": "RETAIL",
        "DISPLAY": _LauncherDisplaySink(),
        "FOTO": photo_dir,
        "STATISTICS": statistics_dir,
        "HAS_CAMERA": False,
        "PRINTER": "",
        # Raw browser names (mpdf/qwe/qwez) always arrive on this computer.
        # In support mode only their finalized files are sent to the main PC.
        "LOCAL_DOWNLOADS": Path.home() / "Downloads",
    }
    configure_worker_storage(worker, settings)
    synchronize_support_statistics(worker)
    # Besides loading known orders, this upgrades existing statistics lines
    # to the explicit `Delivery TrackNumber` prefix on launcher startup.
    load_RETAIL_orders_from_statistics(worker, settings)
    return worker


def launcher_statistics_order_progress(line: str) -> tuple[bool, bool]:
    """Return assembled/packed state after the latest Change boundary."""
    assembled = "+" in retail_stat_effective_suffix(line)
    packed = retail_stat_packing_name(line) is not None
    return assembled, packed


def print_launcher_completion_summary(
    delivery: str,
    completed: int,
    incomplete: list[str],
    cancelled: list[str],
) -> None:
    if incomplete:
        active_total = completed + len(incomplete)
        message = (
            f"! {delivery} ({completed}/{active_total} completed): "
            f"incomplete orders: {', '.join(incomplete)}."
        )
    else:
        message = (
            f"* {delivery} ({completed} completed): all orders are completed."
        )
    if cancelled:
        message += f" Cancelled orders: {', '.join(cancelled)}."
    print(message)


def check_launcher_orders_completion(worker: dict) -> None:
    """Implement Admin's `check` command against authoritative statistics."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        print(f"XXX Statistics file is unavailable: {stat_file}")
        return
    results = {
        delivery: {"completed": [], "incomplete": [], "cancelled": []}
        for delivery in RETAIL_DELIVERY_ORDER
    }
    for line in read_stat_lines(stat_file):
        order_id = retail_order_id_from_stat_line(line)
        if order_id is None:
            continue
        status = results[RETAIL_delivery_from_stat_line(line)]
        if retail_stat_is_cancelled(line):
            if order_id not in status["cancelled"]:
                status["cancelled"].append(order_id)
            continue
        assembled, packed = launcher_statistics_order_progress(line)
        target = "completed" if assembled and packed else "incomplete"
        if order_id not in status[target]:
            status[target].append(order_id)

    display_names = {
        "UPS": "UPS",
        "Zasilkovna": "Zasilkovna (Packeta)",
        "Postal": "Postal",
    }
    for delivery in RETAIL_DELIVERY_ORDER:
        status = results[delivery]
        print_launcher_completion_summary(
            display_names[delivery],
            len(status["completed"]),
            status["incomplete"],
            status["cancelled"],
        )


def report_launcher_order_progress(
    worker: dict,
    order_ids: list[str],
) -> None:
    """Print Admin-compatible assembly/packing warnings for changed orders."""
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        print(f"XXX Statistics file is unavailable: {stat_file}")
        return
    lines = read_stat_lines(stat_file)
    requested = set(order_ids)
    found = set()
    part_number = None
    number_in_part = 0
    for line in lines:
        header = re.search(
            r"-{10}.*?\sPart\s+(\d+)\.pdf-{10}",
            line,
            re.IGNORECASE,
        )
        if header is not None:
            part_number = int(header.group(1))
            number_in_part = 0
            continue
        stored_order = retail_order_id_from_stat_line(line)
        if stored_order is None:
            continue
        number_in_part += 1
        if stored_order not in requested:
            continue
        found.add(stored_order)
        assembled, packed = launcher_statistics_order_progress(line)
        location = (
            f"Part {part_number if part_number is not None else 'unknown'}, "
            f"number {number_in_part}"
        )
        if assembled:
            print(f"? Order {stored_order} is already assembled; {location}")
        if packed:
            print(f"? Order {stored_order} is already packed; {location}")
        if not assembled and not packed:
            print(f"* Order {stored_order} is active; {location}")
    for missing_order in order_ids:
        if missing_order not in found:
            print(f"XXX Order not found in today's statistics: {missing_order}")


def toggle_launcher_order_cancelled(worker: dict, order_id: str) -> bool:
    """Add Cancelled, or interactively remove an existing marker."""
    normalized_order = f"#{order_id.strip().lstrip('#')}"
    stat_file = retail_statistics_file(worker)
    if not stat_file.is_file():
        print(f"XXX Statistics file is unavailable: {stat_file}")
        return False

    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        line_index = next(
            (
                index
                for index, line in enumerate(lines)
                if retail_order_id_from_stat_line(line) == normalized_order
            ),
            None,
        )
        if line_index is None:
            print(f"XXX Active order not found: {normalized_order}")
            return False
        raw_text = lines[line_index].rstrip("\r\n")
        already_cancelled = retail_stat_is_cancelled(raw_text)
        assembled, packed = launcher_statistics_order_progress(raw_text)
        if not already_cancelled:
            lines[line_index] = raw_text + " Cancelled\n"
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, stat_file, lines)

    if not already_cancelled:
        print(f"* Order {normalized_order} cancelled successfully")
        if assembled:
            print(f"? Order {normalized_order} was already assembled")
        if packed:
            print(f"? Order {normalized_order} was already packed")
        return True

    print(
        f"? Order {normalized_order} is already cancelled. "
        "Remove cancellation? Enter y or n:"
    )
    try:
        answer = input("> ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in {"y", "yes", "у", "да"}:
        print(f"* Cancellation kept for order {normalized_order}")
        return True

    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        for index, line in enumerate(lines):
            raw_text = line.rstrip("\r\n")
            if retail_order_id_from_stat_line(raw_text) != normalized_order:
                continue
            order_match = re.search(
                rf"(?<!\S){re.escape(normalized_order)}(?=\s|$)",
                raw_text,
            )
            if order_match is None:
                break
            suffix = raw_text[order_match.end():]
            cleaned_suffix = re.sub(
                r"(?<!\S)Cancelled(?=\s|$)",
                "",
                suffix,
                count=1,
                flags=re.IGNORECASE,
            )
            cleaned_suffix = " ".join(cleaned_suffix.split())
            lines[index] = (
                raw_text[:order_match.end()]
                + (f" {cleaned_suffix}" if cleaned_suffix else "")
                + "\n"
            )
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            mirror_statistics_lines(worker, stat_file, lines)
            print(f"* Cancellation removed from order {normalized_order}")
            return True
    print(f"XXX Order not found while removing cancellation: {normalized_order}")
    return False


def send_launcher_label_part(worker: dict, part_number: int) -> bool:
    """Implement `send part N`; it is meaningful only on a support PC."""
    if not worker.get("SUPPORT"):
        print(
            "XXX send part is unavailable when support is false: "
            "this is already the main computer"
        )
        return False
    filename = (
        f"{datetime.date.today():%d.%m.%Y} Part {part_number} (Label).pdf"
    )
    source = launcher_local_downloads(worker) / filename
    if not source.is_file():
        print(f"XXX Today's Label PDF was not found: {source.name}")
        return False
    destination = Path(worker["DOWNLOAD_DIRECTORIES"][0]) / filename
    try:
        copy_file_atomically(source, destination)
    except Exception as error:
        print(f"XXX Cannot send {filename}: {error}")
        return False
    print(f"* Label PDF sent to main computer: {destination}")
    return True


def handle_launcher_console_command(
    command: str,
    worker: dict,
) -> None:
    """Handle the maintained subset of Admin console commands."""
    if not command:
        return
    if command.casefold() == "help":
        print("check                 - check today's RETAIL orders")
        print("cancel [#]1234567     - add or remove Cancelled")
        print("send part N           - send today's Label PDF to the main PC")
        return
    if command.casefold() == "check":
        check_launcher_orders_completion(worker)
        return
    cancel_match = re.fullmatch(r"cancel\s+#?(\d+)", command, re.IGNORECASE)
    if cancel_match:
        toggle_launcher_order_cancelled(worker, cancel_match.group(1))
        return
    send_match = re.fullmatch(r"send\s+part\s+(\d+)", command, re.IGNORECASE)
    if send_match:
        part_number = int(send_match.group(1))
        if part_number < 1:
            print("XXX Part number must be 1 or greater")
        else:
            send_launcher_label_part(worker, part_number)
        return
    print("XXX Unknown command. Type: help")


def launcher_console_loop(worker: dict) -> None:
    print("* Commands: check | cancel [#]1234567 | send part N | help")
    while True:
        try:
            command = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        try:
            handle_launcher_console_command(command, worker)
        except Exception as error:
            print(f"XXX Console command failed: {error}")


def dispatcher() -> None:
    """Own idle display ports and hand each busy port to its worker process."""
    latest_display_versions = get_latest_display_versions()

    cleanup_launcher_photos()
    cameras = find_autochecker_cameras()
    if not cameras:
        print("? No AutoChecker cameras found; launcher will continue without cameras")
    try:
        printers = find_available_printers(announce=True)
    except Exception as error:
        printers = []
        print(f"XXX Cannot enumerate printers: {error}")
    if not printers:
        print("? No available printers found; launcher will continue without printers")

    # Camera focus defaults do not depend on whether a display is currently on.
    settings = ensure_detected_settings(cameras, {})
    display_ports = find_autochecker_displays(latest_display_versions)
    if not display_ports:
        print("? No AutoChecker displays found; monitoring new COM ports")

    scanners = find_external_scanners(
        set(display_ports.values()),
        announce=True,
    )
    if not scanners:
        print("? No external scanners found; launcher will continue without them")

    # Add the per-display saved-selection slots after their IDs are known.
    settings = ensure_detected_settings(cameras, display_ports)
    ensure_worker_statistics_files(settings)
    try:
        refresh_B2B_product_count(settings)
    except Exception as error:
        print(f"XXX Cannot update B2B Count.json: {error}")

    launcher_retail_worker = None
    launcher_console_started = False
    retail_monitor_last_error: str | None = None
    retail_monitor_auth_blocked = SUPPORT_NETWORK_AUTH_BLOCKED
    if retail_monitor_auth_blocked:
        print(
            "? RETAIL order monitor is paused until network credentials "
            "are fixed and AutoChecker is restarted"
        )
    else:
        try:
            launcher_retail_worker = create_launcher_RETAIL_worker(settings)
            process_pending_admin_download_files(launcher_retail_worker)
            process_pending_RETAIL_order_sources(
                launcher_retail_worker, settings
            )
            process_pending_RETAIL_change_pdfs(
                launcher_retail_worker, settings
            )
            process_pending_RETAIL_change_label_pdfs(
                launcher_retail_worker
            )
        except Exception as error:
            retail_monitor_last_error = str(error)
            print(
                "XXX RETAIL order monitor initialization failed: "
                f"{error}"
            )
            if is_support_authentication_error(error):
                retail_monitor_auth_blocked = True
                print(
                    "? RETAIL order monitor stopped its automatic network "
                    "retries to prevent a Windows account lockout"
                )

    if launcher_retail_worker is not None:
        threading.Thread(
            target=launcher_console_loop,
            args=(launcher_retail_worker,),
            name="AutoChecker console commands",
            daemon=True,
        ).start()
        launcher_console_started = True

    idle_displays: dict[str, serial.Serial] = {}
    workers: dict[str, multiprocessing.Process] = {}
    worker_identities: dict[str, CheckerIdentity] = {}
    worker_update_queues: dict[str, multiprocessing.Queue] = {}
    worker_reset_events: dict[str, multiprocessing.Event] = {}
    worker_disconnect_events: dict[str, multiprocessing.Event] = {}
    reset_recovery_deadlines: dict[str, float] = {}
    settings_update_requests: multiprocessing.Queue = multiprocessing.Queue()
    observed_ports = {port_info.device for port_info in list_ports.comports()}
    disconnected_ports_waiting_removal: set[str] = set()
    scanner_identity_retry_deadlines = {
        port: time.monotonic() + 1.0
        for port in SCANNER_IDENTITY_RETRY_PORTS
        if port not in set(display_ports.values()) | set(scanners.values())
    }
    next_port_scan = time.monotonic() + PORT_SCAN_INTERVAL
    next_camera_scan = time.monotonic() + CAMERA_SCAN_INTERVAL
    pending_camera_snapshot: dict[int, int] | None = None
    pending_camera_polls = 0
    next_printer_scan = time.monotonic() + PRINTER_SCAN_INTERVAL
    pending_printer_snapshot: list[str] | None = None
    pending_printer_polls = 0
    next_retail_order_scan = time.monotonic() + ORDER_SOURCE_SCAN_INTERVAL

    try:
        for display_number, port in list(display_ports.items()):
            try:
                idle_displays[display_number] = open_display(port)
                upload_display_data(
                    idle_displays[display_number],
                    display_number,
                    settings,
                    cameras,
                    printers,
                    list(scanners),
                )
                notify_launcher_ready(idle_displays[display_number])
            except (serial.SerialException, OSError) as error:
                print(f"XXX Cannot open AutoChecker {display_number} on {port}: {error}")
                close_serial_safely(idle_displays.pop(display_number, None))
                display_ports.pop(display_number, None)

        if display_ports:
            print("* Dispatcher ready: " + ", ".join(
                f"AutoChecker {number} ({port})" for number, port in display_ports.items()
            ))
        else:
            print("* Dispatcher ready; waiting for AutoChecker displays")

        while True:
            # A stopped worker releases its port; the dispatcher can listen to it again.
            for display_number, process in list(workers.items()):
                if not process.is_alive():
                    process.join()
                    del workers[display_number]
                    worker_identities.pop(display_number, None)
                    update_queue = worker_update_queues.pop(display_number)
                    update_queue.close()
                    update_queue.join_thread()
                    was_reset = worker_reset_events.pop(display_number).is_set()
                    was_disconnected = worker_disconnect_events.pop(display_number).is_set()
                    port = display_ports.get(display_number)

                    if was_disconnected or port is None:
                        if port is not None:
                            display_ports.pop(display_number, None)
                            disconnected_ports_waiting_removal.add(port)
                        settings = load_settings()
                        refresh_idle_display_data(
                            settings,
                            cameras_available_for_selection(
                                cameras,
                                worker_identities,
                            ),
                            printers_available_for_selection(
                                printers,
                                worker_identities,
                            ),
                            scanners_available_for_selection(
                                scanners,
                                worker_identities,
                            ),
                            idle_displays,
                        )
                        print(
                            f"? AutoChecker {display_number} disconnected; "
                            "monitoring COM ports"
                        )
                        continue

                    try:
                        idle_displays[display_number] = open_display(port)
                        if was_reset:
                            time.sleep(DISPLAY_BOOT_DELAY)
                            idle_displays[display_number].reset_input_buffer()
                        settings = load_settings()
                        refresh_idle_display_data(
                            settings,
                            cameras_available_for_selection(
                                cameras,
                                worker_identities,
                            ),
                            printers_available_for_selection(
                                printers,
                                worker_identities,
                            ),
                            scanners_available_for_selection(
                                scanners,
                                worker_identities,
                            ),
                            idle_displays,
                        )
                        notify_launcher_ready(idle_displays[display_number])
                        print(
                            f"* AutoChecker {display_number} is ready for a new Start command"
                        )
                    except (serial.SerialException, OSError) as error:
                        print(
                            f"? AutoChecker {display_number} unavailable on {port}: {error}; "
                            "monitoring COM ports"
                        )
                        close_serial_safely(idle_displays.pop(display_number, None))
                        display_ports.pop(display_number, None)
                        disconnected_ports_waiting_removal.add(port)

            if (
                time.monotonic() >= next_retail_order_scan
                and not retail_monitor_auth_blocked
            ):
                next_retail_order_scan = (
                    time.monotonic() + ORDER_SOURCE_SCAN_INTERVAL
                )
                try:
                    if launcher_retail_worker is None:
                        launcher_retail_worker = create_launcher_RETAIL_worker(
                            settings
                        )
                    if not launcher_console_started:
                        threading.Thread(
                            target=launcher_console_loop,
                            args=(launcher_retail_worker,),
                            name="AutoChecker console commands",
                            daemon=True,
                        ).start()
                        launcher_console_started = True
                    process_pending_admin_download_files(
                        launcher_retail_worker
                    )
                    imported_order_ids = process_pending_RETAIL_order_sources(
                        launcher_retail_worker, settings
                    )
                    changed_orders, _changed_metadata, _new_markers = (
                        process_pending_RETAIL_change_pdfs(
                            launcher_retail_worker, settings
                        )
                    )
                    changed_label_ids = (
                        process_pending_RETAIL_change_label_pdfs(
                            launcher_retail_worker
                        )
                    )
                    refreshed_order_ids = (
                        set(imported_order_ids) | set(changed_orders)
                    )
                    if refreshed_order_ids:
                        broadcast_RETAIL_orders_updated(
                            worker_update_queues,
                            worker_identities,
                            refreshed_order_ids,
                        )
                    if changed_label_ids:
                        broadcast_RETAIL_UP_header(
                            worker_update_queues,
                            worker_identities,
                        )
                    if retail_monitor_last_error is not None:
                        print("* RETAIL order monitor connection restored")
                        retail_monitor_last_error = None
                except Exception as error:
                    error_text = str(error)
                    if error_text != retail_monitor_last_error:
                        print(f"XXX RETAIL order monitor error: {error}")
                        retail_monitor_last_error = error_text
                    next_retail_order_scan = (
                        time.monotonic() + ORDER_SOURCE_RETRY_INTERVAL
                    )
                    if is_support_authentication_error(error):
                        retail_monitor_auth_blocked = True
                        print(
                            "? RETAIL order monitor stopped its automatic "
                            "network retries to prevent a Windows account "
                            "lockout; fix credentials and restart AutoChecker"
                        )

            if time.monotonic() >= next_port_scan:
                try:
                    current_port_infos = {
                        port_info.device: port_info
                        for port_info in list_ports.comports()
                    }
                    current_ports = set(current_port_infos)
                except OSError as error:
                    print(f"XXX Cannot enumerate COM ports: {error}")
                    current_port_infos = {}
                    current_ports = observed_ports

                scan_time = time.monotonic()
                observed_ports.intersection_update(current_ports)
                disconnected_ports_waiting_removal.intersection_update(current_ports)
                disconnected_scanners = [
                    scanner_name
                    for scanner_name, scanner_port in scanners.items()
                    if scanner_port not in current_ports
                ]
                if disconnected_scanners:
                    for scanner_name in disconnected_scanners:
                        scanner_port = scanners.pop(scanner_name)
                        print(f"? {scanner_name} disconnected from {scanner_port}")
                    settings = load_settings()
                    broadcast_display_data(
                        settings,
                        cameras,
                        printers,
                        scanners,
                        idle_displays,
                        worker_update_queues,
                        worker_identities,
                    )

                known_ports = set(display_ports.values()) | set(scanners.values())
                for port in list(scanner_identity_retry_deadlines):
                    if port not in current_ports or port in known_ports:
                        scanner_identity_retry_deadlines.pop(port, None)
                        SCANNER_IDENTITY_RETRY_PORTS.discard(port)
                for port in SCANNER_IDENTITY_RETRY_PORTS:
                    if port in current_ports and port not in known_ports:
                        scanner_identity_retry_deadlines.setdefault(
                            port,
                            scan_time + SCANNER_IDENTITY_RETRY_INTERVAL,
                        )
                retry_ports = {
                    port
                    for port, deadline in scanner_identity_retry_deadlines.items()
                    if deadline <= scan_time
                    and port not in disconnected_ports_waiting_removal
                }
                new_ports = (
                    current_ports
                    - observed_ports
                    - known_ports
                    - disconnected_ports_waiting_removal
                ) | retry_ports
                for port in sorted(new_ports):
                    observed_ports.add(port)
                    display_number = identify_autochecker_display(
                        port,
                        latest_display_versions,
                    )
                    if display_number is None:
                        port_info = current_port_infos.get(port)
                        if port_info is None:
                            continue
                        scanner_name = identify_external_scanner(port_info)
                        if scanner_name is None:
                            if port in SCANNER_IDENTITY_RETRY_PORTS:
                                scanner_identity_retry_deadlines[port] = (
                                    time.monotonic()
                                    + SCANNER_IDENTITY_RETRY_INTERVAL
                                )
                            else:
                                scanner_identity_retry_deadlines.pop(port, None)
                            continue
                        scanner_identity_retry_deadlines.pop(port, None)
                        duplicate = next(
                            (
                                existing_name
                                for existing_name in scanners
                                if existing_name.casefold()
                                == scanner_name.casefold()
                            ),
                            None,
                        )
                        if duplicate is not None:
                            print(
                                f"XXX Duplicate {scanner_name} on {port}; "
                                f"already using {scanners[duplicate]}"
                            )
                            continue
                        scanners[scanner_name] = port
                        settings = load_settings()
                        broadcast_display_data(
                            settings,
                            cameras,
                            printers,
                            scanners,
                            idle_displays,
                            worker_update_queues,
                            worker_identities,
                        )
                        continue
                    scanner_identity_retry_deadlines.pop(port, None)
                    SCANNER_IDENTITY_RETRY_PORTS.discard(port)
                    if display_number in display_ports:
                        print(
                            f"XXX Duplicate AutoChecker {display_number} on {port}; "
                            f"already using {display_ports[display_number]}"
                        )
                        continue

                    current_settings = ensure_detected_settings(
                        cameras,
                        {display_number: port},
                    )
                    display = None
                    try:
                        display = open_display(port)
                        upload_display_data(
                            display,
                            display_number,
                            current_settings,
                            cameras_available_for_selection(
                                cameras,
                                worker_identities,
                            ),
                            printers_available_for_selection(
                                printers,
                                worker_identities,
                            ),
                            scanners_available_for_selection(
                                scanners,
                                worker_identities,
                            ),
                        )
                        notify_launcher_ready(display)
                    except (serial.SerialException, OSError) as error:
                        print(
                            f"XXX Cannot activate AutoChecker {display_number} "
                            f"on {port}: {error}"
                        )
                        close_serial_safely(display)
                        continue

                    display_ports[display_number] = port
                    idle_displays[display_number] = display
                    settings = current_settings
                    print(
                        f"* AutoChecker {display_number} connected on {port} and ready"
                    )

                next_port_scan = time.monotonic() + PORT_SCAN_INTERVAL

            if time.monotonic() >= next_camera_scan:
                try:
                    detected_cameras = find_autochecker_cameras(announce=False)
                except Exception as error:
                    print(f"XXX Cannot enumerate AutoChecker cameras: {error}")
                    detected_cameras = cameras

                previous_camera_numbers = set(cameras)
                if detected_cameras == cameras:
                    pending_camera_snapshot = None
                    pending_camera_polls = 0
                elif detected_cameras == pending_camera_snapshot:
                    pending_camera_polls += 1
                else:
                    pending_camera_snapshot = detected_cameras
                    pending_camera_polls = 1

                if (
                    pending_camera_snapshot is not None
                    and pending_camera_polls >= CAMERA_STABLE_POLLS
                ):
                    stable_cameras = pending_camera_snapshot
                    stable_camera_numbers = set(stable_cameras)
                    connected_numbers = (
                        stable_camera_numbers - previous_camera_numbers
                    )
                    disconnected_numbers = (
                        previous_camera_numbers - stable_camera_numbers
                    )

                    for camera_number in sorted(connected_numbers):
                        print(
                            f"* Camera connected: AutoChecker {camera_number} "
                            f"(OpenCV ID {stable_cameras[camera_number]})"
                        )
                    for camera_number in sorted(disconnected_numbers):
                        print(f"? Camera disconnected: AutoChecker {camera_number}")

                    camera_configuration_changed = stable_cameras != cameras
                    cameras = stable_cameras
                    pending_camera_snapshot = None
                    pending_camera_polls = 0

                    if connected_numbers and disconnected_numbers:
                        stop_reason = "Camera configuration changed"
                    elif connected_numbers:
                        stop_reason = "Camera connected"
                    elif disconnected_numbers:
                        stop_reason = "Camera disconnected"
                    else:
                        stop_reason = "Camera configuration changed"

                    if camera_configuration_changed:
                        for worker_display_number, worker_identity in list(
                            worker_identities.items()
                        ):
                            if worker_identity.camera_number is None:
                                continue
                            update_queue = worker_update_queues.get(
                                worker_display_number
                            )
                            if update_queue is not None:
                                update_queue.put_nowait(("stop", stop_reason))

                    settings = ensure_detected_settings(cameras, display_ports)
                    broadcast_display_data(
                        settings,
                        cameras,
                        printers,
                        scanners,
                        idle_displays,
                        worker_update_queues,
                        worker_identities,
                    )

                next_camera_scan = time.monotonic() + CAMERA_SCAN_INTERVAL

            if time.monotonic() >= next_printer_scan:
                try:
                    detected_printers = find_available_printers()
                except Exception as error:
                    print(f"XXX Cannot enumerate printers: {error}")
                    detected_printers = printers

                if detected_printers == printers:
                    pending_printer_snapshot = None
                    pending_printer_polls = 0
                elif detected_printers == pending_printer_snapshot:
                    pending_printer_polls += 1
                else:
                    pending_printer_snapshot = detected_printers
                    pending_printer_polls = 1

                if (
                    pending_printer_snapshot is not None
                    and pending_printer_polls >= PRINTER_STABLE_POLLS
                ):
                    stable_printers = pending_printer_snapshot
                    previous_by_key = {
                        printer.casefold(): printer for printer in printers
                    }
                    stable_by_key = {
                        printer.casefold(): printer for printer in stable_printers
                    }
                    for key in sorted(
                        stable_by_key.keys() - previous_by_key.keys()
                    ):
                        print(f"* Printer connected: {stable_by_key[key]}")
                    for key in sorted(
                        previous_by_key.keys() - stable_by_key.keys()
                    ):
                        print(f"? Printer disconnected: {previous_by_key[key]}")

                    printers = stable_printers
                    pending_printer_snapshot = None
                    pending_printer_polls = 0
                    broadcast_display_data(
                        settings,
                        cameras,
                        printers,
                        scanners,
                        idle_displays,
                        worker_update_queues,
                        worker_identities,
                    )

                next_printer_scan = (
                    time.monotonic() + PRINTER_SCAN_INTERVAL
                )

            settings_changed = False
            retail_up_progress_changed = False
            while True:
                try:
                    update_request = settings_update_requests.get_nowait()
                    if (
                        isinstance(update_request, tuple)
                        and update_request
                        and update_request[0] == "retail_up_progress"
                    ):
                        retail_up_progress_changed = True
                    else:
                        # Backward compatible with display-number messages
                        # sent by ADD/APPLY in older child processes.
                        settings_changed = True
                except queue.Empty:
                    break
            if retail_up_progress_changed:
                broadcast_RETAIL_UP_header(
                    worker_update_queues,
                    worker_identities,
                )
            if settings_changed:
                settings = load_settings()
                ensure_worker_statistics_files(settings)
                broadcast_display_data(
                    settings,
                    cameras,
                    printers,
                    scanners,
                    idle_displays,
                    worker_update_queues,
                    worker_identities,
                )

            for display_number, display in list(idle_displays.items()):
                port = display_ports[display_number]
                recovery_deadline = reset_recovery_deadlines.get(display_number)
                if recovery_deadline is not None and time.monotonic() >= recovery_deadline:
                    try:
                        display.reset_input_buffer()
                        settings = load_settings()
                        upload_display_data(
                            display,
                            display_number,
                            settings,
                            cameras_available_for_selection(
                                cameras,
                                worker_identities,
                            ),
                            printers_available_for_selection(
                                printers,
                                worker_identities,
                            ),
                            scanners_available_for_selection(
                                scanners,
                                worker_identities,
                            ),
                        )
                        notify_launcher_ready(display)
                        print(f"* AutoChecker {display_number} recovered after reset")
                        reset_recovery_deadlines.pop(display_number, None)
                    except (serial.SerialException, OSError) as error:
                        print(f"XXX Reset recovery failed on {port}: {error}")
                        reset_recovery_deadlines[display_number] = (
                            time.monotonic() + DISPLAY_BOOT_DELAY
                        )

                try:
                    line = display.readline().decode("utf-8", errors="ignore").strip()
                    if line:
                        print(line)
                except (serial.SerialException, OSError) as error:
                    print(f"XXX Read error on {port}: {error}")
                    close_serial_safely(display)
                    del idle_displays[display_number]
                    display_ports.pop(display_number, None)
                    disconnected_ports_waiting_removal.add(port)
                    reset_recovery_deadlines.pop(display_number, None)
                    print(
                        f"? AutoChecker {display_number} disconnected; "
                        "monitoring COM ports"
                    )
                    continue

                if is_display_boot_message(line):
                    reset_recovery_deadlines[display_number] = (
                        time.monotonic() + DISPLAY_BOOT_DELAY
                    )
                    print(
                        f"? AutoChecker {display_number} reset detected; "
                        "waiting for display startup"
                    )
                    continue

                # Ignore the remaining ESP32 boot log until recovery upload.
                if display_number in reset_recovery_deadlines:
                    continue

                if line.startswith("ADD:"):
                    parameters = line[len("ADD:"):].split(";")
                    if len(parameters) != 3:
                        message = "XXX ADD command must contain exactly three parameters"
                        Printer(message)
                        send(display, message)
                    else:
                        previous_settings = load_settings()
                        added, error = add_setting_value(*parameters)
                        if added:
                            settings = load_settings()
                            display.write(b"<ok>\n")
                            display.flush()
                            if settings != previous_settings:
                                ensure_worker_statistics_files(settings)
                                broadcast_display_data(
                                    settings,
                                    cameras,
                                    printers,
                                    scanners,
                                    idle_displays,
                                    worker_update_queues,
                                    worker_identities,
                                )
                        else:
                            message = f"XXX {error}"
                            Printer(message)
                            send(display, message)
                    continue

                if line.startswith("APPLY:"):
                    parameters = line[len("APPLY:"):].split(";")
                    previous_settings = load_settings()
                    applied, error = apply_other_colours(parameters)
                    if applied:
                        settings = load_settings()
                        display.write(b"<ok>\n")
                        display.flush()
                        if settings != previous_settings:
                            broadcast_display_data(
                                settings,
                                cameras,
                                printers,
                                scanners,
                                idle_displays,
                                worker_update_queues,
                                worker_identities,
                            )
                    else:
                        message = f"XXX {error}"
                        Printer(message)
                        send(display, message)
                    continue

                if line.startswith("CancelOrder:"):
                    cancelled_order = line[len("CancelOrder:"):].strip()
                    if (
                        cancelled_order
                        and cancel_order_from_launcher(
                            display_number,
                            cancelled_order,
                            settings,
                        )
                    ):
                        display.write(b"<ok>\n")
                        display.flush()
                    else:
                        message = f"XXX Order not found {cancelled_order}"
                        Printer(message)
                        send(display, message)
                        display.write(b"</ok>\n")
                        display.flush()
                    continue

                command = parse_command(line) if line else None
                if command is None:
                    continue

                action, identity = command
                if action != "Start":
                    print(f"? Ignoring Stop on idle port {port}: {identity}")
                    continue

                if identity.camera_number is None:
                    camera_id = None
                    focus = None
                else:
                    camera_owner = next(
                        (
                            worker_display
                            for worker_display, worker_identity
                            in worker_identities.items()
                            if worker_identity.camera_number
                            == identity.camera_number
                        ),
                        None,
                    )
                    if camera_owner is not None:
                        message = (
                            f"XXX Camera {identity.camera_number} is already in use"
                        )
                        Printer(message)
                        send(display, message)
                        notify_launcher_ready(display)
                        continue

                    current_settings = load_settings()
                    camera_id = cameras.get(identity.camera_number)
                    focus = current_settings.get("FOCUS", {}).get(
                        str(identity.camera_number)
                    )
                    if camera_id is None or focus is None:
                        message = (
                            f"XXX Camera {identity.camera_number} is unavailable"
                        )
                        Printer(message)
                        send(display, message)
                        notify_launcher_ready(display)
                        continue

                if identity.printer_name:
                    requested_printer = identity.printer_name.casefold()
                    printer_owner = next(
                        (
                            worker_display
                            for worker_display, worker_identity
                            in worker_identities.items()
                            if worker_identity.printer_name
                            and worker_identity.printer_name.casefold()
                            == requested_printer
                        ),
                        None,
                    )
                    if printer_owner is not None:
                        message = (
                            f"XXX Printer {identity.printer_name} is already in use"
                        )
                        Printer(message)
                        send(display, message)
                        notify_launcher_ready(display)
                        continue

                    actual_printer = next(
                        (
                            printer_name
                            for printer_name in printers
                            if printer_name.casefold() == requested_printer
                        ),
                        None,
                    )
                    if actual_printer is None:
                        message = (
                            f"XXX Printer {identity.printer_name} is unavailable"
                        )
                        Printer(message)
                        send(display, message)
                        notify_launcher_ready(display)
                        continue
                    identity = CheckerIdentity(
                        identity.name,
                        identity.department,
                        identity.camera_number,
                        actual_printer,
                        identity.scanner_name,
                    )

                scanner_port = None
                if identity.scanner_name:
                    requested_scanner = identity.scanner_name.casefold()
                    scanner_owner = next(
                        (
                            worker_display
                            for worker_display, worker_identity
                            in worker_identities.items()
                            if worker_identity.scanner_name
                            and worker_identity.scanner_name.casefold()
                            == requested_scanner
                        ),
                        None,
                    )
                    if scanner_owner is not None:
                        message = (
                            f"XXX {identity.scanner_name} is already in use"
                        )
                        Printer(message)
                        send(display, message)
                        notify_launcher_ready(display)
                        continue

                    actual_scanner = next(
                        (
                            scanner_name
                            for scanner_name in scanners
                            if scanner_name.casefold() == requested_scanner
                        ),
                        None,
                    )
                    if actual_scanner is None:
                        message = f"XXX {identity.scanner_name} is unavailable"
                        Printer(message)
                        send(display, message)
                        notify_launcher_ready(display)
                        continue
                    scanner_port = scanners[actual_scanner]
                    identity = CheckerIdentity(
                        identity.name,
                        identity.department,
                        identity.camera_number,
                        identity.printer_name,
                        actual_scanner,
                    )

                # Windows locks COM ports exclusively. Close it before the child opens it.
                display.close()
                del idle_displays[display_number]
                update_display_last_identity(display_number, identity)

                display_update_queue: multiprocessing.Queue = multiprocessing.Queue()
                display_reset_event: multiprocessing.Event = multiprocessing.Event()
                display_disconnect_event: multiprocessing.Event = multiprocessing.Event()
                process = multiprocessing.Process(
                    target=main,
                    args=(
                        identity,
                        camera_id,
                        focus,
                        port,
                        scanner_port,
                        display_number,
                        cameras,
                        printers,
                        list(scanners),
                        settings_update_requests,
                        display_update_queue,
                        display_reset_event,
                        display_disconnect_event,
                    ),
                    name=(
                        f"AutoChecker-{identity.camera_number}"
                        if identity.camera_number is not None
                        else f"AutoChecker-display-{display_number}-no-camera"
                    ),
                )
                process.start()
                workers[display_number] = process
                worker_identities[display_number] = identity
                worker_update_queues[display_number] = display_update_queue
                worker_reset_events[display_number] = display_reset_event
                worker_disconnect_events[display_number] = display_disconnect_event

                settings = load_settings()
                refresh_idle_display_data(
                    settings,
                    cameras_available_for_selection(
                        cameras,
                        worker_identities,
                    ),
                    printers_available_for_selection(
                        printers,
                        worker_identities,
                    ),
                    scanners_available_for_selection(
                        scanners,
                        worker_identities,
                    ),
                    idle_displays,
                )

            time.sleep(0.01)

    except KeyboardInterrupt:
        print("* Dispatcher stopped")
    finally:
        for display in idle_displays.values():
            close_serial_safely(display)
        for update_queue in worker_update_queues.values():
            try:
                update_queue.put_nowait(("stop", "Launcher stopped"))
            except (OSError, ValueError, queue.Full):
                pass
        for process in workers.values():
            process.join(timeout=3)
            if process.is_alive():
                process.terminate()
                process.join()
        for update_queue in worker_update_queues.values():
            update_queue.close()
            update_queue.join_thread()
        settings_update_requests.close()
        settings_update_requests.join_thread()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    check_for_updates()
    ensure_default_sound_file()
    startup_settings = ensure_settings_schema()
    if not support_mode_enabled(startup_settings):
        ensure_windows_network_discovery_enabled()
    ensure_windows_protected_print_disabled()
    if support_mode_enabled(startup_settings):
        if connect_support_network_session(startup_settings):
            startup_settings = synchronize_support_catalog_settings(
                startup_settings
            )
        else:
            print("? Product settings were not updated from the main computer")
    else:
        ensure_main_network_shares(startup_settings)
    sync_order_label_templates()
    dispatcher()

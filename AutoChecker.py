"""AutoChecker v2: dynamic discovery of displays, cameras and printers.

Protocol received from a display (one command per line):
    Start:<name>;<department>;<camera>;<printer>\n
    Stop:<name>;<department>;<camera>;<printer>\n
The printer field is optional for compatibility with existing displays.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile


PACKAGES_INSTALLED = False
DEPENDENCY_RESTART_MARKER = "AUTOCHECKER_DEPENDENCIES_RESTARTED"


def ensure_package(module_name: str, pip_name: str | None = None) -> None:
    """Install a missing third-party module for the current Python."""
    global PACKAGES_INSTALLED
    if pip_name is None:
        pip_name = module_name

    try:
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
    ("pygrabber", "pygrabber"),
    ("pymupdf", "PyMuPDF>=1.28,<2"),
    ("rapidocr_onnxruntime", "rapidocr-onnxruntime>=1.4,<2"),
    ("esptool", "esptool>=4.8,<6"),
    ("PIL", "Pillow>=10"),
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
import portalocker
import pymupdf
import pywintypes
import requests
import serial
import win32con
import win32gui
import win32print
from PIL import Image, ImageWin
from pygrabber.dshow_graph import FilterGraph
from send2trash import send2trash
from serial.tools import list_ports


BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "Settings.json"
DESKTOP_DIR = Path.home() / "Desktop"

DISPLAY_BAUDRATE = 115200
DISPLAY_TIMEOUT = 0.1
DISPLAY_WRITE_TIMEOUT = 5.0
HANDSHAKE_TIMEOUT = 1.0
DISPLAY_BOOT_DELAY = 1.5
ESP_ROM_PREFIX = "ESP-ROM:"
PORT_SCAN_INTERVAL = 1.5
CAMERA_SCAN_INTERVAL = 1.5
CAMERA_STABLE_POLLS = 2
PRINTER_SCAN_INTERVAL = 1.5
PRINTER_STABLE_POLLS = 2
SCAN_INTERVAL = 0.2
PHOTO_DELAY = 2.5
B2B_INDEX_FILENAME = "B2B_Order_Index.txt"
RETAIL_ORDER_CACHE_SUFFIX = " (Order).json"
OTHER_SLOT_COUNT = 4
DEFAULT_OTHER_COLOUR = "#ffffff"
LABEL_OCR_LOCK_FILE = BASE_DIR / ".pdf_label_ocr.lock"

CURRENT_VERSION = "2.8"

VERSION_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/version.txt"

PYTHON_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/AutoChecker.py"

PDF_LABEL_OCR_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/pdf_label_ocr.py"

DISPLAY_VERSION_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
    "main/Display_version.txt"
)
DISPLAY_BUILD_URL = (
    "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/"
    "main/build/esp32.esp32.esp32s3"
)
DISPLAY_APP_FILENAME = "Display.ino.bin"
DISPLAY_PARTITIONS_FILENAME = "Display.ino.partitions.bin"
DISPLAY_FLASH_BAUDRATE = 460800
DISPLAY_UPDATE_TIMEOUT = 180
DISPLAY_RESTART_TIMEOUT = 12.0
RETAIL_UP_DEPARTMENT = "RETAIL_UP"
RETAIL_UP_PRINTER_DPI = 200
RETAIL_UP_PAPER_WIDTH_TENTHS_MM = 1016
RETAIL_UP_PAPER_HEIGHT_TENTHS_MM = 1524
RETAIL_UP_PRINT_SCALE = 1.0
EXPECTED_DISPLAY_APP_SLOTS = (
    (0x10000, 0x140000, "app0"),
    (0x150000, 0x140000, "app1"),
)
DISPLAY_HANDSHAKE_PATTERN = re.compile(
    r"Display:([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5});([^;\s]+)",
    re.IGNORECASE,
)

# The cursor makes repeated `previous=True` calls move backwards through all
# PDF parts, including parts from previous days.
PDF_HISTORY_CURSOR: Path | None = None
OTHER_CACHE_SIGNATURE: tuple[int, int] | None = None
OTHER_CACHE_RULES: list[tuple[str, str]] = []
DISPLAY_FIRMWARE_CACHE: tuple[bytes, bytes] | None = None


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
    """Download and install one validated AutoChecker/OCR code bundle."""
    try:
        current_file = Path(__file__).resolve()
        update_files = (
            (current_file, PYTHON_URL),
            (BASE_DIR / "pdf_label_ocr.py", PDF_LABEL_OCR_URL),
        )
        downloaded_files: list[tuple[Path, str]] = []
        for target_file, url in update_files:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                print(f"XXX Cannot download update file: {target_file.name}")
                return
            downloaded_code = response.text
            # Validate the complete bundle before writing either temporary
            # file, so incompatible partial downloads are never installed.
            compile(downloaded_code, url, "exec")
            downloaded_files.append((target_file, downloaded_code))

        temp_files = []
        for target_file, downloaded_code in downloaded_files:
            temp_file = Path(str(target_file) + ".new")
            temp_file.write_text(downloaded_code, encoding="utf-8")
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


def load_label_ocr_processor():
    """Import the OCR module lazily, after the launcher update check."""
    from pdf_label_ocr import process_pending_label_pdfs

    return process_pending_label_pdfs


def get_latest_display_version() -> str | None:
    """Return the published display firmware version, or None when offline."""
    try:
        response = requests.get(DISPLAY_VERSION_URL, timeout=5)
        response.raise_for_status()
        version = response.text.strip()
        if _version_key(version) is None:
            print(f"XXX Invalid display update version: {version!r}")
            return None
        print(f"* Latest display firmware version: {version}")
        return version
    except requests.RequestException as error:
        print(f"XXX Display update check failed: {error}")
        return None


def normalise_display_mac(value: str) -> str | None:
    """Return a stable uppercase colon-separated ESP32 MAC address."""
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != 12:
        return None
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2)).upper()


def _download_display_firmware() -> tuple[bytes, bytes]:
    """Download and validate the application image and its partition table once."""
    global DISPLAY_FIRMWARE_CACHE
    if DISPLAY_FIRMWARE_CACHE is not None:
        return DISPLAY_FIRMWARE_CACHE

    downloaded: list[bytes] = []
    for filename in (DISPLAY_APP_FILENAME, DISPLAY_PARTITIONS_FILENAME):
        response = requests.get(f"{DISPLAY_BUILD_URL}/{filename}", timeout=30)
        response.raise_for_status()
        if not response.content:
            raise RuntimeError(f"Downloaded firmware file is empty: {filename}")
        downloaded.append(response.content)

    application, partitions = downloaded
    if application[0] != 0xE9:
        raise RuntimeError("Display.ino.bin is not an ESP application image")
    if len(partitions) < 32 or partitions[:2] != b"\xAA\x50":
        raise RuntimeError("Display partition table has an invalid format")

    DISPLAY_FIRMWARE_CACHE = application, partitions
    return DISPLAY_FIRMWARE_CACHE


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


def update_display_firmware(port: str, display_mac: str,
                            latest_version: str) -> bool:
    """Flash the application into every app slot without erasing NVS."""
    try:
        application, partition_data = _download_display_firmware()
        app_slots = _application_partitions(partition_data)
        if not app_slots:
            raise RuntimeError("No application slots found in partition table")
        if tuple(app_slots) != EXPECTED_DISPLAY_APP_SLOTS:
            raise RuntimeError(
                "Display partition layout changed; perform one manual full flash "
                "before enabling automatic updates"
            )
        oversized = [label for _, size, label in app_slots if len(application) > size]
        if oversized:
            raise RuntimeError(
                "Display firmware does not fit app slot(s): " + ", ".join(oversized)
            )

        with tempfile.TemporaryDirectory(prefix="AutoChecker-display-") as temp_dir:
            firmware_path = Path(temp_dir) / DISPLAY_APP_FILENAME
            firmware_path.write_bytes(application)
            command = [
                sys.executable,
                "-m",
                "esptool",
                "--chip",
                "esp32s3",
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
                f"* Updating display {display_mac} on {port} "
                f"to firmware {latest_version}"
            )
            subprocess.run(command, check=True, timeout=DISPLAY_UPDATE_TIMEOUT)
        print(f"* Display {display_mac} firmware update completed")
        return True
    except (requests.RequestException, OSError, subprocess.SubprocessError,
            RuntimeError) as error:
        print(f"XXX Display {display_mac} firmware update failed: {error}")
        return False


def run_pending_label_ocr(department: str) -> None:
    """Create missing label JSON files in an isolated, serialized process."""
    try:
        process_pending_label_pdfs = load_label_ocr_processor()
        # Launcher startup and NextPDF can request OCR at nearly the same time.
        # Serialize them so a label PDF is never recognized twice concurrently.
        with portalocker.Lock(
            str(LABEL_OCR_LOCK_FILE),
            mode="a+",
            encoding="utf-8",
            timeout=3600,
        ):
            created_json_files = process_pending_label_pdfs(department)
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


def start_label_ocr_process(department: str) -> multiprocessing.Process:
    """Start non-blocking label OCR and return its short-lived process."""
    process = multiprocessing.Process(
        target=run_pending_label_ocr,
        args=(department,),
        name=f"AutoChecker-label-OCR-{department.upper()}",
        daemon=True,
    )
    process.start()
    return process


def stop_label_ocr_process(process: multiprocessing.Process | None) -> None:
    """Reap a completed OCR process, or stop it when its owner is exiting."""
    if process is None:
        return
    process.join(timeout=0.2)
    if process.is_alive():
        process.terminate()
        process.join()


@dataclass(frozen=True)
class CheckerIdentity:
    name: str
    department: str
    camera_number: int | None
    printer_name: str | None = None


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
    """Persist the last name, department, camera and printer selection."""
    with portalocker.Lock(str(SETTINGS_FILE), mode="r+", encoding="utf-8", timeout=30) as file:
        file.seek(0)
        settings = json.load(file)
        settings.setdefault("DISPLAY", {})[display_mac] = [
            identity.name,
            identity.department,
            identity.camera_number if identity.camera_number is not None else "",
            identity.printer_name or "",
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
    settings = load_settings()
    focus = settings.setdefault("FOCUS", {})
    display_settings = settings.setdefault("DISPLAY", {})
    _, changed = ensure_other_slots(settings)

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
            values = ["", "", "", ""]
        else:
            values = (values + ["", "", "", ""])[:4]
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
            display_settings[display_mac] = ["", "", "", ""]
            changed = True

    if changed:
        save_settings(settings)
    return settings


def _probe_autochecker_display(port: str) -> tuple[str, str] | None:
    """Ask one COM device for its MAC and firmware version."""
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

        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        answers: list[str] = []
        while time.monotonic() < deadline:
            answer = display.readline().decode("utf-8", errors="ignore").strip()
            if not answer:
                continue
            answers.append(answer)
            match = DISPLAY_HANDSHAKE_PATTERN.fullmatch(answer)
            if match:
                display_mac = normalise_display_mac(match.group(1))
                if display_mac is None:
                    break
                version = match.group(2).strip()
                if _version_key(version) is None:
                    print(f"XXX {port} returned invalid firmware version: {version!r}")
                    return None
                print(
                    f"* Display found: {display_mac} ({port}), firmware {version}"
                )
                return display_mac, version

            legacy = re.fullmatch(r"AutoChecker\s+(\d+)", answer, re.IGNORECASE)
            if legacy:
                print(
                    f"XXX Display on {port} uses the old AutoChecker "
                    f"{legacy.group(1)} handshake; flash firmware that replies "
                    "Display:{MAC};{version}"
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


def identify_autochecker_display(port: str,
                                 latest_version: str | None) -> str | None:
    """Identify a display, update old firmware, and return its stable MAC."""
    identity = _probe_autochecker_display(port)
    if identity is None:
        return None

    display_mac, installed_version = identity
    installed_key = _version_key(installed_version)
    latest_key = _version_key(latest_version) if latest_version is not None else None
    if (
        installed_key is not None
        and latest_key is not None
        and latest_key > installed_key
    ):
        if update_display_firmware(port, display_mac, latest_version):
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
            updated_mac, updated_version = updated_identity
            if updated_mac != display_mac:
                print(
                    f"XXX Display MAC changed after update: "
                    f"{display_mac} -> {updated_mac}"
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


def find_autochecker_displays(latest_version: str | None) -> dict[str, str]:
    """Return {display MAC: COM port} from the <WHO?> handshake."""
    displays: dict[str, str] = {}
    for port_info in list_ports.comports():
        display_mac = identify_autochecker_display(
            port_info.device,
            latest_version,
        )
        if display_mac is not None:
            displays[display_mac] = port_info.device
    return displays


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
    """Return installed Windows printer queues that are not marked offline."""
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
            status = int(printer_info.get("Status", 0) or 0)
            attributes = int(printer_info.get("Attributes", 0) or 0)
            if status & offline_status or attributes & work_offline:
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
) -> None:
    """Upload menu options, placing the display's last selection first."""
    last_values = settings.get("DISPLAY", {}).get(
        display_mac,
        ["", "", "", ""],
    )
    last_values = (list(last_values) + ["", "", "", ""])[:4]
    names = _prioritise(list(settings.get("NAME", [])), last_values[0])
    departments = _prioritise(list(settings.get("DEPARTMENT", [])), last_values[1])
    camera_numbers = _prioritise(sorted(cameras), last_values[2])
    printer_names = _prioritise(list(printers), last_values[3])

    fields = [
        ",".join(map(str, names)),
        ",".join(map(str, departments)),
        ",".join(map(str, camera_numbers)),
        ",".join(printer_names),
        "*ORDER*",
    ]
    for setting_name, setting_value in settings.items():
        if setting_name in {"DEPARTMENT", "DISPLAY", "OTHER", "PRINTER"}:
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


def refresh_idle_display_data(
    settings: dict,
    cameras: dict[int, int],
    printers: list[str],
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
            )
        except (serial.SerialException, OSError) as error:
            print(f"XXX Cannot upload data to AutoChecker {display_number}: {error}")


def broadcast_display_data(
        settings: dict,
        cameras: dict[int, int],
        printers: list[str],
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
    refresh_idle_display_data(
        settings,
        available_cameras,
        available_printers,
        idle_displays,
    )

    for display_number, update_queue in worker_update_queues.items():
        try:
            update_queue.put_nowait((
                "uploadData",
                dict(available_cameras),
                list(available_printers),
            ))
        except (OSError, ValueError, queue.Full) as error:
            print(f"XXX Cannot notify AutoChecker {display_number}: {error}")


def parse_command(line: str) -> tuple[str, CheckerIdentity] | None:
    """Parse one Start or Stop line; reject malformed data."""
    match = re.fullmatch(
        r"(Start|Stop):([^;\r\n]+);([^;\r\n]+);(\d*)"
        r"(?:;([^;\r\n]*))?",
        line.strip(),
    )
    if not match:
        return None

    action, name, department, camera_number, printer_name = match.groups()
    identity = CheckerIdentity(
        name.strip(),
        department.strip(),
        int(camera_number) if camera_number else None,
        printer_name.strip() if printer_name and printer_name.strip() else None,
    )

    if not identity.name or not identity.department:
        return None

    return action, identity


def stop_matches_identity(
    stop_identity: CheckerIdentity,
    running_identity: CheckerIdentity,
) -> bool:
    """Match legacy three-field Stop commands and new four-field commands."""
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
    )


def department_paths(department: str) -> tuple[Path, Path]:
    """Create and return Desktop/<department>/Photo and /Statistik safely."""
    # department is a directory name received over serial; do not permit paths.
    if Path(department).name != department or department in {".", ".."}:
        raise ValueError(f"Invalid department name: {department!r}")

    department_dir = DESKTOP_DIR / department
    photo_dir = department_dir / "Photo"
    statistics_dir = department_dir / "Statistik"
    photo_dir.mkdir(parents=True, exist_ok=True)
    statistics_dir.mkdir(parents=True, exist_ok=True)
    return photo_dir, statistics_dir


def safe_worker_filename(name: str) -> str:
    """Return a Windows-safe filename while keeping the worker name readable."""
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return safe_name or "Unknown"


class WorkerPerformanceStats:
    """Collect per-worker scan timings and merge them atomically into JSON."""

    def __init__(self, name: str, department: str):
        self.name = name
        self.department = department
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
        return (
            DESKTOP_DIR
            / self.department
            / "Worker_Statistics"
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
    normalized_name = normalize(name)
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
    """Ignore only the free B2B +X bonus when comparing a scanned package."""
    return re.sub(
        r"(\s*-\s*\d+)\+\d+(\s*fem\s*)$",
        r"\1\2",
        normalize(name),
        flags=re.IGNORECASE,
    )


def sort_key(item: tuple[str, int], config: dict) -> tuple[int, str, int]:
    name, _ = item
    normalized = product_base_name(name, config)
    is_bonus = "Bonus" in name
    is_no_barcode = not has_barcode_for_item(name, config)
    fem_match = re.search(r"(\d+)\s*fem", name, re.IGNORECASE)
    fem_count = int(fem_match.group(1)) if fem_match else 0
    base_name = re.sub(r"\s*-\s*\d+\s*fem", "", normalized, flags=re.IGNORECASE).strip().lower()
    category = 0 if not is_bonus and not is_no_barcode else 1 if is_bonus else 2
    return category, base_name, fem_count


def _available_order_pdfs() -> list[tuple[datetime.date, int, Path]]:
    pattern = re.compile(r"^(\d{2}\.\d{2}\.\d{4}) Part (\d+)\.pdf$", re.IGNORECASE)
    found = []
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return found
    for path in downloads.iterdir():
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        try:
            date = datetime.datetime.strptime(match.group(1), "%d.%m.%Y").date()
            found.append((date, int(match.group(2)), path))
        except ValueError:
            continue
    return sorted(found)


def find_latest_pdf(previous: bool = False) -> tuple[Path | None, int | None]:
    """Find latest PDF, or step back one part/date when previous is requested."""
    global PDF_HISTORY_CURSOR
    files = _available_order_pdfs()
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
    """Return an exclusive, cross-process lock for a statistics file."""
    stat_file.parent.mkdir(parents=True, exist_ok=True)
    stat_file.touch(exist_ok=True)
    return portalocker.Lock(str(stat_file), mode="r+", encoding="utf-8", timeout=30)


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
) -> list[tuple[Path, Path, int]]:
    """Return today's existing (JSON, PDF, part number) label file pairs."""
    day = day or datetime.date.today()
    pattern = re.compile(
        rf"^{re.escape(day.strftime('%d.%m.%Y'))} Part (\d+) "
        r"\(Label\)\.json$",
        re.IGNORECASE,
    )
    result: list[tuple[Path, Path, int]] = []
    for directory in B2B_download_directories():
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


def prepare_RETAIL_UP_label_data() -> list[tuple[Path, Path, int]]:
    """OCR today's pending Label PDFs and return all usable JSON/PDF pairs."""
    process_pending_label_pdfs = load_label_ocr_processor()
    today = datetime.date.today()
    with portalocker.Lock(
        str(LABEL_OCR_LOCK_FILE),
        mode="a+",
        encoding="utf-8",
        timeout=3600,
    ):
        for directory in B2B_download_directories():
            process_pending_label_pdfs(
                RETAIL_UP_DEPARTMENT,
                downloads_dir=directory,
                label_date=today,
            )

    label_files = current_RETAIL_UP_label_files(today)
    if not label_files:
        raise FileNotFoundError(
            f"Label PDF/JSON for {today:%d.%m.%Y} was not found in Downloads"
        )
    return label_files


def RETAIL_UP_statistics_line(order_id: str) -> str:
    """Return one RETAIL statistics line without checking its order status."""
    normalized_order = order_id.strip()
    if not re.fullmatch(r"#\d+", normalized_order):
        raise ValueError(f"Invalid order number {order_id}")

    stat_file = (
        DESKTOP_DIR
        / "RETAIL"
        / "Statistik"
        / f"{datetime.date.today():%d.%m.%Y}.txt"
    )
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


def RETAIL_UP_ready_statistics_line(order_id: str) -> str:
    """Return one completed, non-cancelled RETAIL statistics line."""
    normalized_order = order_id.strip()
    matching_line = RETAIL_UP_statistics_line(normalized_order)

    status = retail_stat_order_suffix(matching_line)
    if "cancelled" in status.casefold():
        raise ValueError(f"Cancelled order {normalized_order}")
    if "+" not in status:
        raise ValueError(f"Order is not completed {normalized_order}")

    return matching_line


def RETAIL_UP_tracking_from_order_line(order_id: str, line: str) -> str:
    """Extract and normalize the tracking prefix from one statistics line."""
    normalized_order = order_id.strip()
    order_position = line.find(normalized_order)
    if order_position < 0:
        raise LookupError(
            f"Order {normalized_order} is missing from its statistics line"
        )
    prefix = line[:order_position].strip()
    tracking_number = prefix.split(maxsplit=1)[0] if prefix else ""
    tracking_number = re.sub(r"[^A-Za-z0-9]", "", tracking_number).upper()
    if not tracking_number:
        raise LookupError(f"Tracking number is missing for {normalized_order}")
    return tracking_number


def RETAIL_UP_tracking_from_statistics(
    order_id: str,
    *,
    require_ready: bool = True,
) -> str:
    """Return an order's tracking number, optionally requiring ready status."""
    normalized_order = order_id.strip()
    matching_line = (
        RETAIL_UP_ready_statistics_line(normalized_order)
        if require_ready
        else RETAIL_UP_statistics_line(normalized_order)
    )
    return RETAIL_UP_tracking_from_order_line(normalized_order, matching_line)


def RETAIL_UP_today_orders() -> dict[str, str]:
    """Return today's RETAIL statistics lines indexed by order number."""
    stat_file = (
        DESKTOP_DIR
        / "RETAIL"
        / "Statistik"
        / f"{datetime.date.today():%d.%m.%Y}.txt"
    )
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


def append_RETAIL_UP_print_name(order_id: str, name: str) -> None:
    """Append the printing worker name to today's RETAIL order line."""
    normalized_order = order_id.strip()
    stat_file = (
        DESKTOP_DIR
        / "RETAIL"
        / "Statistik"
        / f"{datetime.date.today():%d.%m.%Y}.txt"
    )
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        for index, line in enumerate(lines):
            if retail_order_id_from_stat_line(line) != normalized_order:
                continue
            lines[index] = line.rstrip("\r\n") + f" {name}\n"
            file.seek(0)
            file.truncate()
            file.writelines(lines)
            file.flush()
            return
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
        status = retail_stat_order_suffix(line)
        printed_name = re.search(r"(?:\(\+\)|\+)\s+(.+?)\s*$", status)
        if (
            "cancelled" not in status.casefold()
            and printed_name is not None
            and printed_name.group(1).strip()
        ):
            completed += 1
    return completed, total - completed, total


def RETAIL_UP_shipping_progress() -> tuple[int, int, int]:
    """Return completed-for-shipping, left and all RETAIL orders for today."""
    stat_file = (
        DESKTOP_DIR
        / "RETAIL"
        / "Statistik"
        / f"{datetime.date.today():%d.%m.%Y}.txt"
    )
    if not stat_file.is_file():
        return 0, 0, 0
    return RETAIL_UP_shipping_progress_from_lines(read_stat_lines(stat_file))


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


def RETAIL_UP_check_lines(order_id: str, settings: dict) -> list[str]:
    """Return quantity/name lines whose products match configured CHECK text."""
    fragments = RETAIL_check_fragments(settings)
    if not fragments:
        return []
    fragment_keys = [fragment.casefold() for fragment in fragments]
    today = datetime.date.today()
    for pdf_date, _part, pdf_path in reversed(_available_order_pdfs()):
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
        return [
            f"{quantity} {name}"
            for name, quantity in matched_order
            if any(fragment in name.casefold() for fragment in fragment_keys)
        ]
    Printer(f"? Order data for CHECK not found: {order_id}")
    return []


def normalize_RETAIL_UP_tracking_number(tracking_number: str) -> str:
    """Normalize a tracking value for statistics/Label JSON comparisons."""
    return re.sub(r"[^A-Za-z0-9]", "", str(tracking_number)).upper()


def RETAIL_UP_label_tracking_sequence() -> list[str]:
    """Return today's Label tracking numbers in PDF part/page order."""
    sequence = []
    label_files = sorted(
        current_RETAIL_UP_label_files(),
        key=lambda item: item[2],
    )
    if not label_files:
        raise FileNotFoundError("Today's Label PDF/JSON was not found")

    for json_path, _pdf_path, _part_number in label_files:
        with json_path.open("r", encoding="utf-8") as file:
            document = json.load(file)
        if not isinstance(document, dict):
            raise ValueError(f"Invalid Label JSON document: {json_path.name}")

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
    tracking_number: str,
) -> tuple[Path, int, str]:
    """Find a tracking number and its label type in today's Label JSON."""
    expected = normalize_RETAIL_UP_tracking_number(tracking_number)
    for json_path, pdf_path, _part_number in current_RETAIL_UP_label_files():
        with json_path.open("r", encoding="utf-8") as file:
            document = json.load(file)
        if not isinstance(document, dict):
            continue
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
    """Validate one order, optionally print it, and record the worker name."""
    printer_name = str(worker.get("PRINTER", "")).strip()
    if printer_name:
        tracking_number = RETAIL_UP_tracking_from_statistics(order_id)
        pdf_path, page_number, label_type = find_RETAIL_UP_label_page(
            tracking_number
        )
        print_pdf_page(pdf_path, page_number, printer_name, label_type)
        status_message = (
            f"+ Label printed {order_id.lstrip('#')} page {page_number}"
        )
        physically_printed = True
    else:
        RETAIL_UP_ready_statistics_line(order_id)
        status_message = (
            f"+ Order {order_id.lstrip('#')} recorded without printing"
        )
        physically_printed = False
    recorded_name = worker["NAME"]
    if physically_printed and parenthesize_print_name:
        recorded_name = f"({recorded_name})"
    append_RETAIL_UP_print_name(order_id, recorded_name)
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
        check_lines = RETAIL_UP_check_lines(order_id, settings)
        success = True
    except Exception as error:
        status_message = f"XXX {error}"
        success = False
    completed, left, total = RETAIL_UP_shipping_progress()
    message = "\n".join((
        f"* Completed: {completed}, Left: {left}, All: {total}",
        status_message,
        *check_lines,
    ))
    Printer(message)
    send(worker["DISPLAY"], message)
    return success


def process_RETAIL_UP_label_range(
    worker: dict,
    settings: dict,
    first_order: str,
    last_order: str,
) -> int:
    """Print an inclusive range in Label JSON part/page order."""
    try:
        orders = RETAIL_UP_today_orders()
        first_tracking = normalize_RETAIL_UP_tracking_number(
            RETAIL_UP_tracking_from_statistics(
                first_order,
                require_ready=False,
            )
        )
        last_tracking = normalize_RETAIL_UP_tracking_number(
            RETAIL_UP_tracking_from_statistics(
                last_order,
                require_ready=False,
            )
        )

        label_sequence = RETAIL_UP_label_tracking_sequence()
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
            status = retail_stat_order_suffix(orders[order_id])
            if "cancelled" in status.casefold():
                status_lines.append(f"XXX {order_id} is Cancelled")
                continue
            if "+" not in status:
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

    completed, left, total = RETAIL_UP_shipping_progress()
    result_line = (
        f"+ Labels printed: {printed_count}"
        if worker.get("PRINTER")
        else f"+ Orders recorded: {processed_count}"
    )
    message = "\n".join((
        f"* Completed: {completed}, Left: {left}, All: {total}",
        result_line,
        *status_lines,
    ))
    Printer(message)
    send(worker["DISPLAY"], message)
    return processed_count


def find_downloaded_B2B_pdf(
    filename: str,
    download_directories: list[Path] | None = None,
) -> tuple[Path | None, list[Path]]:
    """Find a B2B PDF in the configured Windows Downloads locations."""
    directories = download_directories or B2B_download_directories()
    for directory in directories:
        found = _case_insensitive_file(directory, filename)
        if found is not None:
            return found, directories
    return None, directories


def prepare_B2B_order_history(
    worker: dict,
    order_id: str,
) -> tuple[Path | None, str | None]:
    """Archive a downloaded B2B PDF or reopen it from its History folder."""
    order_name = B2B_order_name(order_id)
    if order_name is None:
        return None, "invalid_order"

    history_dir = worker.get(
        "B2B_HISTORY", worker["STATISTICS"].parent / "History"
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    order_dir = history_dir / order_name
    pdf_filename = f"{order_name}.pdf"

    # Two displays may scan the same newly downloaded order nearly
    # simultaneously. Keep folder creation and moving under one lock.
    history_lock = history_dir / ".history.lock"
    with portalocker.Lock(
        str(history_lock), mode="a+", encoding="utf-8", timeout=30
    ):
        downloaded_pdf, checked_downloads = find_downloaded_B2B_pdf(
            pdf_filename,
            worker.get("DOWNLOAD_DIRECTORIES"),
        )
        if downloaded_pdf is not None:
            order_dir.mkdir(parents=True, exist_ok=True)
            archived_pdf = order_dir / pdf_filename
            existing_pdf = _case_insensitive_file(order_dir, pdf_filename)
            if existing_pdf is not None:
                return None, "archive_conflict"
            try:
                shutil.move(str(downloaded_pdf), str(archived_pdf))
            except (OSError, shutil.Error):
                return None, "move_failed"
        else:
            print(
                f"? B2B PDF {pdf_filename} was not found; checked: "
                + ", ".join(str(path) for path in checked_downloads)
            )
            if not order_dir.is_dir():
                return None, "unknown_order"
            archived_pdf = _case_insensitive_file(order_dir, pdf_filename)
            if archived_pdf is None:
                return None, "unknown_order"

        photo_dir = order_dir / "Photo"
        photo_dir.mkdir(parents=True, exist_ok=True)

    progress_file = order_dir / f"{order_name}_Sborka.txt"
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
) -> list[str]:
    """Build aligned status/name columns for one B2B order."""
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
            rows.append((f"{extra_quantity} !!! EXTRA", "", names, True))

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
    section.append("\n")
    return section


def load_B2B_progress(worker: dict, items: list[tuple[str, int]], counts: defaultdict,
                      extras: defaultdict,
                      no_barcode_items: list[tuple[str, int]]) -> None:
    """Restore counts and contributor names, creating the section if needed."""
    stat_file = worker["ACTIVE_STAT_FILE"]
    header = f"----------{worker['B2B_PDF_NAME']}----------"
    contributors: dict[str, list[str]] = {}
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
                    header, items, counts, extras, no_barcode_items, contributors
                )
            )
            file.flush()
        else:
            start, end = bounds
            item_quantities = dict(items)
            match_names = worker.get("B2B_MATCH_NAMES", {})
            last_item_name = None
            for line in lines[start + 1:end]:
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


def save_B2B_progress(worker: dict, items: list[tuple[str, int]], counts: defaultdict,
                      extras: defaultdict,
                      no_barcode_items: list[tuple[str, int]], changed_item: str | None = None) -> None:
    """Atomically rewrite one B2B order section with current position progress."""
    contributors = worker.setdefault("B2B_POSITION_NAMES", {})
    if changed_item is not None:
        names = contributors.setdefault(changed_item, [])
        if worker["NAME"] not in names:
            names.append(worker["NAME"])

    stat_file = worker["ACTIVE_STAT_FILE"]
    header = f"----------{worker['B2B_PDF_NAME']}----------"
    section = _format_B2B_progress_section(
        header, items, counts, extras, no_barcode_items, contributors
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


def retail_stat_order_suffix(text: str) -> str:
    """Return only assembler/status text following the RETAIL order number."""
    match = re.search(r"(?<!\S)#\d+(?=\s|$)", text)
    return text[match.end():] if match else ""


def retail_stat_metadata_values(metadata: dict) -> tuple[str, str]:
    tracking_number = re.sub(
        r"\s+", " ", str(metadata.get("tracking_number", ""))
    ).strip()
    customer_name = re.sub(
        r"\s+", " ", str(metadata.get("customer_name", ""))
    ).strip()
    return tracking_number, customer_name


def retail_stat_order_prefix(order_id: str, metadata: dict, *,
                             tracking_width: int | None = None,
                             customer_width: int | None = None) -> str:
    tracking_number, customer_name = retail_stat_metadata_values(metadata)
    tracking_width = (
        len(tracking_number) if tracking_width is None else tracking_width
    )
    customer_width = (
        len(customer_name) if customer_width is None else customer_width
    )
    return (
        f"{tracking_number:<{tracking_width}}   "
        f"{customer_name:<{customer_width}}   {order_id}"
    )


def save_part_to_statistics(worker: dict, pdf_path: Path, parsed_orders: dict) -> None:
    stat_file = statistics_file(worker)
    header = f"----------{pdf_path.name}----------"
    metadata_by_order = worker.get("RETAIL_ORDER_META", {})
    metadata_values = {
        order_id: retail_stat_metadata_values(
            metadata_by_order.get(order_id, {})
        )
        for order_id in parsed_orders
    }
    tracking_width = max(
        (len(values[0]) for values in metadata_values.values()),
        default=0,
    )
    customer_width = max(
        (len(values[1]) for values in metadata_values.values()),
        default=0,
    )
    # Keep the lock across the complete read-modify-write operation.
    with _locked_statistics_file(stat_file) as file:
        file.seek(0)
        lines = file.readlines()
        header_index = next(
            (index for index, line in enumerate(lines) if line.strip() == header),
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
                    tracking_width=tracking_width,
                    customer_width=customer_width,
                ) + "\n"
                for order_id in parsed_orders
            )
            lines.append("\n")
        else:
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
            for order_id in parsed_orders:
                prefix = retail_stat_order_prefix(
                    order_id,
                    metadata_by_order.get(order_id, {}),
                    tracking_width=tracking_width,
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

        file.seek(0)
        file.truncate()
        file.writelines(lines)
        file.flush()


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
        if not {"TrackNumber", "CustomerName", "Stealth", "Order"} <= entry.keys():
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
        }
    return parsed, metadata_by_order


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
        document = RETAIL_order_cache_document(parsed, metadata_by_order)
        file.seek(0)
        file.truncate()
        json.dump(document, file, ensure_ascii=False, indent=4)
        file.write("\n")
        file.flush()
        Printer(f"* Order JSON created | {cache_path.name}")
        return parsed, metadata_by_order


def parse_orders_RETAIL(worker: dict, config: dict, previous: bool = False) -> dict:
    pdf_path, _ = find_latest_pdf(previous)
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
    save_part_to_statistics(worker, pdf_path, parsed)
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
        if not match:
            message = f"XXX Cannot parse B2B position: {line}"
            Printer(message)
            send(worker["DISPLAY"], message)
            return {}
        name, package_size, quantity = match.groups()
        display_name = f"{name} - {package_size} fem"
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
                       match_names: dict[str, str] | None = None) -> tuple[str | None, int | None]:
    match_names = match_names or {}
    for item_name, quantity in order_items:
        comparison_name = match_names.get(item_name, item_name)
        if scan_match_name(comparison_name) == scan_match_name(scanned_name):
            return item_name, quantity
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
        status_suffix = retail_stat_order_suffix(text)
        cancelled = "Cancelled" in status_suffix
        completed = "+" in status_suffix
        if cancelled and completed:
            return "completed_cancelled"
        if cancelled:
            return "cancelled"
        if completed:
            return "completed"
        return "active"
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
                retail_stat_order_suffix(raw_text)
                if department == "RETAIL" else text
            )
            if matches_order and "Cancelled" not in status_suffix:
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
        return updated


def cancel_order(worker: dict, order_id: str) -> bool:
    """Mark a RETAIL daily order or B2B index entry as Cancelled."""
    department = worker.get("DEPARTMENT", "").upper()
    if department == "B2B":
        target = B2B_index_file(worker)
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
            if "Cancelled" not in status_text:
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
        return changed


def cancel_order_from_launcher(display_mac: str, order_id: str, settings: dict) -> bool:
    """Cancel an order while its display is idle, preferring its last department."""
    last_values = settings.get("DISPLAY", {}).get(display_mac, ["", "", ""])
    preferred_department = (
        str(last_values[1]).upper()
        if isinstance(last_values, (list, tuple)) and len(last_values) > 1
        else ""
    )
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


def save_photo(worker: dict, order_id: str | None, frame, *, manually: bool = False) -> bool:
    if not worker.get("HAS_CAMERA", True):
        message = "XXX Process without camera"
        Printer(message)
        send(worker["DISPLAY"], message)
        return False
    if frame is None:
        message = "XXX Camera frame unavailable"
        Printer(message)
        send(worker["DISPLAY"], message)
        return False
    if not order_id:
        message = "XXX Order not selected"
        Printer(message)
        send(worker["DISPLAY"], message)
        return False
    number = 1
    while True:
        suffix = "" if number == 1 else f"_{number}"
        path = worker["FOTO"] / f"{order_id}{suffix}.jpg"
        if not path.exists():
            break
        number += 1
    if not cv2.imwrite(str(path), frame):
        message = f"XXX Cannot save photo {path.name}"
        saved = False
    else:
        display_filename = path.name.lstrip("#")
        message = f"* {display_filename} Photo taken manually" if manually else f"* {display_filename} Photo saved"
        update_order_in_statistics(worker, order_id, add_plus=not manually, manual_plus=manually)
        saved = True
    Printer(message)
    send(worker["DISPLAY"], message)
    return saved


def process_scan(worker: dict, config: dict, code: str, orders: dict, current_order: str | None,
                 counts: defaultdict, extras: defaultdict, errors: set,
                 no_barcode_items: list[tuple[str, int]], photo_pending: bool,
                 photo_start: float) -> tuple[str | None, bool, float]:
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
            status = get_order_status(worker, matched)
            if status != "active" and status != "not_found":
                if performance_stats is not None:
                    performance_stats.record_scan()
                    performance_stats.record_mistake()
                message = f"XXX Order unavailable {matched}: {status}"
                Printer(message)
                send_fn(message)
                return current_order, photo_pending, photo_start
        if performance_stats is not None:
            performance_stats.record_scan(matched)
        current_order = matched
        counts.clear(); extras.clear(); errors.clear(); no_barcode_items.clear()
        for item, quantity in orders[current_order]:
            has_barcode = (
                item in B2B_match_names if is_B2B
                else has_barcode_for_item(item, config)
            )
            if not has_barcode and "Bonus" not in item:
                if quantity == 1 and item in config["AUTO_COMPLETE_ITEMS"]:
                    counts[item] = 1
                else:
                    no_barcode_items.append((item, quantity))
        if worker.get("DEPARTMENT", "").upper() == "B2B":
            load_B2B_progress(
                worker, orders[current_order], counts, extras, no_barcode_items
            )
            save_B2B_progress(
                worker, orders[current_order], counts, extras, no_barcode_items
            )
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
        "Bonus" in name
        or (name not in B2B_match_names if is_B2B else not has_barcode_for_item(name, config))
        or counts.get(name, 0) >= quantity
        for name, quantity in orders[current_order]
    )

    if regular_ready:
        if code in config["FEM_BONUS"]:
            product_name = "FEM | Bonus Fem seeds - 1 fem"
        elif code in config["AUTO_BONUS"]:
            product_name = "AUTO | Bonus Auto seeds - 1 fem"
        elif code in config["OTHER_BONUS"]:
            product_name = config["OTHER_BONUS"][code]

    item_name, quantity = (
        find_original_item(orders[current_order], product_name, B2B_match_names)
        if product_name else (None, None)
    )
    changed_item = None
    if item_name is None or item_name not in order_items:
        if performance_stats is not None:
            performance_stats.record_mistake()
        errors.add(product_name or f"Unknown barcode {code}")
        photo_pending = False
    else:
        counts[item_name] += 1
        changed_item = item_name
        if counts[item_name] > quantity:
            if performance_stats is not None:
                performance_stats.record_mistake()
            extras[item_name] = counts[item_name] - quantity
            photo_pending = False

    if worker.get("DEPARTMENT", "").upper() == "B2B" and changed_item is not None:
        save_B2B_progress(
            worker, orders[current_order], counts, extras, no_barcode_items, changed_item
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


def draw_RETAIL_order_counters(frame, completed: int, total: int) -> None:
    """Draw completed, remaining and total order counts on a camera preview."""
    height, width = frame.shape[:2]
    remaining = max(0, total - completed)
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


def RETAIL_daily_order_counts(worker: dict) -> tuple[int, int]:
    """Count completed and total orders in today's RETAIL statistics file."""
    stat_file = statistics_file(worker)
    try:
        stat = stat_file.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        worker.pop("RETAIL_ORDER_COUNTER_CACHE", None)
        return 0, 0

    cached = worker.get("RETAIL_ORDER_COUNTER_CACHE")
    if cached and cached[0] == signature:
        return cached[1], cached[2]

    completed = 0
    total = 0
    for line in read_stat_lines(stat_file):
        if retail_order_id_from_stat_line(line) is None:
            continue
        total += 1
        if "+" in line:
            completed += 1

    worker["RETAIL_ORDER_COUNTER_CACHE"] = (
        signature,
        completed,
        total,
    )
    return completed, total
































def main(identity: CheckerIdentity, camera_id: int | None, focus: int | float | None,
         display_port: str, display_number: str, cameras: dict[int, int],
         printers: list[str],
         settings_update_requests: multiprocessing.Queue,
         display_update_queue: multiprocessing.Queue,
         display_reset_event: multiprocessing.Event,
         display_disconnect_event: multiprocessing.Event) -> None:
    """Run one checker and own its COM port until its matching Stop command."""
    cap = None
    display = None
    performance_stats = None
    label_ocr_process = None
    label_ocr_rerun_requested = False

    try:
        photo_dir, statistics_dir = department_paths(identity.department)
        print(f"* Photo folder: {photo_dir}")
        print(f"* Statistics folder: {statistics_dir}")

        display = serial.Serial(
            display_port,
            baudrate=DISPLAY_BAUDRATE,
            timeout=DISPLAY_TIMEOUT,
            write_timeout=DISPLAY_WRITE_TIMEOUT,
        )

        if camera_id is not None:
            cap = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open camera ID {camera_id}")

            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("M", "J", "P", "G"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 4656)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 3496)
            if focus is not None:
                cap.set(cv2.CAP_PROP_FOCUS, focus)

        config = load_settings()
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
        if (
            identity.department.upper() == RETAIL_UP_DEPARTMENT
            and identity.printer_name
        ):
            message = "* Label OCR started"
            Printer(message)
            send(display, message)
            try:
                label_files = prepare_RETAIL_UP_label_data()
                message = f"* Label data ready: {len(label_files)} file(s)"
            except Exception as error:
                message = f"XXX {error}"
            Printer(message)
            send(display, message)

        if identity.department.upper() == "B2B":
            B2B_history = statistics_dir.parent / "History"
            B2B_history.mkdir(parents=True, exist_ok=True)
            worker["B2B_HISTORY"] = B2B_history
        # RETAIL uses the PDF order list. B2B will load its order PDF when it
        # receives Scan:<pdf filename>; it never parses a retail PDF here.
        orders = parse_orders_RETAIL(worker, config) if identity.department.upper() == "RETAIL" else {}
        current_order = None
        counts = defaultdict(int)
        extras = defaultdict(int)
        errors = set()
        no_barcode_items: list[tuple[str, int]] = []
        photo_pending = False
        photo_start = 0.0

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
            if label_ocr_process is not None and not label_ocr_process.is_alive():
                label_ocr_process.join()
                label_ocr_process = None
                if label_ocr_rerun_requested:
                    label_ocr_rerun_requested = False
                    label_ocr_process = start_label_ocr_process(
                        identity.department
                    )

            display_update_requested = False
            updated_cameras = None
            updated_printers = None
            camera_stop_reason = None
            while True:
                try:
                    update_message = display_update_queue.get_nowait()
                    if (
                        isinstance(update_message, tuple)
                        and len(update_message) in {2, 3}
                    ):
                        if (
                            update_message[0] == "uploadData"
                            and isinstance(update_message[1], dict)
                        ):
                            display_update_requested = True
                            updated_cameras = update_message[1]
                            if (
                                len(update_message) == 3
                                and isinstance(update_message[2], list)
                            ):
                                updated_printers = update_message[2]
                        elif update_message[0] == "stop":
                            camera_stop_reason = str(update_message[1])
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
                config = load_settings()
                try:
                    upload_display_data(
                        display,
                        display_number,
                        config,
                        cameras,
                        printers,
                    )
                except serial.SerialTimeoutException as error:
                    # A settings refresh must not stop order processing merely
                    # because the display consumed a large packet too slowly.
                    print(
                        f"XXX AutoChecker {display_number} uploadData timeout: {error}"
                    )

            ok = False
            frame = None
            if cap is not None:
                ok, frame = cap.read()
            if cap is not None and ok:
                preview = cv2.resize(frame, None, fx=0.15, fy=0.15)
                if identity.department.upper() == "RETAIL":
                    completed_orders, total_orders = RETAIL_daily_order_counts(worker)
                    draw_RETAIL_order_counters(
                        preview,
                        completed_orders,
                        total_orders,
                    )
                cv2.imshow(window_name, preview)

            line = display.readline().decode("utf-8", errors="ignore").strip()
            if line.startswith(ESP_ROM_PREFIX):
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
                            and label_ocr_process.is_alive()
                        ):
                            # Coalesce repeated requests into one follow-up run.
                            label_ocr_rerun_requested = True
                        else:
                            if label_ocr_process is not None:
                                label_ocr_process.join()
                            label_ocr_process = start_label_ocr_process(
                                identity.department
                            )
                    loaded_orders = parse_orders_RETAIL(
                        worker, config, previous=(line == "LastPDF")
                    )
                    # Keep the currently loaded list if no previous PDF exists.
                    if loaded_orders:
                        orders = loaded_orders
                        current_order = None
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
                            process_RETAIL_UP_scan(
                                worker,
                                config,
                                first_order,
                                parenthesize_print_name=True,
                            )
                        else:
                            process_RETAIL_UP_label_range(
                                worker,
                                config,
                                first_order,
                                last_order,
                            )

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
                        if not process_RETAIL_UP_scan(worker, config, code):
                            performance_stats.record_mistake()
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
                                if history_error == "archive_conflict":
                                    message = (
                                        f"XXX Order already exists in History: {code}"
                                    )
                                elif history_error == "move_failed":
                                    message = (
                                        f"XXX Cannot move order PDF to History: {code}"
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
                if save_photo(
                    worker,
                    current_order,
                    frame if ok else None,
                    manually=True,
                ):
                    performance_stats.record_photo(current_order)
                photo_pending = False

            if photo_pending and time.time() - photo_start >= PHOTO_DELAY:
                if save_photo(worker, current_order, frame if ok else None):
                    performance_stats.record_photo(current_order)
                photo_pending = False

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
        stop_label_ocr_process(label_ocr_process)


































def open_display(port: str) -> serial.Serial:
    return serial.Serial(port, baudrate=DISPLAY_BAUDRATE, timeout=DISPLAY_TIMEOUT)


def notify_launcher_ready(display: serial.Serial) -> None:
    """Tell the display that the launcher is ready to accept commands."""
    display.write(b"</Ready>\n")
    display.flush()


def dispatcher() -> None:
    """Own idle display ports and hand each busy port to its worker process."""
    latest_display_version = get_latest_display_version()

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
    display_ports = find_autochecker_displays(latest_display_version)
    if not display_ports:
        print("? No AutoChecker displays found; monitoring new COM ports")

    # Add the per-display saved-selection slots after their IDs are known.
    settings = ensure_detected_settings(cameras, display_ports)
    ensure_worker_statistics_files(settings)

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
    next_port_scan = time.monotonic() + PORT_SCAN_INTERVAL
    next_camera_scan = time.monotonic() + CAMERA_SCAN_INTERVAL
    pending_camera_snapshot: dict[int, int] | None = None
    pending_camera_polls = 0
    next_printer_scan = time.monotonic() + PRINTER_SCAN_INTERVAL
    pending_printer_snapshot: list[str] | None = None
    pending_printer_polls = 0

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

            if time.monotonic() >= next_port_scan:
                try:
                    current_ports = {
                        port_info.device for port_info in list_ports.comports()
                    }
                except OSError as error:
                    print(f"XXX Cannot enumerate COM ports: {error}")
                    current_ports = observed_ports

                observed_ports.intersection_update(current_ports)
                disconnected_ports_waiting_removal.intersection_update(current_ports)
                known_ports = set(display_ports.values())
                new_ports = (
                    current_ports
                    - observed_ports
                    - known_ports
                    - disconnected_ports_waiting_removal
                )
                for port in sorted(new_ports):
                    observed_ports.add(port)
                    display_number = identify_autochecker_display(
                        port,
                        latest_display_version,
                    )
                    if display_number is None:
                        continue
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
                        idle_displays,
                        worker_update_queues,
                        worker_identities,
                    )

                next_printer_scan = (
                    time.monotonic() + PRINTER_SCAN_INTERVAL
                )

            settings_changed = False
            while True:
                try:
                    settings_update_requests.get_nowait()
                    settings_changed = True
                except queue.Empty:
                    break
            if settings_changed:
                settings = load_settings()
                ensure_worker_statistics_files(settings)
                broadcast_display_data(
                    settings,
                    cameras,
                    printers,
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

                if line.startswith(ESP_ROM_PREFIX):
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
                        display_number,
                        cameras,
                        printers,
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
    dispatcher()

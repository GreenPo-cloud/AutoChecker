import sys
import subprocess
import importlib

def ensure_package(module_name, pip_name=None):

    """
    module_name = как импортируется
    pip_name = как ставится через pip
    """

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
            pip_name
        ])

        print(f"{pip_name} installed")


REQUIRED_PACKAGES = [

    ("cv2", "opencv-python"),
    ("serial", "pyserial"),
    ("pdfplumber", "pdfplumber"),
    ("send2trash", "send2trash"),
    ("numpy", "numpy"),
    ("requests", "requests"),

]

for module_name, pip_name in REQUIRED_PACKAGES:
    ensure_package(module_name, pip_name)
        
        
        
import os
import re
import glob
import time
import cv2
import serial
import datetime
import pdfplumber
from collections import defaultdict, Counter, OrderedDict
import json
import threading
from send2trash import send2trash
import datetime
import numpy as np
import requests









CURRENT_VERSION = "1.0"

VERSION_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/version.txt"

PYTHON_URL = "https://raw.githubusercontent.com/GreenPo-cloud/AutoChecker/main/AutoChecker.py"


def check_for_updates():

    try:
        response = requests.get(VERSION_URL, timeout=5)

        if response.status_code != 200:
            return

        latest_version = response.text.strip()

        if latest_version == CURRENT_VERSION:
            print("* Latest version")
            return

        print(f"* New version found: {latest_version}")

        update_program()

    except Exception as e:
        print(f"XXX Update check failed: {e}")


def update_program():

    try:
        response = requests.get(PYTHON_URL, timeout=10)

        if response.status_code != 200:
            print("XXX Cannot download update")
            return

        current_file = os.path.abspath(__file__)

        temp_file = current_file + ".new"

        # сохраняем новую версию
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(response.text)

        # BAT файл для замены
        bat_path = current_file + ".bat"

        with open(bat_path, "w", encoding="utf-8") as bat:

            bat.write(f"""
@echo off
timeout /t 2 >nul
move /Y "{temp_file}" "{current_file}"
start "" python "{current_file}"
del "%~f0"
""")

        print("* Updating program...")

        os.startfile(bat_path)

        sys.exit()

    except Exception as e:
        print(f"XXX Update failed: {e}")
        
        

check_for_updates()





















error_flash_start = 0
ERROR_FLASH_DURATION = 0.8  # секунд

def trigger_error_flash():
    global error_flash_start
    error_flash_start = time.time()


current_py = os.path.basename(__file__)
base_name = os.path.splitext(current_py)[0]
settings_file = f"{base_name}_settings.json"

with open(settings_file, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

NAME = CONFIG["NAME"]
CAMERA_ID = CONFIG["CAMERA_ID"]
FOCUS = CONFIG["FOCUS"]
PHOTO_DELAY = CONFIG["PHOTO_DELAY"]
SCAN_PORT = CONFIG["SCAN_PORT"]
DISPLAY_PORT = CONFIG["DISPLAY_PORT"]
OLDER = CONFIG["OLDER"]
REMOVE_ITEMS = CONFIG["REMOVE_ITEMS"]

with open("PRODUCTS.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

REMOVE_PHRASES = CONFIG["REMOVE_PHRASES"]
FEM_BONUS = CONFIG["FEM_BONUS"]
AUTO_BONUS = CONFIG["AUTO_BONUS"]
OTHER_BONUS = CONFIG["OTHER_BONUS"]

PRODUCTS = CONFIG["PRODUCTS"]


DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
FOTO = os.path.join(DESKTOP, "Foto")
STATISTICS = os.path.join(DESKTOP, "Statistik")
SCAN_INTERVAL = 0.2
scale = 0.15

AUTO_COMPLETE_ITEMS = CONFIG["AUTO_COMPLETE_ITEMS"]

def Printer(text):
    global STATUS_LINES

    REMOVE_FRAGMENTS = ["Auto ", "Fem"]

    if isinstance(text, str):
        lines = text.splitlines()
    else:
        lines = list(text)

    cleaned_lines = []

    for line in lines:
        for fragment in REMOVE_FRAGMENTS:
            line = line.replace(fragment, "")
        cleaned_lines.append(line.strip())

    STATUS_LINES = cleaned_lines[-20:]  # ограничим количество строк


def wait_for_serial(port, baudrate, timeout, name):

    while True:
        try:
            ser = serial.Serial(port, baudrate=baudrate, timeout=timeout)
            print(f"* {name} connected on {port}")
            return ser
        except serial.SerialException:
            print(f"XXX {name} not connected ({port}). Please connect...")
            time.sleep(1)

SCAN = wait_for_serial(
    port=SCAN_PORT,
    baudrate=9600,
    timeout=0.1,
    name="Scanner"
)

DISPLAY = wait_for_serial(
    port=DISPLAY_PORT,
    baudrate=230400,
    timeout=0,
    name="Display"
)



STATUS_LINES = []


def get_line_color(line):
    if line.startswith("XXX"):
        return (0, 0, 255)       # red
    elif line.startswith("+"):
        return (0, 255, 0)       # green
    elif line.startswith("?"):
        return (0, 255, 255)     # yellow
    elif line.startswith("*"):
        return (255, 255, 0)     # cyan
    elif re.search(r"\(\+\d+\)", line):
        return (255, 255, 0)     # cyan
    elif line.startswith("#"):
        return (255, 0, 255)     # purple
    elif "Bonus" in line:
        return (160, 255, 255)   # light yellow
    return (255, 255, 255)       # white

def draw_status_panel(frame, panel_width=500):
    global STATUS_LINES

    h, w = frame.shape[:2]

    # создаём чёрную панель
    panel = np.zeros((h, panel_width, 3), dtype=np.uint8)

    font = cv2.FONT_HERSHEY_SIMPLEX
    y = 30
    line_height = 24

    for line in STATUS_LINES:
        color = get_line_color(line)
        cv2.putText(
            panel,
            line[:50],          # защита от слишком длинных строк
            (10, y),
            font,
            0.6,
            color,
            1,
            cv2.LINE_AA
        )
        y += line_height
        if y > h - 10:
            break

    # склеиваем кадр + панель
    return np.hstack((frame, panel))






def send(ser, message):
    for fragment in ["Auto ", "#", "Fem"]:
        message = message.replace(fragment, "")

    message = message.strip()

    packet = "<BEGIN>\n" + message + "\n<END>\n"

    try:
        ser.write(packet.encode("utf-8"))
    except:
        pass






def normalize(name):

    # убираем Outlet |
    name = re.sub(r"^Outlet \|\s*", "", name)

    # убираем (+2)
    name = re.sub(r"\(\+\d+\)", "", name)

    # убираем лишние пробелы
    name = re.sub(r"\s+", " ", name)

    return name.strip()

def sort_key(item):

    name, qty = item

    normalized = normalize(name)

    # ===== BONUS =====
    is_bonus = "Bonus" in name

    # ===== NO BARCODE =====
    is_no_barcode = normalized not in PRODUCTS.values()

    # ===== fem count =====
    fem_match = re.search(r"(\d+)\s*fem", name)

    fem_count = int(fem_match.group(1)) if fem_match else 0

    # ===== base name =====
    base_name = re.sub(
        r"\s*-\s*\d+\s*fem",
        "",
        normalized
    ).strip().lower()

    # ===== priority =====
    if not is_bonus and not is_no_barcode:
        category = 0  # обычные

    elif is_bonus:
        category = 1  # bonus

    else:
        category = 2  # no barcode

    return (
        category,
        base_name,
        fem_count
    )


def find_latest_pdf(previous=False):

    today = datetime.datetime.now().strftime("%d.%m.%Y")

    pattern = re.compile(
        rf"^{re.escape(today)} Part (\d+)\.pdf$",
        re.IGNORECASE
    )

    found = []

    for file in os.listdir(DOWNLOADS):

        if not file.lower().endswith(".pdf"):
            continue

        match = pattern.match(file)

        if match:

            part = int(match.group(1))

            full_path = os.path.join(DOWNLOADS, file)

            found.append((part, full_path))

    if not found:
        return None, None

    # сортировка по номеру Part
    found.sort(key=lambda x: x[0])

    latest_part, latest_path = found[-1]

    # если нужен предыдущий PDF
    if previous and latest_part > 1:

        previous_part = latest_part - 1

        for part, path in found:
            if part == previous_part:
                return path, previous_part

    return latest_path, latest_part

def save_part_to_statistics(pdf_path, parsed_orders):

    today = datetime.datetime.now().strftime("%d.%m.%Y")

    stat_file = os.path.join(
        STATISTICS,
        f"{today}.txt"
    )

    pdf_name = os.path.basename(pdf_path)

    header = f"----------{pdf_name}----------"

    # если файла нет — создаём
    if not os.path.exists(stat_file):
        with open(stat_file, "w", encoding="utf-8") as f:
            pass

    # читаем содержимое
    with open(stat_file, "r", encoding="utf-8") as f:
        content = f.read()

    # если такая part уже записана
    if header in content:
        return

    # добавляем новую запись
    with open(stat_file, "a", encoding="utf-8") as f:

        f.write(header + "\n")

        for order_id in parsed_orders.keys():
            f.write(order_id + "\n")

        f.write("\n")


def parse_orders(previous=False):

    latest, part = find_latest_pdf(previous)

    if not latest:
        Printer("XXX Orders PDF not found")
        return None

    Printer(f"*Update PDF | Part {part}")
    send(DISPLAY, f"*Update PDF | Part {part}")

    with pdfplumber.open(latest) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]

    text = "\n".join(pages).replace('\r', '\n')

    for p in REMOVE_PHRASES:
        text = text.replace(p, "")
    
    text = re.sub(r"\S*1ZR\S*(?=[☐\s-])", "", text)

    text = re.sub(r"(fem -).*?(x\d+)", r"\1 \2", text, flags=re.DOTALL)

    order_blocks = re.split(r"(#\S+)", text)
    contents = OrderedDict()

    for i in range(1, len(order_blocks), 2):
        oid = order_blocks[i].strip()
        block = order_blocks[i + 1] if i + 1 < len(order_blocks) else ""
        contents[oid] = contents.get(oid, "") + " " + block

    parsed = {}
    for oid, block in contents.items():
        matches = re.findall(r"☐\s+(.*?)\s*[-–]\s*x(\d+)", block, flags=re.DOTALL)
        items = [(re.sub(r"\s+", " ", n).strip(), int(q)) for n, q in matches]

        agg = OrderedDict()
        for name, qty in items:
            agg[name] = agg.get(name, 0) + qty

        parsed[oid] = list(agg.items())

    filtered_items = []

    for name, qty in items:

        # проверяем список исключений
        skip = False

        for remove_text in REMOVE_ITEMS:

            if remove_text in name:
                skip = True
                break

        if not skip:
            filtered_items.append((name, qty))

    # сортировка позиций
    filtered_items.sort(key=sort_key)

    parsed[oid] = filtered_items
        
    # сохраняем информацию о Part
    save_part_to_statistics(latest, parsed)

    return parsed



def cleanup_old_photos(folder, older_days):

    if not os.path.isdir(folder):
        return

    now = time.time()
    max_age = older_days * 30 * 24 * 60 * 60

    removed = 0

    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue

                try:
                    age = now - entry.stat().st_mtime
                except FileNotFoundError:
                    continue

                if age > max_age:
                    send2trash(entry.path)
                    removed += 1

    except Exception as e:
        print(f"Cleanup error: {e}")

    if removed:
        print(f"* Old photos moved to trash: {removed}")
        
def find_original_item(order_items, scanned_name):

    for item_name, qty in order_items:
        if normalize(item_name) == scanned_name:
            return item_name, qty

    return None, None
        
        




















def main():
    os.makedirs(FOTO, exist_ok=True)
    os.makedirs(STATISTICS, exist_ok=True)
    
    today = datetime.datetime.now().day

    if today == 5:
        if OLDER > 0:
            cleanup_old_photos(FOTO, OLDER)


    orders = parse_orders()
    if not orders:
        return
    cap = cv2.VideoCapture(CAMERA_ID, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 4656)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 3496)
    cap.set(cv2.CAP_PROP_FOCUS, FOCUS)

    print("Установленное разрешение:", cap.get(cv2.CAP_PROP_FRAME_WIDTH), "x", cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    current_order = None
    no_barcode_items = []
    counts = defaultdict(int)
    extras = defaultdict(int)
    errors = set()

    last_scan_time = 0
    photo_pending = False
    photo_start = 0

    while True:
        ret, frame = cap.read()
        if ret:
            frame_for_show = cv2.resize(frame, (-1, -1), fx=scale, fy=scale)

            # вывод количества выполненных заказов
            text = f"{completed_orders}"

            font = cv2.FONT_HERSHEY_SIMPLEX

            # измеряем ширину текста, чтобы центрировать
            (text_width, text_height), _ = cv2.getTextSize(text, font, 1, 2)

            # рассчитываем позицию по центру
            x = (frame_for_show.shape[1] - text_width) // 2
            y = 40  # отступ сверху

            # рисуем текст (красный)
            cv2.putText(frame_for_show, text, (x, y), font, 1, (0, 255, 0), 2)

            frame_for_show = draw_error_flash(frame_for_show)
            frame_for_show = draw_status_panel(frame_for_show)
            cv2.imshow("AutoCheck", frame_for_show)


        now = time.time()
        if now - last_scan_time >= SCAN_INTERVAL:
            last_scan_time = now
            try:
                if SCAN.in_waiting:
                    code = SCAN.readline().decode('utf-8', errors='ignore').strip()
                    if code:

                        current_order, photo_pending, photo_start = process_scan(
                            code, orders, current_order, counts, extras, errors, no_barcode_items,
                            Printer, lambda msg: send(DISPLAY, msg),
                            photo_pending, photo_start
                        )
            except Exception as e:
                print(f"[Ошибка чтения сканера]: {e}")

        if photo_pending and (time.time() - photo_start >= PHOTO_DELAY):
            save_photo(current_order, frame)
            photo_pending = False

        key = cv2.waitKey(1)
        if key == 27:
            break
        elif key == ord('x') and current_order:
            save_photo(current_order, frame)
            Printer("\n*Photo taken (manual)")
            photo_pending = False

        elif key == ord('e'):
            # Копируем кадр, чтобы поток не трогал текущий frame
            frame_copy = frame.copy()

            t = threading.Thread(
                target=manual_photo_with_order,
                args=(frame_copy,),
                daemon=True
            )
            t.start()
            
        elif key == ord('u'):
            orders = parse_orders()
        elif key == ord('U'):
            orders = parse_orders(previous=True)
        elif key == ord('r'):
            check_for_updates()
        elif key == ord('c'):

            t = threading.Thread(
                target=cancel_order_manual,
                daemon=True
            )

            t.start()
            

        try:
            if DISPLAY.in_waiting > 0:
                message = DISPLAY.read(DISPLAY.in_waiting).decode(errors='ignore').strip()
                if "x" in message and current_order:
                    save_photo(current_order, frame, manually=True)
                    # Printer("\n*Photo taken (manual display)")
                    photo_pending = False
                if "e" in message:
                    # Копируем кадр, чтобы поток не трогал текущий frame
                    frame_copy = frame.copy()

                    t = threading.Thread(
                        target=manual_photo_with_order,
                        args=(frame_copy,),
                        daemon=True
                    )
                    t.start()
        except Exception as e:
            print(f"[Ошибка чтения DISPLAY]: {e}")

    cap.release()
    cv2.destroyAllWindows()





























def process_scan(code, orders, current_order, counts, extras, errors, no_barcode_items, print_fn, send_fn, photo_pending, photo_start):

    if code.startswith('#'):
        matched = next((oid for oid in orders if oid.replace('#', '') == code.replace('#', '')), None)

        if not matched:
            print_fn(f"XXX Unknown order {code}")
            send_fn(f"XXX Unknown order {code}")
            return current_order, photo_pending, photo_start
        
        if is_order_cancelled(matched):

            print_fn(f"XXX Cancelled order {matched}")
            send_fn(f"XXX Cancelled order {matched}")

            return current_order, photo_pending, photo_start

        current_order = matched
        counts.clear()
        extras.clear()
        errors.clear()
        no_barcode_items.clear()

        for item, qty in orders[current_order]:

            if normalize(item) not in PRODUCTS.values() and "Bonus" not in item:

                # если позиция без штрихкода и qty == 1
                # считаем её автоматически собранной
                if qty == 1 and item in AUTO_COMPLETE_ITEMS:
                    counts[item] = 1

                else:
                    no_barcode_items.append((item, qty))


        update_order_in_statistics(
            current_order,
            add_name=True
        )

        show_status(current_order, orders[current_order], counts, extras, errors, no_barcode_items, print_fn, send_fn)

        return current_order, photo_pending, photo_start

    # Получаем список обычных позиций для проверки
    order_items = dict(orders[current_order])

    name = PRODUCTS.get(code)
    

    # Проверяем, собраны ли все обычные позиции
    def all_regular_collected():
        for item_name, qty in orders[current_order]:
            if "Bonus" in item_name:
                continue
            if normalize(item_name) not in PRODUCTS.values():
                continue
            if counts.get(item_name, 0) < qty:
                return False
        return True

    regular_ready = all_regular_collected()

    if not regular_ready:
        if name:
            item_name, qty = find_original_item(
            orders[current_order],
            name
        )

        if item_name:

            counts[item_name] += 1

            if counts[item_name] > qty:
                extras[item_name] = counts[item_name] - qty

        else:
            errors.add(name)
            trigger_error_flash()
            photo_pending = False

        show_status(current_order, orders[current_order], counts, extras, errors, no_barcode_items, print_fn, send_fn)
        if order_ready(orders[current_order], counts, extras, errors, no_barcode_items):
            print_fn("+ Order complete")
            send_fn("+ Order complete")
            photo_pending = True
            photo_start = time.time()
        return current_order, photo_pending, photo_start

    bonus_type = None

    if code in FEM_BONUS:
        bonus_type = "FEM"

    elif code in AUTO_BONUS:
        bonus_type = "AUTO"
    
    elif code in OTHER_BONUS:
        bonus_type = "OTHER"

    if bonus_type:

        # Преобразуем имя бонуса
        if bonus_type == "FEM":
            final_name = "FEM | Bonus Fem seeds - 1 fem"
        elif bonus_type == "AUTO":
            final_name = "AUTO | Bonus Auto seeds - 1 fem"
        else:
            final_name = OTHER_BONUS.get(code)

        if final_name not in order_items:
            errors.add(name)
            trigger_error_flash()
            show_status(current_order, orders[current_order], counts, extras, errors, no_barcode_items, print_fn, send_fn)
            return current_order, photo_pending, photo_start

        counts[final_name] += 1
        if counts[final_name] > order_items[final_name]:
            extras[final_name] = counts[final_name] - order_items[final_name]
            photo_pending = False

        show_status(current_order, orders[current_order], counts, extras, errors, no_barcode_items, print_fn, send_fn)
        if order_ready(orders[current_order], counts, extras, errors, no_barcode_items):
            print_fn("+ Order complete")
            send_fn("+ Order complete")
            photo_pending = True
            photo_start = time.time()
        return current_order, photo_pending, photo_start

    if name:

        item_name, qty = find_original_item(
            orders[current_order],
            name
        )

        if item_name:

            counts[item_name] += 1

            if counts[item_name] > qty:
                extras[item_name] = counts[item_name] - qty
                photo_pending = False

        else:
            errors.add(name)
            trigger_error_flash()
            photo_pending = False

        show_status(
            current_order,
            orders[current_order],
            counts,
            extras,
            errors,
            no_barcode_items,
            print_fn,
            send_fn
        )

        return current_order, photo_pending, photo_start



    show_status(current_order, orders[current_order], counts, extras, errors, no_barcode_items, Printer, send_fn)

    if order_ready(orders[current_order], counts, extras, errors, no_barcode_items):
        print_fn("+ Order complete")
        send_fn("+ Order complete")
        photo_pending = True
        photo_start = time.time()

    return current_order, photo_pending, photo_start













def show_status(order_id, items, counts, extras, errors, no_barcode_items, print_fn, send_fn):
    msg = [f"#Order status {order_id}"]

    for name, qty in items:
        if (name, qty) in no_barcode_items:
            continue

        c = counts.get(name, 0)
        if c == qty:
            msg.append(f"+ {name} x{qty}")
        elif c < qty:
            msg.append(f"{c}/{qty} {name} x{qty}")
        else:
            msg.append(f"+ {name} x{qty}")
            msg.append(f"!!! Extra {extras.get(name, 0)}")

    for name, qty in no_barcode_items:
        msg.append(f"? {name} x{qty}")

    for e in errors:
        msg.append(f"XXX {e}")

    full = "\n".join(msg)
    print_fn(full)
    send_fn(full)








def order_ready(items, counts, extras, errors, no_barcode_items):
    if errors:
        return False

    if extras:
        return False
    
    if no_barcode_items:
        return False

    for name, qty in items:
        if 'Bonus' in name:
            continue
        if counts.get(name, 0) != qty:
            return False

    for name, qty in items:
        if 'Bonus' in name:
            if counts.get(name, 0) != qty:
                return False
    return True

def update_order_in_statistics(order_id, add_name=False, add_plus=False, add_cancelled=False):
    
    updated = False

    today = datetime.datetime.now().strftime("%d.%m.%Y")

    stat_file = os.path.join(
        STATISTICS,
        f"{today}.txt"
    )

    if not os.path.exists(stat_file):
        return

    try:

        with open(stat_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        updated_lines = []

        for line in lines:

            stripped = line.strip()

            # ищем строку заказа
            if stripped.startswith(order_id):

                # если заказ уже Cancelled —
                # ===== пропускаем cancelled =====
                if "Cancelled" in stripped:
                    updated_lines.append(line)
                    continue

                # ===== добавляем NAME =====
                if add_name and NAME not in stripped:
                    stripped += f" {NAME}"

                # ===== добавляем + =====
                if add_plus and "+" not in stripped:
                    stripped += " +"
                
                # ===== добавляем Cancelled =====
                if add_cancelled and "Cancelled" not in stripped:
                    stripped += " Cancelled"

                line = stripped + "\n"
                updated = True

            updated_lines.append(line)

        with open(stat_file, "w", encoding="utf-8") as f:
            f.writelines(updated_lines)

    except Exception as e:
        Printer(f"XXX Statistics update error: {e}")
        
    return updated
        
def cancel_order_manual():

    order = input("Enter cancelled order number: #").strip()

    if not order.isdigit():
        print("Invalid order number")
        return

    order_id = f"#{order}"

    success = update_order_in_statistics(
    order_id,
    add_cancelled=True
)

    if success:
        Printer(f"XXX {order_id} Cancelled")
        send(DISPLAY, f"XXX {order_id} Cancelled")
    else:
        Printer(f"XXX Active order not found {order_id}")
        send(DISPLAY, f"XXX Active order not found {order_id}")



def is_order_cancelled(order_id):

    today = datetime.datetime.now().strftime("%d.%m.%Y")

    stat_file = os.path.join(
        STATISTICS,
        f"{today}.txt"
    )

    if not os.path.exists(stat_file):
        return False

    try:

        with open(stat_file, "r", encoding="utf-8") as f:

            for line in f:

                stripped = line.strip()

                if stripped.startswith(order_id):

                    if "Cancelled" not in stripped:
                        return False

        return True

    except:
        return False



completed_orders = 0
def save_photo(order_id, frame, manually=False):

    if not order_id or frame is None:
        return

    # =========================
    # Поиск свободного имени
    # =========================

    photo_number = 1

    while True:

        if photo_number == 1:
            filename = f"{order_id}.jpg"
        else:
            filename = f"{order_id}_{photo_number}.jpg"

        path = os.path.join(FOTO, filename)

        if not os.path.exists(path):
            break

        photo_number += 1

    # =========================
    # Сохраняем фото
    # =========================

    cv2.imwrite(path, frame)

    # =========================
    # Сообщение
    # =========================

    if manually:
        message = f"* {filename} * Photo taken (manual display)"
    else:
        message = f"* {filename} * Photo saved"

    send(DISPLAY, message)
    Printer(message)
        
    update_order_in_statistics(
        order_id,
        add_plus=True
    )

    # ==== считаем количество уникальных заказов в статистике ====
    global completed_orders

    name_folder = os.path.join(STATISTICS, NAME)
    date_filename = datetime.datetime.now().strftime("%Y-%m-%d") + ".txt"
    stat_file = os.path.join(name_folder, date_filename)

    # если файла нет – завершённых заказов 0
    if not os.path.exists(stat_file):
        completed_orders = 0
        return

    completed_orders = 0

    try:

        with open(stat_file, 'r', encoding='utf-8') as f:

            for line in f:

                stripped = line.strip()

                # считаем только:
                # заказ + NAME + +
                if (
                    stripped.startswith("#")
                    and NAME in stripped
                    and "+" in stripped
                ):
                    completed_orders += 1

    except:
        pass


def manual_photo_with_order(frame):
    order = input("Enter order number: #").strip()

    if not order.isdigit():
        print("Invalid order number")
        return

    order_id = f"#{order}"
    save_photo(order_id, frame)


def draw_error_flash(frame):
    global error_flash_start

    if error_flash_start == 0:
        return frame

    elapsed = time.time() - error_flash_start

    if elapsed > ERROR_FLASH_DURATION:
        error_flash_start = 0
        return frame

    h, w = frame.shape[:2]

    # Нормализуем время (0 → 1)
    t = elapsed / ERROR_FLASH_DURATION

    # Плавная кривая (ease in-out)
    alpha = 0.6 * (1 - abs(2 * t - 1))  # пик в середине

    overlay = frame.copy()

    thickness = int(min(w, h) * 0.1)  # толщина рамки ~10%

    color = (0, 0, 255)  # красный (BGR)

    # Верх
    cv2.rectangle(overlay, (0, 0), (w, thickness), color, -1)
    # Низ
    cv2.rectangle(overlay, (0, h - thickness), (w, h), color, -1)
    # Лево
    cv2.rectangle(overlay, (0, 0), (thickness, h), color, -1)
    # Право
    cv2.rectangle(overlay, (w - thickness, 0), (w, h), color, -1)

    # Смешиваем с прозрачностью
    return cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0)

        

if __name__ == "__main__":
    main()

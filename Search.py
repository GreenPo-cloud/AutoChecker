"""Manual search for an item and quantity in AutoChecker Order JSON files."""

from __future__ import annotations

import json
import re
from pathlib import Path


# Change these values before starting the script.
SEARCH_NAME = "AUTO | Papaya Cookies Auto - 2(+1) fem"
SEARCH_QUANTITY = 1

# False means an exact name (recommended).  Set True to search by a fragment.
PARTIAL_NAME_MATCH = False

SEARCH_FOLDER = Path.home() / "Desktop" / "Testttt"


def normalized_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def iter_order_items(order: object):
    """Yield (name, quantity) from current and older Order JSON layouts."""
    if isinstance(order, dict):
        for name, quantity in order.items():
            yield str(name), quantity
        return
    if not isinstance(order, list):
        return
    for item in order:
        if isinstance(item, dict):
            name = item.get("Name", item.get("name"))
            quantity = item.get("Quantity", item.get("quantity"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name, quantity = item[0], item[1]
        else:
            continue
        if name is not None:
            yield str(name), quantity


def item_matches(name: str, quantity: object) -> bool:
    expected_name = normalized_name(SEARCH_NAME)
    actual_name = normalized_name(name)
    name_matches = (
        expected_name in actual_name
        if PARTIAL_NAME_MATCH else actual_name == expected_name
    )
    try:
        quantity_matches = int(quantity) == int(SEARCH_QUANTITY)
    except (TypeError, ValueError):
        quantity_matches = False
    return name_matches and quantity_matches


def search_file(path: Path) -> list[str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"XXX Cannot read {path.name}: {error}")
        return []
    if not isinstance(document, dict):
        return []

    matching_orders = []
    for order_number, entry in document.items():
        if not isinstance(entry, dict) or "Order" not in entry:
            continue
        if any(
            item_matches(name, quantity)
            for name, quantity in iter_order_items(entry["Order"])
        ):
            matching_orders.append(str(order_number))
    return matching_orders


def main() -> None:
    if not SEARCH_NAME.strip():
        print("XXX Set SEARCH_NAME at the top of Search.py")
        return
    if not SEARCH_FOLDER.is_dir():
        print(f"XXX Folder not found: {SEARCH_FOLDER}")
        return

    found = 0
    for json_path in sorted(
        SEARCH_FOLDER.rglob("*.json"),
        key=lambda path: str(path).casefold(),
    ):
        for order_number in search_file(json_path):
            print(f"* {json_path.name} | {order_number}")
            found += 1

    if found:
        print(f"* Found: {found}")
    else:
        print("? No matching orders found")


if __name__ == "__main__":
    main()

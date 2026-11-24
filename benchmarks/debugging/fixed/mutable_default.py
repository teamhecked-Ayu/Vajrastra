from typing import Optional


def collect(value: int, items: Optional[list] = None) -> list:
    if items is None:
        items = []
    items.append(value)
    return items

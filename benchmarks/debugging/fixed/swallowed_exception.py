def parse_number(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0

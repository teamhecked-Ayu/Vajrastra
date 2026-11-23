def ratio(total: int, count: int) -> int:
    if count == 0:
        raise ValueError("count must not be zero")
    return total // count

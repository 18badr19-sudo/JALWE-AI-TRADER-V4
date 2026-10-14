"""Lossless Telegram text splitting shared by report senders."""


def telegram_report_parts(message: str, limit: int = 3800) -> list[str]:
    """Keep each part within UTF-16 limits, without splitting an emoji."""
    if type(limit) is not int or limit < 2:
        raise ValueError('Telegram part limit must be an integer of at least 2')
    parts = []
    current = ""
    units = 0
    for char in message:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            parts.append(current)
            current, units = "", 0
        current += char
        units += size
    if current:
        parts.append(current)
    return parts or [""]

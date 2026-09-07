"""Daily cache generation changes at 05:00 Europe/Kyiv, including DST."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def refresh_generation(now: datetime | None = None) -> str:
    local = (now or datetime.now(ZoneInfo("Europe/Kyiv"))).astimezone(ZoneInfo("Europe/Kyiv"))
    day = local.date() if local.hour >= 5 else local.date() - timedelta(days=1)
    return f"daily-{day.isoformat()}"

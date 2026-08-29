from datetime import datetime
from zoneinfo import ZoneInfo


def is_market_open(asset_type: str, now_ts: float, market_timezone: str = "America/New_York", allow_extended_hours: bool = False) -> bool:
    if asset_type == "crypto":
        return True
    if allow_extended_hours:
        return True
    local = datetime.fromtimestamp(now_ts, tz=ZoneInfo(market_timezone))
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)

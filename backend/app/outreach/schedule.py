"""发送时间窗口：收件人当地时间周一至周五 8:00–17:00，跳过公共假日（M6-05 / M6-15 / BR-6-23）。"""
import random
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays as pyholidays

# 国家 → 代表时区（多时区国家取商务中心）
COUNTRY_TZ = {
    "US": "America/New_York", "CA": "America/Toronto", "MX": "America/Mexico_City", "BR": "America/Sao_Paulo",
    "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago", "CO": "America/Bogota", "PE": "America/Lima",
    "GB": "Europe/London", "IE": "Europe/Dublin", "DE": "Europe/Berlin", "FR": "Europe/Paris", "IT": "Europe/Rome",
    "ES": "Europe/Madrid", "PT": "Europe/Lisbon", "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "CH": "Europe/Zurich",
    "AT": "Europe/Vienna", "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki",
    "PL": "Europe/Warsaw", "CZ": "Europe/Prague", "HU": "Europe/Budapest", "RO": "Europe/Bucharest", "GR": "Europe/Athens",
    "TR": "Europe/Istanbul", "RU": "Europe/Moscow", "UA": "Europe/Kyiv", "AE": "Asia/Dubai", "SA": "Asia/Riyadh",
    "IL": "Asia/Jerusalem", "EG": "Africa/Cairo", "ZA": "Africa/Johannesburg", "NG": "Africa/Lagos", "KE": "Africa/Nairobi",
    "MA": "Africa/Casablanca", "IN": "Asia/Kolkata", "PK": "Asia/Karachi", "BD": "Asia/Dhaka", "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei", "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "SG": "Asia/Singapore",
    "MY": "Asia/Kuala_Lumpur", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh", "ID": "Asia/Jakarta", "PH": "Asia/Manila",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland",
}
# 周末不是周六日的国家
WEEKEND = {"SA": (4, 5), "IL": (4, 5), "EG": (4, 5), "QA": (4, 5), "KW": (4, 5), "OM": (4, 5), "BH": (4, 5), "JO": (4, 5)}  # noqa: E501  阿联酋 2022 起为周六日


def tz_for(country: str | None) -> ZoneInfo:
    return ZoneInfo(COUNTRY_TZ.get((country or "").upper(), "UTC"))


@lru_cache(maxsize=256)
def _holidays(country: str, year: int):
    try:
        return pyholidays.country_holidays(country, years=year)
    except (NotImplementedError, KeyError):
        return {}  # 缺少日历的国家只按周末处理


def is_business_day(d, country: str | None) -> bool:
    wk = WEEKEND.get((country or "").upper(), (5, 6))
    if d.weekday() in wk:
        return False
    return not (country and d in _holidays(country.upper(), d.year))


def next_window(after: datetime, country: str | None, start_h: int = 8, end_h: int = 17, rng: random.Random | None = None,
                jitter: bool = True) -> datetime:
    """返回不早于 after 的、收件人当地工作时间内的时刻（UTC aware）。"""
    rng = rng or random
    tz = tz_for(country)
    local = after.astimezone(tz)
    for _ in range(30):
        if is_business_day(local.date(), country):
            start = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
            end = local.replace(hour=end_h, minute=0, second=0, microsecond=0)
            if local < start:
                cand = start + (timedelta(minutes=rng.randint(0, 90)) if jitter else timedelta())
                return cand.astimezone(after.tzinfo)
            if local < end:
                return local.astimezone(after.tzinfo)
        local = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return after


def in_window(at: datetime, country: str | None, start_h=8, end_h=17) -> bool:
    local = at.astimezone(tz_for(country))
    return is_business_day(local.date(), country) and start_h <= local.hour < end_h

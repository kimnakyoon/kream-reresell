"""KREAM 체결 내역의 거래일 표기("1시간 전", "3일 전", "25/08/01") 를 datetime 으로."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_REL = re.compile(r"(\d+)\s*(분|시간|일|주|개월|달|년)\s*전")
_ABS = re.compile(r"(\d{2,4})[./-](\d{1,2})[./-](\d{1,2})")


def parse_trade_date(text: str, now: datetime | None = None) -> datetime | None:
    """거래일 문자열을 시각으로. 못 읽으면 None.

    상대 표기는 '이 시각보다 오래되지 않았다' 는 뜻이므로 가장 최근 쪽으로 해석한다
    (예: "3일 전" -> now - 3일). 30일 판정에는 그 정도 오차면 충분하다.
    """
    now = now or datetime.now()
    text = text.strip()
    if text in ("방금 전", "방금"):
        return now
    m = _REL.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "분": timedelta(minutes=n),
            "시간": timedelta(hours=n),
            "일": timedelta(days=n),
            "주": timedelta(weeks=n),
            "개월": timedelta(days=30 * n),
            "달": timedelta(days=30 * n),
            "년": timedelta(days=365 * n),
        }[unit]
        return now - delta
    m = _ABS.search(text)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        if y < 100:
            y += 2000
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    return None


def parse_won(text: str) -> int | None:
    """'89,000원' -> 89000. 숫자가 없으면 None."""
    m = re.search(r"(\d[\d,]*)\s*원", text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))

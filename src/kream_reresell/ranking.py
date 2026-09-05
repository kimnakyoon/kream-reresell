"""랭킹 탭에서 상품군을 골라 순위대로 상품 ID 를 모은다."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from playwright.sync_api import Page

log = logging.getLogger(__name__)

RANKING_URL = "https://kream.co.kr/?tab=home_ranking_v2"

# 랭킹 탭의 상품군 칩 순서 그대로 (2026-09-04 실측). 값은 URL 의 category_filter.
# 신발류(신발/러닝화/부츠)·의류도 포함한다 - 사이즈 옵션이 있는 상품은 옵션마다 따로 판정한다 (pipeline).
CATEGORY_IDS: dict[str, int] = {
    "신발": 12,
    "바람막이": 49,
    "후디": 285,
    "러닝화": 273,
    "긴소매 티셔츠": 274,
    "패딩": 281,
    "부츠": 46,
    "트레이딩카드": 282,
    "상의": 38,
    "팬츠": 275,
    "시계": 272,
    "주얼리": 36,
    "뷰티": 31,
    "가방": 34,
    "지갑": 33,
    "아이웨어": 276,
    "키링": 43,
    "테크": 16,
    "라이프": 27,
    "레고": 37,
    "컨템포러리": 280,
}
ALL_CATEGORIES: list[str] = list(CATEGORY_IDS)
DEFAULT_CATEGORY = "가방"

_CHIP = "button.filter_button"
_ACTIVE_CHIP = "button.filter_button.active"
_PRODUCT_HREF = re.compile(r"/products/(\d+)")


@dataclass
class RankedProduct:
    rank: int
    product_id: int
    name: str
    price: int | None
    url: str
    category: str = ""


def _active_chip_text(page: Page) -> str:
    try:
        chip = page.locator(_ACTIVE_CHIP).first
        if chip.count() == 0:
            return ""
        return chip.inner_text(timeout=2000).strip()
    except Exception:  # noqa: BLE001
        return ""


def open_category(page: Page, category: str) -> None:
    """랭킹 탭에서 상품군을 연다.

    알고 있는 category_filter 로 바로 열고, 선택된 칩이 다른 상품군이면(ID 가 바뀐 경우)
    칩을 직접 눌러 맞춘 뒤 새 ID 를 기억한다.
    """
    cid = CATEGORY_IDS.get(category)
    if cid is not None:
        page.goto(f"{RANKING_URL}&category_filter={cid}", wait_until="domcontentloaded")
        page.locator('a[href*="/products/"]').first.wait_for(state="visible", timeout=15_000)
        page.wait_for_timeout(500)
        active = _active_chip_text(page)
        if active == category or not active:
            return
        log.warning("category_filter=%s 로 열었더니 '%s' 가 선택됨 - '%s' 칩을 직접 누른다", cid, active, category)
    else:
        page.goto(RANKING_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

    chip = page.locator(_CHIP, has_text=re.compile(rf"^\s*{re.escape(category)}\s*$")).first
    if chip.count() == 0:
        chip = page.get_by_text(category, exact=True).first
    if chip.count() == 0:
        raise ValueError(f"랭킹 탭에 '{category}' 상품군 칩이 없습니다")
    chip.scroll_into_view_if_needed()
    chip.click()
    page.wait_for_url(re.compile(r"category_filter=\d+"), timeout=10_000)
    m = re.search(r"category_filter=(\d+)", page.url)
    if m:
        CATEGORY_IDS[category] = int(m.group(1))
        log.info("상품군 '%s' -> category_filter=%s", category, m.group(1))
    page.locator('a[href*="/products/"]').first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(800)   # 이전 상품군 카드가 새 카드로 바뀔 시간


def collect_products(page: Page, limit: int, category: str = "") -> list[RankedProduct]:
    """현재 랭킹 화면을 스크롤해 가며 순위대로 limit 개까지 모은다."""
    seen: dict[int, RankedProduct] = {}
    stale_rounds = 0
    while len(seen) < limit and stale_rounds < 4:
        before = len(seen)
        for item in page.evaluate(_COLLECT_JS):
            m = _PRODUCT_HREF.search(item["href"])
            if not m:
                continue
            pid = int(m.group(1))
            if pid in seen:
                continue
            seen[pid] = RankedProduct(
                rank=len(seen) + 1,
                product_id=pid,
                name=item["name"],
                price=item["price"],
                url=f"https://kream.co.kr/products/{pid}",
                category=category,
            )
        if len(seen) == before:
            stale_rounds += 1
        else:
            stale_rounds = 0
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(800)
    result = list(seen.values())[:limit]
    log.info("랭킹 '%s' 상품 %d개 수집", category, len(result))
    return result


# 랭킹 카드는 /products/{id} 링크 하나가 카드 전체를 감싼다.
# 텍스트는 "거래 4.1만 / 1 / - / 상품명 / 90,000원" 순으로 줄바꿈되어 나온다.
_COLLECT_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/products/"]')) {
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    const lines = a.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    const priceLine = lines.find(s => /\\d[\\d,]*원$/.test(s));
    const price = priceLine ? parseInt(priceLine.match(/(\\d[\\d,]*)원$/)[1].replace(/,/g, ''), 10) : null;
    // 가격/거래수/순위/변동 표시가 아닌 가장 긴 줄을 상품명으로 본다
    const name = lines
      .filter(s => !/원$/.test(s) && !/^거래\\s/.test(s) && !/^\\d+$/.test(s) && !/^[-▲▼]\\d*$/.test(s) && !/^\\d+%$/.test(s))
      .sort((x, y) => y.length - x.length)[0] || '';
    out.push({ href: a.href, name, price });
  }
  return out;
}
"""

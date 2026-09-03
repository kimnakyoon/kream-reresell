"""랭킹 탭에서 상품군을 골라 순위대로 상품 ID 를 모은다."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from playwright.sync_api import Page

log = logging.getLogger(__name__)

RANKING_URL = "https://kream.co.kr/?tab=home_ranking_v2"

# 실측으로 알아낸 상품군 -> category_filter 값. 모르는 상품군은 칩을 눌러 URL 에서 읽는다.
KNOWN_CATEGORY_IDS = {
    "가방": 34,
}
# 신발류는 하지 않는다 (옵션이 있고 사이즈별로 시세가 다르다)
EXCLUDED_CATEGORIES = {"신발", "러닝화", "부츠", "슬리퍼", "샌들"}

_PRODUCT_HREF = re.compile(r"/products/(\d+)")


@dataclass
class RankedProduct:
    rank: int
    product_id: int
    name: str
    price: int | None
    url: str


def open_category(page: Page, category: str) -> None:
    if category in EXCLUDED_CATEGORIES:
        raise ValueError(f"'{category}' 은(는) 제외 상품군입니다.")
    cid = KNOWN_CATEGORY_IDS.get(category)
    if cid is not None:
        page.goto(f"{RANKING_URL}&category_filter={cid}", wait_until="domcontentloaded")
        page.locator('a[href*="/products/"]').first.wait_for(state="visible", timeout=15_000)
        page.wait_for_timeout(500)
        return

    page.goto(RANKING_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    chip = page.get_by_text(category, exact=True).first
    chip.scroll_into_view_if_needed()
    chip.click()
    page.wait_for_url(re.compile(r"category_filter=\d+"), timeout=10_000)
    page.wait_for_timeout(1500)
    m = re.search(r"category_filter=(\d+)", page.url)
    if m:
        KNOWN_CATEGORY_IDS[category] = int(m.group(1))
        log.info("상품군 '%s' -> category_filter=%s", category, m.group(1))


def collect_products(page: Page, limit: int) -> list[RankedProduct]:
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
            )
        if len(seen) == before:
            stale_rounds += 1
        else:
            stale_rounds = 0
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(800)
    result = list(seen.values())[:limit]
    log.info("랭킹 상품 %d개 수집", len(result))
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

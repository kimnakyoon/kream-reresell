"""검색 결과 화면에서 상품 ID 를 화면 순서대로 모은다 ([입찰] 의 '검색' 모드).

랭킹 대신 검색어로 상품을 고르는 방식이다 (2026-09-06 실측):
- 주소 `https://kream.co.kr/search?keyword=검색어` (상품 탭). 필터 패널의 [빠른배송] 을 켜면 `&tab=products&delivery_method=quick_delivery`
  가 붙고 지금 빠른배송 판매자가 있는 상품만 나온다 (롱샴: 1,276개 → 45개). 빠른배송 판매자가 없는 상품은 어차피 A 를 못 읽어
  건너뛰므로 기본으로 이 필터를 켜서 스로틀 대상 요청을 아낀다 (.env SEARCH_QUICK_ONLY=0 / GUI 체크 해제로 끌 수 있다).
- 카드는 `a.product_card[href*="/products/"]`, 50개씩 스크롤(무한 스크롤)로 더 그려진다. 스크롤은 마우스 휠 이벤트여야 다음 50개가
  불러와진다 (window.scrollTo 로는 안 됨). 카드 사이에 '함께 찾는 키워드' 블록이 끼지만 카드의 DOM 순서 = 화면 순서다.
- 카드 글줄은 브랜드 / 상품명 / (할인율%) / 가격원 / 관심·리뷰·거래 / (빠른배송 내일 도착 예정) 순.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from playwright.sync_api import Page

from .ranking import RankedProduct, parse_count

log = logging.getLogger(__name__)

SEARCH_URL = "https://kream.co.kr/search"
_PRODUCT_HREF = re.compile(r"/products/(\d+)")
_CARD = 'a.product_card[href*="/products/"]'


def search_url(keyword: str, quick_only: bool = True) -> str:
    q = f"keyword={quote(keyword.strip())}"
    if quick_only:
        return f"{SEARCH_URL}?tab=products&delivery_method=quick_delivery&{q}"
    return f"{SEARCH_URL}?{q}"


def category_label(keyword: str) -> str:
    """보고서의 '랭킹' 열과 상태줄에 쓰는 이름."""
    return f"검색:{keyword.strip()}"


def open_search(page: Page, keyword: str, quick_only: bool = True) -> int:
    """검색 결과(상품 탭)를 연다. 첫 화면에 그려진 카드 수를 돌려준다 (0 이면 결과 없음)."""
    page.goto(search_url(keyword, quick_only), wait_until="domcontentloaded")
    return wait_cards(page, f"검색 '{keyword}'" + (" (빠른배송 필터)" if quick_only else ""))


def wait_cards(page: Page, log_name: str) -> int:
    """카드 목록 화면(검색·SHOP)이 그려질 때까지 기다린다. 첫 화면 카드 수를 돌려준다 (0 이면 결과 없음)."""
    try:
        page.locator(_CARD).first.wait_for(state="visible", timeout=15_000)
    except Exception:  # noqa: BLE001
        # 결과가 하나도 없거나 아직 안 그려짐 - 본문으로 한 번 더 판단
        page.wait_for_timeout(1500)
        if page.locator(_CARD).count() == 0:
            body = page.locator("body").inner_text(timeout=3000)
            if "검색 결과가 없" in body or "결과가 없습니다" in body or "상품이 없" in body:
                log.info("%s: 결과 없음", log_name)
                return 0
            raise
    page.wait_for_timeout(800)
    n = page.locator(_CARD).count()
    log.info("%s 열림 - 첫 화면 카드 %d개", log_name, n)
    return n


def collect_products(page: Page, limit: int, keyword: str, quick_only: bool = True) -> list[RankedProduct]:
    """검색 결과를 화면 순서대로 limit 개까지 모은다. 부족하면 휠 스크롤로 다음 50개를 불러온다."""
    return collect_cards(page, limit, category_label(keyword),
                         f"검색 '{keyword}'" + (" (빠른배송 필터)" if quick_only else ""))


def collect_cards(page: Page, limit: int, label: str, log_name: str) -> list[RankedProduct]:
    """상품 카드(`a.product_card`) 목록 화면을 화면 순서대로 limit 개까지 모은다 (검색·SHOP 공통).

    부족하면 휠 스크롤로 다음 50개를 불러온다. label 은 보고서의 '랭킹' 열, log_name 은 로그에 적을 이름.
    """
    seen: dict[int, RankedProduct] = {}
    stale_rounds = 0
    page.mouse.move(600, 400)   # 휠 이벤트가 문서에 닿도록
    while len(seen) < limit and stale_rounds < 6:
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
                category=label,
                # 카드에 '거래' 줄이 없으면 거래 0건 (관심·리뷰만 있는 카드가 그렇다)
                trades=parse_count(item["trades"]) if item["trades"] else 0,
            )
        if len(seen) >= limit:
            break
        stale_rounds = stale_rounds + 1 if len(seen) == before else 0
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1000)   # 다음 50개가 그려질 시간 (스피너 → 카드)
    result = list(seen.values())[:limit]
    log.info("%s 상품 %d개 수집%s", log_name, len(result),
             "" if len(result) >= limit else f" (결과가 {limit}개보다 적음)")
    return result


# 카드 글줄: 브랜드 / 상품명 / (17%) / 145,000원 / 관심 2.8만 / · 리뷰 1,494 / · 거래 2.3만 / 빠른배송 / 내일 도착 예정
_COLLECT_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a.product_card[href*="/products/"]')) {
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    const lines = a.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    const priceLine = lines.find(s => /\\d[\\d,]*원$/.test(s));
    const price = priceLine ? parseInt(priceLine.match(/(\\d[\\d,]*)원$/)[1].replace(/,/g, ''), 10) : null;
    const trades = lines.find(s => /^·?\\s*거래\\s/.test(s)) || null;
    const rest = lines.filter(s => !/원$/.test(s) && !/^\\d+%$/.test(s) && !/^관심/.test(s) && !/^·/.test(s)
                                && !/리뷰|거래/.test(s) && !/빠른배송|도착 예정/.test(s));
    // 첫 줄은 브랜드, 둘째 줄이 상품명
    const name = rest.length > 1 ? rest[1] : (rest[0] || '');
    out.push({ href: a.href, name, price, trades });
  }
  return out;
}
"""

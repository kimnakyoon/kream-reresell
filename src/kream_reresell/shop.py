"""SHOP 탭의 카테고리 목록 화면에서 상품 ID 를 화면 순서대로 모은다 ([입찰] 의 'SHOP' 모드).

사이트 상단 HOME / STYLE / SHOP 의 SHOP (2026-09-06 실측):
- 주소는 검색과 같은 `/search` 이고 카테고리 탭이 `?tab=번호` 다 (전체는 `/search`, 신발 `/search?tab=44` ...).
  '전체' 는 뺀 19개 카테고리를 탭 순서대로 둔다 (SHOP_CATEGORY_IDS).
- 필터·카드·무한 스크롤은 검색 결과와 같다. `&delivery_method=quick_delivery` 를 붙이면 지금 빠른배송 판매자가 있는
  상품만 나온다. 빠른배송이 없는 상품은 A 를 못 읽어 어차피 건너뛰므로 기본으로 이 필터를 켠다
  (.env SHOP_QUICK_ONLY=0 / GUI 체크 해제 / --no-quick-filter 로 끌 수 있다).
- 카드는 `a.product_card`, 50개씩 휠 스크롤로 이어 붙는다 - 수집은 search.collect_cards 를 그대로 쓴다.
- 탭 번호가 바뀌면 열린 화면의 선택 탭이 다르므로, 그때는 탭을 글자로 찾아 눌러 맞추고 새 번호를 기억한다 (ranking 과 같은 방식).
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Page

from . import search
from .ranking import RankedProduct

log = logging.getLogger(__name__)

SHOP_URL = "https://kream.co.kr/search"

# SHOP 탭 순서 그대로 (2026-09-06 실측). 값은 URL 의 tab.
SHOP_CATEGORY_IDS: dict[str, int] = {
    "럭셔리": 72,
    "트레이딩 카드": 71,
    "신발": 44,
    "상의": 50,
    "아우터": 49,
    "하의": 51,
    "가방": 63,
    "지갑": 53,
    "시계": 64,
    "패션잡화": 46,
    "컬렉터블": 54,
    "티켓": 76,
    "금/은": 77,
    "뷰티": 65,
    "테크": 48,
    "가전": 73,
    "캠핑": 66,
    "가구/리빙": 55,
    "푸드": 74,
}
ALL_SHOP_CATEGORIES: list[str] = list(SHOP_CATEGORY_IDS)
DEFAULT_SHOP_CATEGORY = "가방"

_TAB = "li.li_search_tab a.tab"
_ACTIVE_TAB = "li.li_search_tab a.tab.active"


def shop_url(category: str, quick_only: bool = True) -> str:
    cid = SHOP_CATEGORY_IDS.get(category)
    base = f"{SHOP_URL}?tab={cid}" if cid is not None else SHOP_URL
    return f"{base}&delivery_method=quick_delivery" if quick_only and cid is not None else base


def category_label(category: str) -> str:
    """보고서의 '랭킹' 열과 상태줄에 쓰는 이름."""
    return f"SHOP:{category.strip()}"


def _log_name(category: str, quick_only: bool) -> str:
    return f"SHOP '{category}'" + (" (빠른배송 필터)" if quick_only else "")


def _active_tab_text(page: Page) -> str:
    try:
        tab = page.locator(_ACTIVE_TAB).first
        if tab.count() == 0:
            return ""
        return tab.inner_text(timeout=2000).strip()
    except Exception:  # noqa: BLE001
        return ""


def open_category(page: Page, category: str, quick_only: bool = True) -> int:
    """SHOP 의 카테고리 목록을 연다. 첫 화면에 그려진 카드 수를 돌려준다 (0 이면 상품 없음).

    알고 있는 tab 번호로 바로 열고, 선택된 탭이 다른 카테고리면(번호가 바뀐 경우) 탭을 직접 눌러 맞춘 뒤 새 번호를 기억한다.
    """
    category = category.strip()
    name = _log_name(category, quick_only)
    if category in SHOP_CATEGORY_IDS:
        page.goto(shop_url(category, quick_only), wait_until="domcontentloaded")
        n = search.wait_cards(page, name)
        active = _active_tab_text(page)
        if active == category or not active:
            return n
        log.warning("tab=%s 로 열었더니 '%s' 탭이 선택됨 - '%s' 탭을 직접 누른다", SHOP_CATEGORY_IDS[category], active, category)
    else:
        page.goto(shop_url(category, quick_only), wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

    tab = page.locator(_TAB, has_text=re.compile(rf"^\s*{re.escape(category)}\s*$")).first
    if tab.count() == 0:
        raise ValueError(f"SHOP 에 '{category}' 탭이 없습니다")
    tab.scroll_into_view_if_needed()
    tab.click()
    page.wait_for_url(re.compile(r"[?&]tab=\d+"), timeout=10_000)
    m = re.search(r"[?&]tab=(\d+)", page.url)
    if m:
        SHOP_CATEGORY_IDS[category] = int(m.group(1))
        log.info("SHOP '%s' -> tab=%s", category, m.group(1))
    if quick_only and "quick_delivery" not in page.url:
        # 탭을 누르면 필터가 풀리므로 새 번호로 다시 연다
        page.goto(shop_url(category, quick_only), wait_until="domcontentloaded")
    return search.wait_cards(page, name)


def collect_products(page: Page, limit: int, category: str, quick_only: bool = True) -> list[RankedProduct]:
    """카테고리 목록을 화면 순서대로 limit 개까지 모은다. 부족하면 휠 스크롤로 다음 50개를 불러온다."""
    return search.collect_cards(page, limit, category_label(category), _log_name(category, quick_only))

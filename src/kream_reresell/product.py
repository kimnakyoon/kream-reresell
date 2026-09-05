"""상품 페이지: 체결 내역(빠른배송 거래량) 과 빠른배송 가격 A 를 읽고 구매 페이지로 넘어간다."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from .dates import parse_trade_date

log = logging.getLogger(__name__)

MAX_SALES_ROWS = 600        # 이보다 많이 읽지 않는다 (거래량이 큰 상품 보호)
MAX_SCROLL_ROUNDS = 40


class SkipProduct(Exception):
    """이 상품은 건너뛴다 (사유를 메시지로)."""


@dataclass
class SalesStats:
    fast_in_window: int      # 기간 내 빠른배송 체결 수
    total_in_window: int     # 기간 내 전체 체결 수
    rows_read: int           # 읽은 행 수
    reached_window_end: bool  # 기간 밖(더 오래된) 행까지 봤는지


def open_product(page: Page, url: str) -> str:
    try:
        page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeout:
        raise
    except PlaywrightError as e:
        # 이동 중 끊김 (net::ERR_ABORTED, 2026-09-05 재입찰 중 실측) - 잠깐 뒤 한 번 더
        log.info("상품 페이지 이동이 끊김 (%s) - 1.5초 뒤 다시 엶", str(e).splitlines()[0])
        page.wait_for_timeout(1500)
        page.goto(url, wait_until="domcontentloaded")
    try:
        page.get_by_role("button", name="구매하기", exact=True).first.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeout as e:
        raise SkipProduct("상품 페이지가 뜨지 않음 ('구매하기' 버튼 없음)") from e
    page.wait_for_timeout(300)
    title = page.title().split(" 정품")[0].strip()
    return title


# ---------------------------------------------------------------- 체결 내역

def read_sales_stats(page: Page, lookback_days: int, need: int) -> SalesStats:
    """'거래 내역 더보기' 패널을 열고 기간 내 빠른배송 체결 수를 센다.

    need 개를 채우거나 기간 밖 행이 나오면 더 읽지 않는다.
    """
    # 아직 리셀 거래(빠른배송)가 열리지 않은 상품은 체결 거래/입찰 표 자체가 없다.
    # 나중에 생길 수 있으므로 매 실행마다 확인은 하고, 없으면 기다리지 않고 바로 넘긴다.
    if page.get_by_text("체결 거래", exact=True).count() == 0:
        raise SkipProduct("체결 거래 표가 없는 상품 (아직 리셀 거래 없음) - 바로 넘김")
    # 본문의 체결 표는 role=tabpanel 이 아니라 _SALES_ROWS_JS 로 잡히지 않는다 (2026-09-04 실측) - 여기서 행을 기다리면
    # 매번 타임아웃만 채우므로 바로 누른다. 하이드레이션 전이라 클릭이 안 먹으면 아래 재시도가 받아 준다.
    more = page.get_by_role("button", name="거래 내역 더보기").first
    for attempt in range(3):
        if "/products/" not in page.url:
            raise SkipProduct(f"'거래 내역 더보기' 를 누르자 다른 페이지로 넘어감: {page.url}")
        try:
            more.scroll_into_view_if_needed(timeout=3000)
            more.click(timeout=3000)
            # 패널 루트 클래스는 숨겨진 모바일 복제본에도 붙어 있어 locator 의 visible 판정을 쓸 수 없다
            page.wait_for_function(_SALES_PANEL_VISIBLE_JS, timeout=4000)
            break
        except PlaywrightTimeout:
            log.debug("거래 내역 패널 열기 재시도 %d", attempt + 1)
            page.wait_for_timeout(700)
    else:
        raise SkipProduct("'거래 내역 더보기' 패널이 열리지 않음")
    # 패널이 뜬 뒤 행이 그려질 때까지만 기다린다 (실측 0.3초쯤). 본문에 '체결 거래' 표가 있는 상품이니 행이 있어야 한다 -
    # 3초가 지나도 비어 있으면 사이트가 내역을 안 준 것 (2026-09-05 실측: 0행이 나온 시간대에 구매 페이지도 안 그려짐).
    # 0건으로 세면 [재입찰]이 기준 미달로 입찰을 지워 버리므로 판단 불가로 넘긴다.
    try:
        page.wait_for_function(f"() => ({_SALES_ROWS_JS})().length > 0", timeout=3000)
    except PlaywrightTimeout:
        _close_sales_panel(page)
        raise SkipProduct("체결 내역 패널에 행이 없음 (내역을 불러오지 못함?)") from None

    cutoff = datetime.now() - timedelta(days=lookback_days)
    fast = total = 0
    reached_end = False
    rows: list[dict] = []
    prev_count = -1
    stale = 0
    for _ in range(MAX_SCROLL_ROUNDS):
        rows = page.evaluate(_SALES_ROWS_JS)
        fast = total = 0
        reached_end = False
        for r in rows:
            when = parse_trade_date(r["date"])
            if when is None:
                continue
            if when < cutoff:
                reached_end = True
                break
            total += 1
            if r["fast"]:
                fast += 1
        if reached_end or fast >= need or len(rows) >= MAX_SALES_ROWS:
            break
        if len(rows) == prev_count:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
        prev_count = len(rows)
        page.evaluate(_SCROLL_SALES_JS)
        page.wait_for_timeout(700)

    stats = SalesStats(fast, total, len(rows), reached_end)
    log.info("체결 내역: %d행 읽음, %d일 내 빠른배송 %d건 / 전체 %d건%s",
             stats.rows_read, lookback_days, fast, total, "" if reached_end else " (기간 끝까지 못 봄)")
    _close_sales_panel(page)
    return stats


def _close_sales_panel(page: Page) -> None:
    """패널 제목('거래 및 입찰 내역') 옆의 이름 없는 X 버튼을 누른다. Escape 는 안 먹는다."""
    for _ in range(3):
        if not page.evaluate(_SALES_PANEL_VISIBLE_JS):
            return
        page.evaluate(_CLICK_PANEL_CLOSE_JS)
        page.wait_for_timeout(400)
    if page.evaluate(_SALES_PANEL_VISIBLE_JS):
        raise SkipProduct("체결 내역 패널을 닫지 못함")


_CLICK_PANEL_CLOSE_JS = r"""
() => {
  const btn = document.querySelector('button.product-transaction-history-drawer__header-close')
           || document.querySelector('.product-transaction-history-drawer a[aria-label="닫기"], .bottom-sheet__layer--open a[aria-label="닫기"]');
  if (!btn) return false;
  btn.click();
  return true;
}
"""


# 패널의 '체결 거래' 탭 행: 옵션 / 가격 / (빠른배송) / 거래일.
# 화면 오른쪽에 슬라이드로 뜨는 패널은 DOM 끝쪽에 붙으므로, '거래일' 헤더를 가진
# tabpanel 중 행이 가장 많은 것을 고른다 (상품 페이지 본문의 5행짜리와 구분).
_SALES_ROWS_JS = r"""
() => {
  const dateRe = /(\d+\s*(분|시간|일|주|개월|달|년)\s*전|방금|\d{2,4}[./-]\d{1,2}[./-]\d{1,2})/;
  const panels = [...document.querySelectorAll('[role=tabpanel]')]
    .filter(p => p.innerText.includes('거래일'));
  let best = null, bestRows = [];
  for (const p of panels) {
    const leaves = [...p.querySelectorAll('*')].filter(e => e.children.length === 0 && dateRe.test(e.textContent.trim()));
    const rows = [];
    for (const leaf of leaves) {
      let row = leaf;
      for (let i = 0; i < 5 && row.parentElement && row.parentElement !== p; i++) {
        row = row.parentElement;
        if (/\d[\d,]*원/.test(row.innerText)) break;
      }
      const text = row.innerText;
      const pm = text.match(/(\d[\d,]*)원/);
      rows.push({ price: pm ? parseInt(pm[1].replace(/,/g, ''), 10) : null,
                  fast: text.includes('빠른배송'),
                  date: leaf.textContent.trim() });
    }
    if (rows.length > bestRows.length) { best = p; bestRows = rows; }
  }
  return bestRows;
}
"""

_SCROLL_SALES_JS = r"""
() => {
  const panels = [...document.querySelectorAll('[role=tabpanel]')].filter(p => p.innerText.includes('거래일'));
  const p = panels.sort((a, b) => b.innerText.length - a.innerText.length)[0];
  if (!p) return false;
  const last = p.lastElementChild;
  if (last) last.scrollIntoView({ block: 'end' });
  // 패널이 자체 스크롤 영역이면 그것도 내린다
  let el = p;
  while (el && el !== document.body) {
    const cs = getComputedStyle(el);
    if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight) { el.scrollTop = el.scrollHeight; }
    el = el.parentElement;
  }
  window.scrollBy(0, 400);
  return true;
}
"""

_SALES_PANEL_VISIBLE_JS = r"""
() => {
  // 같은 패널이 모바일용(display:none)과 PC용 drawer 두 벌 있다 - 실제로 보이는 것만 본다
  return [...document.querySelectorAll('.product-transaction-history-drawer, .bottom-sheet__layer--open')]
    .some(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
}
"""


# ---------------------------------------------------------------- 구매하기 모달 -> A

def read_price_a_and_go_to_buy(page: Page, product_id: int) -> int:
    """'구매하기' 모달에서 빠른배송 가격 A 를 읽고, 일반배송을 골라 구매 페이지로 넘어간다.

    모달 루트는 .bottom-sheet__layer--open.layer-option-picker (사이즈 목록 + 배송 선택 + 버튼).
    """
    buy = page.get_by_role("button", name="구매하기", exact=True).first
    modal = page.locator(MODAL_SELECTOR).first
    for attempt in range(3):
        try:
            buy.scroll_into_view_if_needed()
            buy.click()
            modal.wait_for(state="visible", timeout=4000)
        except PlaywrightTimeout:
            log.debug("구매하기 모달 열기 재시도 %d", attempt + 1)
            continue
        # 사이즈 옵션 상품(신발 등)은 모달에 사이즈 목록만 있고 ONE SIZE / '장바구니 담기' 가 없다.
        # ONE SIZE 가 그려지면 바로 진행하고, 2초 안에 안 나오는데 내용은 있으면 옵션 상품으로 보고 건너뛴다.
        try:
            page.wait_for_function(
                f"() => {{ const m = document.querySelector('{MODAL_SELECTOR}');"
                " return !!m && m.innerText.includes('ONE SIZE'); }", timeout=2000)
        except PlaywrightTimeout:
            if len(modal.inner_text().strip()) > 20:
                raise SkipProduct("옵션(사이즈)이 있는 상품 - 지금은 ONE SIZE 상품만 진행") from None
            log.debug("구매하기 모달 내용 대기 재시도 %d", attempt + 1)
            continue
        try:
            modal.get_by_role("button", name="장바구니 담기").first.wait_for(state="visible", timeout=4000)
            break
        except PlaywrightTimeout:
            log.debug("구매하기 모달 내용 대기 재시도 %d", attempt + 1)
    else:
        raise SkipProduct("구매하기 모달이 뜨지 않음")
    page.wait_for_timeout(400)

    if "ONE SIZE" not in modal.inner_text():
        raise SkipProduct("옵션(사이즈)이 있는 상품 - 지금은 ONE SIZE 상품만 진행")
    # ONE SIZE 가 이미 선택돼 있어도 한 번 눌러 확실히 한다
    modal.get_by_text("ONE SIZE", exact=False).first.click()
    page.wait_for_timeout(400)

    price_a = _parse_fast_price(modal.inner_text())
    if price_a is None:
        raise SkipProduct("빠른배송 가격을 읽지 못함 (빠른배송 판매자 없음?)")
    log.info("A(빠른배송 가격) = %s원", f"{price_a:,}")

    modal.get_by_text("일반배송", exact=False).first.click()
    page.wait_for_timeout(400)
    go = modal.get_by_role("button", name=re.compile("구매 입찰")).first
    try:
        go.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout as e:
        raise SkipProduct("'즉시 구매 / 구매 입찰' 버튼이 없음") from e
    go.click()
    page.wait_for_url(re.compile(rf"/buy/{product_id}"), timeout=15_000)
    try:
        page.get_by_text("즉시 판매가", exact=True).first.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeout as e:
        raise SkipProduct("구매 페이지가 뜨지 않음 ('즉시 판매가' 없음)") from e
    page.wait_for_timeout(300)
    return price_a


MODAL_SELECTOR = ".bottom-sheet__layer--open.layer-option-picker"


def _parse_fast_price(modal_text: str) -> int | None:
    """빠른배송 ~ 일반배송 사이의 가격 중 '95점' 이 붙지 않은 첫 가격."""
    start = modal_text.find("빠른배송")
    if start < 0:
        return None
    end = modal_text.find("일반배송", start)
    segment = modal_text[start:end if end > 0 else None]
    for m in re.finditer(r"(95점\s*)?(\d[\d,]*)\s*원", segment):
        if not m.group(1):
            return int(m.group(2).replace(",", ""))
    return None


def read_price_b(page: Page) -> int:
    """구매 페이지 상단의 '즉시 판매가' = B."""
    text = page.locator("body").inner_text()
    m = re.search(r"즉시 판매가\s*([\d,]+)\s*원", text)
    if not m:
        raise SkipProduct("즉시 판매가(B)를 읽지 못함 (구매 입찰 없음?)")
    price_b = int(m.group(1).replace(",", ""))
    log.info("B(즉시 판매가) = %s원", f"{price_b:,}")
    return price_b

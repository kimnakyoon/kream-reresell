"""상품 페이지: 체결 내역(빠른배송 거래량) 과 빠른배송 가격 A 를 읽고 구매 페이지로 넘어간다.

옵션(사이즈)이 있는 상품 (2026-09-05 실측, 신발 '(W) 나이키 토탈 90'):
  - '거래 내역 더보기' 패널 위에 옵션 선택 버튼(.detail-size, 처음엔 '모든 옵션')이 있다. 누르면 '옵션 선택' 레이어에
    '모든 옵션' 과 옵션들(W220 … W290) 이 button.size_list_item 으로 나오고, 하나를 고르면 레이어가 닫히며
    체결 표가 그 옵션의 거래만 보여 준다 (행 첫 줄 = 옵션명). 옵션마다 이렇게 골라 30일 빠른배송 건수를 센다.
  - 구매하기 모달에는 ONE SIZE 대신 옵션 목록(.select_option_picker: 옵션명 + 즉시 구매가)이 있고, 하나를 고르면
    ONE SIZE 상품과 똑같이 빠른배송/일반배송 가격과 '장바구니 담기' 가 나온다. 이후는 같다.
  - 구매 페이지 주소는 /buy/{id}?size=240 처럼 옵션의 값(product_option.key) 을 쓴다 - 화면 표기(W240)와 다르다.
    그래서 주소를 직접 만들지 않고 모달을 거쳐 도착한 주소에서 size 값을 읽어 기록해 둔다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from .dates import parse_trade_date
from .store import ONE_SIZE

log = logging.getLogger(__name__)

MAX_SALES_ROWS = 600        # 이보다 많이 읽지 않는다 (거래량이 큰 상품 보호)
SALES_PAGE_SIZE = 50        # 체결 표는 50행씩 불러온다 (2026-09-05 실측). 50의 배수가 아니면 더 불러올 것이 없다
MAX_SCROLL_ROUNDS = 40
ALL_OPTIONS_LABEL = "모든 옵션"


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


def on_product_page(page: Page, product_id: int) -> bool:
    return urlparse(page.url).path.rstrip("/") == f"/products/{product_id}"


# ---------------------------------------------------------------- 체결 내역

def read_sales_stats(page: Page, lookback_days: int, need: int, option: str | None = None) -> SalesStats:
    """'거래 내역 더보기' 패널을 열고 기간 내 빠른배송 체결 수를 센다. option 을 주면 그 옵션(사이즈)의 거래만 센다.

    need 개를 채우거나 기간 밖 행이 나오면 더 읽지 않는다. 끝나면 패널을 닫는다.
    """
    open_sales_panel(page)
    if option:
        select_option(page, option)
    stats = count_sales(page, lookback_days, need, option)
    close_sales_panel(page)
    return stats


def open_sales_panel(page: Page) -> None:
    """'거래 내역 더보기' 패널을 연다 (이미 열려 있으면 그대로). 행이 그려질 때까지만 기다린다."""
    if page.evaluate(_SALES_PANEL_VISIBLE_JS):
        return
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
        close_sales_panel(page)
        raise SkipProduct("체결 내역 패널에 행이 없음 (내역을 불러오지 못함?)") from None


def list_options(page: Page) -> list[str]:
    """패널(열려 있어야 함)의 옵션 선택 버튼으로 옵션(사이즈) 목록을 읽는다. 옵션이 없는(ONE SIZE) 상품이면 빈 목록.

    '옵션 선택' 레이어를 열어 읽고, 이미 선택돼 있는 '모든 옵션' 을 다시 눌러 닫는다 (선택은 그대로).
    """
    title = page.evaluate(_OPTION_PICKER_TITLE_JS)
    if title is None:
        return []
    labels = _open_option_layer(page)
    if not labels:
        return []
    # 레이어를 닫는다: 지금 선택된 항목(처음엔 '모든 옵션')을 누르면 선택이 바뀌지 않은 채 닫힌다
    current = title if title in labels else ALL_OPTIONS_LABEL
    _click_option_in_layer(page, current)
    try:
        page.wait_for_function(f"() => !({_OPTION_LAYER_JS})()", timeout=3000)
    except PlaywrightTimeout:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    options = [x for x in labels if x != ALL_OPTIONS_LABEL]
    if options == [ONE_SIZE]:
        return []
    return options


def select_option(page: Page, label: str) -> None:
    """패널의 옵션 선택 레이어에서 label 을 골라 체결 표를 그 옵션의 거래로 바꾼다."""
    if page.evaluate(_OPTION_PICKER_TITLE_JS) == label and page.evaluate(_ROWS_OPTION_MATCH_JS, label):
        return
    labels = _open_option_layer(page)
    if label not in labels:
        page.keyboard.press("Escape")
        raise SkipProduct(f"옵션 선택 레이어에 '{label}' 이 없음 (있는 것: {', '.join(labels[:8])})")
    _click_option_in_layer(page, label)
    try:
        page.wait_for_function(f"(label) => ({_OPTION_PICKER_TITLE_JS})() === label", arg=label, timeout=5000)
    except PlaywrightTimeout:
        raise SkipProduct(f"옵션 '{label}' 을 골랐는데 패널 제목이 바뀌지 않음") from None
    # 표가 그 옵션의 행으로 바뀔 때까지 (실측 즉시). 거래가 하나도 없는 옵션이면 행이 없을 수 있어 짧게만 기다린다.
    try:
        page.wait_for_function(_ROWS_OPTION_MATCH_JS, arg=label, timeout=3000)
    except PlaywrightTimeout:
        rows = page.evaluate(_SALES_ROWS_JS)
        others = [r for r in rows if r.get("option") and r["option"] != label]
        if others:
            raise SkipProduct(f"옵션 '{label}' 을 골랐는데 표에 다른 옵션({others[0]['option']}) 행이 남아 있음") from None
        log.info("옵션 %s: 체결 행 없음", label)


def count_sales(page: Page, lookback_days: int, need: int, option: str | None = None) -> SalesStats:
    """열려 있는 패널의 체결 표에서 기간 내 빠른배송 체결 수를 센다. option 을 주면 다른 옵션 행은 세지 않는다."""
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
            if option and r.get("option") and r["option"] != option:
                continue
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
        if len(rows) == 0 or len(rows) % SALES_PAGE_SIZE != 0:
            # 마지막 페이지가 꽉 차지 않았다 = 표가 끝났다. 스크롤해도 더 안 나오니 (3번 × 0.7초) 기다리지 않는다
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
    log.info("체결 내역%s: %d행 읽음, %d일 내 빠른배송 %d건 / 전체 %d건%s", f" [{option}]" if option else "",
             stats.rows_read, lookback_days, fast, total, "" if reached_end else " (기간 끝까지 못 봄)")
    return stats


def close_sales_panel(page: Page) -> None:
    """패널 제목('거래 및 입찰 내역') 옆의 이름 없는 X 버튼을 누른다. Escape 는 안 먹는다."""
    for _ in range(3):
        if not page.evaluate(_SALES_PANEL_VISIBLE_JS):
            return
        page.evaluate(_CLICK_PANEL_CLOSE_JS)
        page.wait_for_timeout(400)
    if page.evaluate(_SALES_PANEL_VISIBLE_JS):
        raise SkipProduct("체결 내역 패널을 닫지 못함")


_close_sales_panel = close_sales_panel   # 예전 이름


def _open_option_layer(page: Page) -> list[str]:
    """패널의 옵션 선택 버튼을 눌러 레이어를 열고 항목 글자를 돌려준다 ('모든 옵션' 포함)."""
    for attempt in range(3):
        if not page.evaluate(_CLICK_OPTION_PICKER_JS):
            raise SkipProduct("패널에 옵션 선택 버튼이 없음")
        try:
            page.wait_for_function(f"() => (({_OPTION_LAYER_JS})() || {{}}).labels?.length > 0", timeout=3000)
            break
        except PlaywrightTimeout:
            log.debug("옵션 선택 레이어 열기 재시도 %d", attempt + 1)
            page.wait_for_timeout(500)
    else:
        raise SkipProduct("옵션 선택 레이어가 열리지 않음")
    page.wait_for_timeout(200)
    layer = page.evaluate(_OPTION_LAYER_JS) or {}
    return [str(x) for x in layer.get("labels", [])]


def _click_option_in_layer(page: Page, label: str) -> None:
    if not page.evaluate(_CLICK_OPTION_IN_LAYER_JS, label):
        raise SkipProduct(f"옵션 선택 레이어에서 '{label}' 을 누르지 못함")
    page.wait_for_timeout(300)


_CLICK_PANEL_CLOSE_JS = r"""
() => {
  const btn = document.querySelector('button.product-transaction-history-drawer__header-close')
           || document.querySelector('.product-transaction-history-drawer a[aria-label="닫기"], .bottom-sheet__layer--open a[aria-label="닫기"]');
  if (!btn) return false;
  btn.click();
  return true;
}
"""

# 보이는 패널(drawer) 하나
_VISIBLE_DRAWER_JS = r"""
[...document.querySelectorAll('.product-transaction-history-drawer, .bottom-sheet__layer--open')]
    .find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && el.querySelector('.detail-size'); })
"""

# 패널의 옵션 선택 버튼 제목 ('모든 옵션' / 고른 옵션). 버튼이 없으면(ONE SIZE 상품) null
_OPTION_PICKER_TITLE_JS = r"""
() => {
  const d = %s;
  if (!d) return null;
  const t = d.querySelector('.detail-size');
  return t ? t.innerText.trim() : null;
}
""" % _VISIBLE_DRAWER_JS

_CLICK_OPTION_PICKER_JS = r"""
() => {
  const d = %s;
  const t = d && d.querySelector('.detail-size');
  if (!t) return false;
  t.click();
  return true;
}
""" % _VISIBLE_DRAWER_JS

# 열려 있는 '옵션 선택' 레이어 (구매하기 모달 .layer-option-picker 와 다르다): 항목 글자와 선택된 항목
_OPTION_LAYER_JS = r"""
() => {
  const layer = [...document.querySelectorAll('.bottom-sheet__layer--open')]
    .find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && el.querySelector('button.size_list_item'); });
  if (!layer) return null;
  const items = [...layer.querySelectorAll('button.size_list_item')];
  return { labels: items.map(b => b.innerText.trim()),
           selected: (items.find(b => b.classList.contains('size_list_item--selected')) || {}).innerText?.trim() || null };
}
"""

_CLICK_OPTION_IN_LAYER_JS = r"""
(label) => {
  const layer = [...document.querySelectorAll('.bottom-sheet__layer--open')]
    .find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && el.querySelector('button.size_list_item'); });
  if (!layer) return false;
  const b = [...layer.querySelectorAll('button.size_list_item')].find(b => b.innerText.trim() === label);
  if (!b) return false;
  b.click();
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
      const first = text.split('\n')[0].trim();
      rows.push({ price: pm ? parseInt(pm[1].replace(/,/g, ''), 10) : null,
                  fast: text.includes('빠른배송'),
                  option: /원$/.test(first) ? '' : first,
                  date: leaf.textContent.trim() });
    }
    if (rows.length > bestRows.length) { best = p; bestRows = rows; }
  }
  return bestRows;
}
"""

# 표의 행이 전부 주어진 옵션의 행인지 (행이 하나는 있어야 true)
_ROWS_OPTION_MATCH_JS = r"""
(label) => {
  const rows = (%s)();
  return rows.length > 0 && rows.every(r => r.option === label);
}
""" % _SALES_ROWS_JS.strip()

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

def read_price_a_and_go_to_buy(page: Page, product_id: int, option: str | None = None) -> int:
    """'구매하기' 모달에서 빠른배송 가격 A 를 읽고, 일반배송을 골라 구매 페이지로 넘어간다.

    모달 루트는 .bottom-sheet__layer--open.layer-option-picker (사이즈 목록 + 배송 선택 + 버튼).
    option 을 주면 모달의 옵션 목록에서 그 옵션(화면 표기, 예 W240)을 고른 뒤 읽는다. 없으면 ONE SIZE 상품이어야 한다.
    """
    want = option or ONE_SIZE
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
        # 모달에 고를 옵션(ONE SIZE 또는 사이즈)이 그려질 때까지. 2초 안에 안 나오는데 내용은 있으면 옵션 구성이 다른 것
        try:
            page.wait_for_function(_MODAL_HAS_OPTION_JS, arg=want, timeout=2000)
        except PlaywrightTimeout:
            if len(modal.inner_text().strip()) > 20:
                if option:
                    raise SkipProduct(f"구매하기 모달에 옵션 '{option}' 이 없음") from None
                raise SkipProduct("옵션(사이즈)이 있는 상품인데 옵션 목록을 읽지 못함 (모달에 ONE SIZE 없음)") from None
            log.debug("구매하기 모달 내용 대기 재시도 %d", attempt + 1)
            continue
        break
    else:
        raise SkipProduct("구매하기 모달이 뜨지 않음")
    page.wait_for_timeout(400)

    # 옵션(또는 ONE SIZE)을 누른다 - 이미 선택돼 있어도 한 번 눌러 확실히 한다. 누르면 빠른배송/일반배송 가격이 그려진다
    if not page.evaluate(_CLICK_MODAL_OPTION_JS, want):
        raise SkipProduct(f"구매하기 모달에서 '{want}' 을 누르지 못함")
    try:
        page.wait_for_function(f"() => {{ const m = document.querySelector('{MODAL_SELECTOR}');"
                               " return !!m && m.innerText.includes('빠른배송') && m.innerText.includes('일반배송'); }",
                               timeout=4000)
    except PlaywrightTimeout:
        raise SkipProduct(f"'{want}' 을 골랐는데 배송 방법(빠른배송/일반배송)이 그려지지 않음") from None
    page.wait_for_timeout(300)

    price_a = _parse_fast_price(modal.inner_text())
    if price_a is None:
        raise SkipProduct("빠른배송 가격을 읽지 못함 (빠른배송 판매자 없음?)")
    log.info("A(빠른배송 가격)%s = %s원", f" [{option}]" if option else "", f"{price_a:,}")

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
    if option:
        # 구매 페이지 상단의 옵션 표기가 고른 것과 같은지 (다른 사이즈에 입찰하지 않도록)
        body = page.locator("body").inner_text()
        if not re.search(rf"(^|\n)\s*{re.escape(option)}\s*(\n|$)", body):
            raise SkipProduct(f"구매 페이지의 옵션 표기가 '{option}' 이 아님 (주소 {page.url})")
    return price_a


MODAL_SELECTOR = ".bottom-sheet__layer--open.layer-option-picker"

# 모달에 고를 항목(ONE SIZE 또는 옵션명)이 있는지. 옵션 항목은 .select_option_picker (옵션명 + 즉시 구매가) 이고,
# ONE SIZE 상품은 'ONE SIZE' 글자만 있다 (2026-09-05 실측)
_MODAL_HAS_OPTION_JS = r"""
(label) => {
  const m = document.querySelector('.bottom-sheet__layer--open.layer-option-picker');
  if (!m) return false;
  const items = [...m.querySelectorAll('.select_option_picker')];
  if (items.some(e => e.innerText.trim().split('\n')[0].trim() === label)) return true;
  return items.length === 0 && m.innerText.includes(label);
}
"""

_CLICK_MODAL_OPTION_JS = r"""
(label) => {
  const m = document.querySelector('.bottom-sheet__layer--open.layer-option-picker');
  if (!m) return false;
  const items = [...m.querySelectorAll('.select_option_picker')];
  const hit = items.find(e => e.innerText.trim().split('\n')[0].trim() === label);
  if (hit) { hit.click(); return true; }
  if (items.length) return false;
  // ONE SIZE 상품: 'ONE SIZE' 글자를 가진 가장 안쪽 요소를 누른다
  const leaves = [...m.querySelectorAll('*')].filter(e => e.children.length === 0 && e.textContent.trim() === label);
  const el = leaves[0] || [...m.querySelectorAll('*')].find(e => e.innerText && e.innerText.trim().startsWith(label));
  if (!el) return false;
  el.click();
  return true;
}
"""


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


def size_from_url(url: str) -> str:
    """구매 페이지 주소의 size 값 (ONE SIZE 상품은 'ONE SIZE', 옵션 상품은 '240' 같은 값). 없으면 빈 문자열."""
    qs = parse_qs(urlparse(url).query)
    return (qs.get("size") or [""])[0].strip()

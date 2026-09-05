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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from .dates import parse_trade_date
from .debug import dump
from .store import ONE_SIZE

log = logging.getLogger(__name__)

MAX_SALES_ROWS = 600        # 이보다 많이 읽지 않는다 (거래량이 큰 상품 보호)
SALES_PAGE_SIZE = 50        # 체결 표는 50행씩 불러온다 (2026-09-05 실측). 50의 배수가 아니면 더 불러올 것이 없다
MAX_SCROLL_ROUNDS = 40
ALL_OPTIONS_LABEL = "모든 옵션"


class SkipProduct(Exception):
    """이 상품은 건너뛴다 (사유를 메시지로)."""


class NoPriceB(SkipProduct):
    """구매 페이지는 떴는데 '즉시 판매가'(B) 가 없다. [재입찰]은 이 경우 입찰을 지운다."""


class NoFastDelivery(SkipProduct):
    """구매하기 모달은 떴는데 빠른배송이 없거나(일반배송만 그려짐) 빠른배송 가격이 없다 = 지금 빠른배송 판매자가 없어 A 를 정할 수 없다.

    [재입찰]은 이 경우 입찰을 지운다 (사용자 결정 2026-09-06). [입찰]·[입찰취소]는 SkipProduct 와 같이 건너뜀·확인필요.
    """


class SalesNotLoaded(SkipProduct):
    """체결 내역 패널이 열렸는데 사이트가 내역을 내려주지 않았다 (오류 표시 또는 끝까지 빈 채).

    거래가 없는 게 아니라 '판단 불가' 다 - 0건으로 세면 [재입찰]이 기준 미달로 입찰을 지워 버린다. [입찰]·[재입찰] 은 이게
    연달아 나면 사이트가 응답을 안 주는 시간대로 보고 멈춰 기다린다 (sitewait).
    """


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
        # 모든 옵션 표의 첫 페이지(패널을 열 때 이미 와 있음)에서 정해지면 옵션을 고르지 않는다 (sales 요청 1건 절약)
        pre, _pages = count_sales_by_option(page, lookback_days, need, [option], before_page=_no_more_pages)
        if option in pre:
            close_sales_panel(page)
            return pre[option]
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
    # 패널이 뜬 뒤 표가 그려질 때까지 기다린다 (정상이면 0.3초쯤). 본문에 '체결 거래' 표가 있는 상품이니 행이 있어야 한다.
    _await_sales_table(page, SALES_LOAD_TIMEOUT_MS)


def sales_available(page: Page, url: str) -> tuple[bool, str]:
    """사이트가 체결 내역을 주고 있는지 - 상품 페이지를 열어 패널 표가 그려지는지 본다. (주는지, 근거).

    [입찰]·[재입찰] 이 멈춰 기다리는 동안의 확인과, [재입찰] 이 '즉시 판매가 없음' 으로 지우기 전 확인에 쓴다.
    """
    try:
        open_product(page, url)
        open_sales_panel(page)
    except SalesNotLoaded as e:
        return False, str(e)
    except SkipProduct as e:
        if "체결 거래 표가 없는" in str(e):
            return True, "체결 거래 표가 없는 상품"   # 리셀 거래가 없는 상품 - 사이트 문제가 아니다
        return False, str(e)
    except (PlaywrightTimeout, PlaywrightError) as e:
        return False, f"상품 페이지를 열지 못함: {str(e).splitlines()[0]}"
    try:
        close_sales_panel(page)
    except SkipProduct:
        pass
    return True, "체결 내역 패널이 그려짐"


SALES_LOAD_TIMEOUT_MS = 10_000   # 패널을 연 뒤 표(행 · 빈 표 · 오류 표시)가 그려지길 기다리는 최대 시간. 오류 표시는 5초쯤 뒤 뜬다
OPTION_LOAD_TIMEOUT_MS = 6_000   # 옵션을 고른 뒤 그 옵션의 표가 그려지길 기다리는 최대 시간 (정상이면 즉시)
EMPTY_TABLE_GRACE_MS = 6_000     # 거래가 있는 상품인데 모든 옵션 표가 빈 채로 열렸을 때 행이 오길 더 기다리는 시간 (정상이면 0.5초쯤)


def _await_sales_table(page: Page, timeout_ms: int, option: str | None = None) -> None:
    """패널의 체결 표가 그려질 때까지 기다린다. 행이 있거나 빈 표(거래가 정말 없음)면 돌아온다.

    사이트가 내역을 안 주는 시간대(2026-09-05 실측)에는 표가 끝까지 빈 채이거나 몇 초 뒤 '불러오는 중 문제가 생겼어요 / 다시 시도'
    오류 표시가 뜬다 - 전체 표면 [다시 시도] 를 한 번 눌러 보고 (옵션을 고른 뒤에는 선택이 풀릴 수 있어 누르지 않음), 그래도
    안 오면 SalesNotLoaded (판단 불가). 0건으로 세면 [재입찰] 이 기준 미달로 입찰을 지우고, 옵션 상품은 S~XXL 이 전부
    '0건 < 15건' 으로 기록되던 문제 (2026-09-05).
    """
    subject = f"옵션 '{option}' 의 체결 내역" if option else "체결 내역"
    state = _wait_sales_state(page, timeout_ms, option)
    if state == "error" and option is None:
        log.info("체결 내역 패널에 '불러오는 중 문제가 생겼어요' - [다시 시도] 한 번")
        _click_panel_retry(page)
        state = _wait_sales_state(page, timeout_ms)
    if state == "rows":
        return
    if state.startswith("empty") and option is None:
        # 상품 페이지 머리의 '거래 N' 이 0 이 아닌데 모든 옵션 표가 비었다. 패널은 열리자마자 '체결된 거래가 아직 없습니다' 로
        # 그려지고 0.5초쯤 뒤에 sales 응답이 와서 행이 채워지므로(2026-09-05 실측) 행이 올 때까지 조금 더 기다리고, 그래도 비면
        # 사이트가 내역을 안 준 것 (스로틀) - 0건으로 세면 옵션 전부 '0건 < 15건' 이 되고 [재입찰]은 입찰을 지운다
        trades = page.evaluate(_TRADE_COUNT_JS)
        if trades:
            state = _wait_sales_state(page, EMPTY_TABLE_GRACE_MS, want_rows=True)
            if state != "rows":
                try:
                    close_sales_panel(page)
                except SkipProduct:
                    pass
                raise SalesNotLoaded(f"체결 내역을 불러오지 못함 (거래 {trades:,}건인 상품인데 표가 빈 채)")
            return
    if state.startswith("empty"):
        log.info("%s: 행 없음 (%s)", subject, state.partition(":")[2].strip() or "빈 표")
        return
    if state == "closed":
        raise SkipProduct("체결 내역 패널이 열렸다가 사라짐")
    if state == "stale":
        rows = page.evaluate(_SALES_ROWS_JS)
        others = [r for r in rows if r.get("option") and r["option"] != option]
        raise SkipProduct(f"옵션 '{option}' 을 골랐는데 표에 다른 옵션({others[0]['option'] if others else '?'}) 행이 남아 있음")
    if option is None:
        try:
            close_sales_panel(page)
        except SkipProduct:
            log.debug("체결 내역 패널을 닫지 못함 - 판단 불가 사유를 그대로 알림")
    if state == "error":
        raise SalesNotLoaded(f"{subject}을 불러오지 못함 (패널에 '불러오는 중 문제가 생겼어요' 표시)")
    raise SalesNotLoaded(f"{subject}을 불러오지 못함 (표가 {timeout_ms // 1000}초 지나도 빈 채)")


def _wait_sales_state(page: Page, timeout_ms: int, option: str | None = None, want_rows: bool = False) -> str:
    """패널의 체결 표 상태가 정해질 때까지 기다린다: 'rows' (행 있음) / 'empty:…' (표는 그려졌는데 행 없음) / 'error'
    (불러오기 오류 표시) / 'closed' (패널 없음). 시간 안에 정해지지 않으면 그때 상태 ('loading', 옵션이면 'stale' 도).
    want_rows 면 빈 표도 아직 정해지지 않은 것으로 보고 행·오류·닫힘까지 기다린다."""
    js = _OPTION_STATE_JS if option else _SALES_PANEL_STATE_JS
    pending = "s === 'loading' || s === 'stale'" + (" || s.startsWith('empty')" if want_rows else "")
    try:
        handle = page.wait_for_function(
            f"(label) => {{ const s = ({js})(label); return ({pending}) ? false : s; }}",
            arg=option, timeout=timeout_ms)
        return str(handle.json_value())
    except PlaywrightTimeout:
        return str(page.evaluate(js, option))


def _click_panel_retry(page: Page) -> None:
    try:
        page.locator(".error-boundary-content__actions__retry").first.click(timeout=2000)
    except PlaywrightError:
        log.debug("[다시 시도] 버튼을 누르지 못함")


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
    # 표가 그 옵션의 행으로 바뀔 때까지 (실측 즉시). 거래가 하나도 없는 옵션이면 표는 그려지되 행이 없다
    _await_sales_table(page, OPTION_LOAD_TIMEOUT_MS, option=label)


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


def count_sales_by_option(page: Page, lookback_days: int, need: int, options: list[str],
                          before_page: Callable[[], None] | None = None) -> tuple[dict[str, SalesStats], int]:
    """열려 있는 패널의 '모든 옵션' 표에서 옵션마다 기간 내 빠른배송 수를 센다 - 옵션을 하나씩 고르기 전에 먼저 부른다.

    사이트 스로틀 대응(pacing): 옵션을 하나 고르면 sales 요청이 1건 나가는데, 모든 옵션 표의 첫 페이지는 패널을 열 때 이미
    와 있어 공짜다. 거기서 need 를 채운 옵션은 정해지고, 표가 기간 끝(또는 표 끝)까지 가면 모든 옵션이 정확히 정해진다.
    더 넘기는 페이지(50행 = sales 요청 1건)는 지금까지 본 거래 속도로 기간 끝까지 몇 페이지가 더 필요한지 어림해,
    그 수가 아직 안 정해진 옵션 수(= 하나씩 고를 때 드는 요청 수) 이하일 때만 넘긴다. 그래서 요청 수가 하나씩 고르는 것보다
    많아지지 않는다. before_page 는 페이지를 더 넘기기 직전에 부른다 (간격·예산). _StopPaging 을 내면 더 넘기지 않는다.

    반환: (정해진 옵션 → SalesStats, 읽은 페이지 수). 안 정해진 옵션은 select_option + count_sales 로 본다.
    """
    cutoff = datetime.now() - timedelta(days=lookback_days)
    pages = 1
    prev_count = -1
    stale = 0
    resolved: dict[str, SalesStats] = {}
    rows: list[dict] = []
    while True:
        rows = page.evaluate(_SALES_ROWS_JS)
        fast = dict.fromkeys(options, 0)
        total = dict.fromkeys(options, 0)
        reached_end = False
        oldest: datetime | None = None
        for r in rows:
            when = parse_trade_date(r["date"])
            if when is None:
                continue
            if when < cutoff:
                reached_end = True
                break
            oldest = when if oldest is None or when < oldest else oldest
            o = r.get("option") or ""
            if o not in fast:
                continue   # 표기가 옵션 목록과 다르면 세지 않는다 (그 옵션은 하나씩 고르는 쪽으로)
            total[o] += 1
            if r["fast"]:
                fast[o] += 1
        exhausted = len(rows) == 0 or len(rows) % SALES_PAGE_SIZE != 0   # 마지막 페이지가 꽉 차지 않았다 = 표가 끝났다
        done = reached_end or exhausted
        for o in options:
            if done or fast[o] >= need:
                resolved[o] = SalesStats(fast[o], total[o], len(rows), done)
        unresolved = [o for o in options if o not in resolved]
        if done or not unresolved or len(rows) >= MAX_SALES_ROWS:
            break
        # 기간 끝까지 몇 페이지가 더 필요한지 어림 (지금까지 본 행이 며칠치인지로)
        span_days = max((datetime.now() - oldest).total_seconds() / 86400, 0.05) if oldest else 0.05
        pages_total = -(-int(len(rows) / span_days * lookback_days) // SALES_PAGE_SIZE)
        more = max(pages_total - pages, 1)
        if more > len(unresolved):
            log.info("모든 옵션 표 %d페이지: 기간 끝까지 %d페이지쯤 더 필요해 안 정해진 옵션 %d개는 하나씩 봄",
                     pages, more, len(unresolved))
            break
        if len(rows) == prev_count:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
        prev_count = len(rows)
        if before_page:
            try:
                before_page()
            except _StopPaging:
                break
        page.evaluate(_SCROLL_SALES_JS)
        page.wait_for_timeout(700)
        if len(page.evaluate(_SALES_ROWS_JS)) > len(rows):
            pages += 1
    if resolved:
        log.info("모든 옵션 표 %d페이지(%d행)에서 %d/%d 옵션 정해짐: %s", pages, len(rows), len(resolved), len(options),
                 ", ".join(f"{o} {s.fast_in_window}건" for o, s in resolved.items()))
    return resolved, pages


class _StopPaging(Exception):
    """count_sales_by_option 의 before_page 가 내면 페이지를 더 넘기지 않는다."""


def _no_more_pages() -> None:
    raise _StopPaging


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

# 패널의 체결 표 상태 (2026-09-05 실측):
#  'closed'  - 보이는 패널이 없음
#  'error'   - 패널 안에 '불러오는 중 문제가 생겼어요 / 다시 시도' 오류 표시 (.error-boundary-content) - 사이트가 내역을 안 줌
#  'rows'    - 체결 행이 있음
#  'empty:…' - 표(머리글 '거래일' 또는 '없습니다' 류 안내)는 그려졌는데 행이 없음 - 그 옵션은 거래가 정말 없는 것
#  'loading' - 표 자리가 아직 비어 있음 (불러오는 중이면 tabpanel 글자가 하나도 없다). 오래 이어지면 사이트가 안 주는 것
_SALES_PANEL_STATE_JS = r"""
() => {
  const panel = [...document.querySelectorAll('.product-transaction-history-drawer, .bottom-sheet__layer--open')]
    .find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (!panel) return 'closed';
  if (panel.querySelector('.error-boundary-content') || panel.innerText.includes('불러오는 중 문제')) return 'error';
  if ((%s)().length > 0) return 'rows';
  const shown = [...panel.querySelectorAll('[role=tabpanel]')]
    .filter(p => p.offsetHeight > 0 && (p.innerText.includes('거래일') || /없습니다|없어요/.test(p.innerText)));
  const text = shown.map(p => p.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean).join(' / ');
  return text ? 'empty:' + text.slice(0, 80) : 'loading';
}
""" % _SALES_ROWS_JS.strip()

# 옵션을 고른 뒤의 표 상태: 위와 같되, 행이 있어도 전부 그 옵션의 행이 아니면 (예전 옵션 행이 남아 있음) 'stale'
_OPTION_STATE_JS = r"""
(label) => {
  const s = (%s)();
  if (s !== 'rows') return s;
  const rows = (%s)();
  return rows.every(r => r.option === label) ? 'rows' : 'stale';
}
""" % (_SALES_PANEL_STATE_JS.strip(), _SALES_ROWS_JS.strip())

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

# 상품 페이지 머리의 누적 거래 수 ('거래 2,984 ▲33,000원' - 2026-09-05 실측). 없으면 0
_TRADE_COUNT_JS = r"""
() => {
  const m = document.body.innerText.match(/거래\s+([\d,]+)/);
  return m ? parseInt(m[1].replace(/,/g, ''), 10) : 0;
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
        text = modal.inner_text()
        if "일반배송" in text and "빠른배송" not in text:
            # 일반배송만 있다 - 지금 빠른배송 판매자가 없는 것 (2026-09-06 실측, 마뗑킴 카드 월렛) → A 를 정할 수 없어 판단 불가
            raise NoFastDelivery(f"'{want}' 에 빠른배송이 없음 (일반배송만 그려짐 - 지금 빠른배송 판매자 없음)") from None
        raise SkipProduct(f"'{want}' 을 골랐는데 배송 방법(빠른배송/일반배송)이 그려지지 않음") from None
    page.wait_for_timeout(300)

    price_a = _parse_fast_price(modal.inner_text())
    if price_a is None:
        # 빠른배송 줄은 있는데 가격이 없다 - 지금 빠른배송 판매자가 없는 것
        raise NoFastDelivery(f"'{want}' 에 빠른배송 가격이 없음 (지금 빠른배송 판매자 없음)")
    log.info("A(빠른배송 가격)%s = %s원", f" [{option}]" if option else "", f"{price_a:,}")

    modal.get_by_text("일반배송", exact=False).first.click()
    page.wait_for_timeout(400)
    go = modal.get_by_role("button", name=re.compile("구매 입찰")).first
    try:
        go.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout as e:
        raise SkipProduct("'즉시 구매 / 구매 입찰' 버튼이 없음") from e
    go.click()
    _pass_model_number_check(page, product_id)
    try:
        page.wait_for_url(re.compile(rf"/buy/{product_id}"), timeout=15_000)
    except PlaywrightTimeout as e:
        dump(page, f"{product_id}_no_buy_page")
        raise SkipProduct(f"'즉시 구매 / 구매 입찰' 을 눌렀는데 구매 페이지로 넘어가지 않음 (지금 주소 {page.url})") from e
    try:
        page.get_by_text("즉시 판매가", exact=True).first.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeout as e:
        if urlparse(page.url).path.startswith(f"/buy/{product_id}"):
            # 구매 페이지에는 왔는데 즉시 판매가가 없다 (구매 입찰이 하나도 없음) - [재입찰]은 이 경우 입찰을 지운다
            raise NoPriceB("구매 페이지가 떴는데 '즉시 판매가' 가 없음") from e
        raise SkipProduct(f"구매 페이지가 뜨지 않음 ('즉시 판매가' 없음, 지금 주소 {page.url})") from e
    page.wait_for_timeout(300)
    if option:
        # 구매 페이지 상단의 옵션 표기가 고른 것과 같은지 (다른 사이즈에 입찰하지 않도록)
        body = page.locator("body").inner_text()
        if not re.search(rf"(^|\n)\s*{re.escape(option)}\s*(\n|$)", body):
            raise SkipProduct(f"구매 페이지의 옵션 표기가 '{option}' 이 아님 (주소 {page.url})")
    return price_a


MODEL_CHECK_BUTTON = "확인 후 계속"


def _pass_model_number_check(page: Page, product_id: int) -> None:
    """'즉시 구매 / 구매 입찰' 을 누른 뒤 뜨는 "모델번호를 확인하셨나요?" 확인창을 [확인 후 계속] 으로 넘긴다.

    같은 디자인의 다른 모델번호 상품이 있는 상품(의류 등, 2026-09-05 바람막이 실측)에서만 뜬다. 확인창이 뜨면 구매 페이지로
    넘어가지 않고 멈춰 있으므로, 구매 페이지로 넘어가거나 확인창이 뜰 때까지 기다렸다가 확인창이면 버튼을 누른다.
    """
    try:
        page.wait_for_function(_BUY_PAGE_OR_MODEL_CHECK_JS, arg=[product_id, MODEL_CHECK_BUTTON], timeout=8000)
    except PlaywrightTimeout:
        return  # 둘 다 아니면 뒤의 wait_for_url 이 판단한다
    if urlparse(page.url).path.startswith(f"/buy/{product_id}"):
        return
    button = page.get_by_role("button", name=MODEL_CHECK_BUTTON, exact=True).first
    try:
        button.wait_for(state="visible", timeout=3000)
        button.click()
        log.info("'모델번호를 확인하셨나요?' 확인창 - [%s] 누름", MODEL_CHECK_BUTTON)
    except PlaywrightTimeout:
        log.debug("모델번호 확인창 버튼을 찾았는데 누르지 못함")


# 구매 페이지로 넘어갔거나, "모델번호를 확인하셨나요?" 확인창의 [확인 후 계속] 버튼이 보이는지
_BUY_PAGE_OR_MODEL_CHECK_JS = r"""
([pid, label]) => {
  if (location.pathname.startsWith('/buy/' + pid)) return true;
  return [...document.querySelectorAll('button')].some(b => b.innerText.trim() === label && b.offsetParent !== null);
}
"""


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
        raise NoPriceB("즉시 판매가(B)를 읽지 못함 (구매 입찰 없음?)")
    price_b = int(m.group(1).replace(",", ""))
    log.info("B(즉시 판매가) = %s원", f"{price_b:,}")
    return price_b


def size_from_url(url: str) -> str:
    """구매 페이지 주소의 size 값 (ONE SIZE 상품은 'ONE SIZE', 옵션 상품은 '240' 같은 값). 없으면 빈 문자열."""
    qs = parse_qs(urlparse(url).query)
    return (qs.get("size") or [""])[0].strip()

"""[재입찰] 마이페이지 > 구매 내역 > 구매 입찰 탭의 입찰을 순서대로 돌며, 남에게 밀린 입찰의 희망가를 [입찰 변경하기] 로 올린다.

흐름 (2026-09-04 실측):
  1. /my/buying?tab=bidding 목록을 읽는다 (cancel.list_open_bids). 상품 ID 는 bids.json 기록 → 이번 실행의 캐시 →
     상세 API 응답(api/m/bids/{입찰번호}) 순으로 알아낸다. 옵션(사이즈) 상품은 구매 페이지 주소의 size 값(product_option.key,
     화면 표기 W240 이 아니라 240) 도 같이 알아야 해서, 그 값을 모르면 상세를 연다. 판정은 그 옵션의 거래량·가격으로 한다.
  2. 상품 페이지 → 구매하기 모달에서 최신 A(빠른배송 가격) → 구매 페이지(/buy/{상품ID}?size=옵션 값)에서 최신 B(즉시 판매가 = 지금
     가장 높은 구매 입찰가)를 읽어 마진(A−B > A×구간별 마진율)을 판정한다 (pipeline.judge_prices, 7초쯤). 밀리지 않은 입찰도 A 가 내려가
     마진이 기준 아래로 떨어졌을 수 있어 매번 본다 (사용자 결정 2026-09-06: A 변동도 항상 확인). 이때 거래량은 보지 않으므로 상품 페이지가
     여는 sales 요청을 빈 응답으로 채워(browser.sales_trimmed) 스로틀 대상 요청은 0건이다.
     B 가 내 희망가 이하이면 밀리지 않은 것: 마진이 기준을 충족하면 그대로 둔다 (순위유지, 사유에 A·마진을 남김), 기준 미달이면 지운다 (아래와 같은 방식).
     (구매 페이지만 먼저 열어 B 를 보던 빠른 확인은 2026-09-06 에 뺐다 - 어차피 같은 구매 페이지를 A 읽으러 다시 열게 되어 2초 낭비.)
  3. B 가 내 희망가보다 높으면(누가 더 비싸게 입찰함) 밀린 것: 마진이 기준 미달이면 지우고, 충족하면 상품 페이지를 다시 열어 (이번엔 sales 를 받아)
     최근 30일 빠른배송 건수까지 처음 입찰할 때와 똑같이 판정한다 (pipeline.check_sales). 거래량 미달이면 지운다.
  4. 조건이 맞으면 입찰 상세의 [입찰 변경하기] 버튼이 여는 것과 같은 주소
     /buy/{상품ID}?size={옵션 값}&bid={입찰번호}&from=changeBidding&type=bid&price={기존 희망가}
     로 가서 처음 입찰과 같은 화면을 채운다: 희망가 = 최신 B, 마감기한, 구매 입찰 계속 → 창고보관 → 포인트 최대 사용
     → 입찰하기 → 동의 3항목 → 입찰하기 (bid.fill_bid_form / choose_warehouse_and_points / submit_bid 그대로).
  5. 목록 끝까지 가면 한 사이클. 정한 횟수(max_cycles, GUI '재입찰 횟수' 칸 / --cycles) 만큼 또는 중지할 때까지 사이클을 반복하되, 사이클 시작 간격(설정, 기본 5분)을 지키고
     입찰 사이에도 2~4초 무작위로 쉰다 (봇 탐지 대비).

즉시 판매가가 없을 때: 구매 페이지에 와 있는데 '즉시 판매가' 가 안 그려지면 (사용자 결정, 2026-09-05) 그 입찰을 지운다
(입찰취소, 방식은 기준 미달 때와 같다). 단 지우기 전에 두 가지를 본다: (1) 상품 페이지의 체결 내역 패널이 그려지는지 - 안 그려지면
사이트가 응답을 안 주는 시간대라 즉시 판매가도 안 보이는 것이니 지우지 않고 판단 불가 (사용자 결정 2026-09-05, 멀쩡한 입찰이
지워지던 문제); (2) 상세 API 로 살아 있는 입찰인지 - 이미 체결·삭제된 입찰이면 건너뜀. 지우지 못하면 확인필요.
지운 상품은 bids.json 에서도 빼서 [입찰]이 조건이 맞으면 다시 넣을 수 있게 한다.
사이트가 응답을 안 주는 시간대(2026-09-05 실측: API 로 채우는 부분만 비는 상태가 계속 두드리는 동안 20분 넘게 이어졌고, 7분쯤 쉬면
풀렸다)에도 같은 일이 생기므로, 이렇게 지운 건도 판단 불가·오류와 함께 '연달아 난 수' 에 넣는다: 연달아 TROUBLE_STREAK 건 나오면
멈춘 채 5분마다 구매 페이지를 한 번 열어 보고, '즉시 판매가' 가 다시 그려지면 로그인 상태를 확인한 뒤 이어서 본다 (사용자 결정
2026-09-05: 시간 제한 없이 다시 줄 때까지 기다린다 - sitewait). 판단 불가·즉시 판매가 없음으로 끝난 입찰은 화면 스냅샷을 dumps/ 에 남긴다.
구매 페이지가 로그인 화면으로 넘어가면(로그인이 풀림) 다시 로그인하고 그 입찰을 한 번 더 본다. 목록 페이지가 로그인 화면으로
넘어가도 (로그인 화면 주소에도 returnUrl 로 tab=bidding 이 들어가 0건으로 읽히던 문제, 2026-09-05) 다시 로그인하고 목록을 다시 읽는다.

밀렸는데 기준(거래량 · 마진)에 못 미쳐 올릴 수 없는 입찰과, 밀리지 않았어도 마진(최신 A·B)이 기준에 못 미치는 입찰은 [입찰취소] 와 같은 방식으로 지운다
(상세의 '입찰 지우기' → 확인창 → DELETE 204 확인, cancel.delete_bid). 판단할 수 없는 경우(상품 페이지 오류 등)는 지우지 않는다.
밀렸고 기준도 충족하는데 [입찰 변경하기] 화면이 예상과 달라(상품명·옵션 불일치, 창고보관 확인 안 됨 등) 못 올린 입찰은
변경 화면을 다시 열어 한 번 더 시도하고(CHANGE_ATTEMPTS - 화면이 늦게 그려져 생기는 일시적 불일치가 대부분), 그래도 못 올리면
같은 방식으로 지운다 (사용자 결정 2026-09-06 - 밀린 채 두지 않음). 마지막 '입찰하기' 를 누른 뒤 결과가 불확실한 건은 지우지 않고 확인필요.
사이클이 끝날 때마다 그 사이클에 나간 스로틀 대상 API 요청 수를 로그에 남긴다 (pacing.BUDGET).
상품 금액 상한은 새로 입찰할 때만 쓰는 규칙이라 ([입찰취소] 와 같음) 기준은 충족하는데 A 가 상한을 넘기만 하는 입찰은
올리지도 지우지도 않고 그대로 둔다 (변경안함).
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from . import auth, browser, pipeline
from . import bid as bid_mod
from . import product as product_mod
from .cancel import (CancelAborted, CancelUncertain, OpenBid, apply_known, delete_bid, ensure_product_id,
                     list_open_bids, match_known_bid, read_bid_info)
from .config import Settings
from .debug import dump
from .report import ProductResult
from . import pacing
from .sitewait import PROBE_SEC, TROUBLE_STREAK, sleep_with_stop, wait_until_site_back
from .store import (ONE_SIZE, BidRecord, append_run_log, load_bid_products, load_bids, remove_bid, save_bid,
                    save_bid_products)

log = logging.getLogger(__name__)

ITEM_PAUSE_SEC = (2.0, 4.0)      # 입찰 하나를 보고 다음으로 가기 전 무작위로 쉬는 시간
MIN_CYCLE_GAP_SEC = 30           # 사이클이 간격보다 오래 걸렸어도 다음 사이클 전에 최소 이만큼은 쉰다
CHANGE_ATTEMPTS = 2              # [입찰 변경하기] 화면이 예상과 다르면 다시 열어 이만큼까지 시도하고, 그래도 안 되면 지운다
CHANGE_RETRY_PAUSE_SEC = (2.0, 4.0)
# 판단 불가(확인필요)·오류·즉시 판매가 없음이 연달아 TROUBLE_STREAK 건 나면 사이트가 응답을 안 주는 것으로 보고 멈춘다.
# PROBE_SEC 마다 마지막에 막힌 입찰의 구매 페이지를 한 번 열어 보고 '즉시 판매가' 가 그려지면 이어서 본다 (sitewait 공용).
MAX_LIST_FAILURES = 3            # 목록을 연달아 이만큼 못 읽으면 멈춘다
ERROR_BACKOFF_SEC = 120          # 목록을 못 읽었을 때 다시 시도하기 전에 쉬는 시간
NO_B_PREFIX = "즉시 판매가 없음"   # 즉시 판매가가 없어 지운(또는 지우려 한) 결과의 사유 머리 - 연달아 난 수를 셀 때 쓴다


class LoginLost(Exception):
    """로그인이 풀려 구매 페이지가 로그인 화면으로 넘어갔다."""


def _on_login_page(page: Page) -> bool:
    return urlparse(page.url).path.startswith("/login")


def buy_page_url(product_id: int, size: str = ONE_SIZE) -> str:
    """구매 페이지 직접 주소. size 없이 /buy/{id} 만 열면 상품 페이지로 돌려보낸다 (2026-09-04 실측).

    size 는 옵션의 값 (ONE SIZE / 240 ...) - 화면 표기(W240)가 아니라 상세 API 의 product_option.key (2026-09-05 실측).
    """
    return f"https://kream.co.kr/buy/{product_id}?size={quote_plus(size or ONE_SIZE)}"


def change_bid_url(bid: OpenBid) -> str:
    """입찰 상세의 [입찰 변경하기] 버튼이 여는 주소 (2026-09-04 실측)."""
    return (f"{buy_page_url(bid.product_id, bid.size_value)}&bid={bid.bid_id}"
            f"&from=changeBidding&type=bid&price={bid.price or ''}")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


# 본문(공백 제거)에 주어진 글자가 있는지 - 상품명 대조용
_BODY_CONTAINS_JS = "(needle) => document.body.innerText.replace(/\\s+/g, '').includes(needle)"
# 상품명(공백 제거해 포함)과 옵션(한 줄이 정확히 그 표기)이 둘 다 그려졌는지 - 변경 화면 대조용. 빈 값은 보지 않는다
_NAME_AND_OPTION_JS = """({name, option}) => {
  const text = document.body.innerText;
  const nameOk = !name || text.replace(/\\s+/g, '').includes(name);
  const optionOk = !option || text.split('\\n').some(line => line.trim() === option);
  return nameOk && optionOk;
}"""
CHANGE_PAGE_MATCH_TIMEOUT_MS = 12_000   # 상품명·옵션이 그려지길 기다리는 최대 시간 (느린 시간대 대비)


# ---------------------------------------------------------------- 빠른 확인

def read_current_b(page: Page, product_id: int, size: str = ONE_SIZE) -> int | None:
    """구매 페이지를 바로 열어 '즉시 판매가' B 만 읽는다. 구매 페이지가 안 뜨고 다른 곳으로 가면(옵션 상품 등) None.

    사이트가 응답을 안 줄 때 다시 주는지 보는 확인(_wait_for_site)에 쓴다. 입찰마다 하던 빠른 확인은 2026-09-06 에 뺐다 -
    A 도 매번 읽게 되어 어차피 같은 구매 페이지를 다시 열기 때문 (지금은 product.read_price_a_and_go_to_buy 가 같은 구매 페이지에서 B 를 읽는다).
    구매 페이지에 와 있는데 '즉시 판매가' 가 안 그려지면 NoPriceB.
    구매 페이지 이동이 15초 안에 안 끝나거나 끊기면(net::ERR_ABORTED) 1.5초 뒤 한 번 더 열고, 그래도 안 되면 SkipProduct(판단 불가).
    """
    url = buy_page_url(product_id, size)
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded")
            break
        except (PlaywrightTimeout, PlaywrightError) as e:
            if attempt:
                raise product_mod.SkipProduct(f"구매 페이지를 열지 못함 (두 번 시도): {str(e).splitlines()[0]}") from e
            log.info("구매 페이지 이동이 안 끝남 (%s) - 1.5초 뒤 다시 엶", str(e).splitlines()[0])
            page.wait_for_timeout(1500)
    # 고정 대기 대신 '즉시 판매가 N원' 이 실제로 그려질 때까지만 기다린다 (상품 페이지로 돌려보내지면 안 그려져 타임아웃)
    try:
        page.wait_for_function(_PRICE_B_RENDERED_JS, timeout=8000)
    except PlaywrightTimeout:
        if _on_login_page(page):
            raise LoginLost(f"구매 페이지가 로그인 화면으로 넘어감: {page.url}") from None
        if urlparse(page.url).path.startswith(f"/buy/{product_id}"):
            raise product_mod.NoPriceB("구매 페이지가 떴는데 '즉시 판매가' 가 없음") from None
        log.info("구매 페이지가 다른 곳으로 넘어감 (지금 주소 %s)", page.url)
        return None
    # 로그인 화면의 returnUrl 에도 /buy/{id} 가 들어가므로 경로로 본다
    if not urlparse(page.url).path.startswith(f"/buy/{product_id}"):
        return None
    try:
        return product_mod.read_price_b(page)
    except product_mod.SkipProduct:
        return None


_PRICE_B_RENDERED_JS = "() => /즉시 판매가\\s*[\\d,]+\\s*원/.test(document.body.innerText)"


# ---------------------------------------------------------------- 변경

def change_bid(page: Page, bid: OpenBid, new_price: int, settings: Settings) -> None:
    """[입찰 변경하기] 화면에서 희망가를 new_price 로 올린다. 화면은 처음 입찰과 같아 bid 모듈의 단계를 그대로 쓴다."""
    page.goto(change_bid_url(bid), wait_until="domcontentloaded")
    try:
        page.get_by_text("즉시 판매가", exact=True).first.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeout as e:
        dump(page, f"rebid{bid.bid_id}_no_change_page")
        raise bid_mod.BidAborted("입찰 변경 화면이 뜨지 않음") from e
    if f"bid={bid.bid_id}" not in page.url or "changeBidding" not in page.url:
        raise bid_mod.BidAborted(f"입찰 변경 화면 주소가 예상과 다름: {page.url}")
    body = page.locator("body").inner_text()
    if "구매 입찰하기" not in body:
        dump(page, f"rebid{bid.bid_id}_not_bid_page")
        raise bid_mod.BidAborted("'구매 입찰하기' 화면이 아님")
    # 상품명·옵션은 '즉시 판매가' 글자보다 늦게 그려진다 (2026-09-04 실측). 2026-09-05~06 에 상품명(5초 안에 안 그려짐)과
    # 옵션(즉시 판매가 직후 한 번 읽은 본문에 아직 없음)을 '다름' 으로 오판해 멀쩡한 입찰 3건이 변경못함이 됐다 (스냅샷에는
    # 둘 다 정확히 있었음) - 둘 다 나타날 때까지 넉넉히 기다린 뒤 대조한다
    expected = {"name": _squash(bid.name) if bid.name else "", "option": "" if bid.is_one_size else bid.option}
    if expected["name"] or expected["option"]:
        try:
            page.wait_for_function(_NAME_AND_OPTION_JS, arg=expected, timeout=CHANGE_PAGE_MATCH_TIMEOUT_MS)
        except PlaywrightTimeout as e:
            name_ok = not expected["name"] or page.evaluate(_BODY_CONTAINS_JS, expected["name"])
            if not name_ok:
                dump(page, f"rebid{bid.bid_id}_name_mismatch")
                raise bid_mod.BidAborted(f"변경 화면의 상품명이 '{bid.name}' 과 다름") from e
            dump(page, f"rebid{bid.bid_id}_option_mismatch")
            raise bid_mod.BidAborted(f"변경 화면의 옵션 표기가 '{bid.option}' 이 아님") from e
    page.wait_for_timeout(300)
    bid_mod.fill_bid_form(page, new_price, settings.bid_days, settings, bid.product_id)
    bid_mod.choose_warehouse_and_points(page, settings, bid.product_id)
    bid_mod.submit_bid(page, new_price, settings, bid.product_id)


def _won(price: int | None) -> str:
    return f"{price:,}원" if price else "?"


def _margin_note(r: ProductResult) -> str:
    """순위유지 사유에 붙일 A·마진 요약 (예: 'A 35,000원, 마진 22.9% > 기준 10%')."""
    rate = r.margin_rate
    if rate is None:
        return f"A {_won(r.price_a)}"
    base = f"기준 {r.margin_min * 100:g}%" if r.margin_min is not None else "기준 없음"
    return f"A {_won(r.price_a)}, 마진 {rate * 100:.1f}% > {base}"


def _record(bid: OpenBid, r: ProductResult, new_price: int, settings: Settings, note: str = "") -> None:
    """bids.json 의 가격을 새 희망가로 바꿔 둔다 - 다음 사이클에 목록의 입찰을 상세 없이 상품 ID 로 이을 수 있게."""
    save_bid(BidRecord(
        product_id=bid.product_id, name=bid.name or r.name, price=new_price, bid_days=settings.bid_days,
        placed_at=datetime.now().isoformat(timespec="seconds") + note,
        fast_sales_30d=r.fast_sales or 0, price_a=r.price_a or 0, price_b=r.price_b or 0,
        option=bid.option or ONE_SIZE, size=bid.size_value or ONE_SIZE,
    ))


# ---------------------------------------------------------------- 입찰 하나

def rebid_one(page: Page, bid: OpenBid, settings: Settings, cycle: int, diagnose: bool = True) -> ProductResult:
    """입찰 하나를 본다: 밀렸는지 → 처음 입찰 기준으로 다시 판정 → [입찰 변경하기] 로 희망가를 B 로 (또는 기준 미달이면 지움).

    page 는 실행 내내 같은 탭을 다시 쓴다 (입찰마다 탭을 열고 닫는 시간을 아낀다. 단계마다 goto 로 시작하므로 앞 입찰의 화면이 남지 않는다).
    로그인이 풀린 것이 보이면 다시 로그인하고 한 번 더 본다. diagnose 가 True 면 판단 불가로 끝날 때 화면 스냅샷을 남긴다.
    """
    r = ProductResult(rank=bid.order, product_id=bid.product_id or 0, name=bid.name, url=bid.url,
                      category=f"{cycle}회차", bid_price=bid.price, option="" if bid.is_one_size else bid.option)
    try:
        try:
            _rebid_one(page, bid, settings, cycle, r, diagnose)
        except LoginLost as e:
            log.warning("[%d회차 %d번째] %s - 다시 로그인하고 한 번 더 봄", cycle, bid.order, e)
            try:
                auth.ensure_logged_in(page, settings)
            except Exception as e3:
                r.status, r.detail = "오류", f"로그인이 풀렸는데 다시 로그인하지 못함: {e3}"
                raise
            try:
                _rebid_one(page, bid, settings, cycle, r, diagnose)
            except LoginLost as e2:
                r.status, r.detail = "확인필요", f"판단 불가 - 올리지 않음: 다시 로그인했는데도 {e2}"
    finally:
        r.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info("[%d회차 %d번째] 결과: %s - %s", cycle, bid.order, r.status, r.detail)
        append_run_log({
            "category": r.category, "rank": r.rank, "product_id": r.product_id, "name": r.name, "option": r.option,
            "fast_sales": r.fast_sales if r.fast_sales is not None else "",
            "price_a": r.price_a or "", "price_b": r.price_b or "",
            "status": r.status, "detail": r.detail,
        })
    return r


def is_site_trouble(r: ProductResult) -> bool:
    """사이트가 응답을 안 줘서 생겼을 수 있는 결과인지 (판단 불가 · 오류 · 즉시 판매가 없음). 연달아 나면 쉬어야 한다."""
    return (r.status == "오류"
            or (r.status == "확인필요" and r.detail.startswith("판단 불가"))
            or r.detail.removeprefix("dry-run: ").startswith(NO_B_PREFIX))


def _delete_bid_and_report(page: Page, bid: OpenBid, settings: Settings, r: ProductResult, why: str) -> None:
    """입찰을 지우고 (dry-run 이면 취소대상으로만) 결과를 r 에 채운다. 기준 미달 · 즉시 판매가 없음 두 경로가 같이 쓴다."""
    if settings.dry_run:
        r.status, r.detail = "취소대상", f"dry-run: {why}"
        return
    try:
        delete_bid(page, bid, settings)
    except CancelAborted as e:
        r.status, r.detail = "확인필요", f"{why} - 그런데 지우지 못함 (안전장치: {e})"
        return
    except CancelUncertain as e:
        r.status, r.detail = "확인필요", f"{why} - {e}, 마이페이지에서 확인"
        return
    remove_bid(bid.product_id, bid.size_value)
    r.status, r.detail = "입찰취소", f"{why} -> 입찰 #{bid.bid_id} 지움"


def _cancel_no_b(page: Page, bid: OpenBid, settings: Settings, r: ProductResult, why: str, diagnose: bool) -> None:
    """즉시 판매가가 없는 입찰을 지운다 (사용자 결정: 즉시 판매가가 없으면 그냥 입찰 취소).

    지우기 전에 상세 API 로 살아 있는 입찰인지 본다 - 체결·만료·이미 지운 입찰이면 즉시 판매가가 없는 게 당연하니 건너뜀.
    """
    log.info("%s (지금 주소 %s) - 사이트가 살아 있는지 확인한 뒤 입찰 #%d 을 지움", why, page.url, bid.bid_id)
    if diagnose:
        dump(page, f"rebid{bid.bid_id}_no_b")
    head = f"{NO_B_PREFIX} ({why}, 내 희망가 {bid.price:,}원)"
    # 사이트가 응답을 안 주는 시간대에는 즉시 판매가도 안 그려진다 - 그때 지우면 멀쩡한 입찰이 사라지므로 (사용자 결정 2026-09-05)
    # 상품 페이지의 체결 내역이 불러와지는지 먼저 보고, 그것도 안 오면 판단 불가로 두고 (연달아 나면 멈춰 기다림) 지우지 않는다
    alive, note = product_mod.sales_available(page, bid.product_url)
    if not alive:
        log.info("체결 내역도 불러오지 못함 - 사이트 문제로 보고 입찰 #%d 은 지우지 않음", bid.bid_id)
        r.status, r.detail = "확인필요", f"판단 불가 - 지우지 않음: {why}인데 체결 내역도 불러오지 못함 ({note})"
        return
    log.info("체결 내역은 불러와짐 (%s) - 즉시 판매가가 정말 없는 것으로 보고 지움", note)
    if not settings.dry_run:
        data = read_bid_info(page, bid)
        status = (data or {}).get("status")
        if status and status != "live":
            # 사이트 문제가 아니라 입찰이 끝난 것 - 사유 머리에 NO_B_PREFIX 를 두지 않아 '연달아 난 수' 에 들어가지 않게
            r.status, r.detail = "건너뜀", (f"입찰 상태가 '{(data or {}).get('status_display') or status}' 라 "
                                          f"살아 있는 입찰이 아님 (그래서 {head})")
            return
    _delete_bid_and_report(page, bid, settings, r, head)


def _rebid_one(page: Page, bid: OpenBid, settings: Settings, cycle: int, r: ProductResult, diagnose: bool) -> None:
    old_price = bid.price
    try:
        if bid.needs_detail:
            data = ensure_product_id(page, bid)
            status = (data or {}).get("status")
            if status and status != "live":
                r.status, r.detail = "건너뜀", f"입찰 상태가 '{(data or {}).get('status_display') or status}' - 살아 있는 입찰이 아님"
                return
        r.product_id, r.url, r.bid_price, r.size = bid.product_id, bid.product_url, bid.price, bid.size_value
        if not bid.price:
            raise bid_mod.BidAborted("내 희망가를 읽지 못함")
        if not bid.size_value:
            raise bid_mod.BidAborted(f"옵션 '{bid.option}' 의 size 값을 알지 못함 (상세 API 에 product_option 없음?)")
        log.info("[%d회차 %d번째] %s - 내 희망가 %s원 (%s)", cycle, bid.order, bid.label, f"{bid.price:,}", bid.product_url)

        # 상품 페이지 → 구매하기 모달에서 최신 A → 구매 페이지에서 최신 B 를 읽고 마진을 판정한다 (처음 입찰과 같은 기준, 상한 제외).
        # 밀리지 않은 입찰도 A 가 내려가 마진이 기준 아래로 떨어졌을 수 있어 매번 본다 (사용자 결정 2026-09-06).
        # 거래량은 밀린 입찰만 보므로 여기서는 상품 페이지의 sales 요청을 빈 응답으로 채워 스로틀 대상 요청을 내지 않는다
        # (구매 페이지를 먼저 여는 빠른 확인은 뺐다 - 어차피 A 를 읽으러 같은 구매 페이지를 다시 열게 되어 2초쯤 낭비였다)
        with browser.sales_trimmed():
            r.name = product_mod.open_product(page, bid.product_url) or r.name
            reason = pipeline.judge_prices(page, r, settings, price_limit=False, option=bid.eval_option)
        pushed = r.price_b > bid.price
        if pushed:
            log.info("밀림: 즉시 판매가 %s원 > 내 희망가 %s원", f"{r.price_b:,}", f"{bid.price:,}")
            if reason is None:
                # 올리려면 거래량 기준도 봐야 한다 - 상품 페이지를 다시 열어 (이번엔 sales 를 받아) 체결 내역을 센다
                reason = pipeline.check_sales(page, bid.product_url, r, settings, bid.eval_option)
        else:
            log.info("밀리지 않음: 즉시 판매가 %s원 <= 내 희망가 %s원", f"{r.price_b:,}", f"{bid.price:,}")
        if reason:
            # 기준 미달이면 밀렸든 아니든 둘 수 없으니 지운다 ([입찰취소] 와 같은 기준·방식). 판단 불가(예외)는 지우지 않는다
            head = "밀렸는데 기준 미달이라 올릴 수 없음" if pushed else "밀리진 않았지만 기준 미달이라 둘 수 없음"
            _delete_bid_and_report(page, bid, settings, r,
                                   f"{head}: {reason} (내 {bid.price:,}원, 지금 A {_won(r.price_a)}, B {_won(r.price_b)})")
            return
        if not pushed:
            r.status, r.detail = "순위유지", (f"즉시 판매가 {r.price_b:,}원 {'=' if r.price_b == bid.price else '<'} "
                                          f"내 희망가 {bid.price:,}원 - 밀리지 않음, {_margin_note(r)}")
            return
        if settings.rules.over_limit(r.price_a):
            # 상한은 새로 입찰할 때만 쓰는 규칙 - 기준은 충족하니 지우지 않고, 새 입찰 규칙상 올리지도 않는다
            r.status, r.detail = "변경안함", (f"밀렸지만 A {r.price_a:,}원 > 상품 금액 상한 {settings.rules.max_price_a:,}원이라 "
                                           f"올리지 않음 (기준은 충족해 지우지도 않음, 내 {bid.price:,}원, 지금 B {r.price_b:,}원)")
            return
        new_price = r.price_b
        r.bid_price, r.bid_days = new_price, settings.bid_days
        if settings.dry_run:
            r.status, r.detail = "변경대상", f"dry-run: {bid.price:,}원 → {new_price:,}원 ({settings.bid_days}일) 으로 올릴 조건 충족"
            return

        for attempt in range(CHANGE_ATTEMPTS):
            try:
                change_bid(page, bid, new_price, settings)
                break
            except bid_mod.BidUncertain as e:
                _record(bid, r, new_price, settings, note=" (확인 필요)")
                r.status, r.detail = "확인필요", f"{e} - 마이페이지에서 희망가 확인 (다음 사이클에 목록으로 다시 확인)"
                return
            except bid_mod.BidAborted as e:
                # 마지막 '입찰하기' 는 누르지 않은 상태 (눌렀으면 BidUncertain) - 희망가는 그대로다.
                # 화면이 늦게 그려져 생기는 일시적 불일치가 대부분이라 변경 화면을 한 번 다시 열어 본다
                if attempt + 1 < CHANGE_ATTEMPTS:
                    log.warning("[%d번째] 입찰 변경 못 함: %s - %d초 뒤 변경 화면을 다시 열어 한 번 더 시도",
                                bid.order, e, int(CHANGE_RETRY_PAUSE_SEC[1]))
                    pacing.pause(CHANGE_RETRY_PAUSE_SEC)
                    continue
                # 다시 열어도 못 올림 - 밀렸고 기준도 충족하는 입찰을 밀린 채 둘 수 없으니 지운다 (사용자 결정 2026-09-06)
                log.warning("[%d번째] 입찰 변경 못 함 (%d번 시도): %s - 입찰을 지움", bid.order, CHANGE_ATTEMPTS, e)
                _delete_bid_and_report(page, bid, settings, r,
                                       f"밀렸는데 {CHANGE_ATTEMPTS}번 시도해도 입찰 변경 못 함: {e} "
                                       f"(내 {bid.price:,}원, 지금 B {new_price:,}원)")
                return
        _record(bid, r, new_price, settings)
        bid.price = new_price
        r.status, r.detail = "변경완료", f"{old_price:,}원 → {new_price:,}원 / {settings.bid_days}일 / 창고보관"
        return
    except product_mod.NoPriceB as e:
        if _on_login_page(page):
            raise LoginLost(f"구매 페이지 확인 중 로그인 화면으로 넘어감: {page.url}") from e
        _cancel_no_b(page, bid, settings, r, str(e), diagnose)
    except product_mod.SkipProduct as e:
        if _on_login_page(page):
            raise LoginLost(f"상품 페이지 확인 중 로그인 화면으로 넘어감: {page.url}") from e
        # 입찰 중인 상품(ONE SIZE, 체결 내역 있음)이 판단 불가가 되는 건 사이트가 응답을 안 준 것 - 화면을 남겨 원인을 볼 수 있게
        log.info("판단 불가 (지금 주소 %s)", page.url)
        if diagnose:
            dump(page, f"rebid{bid.bid_id}_skip")
        r.status, r.detail = "확인필요", f"판단 불가 - 올리지 않음: {e}"
    except LoginLost:
        raise   # rebid_one 이 다시 로그인하고 한 번 더 본다 (아래 Exception 에 삼켜져 '오류' 로 끝나던 문제, 2026-09-05)
    except bid_mod.StoppedBeforeSubmit as e:
        r.status, r.detail = "중단", str(e)
    except bid_mod.BidAborted as e:
        # 변경 화면까지 가기 전(희망가·옵션 size 값을 못 읽음) - 밀렸는지도 모르는 상태라 지우지 않고 남긴다
        log.warning("[%d번째] 입찰 변경 못 함: %s", bid.order, e)
        r.status, r.detail = "변경못함", f"입찰 변경 못 함: {e}"
    except CancelAborted as e:
        r.status, r.detail = "건너뜀", str(e)
    except Exception as e:  # noqa: BLE001
        dump(page, f"rebid{bid.bid_id}_error")
        log.exception("입찰 #%d 재입찰 처리 중 오류", bid.bid_id)
        r.status, r.detail = "오류", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 사이클 반복

def _wait_for_site(page: Page, probe: OpenBid, should_stop: Callable[[], bool],
                   on_status: Callable[[str], None]) -> bool:
    """사이트가 응답을 안 줄 때 다시 줄 때까지 멈춘다. PROBE_SEC 마다 마지막에 막힌 입찰의 구매 페이지를 한 번 열어 보고
    '즉시 판매가' 가 그려지면 돌아온다 (True). 중지 요청이면 False."""
    def check() -> bool:
        if not probe.product_id:
            # 확인할 상품을 모르면 (상세를 못 읽은 입찰) 한 번 쉰 뒤 그냥 이어서 본다 - 또 막히면 다시 멈춘다
            return True
        b = read_current_b(page, probe.product_id, probe.size_value or ONE_SIZE)
        if b is not None:
            log.info("즉시 판매가 %s원이 다시 그려짐", f"{b:,}")
        return b is not None

    return wait_until_site_back(check, should_stop, on_status, what="즉시 판매가")


def _list_bids(page: Page, settings: Settings) -> list[OpenBid]:
    """목록을 읽는다. 로그인이 풀려 목록이 안 열리면 다시 로그인하고 한 번 더."""
    try:
        return list_open_bids(page)
    except CancelAborted:
        log.info("구매 입찰 목록이 열리지 않음 - 로그인 상태 확인")
        auth.ensure_logged_in(page, settings)
        return list_open_bids(page)


def run(context: BrowserContext, page: Page, settings: Settings,
        should_stop: Callable[[], bool] | None = None,
        on_result: Callable[[ProductResult], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_cycle: Callable[[int, list[ProductResult]], None] | None = None,
        max_cycles: int | None = None) -> list[ProductResult]:
    """구매 입찰 목록을 반복해서 돈다. should_stop 이 True 가 되면 지금 보는 입찰까지 처리하고 멈춘다.

    on_status: 상태 한 줄 (GUI 상태 표시용). on_cycle: 사이클이 끝날 때마다 (회차, 그 회차 결과) - 보고서 중간 저장용.
    max_cycles: 이만큼 돌고 멈춘다 (명령행 --once 등). None 이면 중지할 때까지.
    """
    stop = should_stop or (lambda: False)
    status = on_status or (lambda _t: None)
    results: list[ProductResult] = []
    # 입찰번호 -> 상품 ID. 파일(data/bid_products.json)에 남겨 두어 프로그램이 넣지 않은 입찰도 상세는 처음 한 번만 연다
    pid_cache = load_bid_products()
    tab: Page = context.new_page()   # 입찰마다 탭을 열고 닫지 않고 실행 내내 이 탭을 다시 쓴다
    cycle = 0
    list_failures = 0
    while not stop():
        cycle += 1
        started = time.monotonic()
        api_calls_before = pacing.BUDGET.total
        status(f"재입찰 {cycle}회차: 구매 입찰 목록 읽는 중...")
        try:
            bids = _list_bids(page, settings)
            list_failures = 0
        except Exception as e:  # noqa: BLE001
            list_failures += 1
            log.exception("%d회차: 구매 입찰 목록을 읽지 못함 (%d/%d)", cycle, list_failures, MAX_LIST_FAILURES)
            if list_failures >= MAX_LIST_FAILURES:
                raise RuntimeError(f"구매 입찰 목록을 {MAX_LIST_FAILURES}번 연달아 읽지 못해 멈춤: {e}") from e
            status(f"목록을 읽지 못해 {ERROR_BACKOFF_SEC // 60}분 뒤 다시 시도")
            sleep_with_stop(stop, ERROR_BACKOFF_SEC)
            continue

        known = load_bids()
        for b in bids:
            cached = pid_cache.get(b.bid_id)
            if cached:
                b.product_id, b.size = cached["product_id"], cached.get("size") or ""
            if b.needs_detail:
                apply_known(b, match_known_bid(b, known))
        # 목록에서 사라진 입찰(체결·만료·지움)은 캐시에서 뺀다
        live_ids = {b.bid_id for b in bids}
        for stale in [k for k in pid_cache if k not in live_ids]:
            del pid_cache[stale]
        log.info("===== 재입찰 %d회차: 구매 입찰 %d건 (상세를 열어야 하는 입찰 %d건) =====",
                 cycle, len(bids), sum(1 for b in bids if b.needs_detail))

        cycle_results: list[ProductResult] = []
        trouble_streak = 0     # 판단 불가·오류가 연달아 난 수
        for bid in bids:
            if stop():
                log.info("사용자 요청으로 중지 - 남은 입찰 %d건은 보지 않음", len(bids) - len(cycle_results))
                break
            status(f"재입찰 {cycle}회차: {bid.order}/{len(bids)} {bid.name[:24]}")
            if tab.is_closed():
                tab = context.new_page()
            # 스냅샷은 연달아 난 처음 몇 건만 남긴다 (사이트가 안 줄 때 수십 장 쌓이지 않게)
            r = rebid_one(tab, bid, settings, cycle, diagnose=trouble_streak < TROUBLE_STREAK)
            if bid.product_id and bid.size_value:
                entry = {"product_id": bid.product_id, "size": bid.size_value, "option": bid.option or ONE_SIZE}
                if pid_cache.get(bid.bid_id) != entry:
                    pid_cache[bid.bid_id] = entry
                    save_bid_products(pid_cache)
            cycle_results.append(r)
            results.append(r)
            if on_result:
                on_result(r)
            trouble_streak = trouble_streak + 1 if is_site_trouble(r) else 0
            if trouble_streak >= TROUBLE_STREAK:
                trouble_streak = 0
                log.warning("판단 불가·오류가 %d건 연달아 남 - 사이트가 응답을 안 주는 듯해 멈춤. %d분마다 확인하고 "
                            "다시 주면 로그인 상태를 확인한 뒤 이어서 봄", TROUBLE_STREAK, PROBE_SEC // 60)
                if not _wait_for_site(tab, bid, stop, lambda t: status(f"재입찰 {cycle}회차 ({bid.order}/{len(bids)} 까지 봄): {t}")):
                    break
                try:
                    auth.ensure_logged_in(page, settings)
                except Exception:  # noqa: BLE001
                    log.exception("쉬고 나서 로그인 상태를 확인하지 못함 - 그대로 이어서 봄")
            if stop():
                break
            sleep_with_stop(stop, random.uniform(*ITEM_PAUSE_SEC))

        counts: dict[str, int] = {}
        for r in cycle_results:
            counts[r.status] = counts.get(r.status, 0) + 1
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "처리한 입찰 없음"
        elapsed = time.monotonic() - started
        api_calls = pacing.BUDGET.total - api_calls_before
        log.info("===== 재입찰 %d회차 끝 (%d초): %s | 스로틀 대상 API 요청 %d건 (지금 10분 창 %d/%d건) =====",
                 cycle, int(elapsed), summary, api_calls, pacing.BUDGET.used(), pacing.BUDGET.limit)
        if on_cycle:
            on_cycle(cycle, cycle_results)
        if stop():
            break
        if max_cycles is not None and cycle >= max_cycles:
            log.info("설정한 %d회를 모두 돌아 재입찰을 마침", max_cycles)
            status(f"재입찰 {cycle}회차 끝({int(elapsed)}초): {summary} - 설정한 {max_cycles}회를 모두 돌아 마침")
            break

        wait = max(settings.rebid_interval_min * 60 - elapsed, MIN_CYCLE_GAP_SEC)
        next_at = datetime.now() + timedelta(seconds=wait)
        # 간격은 '사이클 시작 시각 사이의 간격' - 이번 사이클이 오래 걸렸으면 그만큼 덜 쉰다 (최소 MIN_CYCLE_GAP_SEC)
        log.info("이번 사이클 %d초 걸림. 사이클 시작 간격 %g분(%d초)이라 %d초 쉬고 %s 에 다음 사이클 시작",
                 int(elapsed), settings.rebid_interval_min, int(settings.rebid_interval_min * 60), int(wait),
                 next_at.strftime("%H:%M:%S"))
        status(f"재입찰 {cycle}회차 끝({int(elapsed)}초): {summary} - {int(wait)}초 쉬고 {next_at:%H:%M} 에 다음 사이클 "
               f"(시작 간격 {settings.rebid_interval_min:g}분)")
        sleep_with_stop(stop, wait)
    try:
        tab.close()
    except Exception:  # noqa: BLE001
        pass
    return results

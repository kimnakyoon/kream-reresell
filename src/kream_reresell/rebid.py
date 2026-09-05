"""[재입찰] 마이페이지 > 구매 내역 > 구매 입찰 탭의 입찰을 순서대로 돌며, 남에게 밀린 입찰의 희망가를 [입찰 변경하기] 로 올린다.

흐름 (2026-09-04 실측):
  1. /my/buying?tab=bidding 목록을 읽는다 (cancel.list_open_bids). 상품 ID 는 bids.json 기록 → 이번 실행의 캐시 →
     상세 API 응답(api/m/bids/{입찰번호}) 순으로 알아낸다.
  2. 빠른 확인: /buy/{상품ID}?size=ONE+SIZE 를 바로 열면 구매 페이지가 뜨고 '즉시 판매가'(B = 지금 가장 높은 구매 입찰가) 가
     보인다 (2초쯤). B 가 내 희망가 이하이면 아직 밀리지 않은 것 - 그대로 둔다 (순위유지). 상품 페이지는 열지 않는다.
  3. B 가 내 희망가보다 높으면(누가 더 비싸게 입찰함) 상품 페이지를 다시 열어 처음 입찰할 때와 똑같이 판정한다
     (pipeline.evaluate: 최근 30일 빠른배송 건수, 최신 A·B, A−B > A×구간별 마진율, 상품 금액 상한).
  4. 조건이 맞으면 입찰 상세의 [입찰 변경하기] 버튼이 여는 것과 같은 주소
     /buy/{상품ID}?size=ONE+SIZE&bid={입찰번호}&from=changeBidding&type=bid&price={기존 희망가}
     로 가서 처음 입찰과 같은 화면을 채운다: 희망가 = 최신 B, 마감기한, 구매 입찰 계속 → 창고보관 → 포인트 최대 사용
     → 입찰하기 → 동의 3항목 → 입찰하기 (bid.fill_bid_form / choose_warehouse_and_points / submit_bid 그대로).
  5. 목록 끝까지 가면 한 사이클. 중지할 때까지 사이클을 반복하되, 사이클 시작 간격(설정, 기본 5분)을 지키고
     입찰 사이에도 2~4초 무작위로 쉰다 (봇 탐지 대비).

사이트가 응답을 안 줄 때 (2026-09-05 실측): 상품 페이지 자체는 뜨는데 구매 페이지의 '즉시 판매가' 와 체결 내역 패널의 행처럼
API 로 채우는 부분만 비는 시간대가 있었다 (한 번 시작하면 계속 두드리는 동안 20분 넘게 이어졌고, 7분쯤 쉬면 풀렸다).
그래서 판단 불가(확인필요)·오류가 연달아 TROUBLE_STREAK 건 나오면 10분 쉬고 로그인 상태를 확인한 뒤 이어서 보고,
같은 사이클에서 또 그러면 사이클을 끊고 다음 사이클 전에 10분 더 쉰다. 판단 불가로 끝난 입찰은 화면 스냅샷을 dumps/ 에 남긴다.
구매 페이지가 로그인 화면으로 넘어가면(로그인이 풀림) 다시 로그인하고 그 입찰을 한 번 더 본다.

밀렸는데 기준(거래량 · 마진)에 못 미쳐 올릴 수 없는 입찰은 [입찰취소] 와 같은 방식으로 지운다
(상세의 '입찰 지우기' → 확인창 → DELETE 204 확인, cancel.delete_bid). 판단할 수 없는 경우(상품 페이지 오류 등)는 지우지 않는다.
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
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from . import auth, pipeline
from . import bid as bid_mod
from . import product as product_mod
from .cancel import (CancelAborted, CancelUncertain, OpenBid, delete_bid, ensure_product_id, list_open_bids,
                     match_known_bid)
from .config import Settings
from .debug import dump
from .report import ProductResult
from .store import BidRecord, append_run_log, load_bid_products, load_bids, remove_bid, save_bid, save_bid_products

log = logging.getLogger(__name__)

ITEM_PAUSE_SEC = (2.0, 4.0)      # 입찰 하나를 보고 다음으로 가기 전 무작위로 쉬는 시간
MIN_CYCLE_GAP_SEC = 30           # 사이클이 간격보다 오래 걸렸어도 다음 사이클 전에 최소 이만큼은 쉰다
TROUBLE_STREAK = 3               # 판단 불가(확인필요)·오류가 연달아 이만큼 나면 사이트가 응답을 안 주는 것으로 보고
TROUBLE_PAUSE_SEC = 600          # 10분 쉬고 로그인 상태를 확인한 뒤 이어서 본다. 같은 사이클에서 또 그러면 사이클을 끊고
                                 # 다음 사이클 전에 10분 더 쉰다 (계속 두드리면 풀리지 않는다 - 2026-09-05 실측)
MAX_LIST_FAILURES = 3            # 목록을 연달아 이만큼 못 읽으면 멈춘다


class LoginLost(Exception):
    """로그인이 풀려 구매 페이지가 로그인 화면으로 넘어갔다."""


def _on_login_page(page: Page) -> bool:
    return urlparse(page.url).path.startswith("/login")


def buy_page_url(product_id: int) -> str:
    """구매 페이지 직접 주소. size 없이 /buy/{id} 만 열면 상품 페이지로 돌려보낸다 (2026-09-04 실측)."""
    return f"https://kream.co.kr/buy/{product_id}?size=ONE+SIZE"


def change_bid_url(bid: OpenBid) -> str:
    """입찰 상세의 [입찰 변경하기] 버튼이 여는 주소 (2026-09-04 실측)."""
    return (f"https://kream.co.kr/buy/{bid.product_id}?size=ONE+SIZE&bid={bid.bid_id}"
            f"&from=changeBidding&type=bid&price={bid.price or ''}")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


# 본문(공백 제거)에 주어진 글자가 있는지 - 상품명 대조용
_BODY_CONTAINS_JS = "(needle) => document.body.innerText.replace(/\\s+/g, '').includes(needle)"


# ---------------------------------------------------------------- 빠른 확인

def read_current_b(page: Page, product_id: int) -> int | None:
    """구매 페이지를 바로 열어 '즉시 판매가' B 만 읽는다. 구매 페이지가 안 뜨면(옵션 상품 등) None."""
    page.goto(buy_page_url(product_id), wait_until="domcontentloaded")
    # 고정 대기 대신 '즉시 판매가 N원' 이 실제로 그려질 때까지만 기다린다 (상품 페이지로 돌려보내지면 안 그려져 타임아웃)
    try:
        page.wait_for_function(_PRICE_B_RENDERED_JS, timeout=8000)
    except PlaywrightTimeout:
        if _on_login_page(page):
            raise LoginLost(f"구매 페이지가 로그인 화면으로 넘어감: {page.url}") from None
        log.info("구매 페이지에 '즉시 판매가' 가 안 그려짐 (지금 주소 %s)", page.url)
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
    if "구매 입찰하기" not in page.locator("body").inner_text():
        dump(page, f"rebid{bid.bid_id}_not_bid_page")
        raise bid_mod.BidAborted("'구매 입찰하기' 화면이 아님")
    # 상품명은 '즉시 판매가' 글자보다 조금 늦게 그려진다 (2026-09-04 실측) - 나타날 때까지 기다렸다가 대조한다
    if bid.name:
        try:
            page.wait_for_function(_BODY_CONTAINS_JS, arg=_squash(bid.name), timeout=5000)
        except PlaywrightTimeout as e:
            dump(page, f"rebid{bid.bid_id}_name_mismatch")
            raise bid_mod.BidAborted(f"변경 화면의 상품명이 '{bid.name}' 과 다름") from e
    page.wait_for_timeout(300)
    bid_mod.fill_bid_form(page, new_price, settings.bid_days, settings, bid.product_id)
    bid_mod.choose_warehouse_and_points(page, settings, bid.product_id)
    bid_mod.submit_bid(page, new_price, settings, bid.product_id)


def _record(bid: OpenBid, r: ProductResult, new_price: int, settings: Settings, note: str = "") -> None:
    """bids.json 의 가격을 새 희망가로 바꿔 둔다 - 다음 사이클에 목록의 입찰을 상세 없이 상품 ID 로 이을 수 있게."""
    save_bid(BidRecord(
        product_id=bid.product_id, name=bid.name or r.name, price=new_price, bid_days=settings.bid_days,
        placed_at=datetime.now().isoformat(timespec="seconds") + note,
        fast_sales_30d=r.fast_sales or 0, price_a=r.price_a or 0, price_b=r.price_b or 0,
    ))


# ---------------------------------------------------------------- 입찰 하나

def rebid_one(page: Page, bid: OpenBid, settings: Settings, cycle: int, diagnose: bool = True) -> ProductResult:
    """입찰 하나를 본다: 밀렸는지 → 처음 입찰 기준으로 다시 판정 → [입찰 변경하기] 로 희망가를 B 로 (또는 기준 미달이면 지움).

    page 는 실행 내내 같은 탭을 다시 쓴다 (입찰마다 탭을 열고 닫는 시간을 아낀다. 단계마다 goto 로 시작하므로 앞 입찰의 화면이 남지 않는다).
    로그인이 풀린 것이 보이면 다시 로그인하고 한 번 더 본다. diagnose 가 True 면 판단 불가로 끝날 때 화면 스냅샷을 남긴다.
    """
    r = ProductResult(rank=bid.order, product_id=bid.product_id or 0, name=bid.name, url=bid.url,
                      category=f"{cycle}회차", bid_price=bid.price)
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
            "category": r.category, "rank": r.rank, "product_id": r.product_id, "name": r.name,
            "fast_sales": r.fast_sales if r.fast_sales is not None else "",
            "price_a": r.price_a or "", "price_b": r.price_b or "",
            "status": r.status, "detail": r.detail,
        })
    return r


def is_site_trouble(r: ProductResult) -> bool:
    """사이트가 응답을 안 줘서 판단하지 못한 결과인지 (판단 불가 · 오류). 연달아 나면 쉬어야 한다."""
    return r.status == "오류" or (r.status == "확인필요" and r.detail.startswith("판단 불가"))


def _rebid_one(page: Page, bid: OpenBid, settings: Settings, cycle: int, r: ProductResult, diagnose: bool) -> None:
    old_price = bid.price
    try:
        if not bid.product_id:
            data = ensure_product_id(page, bid)
            status = (data or {}).get("status")
            if status and status != "live":
                r.status, r.detail = "건너뜀", f"입찰 상태가 '{(data or {}).get('status_display') or status}' - 살아 있는 입찰이 아님"
                return
        r.product_id, r.url, r.bid_price = bid.product_id, bid.product_url, bid.price
        if not bid.price:
            raise bid_mod.BidAborted("내 희망가를 읽지 못함")
        log.info("[%d회차 %d번째] %s - 내 희망가 %s원 (%s)", cycle, bid.order, bid.name, f"{bid.price:,}", bid.product_url)

        current_b = read_current_b(page, bid.product_id)
        if current_b is not None:
            r.price_b = current_b
            if current_b <= bid.price:
                r.status, r.detail = "순위유지", (f"즉시 판매가 {current_b:,}원 {'=' if current_b == bid.price else '<'} "
                                              f"내 희망가 {bid.price:,}원 - 밀리지 않음")
                return
            log.info("밀림: 즉시 판매가 %s원 > 내 희망가 %s원 - 상품 페이지에서 다시 판정", f"{current_b:,}", f"{bid.price:,}")
        else:
            log.info("구매 페이지를 바로 열지 못해 상품 페이지부터 확인")

        # 처음 입찰할 때와 같은 기준 (거래량 · 최신 A · 최신 B · 마진 · 상품 금액 상한). 거래량이 모자라도 A/B 는 읽어 보고서에 남긴다
        # 거래량 · 마진 기준은 상한 없이 판정한다. 못 미치면 올릴 수 없으니 지운다 ([입찰취소] 와 같은 기준·방식)
        reason = pipeline.evaluate(page, bid.product_url, r, settings, stop_early=False, price_limit=False)
        if reason:
            why = f"밀렸는데 기준 미달이라 올릴 수 없음: {reason} (내 {bid.price:,}원, 지금 B {r.price_b:,}원)"
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
            remove_bid(bid.product_id)
            r.status, r.detail = "입찰취소", f"{why} -> 입찰 #{bid.bid_id} 지움"
            return
        if settings.rules.over_limit(r.price_a):
            # 상한은 새로 입찰할 때만 쓰는 규칙 - 기준은 충족하니 지우지 않고, 새 입찰 규칙상 올리지도 않는다
            r.status, r.detail = "변경안함", (f"밀렸지만 A {r.price_a:,}원 > 상품 금액 상한 {settings.rules.max_price_a:,}원이라 "
                                           f"올리지 않음 (기준은 충족해 지우지도 않음, 내 {bid.price:,}원, 지금 B {r.price_b:,}원)")
            return
        new_price = r.price_b
        if new_price <= bid.price:
            r.status, r.detail = "순위유지", f"다시 읽은 즉시 판매가 {new_price:,}원이 내 희망가 {bid.price:,}원 이하 - 밀리지 않음"
            return
        r.bid_price, r.bid_days = new_price, settings.bid_days
        if settings.dry_run:
            r.status, r.detail = "변경대상", f"dry-run: {bid.price:,}원 → {new_price:,}원 ({settings.bid_days}일) 으로 올릴 조건 충족"
            return

        try:
            change_bid(page, bid, new_price, settings)
        except bid_mod.BidUncertain as e:
            _record(bid, r, new_price, settings, note=" (확인 필요)")
            r.status, r.detail = "확인필요", f"{e} - 마이페이지에서 희망가 확인 (다음 사이클에 목록으로 다시 확인)"
            return
        _record(bid, r, new_price, settings)
        bid.price = new_price
        r.status, r.detail = "변경완료", f"{old_price:,}원 → {new_price:,}원 / {settings.bid_days}일 / 창고보관"
        return
    except product_mod.SkipProduct as e:
        if _on_login_page(page):
            raise LoginLost(f"상품 페이지 확인 중 로그인 화면으로 넘어감: {page.url}") from e
        # 입찰 중인 상품(ONE SIZE, 체결 내역 있음)이 판단 불가가 되는 건 사이트가 응답을 안 준 것 - 화면을 남겨 원인을 볼 수 있게
        log.info("판단 불가 (지금 주소 %s)", page.url)
        if diagnose:
            dump(page, f"rebid{bid.bid_id}_skip")
        r.status, r.detail = "확인필요", f"판단 불가 - 올리지 않음: {e}"
    except bid_mod.StoppedBeforeSubmit as e:
        r.status, r.detail = "중단", str(e)
    except bid_mod.BidAborted as e:
        log.warning("[%d번째] 입찰 변경 못 함: %s", bid.order, e)
        r.status, r.detail = "변경못함", f"입찰 변경 못 함: {e}"
    except CancelAborted as e:
        r.status, r.detail = "건너뜀", str(e)
    except Exception as e:  # noqa: BLE001
        dump(page, f"rebid{bid.bid_id}_error")
        log.exception("입찰 #%d 재입찰 처리 중 오류", bid.bid_id)
        r.status, r.detail = "오류", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 사이클 반복

def _sleep(should_stop: Callable[[], bool], seconds: float) -> None:
    """중지 요청을 1초마다 보며 쉰다."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop():
            return
        time.sleep(min(1.0, deadline - time.monotonic()))


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
            _sleep(stop, ERROR_BACKOFF_SEC)
            continue

        known = load_bids()
        for b in bids:
            b.product_id = pid_cache.get(b.bid_id) or match_known_bid(b, known)
        # 목록에서 사라진 입찰(체결·만료·지움)은 캐시에서 뺀다
        live_ids = {b.bid_id for b in bids}
        for stale in [k for k in pid_cache if k not in live_ids]:
            del pid_cache[stale]
        log.info("===== 재입찰 %d회차: 구매 입찰 %d건 (상세를 열어야 하는 입찰 %d건) =====",
                 cycle, len(bids), sum(1 for b in bids if not b.product_id))

        cycle_results: list[ProductResult] = []
        trouble_streak = 0     # 판단 불가·오류가 연달아 난 수
        paused = False         # 이번 사이클에서 이미 한 번 쉬었는지
        broke_off = False      # 쉬고도 또 연달아 나서 사이클을 끊었는지
        for bid in bids:
            if stop():
                log.info("사용자 요청으로 중지 - 남은 입찰 %d건은 보지 않음", len(bids) - len(cycle_results))
                break
            status(f"재입찰 {cycle}회차: {bid.order}/{len(bids)} {bid.name[:24]}")
            if tab.is_closed():
                tab = context.new_page()
            # 스냅샷은 연달아 난 처음 몇 건만 남긴다 (사이트가 안 줄 때 수십 장 쌓이지 않게)
            r = rebid_one(tab, bid, settings, cycle, diagnose=trouble_streak < TROUBLE_STREAK)
            if bid.product_id and pid_cache.get(bid.bid_id) != bid.product_id:
                pid_cache[bid.bid_id] = bid.product_id
                save_bid_products(pid_cache)
            cycle_results.append(r)
            results.append(r)
            if on_result:
                on_result(r)
            trouble_streak = trouble_streak + 1 if is_site_trouble(r) else 0
            if trouble_streak >= TROUBLE_STREAK:
                if paused:
                    broke_off = True
                    log.warning("쉬고 나서도 판단 불가·오류가 %d건 연달아 남 - 이번 사이클을 끊고 %d분 더 쉰 뒤 다음 사이클",
                                trouble_streak, TROUBLE_PAUSE_SEC // 60)
                    break
                paused = True
                trouble_streak = 0
                log.warning("판단 불가·오류가 %d건 연달아 남 - 사이트가 응답을 안 주는 듯해 %d분 쉬고 로그인 상태를 확인한 뒤 이어서 봄",
                            TROUBLE_STREAK, TROUBLE_PAUSE_SEC // 60)
                status(f"재입찰 {cycle}회차: 판단 불가가 {TROUBLE_STREAK}건 연달아 나서 {TROUBLE_PAUSE_SEC // 60}분 쉬는 중 "
                       f"({bid.order}/{len(bids)} 까지 봄)")
                _sleep(stop, TROUBLE_PAUSE_SEC)
                if stop():
                    break
                try:
                    auth.ensure_logged_in(page, settings)
                except Exception:  # noqa: BLE001
                    log.exception("쉬고 나서 로그인 상태를 확인하지 못함 - 그대로 이어서 봄")
            if stop():
                break
            _sleep(stop, random.uniform(*ITEM_PAUSE_SEC))

        counts: dict[str, int] = {}
        for r in cycle_results:
            counts[r.status] = counts.get(r.status, 0) + 1
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "처리한 입찰 없음"
        elapsed = time.monotonic() - started
        log.info("===== 재입찰 %d회차 끝 (%d초): %s =====", cycle, int(elapsed), summary)
        if on_cycle:
            on_cycle(cycle, cycle_results)
        if stop() or (max_cycles is not None and cycle >= max_cycles):
            break

        wait = max(settings.rebid_interval_min * 60 - elapsed, MIN_CYCLE_GAP_SEC)
        if broke_off:
            wait += TROUBLE_PAUSE_SEC
        next_at = datetime.now() + timedelta(seconds=wait)
        # 간격은 '사이클 시작 시각 사이의 간격' - 이번 사이클이 오래 걸렸으면 그만큼 덜 쉰다 (최소 MIN_CYCLE_GAP_SEC)
        log.info("이번 사이클 %d초 걸림. 사이클 시작 간격 %g분(%d초)이라 %d초 쉬고 %s 에 다음 사이클 시작",
                 int(elapsed), settings.rebid_interval_min, int(settings.rebid_interval_min * 60), int(wait),
                 next_at.strftime("%H:%M:%S"))
        status(f"재입찰 {cycle}회차 끝({int(elapsed)}초): {summary} - {int(wait)}초 쉬고 {next_at:%H:%M} 에 다음 사이클 "
               f"(시작 간격 {settings.rebid_interval_min:g}분)")
        _sleep(stop, wait)
    try:
        tab.close()
    except Exception:  # noqa: BLE001
        pass
    return results

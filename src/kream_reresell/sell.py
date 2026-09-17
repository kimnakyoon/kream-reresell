"""[판매] 보관 판매 최저가 경쟁 - 하한(매입가 + 마진) 위에서만 (사용자 결정 2026-09-17).

배경: 9월 판매 38건 중 32건이 그날 빠른배송 체결 최저가에 팔렸고 30건이 역마진이었다. 사용자가 쓰던 No1 Seller Center 의 보관관리
최저가 경쟁이 남의 최저가보다 1,000원 아래로 계속 따라 내려가는데(undercut), 그 하한이 매입가 아래였기 때문이다 (로그 09-15~17 확인).
여기서는 같은 경쟁을 하되 **하한을 매입가(수수료 포함 결제금액)에서 자동으로 계산**해 그 밑으로는 절대 내리지 않는다.

흐름 (한 사이클):
  1. 보관 목록 API `GET /api/seller/inventory/items/in_stock?status=live` (입찰중) 와 `status=in_storage` (판매대기) 를 읽는다 - 페이지 이동 없음
     (2026-09-17 실측: 항목에 id(= 보관번호 = ask_id) · product_id · product_option.key · price · price_breakdown.processing_fee(0.5%) · expires_at).
  2. 항목마다 매입가를 붙인다 - [내역] 과 같은 방식: 구매 내역(api/o/bids 종료 탭)의 창고보관 링크 id 가 보관번호와 같은 구매, 없으면 상품명·옵션이
     같은 구매의 상세(api/m/bids/{id})에서 keep.ask_id 로 확인. 매입가 = 그 상세의 price_breakdown.total_price. 짝이 없으면 건너뛴다 (보고서에 남김).
     한 번 붙인 매입가는 실행 내내 기억한다 (사이클마다 다시 읽지 않음).
  3. 하한 = 매입가 × (1 + 하한 마진율) ÷ (1 − 판매 수수료율) 을 1,000원 단위로 올림. 판매 수수료율은 항목의 price_breakdown 에서 읽는다 (지금 0.5%).
  4. 항목 하나마다 시세 API 한 번(market.fetch_market_paced, 틱 6초)으로 그 옵션의 빠른배송 최저가(lowest_100 = A)를 읽는다.
     - A 가 내 가격보다 낮으면 (남이 아래로 걸음) → 목표가 = max(하한, A − 1,000). 목표가가 지금 가격과 같으면 '하한대기'.
     - A 가 내 가격과 같으면 내 것인지 남의 것인지 API 로는 알 수 없다 (같은 값에 먼저 등록한 사람이 먼저 팔린다) → **탐침**: 내 가격을 잠깐 올려
       (PROBE_UP) 다시 읽어 2등 가격을 알아내고, 2등이 있으면 max(하한, 2등 − 1,000) 으로, 탐침가 아래에 아무도 없으면 탐침가 − 1,000 으로 둔다
       (경쟁자가 빠졌으면 가격이 올라간다 - No1 의 holdAtMax·탐침과 같은 발상). 탐침은 항목마다 PROBE_EVERY_SEC 에 한 번만, 오르는 건
       실행 시작 때 가격의 PROBE_CEILING 배까지만 (경쟁자가 아예 없는 상품이 끝없이 오르지 않게).
     - A 가 없거나(빠른배송 판매자 없음 - 판매대기 항목은 내 것이 시세에 안 잡힌다) 내 가격보다 높으면 → 목표가 = max(하한, A − 1,000) (A 없으면 max(하한, 내 가격)).
     - 내 가격이 하한 아래면 어떤 경우든 하한으로 올린다.
  5. 가격 변경은 No1 과 같은 경로: `POST /api/seller/inventory/actions/review_live` (견적 - 응답 items[].review.processing_fee.value) →
     `POST /api/seller/inventory/actions/set_live` (본문 items[{ask_id, product_id, price, warning: null, processing_fee}]). 판매대기 항목은 이걸로 입찰중이 된다.
     바꾼 가격은 다음 사이클에 목록에서 확인해 다르면 '확인필요' 로 남긴다.
  6. 목록 끝까지 가면 한 사이클. CYCLE_GAP_SEC 쉬고 반복 (횟수는 settings.sell_cycles, 0 이면 [중지]까지).

주의: No1 Seller Center 의 최저가 경쟁이 같은 항목에 켜져 있으면 둘이 번갈아 가격을 바꾼다 - 여기서 다루는 항목은 그쪽 경쟁을 꺼야 한다.
다른 버튼과 같은 크롬 프로필을 쓰므로 [재입찰] 등과 동시에는 못 돈다.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime

from playwright.sync_api import BrowserContext, Page

from . import auth, hangwatch, pacing
from . import market as market_mod
from . import product as product_mod
from .api import ApiClient, ApiError
from .config import DATA_DIR, Settings
from .history import PurchaseRecord, fetch_purchases, load_purchase_details
from .pacing import sleep_with_stop
from .report import ProductResult, summarize
from .store import ONE_SIZE

log = logging.getLogger(__name__)

STEP = 1000                 # 경쟁 최저가보다 이만큼 아래로 (사용자 결정 2026-09-17: 1순위가 되는 값)
CYCLE_GAP_SEC = 60          # 사이클 사이 쉼
PROBE_EVERY_SEC = 600       # 같은 항목의 탐침은 이 간격으로만
PROBE_MIN_UP = 5000         # 탐침 때 올리는 최소 폭
PROBE_UP_RATE = 0.05        # 탐침 때 올리는 비율 (내 가격의 5%, 1,000원 단위 올림)
PROBE_CEILING = 1.3         # 탐침으로 오르는 상한 = 실행 시작 때 가격 × 이 배수
MAX_PAGES = 20              # 보관 목록 페이지 상한
PER_PAGE = 50
SET_PAUSE_SEC = (1.0, 2.0)  # review → set 사이, 가격 변경 뒤 쉼 (판매자 API 는 스로틀 대상 상품 API 와 다르지만 사람 속도로)
NOT_LOADED_PREFIX = "판단 불가"
STOCK_QUERIES = (("live", "입찰중"), ("in_storage", "판매대기"))


class SellError(Exception):
    """가격 변경 요청을 사이트가 받지 않았다 (업무 오류 메시지)."""


@dataclass
class StockItem:
    ask_id: int                 # 보관번호 (= 목록의 id, review_live·set_live 의 ask_id)
    product_id: int
    name: str
    option: str                 # 화면 표기 (ONE SIZE / M / 275 ...)
    size: str                   # 시세 API 의 product_option.key (옵션 찾기용)
    price: int                  # 지금 판매 희망가
    status: str                 # live / in_storage
    status_text: str            # 입찰중 / 판매대기
    fee_rate: float             # 판매 수수료율 (price_breakdown.processing_fee / price, 0.005)
    expires_at: str = ""
    oid: str = ""               # 보관판매 주문번호 I-…
    buy_price: int | None = None    # 매입가 (수수료 포함) - 구매 내역 짝. None = 짝 없음
    buy_oid: str = ""

    @property
    def is_one_size(self) -> bool:
        return not self.option or self.option == ONE_SIZE

    @property
    def label(self) -> str:
        return self.name if self.is_one_size else f"{self.name} [{self.option}]"

    @property
    def product_url(self) -> str:
        return f"https://kream.co.kr/products/{self.product_id}"


def parse_stock_item(raw: dict, status_text: str) -> StockItem | None:
    release = (raw.get("product") or {}).get("release") or {}
    option = raw.get("product_option") or {}
    breakdown = raw.get("price_breakdown") or {}
    price = _int(raw.get("price"))
    if not raw.get("id") or not raw.get("product_id") or not price:
        return None
    fee = abs(_int((breakdown.get("processing_fee") or {}).get("value")))
    return StockItem(
        ask_id=int(raw["id"]), product_id=int(raw["product_id"]),
        name=str(release.get("translated_name") or release.get("name") or ""),
        option=str(option.get("name_display") or option.get("key") or ONE_SIZE),
        size=str(option.get("key") or ONE_SIZE),
        price=price, status=str(raw.get("status") or ""),
        status_text=str(((raw.get("status_display_item") or {}).get("text")) or status_text),
        fee_rate=(fee / price) if price else 0.0,
        expires_at=str(raw.get("expires_at") or ""), oid=str(raw.get("oid") or ""),
    )


def _int(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def list_stock(api: ApiClient, should_stop: Callable[[], bool] | None = None) -> list[StockItem]:
    """보관 목록의 입찰중 + 판매대기 항목."""
    out: list[StockItem] = []
    seen: set[int] = set()
    for status, text in STOCK_QUERIES:
        cursor: str | None = "1"
        pages = 0
        while cursor and pages < MAX_PAGES:
            if should_stop and should_stop():
                break
            body = api.get(f"/api/seller/inventory/items/in_stock?per_page={PER_PAGE}&cursor={cursor}&status={status}")
            pages += 1
            for raw in body.get("items") or []:
                item = parse_stock_item(raw, text)
                if item and item.ask_id not in seen:
                    seen.add(item.ask_id)
                    out.append(item)
            nxt = body.get("next_cursor")
            cursor = str(nxt) if nxt and str(nxt) != cursor else None
        log.info("보관 목록 %s: %d건", text, sum(1 for i in out if i.status == status))
    return out


# ---------------------------------------------------------------- 매입가

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def attach_buy_prices(api: ApiClient, items: list[StockItem], known: dict[int, tuple[int, str]],
                      should_stop: Callable[[], bool] | None = None) -> None:
    """매입가가 없는 항목에 구매 내역을 짝지어 buy_price 를 채운다 ([내역] history.match 와 같은 순서). known 은 보관번호 → (매입가, 매입 주문번호) 캐시."""
    todo = [i for i in items if i.buy_price is None]
    for i in todo:
        if i.ask_id in known:
            i.buy_price, i.buy_oid = known[i.ask_id]
    todo = [i for i in todo if i.buy_price is None]
    if not todo:
        return
    log.info("매입가를 모르는 항목 %d건 - 구매 내역을 읽어 짝을 맞춤", len(todo))
    purchases = fetch_purchases(api, should_stop)
    by_inv: dict[int, PurchaseRecord] = {}
    for p in purchases:
        if p.inventory_id and p.inventory_id not in by_inv:
            by_inv[p.inventory_id] = p

    def same_product(p: PurchaseRecord, i: StockItem) -> bool:
        return "취소" not in p.status and _norm(p.name_ko) == _norm(i.name) and _norm(p.option) == _norm(i.option if not i.is_one_size else "")

    pending = [i for i in todo if i.ask_id not in by_inv]
    if pending and not (should_stop and should_stop()):
        candidates = [p for p in purchases if not p.inventory_id and any(same_product(p, i) for i in pending)]
        if candidates:
            load_purchase_details(api, candidates)
            for p in candidates:
                if p.inventory_id:
                    by_inv.setdefault(p.inventory_id, p)
    matched = [(i, by_inv[i.ask_id]) for i in todo if i.ask_id in by_inv]
    if matched and not (should_stop and should_stop()):
        load_purchase_details(api, [p for _, p in matched])
    for i, p in matched:
        paid = p.total_price if p.total_price is not None else p.price
        if paid:
            i.buy_price, i.buy_oid = int(paid), p.oid or f"#{p.bid_id}"
            known[i.ask_id] = (i.buy_price, i.buy_oid)
            log.info("매입가 %s: %s ← %s %s원 (%s)", i.ask_id, i.label[:30], i.buy_oid, f"{i.buy_price:,}",
                     p.ordered_at.strftime("%m/%d") if p.ordered_at else "?")
    for i in todo:
        if i.buy_price is None:
            log.warning("매입 내역 없음: %s (보관 %s) - 이 항목은 건너뜀", i.label[:30], i.oid or i.ask_id)


def floor_price(buy_price: int, margin_rate: float, fee_rate: float) -> int:
    """하한 = 매입가 × (1 + 마진율) ÷ (1 − 판매 수수료율), 1,000원 단위 올림 - 이 값에 팔리면 정산금이 매입가 × (1 + 마진율) 이상."""
    return int(math.ceil(buy_price * (1 + margin_rate) / (1 - fee_rate) / 1000.0)) * 1000


def probe_up(price: int) -> int:
    return max(PROBE_MIN_UP, int(math.ceil(price * PROBE_UP_RATE / 1000.0)) * 1000)


def target_price(floor: int, lowest: int | None, mine: int) -> int:
    """경쟁 최저가(lowest, 내 것이 아닌 것으로 본다) 기준 목표가. 없으면 내 가격을 하한 위로."""
    if lowest is None:
        return max(floor, mine)
    return max(floor, lowest - STEP)


# ---------------------------------------------------------------- 가격 변경

def set_price(api: ApiClient, item: StockItem, price: int) -> None:
    """review_live(견적) → set_live 로 판매 희망가를 price 로 바꾼다. 사이트가 거절하면 SellError."""
    path = "/api/seller/inventory/actions/"
    entry = {"ask_id": item.ask_id, "product_id": item.product_id, "price": price}
    review = api.post(path + "review_live", {"items": [{**entry, "isValid": True, "warning": None, "processing_fee": None}]})
    _raise_if_rejected("review_live", review)
    rows = review.get("items") or []
    fee = None
    if rows and isinstance(rows[0], dict):
        fee = ((rows[0].get("review") or {}).get("processing_fee") or {}).get("value")
    if fee is None:
        raise SellError(f"견적 응답에 processing_fee 가 없음: {str(review)[:200]}")
    pacing.pause(SET_PAUSE_SEC)
    done = api.post(path + "set_live", {"items": [{**entry, "warning": None, "processing_fee": fee}]})
    log.debug("set_live 응답: %s", str(done)[:300])
    # 응답 모양을 다 알지 못하므로(2026-09-17 견적만 실측) 본문의 message 는 경고로만 남기고, 실제로 바뀌었는지는 항목 상세를 다시 읽어 확인한다
    note = done.get("message") or done.get("msg")
    if note:
        log.warning("set_live 응답 메시지: %s", note)
    detail = api.get(f"/api/seller/inventory/items/{item.ask_id}/")
    now = _int(detail.get("price"))
    if now != price:
        raise SellError(f"set_live 뒤 상세의 가격이 {now:,}원 (넣은 값 {price:,}원){f' - {note}' if note else ''}")
    item.price = price
    item.status = str(detail.get("status") or item.status)


def _raise_if_rejected(what: str, body: dict) -> None:
    """응답 본문의 업무 오류 (message / code / items[].message·success=false)."""
    msg = body.get("message") or body.get("msg")
    if msg:
        raise SellError(f"{what} 거절: {msg}")
    for row in body.get("items") or []:
        if isinstance(row, dict) and (row.get("success") is False or row.get("message") or row.get("reason")):
            raise SellError(f"{what} 거절: {row.get('message') or row.get('reason') or row}")


# ---------------------------------------------------------------- 한 항목 판정

@dataclass
class SellState:
    """실행 내내 기억하는 것."""
    known_buy: dict[int, tuple[int, str]] = field(default_factory=dict)   # 보관번호 → (매입가, 매입 주문번호)
    expected: dict[int, int] = field(default_factory=dict)                # 보관번호 → 마지막에 넣은 가격 (다음 사이클 확인용)
    base_price: dict[int, int] = field(default_factory=dict)              # 보관번호 → 처음 본 가격 (탐침 상한 기준)
    last_probe: dict[int, float] = field(default_factory=dict)            # 보관번호 → 마지막 탐침 시각 (monotonic)


def _result(item: StockItem, order: int, cycle: int, settings: Settings) -> ProductResult:
    r = ProductResult(rank=order, product_id=item.product_id, name=item.name, url=item.product_url,
                      category=f"{cycle}회차", option="" if item.is_one_size else item.option, size=item.size,
                      price_b=item.price, bid_price=item.price, buy_price=item.buy_price, margin_min=settings.sell_margin_rate)
    return r


def _won(v: int | None) -> str:
    return f"{v:,}원" if v else "없음"


def _set(r: ProductResult, status: str, detail: str) -> ProductResult:
    """판정과 사유를 채운다 - 사유 앞에 이미 적어 둔 경고(지난 사이클 가격 불일치)가 있으면 그 뒤에 잇는다."""
    r.status, r.detail = status, r.detail + detail
    return r


def sell_one(item: StockItem, order: int, cycle: int, api: ApiClient, settings: Settings, state: SellState,
             should_stop: Callable[[], bool] | None, on_status: Callable[[str], None] | None,
             floor: int | None = None) -> ProductResult:
    """항목 하나를 판정하고 필요하면 가격을 바꾼다. floor 를 주면(판매 관리 창의 항목별 하한) 그 값을, 아니면 매입가로 계산한 하한을 쓴다."""
    r = _result(item, order, cycle, settings)
    try:
        if floor is None and item.buy_price is None:
            r.status, r.detail = "건너뜀", "매입 내역을 찾지 못해 하한을 정할 수 없음 (수동으로 산 상품이면 하한을 직접 넣거나 이 프로그램으로 팔지 않음)"
            return r
        expected = state.expected.get(item.ask_id)
        if expected is not None and expected != item.price:
            log.warning("보관 %s: 지난번에 %s원으로 바꿨는데 목록은 %s원 - 다른 프로그램(No1 최저가 경쟁?)이 바꿨거나 반영 안 됨",
                        item.ask_id, f"{expected:,}", f"{item.price:,}")
            r.detail = f"지난 사이클에 넣은 {expected:,}원이 아니라 {item.price:,}원으로 읽힘 (다른 프로그램이 바꿨는지 확인) - "
        state.base_price.setdefault(item.ask_id, item.price)
        if floor is None:
            floor = floor_price(item.buy_price, settings.sell_margin_rate, item.fee_rate)
        r.price_r = floor

        market = market_mod.fetch_market_paced(api, item.product_id, should_stop, on_status)
        entry = market.find(item.size, item.option)
        if entry is None:
            have = ", ".join(f"{o.label}(size={o.key})" for o in market.options[:8])
            _set(r, "확인필요", f"시세 응답에 옵션 '{item.option}'(size={item.size}) 이 없음 (있는 옵션: {have or '없음'})")
            return r
        lowest = entry.fast
        r.price_a = lowest
        live = item.status == "live"
        log.info("[%d회차 %d번째] %s - 내 %s원 (%s), 빠른배송 최저가 %s, 하한 %s원 (매입 %s, 수수료 %.2f%%)",
                 cycle, order, item.label[:40], f"{item.price:,}", item.status_text, _won(lowest), f"{floor:,}",
                 _won(item.buy_price), item.fee_rate * 100)

        if item.price < floor:
            return _apply(api, item, floor, r, settings, state, f"내 가격이 하한 아래라 하한으로 올림 (경쟁 최저가 {_won(lowest)})")
        if live and lowest is not None and lowest == item.price:
            return _probe_or_hold(api, item, floor, r, settings, state, should_stop, on_status)
        target = target_price(floor, lowest, item.price)
        if target == item.price:
            if lowest is not None and lowest < item.price:
                _set(r, "하한대기", f"경쟁 최저가 {lowest:,}원이 하한 아래 - 하한 {floor:,}원에서 기다림")
            else:
                _set(r, "유지", f"경쟁 최저가 {_won(lowest)}, 내 가격 그대로")
            return r
        why = (f"경쟁 최저가 {lowest:,}원 − {STEP:,}원" if lowest is not None and target == lowest - STEP
               else f"경쟁 최저가 {_won(lowest)}이 하한 아래 - 하한에 둠" if lowest is not None
               else "빠른배송 판매자 없음 - 하한에 둠")
        if not live:
            why = f"판매대기 → 입찰중으로 등록: {why}"
        return _apply(api, item, target, r, settings, state, why)
    except market_mod.MarketUnavailable as e:
        r.status, r.detail = market_mod.unavailable_result(e, "확인필요", NOT_LOADED_PREFIX)
    except SellError as e:
        r.status, r.detail = "확인필요", f"가격을 바꾸지 못함: {e}"
    except ApiError as e:
        if e.is_auth_lost:
            raise product_mod.LoginNeeded(str(e)) from e
        r.status, r.detail = "확인필요", f"{NOT_LOADED_PREFIX}: {e}"
    return r


def _apply(api: ApiClient, item: StockItem, price: int, r: ProductResult, settings: Settings, state: SellState, why: str) -> ProductResult:
    old = item.price
    r.bid_price = price
    if settings.dry_run:
        _set(r, "변경대상", f"dry-run: {old:,}원 → {price:,}원 ({why})")
        return r
    set_price(api, item, price)
    state.expected[item.ask_id] = price
    pacing.pause(SET_PAUSE_SEC)
    _set(r, "가격변경", f"{old:,}원 → {price:,}원 ({why})")
    return r


def _probe_or_hold(api: ApiClient, item: StockItem, floor: int, r: ProductResult, settings: Settings, state: SellState,
                   should_stop: Callable[[], bool] | None, on_status: Callable[[str], None] | None) -> ProductResult:
    """빠른배송 최저가가 내 가격과 같다 - 내 것인지 남의 것인지 모른다. 탐침 조건이 되면 잠깐 올려 2등을 읽는다 (머리글 4)."""
    now = time.monotonic()
    last = state.last_probe.get(item.ask_id)
    if not settings.sell_probe or (last is not None and now - last < PROBE_EVERY_SEC):
        _set(r, "유지", f"빠른배송 최저가 {item.price:,}원 = 내 가격 (1순위이거나 같은 값에 줄 서 있음)")
        return r
    ceiling = int(state.base_price.get(item.ask_id, item.price) * PROBE_CEILING)
    probe = item.price + probe_up(item.price)
    if probe > ceiling:
        _set(r, "유지", f"빠른배송 최저가 {item.price:,}원 = 내 가격, 탐침 상한({ceiling:,}원) 이라 올려 보지 않음")
        return r
    old = item.price
    r.bid_price = old
    if settings.dry_run:
        _set(r, "유지", f"dry-run: 최저가 {old:,}원 = 내 가격, 실제라면 {probe:,}원으로 올려 2등을 확인")
        return r
    state.last_probe[item.ask_id] = now
    set_price(api, item, probe)
    state.expected[item.ask_id] = probe
    try:
        market = market_mod.fetch_market_paced(api, item.product_id, should_stop, on_status)
        entry = market.find(item.size, item.option)
        second = entry.fast if entry else None
    except market_mod.MarketUnavailable as e:
        # 올려 둔 채 시세를 못 읽었다 - 원래 가격으로 되돌린다
        set_price(api, item, old)
        state.expected[item.ask_id] = old
        r.status, r.detail = "확인필요", f"탐침 뒤 시세를 못 읽어 {old:,}원으로 되돌림: {e}"
        return r
    if second is None:
        target = max(floor, old)
        why = f"탐침 {probe:,}원 뒤 빠른배송 판매자가 안 보임 - 원래 가격으로"
    elif second < probe:
        target = max(floor, second - STEP)
        why = f"탐침 {probe:,}원으로 올려 2등 {second:,}원 확인 → 2등 − {STEP:,}원" + (" (하한)" if target == floor else "")
    else:
        target = max(floor, probe - STEP)
        why = f"탐침 {probe:,}원 아래에 경쟁자 없음 → 탐침가 − {STEP:,}원으로 올림"
    if target != probe:
        set_price(api, item, target)
    state.expected[item.ask_id] = target
    pacing.pause(SET_PAUSE_SEC)
    r.bid_price = target
    r.price_a = second
    r.status = "가격변경" if target != old else "유지"
    r.detail += f"{old:,}원 → {target:,}원 ({why})" if target != old else f"{old:,}원 그대로 ({why})"
    return r


# ---------------------------------------------------------------- 사이클 반복

def run(context: BrowserContext, page: Page, settings: Settings,
        should_stop: Callable[[], bool] | None = None,
        on_result: Callable[[ProductResult], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_cycle: Callable[[int, list[ProductResult]], None] | None = None,
        max_cycles: int | None = None) -> list[ProductResult]:
    """보관 목록을 반복해서 돈다. should_stop 이 True 가 되면 지금 보는 항목까지 처리하고 멈춘다."""
    stop = should_stop or (lambda: False)
    status = on_status or (lambda _t: None)
    results: list[ProductResult] = []
    api = ApiClient(page, context)
    state = SellState()
    pacer = pacing.API_PACER
    log.info(pacer.describe_setup())
    cycle = 0
    while not stop():
        cycle += 1
        started = time.monotonic()
        status(f"판매 {cycle}회차: 보관 목록 읽는 중...")
        try:
            with hangwatch.watching(page):
                items = list_stock(api, stop)
                attach_buy_prices(api, items, state.known_buy, stop)
        except ApiError as e:
            if e.is_auth_lost:
                log.info("로그인이 풀림 (%s) - 다시 로그인하고 목록을 다시 읽음", e)
                auth.ensure_logged_in(page, settings)
                api.invalidate()
                continue
            log.exception("%d회차: 보관 목록을 읽지 못함 - %d초 뒤 다시", cycle, CYCLE_GAP_SEC)
            if not sleep_with_stop(CYCLE_GAP_SEC, stop):
                break
            continue
        # 목록에서 사라진 항목(팔림·취소)은 기억에서 뺀다
        live_ids = {i.ask_id for i in items}
        for d in (state.expected, state.base_price, state.last_probe):
            for k in [k for k in d if k not in live_ids]:
                del d[k]
        log.info("===== 판매 %d회차: 보관 %d건 (매입가 있음 %d건, 하한 마진 %.0f%%) =====", cycle, len(items),
                 sum(1 for i in items if i.buy_price is not None), settings.sell_margin_rate * 100)
        cycle_results: list[ProductResult] = []
        for order, item in enumerate(items, start=1):
            if stop():
                log.info("사용자 요청으로 중지 - 남은 항목 %d건은 보지 않음", len(items) - len(cycle_results))
                break
            status(f"판매 {cycle}회차: {order}/{len(items)} {item.name[:24]} ({pacer.describe()})")
            try:
                with hangwatch.watching(page):
                    r = sell_one(item, order, cycle, api, settings, state, stop, status)
            except product_mod.LoginNeeded as e:
                log.info("로그인이 풀림 (%s) - 다시 로그인하고 이 항목을 한 번 더 봄", e)
                auth.ensure_logged_in(page, settings)
                api.invalidate()
                with hangwatch.watching(page):
                    r = sell_one(item, order, cycle, api, settings, state, stop, status)
            except Exception as e:  # noqa: BLE001
                log.exception("보관 %s 처리 중 오류", item.ask_id)
                r = _result(item, order, cycle, settings)
                r.status, r.detail = "오류", f"{type(e).__name__}: {e}"
            r.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.info("[%d회차 %d번째] 결과: %s - %s", cycle, order, r.status, r.detail)
            cycle_results.append(r)
            results.append(r)
            if on_result:
                on_result(r)
            if r.status == "중단":
                break
        log.info("===== 판매 %d회차 끝 (%d초): %s | 시세·판매자 API %d건 (%s, 차단 신호 %d번) =====", cycle,
                 int(time.monotonic() - started), summarize(cycle_results, unit="", empty="처리한 항목 없음"), api.calls, pacer.describe(), pacer.blocks)
        if on_cycle:
            on_cycle(cycle, cycle_results)
        if max_cycles and cycle >= max_cycles:
            break
        if any(r.status == "중단" for r in cycle_results) or stop():
            break
        status(f"판매 {cycle}회차 끝 - {CYCLE_GAP_SEC}초 뒤 다시")
        if not sleep_with_stop(CYCLE_GAP_SEC, stop):
            break
    return results


# ---------------------------------------------------------------- 판매 관리 창용: 항목별 규칙 + 명령 큐로 움직이는 엔진

SELL_RULES_PATH = DATA_DIR / "sell_rules.json"   # 보관번호별 하한·경쟁 여부 (판매 관리 창에서 저장)


@dataclass
class SellRule:
    floor: int | None = None        # 이 항목의 하한 (직접 넣었거나 매입가로 계산한 값). None = 아직 없음
    compete: bool = False           # 최저가 경쟁에 넣었는지
    buy_price: int | None = None    # 짝지은 매입가 (다음 실행에 구매 내역을 다시 읽지 않게)
    buy_oid: str = ""


def load_sell_rules() -> dict[int, SellRule]:
    if not SELL_RULES_PATH.exists():
        return {}
    try:
        raw = json.loads(SELL_RULES_PATH.read_text(encoding="utf-8"))
        return {int(k): SellRule(**v) for k, v in raw.items()}
    except (ValueError, TypeError, KeyError):
        log.exception("판매 규칙 파일을 읽지 못해 빈 상태로 시작: %s", SELL_RULES_PATH)
        return {}


def save_sell_rules(rules: dict[int, SellRule]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SELL_RULES_PATH.write_text(json.dumps({str(k): asdict(v) for k, v in rules.items()}, ensure_ascii=False, indent=1), encoding="utf-8")


class SellEngine:
    """판매 관리 창이 쓰는 작업 스레드. 크롬(봇 프로필)을 창이 열려 있는 동안 붙들고 있고, 명령 큐로 움직인다.

    Playwright 동기 API 는 만든 스레드에서만 부를 수 있어 브라우저·API 호출은 전부 이 스레드 안에서 한다.
    명령: ("refresh",) 목록 다시 읽기 · ("start",) 경쟁 시작 · ("stop",) 경쟁 멈춤 · ("close",) 브라우저 닫고 끝.
    경쟁 중에는 한 틱에 항목 하나씩(sell_one) 보고 그 사이사이 명령 큐를 본다. 대상은 rules 에서 compete 가 켜지고 하한이 있는 항목뿐.
    이벤트(on_event(kind, payload), 다른 스레드에서 호출됨 - GUI 는 큐로 받을 것):
      ("items", list[StockItem]) 목록 · ("result", (StockItem, ProductResult)) 판정 하나 · ("status", str) · ("running", bool) ·
      ("error", str) · ("closed", None)
    """

    def __init__(self, settings: Settings, rules: dict[int, SellRule], on_event: Callable[[str, object], None]) -> None:
        self.settings = settings
        self.rules = rules              # GUI 와 공유 - 바꾸는 쪽은 GUI, 여기서는 읽고 매입가·하한 제안만 채운다 (dict 갱신은 원자적)
        self.on_event = on_event
        self.commands: queue.Queue = queue.Queue()
        self.items: list[StockItem] = []
        self.results: list[ProductResult] = []
        self.thread = threading.Thread(target=self._main, name="sell-engine", daemon=True)
        self._stop_tick = threading.Event()

    # ---- GUI 스레드에서 부르는 것
    def start(self) -> None:
        self.thread.start()

    def request(self, cmd: str) -> None:
        if cmd in ("stop", "close"):
            self._stop_tick.set()   # 사이클 사이 쉼(sleep_with_stop)이나 틱 대기를 바로 끊는다
        self.commands.put(cmd)

    # ---- 작업 스레드
    def _emit(self, kind: str, payload: object = None) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:  # noqa: BLE001
            log.exception("판매 관리 창 이벤트 처리 중 오류 (%s)", kind)

    def _main(self) -> None:
        from playwright.sync_api import sync_playwright
        from .browser import real_chrome_context
        s = self.settings
        try:
            s.validate()
            with sync_playwright() as pw, real_chrome_context(pw, block_images=s.block_images, trim_api=s.trim_api,
                                                                show_chrome=s.show_chrome) as context:
                page = context.pages[0] if context.pages else context.new_page()
                self._emit("status", "로그인 확인 중...")
                auth.ensure_logged_in(page, s)
                api = ApiClient(page, context)
                log.info(pacing.API_PACER.describe_setup())
                self._loop(page, api)
        except Exception as e:  # noqa: BLE001
            log.exception("판매 엔진 오류")
            self._emit("error", f"{type(e).__name__}: {e}")
        finally:
            self._emit("closed")

    def _loop(self, page: Page, api: ApiClient) -> None:
        state = SellState()
        running = False
        cycle = 0
        order = 0
        pending: list[StockItem] = []
        self._refresh(page, api, state)
        while True:
            # 경쟁 중이면 명령을 기다리지 않고 바로 다음 항목, 아니면 명령이 올 때까지 기다린다
            try:
                cmd = self.commands.get(timeout=0.2 if running else None)
            except queue.Empty:
                cmd = None
            if cmd == "close":
                return
            if cmd == "refresh":
                self._refresh(page, api, state)
                pending = []
                continue
            if cmd == "stop":
                running = False
                pending = []
                self._stop_tick.clear()
                self._emit("running", False)
                self._emit("status", "경쟁 멈춤")
                continue
            if cmd == "start":
                running = True
                pending = []
                self._stop_tick.clear()
                self._emit("running", True)
                continue
            if not running:
                continue
            if not pending:
                # 사이클 시작: 목록을 다시 읽고(팔린 것 빠짐, 남이 바꾼 가격 반영) 경쟁 대상만 고른다
                cycle += 1
                order = 0
                self._refresh(page, api, state, quiet=True)
                pending = [i for i in self.items if self._rule(i).compete and self._rule(i).floor]
                if not pending:
                    self._emit("status", f"{cycle}회차: 경쟁에 넣은 항목이 없음 - {CYCLE_GAP_SEC}초 뒤 다시")
                    if not sleep_with_stop(CYCLE_GAP_SEC, self._stop_tick.is_set):
                        continue
                    continue
                log.info("===== 판매 %d회차: 경쟁 대상 %d건 / 보관 %d건 =====", cycle, len(pending), len(self.items))
            item = pending.pop(0)
            order += 1
            rule = self._rule(item)
            self._emit("status", f"{cycle}회차 {order}: {item.name[:24]} ({pacing.API_PACER.describe()})")
            try:
                with hangwatch.watching(page):
                    r = sell_one(item, order, cycle, api, self.settings, state, self._stop_tick.is_set, None, floor=rule.floor)
            except product_mod.LoginNeeded as e:
                log.info("로그인이 풀림 (%s) - 다시 로그인하고 이 항목을 한 번 더 봄", e)
                auth.ensure_logged_in(page, self.settings)
                api.invalidate()
                with hangwatch.watching(page):
                    r = sell_one(item, order, cycle, api, self.settings, state, self._stop_tick.is_set, None, floor=rule.floor)
            except Exception as e:  # noqa: BLE001
                log.exception("보관 %s 처리 중 오류", item.ask_id)
                r = _result(item, order, cycle, self.settings)
                r.status, r.detail = "오류", f"{type(e).__name__}: {e}"
            r.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.info("[%d회차 %d번째] 결과: %s - %s", cycle, order, r.status, r.detail)
            self.results.append(r)
            self._emit("result", (item, r))
            if not pending:
                self._emit("status", f"{cycle}회차 끝 - {CYCLE_GAP_SEC}초 뒤 다시")
                sleep_with_stop(CYCLE_GAP_SEC, self._stop_tick.is_set)

    def _rule(self, item: StockItem) -> SellRule:
        return self.rules.setdefault(item.ask_id, SellRule())

    def _refresh(self, page: Page, api: ApiClient, state: SellState, quiet: bool = False) -> None:
        """목록을 읽고 매입가를 붙여 GUI 로 보낸다. 매입가·하한 제안은 규칙에 채워 둔다 (경쟁 여부는 건드리지 않음)."""
        if not quiet:
            self._emit("status", "보관 목록 읽는 중...")
        try:
            with hangwatch.watching(page):
                items = list_stock(api)
                for i in items:
                    rule = self._rule(i)
                    if rule.buy_price:
                        i.buy_price, i.buy_oid = rule.buy_price, rule.buy_oid
                attach_buy_prices(api, items, state.known_buy)
        except ApiError as e:
            if e.is_auth_lost:
                auth.ensure_logged_in(page, self.settings)
                api.invalidate()
                return self._refresh(page, api, state, quiet)
            log.exception("보관 목록을 읽지 못함")
            self._emit("error", f"보관 목록을 읽지 못함: {e}")
            return
        for i in items:
            rule = self._rule(i)
            if i.buy_price and not rule.buy_price:
                rule.buy_price, rule.buy_oid = i.buy_price, i.buy_oid
            if rule.floor is None and i.buy_price:
                rule.floor = floor_price(i.buy_price, self.settings.sell_margin_rate, i.fee_rate)
        gone = set(self.rules) - {i.ask_id for i in items}
        for k in gone:   # 팔렸거나 취소된 항목의 규칙은 지운다
            del self.rules[k]
        save_sell_rules(self.rules)
        self.items = items
        self._emit("items", list(items))
        if not quiet:
            self._emit("status", f"보관 {len(items)}건 (매입가 있음 {sum(1 for i in items if i.buy_price)}건)")


__all__ = ["run", "list_stock", "attach_buy_prices", "floor_price", "target_price", "set_price", "StockItem",
           "SellRule", "SellEngine", "load_sell_rules", "save_sell_rules"]

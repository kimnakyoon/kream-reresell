"""상품 상세 API 로 옵션별 시세(A 빠른배송 가격 · B 즉시 판매가)를 한 번에 읽는다 - [재입찰] · [입찰] 공용.

GET api.kream.co.kr/api/p/products/{상품ID}?base_product_id={상품ID} 응답의 sales_options[] 에 옵션마다
  - product_option.key          구매 페이지 주소의 size 값 (ONE SIZE / 240 / Ungraded ...) - 입찰 기록의 size 와 같다
  - product_option.name_display 화면 표기 (ONE SIZE / W240 / Ungraded A (Pack Ver.) ...) - 마이페이지·모달·패널의 옵션 표기와 같다
  - lowest_100                  보관(새 상품) 판매 최저가 = 구매하기 모달의 **빠른배송 가격 A**. 없으면(null) 지금 빠른배송 판매자가 없음
  - highest_bid                 가장 높은 구매 입찰가 = 구매 페이지의 **즉시 판매가 B**. 없으면(null) 구매 입찰이 하나도 없음
가 들어 있다 (2026-09-13 실측: ONE SIZE 2건 + 옵션 2건을 모달·구매 페이지와 앞뒤로 대조해 4건 모두 일치).
같이 오는 lowest_95(95점 보관 최저가)·lowest_normal(일반배송 최저가 - A 보다 쌀 수도 있다)·market.total_sales(전체 거래 수, 기간·배송 구분 없음)는
쓰지 않는다 - A 는 반드시 lowest_100 이고, 30일 빠른배송 건수는 지금처럼 체결 내역 패널로 센다.

이 호출 하나가 상품 페이지 + 구매하기 모달 + 구매 페이지(페이지 이동 2번, API 수십 건)를 대신한다. 상품 페이지가 열릴 때마다 부르는
API 라 2026-09-05 실측 때 sales·asks 가 막히는 순간에도 정상이던 부류이고, No1 Seller Center 는 이 호출을 19초마다 5일 연속
(시간당 200~230건) 보내면서 한 번도 막히지 않았다 (2026-09-13 로그 조사). 페이지 안에서 부른다 (api.ApiClient).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from . import pacing
from .api import ApiClient, ApiError
from .store import ONE_SIZE

log = logging.getLogger(__name__)


class MarketUnavailable(Exception):
    """시세를 못 읽었다. block_signal 이면 사이트가 막았을 때 나는 모양(무응답·5xx 등) - 연달아 나면 쉬어야 한다.
    gone 이면 상품이 없는 응답(404 등) - 그 상품만 건너뛴다. stopped 면 틱을 기다리던 중 중지 요청이 온 것."""

    def __init__(self, message: str, block_signal: bool = False, gone: bool = False, stopped: bool = False) -> None:
        super().__init__(message)
        self.block_signal = block_signal
        self.gone = gone
        self.stopped = stopped


def unavailable_result(e: MarketUnavailable, status: str, not_loaded_prefix: str) -> tuple[str, str]:
    """MarketUnavailable 을 결과 (판정, 사유) 로. 중지 요청 → 중단, 상품 없음 → status + 확인 안내,
    그 밖(차단 신호 · 응답 모양 다름) → status + '판단 불가' 사유 (not_loaded_prefix 로 시작해 부르는 쪽의 '연달아 난 수' 에 들어간다 - sitewait).
    간격은 fetch_market_paced 가 이미 늘렸다."""
    if e.stopped:
        return "중단", str(e)
    if e.gone:
        return status, f"시세를 읽지 못함: {e} (상품이 내려갔거나 주소가 바뀌었는지 확인)"
    return status, f"{not_loaded_prefix}: {e}"


@dataclass
class OptionPrice:
    key: str                    # 주소 size 값 (product_option.key)
    label: str                  # 화면 표기 (name_display)
    fast: int | None            # A: 빠른배송(보관 100) 최저가. None = 빠른배송 판매자 없음
    sell: int | None            # B: 즉시 판매가(최고 구매 입찰가). None = 구매 입찰 없음


@dataclass
class ProductMarket:
    product_id: int
    options: list[OptionPrice] = field(default_factory=list)

    @property
    def is_one_size(self) -> bool:
        return len(self.options) == 1 and self.options[0].key == ONE_SIZE

    def find(self, key: str = "", label: str = "") -> OptionPrice | None:
        """size 값(key)으로 먼저, 없으면 화면 표기(label)로 찾는다. ONE SIZE 상품은 둘 다 'ONE SIZE'."""
        for o in self.options:
            if key and o.key == key:
                return o
        for o in self.options:
            if label and o.label == label:
                return o
        return None


def _amount(value) -> int | None:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def parse_market(product_id: int, body: dict) -> ProductMarket:
    raw_options = body.get("sales_options")
    if not isinstance(raw_options, list):
        raise MarketUnavailable(f"상품 {product_id} 상세 응답에 sales_options 가 없음")
    market = ProductMarket(product_id=product_id)
    for entry in raw_options:
        if not isinstance(entry, dict):
            continue
        po = entry.get("product_option") or {}
        key = str(po.get("key") or entry.get("option") or "").strip()
        label = str(po.get("name_display") or po.get("name") or key).strip()
        if not key and not label:
            continue
        market.options.append(OptionPrice(key=key or label, label=label or key,
                                          fast=_amount(entry.get("lowest_100")), sell=_amount(entry.get("highest_bid"))))
    return market


def fetch_market(client: ApiClient, product_id: int) -> ProductMarket:
    """상품 상세 API 한 번으로 옵션별 A·B 를 읽는다. 못 읽으면 MarketUnavailable."""
    path = f"/api/p/products/{product_id}?base_product_id={product_id}"
    try:
        body = client.get(path)
    except ApiError as e:
        raise MarketUnavailable(f"시세 API {e}", block_signal=e.is_block_signal, gone=e.is_gone) from e
    market = parse_market(product_id, body)
    log.info("시세 API: 상품 %d 옵션 %d개 - %s", product_id, len(market.options),
             ", ".join(f"{o.label} A={_fmt(o.fast)} B={_fmt(o.sell)}" for o in market.options[:12])
             + (" ..." if len(market.options) > 12 else ""))
    return market


def fetch_market_paced(client: ApiClient, product_id: int, should_stop: Callable[[], bool] | None = None,
                       on_status: Callable[[str], None] | None = None) -> ProductMarket:
    """틱(pacing.API_PACER)을 지켜 fetch_market 을 부르고 결과를 페이서에 알린다 - [재입찰] · [입찰] 이 시세를 읽는 유일한 경로.

    차단 신호(MarketUnavailable.block_signal)면 페이서가 간격을 늘린다. 틱을 기다리는 중 중지 요청이 오면 stopped 로 올린다.
    """
    if not pacing.API_PACER.wait_turn(should_stop, on_status):
        raise MarketUnavailable("중지 요청 - 시세를 읽지 않음", stopped=True)
    try:
        market = fetch_market(client, product_id)
    except MarketUnavailable as e:
        if e.block_signal:
            pacing.API_PACER.report_block(str(e))
        raise
    pacing.API_PACER.report_ok()
    return market


def _fmt(v: int | None) -> str:
    return f"{v:,}" if v else "없음"

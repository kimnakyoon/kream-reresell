"""랭킹 상품을 순서대로 보며 조건에 맞으면 구매 입찰까지 진행."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from playwright.sync_api import BrowserContext, Page

from . import bid as bid_mod
from . import product as product_mod
from .config import Settings
from .debug import dump
from .ranking import RankedProduct
from .report import ProductResult
from .store import BidRecord, append_run_log, save_bid

if TYPE_CHECKING:  # cancel 이 pipeline 을 import 하므로 타입 표기용으로만
    from .cancel import OpenBid, OpenBids

log = logging.getLogger(__name__)


def evaluate(page: Page, url: str, r: ProductResult, settings: Settings, stop_early: bool = True,
             price_limit: bool = True) -> str | None:
    """상품 페이지를 열어 거래량 / A / B 를 읽어 r 에 채우고 입찰 조건을 판정한다.

    조건 미달이면 사유 문자열을, 충족이면 None 을 돌려준다. 입찰과 입찰취소가 같은 기준을 쓴다.
    stop_early 가 True 면 거래량 미달에서 바로 돌아온다 (A/B 는 읽지 않음).
    price_limit 가 True 면 A 가 상품 금액 상한을 넘는 상품은 B 를 읽지 않고 바로 돌아온다 (입찰 전용 규칙).
    조건을 다 봤으면 page 는 구매 페이지(/buy/{id}) 에 있다.
    """
    pid = r.product_id
    r.name = product_mod.open_product(page, url) or r.name
    if settings.inspect:
        dump(page, f"{pid}_0_product")

    stats = product_mod.read_sales_stats(page, settings.lookback_days, settings.min_fast_sales)
    r.fast_sales, r.total_sales = stats.fast_in_window, stats.total_in_window
    sales_reason = None
    if stats.fast_in_window < settings.min_fast_sales:
        sales_reason = f"{settings.lookback_days}일 빠른배송 {stats.fast_in_window}건 < {settings.min_fast_sales}건"
        if stop_early:
            return sales_reason

    r.price_a = product_mod.read_price_a_and_go_to_buy(page, pid)
    if price_limit and settings.rules.over_limit(r.price_a):
        log.info("A %s원 > 상품 금액 상한 %s원 - 바로 건너뜀", f"{r.price_a:,}", f"{settings.rules.max_price_a:,}")
        return f"A {r.price_a:,}원 > 상품 금액 상한 {settings.rules.max_price_a:,}원"
    if settings.inspect:
        dump(page, f"{pid}_1_buy_page")
    r.price_b = product_mod.read_price_b(page)

    rate = r.margin_rate or 0.0
    tier = settings.rules.tier_for(r.price_a)
    r.margin_min = tier.margin_rate if tier else None
    log.info("A-B = %s원 (A의 %.1f%%), 기준 %s", f"{r.margin:,}",
             rate * 100, tier.describe() if tier else "없음 (A 가 설정한 금액 구간 밖)")
    if sales_reason:
        return sales_reason
    if tier is None:
        return f"A {r.price_a:,}원은 설정한 금액 구간에 없음"
    if rate <= r.margin_min:
        return f"마진 {rate*100:.1f}% <= 기준 {tier.margin_pct:g}% ({tier.label})"
    return None


def process_product(context: BrowserContext, item: RankedProduct, settings: Settings,
                    open_bids: "OpenBids | None" = None) -> ProductResult:
    """상품 하나를 새 탭에서 처리한다.

    기준에 맞으면 입찰을 시도하고, 시도 중 안전장치에 걸리거나 화면이 예상과 다르면 그 상품은 건너뛰고 다음으로 간다.
    open_bids 에 상품 ID 를 못 읽은 입찰이 있으면 상품 페이지 제목(= 마이페이지 표기)으로 대조해 이미 입찰 중이면 건너뛴다.
    """
    page: Page = context.new_page()
    pid = item.product_id
    r = ProductResult(rank=item.rank, product_id=pid, name=item.name, url=item.url, category=item.category)
    try:
        log.info("[%s %d위] %s (%s)", item.category, item.rank, r.name, item.url)
        reason = evaluate(page, item.url, r, settings, stop_early=not settings.force)
        ob = open_bids.by_name(r.name) if open_bids is not None and not settings.force else None
        if ob is not None:
            r.status, r.detail = "건너뜀", _open_bid_detail(ob) + " - 상품명으로 확인"
            r.bid_price = ob.price
            return r
        if reason and not settings.force:
            r.status, r.detail = "건너뜀", reason
            return r

        r.bid_price, r.bid_days = r.price_b, settings.bid_days
        if settings.dry_run:
            r.status, r.detail = "입찰대상", f"dry-run: {r.bid_price:,}원에 {settings.bid_days}일 입찰 조건 충족"
            return r

        bid_mod.fill_bid_form(page, r.bid_price, settings.bid_days, settings, pid)
        bid_mod.choose_warehouse_and_points(page, settings, pid)
        try:
            bid_mod.submit_bid(page, r.bid_price, settings, pid)
        except bid_mod.BidUncertain as e:
            # 입찰이 들어갔을 수 있으므로 기록해 두고 사람이 확인하게 한다
            _record_bid(r, r.fast_sales or 0, settings, note=" (확인 필요)")
            r.status, r.detail = "확인필요", f"{e} - 마이페이지에서 입찰 여부 확인"
            return r
        _record_bid(r, r.fast_sales or 0, settings)
        r.status, r.detail = "입찰완료", f"{r.bid_price:,}원 / {settings.bid_days}일 / 창고보관"
        return r
    except product_mod.SkipProduct as e:
        r.status, r.detail = "건너뜀", str(e)
        return r
    except bid_mod.StoppedBeforeSubmit as e:
        r.status, r.detail = "중단", str(e)
        return r
    except bid_mod.BidAborted as e:
        # 입찰 화면이 예상과 달라 넣지 못한 것 - 이 상품만 건너뛰고 다음 상품으로
        log.warning("[%d위] 입찰 못 함, 건너뜀: %s", item.rank, e)
        r.status, r.detail = "건너뜀", f"입찰 못 함: {e}"
        return r
    except Exception as e:  # noqa: BLE001
        dump(page, f"{pid}_error")
        log.exception("상품 %s 처리 중 오류", pid)
        r.status, r.detail = "오류", f"{type(e).__name__}: {e}"
        return r
    finally:
        r.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info("[%d위] 결과: %s - %s", item.rank, r.status, r.detail)
        append_run_log({
            "category": r.category, "rank": r.rank, "product_id": pid, "name": r.name,
            "fast_sales": r.fast_sales if r.fast_sales is not None else "",
            "price_a": r.price_a or "", "price_b": r.price_b or "",
            "status": r.status, "detail": r.detail,
        })
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def _record_bid(r: ProductResult, fast_sales: int, settings: Settings, note: str = "") -> None:
    save_bid(BidRecord(
        product_id=r.product_id, name=r.name, price=r.bid_price or 0, bid_days=settings.bid_days,
        placed_at=datetime.now().isoformat(timespec="seconds") + note,
        fast_sales_30d=fast_sales, price_a=r.price_a or 0, price_b=r.price_b or 0,
    ))


def _open_bid_detail(ob: "OpenBid") -> str:
    if ob.price:
        return f"마이페이지에 이미 입찰 중 (입찰 #{ob.bid_id}, {ob.price:,}원, 마감 {ob.deadline or ob.expires_at[:10]})"
    return f"마이페이지에 이미 입찰 중 (입찰 #{ob.bid_id})"


def run(context: BrowserContext, items: list[RankedProduct], settings: Settings,
        should_stop: Callable[[], bool] | None = None,
        on_result: Callable[[ProductResult], None] | None = None,
        open_bids: "OpenBids | None" = None) -> list[ProductResult]:
    """open_bids: 마이페이지 구매 입찰 탭에 지금 살아 있는 입찰 (cancel.OpenBids).

    거기에 있는 상품만 건너뛴다. 그 밖의 상품은 (예전에 입찰했다가 체결·만료로 사라진 것도) 기준에 따라 다시 판정해
    조건이 맞으면 입찰을 시도하고, 시도가 안 되면 건너뛰고 다음 상품으로 간다.
    `data/bids.json` 은 건너뛰기 기준이 아니라 목록의 입찰을 상품 ID 로 잇는 기록으로만 쓴다.
    """
    results: list[ProductResult] = []
    for item in items:
        if should_stop and should_stop():
            log.info("사용자 요청으로 중지 - 남은 %d개는 보지 않음", len(items) - len(results))
            break
        if open_bids is not None and item.product_id in open_bids and not settings.force:
            ob = open_bids.by_product[item.product_id]
            log.info("[%d위] %s - 마이페이지에 이미 입찰 중, 건너뜀", item.rank, item.name)
            results.append(ProductResult(
                rank=item.rank, product_id=item.product_id, name=item.name, url=item.url,
                category=item.category, status="건너뜀", detail=_open_bid_detail(ob), bid_price=ob.price,
            ))
            if on_result:
                on_result(results[-1])
            continue
        results.append(process_product(context, item, settings, open_bids))
        if on_result:
            on_result(results[-1])
    return results

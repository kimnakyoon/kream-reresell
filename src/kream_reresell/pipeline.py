"""랭킹 상품을 순서대로 보며 조건에 맞으면 구매 입찰까지 진행."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from playwright.sync_api import BrowserContext, Page

from . import bid as bid_mod
from . import product as product_mod
from .config import Settings
from .debug import dump
from .ranking import RankedProduct
from .report import ProductResult
from .store import BidRecord, append_run_log, load_bids, save_bid

log = logging.getLogger(__name__)


def process_product(context: BrowserContext, item: RankedProduct, settings: Settings) -> ProductResult:
    """상품 하나를 새 탭에서 처리한다."""
    page: Page = context.new_page()
    pid = item.product_id
    r = ProductResult(rank=item.rank, product_id=pid, name=item.name, url=item.url, category=item.category)
    try:
        r.name = product_mod.open_product(page, item.url) or item.name
        log.info("[%s %d위] %s (%s)", item.category, item.rank, r.name, item.url)
        if settings.inspect:
            dump(page, f"{pid}_0_product")

        stats = product_mod.read_sales_stats(page, settings.lookback_days, settings.min_fast_sales)
        r.fast_sales, r.total_sales = stats.fast_in_window, stats.total_in_window
        if stats.fast_in_window < settings.min_fast_sales and not settings.force:
            r.status, r.detail = "건너뜀", f"{settings.lookback_days}일 빠른배송 {stats.fast_in_window}건 < {settings.min_fast_sales}건"
            return r

        r.price_a = product_mod.read_price_a_and_go_to_buy(page, pid)
        if settings.inspect:
            dump(page, f"{pid}_1_buy_page")
        r.price_b = product_mod.read_price_b(page)

        rate = r.margin_rate or 0.0
        log.info("A-B = %s원 (A의 %.1f%%), 기준 %.0f%%", f"{r.margin:,}", rate * 100, settings.min_margin_rate * 100)
        if rate <= settings.min_margin_rate and not settings.force:
            r.status, r.detail = "건너뜀", f"마진 {rate*100:.1f}% <= 기준 {settings.min_margin_rate*100:.0f}%"
            return r

        r.bid_price, r.bid_days = r.price_b, settings.bid_days
        if settings.dry_run:
            r.status, r.detail = "입찰대상", f"dry-run: {r.price_b:,}원에 {settings.bid_days}일 입찰 조건 충족"
            return r

        bid_mod.fill_bid_form(page, r.price_b, settings.bid_days, settings, pid)
        bid_mod.choose_warehouse_and_points(page, settings, pid)
        try:
            bid_mod.submit_bid(page, r.price_b, settings, pid)
        except bid_mod.BidUncertain as e:
            # 입찰이 들어갔을 수 있으므로 기록해 두고(중복 입찰 방지) 사람이 확인하게 한다
            _record_bid(r, stats.fast_in_window, settings, note=" (확인 필요)")
            r.status, r.detail = "확인필요", f"{e} - 마이페이지에서 입찰 여부 확인"
            return r
        _record_bid(r, stats.fast_in_window, settings)
        r.status, r.detail = "입찰완료", f"{r.price_b:,}원 / {settings.bid_days}일 / 창고보관"
        return r
    except product_mod.SkipProduct as e:
        r.status, r.detail = "건너뜀", str(e)
        return r
    except bid_mod.StoppedBeforeSubmit as e:
        r.status, r.detail = "중단", str(e)
        return r
    except bid_mod.BidAborted as e:
        r.status, r.detail = "중단", f"안전장치: {e}"
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
        product_id=r.product_id, name=r.name, price=r.price_b or 0, bid_days=settings.bid_days,
        placed_at=datetime.now().isoformat(timespec="seconds") + note,
        fast_sales_30d=fast_sales, price_a=r.price_a or 0, price_b=r.price_b or 0,
    ))


def run(context: BrowserContext, items: list[RankedProduct], settings: Settings,
        should_stop: Callable[[], bool] | None = None,
        on_result: Callable[[ProductResult], None] | None = None) -> list[ProductResult]:
    already = load_bids()
    results: list[ProductResult] = []
    for item in items:
        if should_stop and should_stop():
            log.info("사용자 요청으로 중지 - 남은 %d개는 보지 않음", len(items) - len(results))
            break
        if item.product_id in already and not settings.force:
            prev = already[item.product_id]
            log.info("[%d위] %s - 이미 입찰한 상품, 건너뜀", item.rank, item.name)
            results.append(ProductResult(
                rank=item.rank, product_id=item.product_id, name=item.name, url=item.url,
                category=item.category, status="건너뜀", detail=f"이미 입찰함 ({prev.placed_at[:16]}, {prev.price:,}원)",
                bid_price=prev.price, bid_days=prev.bid_days,
            ))
            if on_result:
                on_result(results[-1])
            continue
        results.append(process_product(context, item, settings))
        if on_result:
            on_result(results[-1])
    return results

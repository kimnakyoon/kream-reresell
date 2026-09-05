"""랭킹 상품을 순서대로 보며 조건에 맞으면 구매 입찰까지 진행.

옵션(사이즈)이 있는 상품은 옵션마다 따로 판정한다: 패널에서 옵션을 하나씩 골라 30일 빠른배송 건수를 다 센 뒤,
기준을 넘는 옵션만 구매하기 모달에서 그 옵션을 골라 A · B 를 읽고 입찰한다 (ONE SIZE 상품과 같은 순서).
결과는 옵션마다 한 줄이다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from playwright.sync_api import BrowserContext, Page

from . import auth
from . import bid as bid_mod
from . import product as product_mod
from .config import Settings
from .debug import dump
from .ranking import RankedProduct
from .report import ProductResult
from . import pacing
from .sitewait import TROUBLE_STREAK, wait_until_site_back
from .store import ONE_SIZE, BidRecord, append_run_log, save_bid

if TYPE_CHECKING:  # cancel 이 pipeline 을 import 하므로 타입 표기용으로만
    from .cancel import OpenBid, OpenBids

log = logging.getLogger(__name__)

NOT_LOADED_PREFIX = "판단 불가"   # 사이트가 체결 내역을 안 줘서 판정하지 못한 결과의 사유 머리 (연달아 난 수를 셀 때 쓴다)


def _skip_detail(e: product_mod.SkipProduct) -> str:
    """SkipProduct 를 결과 사유로. 내역을 못 불러온 것은 '판단 불가:' 를 앞에 붙여 거래가 없는 것과 구별한다."""
    if isinstance(e, product_mod.SalesNotLoaded):
        return f"{NOT_LOADED_PREFIX}: {e}"
    return str(e)


def is_site_trouble(r: ProductResult) -> bool:
    """사이트가 응답을 안 줘서 생겼을 수 있는 결과인지 (판단 불가 · 오류)."""
    return r.status == "오류" or r.detail.startswith(NOT_LOADED_PREFIX)


def evaluate(page: Page, url: str, r: ProductResult, settings: Settings, stop_early: bool = True,
             price_limit: bool = True, option: str | None = None) -> str | None:
    """상품 페이지를 열어 거래량 / A / B 를 읽어 r 에 채우고 입찰 조건을 판정한다.

    조건 미달이면 사유 문자열을, 충족이면 None 을 돌려준다. 입찰과 입찰취소·재입찰이 같은 기준을 쓴다.
    stop_early 가 True 면 거래량 미달에서 바로 돌아온다 (A/B 는 읽지 않음).
    price_limit 가 True 면 A 가 상품 금액 상한을 넘는 상품은 B 를 읽지 않고 바로 돌아온다 (입찰 전용 규칙).
    option 을 주면 (옵션 상품) 그 옵션의 거래량·가격으로 판정한다. 조건을 다 봤으면 page 는 구매 페이지(/buy/{id}) 에 있다.
    """
    r.name = product_mod.open_product(page, url) or r.name
    if settings.inspect:
        dump(page, f"{r.product_id}_0_product")

    pacing.before_sales_request()
    stats = product_mod.read_sales_stats(page, settings.lookback_days, settings.min_fast_sales, option)
    r.fast_sales, r.total_sales = stats.fast_in_window, stats.total_in_window
    sales_reason = _sales_reason(stats, settings)
    if sales_reason and stop_early:
        return sales_reason
    return _judge_prices(page, r, settings, price_limit, option, sales_reason)


def _sales_reason(stats: product_mod.SalesStats, settings: Settings) -> str | None:
    if stats.fast_in_window < settings.min_fast_sales:
        return f"{settings.lookback_days}일 빠른배송 {stats.fast_in_window}건 < {settings.min_fast_sales}건"
    return None


def _judge_prices(page: Page, r: ProductResult, settings: Settings, price_limit: bool,
                  option: str | None, sales_reason: str | None) -> str | None:
    """상품 페이지에 있는 상태에서 A (모달) → 구매 페이지 → B 를 읽고 마진을 판정한다. 미달이면 사유."""
    pid = r.product_id
    r.price_a = product_mod.read_price_a_and_go_to_buy(page, pid, option)
    r.size = product_mod.size_from_url(page.url) or (ONE_SIZE if not option else "")
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
                    open_bids: "OpenBids | None" = None, should_stop: Callable[[], bool] | None = None,
                    on_status: Callable[[str], None] | None = None) -> list[ProductResult]:
    """상품 하나를 새 탭에서 처리한다. ONE SIZE 상품은 결과 한 줄, 옵션 상품은 옵션마다 한 줄.

    기준에 맞으면 입찰을 시도하고, 시도 중 안전장치에 걸리거나 화면이 예상과 다르면 그 상품(옵션)은 건너뛰고 다음으로 간다.
    open_bids 에 상품 ID 를 못 읽은 입찰이 있으면 상품 페이지 제목(= 마이페이지 표기)으로 대조해 이미 입찰 중이면 건너뛴다.
    """
    page: Page = context.new_page()
    pid = item.product_id
    results: list[ProductResult] = []

    def new_result(option: str = "") -> ProductResult:
        return ProductResult(rank=item.rank, product_id=pid, name=item.name, url=item.url, category=item.category,
                             option=option)

    try:
        log.info("[%s %d위] %s (%s)", item.category, item.rank, item.name, item.url)
        r = new_result()
        try:
            r.name = product_mod.open_product(page, item.url) or r.name
            if settings.inspect:
                dump(page, f"{pid}_0_product")
            pacing.before_sales_request(should_stop, on_status=on_status)
            product_mod.open_sales_panel(page)
            options = product_mod.list_options(page)
        except product_mod.SkipProduct as e:
            r.status, r.detail = "건너뜀", _skip_detail(e)
            return _done(results, r, item)

        if not options:
            # ONE SIZE 상품: 패널은 열려 있다. 거래량을 센 뒤 판정·입찰
            try:
                stats = product_mod.count_sales(page, settings.lookback_days, settings.min_fast_sales)
                product_mod.close_sales_panel(page)
            except product_mod.SkipProduct as e:
                r.status, r.detail = "건너뜀", _skip_detail(e)
                return _done(results, r, item)
            _judge_and_bid(page, r, stats, item, settings, open_bids)
            return _done(results, r, item)

        if settings.options:
            wanted = [o for o in options if o in settings.options]
            log.info("옵션 %d개 중 지정한 %s 만 봄", len(options), ", ".join(wanted) or "(없음)")
            if not wanted:
                r.status, r.detail = "건너뜀", f"지정한 옵션 {', '.join(settings.options)} 이 없음 (있는 옵션: {', '.join(options)})"
                return _done(results, r, item)
            options = wanted
        else:
            log.info("옵션 %d개: %s", len(options), ", ".join(options))

        # 옵션 상품: 먼저 패널에서 옵션마다 거래량을 센다 (페이지 이동 없음). 기준을 넘는 옵션만 뒤에서 A/B 를 읽는다
        stats_by_option: dict[str, product_mod.SalesStats | str] = {}
        for n, label in enumerate(options):
            if n:
                pacing.pause(pacing.OPTION_PAUSE_SEC, should_stop)   # 옵션을 바꿀 때마다 sales 요청이 나간다 - 사람 속도로
            if should_stop and should_stop():
                stats_by_option[label] = "중지 요청"
                continue
            pacing.before_sales_request(should_stop, on_status=on_status)
            try:
                product_mod.select_option(page, label)
                stats_by_option[label] = product_mod.count_sales(page, settings.lookback_days, settings.min_fast_sales, label)
            except product_mod.SkipProduct as e:
                stats_by_option[label] = _skip_detail(e) if isinstance(e, product_mod.SalesNotLoaded) else f"거래량을 세지 못함: {e}"
        try:
            product_mod.close_sales_panel(page)
        except product_mod.SkipProduct:
            pass
        name = r.name
        for label in options:
            r = new_result(label)
            r.name = name
            st = stats_by_option[label]
            if isinstance(st, str):
                r.status, r.detail = "건너뜀", st
            else:
                _judge_and_bid(page, r, st, item, settings, open_bids)
            _done(results, r, item)
        return results
    except Exception as e:  # noqa: BLE001
        dump(page, f"{pid}_error")
        log.exception("상품 %s 처리 중 오류", pid)
        r = new_result()
        r.status, r.detail = "오류", f"{type(e).__name__}: {e}"
        return _done(results, r, item)
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def _done(results: list[ProductResult], r: ProductResult, item: RankedProduct) -> list[ProductResult]:
    """결과 한 줄을 마무리한다 (시각, 로그, run_log)."""
    r.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("[%d위%s] 결과: %s - %s", item.rank, f" {r.option}" if r.option else "", r.status, r.detail)
    append_run_log({
        "category": r.category, "rank": r.rank, "product_id": r.product_id, "name": r.name, "option": r.option,
        "fast_sales": r.fast_sales if r.fast_sales is not None else "",
        "price_a": r.price_a or "", "price_b": r.price_b or "",
        "status": r.status, "detail": r.detail,
    })
    results.append(r)
    return results


def _judge_and_bid(page: Page, r: ProductResult, stats: product_mod.SalesStats, item: RankedProduct,
                   settings: Settings, open_bids: "OpenBids | None") -> None:
    """거래량(stats)을 안 상태에서 이미 입찰 중인지 → 거래량 → A/B·마진 → 입찰 순으로 판정해 r.status/detail 을 채운다.

    ONE SIZE 상품(r.option 비움)과 옵션 상품(r.option = 옵션 표기) 이 같은 순서를 쓴다. 옵션 상품은 앞 옵션을 입찰하고 나면
    구매 완료 화면에 있으므로 상품 페이지를 다시 연다.
    """
    option = r.option or None
    r.fast_sales, r.total_sales = stats.fast_in_window, stats.total_in_window
    try:
        if open_bids is not None and not settings.force:
            ob = open_bids.find(r.product_id, r.option or ONE_SIZE)
            by_name = ob is None and (ob := open_bids.by_name(r.name, r.option or ONE_SIZE)) is not None
            if ob is not None:
                r.status, r.detail = "건너뜀", _open_bid_detail(ob) + (" - 상품명으로 확인" if by_name else "")
                r.bid_price = ob.price
                return
        sales_reason = _sales_reason(stats, settings)
        if sales_reason and not settings.force:
            r.status, r.detail = "건너뜀", sales_reason
            return
        if not product_mod.on_product_page(page, r.product_id):
            product_mod.open_product(page, item.url)
        pacing.before_sales_request()
        reason = _judge_prices(page, r, settings, price_limit=True, option=option, sales_reason=sales_reason)
        if reason and not settings.force:
            r.status, r.detail = "건너뜀", reason
            return
        _place_bid(page, r, settings)
    except product_mod.SkipProduct as e:
        r.status, r.detail = "건너뜀", _skip_detail(e)
    except bid_mod.StoppedBeforeSubmit as e:
        r.status, r.detail = "중단", str(e)
    except bid_mod.BidAborted as e:
        # 입찰 화면이 예상과 달라 넣지 못한 것 - 이 상품(옵션)만 건너뛰고 다음으로
        log.warning("[%d위%s] 입찰 못 함, 건너뜀: %s", item.rank, f" {r.option}" if r.option else "", e)
        r.status, r.detail = "건너뜀", f"입찰 못 함: {e}"


def _place_bid(page: Page, r: ProductResult, settings: Settings) -> None:
    """구매 페이지에 있는 상태에서 B 로 입찰한다 (dry-run 이면 입찰대상으로만)."""
    r.bid_price, r.bid_days = r.price_b, settings.bid_days
    if settings.dry_run:
        r.status, r.detail = "입찰대상", f"dry-run: {r.bid_price:,}원에 {settings.bid_days}일 입찰 조건 충족"
        return
    pid = r.product_id
    bid_mod.fill_bid_form(page, r.bid_price, settings.bid_days, settings, pid)
    bid_mod.choose_warehouse_and_points(page, settings, pid)
    try:
        bid_mod.submit_bid(page, r.bid_price, settings, pid)
    except bid_mod.BidUncertain as e:
        # 입찰이 들어갔을 수 있으므로 기록해 두고 사람이 확인하게 한다
        _record_bid(r, r.fast_sales or 0, settings, note=" (확인 필요)")
        r.status, r.detail = "확인필요", f"{e} - 마이페이지에서 입찰 여부 확인"
        return
    _record_bid(r, r.fast_sales or 0, settings)
    r.status, r.detail = "입찰완료", f"{r.bid_price:,}원 / {settings.bid_days}일 / 창고보관" + (f" / {r.option}" if r.option else "")


def _record_bid(r: ProductResult, fast_sales: int, settings: Settings, note: str = "") -> None:
    save_bid(BidRecord(
        product_id=r.product_id, name=r.name, price=r.bid_price or 0, bid_days=settings.bid_days,
        placed_at=datetime.now().isoformat(timespec="seconds") + note,
        fast_sales_30d=fast_sales, price_a=r.price_a or 0, price_b=r.price_b or 0,
        option=r.option or ONE_SIZE, size=r.size or ONE_SIZE,
    ))


def _open_bid_detail(ob: "OpenBid") -> str:
    opt = f", {ob.option}" if ob.option and ob.option != ONE_SIZE else ""
    if ob.price:
        return f"마이페이지에 이미 입찰 중 (입찰 #{ob.bid_id}{opt}, {ob.price:,}원, 마감 {ob.deadline or ob.expires_at[:10]})"
    return f"마이페이지에 이미 입찰 중 (입찰 #{ob.bid_id}{opt})"


def run(context: BrowserContext, items: list[RankedProduct], settings: Settings,
        should_stop: Callable[[], bool] | None = None,
        on_result: Callable[[ProductResult], None] | None = None,
        open_bids: "OpenBids | None" = None,
        page: Page | None = None,
        on_status: Callable[[str], None] | None = None) -> list[ProductResult]:
    """open_bids: 마이페이지 구매 입찰 탭에 지금 살아 있는 입찰 (cancel.OpenBids).

    거기에 있는 상품(옵션)만 건너뛴다. 그 밖의 상품은 (예전에 입찰했다가 체결·만료로 사라진 것도) 기준에 따라 다시 판정해
    조건이 맞으면 입찰을 시도하고, 시도가 안 되면 건너뛰고 다음 상품으로 간다.
    ONE SIZE 입찰이 있는 상품은 상품 페이지를 열지 않고 바로 건너뛴다. 옵션 상품은 입찰 중인 옵션만 건너뛰고 나머지 옵션은 본다.
    `data/bids.json` 은 건너뛰기 기준이 아니라 목록의 입찰을 상품 ID 로 잇는 기록으로만 쓴다.

    사이트가 체결 내역을 안 주는 시간대: 상품이 연달아 TROUBLE_STREAK 개 판단 불가·오류로 끝나면 (옵션 상품은 모든 옵션이)
    더 열지 않고 멈춘 채 2분마다 확인, 다시 주면 로그인 상태를 확인하고 그 상품들부터 다시 본다 (앞서 남긴 판단 불가 결과는
    바꿔 넣는다). 사용자가 중지할 때까지 기다린다.
    page: 로그인 확인용 메인 탭 (없으면 확인만 건너뜀). on_status: GUI 상태 한 줄.
    """
    stop = should_stop or (lambda: False)
    status = on_status or (lambda _t: None)
    results: list[ProductResult] = []
    queue = list(items)
    done = 0
    trouble_streak: list[RankedProduct] = []   # 연달아 판단 불가·오류로 끝난 상품 (사이트가 풀리면 다시 본다)
    while queue:
        if stop():
            log.info("사용자 요청으로 중지 - 남은 %d개는 보지 않음", len(queue))
            break
        item = queue.pop(0)
        done += 1
        ob = open_bids.find(item.product_id, ONE_SIZE) if open_bids is not None and not settings.force else None
        if ob is not None:
            log.info("[%d위] %s - 마이페이지에 이미 입찰 중, 건너뜀", item.rank, item.name)
            results.append(ProductResult(
                rank=item.rank, product_id=item.product_id, name=item.name, url=item.url,
                category=item.category, status="건너뜀", detail=_open_bid_detail(ob), bid_price=ob.price,
            ))
            if on_result:
                on_result(results[-1])
            continue
        if done > 1:
            pacing.pause(pacing.PRODUCT_PAUSE_SEC, stop)   # 상품 사이 간격 (사이트 스로틀 대응)
        status(f"[{item.category}] {item.rank}위 {item.name[:24]} 확인 중 ({done}/{len(items)})")
        product_results = process_product(context, item, settings, open_bids, stop, status)
        for r in product_results:
            results.append(r)
            if on_result:
                on_result(r)
        if product_results and all(is_site_trouble(r) for r in product_results):
            trouble_streak.append(item)
        else:
            trouble_streak = []
        if len(trouble_streak) < TROUBLE_STREAK:
            continue

        log.warning("판단 불가·오류가 %d개 상품 연달아 남 - 사이트가 체결 내역을 안 주는 듯해 멈춤. 2분마다 확인하고 "
                    "다시 주면 이 %d개부터 이어서 봄", len(trouble_streak), len(trouble_streak))
        probe_item = trouble_streak[-1]
        if not wait_until_site_back(lambda: _site_gives_sales(context, probe_item), stop, status):
            break
        if page is not None:
            try:
                auth.ensure_logged_in(page, settings)
            except Exception:  # noqa: BLE001
                log.exception("멈췄다 이어가며 로그인 상태를 확인하지 못함 - 그대로 이어서 봄")
        # 판단 불가로 남긴 결과를 빼고 그 상품들을 맨 앞에 다시 넣는다
        retry_ids = {it.product_id for it in trouble_streak}
        results = [r for r in results if not (r.product_id in retry_ids and is_site_trouble(r))]
        queue = trouble_streak + queue
        done -= len(trouble_streak)
        trouble_streak = []
    return results


def _site_gives_sales(context: BrowserContext, item: RankedProduct) -> bool:
    """사이트가 체결 내역을 다시 주는지 - 막혔던 상품의 페이지를 새 탭에 열고 패널 표가 그려지는지 본다."""
    tab = context.new_page()
    try:
        ok, note = product_mod.sales_available(tab, item.url)
        log.info("확인: %s", note)
        return ok
    finally:
        try:
            tab.close()
        except Exception:  # noqa: BLE001
            pass

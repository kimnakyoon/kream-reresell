"""랭킹 상품을 순서대로 보며 조건에 맞으면 구매 입찰까지 진행.

순서 (2026-09-13 부터 - 시세 API 로 가격을 먼저 거른다, 사용자 결정. 거래량 기준은 그대로):
  1. 상품 하나마다 **상품 상세 API 한 번**(market.fetch_market_paced, 페이지 이동 없음)으로 모든 옵션의 A(빠른배송 가격)·B(즉시 판매가)를 읽어,
     빠른배송 판매자가 없거나 · 즉시 판매가가 없거나 · A 가 상한을 넘거나 · 마진이 기준에 못 미치는 옵션을 상품 페이지를 열지 않고 거른다.
     (예전에는 옵션마다 체결 내역을 다 센 뒤 가격을 봤다 - 09-07 이후 실측에서 체결 내역 조회의 46% 가 어차피 가격에서 떨어질 옵션에 쓰였다.)
     모든 옵션이 걸러지면 상품 페이지는 열지 않는다.
  2. 남은 옵션만 상품 페이지의 체결 내역 패널에서 30일 빠른배송 건수를 센다 (스로틀 대상 요청 - 이 기준은 시세 API 로 대신할 수 없다).
  3. 거래량도 넘는 옵션은 구매 페이지(/buy/{id}?size=옵션값)를 바로 열어 (구매하기 모달을 거치지 않음) 최신 B 를 한 번 더 읽고
     마진을 다시 판정한 뒤 입찰한다 (ONE SIZE 상품과 같은 순서). 결과는 옵션마다 한 줄이다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from . import auth, hangwatch
from . import bid as bid_mod
from . import market as market_mod
from . import product as product_mod
from .api import ApiClient
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

NOT_LOADED_PREFIX = "판단 불가"   # 사이트가 체결 내역(또는 시세)을 안 줘서 판정하지 못한 결과의 사유 머리 (연달아 난 수를 셀 때 쓴다)


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
    """상품 페이지를 열어 거래량 / A / B 를 읽어 r 에 채우고 입찰 조건을 판정한다 ([입찰취소]가 쓴다).

    조건 미달이면 사유 문자열을, 충족이면 None 을 돌려준다.
    stop_early 가 True 면 거래량 미달에서 바로 돌아온다 (A/B 는 읽지 않음).
    price_limit 가 True 면 A 가 상품 금액 상한을 넘는 상품은 B 를 읽지 않고 바로 돌아온다 (입찰 전용 규칙).
    option 을 주면 (옵션 상품) 그 옵션의 거래량·가격으로 판정한다. 조건을 다 봤으면 page 는 구매 페이지(/buy/{id}) 에 있다.
    """
    sales_reason = check_sales(page, url, r, settings, option)
    if sales_reason and stop_early:
        return sales_reason
    return judge_prices(page, r, settings, price_limit, option, sales_reason)


def check_sales(page: Page, url: str, r: ProductResult, settings: Settings, option: str | None = None) -> str | None:
    """상품 페이지를 열어 거래량을 읽어 r 에 채우고 거래량 기준을 판정한다. 미달이면 사유. 끝나면 page 는 상품 페이지에 있다."""
    r.name = product_mod.open_product(page, url) or r.name
    if settings.inspect:
        dump(page, f"{r.product_id}_0_product")

    pacing.before_sales_request()
    stats = product_mod.read_sales_stats(page, settings.lookback_days, settings.min_fast_sales, option)
    r.fast_sales, r.total_sales = stats.fast_in_window, stats.total_in_window
    return _sales_reason(stats, settings)


def _sales_reason(stats: product_mod.SalesStats, settings: Settings) -> str | None:
    if stats.fast_in_window < settings.min_fast_sales:
        return f"{settings.lookback_days}일 빠른배송 {stats.fast_in_window}건 < {settings.min_fast_sales}건"
    return None


def judge_margin(r: ProductResult, settings: Settings, price_limit: bool = True) -> str | None:
    """r.price_a / r.price_b 가 채워진 상태에서 상한·마진을 판정한다. 미달이면 사유, 충족이면 None. [입찰] · [재입찰] · [입찰취소] 공용.

    price_limit 가 True 면 A 가 상품 금액 상한을 넘으면 바로 사유 (입찰 전용 규칙 - 재입찰·취소는 False).
    r.margin_min 에 적용된 구간의 기준 마진율을 남긴다 (보고서).
    """
    if r.price_a is None or r.price_b is None:
        return "A 또는 B 를 읽지 못함"
    if price_limit and settings.rules.over_limit(r.price_a):
        log.info("A %s원 > 상품 금액 상한 %s원 - 바로 건너뜀", f"{r.price_a:,}", f"{settings.rules.max_price_a:,}")
        return f"A {r.price_a:,}원 > 상품 금액 상한 {settings.rules.max_price_a:,}원"
    rate = r.margin_rate or 0.0
    tier = settings.rules.tier_for(r.price_a)
    r.margin_min = tier.margin_rate if tier else None
    log.info("A-B = %s원 (A의 %.1f%%), 기준 %s", f"{r.margin:,}",
             rate * 100, tier.describe() if tier else "없음 (A 가 설정한 금액 구간 밖)")
    if tier is None:
        return f"A {r.price_a:,}원은 설정한 금액 구간에 없음"
    if rate <= r.margin_min:
        return f"마진 {rate*100:.1f}% <= 기준 {tier.margin_pct:g}% ({tier.label})"
    return None


def judge_prices(page: Page, r: ProductResult, settings: Settings, price_limit: bool = True,
                 option: str | None = None, sales_reason: str | None = None) -> str | None:
    """상품 페이지에 있는 상태에서 A (모달) → 구매 페이지 → B 를 읽고 마진을 판정한다. 미달이면 사유 ([입찰취소]가 쓴다).

    sales_reason 은 앞서 본 거래량 미달 사유 - 있으면 A·B 를 읽어 r 에 채운 뒤 그 사유를 돌려준다 (보고서에 A/B 를 남기려고).
    """
    pid = r.product_id
    r.price_a, r.price_b = product_mod.read_price_a_and_go_to_buy(page, pid, option)
    r.size = product_mod.size_from_url(page.url) or (ONE_SIZE if not option else "")
    if settings.inspect:
        dump(page, f"{pid}_1_buy_page")
    reason = judge_margin(r, settings, price_limit)
    if price_limit and settings.rules.over_limit(r.price_a):
        return reason   # 상한 초과는 거래량 사유보다 앞선다 (예전과 같은 순서)
    return sales_reason or reason


def _prefilter(market: market_mod.ProductMarket, item: RankedProduct, settings: Settings,
               open_bids: "OpenBids | None", results: list[ProductResult]
               ) -> list[tuple[market_mod.OptionPrice, ProductResult]]:
    """시세로 옵션을 거른다 - 남는 옵션(체결 내역을 세어 볼 것)과 그 결과 줄을 돌려주고, 걸러진 옵션은 results 에 건너뜀으로 남긴다."""
    options = market.options
    one_size = market.is_one_size
    if settings.options and not one_size:
        wanted = [o for o in options if o.label in settings.options]
        log.info("옵션 %d개 중 지정한 %s 만 봄", len(options), ", ".join(o.label for o in wanted) or "(없음)")
        if not wanted:
            r = _item_result(item, status="건너뜀",
                             detail=f"지정한 옵션 {', '.join(settings.options)} 이 없음 (있는 옵션: {', '.join(o.label for o in options)})")
            _done(results, r, item)
            return []
        options = wanted
    elif not one_size:
        log.info("옵션 %d개 (시세 API): %s", len(options), ", ".join(o.label for o in options))
    candidates: list[tuple[market_mod.OptionPrice, ProductResult]] = []
    for o in options:
        r = _item_result(item, option="" if one_size else o.label)
        r.size, r.price_a, r.price_b = o.key, o.fast, o.sell
        if open_bids is not None and not settings.force:
            ob = open_bids.find(item.product_id, r.option or ONE_SIZE)
            if ob is not None:
                r.status, r.detail, r.bid_price = "건너뜀", _open_bid_detail(ob), ob.price
                _done(results, r, item)
                continue
        if o.fast is None:
            r.status, r.detail = "건너뜀", "시세에 빠른배송 가격이 없음 (지금 빠른배송 판매자 없음)"
        elif o.sell is None:
            r.status, r.detail = "건너뜀", "시세에 즉시 판매가가 없음 (구매 입찰 없음)"
        else:
            log.info("A(빠른배송 가격)%s = %s원, B(즉시 판매가) = %s원 (시세 API)", f" [{o.label}]" if not one_size else "",
                     f"{o.fast:,}", f"{o.sell:,}")
            reason = judge_margin(r, settings, price_limit=True)
            if reason is None or settings.force:
                candidates.append((o, r))
                continue
            r.status, r.detail = "건너뜀", reason
        _done(results, r, item)
    if options and not candidates:
        log.info("[%d위] 가격 기준을 통과한 옵션이 없어 상품 페이지를 열지 않음", item.rank)
    return candidates


def process_product(context: BrowserContext, item: RankedProduct, settings: Settings,
                    open_bids: "OpenBids | None" = None, should_stop: Callable[[], bool] | None = None,
                    on_status: Callable[[str], None] | None = None, api: ApiClient | None = None) -> list[ProductResult]:
    """상품 하나를 처리한다 (머리글의 순서). ONE SIZE 상품은 결과 한 줄, 옵션 상품은 옵션마다 한 줄.

    시세로 다 걸러지면 탭을 열지 않는다. 남는 옵션이 있으면 새 탭에서 체결 내역을 세고 구매 페이지에서 입찰한다.
    기준에 맞으면 입찰을 시도하고, 시도 중 안전장치에 걸리거나 화면이 예상과 다르면 그 상품(옵션)은 건너뛰고 다음으로 간다.
    open_bids 에 상품 ID 를 못 읽은 입찰이 있으면 상품 페이지 제목(= 마이페이지 표기)으로 대조해 이미 입찰 중이면 건너뛴다.
    """
    pid = item.product_id
    results: list[ProductResult] = []
    log.info("[%s %d위] %s (%s)", item.category, item.rank, item.name, item.url)

    # 1. 시세 API - 페이지를 열지 않고 가격에서 떨어지는 옵션을 거른다
    r = _item_result(item)
    try:
        market = market_mod.fetch_market_paced(api, pid, should_stop, on_status)
    except market_mod.MarketUnavailable as e:
        if e.stopped:
            r.status, r.detail = "중단", str(e)
        elif e.gone:
            r.status, r.detail = "건너뜀", f"시세를 읽지 못함: {e}"
        else:
            # 차단 신호 - 연달아 나면 run 이 sitewait 로 멈춘다. 간격은 fetch_market_paced 가 이미 늘렸다
            r.status, r.detail = "건너뜀", f"{NOT_LOADED_PREFIX}: {e}"
        return _done(results, r, item)
    if not market.options:
        r.status, r.detail = "건너뜀", "시세 응답에 옵션이 하나도 없음"
        return _done(results, r, item)
    candidates = _prefilter(market, item, settings, open_bids, results)
    if not candidates:
        return results

    # 2. 거래량 - 상품 페이지의 체결 내역 패널 (남은 옵션만)
    page: Page = context.new_page()
    hangwatch.set_page(page)   # 탭이 아예 멈추면 감시 스레드가 닫는다 (이 상품은 오류로 끝나고 다음 상품은 새 탭) - hangwatch 참고
    try:
        try:
            name = product_mod.open_product(page, item.url)
            for _, cr in candidates:
                cr.name = name or cr.name
            if settings.inspect:
                dump(page, f"{pid}_0_product")
            pacing.before_sales_request(should_stop, on_status=on_status)
            product_mod.open_sales_panel(page)
        except product_mod.SkipProduct as e:
            for _, cr in candidates:
                cr.status, cr.detail = "건너뜀", _skip_detail(e)
                _done(results, cr, item)
            return results

        if market.is_one_size:
            opt, cr = candidates[0]
            try:
                stats = product_mod.count_sales(page, settings.lookback_days, settings.min_fast_sales)
                product_mod.close_sales_panel(page)
            except product_mod.SkipProduct as e:
                product_mod.raise_if_login_lost(page, "체결 내역 확인", e)
                cr.status, cr.detail = "건너뜀", _skip_detail(e)
                return _done(results, cr, item)
            _judge_sales_and_bid(page, cr, stats, opt, item, settings, open_bids)
            return _done(results, cr, item)

        labels = [opt.label for opt, _ in candidates]
        log.info("가격 기준을 통과한 옵션 %d개의 거래량을 셈: %s", len(labels), ", ".join(labels))
        # 옵션 상품: 먼저 패널에서 옵션마다 거래량을 센다 (페이지 이동 없음). 모든 옵션 표에서 정해지지 않은 옵션만 하나씩 고른다
        stats_by_option: dict[str, product_mod.SalesStats | str] = {}

        def before_page() -> None:   # 모든 옵션 표를 한 페이지 더 넘기기 전 (sales 요청 1건) - 옵션을 고를 때와 같은 간격·예산
            pacing.pause(pacing.PAGE_PAUSE_SEC, should_stop)
            pacing.before_sales_request(should_stop, on_status=on_status)

        try:
            pre, _pages = product_mod.count_sales_by_option(page, settings.lookback_days, settings.min_fast_sales, labels,
                                                            before_page=before_page)
            stats_by_option.update(pre)
        except product_mod.SkipProduct as e:
            log.info("모든 옵션 표를 읽지 못함 (%s) - 옵션을 하나씩 봄", e)
        for label in labels:
            if label in stats_by_option:
                continue
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
        for opt, cr in candidates:
            st = stats_by_option[opt.label]
            if isinstance(st, str):
                cr.status, cr.detail = "건너뜀", st
            else:
                _judge_sales_and_bid(page, cr, st, opt, item, settings, open_bids)
            _done(results, cr, item)
        return results
    except product_mod.LoginNeeded as e:
        if any(x.status == "입찰완료" for x in results):
            # 이 상품에 이미 입찰을 넣은 뒤 풀렸다 - 다시 보면 같은 옵션에 또 입찰할 수 있어 (open_bids 는 실행 시작 때 목록) 여기서 끝냄.
            # 다음 상품에서 다시 로그인한다
            r = _item_result(item)
            r.status, r.detail = "오류", f"앞 옵션을 입찰한 뒤 {e} - 남은 옵션은 보지 않음"
            return _done(results, r, item)
        raise   # _process_with_relogin 이 다시 로그인하고 이 상품을 다시 본다 (결과를 남기지 않음)
    except Exception as e:  # noqa: BLE001
        dump(page, f"{pid}_error")
        log.exception("상품 %s 처리 중 오류", pid)
        r = _item_result(item)
        r.status, r.detail = "오류", f"{type(e).__name__}: {e}"
        return _done(results, r, item)
    finally:
        hangwatch.clear_page(page)
        hangwatch.take_trip()
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


def _judge_sales_and_bid(page: Page, r: ProductResult, stats: product_mod.SalesStats, opt: market_mod.OptionPrice,
                         item: RankedProduct, settings: Settings, open_bids: "OpenBids | None") -> None:
    """거래량(stats)을 안 상태에서 상품명으로 이미 입찰 중인지 → 거래량 → 구매 페이지의 최신 B 로 마진 → 입찰 순으로 r.status/detail 을 채운다.

    ONE SIZE 상품(r.option 비움)과 옵션 상품(r.option = 옵션 표기) 이 같은 순서를 쓴다. 가격은 시세 API 로 이미 한 번 걸렀고,
    여기서는 입찰 직전에 구매 페이지에서 읽은 B 로 한 번 더 판정한다 (A 는 시세 API 값).
    """
    r.fast_sales, r.total_sales = stats.fast_in_window, stats.total_in_window
    try:
        if open_bids is not None and not settings.force:
            ob = open_bids.by_name(r.name, r.option or ONE_SIZE)
            if ob is not None:
                r.status, r.detail = "건너뜀", _open_bid_detail(ob) + " - 상품명으로 확인"
                r.bid_price = ob.price
                return
        sales_reason = _sales_reason(stats, settings)
        if sales_reason and not settings.force:
            r.status, r.detail = "건너뜀", sales_reason
            return
        _open_buy_page(page, r, opt.label if r.option else ONE_SIZE, settings)
        reason = judge_margin(r, settings, price_limit=True)
        if reason and not settings.force:
            r.status, r.detail = "건너뜀", f"{reason} (구매 페이지의 최신 B 로 다시 판정)"
            return
        _place_bid(page, r, settings)
    except product_mod.SkipProduct as e:
        product_mod.raise_if_login_lost(page, "구매 페이지 확인", e)
        r.status, r.detail = "건너뜀", _skip_detail(e)
    except bid_mod.StoppedBeforeSubmit as e:
        r.status, r.detail = "중단", str(e)
    except bid_mod.BidAborted as e:
        # 입찰 화면이 예상과 달라 넣지 못한 것 - 이 상품(옵션)만 건너뛰고 다음으로
        log.warning("[%d위%s] 입찰 못 함, 건너뜀: %s", item.rank, f" {r.option}" if r.option else "", e)
        r.status, r.detail = "건너뜀", f"입찰 못 함: {e}"


def _open_buy_page(page: Page, r: ProductResult, label: str, settings: Settings) -> None:
    """구매 페이지(/buy/{id}?size=옵션값)를 바로 열어 상품 정보를 다 불러올 때까지 기다리고 최신 B 를 r.price_b 에 넣는다.

    구매하기 모달을 거치지 않는다 (2026-09-13 - 옵션 값은 시세 API 의 product_option.key 로 이미 안다). 이동이 15초 안에 안 끝나거나
    끊기면(net::ERR_ABORTED) 1.5초 뒤 한 번 더 열고, 그래도 안 되면 SkipProduct. 옵션 상품은 상단의 옵션 표기가 고른 것과 같아야 한다.
    """
    url = product_mod.buy_page_url(r.product_id, r.size or ONE_SIZE)
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded")
            break
        except PlaywrightError as e:   # 시간 제한 또는 이동 중 끊김
            timed_out = isinstance(e, PlaywrightTimeout)
            stall = product_mod.page_stall(page) if timed_out else None
            if stall:
                raise product_mod.PageStalled(f"구매 페이지 이동이 안 끝남 - {stall}") from e
            if attempt:
                raise product_mod.SkipProduct(f"구매 페이지를 열지 못함 (두 번 시도): {product_mod.timeout_why(e)}") from e
            log.info("구매 페이지 이동이 안 끝남 (%s) - 1.5초 뒤 다시 엶", product_mod.timeout_why(e))
            page.wait_for_timeout(1500)
    loaded = product_mod.wait_buy_page_loaded(page, r.product_id, label)
    if r.option and not loaded.option_shown:
        # 구매 페이지 상단의 옵션 표기가 고른 것과 같아야 한다 (다른 사이즈에 입찰하지 않도록)
        raise product_mod.SkipProduct(f"구매 페이지의 옵션 표기가 '{r.option}' 이 아님 (주소 {page.url})")
    before = r.price_b
    r.price_b = loaded.price_b
    log.info("B(즉시 판매가) = %s원 (구매 페이지%s)", f"{r.price_b:,}",
             f", 시세 API 는 {before:,}원" if before and before != r.price_b else "")
    if settings.inspect:
        dump(page, f"{r.product_id}_1_buy_page")


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
        on_status: Callable[[str], None] | None = None,
        api: ApiClient | None = None) -> list[ProductResult]:
    """open_bids: 마이페이지 구매 입찰 탭에 지금 살아 있는 입찰 (cancel.OpenBids).

    거기에 있는 상품(옵션)만 건너뛴다. 그 밖의 상품은 (예전에 입찰했다가 체결·만료로 사라진 것도) 기준에 따라 다시 판정해
    조건이 맞으면 입찰을 시도하고, 시도가 안 되면 건너뛰고 다음 상품으로 간다.
    ONE SIZE 입찰이 있는 상품은 시세도 읽지 않고 바로 건너뛴다. 옵션 상품은 입찰 중인 옵션만 건너뛰고 나머지 옵션은 본다.
    `data/bids.json` 은 건너뛰기 기준이 아니라 목록의 입찰을 상품 ID 로 잇는 기록으로만 쓴다.

    사이트가 체결 내역(또는 시세)을 안 주는 시간대: 상품이 연달아 TROUBLE_STREAK 개 판단 불가·오류로 끝나면 (옵션 상품은 모든 옵션이)
    더 열지 않고 멈춘 채 5분마다 확인, 다시 주면 그 상품들부터 다시 본다 (앞서 남긴 판단 불가 결과는 바꿔 넣는다. 확인이 패널을
    열어 보는 것이라 로그인 확인을 겸한다). 사용자가 중지할 때까지 기다린다.
    로그인이 풀리면 (product.LoginNeeded) 다시 로그인하고 그 상품을 한 번 더 본다 (_process_with_relogin). 또 풀리면 오류.
    page: 다시 로그인할 때 쓰는 메인 탭 (없거나 닫혔으면 새 탭). api: 시세 API 클라이언트 (없으면 page 로 만든다). on_status: GUI 상태 한 줄.
    """
    stop = should_stop or (lambda: False)
    status = on_status or (lambda _t: None)
    if api is None:
        api = ApiClient(page if page is not None and not page.is_closed() else context.new_page())
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
            results.append(_item_result(item, status="건너뜀", detail=_open_bid_detail(ob), bid_price=ob.price))
            if on_result:
                on_result(results[-1])
            continue
        if done > 1:
            pacing.pause(pacing.PRODUCT_PAUSE_SEC, stop)   # 상품 사이 간격 (사이트 스로틀 대응)
        if not pacing.before_product(stop, status):        # 접속 예산 (pacing 대응 5)
            break
        status(f"[{item.category}] {item.rank}위 {item.name[:24]} 확인 중 ({done}/{len(items)}, 시세 {pacing.API_PACER.describe()})")
        product_results = _process_with_relogin(context, page, item, settings, open_bids, stop, status, api)
        for r in product_results:
            results.append(r)
            if on_result:
                on_result(r)
        if product_results and product_results[-1].status == "중단":
            break
        if product_results and all(is_site_trouble(r) for r in product_results):
            trouble_streak.append(item)
        else:
            trouble_streak = []
        if len(trouble_streak) < TROUBLE_STREAK:
            continue

        log.warning("판단 불가·오류가 %d개 상품 연달아 남 - 사이트가 체결 내역(시세)을 안 주는 듯해 멈춤. 5분마다 확인하고 "
                    "다시 주면 이 %d개부터 이어서 봄", len(trouble_streak), len(trouble_streak))
        probe_item = trouble_streak[-1]
        # (_site_gives_sales 가 패널을 열었으면 로그인도 돼 있다 - 패널은 로그인이 필요한 동작, 풀렸으면 거기서 다시 로그인함)
        if not wait_until_site_back(lambda: _site_gives_sales(context, probe_item, settings, api), stop, status):
            break
        pacing.API_PACER.reset_streak()
        # 판단 불가로 남긴 결과를 빼고 그 상품들을 맨 앞에 다시 넣는다
        retry_ids = {it.product_id for it in trouble_streak}
        results = [r for r in results if not (r.product_id in retry_ids and is_site_trouble(r))]
        queue = trouble_streak + queue
        done -= len(trouble_streak)
        trouble_streak = []
    return results


def _item_result(item: RankedProduct, **fields) -> ProductResult:
    return ProductResult(rank=item.rank, product_id=item.product_id, name=item.name, url=item.url,
                         category=item.category, **fields)


def _process_with_relogin(context: BrowserContext, page: Page | None, item: RankedProduct, settings: Settings,
                          open_bids: "OpenBids | None", stop: Callable[[], bool],
                          status: Callable[[str], None], api: ApiClient) -> list[ProductResult]:
    """process_product 를 부르되, 로그인이 풀린 것이 보이면 다시 로그인하고 한 번 더 본다 (rebid._rebid_with_relogin 과 같은 꼴)."""
    try:
        return process_product(context, item, settings, open_bids, stop, status, api)
    except product_mod.LoginNeeded as e:
        log.warning("[%d위] %s - 다시 로그인하고 한 번 더 봄", item.rank, e)
        status("로그인이 풀려 다시 로그인하는 중")
        tab = page if page is not None and not page.is_closed() else context.new_page()
        try:
            auth.ensure_logged_in(tab, settings)
        except Exception as e2:  # noqa: BLE001
            log.exception("다시 로그인하지 못함")
            return _done([], _item_result(item, status="오류", detail=f"로그인이 풀렸는데 다시 로그인하지 못함: {e2}"), item)
        finally:
            if tab is not page:
                tab.close()
        api.headers = {}   # 새 세션의 헤더를 다시 잡는다
    try:
        return process_product(context, item, settings, open_bids, stop, status, api)
    except product_mod.LoginNeeded as e2:
        return _done([], _item_result(item, status="오류", detail=f"다시 로그인했는데도 {e2}"), item)


def _site_gives_sales(context: BrowserContext, item: RankedProduct, settings: Settings, api: ApiClient) -> bool:
    """사이트가 다시 주는지 - 시세 API 를 한 번 부르고, 막혔던 상품의 페이지를 새 탭에 열어 패널 표가 그려지는지 본다.

    그 사이 로그인이 풀렸으면 (패널 대신 로그인 화면) 기다려도 소용없으니 여기서 다시 로그인하고 한 번 더 본다."""
    try:
        market_mod.fetch_market(api, item.product_id)   # 틱 없이 한 번 (5분마다 한 번이라 예산에 뜻이 없다)
    except market_mod.MarketUnavailable as e:
        if e.block_signal:
            log.info("확인: 시세 API 가 아직 응답하지 않음 (%s)", e)
            return False
    tab = context.new_page()
    try:
        try:
            ok, note = product_mod.sales_available(tab, item.url)
        except product_mod.LoginNeeded as e:
            log.warning("확인 중 %s - 다시 로그인하고 한 번 더 확인", e)
            auth.ensure_logged_in(tab, settings)
            api.headers = {}
            ok, note = product_mod.sales_available(tab, item.url)   # 또 풀리면 그대로 올라감 (wait_until_site_back 이 '아직 안 줌' 으로 봄)
        log.info("확인: %s", note)
        return ok
    finally:
        try:
            tab.close()
        except Exception:  # noqa: BLE001
            pass

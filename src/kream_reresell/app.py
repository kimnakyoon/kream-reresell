"""한 번의 실행(브라우저 띄우기 → 로그인 → 랭킹 → 상품 처리 → 엑셀 보고서) 을 묶은 진입점.

명령행(scripts/run.py) 과 GUI(scripts/gui.pyw) 가 같이 쓴다.
상품군을 여러 개 주면 준 순서대로 하나씩 랭킹을 열어 처리하고, 결과는 엑셀 보고서 하나에 모은다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import auth, cancel, pipeline, ranking, report
from .browser import real_chrome_context
from .config import Settings
from .ranking import DEFAULT_CATEGORY
from .report import ProductResult

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    results: list[ProductResult]
    report_path: Path
    mode: str


def describe_mode(settings: Settings, kind: str = "입찰") -> str:
    if settings.dry_run:
        return "DRY-RUN (판단만)"
    if settings.stop_before_submit:
        return f"점검 (마지막 {kind} 버튼 직전 멈춤)"
    return f"실제 {kind}"


def describe_settings(settings: Settings, categories: list[str]) -> str:
    return (f"조건: 최근 {settings.lookback_days}일 빠른배송 {settings.min_fast_sales}건 이상, "
            f"{settings.rules.describe()}, 입찰기한 {settings.bid_days}일, "
            f"상품군 {' → '.join(categories)} (상품군마다 {settings.max_products}개)"
            + (", 조건 무시(--force)" if settings.force else ""))


def normalize_categories(categories: str | list[str] | None) -> list[str]:
    """문자열 하나 / 목록 / None 을 순서를 지키고 중복을 뺀 목록으로."""
    if not categories:
        return [DEFAULT_CATEGORY]
    if isinstance(categories, str):
        categories = [categories]
    out: list[str] = []
    for c in categories:
        c = c.strip()
        if c and c not in out:
            out.append(c)
    return out or [DEFAULT_CATEGORY]


def run_job(settings: Settings, categories: str | list[str] | None = None,
            product_ids: list[int] | None = None,
            should_stop: Callable[[], bool] | None = None,
            on_result: Callable[[ProductResult], None] | None = None) -> JobResult:
    settings.validate()
    categories = normalize_categories(categories)
    mode = describe_mode(settings)
    settings_line = describe_settings(settings, categories)
    log.info("%s | %s", mode, settings_line)

    results: list[ProductResult] = []
    with sync_playwright() as pw, real_chrome_context(pw, block_images=settings.block_images,
                                                        show_chrome=settings.show_chrome) as context:
        page = context.pages[0] if context.pages else context.new_page()
        auth.ensure_logged_in(page, settings)
        # 마이페이지에 이미 입찰 중인 상품은 건너뛴다 (bids.json 과 별개로 실제 목록을 본다)
        log.info("마이페이지 구매 입찰 목록 확인 중...")
        open_bids = cancel.open_bid_products(context, page)

        if product_ids:
            items = [ranking.RankedProduct(rank=i + 1, product_id=pid, name=str(pid), price=None,
                                           url=f"https://kream.co.kr/products/{pid}", category="지정")
                     for i, pid in enumerate(product_ids)]
            results = pipeline.run(context, items, settings, should_stop=should_stop, on_result=on_result, open_bids=open_bids)
        else:
            for n, category in enumerate(categories, start=1):
                if should_stop and should_stop():
                    log.info("사용자 요청으로 중지 - 남은 상품군 %s 은 보지 않음", ", ".join(categories[n - 1:]))
                    break
                log.info("===== 상품군 %d/%d: %s =====", n, len(categories), category)
                try:
                    ranking.open_category(page, category)
                    items = ranking.collect_products(page, settings.max_products, category)
                except Exception as e:  # noqa: BLE001
                    log.exception("상품군 '%s' 랭킹을 열지 못해 건너뜀", category)
                    results.append(ProductResult(
                        rank=0, product_id=0, name=f"[{category}] 랭킹 열기 실패",
                        url=ranking.RANKING_URL, category=category,
                        status="오류", detail=f"{type(e).__name__}: {e}"))
                    if on_result:
                        on_result(results[-1])
                    continue
                for it in items:
                    log.info("  %2d위 %s %s %s", it.rank, it.name, f"{it.price:,}원" if it.price else "", it.url)
                results.extend(pipeline.run(context, items, settings, should_stop=should_stop, on_result=on_result, open_bids=open_bids))

    log.info("==== 결과 ====")
    for r in results:
        log.info("[%s] %2d위 %-40s %s - %s", r.category, r.rank, r.name[:40], r.status, r.detail)
    path = report.write_report(results, settings_line, mode)
    log.info("엑셀 보고서: %s", path)
    return JobResult(results=results, report_path=path, mode=mode)


def describe_cancel_settings(settings: Settings) -> str:
    return (f"입찰취소 기준: 최근 {settings.lookback_days}일 빠른배송 {settings.min_fast_sales}건 미만이거나 "
            f"마진 기준에 못 미치면 입찰을 지움 ({settings.rules.describe()}) "
            f"(마이페이지 > 구매 내역 > 구매 입찰 순서대로)")


def run_cancel_job(settings: Settings,
                   should_stop: Callable[[], bool] | None = None,
                   on_result: Callable[[ProductResult], None] | None = None) -> JobResult:
    """마이페이지 구매 입찰 목록을 순서대로 다시 판정해 기준 미달 입찰을 지운다. 결과는 엑셀 보고서로."""
    settings.validate()
    mode = describe_mode(settings, "입찰취소")
    settings_line = describe_cancel_settings(settings)
    log.info("%s | %s", mode, settings_line)

    with sync_playwright() as pw, real_chrome_context(pw, block_images=settings.block_images,
                                                        show_chrome=settings.show_chrome) as context:
        page = context.pages[0] if context.pages else context.new_page()
        auth.ensure_logged_in(page, settings)
        results = cancel.run(context, page, settings, should_stop=should_stop, on_result=on_result)

    log.info("==== 결과 ====")
    for r in results:
        log.info("[입찰 %d번째] %-40s %s - %s", r.rank, r.name[:40], r.status, r.detail)
    path = report.write_report(results, settings_line, mode, kind="입찰취소")
    log.info("엑셀 보고서: %s", path)
    return JobResult(results=results, report_path=path, mode=mode)

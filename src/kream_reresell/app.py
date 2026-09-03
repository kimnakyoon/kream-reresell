"""한 번의 실행(브라우저 띄우기 → 로그인 → 랭킹 → 상품 처리 → 엑셀 보고서) 을 묶은 진입점.

명령행(scripts/run.py) 과 GUI(scripts/gui.pyw) 가 같이 쓴다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import auth, pipeline, ranking, report
from .browser import real_chrome_context
from .config import Settings
from .report import ProductResult

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    results: list[ProductResult]
    report_path: Path
    mode: str


def describe_mode(settings: Settings) -> str:
    if settings.dry_run:
        return "DRY-RUN (판단만)"
    if settings.stop_before_submit:
        return "점검 (마지막 입찰하기 직전 멈춤)"
    return "실제 입찰"


def describe_settings(settings: Settings, category: str) -> str:
    return (f"조건: 최근 {settings.lookback_days}일 빠른배송 {settings.min_fast_sales}건 이상, "
            f"마진 (A−B) > A×{settings.min_margin_rate*100:.0f}%, 입찰기한 {settings.bid_days}일, 상품군 {category}"
            + (", 조건 무시(--force)" if settings.force else ""))


def run_job(settings: Settings, category: str, product_ids: list[int] | None = None,
            should_stop: Callable[[], bool] | None = None,
            on_result: Callable[[ProductResult], None] | None = None) -> JobResult:
    settings.validate()
    mode = describe_mode(settings)
    settings_line = describe_settings(settings, category)
    log.info("%s | %s", mode, settings_line)

    with sync_playwright() as pw, real_chrome_context(pw, block_images=settings.block_images) as context:
        page = context.pages[0] if context.pages else context.new_page()
        auth.ensure_logged_in(page, settings)

        if product_ids:
            items = [ranking.RankedProduct(rank=i + 1, product_id=pid, name=str(pid), price=None,
                                           url=f"https://kream.co.kr/products/{pid}")
                     for i, pid in enumerate(product_ids)]
        else:
            ranking.open_category(page, category)
            items = ranking.collect_products(page, settings.max_products)
            for it in items:
                log.info("  %2d위 %s %s %s", it.rank, it.name, f"{it.price:,}원" if it.price else "", it.url)

        results = pipeline.run(context, items, settings, should_stop=should_stop, on_result=on_result)

    log.info("==== 결과 ====")
    for r in results:
        log.info("%2d위 %-40s %s - %s", r.rank, r.name[:40], r.status, r.detail)
    path = report.write_report(results, settings_line, mode)
    log.info("엑셀 보고서: %s", path)
    return JobResult(results=results, report_path=path, mode=mode)

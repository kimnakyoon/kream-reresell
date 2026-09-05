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

from datetime import datetime

from . import auth, cancel, history, history_report, pacing, pipeline, ranking, rebid, report
from .browser import real_chrome_context
from .config import Settings
from .history import HistoryResult
from .ranking import DEFAULT_CATEGORY
from .report import REPORT_DIR, ProductResult

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    results: list[ProductResult]
    report_path: Path
    mode: str


@dataclass
class HistoryJobResult:
    result: HistoryResult
    report_path: Path


def run_history_job(settings: Settings, year: int, month: int,
                    should_stop: Callable[[], bool] | None = None) -> HistoryJobResult:
    """[내역]: 보관 판매 거래일시가 year-month 인 판매를 구매 내역과 짝지어 바탕화면에 엑셀로 저장한다."""
    log.info("판매 내역 정리: %d년 %d월 (보관 판매 거래일시 기준)", year, month)
    with sync_playwright() as pw, real_chrome_context(pw, block_images=settings.block_images, trim_api=settings.trim_api,
                                                        show_chrome=settings.show_chrome) as context:
        page = context.pages[0] if context.pages else context.new_page()
        auth.ensure_logged_in(page, settings)
        result = history.collect(page, year, month, should_stop=should_stop)
    path = history_report.write_history(result)
    log.info("판매 %d건, 매입 못 찾음 %d건 → %s", len(result.sales), len(result.unmatched), path)
    return HistoryJobResult(result=result, report_path=path)


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
            on_result: Callable[[ProductResult], None] | None = None,
            on_status: Callable[[str], None] | None = None) -> JobResult:
    """[입찰]. on_status: 상태 한 줄 (GUI 상태 표시용 - 사이트가 내역을 안 줘 멈춰 있을 때 그 사정을 보인다)."""
    settings.validate()
    categories = normalize_categories(categories)
    mode = describe_mode(settings)
    settings_line = describe_settings(settings, categories)
    log.info("%s | %s", mode, settings_line)

    results: list[ProductResult] = []
    with sync_playwright() as pw, real_chrome_context(pw, block_images=settings.block_images, trim_api=settings.trim_api,
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
            results = pipeline.run(context, items, settings, should_stop=should_stop, on_result=on_result,
                                   open_bids=open_bids, page=page, on_status=on_status)
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
                results.extend(pipeline.run(context, items, settings, should_stop=should_stop, on_result=on_result,
                                            open_bids=open_bids, page=page, on_status=on_status))

    log.info("==== 결과 ====")
    for r in results:
        log.info("[%s] %2d위 %-40s %s%s - %s", r.category, r.rank, r.name[:40], f"[{r.option}] " if r.option else "",
                 r.status, r.detail)
    path = report.write_report(results, settings_line, mode)
    log.info("엑셀 보고서: %s", path)
    log.info("사이트 스로틀 대상 API 요청 %d건 보냄 (10분 예산 %d건)", pacing.BUDGET.total, pacing.BUDGET.limit)
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

    with sync_playwright() as pw, real_chrome_context(pw, block_images=settings.block_images, trim_api=settings.trim_api,
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


def describe_rebid_settings(settings: Settings) -> str:
    return (f"재입찰 기준: 마이페이지 > 구매 내역 > 구매 입찰 순서대로, 즉시 판매가(B) 가 내 희망가보다 높은(밀린) 입찰만 "
            f"상품 페이지에서 처음 입찰 때와 같은 기준으로 다시 판정 (최근 {settings.lookback_days}일 빠른배송 "
            f"{settings.min_fast_sales}건 이상, {settings.rules.describe()}) 하고, 충족하면 [입찰 변경하기] 로 희망가를 "
            f"최신 B 로 올림 (마감 {settings.bid_days}일, 창고보관). 기준 미달이면 입찰을 지움 (상한만 넘는 것은 그대로 둠). "
            f"사이클 간격 {settings.rebid_interval_min:g}분")


def _write_rebid_report(results: list[ProductResult], settings_line: str, mode: str, path: Path) -> Path:
    """사이클마다 같은 파일에 덮어쓴다. 사용자가 엑셀로 열어 둔 상태면 시각을 붙인 다른 이름으로 저장."""
    try:
        return report.write_report(results, settings_line, mode, path=path, kind="재입찰")
    except PermissionError:
        alt = path.with_name(f"{path.stem} ({datetime.now():%H%M%S}){path.suffix}")
        log.warning("보고서가 열려 있어 다른 이름으로 저장: %s", alt)
        return report.write_report(results, settings_line, mode, path=alt, kind="재입찰")


def run_rebid_job(settings: Settings,
                  should_stop: Callable[[], bool] | None = None,
                  on_result: Callable[[ProductResult], None] | None = None,
                  on_status: Callable[[str], None] | None = None,
                  max_cycles: int | None = None) -> JobResult:
    """[재입찰]: 구매 입찰 목록을 반복해서 돌며 밀린 입찰의 희망가를 올린다. 중지할 때까지 (또는 max_cycles 만큼) 돈다.

    보고서는 사이클이 끝날 때마다 같은 파일에 덮어써서, 중간에 프로그램이 죽어도 그때까지의 결과가 남는다.
    """
    settings.validate()
    mode = describe_mode(settings, "재입찰")
    settings_line = describe_rebid_settings(settings)
    log.info("%s | %s", mode, settings_line)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"KREAM 재입찰결과 {datetime.now():%Y-%m-%d %H%M}.xlsx"
    results: list[ProductResult] = []

    def collect(r: ProductResult) -> None:
        results.append(r)
        if on_result:
            on_result(r)

    def cycle_done(cycle: int, _cycle_results: list[ProductResult]) -> None:
        nonlocal path
        path = _write_rebid_report(results, settings_line, mode, path)

    try:
        with sync_playwright() as pw, real_chrome_context(pw, block_images=settings.block_images, trim_api=settings.trim_api,
                                                            show_chrome=settings.show_chrome) as context:
            page = context.pages[0] if context.pages else context.new_page()
            auth.ensure_logged_in(page, settings)
            rebid.run(context, page, settings, should_stop=should_stop, on_result=collect,
                      on_status=on_status, on_cycle=cycle_done, max_cycles=max_cycles)
    except KeyboardInterrupt:
        log.info("Ctrl+C 로 중지")
    finally:
        try:
            path = _write_rebid_report(results, settings_line, mode, path)
            log.info("엑셀 보고서: %s", path)
        except Exception:  # noqa: BLE001
            log.exception("보고서 저장 실패")

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("==== 재입찰 결과: %s ====", ", ".join(f"{k} {v}건" for k, v in sorted(counts.items())) or "처리한 입찰 없음")
    return JobResult(results=results, report_path=path, mode=mode)

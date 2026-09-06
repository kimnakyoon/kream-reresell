"""화면 점검용 스냅샷 (접근성 트리 + 스크린샷) 을 dumps/ 에 남긴다."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime

from playwright.sync_api import Page

from .config import DUMP_DIR

log = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT_MS = 5000   # 스크린샷·접근성 트리 각각의 제한 (정상이면 1~2초)


def dump(page: Page, label: str) -> None:
    DUMP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    base = DUMP_DIR / f"{stamp}_{label}"
    # 화면을 그리지 않는 탭에서는 둘 다 타임아웃만 채운다 (2026-09-06 실측 15초 × 2) - 짧게 잡고, 못 남기면 그렇다고 적는다
    saved: list[str] = []
    with contextlib.suppress(Exception):
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True, timeout=SNAPSHOT_TIMEOUT_MS)
        saved.append("png")
    with contextlib.suppress(Exception):
        tree = page.locator("body").aria_snapshot(timeout=SNAPSHOT_TIMEOUT_MS)
        base.with_suffix(".txt").write_text(f"URL: {page.url}\n\n{tree}", encoding="utf-8")
        saved.append("txt")
    if saved:
        log.info("스냅샷 저장: %s (%s)", base, ", ".join(saved))
    else:
        log.info("스냅샷을 남기지 못함 (화면이 응답하지 않음): %s", base)

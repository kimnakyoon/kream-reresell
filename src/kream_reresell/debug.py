"""화면 점검용 스냅샷 (접근성 트리 + 스크린샷) 을 dumps/ 에 남긴다."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime

from playwright.sync_api import Page

from .config import DUMP_DIR

log = logging.getLogger(__name__)


def dump(page: Page, label: str) -> None:
    DUMP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    base = DUMP_DIR / f"{stamp}_{label}"
    with contextlib.suppress(Exception):
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    with contextlib.suppress(Exception):
        tree = page.locator("body").aria_snapshot()
        base.with_suffix(".txt").write_text(f"URL: {page.url}\n\n{tree}", encoding="utf-8")
    log.info("스냅샷 저장: %s", base)

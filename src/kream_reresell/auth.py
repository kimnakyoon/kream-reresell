"""KREAM 로그인 상태 확인 / 이메일 자동 로그인.

로그인 첫 화면(/login)은 네이버·Apple·휴대폰 버튼과 '이메일 로그인' 링크만 있고,
이메일 폼은 /login/email 에 있다 (input[type=email], input[type=password], '로그인' 버튼은
둘 다 채워지면 활성화). 2026-09-03 실측 기준 캡차는 없다.
"""

from __future__ import annotations

import logging
import time

from playwright.sync_api import Page

from . import browser
from .config import Settings

log = logging.getLogger(__name__)

HOME = "https://kream.co.kr/"
LOGIN_URL = "https://kream.co.kr/login"
EMAIL_LOGIN_URL = "https://kream.co.kr/login/email?returnUrl=/"
MANUAL_LOGIN_WAIT_SEC = 300


class LoginFailed(Exception):
    pass


def is_logged_in(page: Page) -> bool:
    """상단 유틸 메뉴에 '로그아웃' 이 있으면 로그인 상태."""
    try:
        return page.get_by_role("link", name="로그아웃").count() > 0
    except Exception:  # noqa: BLE001
        return False


def _check_home(page: Page) -> bool:
    page.goto(HOME, wait_until="domcontentloaded")
    # 상단 유틸 메뉴(로그인 또는 로그아웃 링크)가 그려질 때까지만 기다린다
    try:
        page.locator("a:has-text('로그아웃'), a:has-text('로그인')").first.wait_for(state="attached", timeout=10_000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(300)
    return is_logged_in(page)


def email_login(page: Page, email: str, password: str) -> bool:
    """이메일 로그인 폼을 채워 로그인. 성공하면 True."""
    page.goto(EMAIL_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    email_box = page.locator("input[type='email']").first
    pw_box = page.locator("input[type='password']").first
    email_box.wait_for(state="visible", timeout=10_000)
    email_box.click()
    email_box.fill(email)
    pw_box.click()
    pw_box.fill(password)
    page.wait_for_timeout(500)
    button = page.get_by_role("button", name="로그인", exact=True).first
    if button.is_disabled():
        log.warning("로그인 버튼이 활성화되지 않음 - 입력값 형식 확인 필요")
        return False
    button.click()
    # 성공하면 returnUrl(홈)로 이동한다. 실패하면 /login/email 에 남고 오류 문구가 뜬다.
    for _ in range(20):
        page.wait_for_timeout(500)
        if "/login" not in page.url:
            break
    if "/login" in page.url:
        body = page.locator("body").inner_text()
        for line in body.splitlines():
            if any(k in line for k in ("일치하지", "올바르", "실패", "확인해", "잠김", "제한")):
                log.warning("로그인 실패 문구: %s", line.strip())
        return False
    return _check_home(page)


def ensure_logged_in(page: Page, settings: Settings) -> None:
    if _check_home(page):
        log.info("로그인 상태 확인됨")
        return

    log.info("로그인이 필요합니다")
    if settings.kream_id and settings.kream_pw:
        log.info("이메일 자동 로그인 시도: %s", settings.kream_id)
        if email_login(page, settings.kream_id, settings.kream_pw):
            log.info("자동 로그인 성공")
            return
        log.warning("자동 로그인이 되지 않았습니다 - 크롬 창에서 직접 로그인해 주세요")
    else:
        log.warning("KREAM_ID/KREAM_PW 가 없습니다 - 크롬 창에서 직접 로그인해 주세요")

    # 사람이 로그인해야 하니 그동안만 크롬 창을 화면 안으로 불러온다
    with browser.window_shown():
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + MANUAL_LOGIN_WAIT_SEC
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            if "/login" not in page.url and _check_home(page):
                log.info("로그인 확인됨")
                return
    raise LoginFailed(f"{MANUAL_LOGIN_WAIT_SEC}초 안에 로그인이 되지 않았습니다.")

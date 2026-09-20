"""KREAM 로그인 상태 확인 / 이메일 자동 로그인.

로그인 첫 화면(/login)은 네이버·Apple·휴대폰 버튼과 '이메일 로그인' 링크만 있고,
이메일 폼은 /login/email 에 있다 (input[type=email], input[type=password], '로그인' 버튼은
둘 다 채워지면 활성화). 2026-09-03 실측 기준 캡차는 없다.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

from playwright.sync_api import Page, sync_playwright

from . import browser, hangwatch
from .config import Settings
from .product import PageStalled, goto_with_retry

log = logging.getLogger(__name__)

HOME = "https://kream.co.kr/"
LOGIN_URL = "https://kream.co.kr/login"
EMAIL_LOGIN_URL = "https://kream.co.kr/login/email?returnUrl=/"
MANUAL_LOGIN_WAIT_SEC = 300
_login_lock = threading.Lock()   # 동시에 도는 작업들이 한꺼번에 로그인하지 않게 - 잠금을 잡은 뒤 다시 확인한다


class LoginFailed(Exception):
    pass


def is_logged_in(page: Page) -> bool:
    """상단 유틸 메뉴에 '로그아웃' 이 있으면 로그인 상태."""
    try:
        return page.get_by_role("link", name="로그아웃").count() > 0
    except Exception:  # noqa: BLE001
        return False


def _check_home(page: Page) -> bool:
    # 홈 이동도 가끔 15초를 넘긴다 - 그대로 올리면 작업이 시작도 못 하고 오류 창으로 끝난다 (2026-09-20 09:38 재입찰 실측)
    goto_with_retry(page, HOME, "홈(로그인 확인)")
    # 상단 유틸 메뉴(로그인 또는 로그아웃 링크)가 그려질 때까지만 기다린다
    try:
        page.locator("a:has-text('로그아웃'), a:has-text('로그인')").first.wait_for(state="attached", timeout=10_000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(300)
    return is_logged_in(page)


def email_login(page: Page, email: str, password: str) -> bool:
    """이메일 로그인 폼을 채워 로그인. 성공하면 True."""
    goto_with_retry(page, EMAIL_LOGIN_URL, "이메일 로그인 페이지")
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
    """로그인 상태가 아니면 로그인한다. 작업들이 크롬(세션)을 나눠 쓰므로 확인·로그인은 한 번에 한 작업만 (다른 작업이 먼저 로그인했으면 확인만)."""
    # 다른 작업이 로그인하는 동안(직접 로그인이면 몇 분) 기다리는 것은 Playwright 호출 밖의 쉼이라 멈춤 감시를 쉬게 한다 (hangwatch 머리글)
    with hangwatch.idle():
        _login_lock.acquire()
    try:
        if _check_home(page):
            log.info("로그인 상태 확인됨")
            return
        _login(page, settings)
    finally:
        _login_lock.release()


@contextlib.contextmanager
def session(settings: Settings):
    """작업 하나의 브라우저 세션: (이 프로세스가 띄운) 크롬에 붙어 이 작업의 탭을 열고 로그인까지 확인한 (context, page) - 작업 진입점 공용."""
    with sync_playwright() as pw, browser.real_chrome_context(pw, block_images=settings.block_images, trim_api=settings.trim_api,
                                                                show_chrome=settings.show_chrome) as context:
        page = context.new_page()
        try:
            ensure_logged_in(page, settings)
        except PageStalled as e:
            # 탭이 응답하지 않는 것 - 같은 탭에서 기다려도 소용없다. 탭을 닫고 새 탭에서 한 번만 더 확인한다
            log.warning("로그인 확인 중 %s - 탭을 닫고 새 탭에서 다시 확인", e)
            with contextlib.suppress(Exception):
                page.close()
            page = context.new_page()
            ensure_logged_in(page, settings)
        yield context, page


def _login(page: Page, settings: Settings) -> None:
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
        goto_with_retry(page, LOGIN_URL, "로그인 페이지")
        deadline = time.monotonic() + MANUAL_LOGIN_WAIT_SEC
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            if "/login" not in page.url and _check_home(page):
                log.info("로그인 확인됨")
                return
    raise LoginFailed(f"{MANUAL_LOGIN_WAIT_SEC}초 안에 로그인이 되지 않았습니다.")

"""설치된 진짜 크롬을 직접 띄우고 CDP 로 붙는다.

KREAM 은 네이버 계열 사이트라 봇 탐지를 염두에 둬야 한다. auto-invoice 에서
확인한 대로, Playwright 번들 브라우저나 headless 크롬은 navigator.webdriver 가
켜져 있거나 점수가 낮아 로그인/보안 확인에 걸릴 수 있다. 평범한 인자로 크롬을
직접 실행한 뒤 --remote-debugging-port 로 붙으면 일반 사용자와 구분되지 않는다.

창 숨기기: headless 대신 크롬 창을 모니터 바깥(-32000,0 - Windows 가 최소화된 창을 두는 자리)에 둔다.
사이트 입장에서는 사용자가 창을 최소화해 둔 것과 같고, 실제 최소화와 달리 페이지가 계속
그려지므로 Playwright 의 클릭/스크롤이 멈추지 않는다 (2026-09-04 실측: 새 탭을 열어도
포커스를 뺏지 않고 창도 움직이지 않는다). 필요하면 show_window() 로 다시 불러온다.

프로필은 auth/chrome_profile_kream 에 계속 남는다(로그인 유지, 쓸수록 이력이 쌓임).
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Playwright

from .config import ROOT

log = logging.getLogger(__name__)

AUTH_DIR = ROOT / "auth"
PROFILE_DIR = AUTH_DIR / "chrome_profile_kream"

CHROME_PATH_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
CDP_READY_TIMEOUT_SEC = 30

# 크롬이 가려지거나 화면 밖에 있어도 페이지를 계속 그리게 한다 (Playwright 가 창 있는 실행에 기본으로 주는 것과 같다).
# 자바스크립트에서 보이지 않는 실행 인자라 봇 탐지와 무관하다.
KEEP_RENDERING_ARGS = (
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
)
OFFSCREEN_POS = (-32000, 0)
ONSCREEN_POS = (40, 40)


def chrome_executable() -> str:
    for candidate in CHROME_PATH_CANDIDATES:
        path = Path(os.path.expandvars(candidate))
        if path.exists():
            return str(path)
    raise RuntimeError("설치된 크롬(chrome.exe)을 찾지 못했습니다.")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


# 판정에 전혀 쓰지 않는 리소스. 상품 페이지는 이미지가 용량의 대부분이라 이것만 안 받아도 훨씬 빠르다.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def _abort_heavy(route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def block_heavy_resources(context: BrowserContext) -> None:
    context.route("**/*", _abort_heavy)


# ---------------------------------------------------------------- 크롬 창 위치 (Windows 전용)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
else:
    _user32 = None

CHROME_WINDOW_CLASS = "Chrome_WidgetWin_1"
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


def _find_chrome_hwnd(pid: int) -> int | None:
    """pid 가 가진, 보이는 크롬 최상위 창 하나."""
    found: list[int] = []
    owner = wintypes.DWORD()
    class_name = ctypes.create_unicode_buffer(64)

    def on_window(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                _user32.GetClassNameW(hwnd, class_name, 64)
                if class_name.value == CHROME_WINDOW_CLASS:
                    found.append(int(hwnd))
                    return False
        return True

    _user32.EnumWindows(_ENUM_WINDOWS_PROC(on_window), 0)
    return found[0] if found else None


class ChromeWindow:
    """크롬 창(HWND)을 화면 밖으로 치우거나 다시 불러온다. Windows 가 아니거나 창을 못 찾으면 아무것도 하지 않는다."""

    def __init__(self, pid: int, hidden: bool, find_timeout_sec: float = 3.0) -> None:
        self.hidden = hidden
        self.hwnd: int | None = None
        if _user32 is None:
            return
        deadline = time.monotonic() + find_timeout_sec
        while self.hwnd is None and time.monotonic() < deadline:
            self.hwnd = _find_chrome_hwnd(pid)
            if self.hwnd is None:
                time.sleep(0.1)

    def _move(self, pos: tuple[int, int], flags: int) -> bool:
        if self.hwnd is None:
            return False
        _user32.SetWindowPos(self.hwnd, 0, pos[0], pos[1], 0, 0, SWP_NOSIZE | SWP_NOZORDER | flags)
        return True

    def hide(self) -> bool:
        """창을 화면 밖으로 옮긴다. 작업표시줄에는 남고, 페이지는 계속 그려진다."""
        self.hidden = self._move(OFFSCREEN_POS, SWP_NOACTIVATE) or self.hidden
        return self.hidden

    def show(self) -> bool:
        """창을 화면 왼쪽 위로 불러와 맨 앞에 둔다."""
        if not self._move(ONSCREEN_POS, 0):
            return False
        _user32.SetForegroundWindow(self.hwnd)
        self.hidden = False
        return True

    @contextlib.contextmanager
    def shown(self):
        """사람이 봐야 하는 동안 창을 보여 주고, 끝나면 원래(숨김) 상태로 돌린다."""
        was_hidden = self.hidden
        self.show()
        try:
            yield
        finally:
            if was_hidden:
                self.hide()


_active_window: ChromeWindow | None = None


def show_window() -> bool:
    """지금 돌고 있는 크롬 창을 화면 안으로 불러온다 (GUI 의 '크롬 창 보기')."""
    return bool(_active_window and _active_window.show())


def hide_window() -> bool:
    return bool(_active_window and _active_window.hide())


@contextlib.contextmanager
def window_shown():
    """직접 로그인처럼 사람이 크롬 창을 봐야 하는 구간을 감싼다."""
    if _active_window is None:
        yield
        return
    with _active_window.shown():
        yield


@contextlib.contextmanager
def real_chrome_context(playwright: Playwright, window_size: str = "1400,1000",
                        profile_dir: Path | None = None, block_images: bool = True,
                        show_chrome: bool = False):
    """크롬을 직접 실행해 CDP 로 붙은 BrowserContext (with 문으로 쓴다).

    headless 는 봇 탐지 점수가 바닥이라 쓰지 않는다. 창은 항상 만들되, show_chrome 이 False 면
    처음부터 화면 밖에 그린다 (작업표시줄에만 남음). 실행 중 show_window()/hide_window() 로 바꿀 수 있다.
    block_images 가 True 면 이미지/동영상/폰트를 받지 않는다 (화면에 그림은 안 보이지만 동작은 같다).
    """
    global _active_window
    profile = (profile_dir or PROFILE_DIR).resolve()  # 상대경로를 주면 크롬이 조용히 종료한다
    profile.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    args = [
        chrome_executable(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        f"--window-size={window_size}",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=ko-KR",
        *KEEP_RENDERING_ARGS,
    ]
    if not show_chrome:
        args.append(f"--window-position={OFFSCREEN_POS[0]},{OFFSCREEN_POS[1]}")
    args.append("about:blank")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        if not _wait_for_port(port, CDP_READY_TIMEOUT_SEC):
            raise RuntimeError(f"크롬이 디버깅 포트({port})를 {CDP_READY_TIMEOUT_SEC}초 안에 열지 않았습니다.")
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        _active_window = ChromeWindow(proc.pid, hidden=not show_chrome)
        if not show_chrome:
            if _active_window.hide():  # 크롬이 시작 위치를 화면 안으로 당겼을 때를 대비해 한 번 더 옮긴다
                log.info("크롬 창을 화면 밖에 두고 실행합니다 (작업표시줄의 크롬 아이콘으로 확인 가능)")
            else:
                log.warning("크롬 창을 찾지 못해 실행 중 창 보이기/숨기기를 쓸 수 없습니다")
        context: BrowserContext = browser.contexts[0] if browser.contexts else browser.new_context()
        context.set_default_timeout(15_000)
        if block_images:
            block_heavy_resources(context)
        yield context
    finally:
        _active_window = None
        if browser is not None:
            # 정상 종료를 요청해야 프로필(쿠키)이 디스크에 남는다
            with contextlib.suppress(Exception):
                browser.new_browser_cdp_session().send("Browser.close")
            with contextlib.suppress(Exception):
                browser.close()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)

"""설치된 진짜 크롬을 직접 띄우고 CDP 로 붙는다.

KREAM 은 네이버 계열 사이트라 봇 탐지를 염두에 둬야 한다. auto-invoice 에서
확인한 대로, Playwright 번들 브라우저나 headless 크롬은 navigator.webdriver 가
켜져 있거나 점수가 낮아 로그인/보안 확인에 걸릴 수 있다. 평범한 인자로 크롬을
직접 실행한 뒤 --remote-debugging-port 로 붙으면 일반 사용자와 구분되지 않는다.

프로필은 auth/chrome_profile_kream 에 계속 남는다(로그인 유지, 쓸수록 이력이 쌓임).
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Playwright

from .config import ROOT

AUTH_DIR = ROOT / "auth"
PROFILE_DIR = AUTH_DIR / "chrome_profile_kream"

CHROME_PATH_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)
CDP_READY_TIMEOUT_SEC = 30


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


@contextlib.contextmanager
def real_chrome_context(playwright: Playwright, window_size: str = "1400,1000",
                        profile_dir: Path | None = None):
    """크롬을 직접 실행해 CDP 로 붙은 BrowserContext (with 문으로 쓴다).

    창은 항상 보이게 띄운다 - headless 는 봇 탐지 점수가 바닥이다.
    """
    profile = (profile_dir or PROFILE_DIR).resolve()  # 상대경로를 주면 크롬이 조용히 종료한다
    profile.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    proc = subprocess.Popen(
        [
            chrome_executable(),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--window-size={window_size}",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=ko-KR",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    browser = None
    try:
        if not _wait_for_port(port, CDP_READY_TIMEOUT_SEC):
            raise RuntimeError(f"크롬이 디버깅 포트({port})를 {CDP_READY_TIMEOUT_SEC}초 안에 열지 않았습니다.")
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context: BrowserContext = browser.contexts[0] if browser.contexts else browser.new_context()
        context.set_default_timeout(15_000)
        yield context
    finally:
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

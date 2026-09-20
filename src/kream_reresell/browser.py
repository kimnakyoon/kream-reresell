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

크롬은 프로필 하나를 한 인스턴스만 쓴다. 같은 프로필로 크롬을 또 띄우면 새 크롬은 떠 있는 쪽에 창만 넘기고 조용히 끝나
디버깅 포트가 열리지 않는다. 그래서 띄우기 전에 프로필을 잡고 있는 크롬이 있는지 보고 (프로필의 lockfile 을 크롬이
독점으로 열어 둔다 - 2026-09-06 실측), 있으면 그걸 띄운 python 이 아직 살아 있는지로 나눈다:
  - 살아 있음: 이 프로그램의 다른 실행(GUI 또는 명령행)이 쓰는 중 → 바로 오류 (30초 기다리지 않음)
  - 죽었음: 이전 실행이 남긴 크롬 (GUI 창을 닫거나 작업 관리자로 끝내면 작업 스레드가 정리를 못 해 남는다,
    2026-09-06 실측) → 정상 종료를 요청해 닫고(안 되면 강제 종료) 새로 띄운다
또 띄운 크롬을 Job Object 에 넣어 이 python 이 어떻게 끝나든 크롬도 같이 끝나게 한다 (winproc 참고).

한 프로세스 안에서는 크롬 하나를 여러 작업이 같이 쓴다 (2026-09-17, GUI 버튼 동시 실행): real_chrome_context 를 처음 부른 작업이 크롬을
띄우고, 그 뒤에 부른 작업은 (자기 스레드의 sync_playwright 로) 같은 디버깅 포트에 따로 붙는다. 마지막 작업이 끝날 때만 크롬을 닫는다
(참조 수). 동기 Playwright 객체는 만든 스레드에서만 쓸 수 있어 연결은 작업(스레드)마다 하나씩이고, 작업은 자기 탭만 쓴다 -
SharedContext.new_page() 로 탭을 열면 이미지 차단·API route·접속 세기가 그 탭에만 붙는다 (context.route 는 다른 작업의 탭까지 두 번 가로채므로
쓰지 않는다). 다른 연결이 연 탭도 이 연결의 컨텍스트에 보이지만 (Playwright 는 모든 탭에 붙는다) 건드리지 않는다. 요청·접속 예산(pacing)은
프로세스 전체가 하나를 나눠 쓰므로 동시에 돌아도 사이트에 보내는 양은 그 예산 안이다.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Frame, Page, Playwright

from . import hangwatch, pacing, winproc
from .tab import close_quietly
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

# ---------------------------------------------------------------- 탭의 요청 처리 (사이트 스로틀 대응은 pacing 참고)

_CORS_HEADERS = {"access-control-allow-origin": "https://kream.co.kr", "access-control-allow-credentials": "true"}


def _route(route, block_images: bool, trim_api: bool) -> None:
    """탭의 요청 하나를 처리한다 (탭마다 route 하나):
      - api.kream.co.kr: trim_api 면 asks·bids·chart (프로그램이 안 봄) 를 서버에 보내지 않고 빈 목록(200)으로 채운다
        (abort 하면 사이트가 오류 표시로 바꾼다 - 실측). sales 등 스로틀 대상은 세고(pacing.BUDGET) 그대로 보낸다
      - 그 밖: block_images 면 이미지·동영상·폰트를 받지 않는다

    같은 sales 요청을 캐시해 돌려주는 것은 하지 않는다 - 응답 핸들러에서 response.body() 를 읽거나 route.fetch() 로 받으면
    페이지가 그 응답을 못 받아 표가 빈 채로 그려진다 (2026-09-05 실측, 옵션 전부 0건으로 세어질 뻔함).
    """
    request = route.request
    if request.url.startswith(pacing.API_ORIGIN):
        path = urlparse(request.url).path
        if trim_api and pacing.TRIMMABLE_PATH_RE.match(path):
            route.fulfill(status=200, content_type="application/json", body=pacing.TRIM_BODY, headers=_CORS_HEADERS)
            return
        if request.method == "GET" and pacing.THROTTLED_PATH_RE.match(path):
            pacing.BUDGET.note(path)
    elif block_images and request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
        return
    route.continue_()


# 탭마다 마지막으로 센 주소 - 사이트가 같은 주소로 되풀이하는 replaceState 를 안 세려고 (pacing 대응 5 참고).
# 공개 API 의 framenavigated 는 진짜 문서 로드와 주소 안 이동을 구분해 주지 않아 주소가 바뀔 때만 센다 - 모든 요청을 파이썬으로
# 받는 request 리스너보다 싸다. 그래서 같은 주소를 다시 여는 재시도는 여기서 안 세지고 tab.goto_with_retry 가 직접 센다
_last_visit: dict[Page, str] = {}


def _count_visit(frame: Frame) -> None:
    """메인 프레임이 kream.co.kr 의 다른 주소로 이동했으면(주소 안 이동 pushState 포함) 접속 예산에 하나 센다.

    호출 지점에서 세지 않고 여기서 세므로 상품·구매 페이지뿐 아니라 변경 화면, 입찰 상세, 재시도, 사이트 확인 페이지도 다 들어간다.
    """
    if frame.parent_frame is not None:
        return
    url = frame.url
    if (urlparse(url).hostname or "").endswith("kream.co.kr") and _last_visit.get(frame.page) != url:
        _last_visit[frame.page] = url
        pacing.PAGE_BUDGET.count()


class SharedContext:
    """작업 하나의 탭 묶음 - 같은 크롬을 다른 작업과 나눠 쓰므로 자기 탭만 다룬다 (머리글).

    new_page() 로 연 탭에만 이미지 차단·API 요청 처리·접속 세기가 붙는다. 연결 전체(다른 작업의 탭 포함)의 이벤트가 필요하면
    raw (원본 BrowserContext) 를 쓴다 - api.ApiClient 의 헤더 잡기. route 같은 컨텍스트 단위 조작은 일부러 열어 두지 않는다.
    """

    def __init__(self, context: BrowserContext, block_images: bool, trim_api: bool) -> None:
        self.raw = context
        self.block_images = block_images
        self.trim_api = trim_api
        self._pages: list[Page] = []

    def new_page(self) -> Page:
        page = self.raw.new_page()
        page.route("**/*", lambda route: _route(route, self.block_images, self.trim_api))
        page.on("framenavigated", _count_visit)
        page.on("close", lambda p: _last_visit.pop(p, None))
        self._pages.append(page)
        return page

    def live_page(self, page: Page | None) -> Page:
        """page 가 열려 있으면 그대로, (멈춰서) 닫혔거나 없으면 새 탭 - 멈춘 탭은 tab.close_quietly 로 닫고 이걸로 갈아 끼운다."""
        if page is not None and not page.is_closed():
            return page
        self._pages = [p for p in self._pages if not p.is_closed()]   # 멈춰 닫힌 탭이 장부에 쌓이지 않게
        return self.new_page()

    def close_pages(self) -> None:
        """연결을 끊어도 탭은 크롬에 남으므로 이 작업이 연 탭을 직접 닫는다."""
        for page in self._pages:
            close_quietly(page)
        self._pages.clear()


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


def _window() -> ChromeWindow | None:
    """지금 떠 있는 크롬의 창 (없으면 None)."""
    return _shared.window if _shared is not None else None


def show_window() -> bool:
    """지금 돌고 있는 크롬 창을 화면 안으로 불러온다 (GUI 의 '크롬 창 보기')."""
    window = _window()
    return bool(window and window.show())


def hide_window() -> bool:
    window = _window()
    return bool(window and window.hide())


@contextlib.contextmanager
def window_shown():
    """직접 로그인처럼 사람이 크롬 창을 봐야 하는 구간을 감싼다."""
    window = _window()
    if window is None:
        yield
        return
    with window.shown():
        yield


# ---------------------------------------------------------------- 프로필을 잡고 있는 크롬 정리

def profile_in_use(profile: Path) -> bool:
    """크롬이 이 프로필로 떠 있는지. 크롬은 켜져 있는 동안 프로필의 lockfile 을 독점으로 열어 둔다 (열면 PermissionError)."""
    lock = profile / "lockfile"
    if not lock.exists():
        return False
    try:
        with open(lock, "ab"):
            return False
    except OSError:
        return True


def _wait_until(check, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.3)
    return check()


def _close_leftover_chrome(playwright: Playwright, chrome: winproc.ChromeProcess) -> None:
    """이전 실행이 남긴 크롬을 닫는다. 정상 종료(Browser.close)를 먼저 시켜 프로필(쿠키)이 저장되게 하고, 안 되면 강제 종료."""
    gone = lambda: winproc.process_image(chrome.pid) is None  # noqa: E731
    asked = False
    if chrome.port:
        try:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{chrome.port}", timeout=5_000)
            try:
                browser.new_browser_cdp_session().send("Browser.close")
                asked = True
            finally:
                with contextlib.suppress(Exception):
                    browser.close()
        except Exception as e:  # noqa: BLE001 - 멈춘 크롬은 붙지 못한다 (2026-09-06 실측: 5초 timeout)
            log.info("남은 크롬에 정상 종료를 요청하지 못함 (%s) - 강제 종료", str(e).splitlines()[0])
    if not (asked and _wait_until(gone, 10)):
        winproc.kill_tree(chrome.pid)
        if not _wait_until(gone, 10):
            raise RuntimeError(f"이전 실행이 남긴 크롬(프로세스 {chrome.pid})을 닫지 못했습니다. "
                               "작업 관리자에서 chrome.exe 를 끝낸 뒤 다시 실행해 주세요.")


def reclaim_profile(playwright: Playwright, profile: Path) -> None:
    """프로필을 잡고 있는 크롬이 있으면: 다른 실행이 쓰는 중이면 RuntimeError, 이전 실행이 남긴 것이면 닫는다."""
    if not profile_in_use(profile):
        return
    chromes = winproc.find_profile_chromes(profile)
    if not chromes:
        log.warning("프로필이 잠겨 있는데 그 크롬 프로세스를 찾지 못함 - 그냥 띄워 봅니다")
        return
    for chrome in chromes:
        owner = winproc.process_image(chrome.parent_pid)
        if owner in winproc.PYTHON_IMAGES:
            raise RuntimeError(f"이 프로그램의 다른 실행(프로세스 {chrome.parent_pid})이 크롬(프로세스 {chrome.pid})을 쓰고 있습니다. "
                               "그쪽을 먼저 끝내거나 [중지]한 뒤 다시 실행해 주세요.")
        log.info("이전 실행이 남긴 크롬(프로세스 %d, 띄운 프로세스 %d 는 %s)을 닫고 새로 띄웁니다",
                 chrome.pid, chrome.parent_pid, f"'{owner}'" if owner else "이미 없음")
        _close_leftover_chrome(playwright, chrome)
    if not _wait_until(lambda: not profile_in_use(profile), 10):
        raise RuntimeError("남은 크롬을 닫았는데도 프로필이 아직 잠겨 있습니다. 잠시 뒤 다시 실행해 주세요.")


class _SharedChrome:
    """이 프로세스가 띄운 크롬 하나 - 작업들이 참조 수로 나눠 쓴다 (머리글)."""

    def __init__(self, proc: subprocess.Popen, port: int, window: ChromeWindow) -> None:
        self.proc = proc
        self.port = port
        self.window = window
        self.refs = 0


_shared: _SharedChrome | None = None
_shared_lock = threading.Lock()   # 띄우기·닫기를 통째로 감싼다 - 닫는 중에 새 작업이 오면 다 닫힌 뒤 새로 띄운다


def _launch(playwright: Playwright, profile: Path, window_size: str, show_chrome: bool) -> _SharedChrome:
    """크롬을 띄우고 디버깅 포트가 열릴 때까지 기다린다."""
    reclaim_profile(playwright, profile)
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
    winproc.kill_with_this_process(proc.pid)   # 이 python 이 어떻게 끝나든 크롬도 같이 끝나게 (남으면 다음 실행이 막힌다)
    if not _wait_for_port(port, CDP_READY_TIMEOUT_SEC):
        _stop_process(proc)
        # 같은 프로필을 쓰는 크롬이 이미 떠 있으면 새 크롬은 조용히 종료된다 (reclaim_profile 이 놓친 경우)
        raise RuntimeError(f"크롬이 디버깅 포트({port})를 {CDP_READY_TIMEOUT_SEC}초 안에 열지 않았습니다. "
                           "이 프로그램의 다른 실행(GUI 또는 명령행)이 아직 크롬을 쓰고 있지 않은지, "
                           "작업 관리자에 chrome.exe 가 남아 있지 않은지 확인해 주세요.")
    window = ChromeWindow(proc.pid, hidden=not show_chrome)
    if not show_chrome:
        if window.hide():  # 크롬이 시작 위치를 화면 안으로 당겼을 때를 대비해 한 번 더 옮긴다
            log.info("크롬 창을 화면 밖에 두고 실행합니다 (작업표시줄의 크롬 아이콘으로 확인 가능)")
        else:
            log.warning("크롬 창을 찾지 못해 실행 중 창 보이기/숨기기를 쓸 수 없습니다")
    return _SharedChrome(proc, port, window)


def _stop_process(proc: subprocess.Popen) -> None:
    """정상 종료를 이미 요청한 크롬 프로세스가 끝나기를 기다리고, 안 끝나면 강제 종료."""
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
    winproc.release(proc.pid)   # 그래도 살아 있으면 Job 핸들이 닫히며 죽는다


def _disconnect(browser, shutdown: bool) -> None:
    """이 작업의 연결을 끊는다. shutdown 이면 그 전에 크롬에 정상 종료를 요청한다 (그래야 프로필(쿠키)이 디스크에 남는다)."""
    if browser is None:
        return
    if shutdown:
        with contextlib.suppress(Exception):
            browser.new_browser_cdp_session().send("Browser.close")
    with contextlib.suppress(Exception):
        browser.close()


@contextlib.contextmanager
def real_chrome_context(playwright: Playwright, window_size: str = "1400,1000",
                        profile_dir: Path | None = None, block_images: bool = True,
                        show_chrome: bool = False, trim_api: bool = True):
    """크롬을 직접 실행(또는 이 프로세스가 이미 띄운 크롬에 합류)해 CDP 로 붙은 SharedContext (with 문으로 쓴다).

    headless 는 봇 탐지 점수가 바닥이라 쓰지 않는다. 창은 항상 만들되, show_chrome 이 False 면
    처음부터 화면 밖에 그린다 (작업표시줄에만 남음). 실행 중 show_window()/hide_window() 로 바꿀 수 있다.
    block_images 가 True 면 이 작업이 여는 탭에서 이미지/동영상/폰트를 받지 않는다 (화면에 그림은 안 보이지만 동작은 같다).
    trim_api 가 True 면 프로그램이 안 보는 상품 API(asks·bids·chart) 요청을 막는다 (사이트 스로틀 대응, pacing 참고).
    playwright 는 부르는 스레드의 sync_playwright() 여야 한다 - 작업마다 자기 연결로 붙는다 (머리글).
    크롬은 처음 부른 작업이 띄우고 (window_size·창 위치는 그때 값), 마지막 작업이 끝날 때 닫힌다.
    """
    global _shared
    profile = (profile_dir or PROFILE_DIR).resolve()  # 상대경로를 주면 크롬이 조용히 종료한다
    profile.mkdir(parents=True, exist_ok=True)
    with _shared_lock:
        if _shared is None:
            _shared = _launch(playwright, profile, window_size, show_chrome)
        else:
            log.info("이 프로그램이 띄워 둔 크롬(포트 %d)에 합류합니다 (작업 %d개째)", _shared.port, _shared.refs + 1)
        chrome = _shared
        chrome.refs += 1
        pacing.job_started()
    browser = None
    context: SharedContext | None = None
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{chrome.port}")
        if show_chrome and chrome.window.hidden:   # 나중에 합류한 작업이 창 보기를 켜 두었으면 불러온다
            chrome.window.show()
        raw: BrowserContext = browser.contexts[0] if browser.contexts else browser.new_context()
        raw.set_default_timeout(15_000)
        context = SharedContext(raw, block_images, trim_api)
        # 탭의 렌더러가 완전히 멈춰 호출이 영영 안 돌아오면 그 탭을 닫아 이어가게 한다 (hangwatch 참고, 2026-09-06 실측) - 연결마다 하나
        hangwatch.start(raw, chrome.port)
        yield context
    finally:
        hangwatch.stop()
        if context is not None:
            context.close_pages()
        # 마지막 작업이면 크롬을 닫는데, 그동안 잠금을 쥔다 - 다 닫히기 전에 새 작업이 같은 프로필로 새 크롬을 띄우면 프로필 잠김에 걸린다
        with _shared_lock:
            chrome.refs -= 1
            pacing.job_ended()
            last = chrome.refs == 0
            if last:
                _shared = None
            _disconnect(browser, shutdown=last)
            if last:
                _stop_process(chrome.proc)
            else:
                log.info("이 작업의 연결을 끊음 - 크롬은 남은 작업 %d개가 계속 씀", chrome.refs)

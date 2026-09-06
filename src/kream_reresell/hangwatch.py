"""크롬 탭이 완전히 멈춰 Playwright 호출이 영영 돌아오지 않을 때 그 탭을 닫아 작업을 이어가게 하는 감시 스레드.

2026-09-06 실측 ([재입찰] 160번째 상품): 구매 페이지로 넘어간 직후 그 탭의 렌더러가 멈춰 (JS·DOM 명령에 답하지 않음,
CPU 0, 브라우저 프로세스와 다른 탭은 정상) product._pass_model_number_check 의 wait_for_function(8초 제한), 이어서
'즉시 판매가' locator.wait_for(10초 제한)가 47분 동안 돌아오지 않았다. Playwright 의 시간 제한은 드라이버(node)가 재는데,
렌더러가 이렇게 멈추면 그 제한마저 돌아오지 않는 경우가 있어 python 은 응답을 기다리며 서 있고 [중지]도 (입찰 사이에서만
확인하므로) 듣지 않았다. 디버깅 포트로 그 탭을 닫자 (/json/close) 걸린 호출이 바로 TargetClosedError 로 돌아왔고 다음 입찰부터 정상.
(화면만 안 그리고 JS 는 도는 가벼운 멈춤은 product.PageStalled 로 따로 다룬다 - 그건 호출이 제때 타임아웃으로 돌아온다.)

방식: 작업 스레드와 별개의 스레드가 TICK_SEC 마다 Playwright 연결의 '응답을 기다리는 요청' 목록을 보고, 같은 요청이
LIMIT_SEC 넘게 남아 있으면 (코드의 시간 제한은 길어야 20초라 정상이면 있을 수 없음) 크롬 디버깅 포트의 HTTP 끝점
(/json/close/{탭 ID} - 브라우저 프로세스가 처리하므로 렌더러가 멈춰도 답한다) 로 지금 보는 탭을 닫는다. 탭이 닫히면
걸려 있던 호출이 '탭이 닫힘' 오류로 돌아오고, 부른 쪽([재입찰] rebid.run)은 새 탭을 열어 그 입찰을 한 번 더 본다.
닫을 탭은 watching()/set_page() 로 알려 준 '지금 쓰는 탭' 을 주소로 찾는다 (알려 주지 않았으면 닫지 않고 로그만 남긴다).

Playwright 의 내부 속성(_connection._callbacks)을 읽으므로 버전이 바뀌어 속성이 없으면 감시를 끄고 경고만 남긴다.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from playwright.sync_api import BrowserContext, Page

log = logging.getLogger(__name__)

LIMIT_SEC = 90       # 요청 하나가 이만큼 응답이 없으면 멈춘 것으로 본다 (코드의 가장 긴 시간 제한 20초의 4배 넘게)
TICK_SEC = 3.0       # 확인 간격
RECHECK_SEC = 60     # 탭을 닫고도 같은 요청이 이만큼 더 남아 있으면 경고 (더 할 수 있는 게 없다)


@dataclass
class Trip:
    """탭을 닫은 기록."""
    at: datetime
    url: str
    waited_sec: int
    note: str

    def describe(self) -> str:
        return f"페이지가 {self.waited_sec}초 넘게 응답하지 않아 탭을 닫음 ({self.note})"


_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_context: BrowserContext | None = None
_port: int | None = None
_pages: list[Page] = []          # 지금 쓰는 탭 (안쪽이 마지막)
_trip: Trip | None = None
_disabled = False


def start(context: BrowserContext, port: int) -> None:
    """크롬에 붙은 직후 부른다. 감시 스레드를 띄운다 (이미 떠 있으면 대상만 바꾼다)."""
    global _thread, _context, _port, _trip, _disabled
    with _lock:
        _context, _port, _trip, _disabled = context, port, None, False
        _pages.clear()
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="hangwatch", daemon=True)
    _thread.start()


def stop() -> None:
    global _context, _port
    _stop.set()
    with _lock:
        _context, _port = None, None
        _pages.clear()


def set_page(page: Page) -> None:
    """멈추면 닫을 탭을 알려 준다 (안 쓰게 되면 clear_page)."""
    with _lock:
        _pages.append(page)


def clear_page(page: Page) -> None:
    with _lock:
        with contextlib.suppress(ValueError):
            _pages.remove(page)


@contextlib.contextmanager
def watching(page: Page):
    set_page(page)
    try:
        yield
    finally:
        clear_page(page)


def tripped() -> Trip | None:
    """탭을 닫은 기록이 있으면 그것 (지우지 않음)."""
    return _trip


def take_trip() -> Trip | None:
    """탭을 닫은 기록을 돌려주고 지운다 - 부른 쪽이 새 탭으로 다시 시도할 때."""
    global _trip
    with _lock:
        t, _trip = _trip, None
    return t


# ---------------------------------------------------------------- 감시 스레드

def _pending_ids(context: BrowserContext) -> set[int] | None:
    """드라이버 응답을 기다리는 요청 ID. 속성이 없으면(Playwright 내부가 바뀜) None."""
    global _disabled
    try:
        callbacks = context._impl_obj._connection._callbacks  # type: ignore[attr-defined]
        return {i for i, cb in list(callbacks.items()) if not cb.no_reply and not cb.future.done()}
    except RuntimeError:       # 다른 스레드가 dict 를 바꾸는 중 - 다음 틱에 다시
        return set()
    except AttributeError:
        if not _disabled:
            _disabled = True
            log.warning("Playwright 내부 구조가 달라 멈춤 감시를 쓸 수 없습니다 (탭이 멈추면 [중지]가 듣지 않을 수 있음)")
        return None


def _loop() -> None:
    first_seen: dict[int, float] = {}
    closed_for: tuple[int, float] | None = None     # (요청 ID, 닫은 시각)
    while not _stop.wait(TICK_SEC):
        with _lock:
            context, port, page = _context, _port, (_pages[-1] if _pages else None)
        if context is None or port is None:
            first_seen.clear()
            continue
        pending = _pending_ids(context)
        if pending is None:
            return
        now = time.monotonic()
        for i in pending:
            first_seen.setdefault(i, now)
        for i in [k for k in first_seen if k not in pending]:
            del first_seen[i]
            if closed_for and closed_for[0] == i:
                closed_for = None
        if not first_seen:
            continue
        oldest = min(first_seen, key=first_seen.get)
        age = now - first_seen[oldest]
        if closed_for and closed_for[0] == oldest:
            if now - closed_for[1] > RECHECK_SEC:
                log.error("탭을 닫았는데도 %d초째 응답이 없음 - 크롬 전체가 멈춘 듯함. GUI 창을 닫고 다시 실행해 주세요", int(age))
                closed_for = (oldest, now)
            continue
        if age < LIMIT_SEC:
            continue
        if page is None:
            log.warning("Playwright 요청이 %d초째 응답이 없는데 닫을 탭을 모름 - 그대로 둠", int(age))
            closed_for = (oldest, now)
            continue
        try:
            _close_tab(context, page, port, int(age))
        except Exception:  # noqa: BLE001
            log.exception("멈춘 탭을 닫지 못함")
        closed_for = (oldest, now)


def _close_tab(context: BrowserContext, page: Page, port: int, age_sec: int) -> None:
    global _trip
    url = page.url
    targets = _list_targets(port)
    pages = [t for t in targets if t.get("type") == "page"]
    hit = [t for t in pages if t.get("url") == url]
    note = "주소로 찾음"
    if not hit:
        # 주소가 막 바뀌는 중이었을 수 있다 - 다른 탭들의 주소에 없는 탭이 하나뿐이면 그것
        others = {p.url for p in context.pages if p._impl_obj is not page._impl_obj}  # type: ignore[attr-defined]
        rest = [t for t in pages if t.get("url") not in others]
        if len(rest) == 1:
            hit, note = rest, "다른 탭 주소를 빼고 찾음"
    if not hit:
        log.warning("Playwright 요청이 %d초째 응답이 없는데 닫을 탭(%s)을 디버깅 포트 목록에서 찾지 못함 - 그대로 둠", age_sec, url)
        return
    target_id = hit[0]["id"]
    log.warning("페이지가 %d초 넘게 응답하지 않음 (크롬 렌더러가 멈춤) - 탭을 닫아 걸린 호출을 끝냄: %s", age_sec, url)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=5) as resp:
        body = resp.read().decode("utf-8", "replace").strip()
    log.info("탭 닫기 요청 응답: %s", body or "(없음)")
    with _lock:
        _trip = Trip(at=datetime.now(), url=url, waited_sec=age_sec, note=note)


def _list_targets(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))

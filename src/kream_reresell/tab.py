"""탭 다루기: 지금 어디에 있는지(사이트·로그인 화면), 탭이 응답하는지(멈춤 감지), 시간 제한 있는 평가·대기, 재시도하는 이동, 조용히 닫기.

KREAM 화면 내용과 무관한 아래층이다 - api·auth·product·cancel·목록 소스(ranking·shop·search)가 같이 쓴다. product 를 import 하지 않는다.
"""

from __future__ import annotations

import contextlib
import logging
import re
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from . import pacing
from .errors import PageStalled

log = logging.getLogger(__name__)

SITE_HOST = "kream.co.kr"


def on_site(page: Page) -> bool:
    """페이지가 kream.co.kr 문서에 있는지 - 다른 곳(about:blank 등)에서 fetch 하면 CORS 로 막힌다."""
    try:
        return not page.is_closed() and (urlparse(page.url).hostname or "").endswith(SITE_HOST)
    except Exception:  # noqa: BLE001
        return False


def on_login_page(page: Page) -> bool:
    """로그인 화면인지. 주소의 returnUrl 에 /products/{id}·/buy/{id} 가 그대로 들어가므로 문자열 포함이 아니라 경로로 본다."""
    return urlparse(page.url).path.startswith("/login")


PROBE_MS = 1500     # 늘 참인 JS 가 이 안에 안 돌아오면 멈춘 것으로 본다 (정상이면 20ms 안)


def eval_bounded(page: Page, js: str, arg=None, what: str = "페이지 상태"):
    """page.evaluate 대신 쓰는 시간 제한 있는 평가 (js 는 인자 하나를 받는 함수, 거짓 값을 돌려줘도 된다).

    evaluate 는 타임아웃이 없어 탭이 멈추면 영영 안 돌아온다 - 시간 제한이 있는 wait_for_function 으로 대신하고
    (타임아웃은 드라이버 쪽에서 재므로 페이지가 응답하지 않아도 제때 돌아온다), 안 돌아오면 PageStalled.
    wait_for_function 은 참 값이 나와야 돌아오므로 결과를 객체로 감싸 한 번에 돌려받는다.
    """
    try:
        return page.wait_for_function(f"(a) => ({{ v: ({js})(a) }})", arg=arg, polling=100,
                                      timeout=PROBE_MS * 2).json_value().get("v")
    except PlaywrightTimeout as e:
        raise PageStalled(f"{what}를 읽지 못함 - 페이지가 응답하지 않음 (탭이 멈춤?)") from e


def page_stall(page: Page) -> str | None:
    """탭이 응답하고 있으면 None, 아니면 사유. wait_for_function 은 조건을 먼저 한 번 바로 평가하므로 늘 참인 조건이
    PROBE_MS 안에 안 돌아오면 렌더러가 JS 를 돌리지 못하는 것이다.

    page.evaluate 는 타임아웃이 없어 렌더러가 완전히 멈추면 영영 돌아오지 않는다 - 그래서 시간 제한이 있는 wait_for_function 을 쓴다
    (타임아웃은 드라이버 쪽에서 재므로 페이지가 응답하지 않아도 제때 돌아온다).
    """
    try:
        page.wait_for_function("() => true", timeout=PROBE_MS)
        return None
    except PlaywrightTimeout:
        return "페이지가 응답하지 않음 (탭이 멈춤?)"
    except PlaywrightError as e:
        return f"페이지가 응답하지 않음 ({str(e).splitlines()[0]})"


_CALL_LOG_NOISE = re.compile(r"^(Call log|retrying|\d+ × |waiting \d+ms|attempting)")


def timeout_why(e: Exception) -> str:
    """Playwright 시간 제한 오류를 한 줄로: 첫 줄 + 호출 기록에서 마지막으로 하던 일. 첫 줄만으로는 버튼을 다른 요소가
    덮은 것('… intercepts pointer events')과 눌렀는데 응답이 없는 것('performing click action')을 가릴 수 없다."""
    lines = [ln.strip().lstrip("- ") for ln in str(e).splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return str(e)
    tail = [ln for ln in lines[1:] if not _CALL_LOG_NOISE.match(ln)]
    return lines[0] if not tail else f"{lines[0]} 마지막 기록: {' / '.join(tail[-2:])}"


def shown_within(page: Page, js: str, timeout_ms: int, arg=None) -> bool:
    """js(인자 하나를 받는 함수)가 timeout_ms 안에 참이 되면 True, 아니면 False. 시간 제한은 드라이버가 재므로 탭이 멈춰도 제때 돌아온다."""
    try:
        page.wait_for_function(js, arg=arg, timeout=timeout_ms)
        return True
    except PlaywrightTimeout:
        return False


def raise_if_stalled(page: Page, button: str, cause: Exception) -> None:
    """버튼 클릭이 타임아웃한 뒤 부른다 - 버튼이 보이는데도 못 누르는 건 탭이 응답하지 않는 것일 수 있고, 그러면 재시도해도
    소용없으니 바로 PageStalled 로 알린다 ([재입찰]은 탭을 닫고 새 탭에서 한 번 더 본다). 응답하면 그냥 돌아온다 (재시도)."""
    stall = page_stall(page)
    if stall:
        raise PageStalled(f"{button} 를 누르지 못함 - {stall}") from cause


def goto_with_retry(page: Page, url: str, what: str) -> None:
    """url 로 이동한다. 이동이 15초 안에 안 끝나면 탭이 응답하는지 본다 - 응답하지 않으면 PageStalled ([재입찰]은 탭을 닫고 새 탭에서
    한 번 더 본다. 2026-09-08 재입찰 123번째 실측: 구매 페이지에서 상품 페이지로 가는 이동이 안 끝나고 스냅샷도 못 찍혔는데 다음 입찰의
    이동은 정상이라 같은 탭에서 다시 시도해도 소용없다). 응답하면 사이트·망이 느린 것 (2026-09-06 47번째: 스냅샷은 찍힘) - 1.5초 뒤 한 번
    더 열고, 그래도 안 되면 그 오류(PlaywrightError)를 그대로 올린다. what 은 로그용 이름 ('상품 페이지' / '구매 페이지').

    작업의 진입 이동(로그인 확인용 홈, 구매 입찰 목록, 랭킹·SHOP·검색 목록)도 이것으로 연다 - 재시도 없이 올리면 넘김 한 번에
    작업이나 목록 하나가 통째로 끝난다 (2026-09-20 09:38 재입찰 실측: 홈 이동이 15초를 넘겨 시작하자마자 오류 창).
    """
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except PlaywrightError as e:   # 시간 제한(PlaywrightTimeout) 또는 이동 중 끊김 (net::ERR_ABORTED, 2026-09-05 재입찰 실측)
            timed_out = isinstance(e, PlaywrightTimeout)
            stall = page_stall(page) if timed_out else None
            if stall:
                raise PageStalled(f"{what} 이동이 안 끝남 - {stall}") from e
            if attempt:
                raise
            log.info("%s 이동이 %s (%s) - 1.5초 뒤 다시 엶", what, "안 끝남" if timed_out else "끊김", timeout_why(e))
            page.wait_for_timeout(1500)
            if page.url == url:
                # 첫 이동이 주소까지는 바꿨다 - 같은 주소를 다시 여는 것은 browser._count_visit 가 못 세니 (주소가 바뀔 때만 셈) 여기서 센다.
                # 주소도 못 바꾸고 끝났으면 (연결 실패 등) 다시 열 때 _count_visit 가 센다
                pacing.PAGE_BUDGET.count()


def close_quietly(page: Page) -> None:
    """탭을 닫는다. 이미 닫혔거나 닫기가 실패해도 조용히 넘어간다 (멈춘 탭도 닫기는 브라우저 프로세스가 처리해 바로 돌아온다)."""
    with contextlib.suppress(Exception):
        page.close()

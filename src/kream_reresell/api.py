"""KREAM API 를 로그인된 페이지 안에서 fetch 로 부른다 - [내역] · [재입찰] · [입찰] 공용.

API 는 브라우저 밖에서 부르면 막히지만(요청 서명, 2026-09-04 실측 - curl 은 늘 10초 뒤 500), 페이지 안에서 사이트가 실제로 보낸
요청의 헤더(authorization, x-kream-*)를 그대로 붙여 fetch 하면 된다 (credentials 는 omit 이어야 CORS 를 통과한다).
헤더는 사이트가 보내는 요청 중 200 을 받은 것에서 복사한다: 컨텍스트를 주면 어느 탭이든 사이트의 API 요청이 200 을 받을 때마다 조용히
갈아 둔다(페이지 이동 없음 - 항상 방금 통한 토큰). 하나도 못 받았거나 401 을 맞으면 마이페이지로 한 번 이동해 잡는다. 시각 헤더만 매번 새로 넣는다.
토큰은 로그인 2시간마다 바뀐다 (2026-09-14·17·18 실측: 401 이 정확히 2시간 간격). 바뀌는 순간 탭의 SPA 가 먼저 옛 토큰으로 한 번 보내고
401 을 받은 뒤 새 토큰으로 다시 보내므로, '첫 요청' 을 잡으면 옛 토큰이 잡힌다 - 그래서 200 응답을 받은 요청만 본다.

시간 제한: 사이트가 막으면 요청이 10초쯤 응답 없이 붙들렸다 끊긴다 (2026-09-05 실측, pacing 참고). 그래서 fetch 에 TIMEOUT_MS 를 두어
그 상태를 '무응답' (ApiError.kind == "timeout") 으로 바로 알린다 - 부르는 쪽(pacing.ApiPacer)이 차단 신호로 세어 간격을 늘린다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from playwright.sync_api import BrowserContext, Page, Request, Response, TimeoutError as PlaywrightTimeout

from .tab import on_login_page, on_site

log = logging.getLogger(__name__)

API_BASE = "https://api.kream.co.kr"
INVENTORY_FINISHED_URL = "https://kream.co.kr/my/inventory?tab=finished"
KST = timezone(timedelta(hours=9))
TIMEOUT_MS = 12_000             # 막히면 10초 홀드 뒤 끊기므로 그보다 조금 길게 - 그 안에 안 오면 무응답으로 본다
PARALLEL_FETCH = 10             # 한 번에 이만큼 동시에 받는다 (순차보다 5배쯤 빠르다, [내역])
HEADER_KEEP = ("authorization", "accept")

# 요청 목록을 한 번에 보낸다 (하나짜리 호출도 이걸로). 요청마다 {url, method, body} - body 가 있으면 JSON 으로 보낸다 ([판매] 의 review_live·set_live).
# 항목마다 {status, body, text} - 무응답 -2, 망 오류 -1
_FETCH_MANY_JS = """
async ([reqs, headers, timeoutMs]) => Promise.all(reqs.map(async ({url, method, body: payload}) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const init = { method: method || 'GET', credentials: 'omit', headers, signal: ctl.signal };
    if (payload !== undefined && payload !== null) {
      init.headers = { ...headers, 'content-type': 'application/json' };
      init.body = JSON.stringify(payload);
    }
    const r = await fetch(url, init);
    const text = await r.text();
    let body = null;
    try { body = JSON.parse(text); } catch (e) { body = null; }
    return { status: r.status, body, text: body === null ? text.slice(0, 300) : '' };
  } catch (e) {
    return { status: (e && e.name === 'AbortError') ? -2 : -1, body: null, text: String(e) };
  } finally {
    clearTimeout(timer);
  }
}))
"""


class ApiError(Exception):
    """API 호출이 200 + JSON 으로 끝나지 않았다.

    status: HTTP 상태 (무응답 -2, 망 오류 -1, 페이지 명령 실패 0).
    kind: "timeout"(시간 안에 응답 없음) / "network"(fetch 자체 실패) / "http"(상태 코드) / "page"(페이지 명령이 실패)
          / "auth"(헤더를 잡으러 간 페이지가 로그인 화면 - status 401 로 두어 is_auth_lost 가 참).
    is_block_signal: 사이트가 막았을 때 나는 모양(무응답 · 망 오류 · 429 · 403 · 5xx)인지 - pacing.ApiPacer 가 간격을 늘리는 근거.
    """

    def __init__(self, message: str, status: int = 0, kind: str = "http") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind

    @property
    def is_block_signal(self) -> bool:
        # kind "closed" (탭이 멈춰 닫혀 호출이 끊김) 는 사이트로 나간 요청이 아니라 차단 신호가 아니다 - 세면 시세 틱만 두 배로 늘어난다
        return self.kind in ("timeout", "network", "page") or self.status in (429, 403) or self.status >= 500

    @property
    def is_gone(self) -> bool:
        """상품(자료)이 없는 응답 - 사이트 문제가 아니라 그 항목만 건너뛴다."""
        return self.status in (400, 404)

    @property
    def is_auth_lost(self) -> bool:
        """헤더를 다시 잡아도 401 - 세션이 끊긴 것 (로그인 뒤 24시간쯤, product.LoginNeeded 참고). 사이트가 막은 게 아니라 다시 로그인해야 한다
        (2026-09-13 03:08 실측: 401 을 '판단 불가' 로 세어 사이트 대기에 들어가 7시간 동안 5분마다 같은 401 만 받았다)."""
        return self.status == 401


def _is_api_request(request: Request) -> bool:
    return request.url.startswith(API_BASE) and "authorization" in request.headers and "notification" not in request.url


def _is_ok_api_response(response: Response) -> bool:
    """인증 헤더를 달고 나가 200 을 받은 API 응답인지 - 이 요청의 토큰은 지금 통하는 것이다."""
    return response.status == 200 and _is_api_request(response.request)


class ApiClient:
    """페이지 안에서 fetch 로 KREAM API 를 부른다. 헤더는 사이트가 실제로 보내 200 을 받은 요청에서 복사한다 (모듈 머리글).

    page: fetch 를 실행할 탭을 돌려주는 함수 - 부를 때마다 지금 쓸 탭. 보통 browser.LiveTab (닫혔으면 새 탭), 닫힌 탭을 그대로 받아야 하는
    [재입찰] 은 LiveTab.same. 어느 쪽인지는 넘기는 쪽이 정한다.
    context: 주면 어느 탭이든 사이트가 API 요청을 보낼 때 헤더를 받아 둔다 - 로그인 뒤 목록 페이지를 여는 동안 저절로 잡혀 마이페이지 이동이 필요 없다.
    """

    def __init__(self, page: Callable[[], Page], context: BrowserContext | None = None) -> None:
        self._page = page
        self.headers: dict[str, str] = {}
        self.calls = 0          # 이 클라이언트로 보낸 요청 수 (로그용)
        if context is not None:
            context.on("response", self._sniff)

    @property
    def page(self) -> Page:
        return self._page()

    def _sniff(self, response: Response) -> None:
        """200 을 받은 API 요청의 헤더를 받아 둔다 - 방금 통한 토큰이라 늘 최신으로 갈아 둔다
        (컨텍스트의 response 이벤트 - 다른 스레드에서 올 수 있어 dict 를 통째로 바꾼다)."""
        if _is_ok_api_response(response):
            self.headers = {k: v for k, v in response.request.headers.items()
                            if k.lower().startswith("x-kream") or k.lower() in HEADER_KEEP}
            log.debug("API 헤더 %d개 확보 (200 을 받은 사이트 요청에서)", len(self.headers))

    def invalidate(self) -> None:
        """로그인을 다시 했다 - 옛 세션의 헤더를 버린다 (다음 호출이 새로 잡는다)."""
        self.headers = {}

    def capture_headers(self, url: str = INVENTORY_FINISHED_URL) -> None:
        """url 로 이동하면서 사이트가 API 에 보내 200 을 받은 요청의 헤더를 잡아 둔다 (페이지 이동 1번 - 저절로 못 잡았거나 401 을 맞았을 때).

        갖고 있던 헤더는 먼저 버린다 - 401 뒤에 옛 토큰을 들고 있으면 다시 잡아도 그대로라 재시도가 반드시 401 이었다 (2026-09-17·18 실측:
        토큰이 바뀌는 2시간째마다 [판매] 가 '다시 로그인했는데도 401' 로 죽음).
        세션이 끊기면 마이페이지가 /login 으로 넘어가 인증된 API 요청이 하나도 안 나간다 - 사이트가 막은 게 아니라 다시 로그인할 일이라
        status 401 (is_auth_lost) 로 올린다 (2026-09-14 10:35 실측: 이 시간 제한을 차단 신호로 세어 사이트 대기에 들어가 20분 동안 안 풀렸다).
        이동 직후 이미 로그인 화면이면 20초를 기다리지 않는다 - with 블록 안의 예외는 대기를 취소한다 (Playwright 1.62 _sync_base.EventContextManager).
        """
        self.invalidate()
        try:
            with self.page.expect_response(_is_ok_api_response, timeout=20_000) as res:
                self.page.goto(url, wait_until="domcontentloaded")
                self._raise_if_login_page()
            self._sniff(res.value)
        except PlaywrightTimeout as e:
            self._raise_if_login_page(e)   # SPA 라우팅으로 늦게 넘어간 경우
            raise ApiError("KREAM API 요청 헤더를 잡지 못했습니다 (로그인 상태와 페이지를 확인)", kind="page") from e

    def _raise_if_login_page(self, cause: Exception | None = None) -> None:
        if on_login_page(self.page):
            raise ApiError(f"헤더를 잡으러 간 페이지가 로그인 화면 (로그인이 풀림): {self.page.url}", status=401, kind="auth") from cause

    def _request_headers(self) -> dict[str, str]:
        if not self.headers or not on_site(self.page):
            # 헤더가 없거나 탭이 사이트 밖(새 탭 about:blank 등)에 있으면 마이페이지로 이동하며 잡는다
            self.capture_headers()
        headers = dict(self.headers)
        headers["x-kream-client-datetime"] = datetime.now(KST).strftime("%Y%m%d%H%M%S+0900")
        return headers

    @staticmethod
    def _url(path: str) -> str:
        return path if path.startswith("http") else API_BASE + path

    @staticmethod
    def _error(path: str, res: dict) -> ApiError:
        status = res.get("status", 0)
        if status == -2:
            return ApiError(f"API 무응답 ({TIMEOUT_MS // 1000}초 안에 응답 없음, {path})", status=-2, kind="timeout")
        if status == -1:
            return ApiError(f"API 호출 실패 ({path}): {res.get('text', '')[:200]}", status=-1, kind="network")
        if status == 0:
            return ApiError(f"API 호출 실패 ({path}): {res.get('text', '')[:200]}", status=0, kind="page")
        return ApiError(f"API 응답 오류 {status} ({path}): {res.get('text', '')[:200]}", status=status, kind="http")

    def _fetch(self, paths: list[str], retry: bool = True) -> list[dict | ApiError]:
        """GET 여러 개를 한 번에 받는다 (PARALLEL_FETCH 개 이하). 항목마다 응답 dict 또는 ApiError."""
        return self._send([(p, "GET", None) for p in paths], retry)

    def _send(self, reqs: list[tuple[str, str, dict | None]], retry: bool = True) -> list[dict | ApiError]:
        """(경로, 메서드, JSON 본문) 요청들을 한 번에 보낸다. 항목마다 응답 dict 또는 ApiError.
        인증이 끊긴 항목(401/403)은 헤더를 한 번만 다시 잡고 그 항목들만 한 번 더 보낸다 (POST 도 같다 - 가격 변경은 멱등이라 다시 보내도 된다)."""
        self.calls += len(reqs)
        paths = [p for p, _, _ in reqs]
        page = self.page
        try:
            if page.is_closed():
                raise RuntimeError("탭이 닫혀 있음")
            results = page.evaluate(_FETCH_MANY_JS, [[{"url": self._url(p), "method": m, "body": b} for p, m, b in reqs],
                                                     self._request_headers(), TIMEOUT_MS])
        except ApiError as e:
            return [e] * len(reqs)
        except Exception as e:  # noqa: BLE001
            if page.is_closed():
                # 탭이 (멈춰서) 닫혀 호출이 끊긴 것 - 사이트 문제가 아니다. 넘겨받은 함수가 새 탭을 주면(LiveTab) 거기서 한 번 더 보낸다
                # (수백 건을 읽는 [내역] 이 통째로 버려지지 않게). 닫힌 탭을 그대로 주면([재입찰] 의 LiveTab.same) 부른 쪽이 새 탭에서 다시 본다
                if retry and self.page is not page:
                    log.info("탭이 닫혀 API 호출이 끊김 - 새 탭에서 한 번 더 보냄 (%s)", paths[0])
                    self.calls -= len(reqs)
                    return self._send(reqs, retry=False)
                return [ApiError(f"API 호출 실패 ({p}): 탭이 닫힘 ({str(e).splitlines()[0] if str(e) else type(e).__name__})", kind="closed")
                        for p in paths]
            return [ApiError(f"API 호출 실패 ({p}): {e}", kind="page") for p in paths]
        out: list[dict | ApiError] = []
        redo: list[int] = []
        for i, (path, res) in enumerate(zip(paths, results)):
            if res["status"] == 200 and isinstance(res.get("body"), dict):
                out.append(res["body"])
            elif res["status"] in (401, 403) and retry:
                redo.append(i)
                out.append(self._error(path, res))
            else:
                out.append(self._error(path, res))
        if redo:
            log.info("API 인증이 끊겨 헤더를 다시 잡습니다 (%d건)", len(redo))
            self.capture_headers()
            for i, again in zip(redo, self._send([reqs[i] for i in redo], retry=False)):
                out[i] = again
        return out

    def get(self, path: str) -> dict:
        """GET 하나. 200 + JSON 객체면 그 객체, 아니면 ApiError."""
        result = self._fetch([path])[0]
        if isinstance(result, ApiError):
            raise result
        return result

    def post(self, path: str, body: dict) -> dict:
        """POST 하나 (JSON 본문). 200 + JSON 객체면 그 객체, 아니면 ApiError. 응답 본문의 업무 오류(message 등)는 부르는 쪽이 본다."""
        result = self._send([(path, "POST", body)])[0]
        if isinstance(result, ApiError):
            raise result
        return result

    def get_many(self, paths: list[str]) -> list[dict | ApiError]:
        """여러 경로를 PARALLEL_FETCH 개씩 동시에 받는다. 항목마다 응답 dict 또는 ApiError."""
        out: list[dict | ApiError] = []
        for i in range(0, len(paths), PARALLEL_FETCH):
            out.extend(self._fetch(paths[i:i + PARALLEL_FETCH]))
        return out

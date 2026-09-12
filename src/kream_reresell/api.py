"""KREAM API 를 로그인된 페이지 안에서 fetch 로 부른다 - [내역] · [재입찰] · [입찰] 공용.

API 는 브라우저 밖에서 부르면 막히지만(요청 서명, 2026-09-04 실측 - curl 은 늘 10초 뒤 500), 페이지 안에서 사이트가 실제로 보낸
요청의 헤더(authorization, x-kream-*)를 그대로 붙여 fetch 하면 된다 (credentials 는 omit 이어야 CORS 를 통과한다).
헤더는 사이트가 보내는 요청에서 복사한다: 컨텍스트를 주면 어느 탭이든 사이트가 API 요청을 보낼 때 조용히 받아 두고(페이지 이동 없음),
그때까지 하나도 못 받았으면 마이페이지로 한 번 이동해 잡는다. 시각 헤더만 매번 새로 넣는다.

시간 제한: 사이트가 막으면 요청이 10초쯤 응답 없이 붙들렸다 끊긴다 (2026-09-05 실측, pacing 참고). 그래서 fetch 에 TIMEOUT_MS 를 두어
그 상태를 '무응답' (ApiError.kind == "timeout") 으로 바로 알린다 - 부르는 쪽(pacing.ApiPacer)이 차단 신호로 세어 간격을 늘린다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Request, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

API_BASE = "https://api.kream.co.kr"
SITE_HOST = "kream.co.kr"
INVENTORY_FINISHED_URL = "https://kream.co.kr/my/inventory?tab=finished"
KST = timezone(timedelta(hours=9))
TIMEOUT_MS = 12_000             # 막히면 10초 홀드 뒤 끊기므로 그보다 조금 길게 - 그 안에 안 오면 무응답으로 본다
PARALLEL_FETCH = 10             # 한 번에 이만큼 동시에 받는다 (순차보다 5배쯤 빠르다, [내역])
HEADER_KEEP = ("authorization", "accept")

# 주소 목록을 한 번에 받는다 (하나짜리 호출도 이걸로). 항목마다 {status, body, text} - 무응답 -2, 망 오류 -1
_FETCH_MANY_JS = """
async ([urls, headers, timeoutMs]) => Promise.all(urls.map(async (url) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { credentials: 'omit', headers, signal: ctl.signal });
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
    kind: "timeout"(시간 안에 응답 없음) / "network"(fetch 자체 실패) / "http"(상태 코드) / "page"(페이지 명령이 실패).
    is_block_signal: 사이트가 막았을 때 나는 모양(무응답 · 망 오류 · 429 · 403 · 5xx)인지 - pacing.ApiPacer 가 간격을 늘리는 근거.
    """

    def __init__(self, message: str, status: int = 0, kind: str = "http") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind

    @property
    def is_block_signal(self) -> bool:
        return self.kind in ("timeout", "network", "page") or self.status in (429, 403) or self.status >= 500

    @property
    def is_gone(self) -> bool:
        """상품(자료)이 없는 응답 - 사이트 문제가 아니라 그 항목만 건너뛴다."""
        return self.status in (400, 404)


def on_site(page: Page) -> bool:
    """페이지가 kream.co.kr 문서에 있는지 - 다른 곳(about:blank 등)에서 fetch 하면 CORS 로 막힌다."""
    try:
        return not page.is_closed() and (urlparse(page.url).hostname or "").endswith(SITE_HOST)
    except Exception:  # noqa: BLE001
        return False


def _is_api_request(request: Request) -> bool:
    return request.url.startswith(API_BASE) and "authorization" in request.headers and "notification" not in request.url


class ApiClient:
    """페이지 안에서 fetch 로 KREAM API 를 부른다. 헤더는 사이트가 실제로 보낸 요청에서 복사한다.

    page: fetch 를 실행할 탭, 또는 그 탭을 돌려주는 함수 (탭을 바꿔 쓰는 [재입찰] - 닫힌 탭이면 새 탭을 만들어 두고 함수가 그것을 돌려준다).
    context: 주면 어느 탭이든 사이트가 API 요청을 보낼 때 헤더를 받아 둔다 - 로그인 뒤 목록 페이지를 여는 동안 저절로 잡혀 마이페이지 이동이 필요 없다.
    """

    def __init__(self, page: Page | Callable[[], Page], context: BrowserContext | None = None) -> None:
        self._page = page
        self.headers: dict[str, str] = {}
        self.calls = 0          # 이 클라이언트로 보낸 요청 수 (로그용)
        if context is not None:
            context.on("request", self._sniff)

    @property
    def page(self) -> Page:
        return self._page() if callable(self._page) else self._page

    def _sniff(self, request: Request) -> None:
        """사이트가 보낸 API 요청에서 헤더를 받아 둔다 (컨텍스트의 request 이벤트 - 다른 스레드에서 올 수 있어 dict 를 통째로 바꾼다)."""
        if not self.headers and _is_api_request(request):
            self.headers = {k: v for k, v in request.headers.items()
                            if k.lower().startswith("x-kream") or k.lower() in HEADER_KEEP}
            log.debug("API 헤더 %d개 확보 (사이트 요청에서)", len(self.headers))

    def invalidate(self) -> None:
        """로그인을 다시 했다 - 옛 세션의 헤더를 버린다 (다음 호출이 새로 잡는다)."""
        self.headers = {}

    def capture_headers(self, url: str = INVENTORY_FINISHED_URL) -> None:
        """url 로 이동하면서 사이트가 API 에 보내는 헤더를 잡아 둔다 (페이지 이동 1번 - 저절로 못 잡았을 때만)."""
        try:
            with self.page.expect_request(_is_api_request, timeout=20_000) as req:
                self.page.goto(url, wait_until="domcontentloaded")
            self._sniff(req.value)
        except PlaywrightTimeout as e:
            raise ApiError("KREAM API 요청 헤더를 잡지 못했습니다 (로그인 상태와 페이지를 확인)", kind="page") from e

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
        """paths 를 한 번에 받는다 (PARALLEL_FETCH 개 이하). 항목마다 응답 dict 또는 ApiError.
        인증이 끊긴 항목(401/403)은 헤더를 한 번만 다시 잡고 그 항목들만 한 번 더 받는다."""
        self.calls += len(paths)
        try:
            results = self.page.evaluate(_FETCH_MANY_JS, [[self._url(p) for p in paths], self._request_headers(), TIMEOUT_MS])
        except ApiError as e:
            return [e] * len(paths)
        except Exception as e:  # noqa: BLE001
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
            for i, again in zip(redo, self._fetch([paths[i] for i in redo], retry=False)):
                out[i] = again
        return out

    def get(self, path: str) -> dict:
        """GET 하나. 200 + JSON 객체면 그 객체, 아니면 ApiError."""
        result = self._fetch([path])[0]
        if isinstance(result, ApiError):
            raise result
        return result

    def get_many(self, paths: list[str]) -> list[dict | ApiError]:
        """여러 경로를 PARALLEL_FETCH 개씩 동시에 받는다. 항목마다 응답 dict 또는 ApiError."""
        out: list[dict | ApiError] = []
        for i in range(0, len(paths), PARALLEL_FETCH):
            out.extend(self._fetch(paths[i:i + PARALLEL_FETCH]))
        return out

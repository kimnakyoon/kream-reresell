"""KREAM API 를 로그인된 페이지 안에서 fetch 로 부른다 - [내역] · [재입찰] · [입찰] 공용.

API 는 브라우저 밖에서 부르면 막히지만(요청 서명, 2026-09-04 실측 - curl 은 늘 10초 뒤 500), 페이지 안에서 사이트가 실제로 보낸
요청의 헤더(authorization, x-kream-*)를 그대로 붙여 fetch 하면 된다 (credentials 는 omit 이어야 CORS 를 통과한다).
헤더는 마이페이지(보관 판매 종료 탭)로 이동하면서 사이트가 보내는 요청에서 한 번 복사해 두고, 시각 헤더만 매번 새로 넣는다.

시간 제한: 사이트가 막으면 요청이 10초쯤 응답 없이 붙들렸다 끊긴다 (2026-09-05 실측, pacing 참고). 그래서 fetch 에 TIMEOUT_MS 를 두어
그 상태를 '무응답' (ApiError.kind == "timeout") 으로 바로 알린다 - 부르는 쪽(pacing.ApiPacer)이 차단 신호로 세어 간격을 늘린다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

API_BASE = "https://api.kream.co.kr"
SITE_HOST = "kream.co.kr"
INVENTORY_FINISHED_URL = "https://kream.co.kr/my/inventory?tab=finished"
KST = timezone(timedelta(hours=9))
TIMEOUT_MS = 12_000             # 막히면 10초 홀드 뒤 끊기므로 그보다 조금 길게 - 그 안에 안 오면 무응답으로 본다
PARALLEL_FETCH = 10             # get_many: 한 번에 이만큼 동시에 받는다 (순차보다 5배쯤 빠르다, [내역])

_FETCH_JS = """
async ([url, headers, timeoutMs]) => {
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
}
"""

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
        return (urlparse(page.url).hostname or "").endswith(SITE_HOST)
    except Exception:  # noqa: BLE001
        return False


class ApiClient:
    """페이지 안에서 fetch 로 KREAM API 를 부른다. 헤더는 사이트가 실제로 보낸 요청에서 복사한다."""

    HEADER_KEEP = ("authorization", "accept")

    def __init__(self, page: Page) -> None:
        self.page = page
        self.headers: dict[str, str] = {}
        self.calls = 0          # 이 클라이언트로 보낸 요청 수 (로그용)

    def capture_headers(self, url: str = INVENTORY_FINISHED_URL) -> None:
        """url 로 이동하면서 사이트가 API 에 보내는 헤더(authorization, x-kream-*)를 잡아 둔다 (페이지 이동 1번)."""
        try:
            with self.page.expect_request(
                    lambda r: r.url.startswith(API_BASE) and "authorization" in r.headers
                    and "notification" not in r.url, timeout=20_000) as req:
                self.page.goto(url, wait_until="domcontentloaded")
            headers = req.value.headers
        except PlaywrightTimeout as e:
            raise ApiError("KREAM API 요청 헤더를 잡지 못했습니다 (로그인 상태와 페이지를 확인)", kind="page") from e
        self.headers = {k: v for k, v in headers.items()
                        if k.lower().startswith("x-kream") or k.lower() in self.HEADER_KEEP}
        log.debug("API 헤더 %d개 확보", len(self.headers))

    def _request_headers(self) -> dict[str, str]:
        if not self.headers or not on_site(self.page):
            # 헤더가 없거나 탭이 사이트 밖(새 탭 about:blank 등)에 있으면 마이페이지로 이동하며 다시 잡는다
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
        return ApiError(f"API 응답 오류 {status} ({path}): {res.get('text', '')[:200]}", status=status, kind="http")

    def get(self, path: str, retry: bool = True) -> dict:
        """GET 하나. 200 + JSON 객체면 그 객체, 아니면 ApiError."""
        self.calls += 1
        try:
            res = self.page.evaluate(_FETCH_JS, [self._url(path), self._request_headers(), TIMEOUT_MS])
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"API 호출 실패 ({path}): {e}", kind="page") from e
        if res["status"] in (401, 403) and retry:
            log.info("API 인증이 끊겨 헤더를 다시 잡습니다 (%s)", res["status"])
            self.capture_headers()
            return self.get(path, retry=False)
        if res["status"] != 200 or not isinstance(res.get("body"), dict):
            raise self._error(path, res)
        return res["body"]

    def get_many(self, paths: list[str]) -> list[dict | ApiError]:
        """여러 경로를 PARALLEL_FETCH 개씩 동시에 받는다. 항목마다 응답 dict 또는 ApiError."""
        out: list[dict | ApiError] = []
        for i in range(0, len(paths), PARALLEL_FETCH):
            chunk = paths[i:i + PARALLEL_FETCH]
            self.calls += len(chunk)
            try:
                results = self.page.evaluate(_FETCH_MANY_JS,
                                             [[self._url(p) for p in chunk], self._request_headers(), TIMEOUT_MS])
            except Exception as e:  # noqa: BLE001
                out.extend(ApiError(f"API 호출 실패 ({p}): {e}", kind="page") for p in chunk)
                continue
            for path, res in zip(chunk, results):
                if res["status"] == 200 and isinstance(res.get("body"), dict):
                    out.append(res["body"])
                elif res["status"] in (401, 403):
                    # 토큰이 끊긴 것 - 헤더를 다시 잡고 하나씩 다시 시도
                    self.capture_headers()
                    try:
                        out.append(self.get(path, retry=False))
                    except ApiError as e:
                        out.append(e)
                else:
                    out.append(self._error(path, res))
        return out

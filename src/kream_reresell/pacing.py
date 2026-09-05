"""사이트 스로틀(IP 단위) 을 피하기 위한 요청 줄이기·간격 두기 - [입찰] · [재입찰] · [입찰취소] 공용.

2026-09-05 실측: 체결 내역이 안 불러와지는 것은 사이트 장애가 아니라 **IP 단위 스로틀**이다.
  - 막히면 api.kream.co.kr/api/p/products/{id}/sales|asks|bids|chart 요청만 10초 동안 응답 없이 홀드되다 끊긴다
    (브라우저 net::ERR_FAILED, curl 은 10초 뒤 500). 상품 페이지·다른 API 는 그 순간에도 정상.
  - 브라우저에서 쿠키·로그인 토큰·기기 ID 를 다 빼고 보내도 똑같이 막힘 → 계정이 아니라 IP(또는 브라우저) 기준. 다른 IP 에선 잘 나옴.
  - 429 나 Retry-After 같은 힌트는 없다 (server: nfront).
  - 호출 수 (실측, 상품 842180): 패널 열기 = sales·asks·bids·chart 각 2건 (8건), 옵션 하나 고르기 = 4건
    (/api/p/products/{id}/{옵션}/…), 구매하기 모달·구매 페이지 = 스로틀 대상 없음 (preview_bid·options/display 등).
  - 트리거: 옵션 상품을 0.6초 간격으로 옵션마다 읽던 [입찰] 실행이 상품 13개·옵션 87개(6.5분, 약 450건 = 분당 70건) 뒤 막혔다.
    [재입찰]의 5분 주기 27건 빠른 확인(구매 페이지만, 스로틀 대상 호출 없음)은 3시간 동안 괜찮았다.
  - 막힌 뒤에는 curl 이 항상 10초 뒤 500 을 주므로(브라우저 밖 요청은 원래 그렇다) curl 로는 풀렸는지 알 수 없다 - 브라우저로 봐야 한다.
  - 한 번 막혔다 풀린 날은 훨씬 적은 양(10분에 110건쯤, 분당 11건)으로 다시 막혔다 (2026-09-05 22:54~23:10). 풀린 뒤라고 안심할 수 없다.
  - 스로틀 중에는 모든 옵션 표가 '체결된 거래가 아직 없습니다' 로 그려질 수 있다 - 거래 수가 있는 상품이면 판단 불가로 본다
    (product._await_sales_table). 0건으로 세면 [재입찰]이 입찰을 지운다.

대응 세 가지 (사용자 결정, 2026-09-05):
  1. 프로그램이 안 보는 asks·bids·chart 요청을 서버에 보내지 않고 빈 응답으로 채운다 (browser.watch_api_requests) - 호출이 1/4 로 준다.
  2. 옵션 사이·상품 사이에 사람 속도의 무작위 간격을 둔다 (OPTION_PAUSE_SEC, PRODUCT_PAUSE_SEC).
  3. 스로틀 대상 API 로 나간 요청을 세서 창(WINDOW_SEC) 안에 LIMIT 을 넘기면 알아서 쉰다 (RequestBudget).
  4. 옵션 상품은 '모든 옵션' 표(첫 페이지는 공짜)에서 정해지는 옵션을 먼저 거르고 남은 옵션만 하나씩 고른다
     (product.count_sales_by_option) - 거래가 적은 상품은 옵션 요청이 0건이 된다.
  (패널을 열 때 사이트가 페이지 로드 때와 똑같은 sales 요청을 한 번 더 보내는데, 이를 브라우저에서 캐시해 돌려주는 것은
   실패했다 - browser._api_route 참고. 그래서 패널 열기는 sales 2건이다.)
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

API_ORIGIN = "https://api.kream.co.kr"
# 스로틀 대상 API (실측). 상품 페이지를 열면 sales·asks·bids·chart 네 개가 두 번(8건) 나가고, 패널에서 옵션을 고를 때마다
# /api/p/products/{id}/{옵션}/sales|asks|bids|chart 네 개가 또 나간다 (2026-09-05 실측)
THROTTLED_PATH_RE = re.compile(r"^/api/p/products/\d+/(?:[^/]+/)?(sales|asks|bids|chart)(/|$)")
# 프로그램이 전혀 안 보는 것 - 서버에 보내지 않고 빈 목록으로 채운다. 요청을 abort 하면 패널이 '불러오는 중 문제' 오류로
# 바뀌지만, 200 + 아래 본문으로 채우면 체결 표·옵션 선택이 정상이다 (실측: {} 는 안 되고 items 가 있어야 한다)
TRIMMABLE_PATH_RE = re.compile(r"^/api/p/products/\d+/(?:[^/]+/)?(asks|bids|chart)(/|$)")
TRIM_BODY = '{"items": [], "cursor": null}'

WINDOW_SEC = 600            # 요청 예산 창 (10분)
DEFAULT_LIMIT = 60          # 창 안에 스로틀 대상 요청을 이만큼까지만 보낸다. .env API_BUDGET_PER_10MIN 으로 바꾼다.
                            # 실측: 처음 막힌 실행은 분당 70건쯤, 한 번 막힌 뒤에는 10분에 110건쯤(분당 11건)에서 또 막혔다.
                            # 그 절반. 옵션 상품은 모든 옵션 표에서 정해지면 1~2건, 거래가 많은 상품은 8건쯤 든다
OPTION_PAUSE_SEC = (2.0, 3.5)    # 옵션 하나를 읽고 다음 옵션을 고르기 전 (예전엔 0.6초 - 그게 걸렸다)
PRODUCT_PAUSE_SEC = (3.0, 6.0)   # 상품 하나를 끝내고 다음 상품 페이지를 열기 전
PAGE_PAUSE_SEC = (1.5, 2.5)      # 모든 옵션 표를 한 페이지 더 넘기기 전 (사람이 스크롤하는 속도)


class RequestBudget:
    """스로틀 대상 요청의 이동 창 예산. 브라우저 컨텍스트의 request 이벤트에서 note() 로 세고, 요청을 일으키는 동작 전에
    wait_for_room() 으로 자리가 날 때까지 쉰다. 스레드 안전 (Playwright 이벤트는 다른 스레드에서 올 수 있다)."""

    def __init__(self, limit: int = DEFAULT_LIMIT, window_sec: float = WINDOW_SEC) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self._times: deque[float] = deque()
        self._lock = threading.Lock()
        self.total = 0

    def _prune(self, now: float) -> None:
        while self._times and now - self._times[0] > self.window_sec:
            self._times.popleft()

    def note(self, path: str) -> None:
        if not THROTTLED_PATH_RE.match(path):
            return
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._times.append(now)
            self.total += 1

    def used(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return len(self._times)

    def seconds_until_room(self, need: int = 1) -> float:
        """need 개를 더 보내도 한도 안이 되려면 얼마나 기다려야 하는지 (0 이면 지금 보내도 됨)."""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            over = len(self._times) + need - self.limit
            if over <= 0:
                return 0.0
            # 가장 오래된 over 개가 창 밖으로 나가는 시각까지
            return max(0.0, self._times[over - 1] + self.window_sec - now)

    def wait_for_room(self, should_stop: Callable[[], bool] | None = None, need: int = 1,
                      on_status: Callable[[str], None] | None = None) -> bool:
        """자리가 날 때까지 쉰다. 중지 요청이면 False. 한도가 0 이하면(끔) 바로 True."""
        if self.limit <= 0:
            return True
        wait = self.seconds_until_room(need)
        if wait <= 0:
            return True
        log.info("요청 예산 소진 (%d분에 %d건) - %d초 쉼", int(self.window_sec // 60), self.limit, int(wait) + 1)
        if on_status:
            on_status(f"사이트 차단 방지: 요청 예산({int(self.window_sec // 60)}분 {self.limit}건) 소진 - {int(wait) + 1}초 쉬는 중")
        deadline = time.monotonic() + wait + 0.5
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return False
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        return True


# 실행 하나가 쓰는 예산 (browser.real_chrome_context 가 컨텍스트에 붙인다)
BUDGET = RequestBudget()


def configure(limit: int) -> None:
    BUDGET.limit = limit


def pause(range_sec: tuple[float, float], should_stop: Callable[[], bool] | None = None) -> None:
    """무작위 간격만큼 쉰다 (중지 요청을 1초마다 본다)."""
    deadline = time.monotonic() + random.uniform(*range_sec)
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            return
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))


def before_sales_request(should_stop: Callable[[], bool] | None = None, need: int = 1,
                         on_status: Callable[[str], None] | None = None) -> bool:
    """스로틀 대상 요청을 일으키는 동작(패널 열기·옵션 고르기·구매 페이지 열기) 직전에 부른다."""
    return BUDGET.wait_for_room(should_stop, need, on_status)

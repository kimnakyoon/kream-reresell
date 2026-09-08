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
  - 2026-09-07 실측: 스로틀 대상 요청이 0건이어도 페이지를 계속 열면 막힌다. [재입찰]이 입찰마다 상품 페이지·구매 페이지를 열며
    분당 8~9건(페이지 이동 17번쯤)으로 36분 돌고, 6분 쉬고, 다시 27분 돌자 구매 페이지 본문(API 로 채우는 부분)이 3건 연달아
    비었고 5분 쉬니 풀렸다. 사용자 판단: 정해진 '응답 안 주는 시간대' 는 없고 계속 접속해서 막히는 것.

대응 (사용자 결정, 2026-09-05 / 접속 예산은 2026-09-07):
  1. 프로그램이 안 보는 asks·bids·chart 요청을 서버에 보내지 않고 빈 응답으로 채운다 (browser.watch_api_requests) - 호출이 1/4 로 준다.
  2. 옵션 사이·상품 사이에 사람 속도의 무작위 간격을 둔다 (OPTION_PAUSE_SEC, PRODUCT_PAUSE_SEC).
  3. 스로틀 대상 API 로 나간 요청을 세서 창(WINDOW_SEC) 안에 LIMIT 을 넘기면 알아서 쉰다 (RequestBudget).
  4. 옵션 상품은 '모든 옵션' 표(첫 페이지는 공짜)에서 정해지는 옵션을 먼저 거르고 남은 옵션만 하나씩 고른다
     (product.count_sales_by_option) - 거래가 적은 상품은 옵션 요청이 0건이 된다.
  5. 접속 예산: kream.co.kr 메인 프레임의 페이지 이동(주소 안 이동 포함 - 상품 페이지, 구매 페이지, 변경 화면, 재시도, 확인 페이지 전부)을
     브라우저 층에서 세서(browser.watch_page_visits → PAGE_BUDGET. 사이트가 같은 주소로 되풀이하는 replaceState 는 안 센다 -
     2026-09-08 실측: 그걸 세면 입찰 하나가 10번쯤으로 세져 상품 8개 만에 예산이 끝나 525초씩 쉬었다) 창 안에 PAGE_LIMIT 을 넘기면 상품(입찰) 하나를 보기 전에 쉰다
     (before_product). 막혔다 풀리면(sitewait) 남은 실행은 한도를 반으로 줄인다 - 한 번 막힌 뒤에는 더 적은 양으로 또 막힌다.
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
# 접속 예산 (위 실측 2026-09-07, 대응 5): 10분 창 안의 페이지 이동 수. .env PAGE_BUDGET_PER_10MIN 으로 바꾼다.
# 막혔던 속도(10분에 170번쯤)의 절반 아래. 입찰(상품) 하나에 상품 페이지 + 구매 페이지 = 2번이 기본이고 밀린 입찰은 더 든다
DEFAULT_PAGE_LIMIT = 80
VISITS_PER_PRODUCT = 2      # 상품(입찰) 하나를 보기 전에 이만큼 자리가 있어야 시작한다
TIGHTEN_MIN_LIMIT = 10      # 막힌 뒤 예산을 반으로 줄일 때의 하한
DEFAULT_LIMIT = 60          # 창 안에 스로틀 대상 요청을 이만큼까지만 보낸다. .env API_BUDGET_PER_10MIN 으로 바꾼다.
                            # 실측: 처음 막힌 실행은 분당 70건쯤, 한 번 막힌 뒤에는 10분에 110건쯤(분당 11건)에서 또 막혔다.
                            # 그 절반. 옵션 상품은 모든 옵션 표에서 정해지면 1~2건, 거래가 많은 상품은 8건쯤 든다
OPTION_PAUSE_SEC = (2.0, 3.5)    # 옵션 하나를 읽고 다음 옵션을 고르기 전 (예전엔 0.6초 - 그게 걸렸다)
PRODUCT_PAUSE_SEC = (3.0, 6.0)   # 상품 하나를 끝내고 다음 상품 페이지를 열기 전
PAGE_PAUSE_SEC = (1.5, 2.5)      # 모든 옵션 표를 한 페이지 더 넘기기 전 (사람이 스크롤하는 속도)


class RequestBudget:
    """스로틀 대상 요청의 이동 창 예산. 브라우저 컨텍스트의 request 이벤트에서 note() 로 세고, 요청을 일으키는 동작 전에
    wait_for_room() 으로 자리가 날 때까지 쉰다. 스레드 안전 (Playwright 이벤트는 다른 스레드에서 올 수 있다)."""

    def __init__(self, limit: int = DEFAULT_LIMIT, window_sec: float = WINDOW_SEC, what: str = "요청") -> None:
        self.limit = limit
        self.window_sec = window_sec
        self.what = what            # 로그·상태창에 쓰는 이름 (요청 / 접속)
        self._times: deque[float] = deque()
        self._lock = threading.Lock()
        self.total = 0

    @property
    def window_min(self) -> int:
        return int(self.window_sec // 60)

    def _span(self) -> str:
        return f"{self.window_min}분 {self.limit}건"

    def _prune(self, now: float) -> None:
        while self._times and now - self._times[0] > self.window_sec:
            self._times.popleft()

    def note(self, path: str) -> None:
        """스로틀 대상 API 요청이면 하나 센다."""
        if THROTTLED_PATH_RE.match(path):
            self.count()

    def count(self) -> None:
        """하나 센다."""
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
        log.info("%s 예산(%s) 소진 - %d초 쉼", self.what, self._span(), int(wait) + 1)
        if on_status:
            on_status(f"사이트 차단 방지: {self.what} 예산({self._span()}) 소진 - {int(wait) + 1}초 쉬는 중")
        deadline = time.monotonic() + wait + 0.5
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return False
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        return True

    def tighten(self) -> None:
        """막혔다 풀린 뒤 부른다 - 남은 실행 동안 한도를 반으로 줄인다 (configure 가 다음 실행에 되돌린다)."""
        if self.limit <= 0:
            return
        self.limit = max(TIGHTEN_MIN_LIMIT, self.limit // 2)
        log.warning("막혔다 풀린 뒤라 %s 예산을 %s으로 줄임", self.what, self._span())


# 실행 하나가 쓰는 예산. 둘 다 브라우저 층에서 센다 (browser.watch_api_requests / watch_page_visits) - 프로세스 안에서 [입찰] · [재입찰] 을
# 이어 돌면 창 안의 기록은 그대로 이어진다
BUDGET = RequestBudget()
PAGE_BUDGET = RequestBudget(limit=DEFAULT_PAGE_LIMIT, what="접속")


def configure(limit: int, page_limit: int) -> None:
    """실행 시작마다 (Settings.validate) 한도를 설정값으로 둔다 - tighten 으로 줄였던 것도 되돌린다. 창 안의 기록은 남긴다."""
    BUDGET.limit = limit
    PAGE_BUDGET.limit = page_limit


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


def before_product(should_stop: Callable[[], bool] | None = None,
                   on_status: Callable[[str], None] | None = None) -> bool:
    """상품(입찰) 하나를 보기 직전에 부른다 - 접속 예산에 페이지 이동 VISITS_PER_PRODUCT 번 자리가 날 때까지 쉰다. 중지 요청이면 False."""
    return PAGE_BUDGET.wait_for_room(should_stop, VISITS_PER_PRODUCT, on_status)

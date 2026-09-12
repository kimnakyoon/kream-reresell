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
     (before_product → RequestBudget.pace). 예산은 창 안에 고르게 나눠 쓴다 - 이동 하나에 창/한도 초(기본 6초)씩, 직전 상품이 실제로 쓴
     이동 수만큼 간격을 두고 다음 상품을 시작해(보통 2번 = 12초) 몰아 쓰고 몇 분씩 쉬는 일이 없게 (2026-09-08 11:58 실측: 한도 80 에서
     35건을 4.7분에 몰아 보고 315초 쉼 - 사용자: 쉬는 시간이 너무 길다, 최대로 줄여 달라). 고정 12초 간격으로 하면 상품 하나가 평균 2.2번
     이동해 창이 10% 넘쳐 50건마다 1분쯤 쉬고, 실제 이동 수 기준이면 215건이 49분에 몇 분짜리 쉼 없음 (가짜 시계 시뮬레이션).
     한도 100 은 2026-09-07 실측에서 안 막혔던 '한 시간 600번' 수준 (막힐 때는 직전 1시간 900번쯤) - 더 올리면 막힐 수 있다.
     막혔다 풀리면(sitewait) 남은 실행은 한도를 반으로 줄인다 - 한 번 막힌 뒤에는 더 적은 양으로 또 막힌다.
  (패널을 열 때 사이트가 페이지 로드 때와 똑같은 sales 요청을 한 번 더 보내는데, 이를 브라우저에서 캐시해 돌려주는 것은
   실패했다 - browser._api_route 참고. 그래서 패널 열기는 sales 2건이다.)
  6. 시세 API 틱 (2026-09-13, No1 Seller Center 조사 뒤 사용자 결정): A·B 는 페이지를 열지 않고 상품 상세 API 한 번(market.fetch_market)으로
     읽는다. 그 호출은 고정 간격(ApiPacer, 기본 6초)으로 하나씩 보내고, 분당 상한(20건)을 넘지 않으며, 차단 신호(무응답·5xx 등)를 맞으면
     간격을 두 배로 늘렸다가 조용해지면 되돌린다. 근거: 시간당 페이지 이동 600번(이동 하나가 API 수십 건)이 안 막혔고, No1 이 같은 호출을
     시간당 200~230건씩 5일 연속 이 PC 에서 보내는 동안 우리 실행도 같이 돌았는데 막힘이 없었다. 3초(시간당 1,200건)는 실측 밖이라 하한을 3초로 둔다.
     [재입찰]은 이 틱이 곧 속도다 (밀리지 않은 입찰 = 호출 1건, 페이지 이동 0번). [입찰]은 이 호출로 가격을 먼저 걸러 체결 내역 조회를 줄인다.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque
from collections.abc import Callable

from . import hangwatch

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
# 접속 예산 (대응 5): 10분 창 안의 페이지 이동 수. .env PAGE_BUDGET_PER_10MIN 으로 바꾼다. 실측이 허용하는 최대 (머리글) - 더 올리면 막힐 수 있다
DEFAULT_PAGE_LIMIT = 100
VISITS_PER_PRODUCT = 2      # 상품(입찰) 하나가 기본으로 쓰는 이동 수 (상품 페이지 + 구매 페이지). 고른 간격과 자리 확인의 여유분
TIGHTEN_MIN_LIMIT = 10      # 막힌 뒤 예산을 반으로 줄일 때의 하한
ANNOUNCE_SEC = 20           # pace 의 쉼이 이만큼 이상일 때만 로그·상태창에 알린다 (정상 상태의 몇 초짜리 쉼은 조용히)
DEFAULT_LIMIT = 60          # 창 안에 스로틀 대상 요청을 이만큼까지만 보낸다. .env API_BUDGET_PER_10MIN 으로 바꾼다.
                            # 실측: 처음 막힌 실행은 분당 70건쯤, 한 번 막힌 뒤에는 10분에 110건쯤(분당 11건)에서 또 막혔다.
                            # 그 절반. 옵션 상품은 모든 옵션 표에서 정해지면 1~2건, 거래가 많은 상품은 8건쯤 든다
OPTION_PAUSE_SEC = (2.0, 3.5)    # 옵션 하나를 읽고 다음 옵션을 고르기 전 (예전엔 0.6초 - 그게 걸렸다)
PRODUCT_PAUSE_SEC = (3.0, 6.0)   # 상품 하나를 끝내고 다음 상품 페이지를 열기 전. [입찰]만 - 상품 하나가 12초를 넘겨 접속 예산의 고른 간격
                                 # (before_product) 밖으로 나가는 게 보통이라 그 뒤에 사람 속도 간격으로 더해진다. [재입찰]은 간격 안에 흡수돼 없앰
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
        self._paced_at = 0.0    # pace 가 직전 동작을 시작시킨 시각 (monotonic) 과 그때의 total - 그 뒤 늘어난 만큼이 직전 동작이 쓴 개수
        self._paced_total = 0

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
        self._announce_wait(wait, on_status)
        return sleep_with_stop(wait + 0.5, should_stop)

    def pace(self, should_stop: Callable[[], bool] | None = None, need: int = 1,
             on_status: Callable[[str], None] | None = None) -> bool:
        """need 개쯤 쓰는 동작(상품 하나) 을 시작하기 직전에 부른다 - 예산을 창 안에 고르게 나눠 쓴다 (머리글 대응 5).

        직전 동작을 시작한 뒤 그 동작이 실제로 쓴 개수 × 창/(한도 - need) 초 (±5% 무작위) 가 지나야 시작한다. 이 간격이 한도를
        조금 밑돌게 잡혀 있어 정상 상태에서는 자리가 늘 있고 쉼도 몇 초라 조용히 쉰다. 자리가 없거나(다른 실행이 몰아 쓴 직후·재시도가
        몰린 뒤) 직전 동작이 많이 써서 ANNOUNCE_SEC 넘게 쉴 때만 로그·상태창에 알린다. 한도가 0 이하면 바로 True. 중지 요청이면 False.
        """
        if self.limit <= 0:
            return True
        total = self.total
        gap = (total - self._paced_total) * self.window_sec / max(1, self.limit - need) * random.uniform(0.95, 1.05)
        room = self.seconds_until_room(need)
        wait = max(self._paced_at + gap - time.monotonic(), room + 0.5 if room > 0 else 0.0)
        if wait >= ANNOUNCE_SEC:
            self._announce_wait(wait, on_status)
        ok = sleep_with_stop(wait, should_stop)     # wait 가 0 이하면 바로 True
        self._paced_at = time.monotonic()
        self._paced_total = total
        return ok

    def _announce_wait(self, wait: float, on_status: Callable[[str], None] | None) -> None:
        log.info("%s 예산(%s)에 맞춰 %d초 쉼", self.what, self._span(), int(wait) + 1)
        if on_status:
            on_status(f"사이트 차단 방지: {self.what} 예산({self._span()})에 맞춰 {int(wait) + 1}초 쉬는 중")

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


# ---------------------------------------------------------------- 시세 API 틱 (대응 6)

API_TICK_SEC = 6.0          # 시세 API(market) 호출 사이의 기본 간격. .env API_TICK_SEC / GUI '시세 조회 간격' 으로 바꾼다
API_TICK_MIN_SEC = 3.0      # 이보다 짧게는 못 잡는다 - 시간당 1,200건은 어떤 실측도 넘어서는 값 (2026-09-13)
API_TICK_MAX_SEC = 60.0     # 차단 신호로 늘려도 이보다 길어지지 않는다 (No1 Seller Center 의 상한과 같음)
API_MAX_PER_MINUTE = 20     # 어떤 경우에도 시세 API 를 1분에 이만큼 넘게 보내지 않는다 (틱 3초 = 분당 20건이 딱 상한)
API_CALM_TICKS = 3          # 차단 신호 뒤 이만큼 연달아 정상이면 간격을 한 계단 되돌린다
API_STEP = 2.0              # 차단 신호 하나에 간격을 이 배수로 늘리고, 조용하면 같은 배수로 되돌린다


class ApiPacer:
    """시세 API 호출의 고정 틱 + 안전장치 - No1 Seller Center 의 자동 경쟁 페이스 조절을 본뜬 것 (2026-09-13 조사).

    - 호출 사이에 tick 초를 지킨다 (직전 호출 시작 시각 기준 - 호출에 걸린 시간은 빠진다). 상품(입찰) 하나 = 호출 하나라 이 값이 곧 속도다.
    - 1분에 API_MAX_PER_MINUTE 건을 넘기지 않는다 (설정을 잘못 넣거나 재시도가 몰려도 이 위로는 못 간다).
    - 차단 신호(무응답 · 망 오류 · 429 · 403 · 5xx, api.ApiError.is_block_signal)를 맞으면 간격을 API_STEP 배로 늘린다 (최대 API_TICK_MAX_SEC).
      연달아 API_CALM_TICKS 번 정상이면 한 계단 되돌리고, 설정값까지 내려간다. 몇 분씩 멈추는 대신 한 계단씩 물러났다 돌아오는 방식이다.
    - 연달아 몇 건이나 막혔는지(streak)는 부르는 쪽이 보고 sitewait(5분마다 확인)로 넘어간다 - 여기서는 간격만 다룬다.
    스레드 안전하지 않다 (작업 스레드 하나에서만 쓴다).
    """

    def __init__(self, tick_sec: float = API_TICK_SEC) -> None:
        self.configured = tick_sec
        self.current = tick_sec
        self.calm = 0
        self.streak = 0
        self.total = 0
        self.blocks = 0
        self._last_started = 0.0
        self._minute: deque[float] = deque()

    def configure(self, tick_sec: float) -> None:
        """실행 시작마다 (Settings.validate) 틱을 설정값으로 둔다. 늘어나 있던 간격도 되돌린다."""
        self.configured = self.current = max(API_TICK_MIN_SEC, min(API_TICK_MAX_SEC, tick_sec))
        self.calm = self.streak = 0

    def describe(self) -> str:
        return f"틱 {self.current:g}초" + (f" (설정 {self.configured:g}초, 차단 신호로 늘림)" if self.current > self.configured else "")

    def _minute_room(self, now: float) -> float:
        while self._minute and now - self._minute[0] > 60:
            self._minute.popleft()
        if len(self._minute) < API_MAX_PER_MINUTE:
            return 0.0
        return max(0.0, self._minute[0] + 60 - now)

    def wait_turn(self, should_stop: Callable[[], bool] | None = None,
                  on_status: Callable[[str], None] | None = None) -> bool:
        """다음 시세 API 호출 직전에 부른다 - 틱과 분당 상한을 지켜 쉰다. 중지 요청이면 False."""
        now = time.monotonic()
        wait = max(self._last_started + self.current - now, self._minute_room(now))
        if wait >= ANNOUNCE_SEC:
            log.info("시세 API %s에 맞춰 %d초 쉼", self.describe(), int(wait) + 1)
            if on_status:
                on_status(f"사이트 차단 방지: 시세 조회 {self.describe()}에 맞춰 {int(wait) + 1}초 쉬는 중")
        ok = sleep_with_stop(wait, should_stop)
        now = time.monotonic()
        self._last_started = now
        self._minute.append(now)
        self.total += 1
        return ok

    def report_ok(self) -> None:
        """호출이 정상으로 끝났다. 늘어나 있던 간격은 조용한 틱 API_CALM_TICKS 번마다 한 계단 되돌린다."""
        self.streak = 0
        if self.current <= self.configured:
            self.calm = 0
            return
        self.calm += 1
        if self.calm >= API_CALM_TICKS:
            self.calm = 0
            self.current = max(self.configured, self.current / API_STEP)
            log.info("시세 API %d번 연달아 정상 - 간격을 %g초로 되돌림 (설정 %g초)", API_CALM_TICKS, self.current, self.configured)

    def report_block(self, why: str) -> int:
        """차단 신호를 맞았다 - 간격을 한 계단 늘린다. 연달아 몇 번째인지 돌려준다 (부르는 쪽이 sitewait 판단)."""
        self.streak += 1
        self.blocks += 1
        self.calm = 0
        before = self.current
        self.current = min(API_TICK_MAX_SEC, self.current * API_STEP)
        log.warning("시세 API 차단 신호 (%d번 연달아): %s - 간격 %g초 → %g초", self.streak, why, before, self.current)
        return self.streak

    def reset_streak(self) -> None:
        """쉬었다 돌아온 뒤 (sitewait) 연달아 센 수만 지운다. 간격은 늘어난 채로 두고 정상 틱이 쌓이면 되돌린다."""
        self.streak = 0


API_PACER = ApiPacer()


def configure(limit: int, page_limit: int, tick_sec: float = API_TICK_SEC) -> None:
    """실행 시작마다 (Settings.validate) 한도를 설정값으로 둔다 - tighten 으로 줄였던 것도 되돌린다. 창 안의 기록은 남긴다."""
    BUDGET.limit = limit
    PAGE_BUDGET.limit = page_limit
    API_PACER.configure(tick_sec)


def sleep_with_stop(seconds: float, should_stop: Callable[[], bool] | None = None) -> bool:
    """중지 요청을 1초마다 보며 쉰다 (0 이하면 바로 돌아온다). 중지 요청이면 False. 기다리는 곳 공용 ([입찰]·[재입찰]·sitewait).

    쉬는 동안은 멈춤 감시(hangwatch)를 쉬게 한다 - Playwright 호출 밖에서 자면 남아 있던 회신이 멈춘 것처럼 보여 멀쩡한 탭을 닫았다.
    """
    deadline = time.monotonic() + seconds
    with hangwatch.idle():
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return False
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
    return True


def pause(range_sec: tuple[float, float], should_stop: Callable[[], bool] | None = None) -> None:
    """무작위 간격만큼 쉰다 (중지 요청을 1초마다 본다)."""
    sleep_with_stop(random.uniform(*range_sec), should_stop)


def before_sales_request(should_stop: Callable[[], bool] | None = None, need: int = 1,
                         on_status: Callable[[str], None] | None = None) -> bool:
    """스로틀 대상 요청을 일으키는 동작(패널 열기·옵션 고르기·구매 페이지 열기) 직전에 부른다."""
    return BUDGET.wait_for_room(should_stop, need, on_status)


def before_product(should_stop: Callable[[], bool] | None = None,
                   on_status: Callable[[str], None] | None = None) -> bool:
    """상품(입찰) 하나를 보기 직전에 부른다 - 접속 예산을 고르게 나눈 간격을 지키고, 자리가 없으면 날 때까지 쉰다 (대응 5). 중지 요청이면 False."""
    return PAGE_BUDGET.pace(should_stop, VISITS_PER_PRODUCT, on_status)

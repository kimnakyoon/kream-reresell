"""사이트가 API 데이터(체결 내역 · 즉시 판매가)를 안 주는 시간대 대응 - [입찰] · [재입찰] 공용.

2026-09-05 실측: 상품마다 옵션 6~9개를 0.6초 간격으로 읽다 보면 어느 순간부터 패널이 비어 나오고 (몇 초 뒤
'불러오는 중 문제가 생겼어요 / 다시 시도' 오류 표시), 계속 두드리는 동안은 20분 넘게 이어지다가 7분쯤 쉬면 풀렸다.
그래서 판단 불가·오류가 TROUBLE_STREAK 건 연달아 나면 상품을 더 열지 않고 멈춘 채, PROBE_SEC 마다 한 번씩만
확인해 보고 다시 주기 시작하면 이어서 본다 (사용자 결정, 2026-09-05: 포기하고 끝내지 않고 풀릴 때까지 기다린다).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

TROUBLE_STREAK = 3     # 판단 불가(내역을 못 불러옴)·오류가 연달아 이만큼 나면 사이트가 응답을 안 주는 것으로 본다
PROBE_SEC = 120        # 멈춘 동안 이만큼마다 한 번 확인한다 (더 자주 두드리면 풀리지 않는다)


def sleep_with_stop(should_stop: Callable[[], bool], seconds: float) -> None:
    """중지 요청을 1초마다 보며 쉰다."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop():
            return
        time.sleep(min(1.0, deadline - time.monotonic()))


def wait_until_site_back(probe: Callable[[], bool], should_stop: Callable[[], bool],
                         on_status: Callable[[str], None], what: str = "거래 내역") -> bool:
    """사이트가 다시 줄 때까지 기다린다. PROBE_SEC 마다 probe() 를 불러 True 가 나오면 돌아온다 (True).

    시간 제한은 없다 - 중지 요청이 올 때까지 기다리고, 중지 요청이면 False. probe 가 예외를 내면 아직 안 풀린 것으로 본다.
    """
    started = time.monotonic()
    tries = 0
    while not should_stop():
        waited = int(time.monotonic() - started)
        on_status(f"사이트가 {what}을 주지 않아 멈춤 - {waited // 60}분 {waited % 60}초 쉬는 중 "
                  f"({PROBE_SEC // 60}분마다 확인, 다시 주면 이어서 봄)")
        sleep_with_stop(should_stop, PROBE_SEC)
        if should_stop():
            break
        tries += 1
        try:
            ok = probe()
        except Exception as e:  # noqa: BLE001
            ok = False
            log.info("아직 %s을 주지 않음 (%d번째 확인, %d분 지남): %s", what, tries,
                     int(time.monotonic() - started) // 60, str(e).splitlines()[0])
        else:
            if not ok:
                log.info("아직 %s을 주지 않음 (%d번째 확인, %d분 지남)", what, tries, int(time.monotonic() - started) // 60)
        if ok:
            log.info("사이트가 %s을 다시 줌 (%d분 멈췄음) - 이어서 봄", what, int(time.monotonic() - started) // 60)
            return True
    log.info("사용자 요청으로 중지 - %s을 기다리던 중", what)
    return False

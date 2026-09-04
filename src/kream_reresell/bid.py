"""구매 페이지에서 구매 입찰을 넣는다.

흐름: 구매 입찰 탭 -> 희망가 입력 -> 마감기한 -> 구매 입찰 계속
      -> 배송방법 '창고 보관' -> 포인트 최대 사용 -> 입찰하기 -> 확인 3항목 체크 -> 입찰하기
      -> '구매 입찰이 완료되었습니다' 확인.

'창고 보관' 이 선택된 것을 확인하지 못하면 어떤 경우에도 입찰하지 않는다.
"""

from __future__ import annotations

import logging
import re

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from .config import Settings
from .debug import dump

log = logging.getLogger(__name__)


# 마지막 '입찰하기' 뒤의 완료 문구. 새 입찰은 '구매 입찰이 완료되었습니다', 입찰 변경([입찰 변경하기] 로 희망가를 올린 경우)은
# 같은 화면을 쓰므로 '변경' 표현도 받아 준다.
COMPLETED_RE = re.compile(r"구매 입찰이 (완료|변경)|입찰 변경이 완료")


class BidAborted(Exception):
    """안전장치에 걸려 입찰을 중단했다."""


class StoppedBeforeSubmit(Exception):
    """--stop-before-submit 로 마지막 버튼 직전에 멈췄다."""


class BidUncertain(Exception):
    """마지막 '입찰하기' 를 눌렀는데 완료 문구를 확인하지 못했다 - 입찰됐을 수 있다."""


def fill_bid_form(page: Page, price: int, bid_days: int, settings: Settings, pid: int) -> None:
    page.get_by_role("link", name="구매 입찰").first.click()
    page.wait_for_timeout(800)
    box = page.get_by_placeholder("희망가 입력").first
    box.click()
    box.fill(str(price))
    page.wait_for_timeout(500)
    page.get_by_role("link", name=f"{bid_days}일", exact=True).first.click()
    page.wait_for_timeout(500)

    body = page.locator("body").inner_text()
    if not re.search(rf"{bid_days}일\s*\(", body):
        raise BidAborted(f"마감기한 {bid_days}일이 선택되지 않음")
    shown = page.get_by_placeholder("희망가 입력").first.input_value().replace(",", "")
    if shown != str(price):
        raise BidAborted(f"희망가 입력값이 다름: {shown!r} != {price}")
    if settings.inspect:
        dump(page, f"{pid}_2_bid_form")

    cont = page.get_by_role("button", name="구매 입찰 계속").first
    if cont.is_disabled():
        raise BidAborted("'구매 입찰 계속' 버튼이 비활성 (입력값 확인 필요)")
    cont.click()
    page.wait_for_timeout(2500)
    if settings.inspect:
        dump(page, f"{pid}_3_after_continue")


def choose_warehouse_and_points(page: Page, settings: Settings, pid: int) -> None:
    """배송방법 '창고보관' 선택 (필수 확인) + 포인트 최대 사용.

    배송/결제 화면의 배송방법은 .select_radio[data-sdui-id=keep](창고보관) /
    [data-sdui-id=normal](일반배송). input 이 없고 SVG 색(#222222 = 선택, #22222233 = 미선택)
    으로만 표시된다.
    """
    keep = page.locator(WAREHOUSE_RADIO).first
    try:
        keep.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeout as e:
        dump(page, f"{pid}_no_warehouse_option")
        raise BidAborted("'창고보관' 선택지가 화면에 없음") from e

    keep.locator(".radio-element").first.click()
    page.wait_for_timeout(800)
    if not page.evaluate(_WAREHOUSE_SELECTED_JS):
        dump(page, f"{pid}_warehouse_not_selected")
        raise BidAborted("'창고보관' 이 선택된 것을 확인하지 못함")
    log.info("배송방법: 창고보관 선택 확인")

    max_use = page.get_by_role("button", name=re.compile(r"최대\s*사용")).first
    if max_use.count() and max_use.is_visible() and not max_use.is_disabled():
        max_use.click()
        page.wait_for_timeout(800)
        log.info("포인트 최대 사용 클릭")
        # "포인트 자동 사용 - 거래 체결 시점에 최대 포인트로 자동 사용" 확인창 -> 계속 진행
        dialog = page.get_by_role("dialog", name=re.compile(r"포인트 자동 사용")).first
        if dialog.count() and dialog.is_visible():
            dialog.get_by_role("button", name="계속 진행").first.click()
            page.wait_for_timeout(800)
            log.info("포인트 자동 사용 확인창: 계속 진행")
    if settings.inspect:
        dump(page, f"{pid}_4_before_submit")


WAREHOUSE_RADIO = ".select_radio[data-sdui-id='keep']"
NORMAL_RADIO = ".select_radio[data-sdui-id='normal']"


def submit_bid(page: Page, price: int, settings: Settings, pid: int) -> None:
    """입찰하기 -> '구매조건 확인 및 거래진행 동의' 모달의 [필수] 3항목 체크 -> 입찰하기."""
    first = page.get_by_role("button", name=re.compile(r"입찰하기")).first
    first.wait_for(state="visible", timeout=10_000)
    if first.is_disabled():
        raise BidAborted("'입찰하기' 버튼이 비활성")
    if not page.evaluate(_WAREHOUSE_SELECTED_JS):
        raise BidAborted("입찰 직전 재확인: '창고보관' 이 선택돼 있지 않음")
    body = page.locator("body").inner_text()
    if not re.search(rf"구매 희망가\s*{price:,}\s*원", body):
        raise BidAborted(f"최종 주문정보의 구매 희망가가 {price:,}원이 아님")
    first.click()
    page.wait_for_timeout(1500)

    if _completed(page):
        log.info("구매 입찰 완료 확인")
        return

    try:
        page.get_by_text("구매조건 확인 및 거래진행 동의").first.wait_for(state="visible", timeout=8000)
    except PlaywrightTimeout as e:
        dump(page, f"{pid}_no_confirm_modal")
        raise BidAborted("'구매조건 확인 및 거래진행 동의' 모달이 뜨지 않음") from e
    if not page.get_by_text(re.compile(r"창고\s*보관으로 진행됩니다")).count():
        dump(page, f"{pid}_confirm_not_warehouse")
        raise BidAborted("확인 모달에 '창고보관으로 진행됩니다' 문구가 없음")

    checked = _check_required_items(page)
    log.info("확인 항목 %d개 체크", checked)
    if settings.inspect:
        dump(page, f"{pid}_5_confirm")
    unchecked = page.evaluate(_UNCHECKED_REQUIRED_JS)
    if unchecked:
        raise BidAborted(f"체크되지 않은 확인 항목: {unchecked}")

    final_buttons = page.get_by_role("button", name=re.compile(r"입찰하기"))
    final = final_buttons.nth(final_buttons.count() - 1)
    if final.is_disabled():
        raise BidAborted("확인 항목을 체크했는데도 마지막 '입찰하기' 가 비활성")
    if settings.stop_before_submit:
        dump(page, f"{pid}_stop_before_submit")
        raise StoppedBeforeSubmit("마지막 '입찰하기' 직전에 멈춤 (--stop-before-submit)")
    final.click()
    try:
        page.get_by_text(COMPLETED_RE).first.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeout as e:
        dump(page, f"{pid}_no_completion")
        raise BidUncertain("마지막 '입찰하기' 를 눌렀으나 '구매 입찰이 완료' 문구를 확인하지 못함") from e
    log.info("구매 입찰 완료 확인")
    if settings.inspect:
        dump(page, f"{pid}_6_done")


def _completed(page: Page) -> bool:
    return page.get_by_text(COMPLETED_RE).count() > 0


def _check_required_items(page: Page) -> int:
    """모달의 '[필수] ...' 버튼 안 체크박스를 누른다 (이미 체크된 것은 건너뜀). 누른 개수.

    항목 글자 가운데를 누르면 '이용정책'/'검수기준' 링크가 눌릴 수 있어 체크박스 아이콘을 누른다.
    """
    n = 0
    for item in page.get_by_role("button", name=re.compile(r"^\[필수\]")).all():
        box = item.get_by_role("checkbox").first
        if box.count() and box.get_attribute("aria-checked") == "true":
            continue
        (box if box.count() else item).click(force=True)
        page.wait_for_timeout(400)
        n += 1
    return n


_UNCHECKED_REQUIRED_JS = r"""
() => [...document.querySelectorAll('button')]
  .filter(b => b.innerText.trim().startsWith('[필수]'))
  .filter(b => { const c = b.querySelector('[role=checkbox]'); return !c || c.getAttribute('aria-checked') !== 'true'; })
  .map(b => b.innerText.trim().split(String.fromCharCode(10))[0])
"""


# '창고 보관' 글자를 가진 요소에서 위로 올라가며 선택 표시(라디오 checked,
# aria-checked/selected, active/selected/checked/on 클래스)를 찾는다.
_WAREHOUSE_SELECTED_JS = r"""
() => {
  const fill = sel => { const svg = document.querySelector(sel + ' svg'); return svg ? (svg.getAttribute('fill') || '').toLowerCase() : null; };
  const keep = fill(".select_radio[data-sdui-id='keep']");
  const normal = fill(".select_radio[data-sdui-id='normal']");
  if (keep !== '#222222') return false;
  if (normal === '#222222') return false;
  // 주문 상품 줄의 "ONE SIZE / 창고보관" 표기도 같이 확인
  const body = document.body.innerText;
  if (/\/\s*일반배송/.test(body) && !/\/\s*창고\s*보관/.test(body)) return false;
  return true;
}
"""

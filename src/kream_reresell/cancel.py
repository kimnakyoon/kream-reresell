"""마이페이지 > 구매 내역 > 구매 입찰 탭의 입찰을 다시 판정해, 기준에 못 미치는 입찰을 지운다.

흐름 (2026-09-04 실측):
  1. https://kream.co.kr/my/buying?tab=bidding 에 살아 있는 구매 입찰이 목록으로 나온다.
     항목마다 /my/buying/{입찰번호} 링크 (상품명 / 옵션 / 창고보관 / 희망가 / 마감일).
  2. 상세 페이지를 열면 api.kream.co.kr/api/m/bids/{입찰번호} 응답에 product_id, price, expires_at 이 있다.
     (응답을 못 잡으면 '상품 상세' 버튼을 눌러 /products/{id} URL 에서 읽는다.)
  3. 상품 페이지를 열어 입찰할 때와 똑같이 거래량 / A / B 를 읽어 판정한다 (pipeline.evaluate).
  4. 조건 미달이면 상세 페이지의 '입찰 지우기' 링크 -> 확인창(alert-dialog) 의 [입찰 지우기] 버튼.
     DELETE /api/m/bids/{입찰번호} 가 204 로 끝나면 지워진 것. 목록으로 돌아온다.

판단할 수 없는 경우(상품 페이지 오류, 빠른배송 가격 못 읽음 등)에는 지우지 않고 '확인필요' 로 남긴다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from . import pipeline
from . import product as product_mod
from .bid import StoppedBeforeSubmit
from .config import Settings
from .debug import dump
from .report import ProductResult
from .store import BidRecord, append_run_log, load_bids, remove_bid

log = logging.getLogger(__name__)

BIDDING_URL = "https://kream.co.kr/my/buying?tab=bidding"
MAX_LIST_SCROLL_ROUNDS = 30


class CancelAborted(Exception):
    """안전장치에 걸려 입찰을 지우지 않았다."""


class CancelUncertain(Exception):
    """[입찰 지우기] 를 눌렀는데 지워졌는지 확인하지 못했다."""


@dataclass
class OpenBid:
    order: int                 # 구매 입찰 목록에서의 순서 (1부터)
    bid_id: int
    url: str                   # /my/buying/{bid_id}
    name: str
    option: str = ""
    price: int | None = None   # 구매 희망가
    deadline: str = ""         # 목록의 마감일 (26/09/05)
    product_id: int | None = None
    expires_at: str = ""

    @property
    def product_url(self) -> str:
        return f"https://kream.co.kr/products/{self.product_id}"


# ---------------------------------------------------------------- 목록

def list_open_bids(page: Page) -> list[OpenBid]:
    """구매 입찰 탭의 입찰을 화면 순서대로 모은다. 탭 머리의 '구매 입찰 N' 만큼 스크롤해 다 읽는다."""
    page.goto(BIDDING_URL, wait_until="domcontentloaded")
    try:
        page.locator('a[href*="/my/buying/"]').or_(page.get_by_text("구매 입찰 내역이 없습니다")) \
            .first.wait_for(state="attached", timeout=15_000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(800)
    if "tab=bidding" not in page.url:
        raise CancelAborted(f"구매 입찰 탭이 열리지 않음: {page.url}")

    expected = page.evaluate(_BIDDING_COUNT_JS)
    rows: list[dict] = []
    prev = -1
    stale = 0
    for _ in range(MAX_LIST_SCROLL_ROUNDS):
        rows = page.evaluate(_BID_ROWS_JS)
        if expected is not None and len(rows) >= expected:
            break
        if len(rows) == prev:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
        prev = len(rows)
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)

    bids = [OpenBid(order=i + 1, bid_id=r["bid_id"], url="https://kream.co.kr" + r["href"],
                    name=r["name"], option=r["option"], price=r["price"], deadline=r["deadline"])
            for i, r in enumerate(rows)]
    log.info("구매 입찰 목록: %d건%s", len(bids), f" (탭 표시 {expected}건)" if expected is not None else "")
    if expected is not None and len(bids) < expected:
        log.warning("탭에는 %d건인데 %d건만 읽음 - 나머지는 다음 실행에서 보게 됨", expected, len(bids))
    return bids


_BIDDING_COUNT_JS = r"""
() => {
  for (const dt of document.querySelectorAll('dt')) {
    if (dt.textContent.trim() === '구매 입찰') {
      const dd = dt.parentElement.querySelector('dd');
      const n = dd ? parseInt(dd.textContent.replace(/[^\d]/g, ''), 10) : NaN;
      return isNaN(n) ? null : n;
    }
  }
  return null;
}
"""

_BID_ROWS_JS = r"""
() => {
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll('a[href*="/my/buying/"]')) {
    const href = a.getAttribute('href');
    const m = href.match(/\/my\/buying\/(\d+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const ps = [...a.querySelectorAll('p')].map(p => p.textContent.trim()).filter(Boolean);
    const text = ps.join(' ');
    const pm = text.match(/(\d[\d,]*)원/);
    const dm = text.match(/\d{2}\/\d{2}\/\d{2}/);
    out.push({ bid_id: parseInt(m[1], 10), href, name: ps[0] || '',
               option: ps[1] && ps[1] !== '/' ? ps[1] : '',
               price: pm ? parseInt(pm[1].replace(/,/g, ''), 10) : null,
               deadline: dm ? dm[0] : '' });
  }
  return out;
}
"""


# ---------------------------------------------------------------- 상세

def read_bid_info(page: Page, bid: OpenBid) -> dict | None:
    """상세 페이지로 이동하면서 api/m/bids/{입찰번호} 응답만 받아 bid 에 채운다 (화면이 다 그려지길 기다리지 않는다).

    API 를 브라우저 밖에서 직접 부르면 500 이 와서(요청 서명 검사로 보임) 페이지 이동으로만 받을 수 있다.
    """
    try:
        with page.expect_response(
                lambda r: f"/api/m/bids/{bid.bid_id}" in r.url and r.request.method == "GET",
                timeout=15_000) as resp:
            page.goto(bid.url, wait_until="commit")
        data = resp.value.json()
    except Exception as e:  # noqa: BLE001
        log.debug("입찰 상세 API 응답을 못 잡음 (%s)", e)
        return None
    if not isinstance(data, dict) or not data.get("product_id"):
        return None
    bid.product_id = int(data["product_id"])
    if data.get("price"):
        bid.price = int(float(data["price"]))
    bid.expires_at = str(data.get("expires_at") or "")
    return data


def match_known_bid(bid: OpenBid, known: dict[int, BidRecord]) -> int | None:
    """이 프로그램이 넣은 입찰(bids.json)과 상품명·희망가가 같으면 그 상품 ID (상세를 열지 않아도 됨)."""
    if not bid.price:
        return None
    for pid, rec in known.items():
        if rec.name == bid.name and rec.price == bid.price:
            return pid
    return None


def open_bid_detail(page: Page, bid: OpenBid) -> None:
    """입찰 상세를 열고 product_id / 희망가 / 마감을 채운 뒤, 살아 있는 입찰인지 확인한다."""
    data = read_bid_info(page, bid)
    page.wait_for_timeout(800)
    if data is not None:
        status = data.get("status")
        if status and status != "live":
            raise CancelAborted(f"입찰 상태가 '{data.get('status_display') or status}' - 살아 있는 입찰이 아님")
    else:
        bid.product_id = _product_id_via_button(page, bid)

    if not page.get_by_role("link", name=re.compile("입찰 지우기")).count():
        raise CancelAborted("상세 화면에 '입찰 지우기' 가 없음 - 이미 지워졌거나 체결된 입찰")
    log.info("입찰 #%d: 상품 %s, 희망가 %s원, 마감 %s", bid.bid_id, bid.product_id,
             f"{bid.price:,}" if bid.price else "?", bid.expires_at or bid.deadline)


def _product_id_via_button(page: Page, bid: OpenBid) -> int:
    """'상품 상세' 버튼을 눌러 /products/{id} 로 갔다가 돌아온다."""
    btn = page.get_by_role("button", name="상품 상세").first
    btn.wait_for(state="visible", timeout=10_000)
    btn.click()
    page.wait_for_url(re.compile(r"/products/(\d+)"), timeout=15_000)
    m = re.search(r"/products/(\d+)", page.url)
    pid = int(m.group(1)) if m else 0
    page.goto(bid.url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if not pid:
        raise CancelAborted("상품 ID 를 알아내지 못함")
    return pid


# ---------------------------------------------------------------- 지우기

def delete_bid(page: Page, bid: OpenBid, settings: Settings) -> None:
    """상세 화면의 '입찰 지우기' -> 확인창 [입찰 지우기]. DELETE 응답 204 를 확인해야 성공."""
    if f"/my/buying/{bid.bid_id}" not in page.url:
        page.goto(bid.url, wait_until="domcontentloaded")
    link = page.get_by_role("link", name=re.compile("입찰 지우기")).first
    try:
        link.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeout as e:
        dump(page, f"bid{bid.bid_id}_no_delete_link")
        raise CancelAborted("'입찰 지우기' 링크가 보이지 않음") from e
    try:
        page.get_by_text("구매 희망가", exact=True).first.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeout:
        pass
    body = page.locator("body").inner_text()
    if bid.price and not re.search(rf"구매 희망가\s*{bid.price:,}\s*원", body):
        raise CancelAborted(f"상세 화면의 구매 희망가가 {bid.price:,}원이 아님 - 다른 입찰일 수 있음")

    link.click()
    dialog = page.locator(".alert-dialog__container").first
    try:
        dialog.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout as e:
        dump(page, f"bid{bid.bid_id}_no_dialog")
        raise CancelAborted("'입찰 지우기' 확인창이 뜨지 않음") from e
    text = dialog.inner_text()
    if "입찰 지우기" not in text or "취소" not in text:
        raise CancelAborted(f"확인창 내용이 예상과 다름: {text[:80]!r}")
    confirm = dialog.locator(".alert-dialog__button--confirm").first
    if confirm.inner_text().strip() != "입찰 지우기":
        raise CancelAborted(f"확인 버튼 글자가 '입찰 지우기' 가 아님: {confirm.inner_text().strip()!r}")
    if settings.inspect:
        dump(page, f"bid{bid.bid_id}_delete_dialog")
    if settings.stop_before_submit:
        raise StoppedBeforeSubmit("확인창의 [입찰 지우기] 직전에 멈춤 (--stop-before-submit)")

    try:
        with page.expect_response(
                lambda r: r.request.method == "DELETE" and f"/api/m/bids/{bid.bid_id}" in r.url,
                timeout=15_000) as resp:
            confirm.click()
        status = resp.value.status
    except PlaywrightTimeout as e:
        dump(page, f"bid{bid.bid_id}_no_delete_response")
        raise CancelUncertain("[입찰 지우기] 를 눌렀으나 삭제 응답을 확인하지 못함") from e
    if status not in (200, 204):
        raise CancelUncertain(f"삭제 요청 응답이 {status}")
    page.wait_for_timeout(1000)
    log.info("입찰 #%d 지움 (DELETE %d)", bid.bid_id, status)


# ---------------------------------------------------------------- 실행

def review_bid(context: BrowserContext, bid: OpenBid, settings: Settings) -> ProductResult:
    """입찰 하나를 새 탭에서 다시 판정하고, 조건 미달이면 지운다."""
    page: Page = context.new_page()
    r = ProductResult(rank=bid.order, product_id=bid.product_id or 0, name=bid.name, url=bid.url,
                      category="구매입찰", bid_price=bid.price)
    try:
        if bid.product_id:
            log.info("입찰 #%d: 상품 %d (bids.json 기록과 일치, 상세 생략), 희망가 %s원, 마감 %s",
                     bid.bid_id, bid.product_id, f"{bid.price:,}" if bid.price else "?", bid.deadline)
        else:
            open_bid_detail(page, bid)
        r.product_id, r.url, r.bid_price = bid.product_id or 0, bid.product_url, bid.price
        log.info("[입찰 %d번째] %s (%s)", bid.order, bid.name, bid.product_url)
        # 거래량이 모자라도 A/B 까지 읽어 보고서에 남긴다 (지운 이유를 나중에 볼 수 있게)
        reason = pipeline.evaluate(page, bid.product_url, r, settings, stop_early=False)
        when = f"마감 {bid.deadline or bid.expires_at[:10]}"
        if reason is None:
            r.status, r.detail = "입찰유지", f"조건 충족 ({when})"
            return r
        if settings.dry_run:
            r.status, r.detail = "취소대상", f"dry-run: {reason} ({when})"
            return r
        delete_bid(page, bid, settings)
        if bid.product_id:
            remove_bid(bid.product_id)
        r.status, r.detail = "입찰취소", f"{reason} -> 입찰 #{bid.bid_id} 지움"
        return r
    except product_mod.SkipProduct as e:
        r.status, r.detail = "확인필요", f"판단 불가 - 지우지 않음: {e}"
        return r
    except StoppedBeforeSubmit as e:
        r.status, r.detail = "중단", str(e)
        return r
    except CancelAborted as e:
        r.status, r.detail = "중단", f"안전장치: {e}"
        return r
    except CancelUncertain as e:
        r.status, r.detail = "확인필요", f"{e} - 마이페이지 구매 입찰 탭에서 확인"
        return r
    except Exception as e:  # noqa: BLE001
        dump(page, f"bid{bid.bid_id}_error")
        log.exception("입찰 #%d 처리 중 오류", bid.bid_id)
        r.status, r.detail = "오류", f"{type(e).__name__}: {e}"
        return r
    finally:
        r.time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info("[입찰 %d번째] 결과: %s - %s", bid.order, r.status, r.detail)
        append_run_log({
            "category": r.category, "rank": r.rank, "product_id": r.product_id, "name": r.name,
            "fast_sales": r.fast_sales if r.fast_sales is not None else "",
            "price_a": r.price_a or "", "price_b": r.price_b or "",
            "status": r.status, "detail": r.detail,
        })
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def run(context: BrowserContext, page: Page, settings: Settings,
        should_stop: Callable[[], bool] | None = None,
        on_result: Callable[[ProductResult], None] | None = None) -> list[ProductResult]:
    """구매 입찰 목록 순서대로 전부 다시 판정한다. 끝나면 목록을 다시 읽어 지웠다는 것이 정말 사라졌는지 확인."""
    bids = list_open_bids(page)
    known = load_bids()
    for bid in bids:
        bid.product_id = match_known_bid(bid, known)
    results: list[ProductResult] = []
    deleted: dict[int, ProductResult] = {}
    for bid in bids:
        if should_stop and should_stop():
            log.info("사용자 요청으로 중지 - 남은 입찰 %d건은 보지 않음", len(bids) - len(results))
            break
        r = review_bid(context, bid, settings)
        results.append(r)
        if r.status == "입찰취소":
            deleted[bid.bid_id] = r
        if on_result:
            on_result(r)

    if deleted:
        try:
            remaining = {b.bid_id for b in list_open_bids(page)}
        except Exception as e:  # noqa: BLE001
            log.warning("지운 뒤 목록 재확인 실패: %s", e)
            remaining = set()
        for bid_id, r in deleted.items():
            if bid_id in remaining:
                r.status = "확인필요"
                r.detail += " - 그런데 목록에 아직 남아 있음, 마이페이지에서 확인"
                log.warning("입찰 #%d 가 지운 뒤에도 목록에 남아 있음", bid_id)
    return results


def open_bid_products(context: BrowserContext, page: Page) -> dict[int, OpenBid]:
    """마이페이지 구매 입찰 목록을 상품 ID -> 입찰 로 돌려준다. 입찰할 때 이미 입찰 중인 상품을 건너뛰는 데 쓴다.

    목록 API 에는 상품 ID 가 없다. 이 프로그램이 넣은 입찰(bids.json)과 상품명·희망가가 같으면 그 ID 를 쓰고,
    아니면 상세 페이지로 이동해 API 응답만 받는다 (하나에 1~2초). 상품 ID 를 알아낸 것은 상태와 무관하게 건너뛰기 대상에 넣는다.
    """
    bids = list_open_bids(page)
    out: dict[int, OpenBid] = {}
    if not bids:
        return out
    known = load_bids()
    unknown: list[OpenBid] = []
    for bid in bids:
        bid.product_id = match_known_bid(bid, known)
        if bid.product_id:
            out.setdefault(bid.product_id, bid)
        else:
            unknown.append(bid)
    if unknown:
        log.info("bids.json 에 없는 입찰 %d건은 상세를 열어 상품 ID 를 읽음", len(unknown))
        tab = context.new_page()
        try:
            for bid in unknown:
                try:
                    if read_bid_info(tab, bid) is None:
                        tab.wait_for_load_state("domcontentloaded")
                        bid.product_id = _product_id_via_button(tab, bid)
                except Exception as e:  # noqa: BLE001
                    log.warning("입찰 #%d 상세를 읽지 못함: %s", bid.bid_id, e)
                if bid.product_id:
                    out.setdefault(bid.product_id, bid)
        finally:
            try:
                tab.close()
            except Exception:  # noqa: BLE001
                pass
    log.info("마이페이지에 입찰 중인 상품 %d개: %s", len(out), ", ".join(str(pid) for pid in out) or "-")
    unread = [b.bid_id for b in bids if not b.product_id]
    if unread:
        log.warning("상품 ID 를 못 읽은 입찰 %d건 (#%s) - 그 상품은 건너뛰지 못할 수 있음", len(unread), ", #".join(map(str, unread)))
    return out

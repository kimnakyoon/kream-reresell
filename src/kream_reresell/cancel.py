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
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from . import pipeline
from . import product as product_mod
from .bid import StoppedBeforeSubmit
from .config import Settings
from .debug import dump
from .report import ProductResult
from .store import ONE_SIZE, BidRecord, append_run_log, load_bids, remove_bid

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
    option: str = ""           # 목록의 옵션 표기 (ONE SIZE / W240 / M ...)
    price: int | None = None   # 구매 희망가
    deadline: str = ""         # 목록의 마감일 (26/09/05)
    product_id: int | None = None
    expires_at: str = ""
    size: str = ""             # 구매 페이지 주소의 size 값 (ONE SIZE / 240 ...). 상세 API 의 product_option.key

    @property
    def product_url(self) -> str:
        return f"https://kream.co.kr/products/{self.product_id}"

    @property
    def is_one_size(self) -> bool:
        return not self.option or self.option == ONE_SIZE

    @property
    def eval_option(self) -> str | None:
        """pipeline.evaluate 에 넘길 옵션 (ONE SIZE 면 None)."""
        return None if self.is_one_size else self.option

    @property
    def size_value(self) -> str:
        """구매 페이지 주소에 쓸 size 값. ONE SIZE 상품은 늘 'ONE SIZE', 옵션 상품은 상세에서 읽기 전엔 빈 문자열."""
        return self.size or (ONE_SIZE if self.is_one_size else "")

    @property
    def needs_detail(self) -> bool:
        """상품 ID 나 (옵션 상품의) size 값을 몰라 상세를 열어야 하는지."""
        return not self.product_id or not self.size_value

    @property
    def label(self) -> str:
        return f"{self.name} [{self.option}]" if not self.is_one_size else self.name


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
    # 로그인이 풀리면 /login?returnUrl=/my/buying?tab=bidding 으로 가는데 주소에 tab=bidding 이 그대로 들어 있어
    # 0건으로 읽히던 문제 (2026-09-05) - 경로로 본다
    parsed = urlparse(page.url)
    if parsed.path.startswith("/login") or parsed.path.rstrip("/") != "/my/buying" or "tab=bidding" not in parsed.query:
        raise CancelAborted(f"구매 입찰 탭이 열리지 않음 (로그인이 풀렸거나 다른 곳으로 넘어감): {page.url}")

    expected = page.evaluate(_BIDDING_COUNT_JS)
    if expected is None and not page.locator('a[href*="/my/buying/"]').count() \
            and not page.get_by_text("구매 입찰 내역이 없습니다").count():
        # 탭 머리의 건수도, 항목도, '없습니다' 안내도 없다 - 목록이 그려지지 않은 것 (사이트가 응답을 안 줌?)
        raise CancelAborted(f"구매 입찰 목록이 그려지지 않음 (건수 표시 없음): {page.url}")
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
    # 옵션: product_option.key 가 구매 페이지 주소의 size 값, name_display 가 화면 표기 (2026-09-05 실측: ONE SIZE 는 둘 다 'ONE SIZE')
    po = data.get("product_option") or {}
    bid.size = str(po.get("key") or data.get("option") or "").strip()
    shown = str(po.get("name_display") or po.get("name") or data.get("option") or "").strip()
    if shown:
        bid.option = shown
    if not bid.size and bid.is_one_size:
        bid.size = ONE_SIZE
    return data


def apply_known(bid: OpenBid, rec: BidRecord | None) -> bool:
    """bids.json 기록으로 상품 ID·size 를 채운다."""
    if rec is None:
        return False
    bid.product_id = rec.product_id
    bid.size = rec.size or (ONE_SIZE if bid.is_one_size else "")
    return True


def ensure_product_id(page: Page, bid: OpenBid) -> dict | None:
    """상세 페이지에서 상품 ID · 희망가를 채운다 (API 응답, 안 되면 '상품 상세' 버튼으로). API 응답(dict)이 있으면 돌려준다."""
    data = read_bid_info(page, bid)
    if data is None:
        page.wait_for_load_state("domcontentloaded")
        bid.product_id = _product_id_via_button(page, bid)
        if bid.is_one_size:
            bid.size = ONE_SIZE
    return data


def match_known_bid(bid: OpenBid, known: dict[str, BidRecord]) -> BidRecord | None:
    """이 프로그램이 넣은 입찰(bids.json)과 상품명·옵션·희망가가 같으면 그 기록 (상세를 열지 않아도 상품 ID·size 를 안다)."""
    if not bid.price:
        return None
    opt = bid.option or ONE_SIZE
    for rec in known.values():
        if rec.name == bid.name and rec.price == bid.price and (rec.option or ONE_SIZE) == opt:
            return rec
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
        if bid.is_one_size:
            bid.size = ONE_SIZE

    if not page.get_by_role("link", name=re.compile("입찰 지우기")).count():
        raise CancelAborted("상세 화면에 '입찰 지우기' 가 없음 - 이미 지워졌거나 체결된 입찰")
    if not bid.size_value:
        raise CancelAborted(f"옵션 '{bid.option}' 의 size 값을 상세에서 읽지 못함")
    log.info("입찰 #%d: 상품 %s%s, 희망가 %s원, 마감 %s", bid.bid_id, bid.product_id,
             f" [{bid.option} / size={bid.size}]" if not bid.is_one_size else "",
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
                      category="구매입찰", bid_price=bid.price, option="" if bid.is_one_size else bid.option)
    try:
        if not bid.needs_detail:
            log.info("입찰 #%d: 상품 %d%s (bids.json 기록과 일치, 상세 생략), 희망가 %s원, 마감 %s",
                     bid.bid_id, bid.product_id, f" [{bid.option}]" if not bid.is_one_size else "",
                     f"{bid.price:,}" if bid.price else "?", bid.deadline)
        else:
            open_bid_detail(page, bid)
        r.product_id, r.url, r.bid_price, r.size = bid.product_id or 0, bid.product_url, bid.price, bid.size_value
        log.info("[입찰 %d번째] %s (%s)", bid.order, bid.label, bid.product_url)
        # 거래량이 모자라도 A/B 까지 읽어 보고서에 남긴다 (지운 이유를 나중에 볼 수 있게)
        # 상품 금액 상한은 새로 입찰할 때만 쓰는 규칙이라 이미 넣은 입찰에는 적용하지 않는다
        reason = pipeline.evaluate(page, bid.product_url, r, settings, stop_early=False, price_limit=False,
                                   option=bid.eval_option)
        when = f"마감 {bid.deadline or bid.expires_at[:10]}"
        if reason is None:
            r.status, r.detail = "입찰유지", f"조건 충족 ({when})"
            return r
        if settings.dry_run:
            r.status, r.detail = "취소대상", f"dry-run: {reason} ({when})"
            return r
        delete_bid(page, bid, settings)
        if bid.product_id:
            remove_bid(bid.product_id, bid.size_value)
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
            "category": r.category, "rank": r.rank, "product_id": r.product_id, "name": r.name, "option": r.option,
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
        apply_known(bid, match_known_bid(bid, known))
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


@dataclass
class OpenBids:
    """마이페이지에 지금 살아 있는 구매 입찰. 입찰할 때 이미 입찰 중인 상품(옵션)을 건너뛰는 유일한 기준."""
    by_product: dict[int, dict[str, OpenBid]] = field(default_factory=dict)   # 상품 ID 를 알아낸 입찰: 상품 ID -> 옵션 표기 -> 입찰
    unread: list[OpenBid] = field(default_factory=list)                       # 상품 ID 를 못 읽은 입찰 (상품명으로만 대조)

    def __contains__(self, product_id: int) -> bool:
        return product_id in self.by_product

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_product.values()) + len(self.unread)

    def add(self, bid: OpenBid) -> None:
        if bid.product_id:
            self.by_product.setdefault(bid.product_id, {}).setdefault(bid.option or ONE_SIZE, bid)

    def find(self, product_id: int, option: str = ONE_SIZE) -> OpenBid | None:
        """이 상품의 이 옵션(ONE SIZE 상품은 ONE SIZE)에 살아 있는 입찰."""
        return self.by_product.get(product_id, {}).get(option or ONE_SIZE)

    def by_name(self, name: str, option: str = ONE_SIZE) -> OpenBid | None:
        """상품 ID 를 못 읽은 입찰 중 상품명(마이페이지 표기 = 상품 페이지 제목)과 옵션이 같은 것."""
        name = name.strip()
        for b in self.unread:
            if b.name.strip() == name and (b.option or ONE_SIZE) == (option or ONE_SIZE):
                return b
        return None


def open_bid_products(context: BrowserContext, page: Page) -> OpenBids:
    """마이페이지 구매 입찰 목록을 읽어 상품 ID -> 입찰 로 돌려준다. 입찰할 때 이미 입찰 중인 상품을 건너뛰는 데 쓴다.

    목록 API 에는 상품 ID 가 없다. 이 프로그램이 넣은 입찰(bids.json)과 상품명·희망가가 같으면 그 ID 를 쓰고,
    아니면 상세 페이지로 이동해 API 응답만 받는다 (하나에 1~2초). 상품 ID 를 알아낸 것은 상태와 무관하게 건너뛰기 대상에 넣고,
    끝내 못 읽은 입찰은 unread 에 남겨 상품명으로 대조한다.
    """
    bids = list_open_bids(page)
    out = OpenBids()
    if not bids:
        return out
    known = load_bids()
    unknown: list[OpenBid] = []
    for bid in bids:
        apply_known(bid, match_known_bid(bid, known))
        if bid.product_id:
            out.add(bid)
        else:
            unknown.append(bid)
    if unknown:
        log.info("bids.json 에 없는 입찰 %d건은 상세를 열어 상품 ID 를 읽음", len(unknown))
        tab = context.new_page()
        try:
            for bid in unknown:
                try:
                    ensure_product_id(tab, bid)
                except Exception as e:  # noqa: BLE001
                    log.warning("입찰 #%d 상세를 읽지 못함: %s", bid.bid_id, e)
                out.add(bid)
        finally:
            try:
                tab.close()
            except Exception:  # noqa: BLE001
                pass
    log.info("마이페이지에 입찰 중인 상품 %d개: %s", len(out.by_product),
             ", ".join(f"{pid}({'/'.join(opts)})" if list(opts) != [ONE_SIZE] else str(pid)
                       for pid, opts in out.by_product.items()) or "-")
    out.unread = [b for b in bids if not b.product_id]
    if out.unread:
        log.warning("상품 ID 를 못 읽은 입찰 %d건 (#%s) - 그 상품은 상품명으로만 대조해 건너뜀",
                    len(out.unread), ", #".join(str(b.bid_id) for b in out.unread))
    return out

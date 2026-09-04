"""판매 내역 정리: 보관 판매(종료) 와 구매 내역(종료) 을 읽어 매입-판매 짝을 맞춘다.

화면 대신 그 화면이 부르는 API 를 그대로 쓴다 (2026-09-04 실측):
  - 보관 판매 > 종료 > 정산완료 : GET api/seller/inventory/items/finished?status=payout_completed&cursor=N
    항목마다 id(보관번호), oid(보관판매 주문번호 I-SW…), price(판매가), price_breakdown.total_payout(정산금액),
    transaction.date_created(보관 상세의 "거래일시"), date_paid(정산일), product.release(상품명 한/영, style_code), product_option(사이즈)
  - 보관 판매 > 보관 중 > 판매완료 : …/items/in_stock?status=sold (팔렸지만 아직 정산 전인 것도 같은 달 판매로 넣는다)
  - 구매 내역 > 종료 : GET api/o/bids/?tab=finished&status=all&cursor=… (화면 구성 그대로 내려오는 목록)
    결제번호 헤더(O-OR…, 결제 일시 = 주문 화면의 "거래 일시") 다음에 상품 줄(/my/buying/입찰번호, 상품명, 사이즈,
    창고보관 링크 /my/inventory?…&id=보관번호). 이 보관번호가 보관 판매의 id 와 같다 → 매입-판매를 정확히 잇는 열쇠.
  - 구매 상세 : GET api/m/bids/입찰번호 → oid(매입 주문번호 B-SW…), price(매입가 = 즉시 구매가), keep.ask_id(보관번호)

API 는 브라우저 밖에서 부르면 막히지만(요청 서명), 페이지 안에서 사이트가 보낸 헤더(authorization, x-kream-*)를
그대로 붙여 fetch 하면 된다 (credentials 는 omit 이어야 CORS 를 통과한다).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

API_BASE = "https://api.kream.co.kr"
INVENTORY_FINISHED_URL = "https://kream.co.kr/my/inventory?tab=finished"
KST = timezone(timedelta(hours=9))
PER_PAGE = 50                   # 서버가 허용하는 최대 (100 을 줘도 50 으로 잘린다, 2026-09-04 실측)
MAX_PAGES = 200                 # 목록 한 종류에 이보다 많은 페이지는 읽지 않는다
PARALLEL_FETCH = 10             # 구매 상세를 한 번에 이만큼 동시에 받는다 (순차보다 5배쯤 빠르다)
# 판매 목록은 보관번호 순(최신 우선)이라 거래일시가 딱 정렬돼 있지는 않다. 한 페이지가 통째로
# 정리하는 달보다 이만큼 오래된 거래뿐이면 그 뒤는 더 읽지 않는다.
SALES_STOP_MARGIN_DAYS = 90

_FETCH_JS = """
async ([url, headers]) => {
  const r = await fetch(url, { credentials: 'omit', headers });
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch (e) { body = null; }
  return { status: r.status, body, text: body === null ? text.slice(0, 300) : '' };
}
"""

_FETCH_MANY_JS = """
async ([urls, headers]) => Promise.all(urls.map(async (url) => {
  try {
    const r = await fetch(url, { credentials: 'omit', headers });
    const text = await r.text();
    let body = null;
    try { body = JSON.parse(text); } catch (e) { body = null; }
    return { status: r.status, body, text: body === null ? text.slice(0, 300) : '' };
  } catch (e) { return { status: -1, body: null, text: String(e) }; }
}))
"""


class HistoryError(Exception):
    pass


# ---------------------------------------------------------------- 자료

@dataclass
class SaleRecord:
    """보관 판매 한 건 (팔린 것)."""
    inventory_id: int          # 보관번호 (= ask id). 구매 내역의 창고보관 링크 id 와 같다
    oid: str                   # 보관판매 주문번호 I-SW12515619-1
    product_id: int
    option: str                # 사이즈 (ONE SIZE 포함)
    style_code: str            # 제품코드 (모델번호). 없으면 ""
    name_ko: str
    name_en: str
    price: int                 # 판매가
    payout: int                # 정산금액 (수수료 뺀 것)
    sold_at: datetime | None   # 거래일시 (KST, 보관 상세의 "거래일시")
    paid_at: datetime | None   # 정산일 (KST)
    status: str                # 화면 표시 (판매자 지급완료 / 판매완료 …)
    status_code: str = ""      # API 상태값: payout_completed(정산완료) / sold(판매완료, 정산 전) …
    purchase: PurchaseRecord | None = None
    match_how: str = ""        # 보관번호 / 상품·사이즈 / (없음)

    @property
    def name(self) -> str:
        return self.name_ko or self.name_en

    @property
    def paid(self) -> bool:
        return self.status_code == "payout_completed"


@dataclass
class PurchaseRecord:
    """구매 내역(종료) 한 건."""
    bid_id: int
    order_no: str              # 결제번호 O-OR46253240
    ordered_at: datetime | None  # 결제(거래) 일시 KST - 주문 화면의 "거래 일시"
    name_ko: str
    option: str
    inventory_id: int | None   # 목록의 창고보관 링크 id. 창고보관이 아니면 None
    status: str = ""
    # 상세(api/m/bids)에서 채우는 것
    oid: str = ""              # 매입 주문번호 B-SW149786945
    price: int | None = None   # 즉시 구매가 (매입가)
    total_price: int | None = None   # 수수료 등 포함 결제금액
    product_id: int | None = None
    detail_loaded: bool = False


@dataclass
class HistoryResult:
    year: int
    month: int
    sales: list[SaleRecord] = field(default_factory=list)          # 그 달에 팔린 것 (엑셀 행)
    purchases_seen: int = 0
    sales_seen: int = 0

    @property
    def unmatched(self) -> list[SaleRecord]:
        return [s for s in self.sales if s.purchase is None]


# ---------------------------------------------------------------- 시각

def parse_utc(text: str | None) -> datetime | None:
    """'2026-08-26T07:35:16Z' -> KST naive datetime. 못 읽으면 None."""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).replace(tzinfo=None)


def month_range(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


# ---------------------------------------------------------------- API

class ApiClient:
    """페이지 안에서 fetch 로 KREAM API 를 부른다. 헤더는 사이트가 실제로 보낸 요청에서 복사한다."""

    HEADER_KEEP = ("authorization", "accept")

    def __init__(self, page: Page) -> None:
        self.page = page
        self.headers: dict[str, str] = {}

    def capture_headers(self, url: str = INVENTORY_FINISHED_URL) -> None:
        """url 로 이동하면서 사이트가 API 에 보내는 헤더(authorization, x-kream-*)를 잡아 둔다."""
        try:
            with self.page.expect_request(
                    lambda r: r.url.startswith(API_BASE) and "authorization" in r.headers
                    and "notification" not in r.url, timeout=20_000) as req:
                self.page.goto(url, wait_until="domcontentloaded")
            headers = req.value.headers
        except PlaywrightTimeout as e:
            raise HistoryError("KREAM API 요청 헤더를 잡지 못했습니다 (로그인 상태와 페이지를 확인)") from e
        self.headers = {k: v for k, v in headers.items()
                        if k.lower().startswith("x-kream") or k.lower() in self.HEADER_KEEP}
        log.debug("API 헤더 %d개 확보", len(self.headers))

    def _request_headers(self) -> dict[str, str]:
        if not self.headers:
            self.capture_headers()
        headers = dict(self.headers)
        headers["x-kream-client-datetime"] = datetime.now(KST).strftime("%Y%m%d%H%M%S+0900")
        return headers

    @staticmethod
    def _url(path: str) -> str:
        return path if path.startswith("http") else API_BASE + path

    def get(self, path: str, retry: bool = True) -> dict:
        try:
            res = self.page.evaluate(_FETCH_JS, [self._url(path), self._request_headers()])
        except Exception as e:  # noqa: BLE001
            raise HistoryError(f"API 호출 실패 ({path}): {e}") from e
        if res["status"] in (401, 403) and retry:
            log.info("API 인증이 끊겨 헤더를 다시 잡습니다 (%s)", res["status"])
            self.capture_headers()
            return self.get(path, retry=False)
        if res["status"] != 200 or not isinstance(res.get("body"), dict):
            raise HistoryError(f"API 응답 오류 {res['status']} ({path}): {res.get('text', '')[:200]}")
        return res["body"]

    def get_many(self, paths: list[str]) -> list[dict | HistoryError]:
        """여러 경로를 PARALLEL_FETCH 개씩 동시에 받는다. 항목마다 응답 dict 또는 HistoryError."""
        out: list[dict | HistoryError] = []
        for i in range(0, len(paths), PARALLEL_FETCH):
            chunk = paths[i:i + PARALLEL_FETCH]
            try:
                results = self.page.evaluate(_FETCH_MANY_JS, [[self._url(p) for p in chunk], self._request_headers()])
            except Exception as e:  # noqa: BLE001
                out.extend(HistoryError(f"API 호출 실패 ({p}): {e}") for p in chunk)
                continue
            for path, res in zip(chunk, results):
                if res["status"] == 200 and isinstance(res.get("body"), dict):
                    out.append(res["body"])
                elif res["status"] in (401, 403):
                    # 토큰이 끊긴 것 - 헤더를 다시 잡고 하나씩 다시 시도
                    self.capture_headers()
                    try:
                        out.append(self.get(path, retry=False))
                    except HistoryError as e:
                        out.append(e)
                else:
                    out.append(HistoryError(f"API 응답 오류 {res['status']} ({path}): {res.get('text', '')[:200]}"))
        return out


# ---------------------------------------------------------------- 보관 판매

def _int(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def parse_sale(item: dict) -> SaleRecord | None:
    """inventory/items 목록의 항목 하나 -> SaleRecord. 거래(판매) 가 없는 항목이면 None."""
    tx = item.get("transaction") or {}
    release = (item.get("product") or {}).get("release") or {}
    option = item.get("product_option") or {}
    breakdown = item.get("price_breakdown") or {}
    status_item = item.get("status_display_item") or {}
    if not item.get("id"):
        return None
    sold_at = parse_utc(tx.get("date_created"))
    payout = breakdown.get("total_payout")
    return SaleRecord(
        inventory_id=int(item["id"]),
        oid=str(item.get("oid") or ""),
        product_id=_int(item.get("product_id") or release.get("id")),
        option=str(option.get("name_display") or option.get("key") or item.get("option") or ""),
        style_code=str(release.get("style_code") or "").strip(" -"),
        name_ko=str(release.get("translated_name") or ""),
        name_en=str(release.get("name") or ""),
        price=_int(item.get("price")),
        payout=_int(payout if payout is not None else item.get("price")),
        sold_at=sold_at,
        paid_at=parse_utc(item.get("date_paid")),
        status=str(status_item.get("text") or item.get("status_display") or item.get("status") or ""),
        status_code=str(item.get("status") or ""),
    )


def fetch_sales(client: ApiClient, year: int, month: int,
                should_stop: Callable[[], bool] | None = None) -> tuple[list[SaleRecord], int]:
    """보관 판매에서 거래일시가 year-month 인 판매를 모은다. (그 달 판매 목록, 읽은 전체 건수)"""
    start, end = month_range(year, month)
    stop_before = start - timedelta(days=SALES_STOP_MARGIN_DAYS)
    picked: dict[int, SaleRecord] = {}
    seen = 0
    sources = (
        ("종료 > 정산완료", "/api/seller/inventory/items/finished", "payout_completed"),
        ("보관 중 > 판매완료", "/api/seller/inventory/items/in_stock", "sold"),
    )
    for label, path, status in sources:
        cursor: str | None = "1"
        pages = 0
        total = None
        while cursor and pages < MAX_PAGES:
            if should_stop and should_stop():
                break
            body = client.get(f"{path}?per_page={PER_PAGE}&cursor={cursor}&status={status}")
            items = body.get("items") or []
            if total is None:
                total = body.get("total")
                log.info("보관 판매 %s: %s건", label, total if total is not None else "?")
            pages += 1
            seen += len(items)
            all_old = bool(items)
            for it in items:
                s = parse_sale(it)
                if s is None:
                    continue
                if s.sold_at is None:
                    all_old = False
                    log.warning("보관번호 %s (%s) 거래일시를 읽지 못해 제외", s.inventory_id, s.name)
                    continue
                if s.sold_at >= stop_before:
                    all_old = False
                if start <= s.sold_at < end:
                    picked[s.inventory_id] = s
            cursor = body.get("next_cursor")
            if cursor is not None:
                cursor = str(cursor)
            if all_old:
                log.info("보관 판매 %s: %d페이지까지 봤고 그 뒤는 %s 이전 거래뿐이라 멈춤",
                         label, pages, stop_before.strftime("%Y-%m-%d"))
                break
    # 거래일시 오름차순 (1일 → 말일). 사용자 요청 2026-09-04
    sales = sorted(picked.values(), key=lambda s: (s.sold_at or datetime.min, s.inventory_id))
    log.info("%d년 %d월 거래일시 판매: %d건 (읽은 항목 %d건)", year, month, len(sales), seen)
    return sales, seen


# ---------------------------------------------------------------- 구매 내역

def _text(node: dict | None) -> str:
    """서버 화면 구성(text_body 등) 노드에서 글자만 뽑는다."""
    if not isinstance(node, dict):
        return ""
    el = node.get("text_element")
    if isinstance(el, dict):
        dv = el.get("default_variation") or {}
        return str(dv.get("text") or "")
    items = node.get("items")
    if isinstance(items, list):
        return " ".join(t for t in (_text(i) for i in items) if t)
    return ""


def _action_url(node: dict | None) -> str:
    if not isinstance(node, dict):
        return ""
    for a in node.get("actions") or []:
        if a.get("type") == "url" and a.get("value"):
            return str(a["value"])
    return ""


def _find_urls(node, out: list[str]) -> None:
    if isinstance(node, dict):
        u = _action_url(node)
        if u:
            out.append(u)
        for v in node.values():
            _find_urls(v, out)
    elif isinstance(node, list):
        for v in node:
            _find_urls(v, out)


def parse_purchase_items(items: list[dict]) -> list[PurchaseRecord]:
    """구매 내역 목록(화면 구성 항목들) -> 구매 건. 결제번호 헤더 뒤에 오는 상품 줄마다 하나."""
    out: list[PurchaseRecord] = []
    order_no = ""
    ordered_at: datetime | None = None
    for it in items:
        kind = it.get("display_type")
        if kind == "text_header_checkout":
            order_no = _text(it.get("subtitle_item"))
            ordered_at = parse_utc(_text(it.get("description_item")))
            continue
        if kind != "product_list_info_action":
            continue
        m = re.search(r"/my/buying/(\d+)", _action_url(it))
        if not m:
            continue
        urls: list[str] = []
        _find_urls(it.get("label_item"), urls)
        inv = None
        for u in urls:
            im = re.search(r"/my/inventory\?.*\bid=(\d+)", u)
            if im:
                inv = int(im.group(1))
                break
        option_item = it.get("option_item") or {}
        option = _text(option_item.get("option1_item"))
        labels = [t for t in (_text(x) for x in (it.get("label_item") or {}).get("items") or []) if t]
        out.append(PurchaseRecord(
            bid_id=int(m.group(1)), order_no=order_no, ordered_at=ordered_at,
            name_ko=_text(it.get("text_item")), option=option if option != "/" else "",
            inventory_id=inv, status=" ".join(labels)))
    return out


def fetch_purchases(client: ApiClient, should_stop: Callable[[], bool] | None = None) -> list[PurchaseRecord]:
    """구매 내역 > 종료 탭 전체."""
    out: list[PurchaseRecord] = []
    cursor: str | None = None
    pages = 0
    total = None
    seen_bids: set[int] = set()
    while pages < MAX_PAGES:
        if should_stop and should_stop():
            break
        path = f"/api/o/bids/?tab=finished&status=all&per_page={PER_PAGE}" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(path)
        if total is None:
            total = body.get("total")
            log.info("구매 내역 종료: %s건", total if total is not None else "?")
        pages += 1
        for p in parse_purchase_items(body.get("items") or []):
            if p.bid_id not in seen_bids:
                seen_bids.add(p.bid_id)
                out.append(p)
        nxt = body.get("next_cursor")
        if not nxt or str(nxt) == str(cursor):
            break
        cursor = str(nxt)
    log.info("구매 내역 읽음: %d건 (창고보관 링크 있는 것 %d건)", len(out), sum(1 for p in out if p.inventory_id))
    return out


def load_purchase_details(client: ApiClient, purchases: list[PurchaseRecord]) -> None:
    """구매 상세(api/m/bids)를 동시에 받아 매입 주문번호 / 매입가 / 보관번호를 채운다. 못 읽은 건은 경고만 남긴다."""
    todo = [p for p in purchases if not p.detail_loaded]
    if not todo:
        return
    for p, body in zip(todo, client.get_many([f"/api/m/bids/{p.bid_id}" for p in todo])):
        if isinstance(body, HistoryError):
            log.warning("구매 #%d 상세를 읽지 못함: %s", p.bid_id, body)
            continue
        apply_purchase_detail(p, body)


def apply_purchase_detail(p: PurchaseRecord, body: dict) -> None:
    p.detail_loaded = True
    p.oid = str(body.get("oid") or "")
    if body.get("price") is not None:
        p.price = _int(body["price"])
    total = (body.get("price_breakdown") or {}).get("total_price")
    if total is not None:
        p.total_price = _int(total)
    if body.get("product_id"):
        p.product_id = int(body["product_id"])
    keep = body.get("keep") or {}
    if keep.get("ask_id") and not p.inventory_id:
        p.inventory_id = int(keep["ask_id"])
    if not p.option:
        p.option = str(body.get("option") or "")


# ---------------------------------------------------------------- 매칭

def match(sales: list[SaleRecord], purchases: list[PurchaseRecord], client: ApiClient,
          should_stop: Callable[[], bool] | None = None) -> None:
    """판매 건마다 매입 건을 찾는다.

    1순위: 보관번호 (구매 목록의 창고보관 링크 id == 보관 판매 id) - 정확한 열쇠.
    2순위: 창고보관 링크가 없는 구매 중 상품명·사이즈가 같은 것의 상세를 열어 keep.ask_id 로 확인.
    3순위: 그래도 없으면 상품명·사이즈가 같고 아직 짝이 없는 구매 중 판매보다 먼저 결제된 가장 오래된 것.
    """
    by_inv: dict[int, PurchaseRecord] = {}
    for p in purchases:
        if p.inventory_id and p.inventory_id not in by_inv:
            by_inv[p.inventory_id] = p
    used: set[int] = set()

    for s in sales:
        p = by_inv.get(s.inventory_id)
        if p and p.bid_id not in used:
            s.purchase, s.match_how = p, "보관번호"
            used.add(p.bid_id)

    def same_product(p: PurchaseRecord, s: SaleRecord) -> bool:
        # 취소된 구매(취소완료 / 검수 불합격 …)는 창고에 들어간 적이 없으니 후보에서 뺀다
        if "취소" in p.status:
            return False
        return _norm(p.name_ko) == _norm(s.name_ko) and _norm(p.option) == _norm(s.option)

    # 2순위: 링크 없는 구매의 상세를 열어 보관번호 확인
    pending = [s for s in sales if s.purchase is None]
    if pending:
        candidates = [p for p in purchases if p.bid_id not in used and not p.inventory_id
                      and any(same_product(p, s) for s in pending)]
        if candidates and not (should_stop and should_stop()):
            load_purchase_details(client, candidates)
        for p in candidates:
            if p.inventory_id:
                by_inv.setdefault(p.inventory_id, p)
        for s in pending:
            p = by_inv.get(s.inventory_id)
            if p and p.bid_id not in used:
                s.purchase, s.match_how = p, "보관번호(상세)"
                used.add(p.bid_id)

    # 3순위: 상품명·사이즈
    for s in sales:
        if s.purchase is not None:
            continue
        cands = [p for p in purchases if p.bid_id not in used and same_product(p, s)
                 and (p.ordered_at is None or s.sold_at is None or p.ordered_at <= s.sold_at)]
        cands.sort(key=lambda p: p.ordered_at or datetime.min)
        if cands:
            s.purchase, s.match_how = cands[0], "상품·사이즈"
            used.add(cands[0].bid_id)

    # 짝이 된 구매의 상세(매입 주문번호 / 매입가)를 한꺼번에 받는다
    if not (should_stop and should_stop()):
        load_purchase_details(client, [s.purchase for s in sales if s.purchase is not None])
    for s in sales:
        p = s.purchase
        if p is None:
            log.warning("매입 내역 없음: %s %s (보관 %s, 거래 %s)", s.name, s.option, s.oid,
                        s.sold_at.strftime("%m/%d") if s.sold_at else "?")
            continue
        paid = p.total_price if p.total_price is not None else p.price
        log.info("[%s] %s %s | 매입 %s %s원 %s | 판매 %s %s원 → 정산 %s원 (%s)",
                 s.match_how, s.name[:30], s.option, p.oid or f"#{p.bid_id}",
                 f"{paid:,}" if paid is not None else "?",
                 p.ordered_at.strftime("%m/%d") if p.ordered_at else "?",
                 s.oid, f"{s.price:,}", f"{s.payout:,}",
                 s.sold_at.strftime("%m/%d") if s.sold_at else "?")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


# ---------------------------------------------------------------- 실행

def collect(page: Page, year: int, month: int,
            should_stop: Callable[[], bool] | None = None) -> HistoryResult:
    """보관 판매 + 구매 내역을 읽고 짝을 맞춘 결과. page 는 로그인된 KREAM 탭."""
    client = ApiClient(page)
    log.info("보관 판매 목록을 읽는 중...")
    client.capture_headers()
    sales, seen = fetch_sales(client, year, month, should_stop)
    result = HistoryResult(year=year, month=month, sales=sales, sales_seen=seen)
    if not sales:
        log.info("%d년 %d월에 거래된 보관 판매가 없습니다", year, month)
        return result
    if should_stop and should_stop():
        return result
    log.info("구매 내역 목록을 읽는 중...")
    purchases = fetch_purchases(client, should_stop)
    result.purchases_seen = len(purchases)
    log.info("매입-판매 짝 맞추는 중...")
    match(sales, purchases, client, should_stop)
    matched = sum(1 for s in sales if s.purchase)
    log.info("짝 맞춤: %d/%d건 (보관번호 %d, 상세 %d, 상품·사이즈 %d, 없음 %d)",
             matched, len(sales),
             sum(1 for s in sales if s.match_how == "보관번호"),
             sum(1 for s in sales if s.match_how == "보관번호(상세)"),
             sum(1 for s in sales if s.match_how == "상품·사이즈"),
             len(sales) - matched)
    return result

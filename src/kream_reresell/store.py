"""입찰 이력 저장 (같은 상품·옵션에 중복 입찰하지 않기 위해)."""

from __future__ import annotations

import contextlib
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from .config import DATA_DIR

BIDS_PATH = DATA_DIR / "bids.json"
RUN_LOG_PATH = DATA_DIR / "run_log.csv"

ONE_SIZE = "ONE SIZE"


def bid_key(product_id: int, size: str = ONE_SIZE) -> str:
    """bids.json 의 키. ONE SIZE 상품은 예전처럼 상품 ID 만, 옵션(사이즈) 상품은 '상품ID:size' (size 는 구매 페이지 주소의 값)."""
    if not size or size == ONE_SIZE:
        return str(product_id)
    return f"{product_id}:{size}"


@dataclass
class BidRecord:
    product_id: int
    name: str
    price: int
    bid_days: int
    placed_at: str
    fast_sales_30d: int
    price_a: int
    price_b: int             # 2026-09-13 부터 B = 1순위가 되는 입찰가 (market.price_b), 그 전 기록은 즉시 판매가 원값
    option: str = ONE_SIZE   # 화면 표기 옵션 (W240, M, ONE SIZE ...) - 마이페이지 목록·상품 페이지의 표기와 같다
    size: str = ONE_SIZE     # 구매 페이지 주소 /buy/{id}?size=... 의 값 (product_option.key, 예: 240). 표기(W240)와 다를 수 있다

    @property
    def key(self) -> str:
        return bid_key(self.product_id, self.size)


def load_bids() -> dict[str, BidRecord]:
    """키 -> 기록. 키는 bid_key() (예전 파일의 '상품ID' 키도 그대로 ONE SIZE 로 읽힌다)."""
    if not BIDS_PATH.exists():
        return {}
    raw = json.loads(BIDS_PATH.read_text(encoding="utf-8"))
    out: dict[str, BidRecord] = {}
    for k, v in raw.items():
        rec = BidRecord(**v)
        out[rec.key] = rec
    return out


def _write_bids(bids: dict[str, BidRecord]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BIDS_PATH.write_text(
        json.dumps({k: asdict(v) for k, v in bids.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_bid(record: BidRecord) -> None:
    bids = load_bids()
    bids[record.key] = record
    _write_bids(bids)


def append_run_log(row: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    row = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **row}
    new = not RUN_LOG_PATH.exists()
    if not new:
        with RUN_LOG_PATH.open(encoding="utf-8-sig") as f:
            header = f.readline().strip().split(",")
        if header != list(row.keys()):  # 컬럼 구성이 바뀌었으면 옛 파일을 옆에 두고 새로 시작
            RUN_LOG_PATH.rename(RUN_LOG_PATH.with_name(f"run_log_old_{datetime.now():%Y%m%d_%H%M%S}.csv"))
            new = True
    with RUN_LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def remove_bid(product_id: int, size: str = ONE_SIZE) -> bool:
    """입찰을 지웠을 때 이력에서 빼서, 나중에 조건이 다시 맞으면 새로 입찰할 수 있게 한다."""
    bids = load_bids()
    key = bid_key(product_id, size)
    if key not in bids:
        return False
    del bids[key]
    _write_bids(bids)
    return True


# ---------------------------------------------------------------- 입찰번호 -> 상품 ID·옵션 (재입찰용 캐시)
# 마이페이지 구매 입찰 목록에는 상품 ID 가 없어 상세를 열어야 한다 (1~2초). 한 번 읽은 것은 여기 남겨 다음 실행에도 다시 열지 않는다.
# 값: {"product_id": 상품 ID, "size": 구매 페이지 주소의 size 값, "option": 화면 표기}. 예전 파일의 정수 값은 상품 ID 만 아는 것으로 읽는다.
BID_PRODUCTS_PATH = DATA_DIR / "bid_products.json"


def load_bid_products() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for k, v in _load_json(BID_PRODUCTS_PATH).items():
        try:
            if isinstance(v, dict):
                out[int(k)] = {"product_id": int(v["product_id"]), "size": str(v.get("size") or ""),
                               "option": str(v.get("option") or "")}
            else:
                out[int(k)] = {"product_id": int(v), "size": "", "option": ""}
        except (ValueError, TypeError, KeyError):
            continue
    return out


def save_bid_products(mapping: dict[int, dict]) -> None:
    _write_json(BID_PRODUCTS_PATH, {str(k): v for k, v in mapping.items()})


def _load_json(path) -> dict:
    """JSON 객체 파일. 없거나 깨졌거나 객체가 아니면 빈 dict."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path, data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 사이트가 카테고리 단위로 거절한 입찰 (그날 하루 건너뜀)
# 마지막 '입찰하기'(POST /api/checkout)를 "신규 보관 신청이 제한된 카테고리의 상품입니다." 로 거절하면 (bid.BidRejected.is_category_limit,
# 2026-09-13 18:16 [입찰] 실측: 라이프 8건 - 쿠션·보틀·라이터·팝콘통·응원봉·텀블러·랜덤박스·담요 - 이 전부 이 거절이었고 마이페이지에 입찰 없음)
# 그 카테고리(시세 API 의 release.category, 예: life)는 그날 내내 안 된다 - 같은 날짜에는 그 카테고리 상품을 전부 건너뛴다
# (사용자 결정 2026-09-13: 13일에 뜨면 13일 내내 안 되는 것. 날짜가 바뀌면 다시 시도). 상품 -> 카테고리는 시세 API 응답에서만 알 수 있어
# 한 번 읽은 것은 product_categories.json 에 남긴다 (카테고리는 바뀌지 않는다) - 거절된 날 두 번째 실행부터는 그 상품의 시세 호출(틱 6초)도 안 쓴다.
# 시세 API 응답의 market.inventory_service_available 는 true 라 사전 신호가 못 된다 - 마지막 요청에서만 거절된다.
REJECTED_CATEGORIES_PATH = DATA_DIR / "rejected_categories.json"   # {"life": {"date": "2026-09-13", "time": "18:16", "message": "..."}}
PRODUCT_CATEGORIES_PATH = DATA_DIR / "product_categories.json"     # {"513852": "life"}


def _rejection_label(v: dict) -> str:
    return f"{v.get('time', '')} {v.get('message', '')}"


class CategoryRejections:
    """오늘 사이트가 거절한 카테고리와, 상품 -> 카테고리 기억 (위 설명). [입찰] 실행마다 하나 만든다 (app.run_job).
    상품 카테고리는 새로 안 것을 모아 flush() 로 한 번 쓴다 (상품마다 파일 전체를 다시 쓰지 않게 - pipeline.run 끝)."""

    def __init__(self) -> None:
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.rejected: dict[str, dict] = {k: v for k, v in _load_json(REJECTED_CATEGORIES_PATH).items()
                                          if isinstance(v, dict) and v.get("date") == self.today}
        self.categories: dict[int, str] = {}
        for k, v in _load_json(PRODUCT_CATEGORIES_PATH).items():
            with contextlib.suppress(ValueError, TypeError):
                self.categories[int(k)] = str(v)
        self._dirty = False

    def describe(self) -> str:
        return ", ".join(f"{c} ({_rejection_label(v)})" for c, v in self.rejected.items())

    def blocked(self, category: str) -> str | None:
        """이 카테고리가 오늘 거절됐으면 건너뛸 사유, 아니면 None."""
        v = self.rejected.get(category)
        if v is None:
            return None
        return f"오늘({self.today}) 사이트가 이 카테고리({category})의 입찰을 거절함 ({_rejection_label(v)}) - 그날은 전부 건너뜀"

    def blocked_product(self, product_id: int) -> str | None:
        """전에 시세를 읽어 카테고리를 아는 상품이 오늘 거절된 카테고리면 그 사유 (시세를 읽지 않고 건너뛴다)."""
        return self.blocked(self.categories.get(product_id, ""))

    def remember(self, product_id: int, category: str) -> None:
        if category and self.categories.get(product_id) != category:
            self.categories[product_id] = category
            self._dirty = True

    def flush(self) -> None:
        if self._dirty:
            _write_json(PRODUCT_CATEGORIES_PATH, {str(k): v for k, v in self.categories.items()})
            self._dirty = False

    def reject(self, category: str, message: str) -> None:
        """오늘 이 카테고리가 거절됐다고 남긴다 (파일에도 - 다음 실행이 같은 날이면 그대로 건너뛴다)."""
        if not category:
            return
        self.rejected[category] = {"date": self.today, "time": datetime.now().strftime("%H:%M"), "message": message}
        _write_json(REJECTED_CATEGORIES_PATH, self.rejected)

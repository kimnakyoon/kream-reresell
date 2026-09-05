"""입찰 이력 저장 (같은 상품·옵션에 중복 입찰하지 않기 위해)."""

from __future__ import annotations

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
    price_b: int
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
    if not BID_PRODUCTS_PATH.exists():
        return {}
    try:
        raw = json.loads(BID_PRODUCTS_PATH.read_text(encoding="utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    out: dict[int, dict] = {}
    for k, v in raw.items():
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
    DATA_DIR.mkdir(exist_ok=True)
    BID_PRODUCTS_PATH.write_text(json.dumps({str(k): v for k, v in mapping.items()}, ensure_ascii=False, indent=1),
                                 encoding="utf-8")

"""입찰 이력 저장 (같은 상품에 중복 입찰하지 않기 위해)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from .config import DATA_DIR

BIDS_PATH = DATA_DIR / "bids.json"
RUN_LOG_PATH = DATA_DIR / "run_log.csv"


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


def load_bids() -> dict[int, BidRecord]:
    if not BIDS_PATH.exists():
        return {}
    raw = json.loads(BIDS_PATH.read_text(encoding="utf-8"))
    return {int(k): BidRecord(**v) for k, v in raw.items()}


def save_bid(record: BidRecord) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    bids = load_bids()
    bids[record.product_id] = record
    BIDS_PATH.write_text(
        json.dumps({str(k): asdict(v) for k, v in bids.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def remove_bid(product_id: int) -> bool:
    """입찰을 지웠을 때 이력에서 빼서, 나중에 조건이 다시 맞으면 새로 입찰할 수 있게 한다."""
    bids = load_bids()
    if product_id not in bids:
        return False
    del bids[product_id]
    BIDS_PATH.write_text(
        json.dumps({str(k): asdict(v) for k, v in bids.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


# ---------------------------------------------------------------- 입찰번호 -> 상품 ID (재입찰용 캐시)
# 마이페이지 구매 입찰 목록에는 상품 ID 가 없어 상세를 열어야 한다 (1~2초). 한 번 읽은 것은 여기 남겨 다음 실행에도 다시 열지 않는다.
BID_PRODUCTS_PATH = DATA_DIR / "bid_products.json"


def load_bid_products() -> dict[int, int]:
    if not BID_PRODUCTS_PATH.exists():
        return {}
    try:
        raw = json.loads(BID_PRODUCTS_PATH.read_text(encoding="utf-8"))
        return {int(k): int(v) for k, v in raw.items()}
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}


def save_bid_products(mapping: dict[int, int]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BID_PRODUCTS_PATH.write_text(json.dumps({str(k): v for k, v in mapping.items()}, indent=1), encoding="utf-8")

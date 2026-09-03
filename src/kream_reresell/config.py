"""실행 설정 (.env + 명령행)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

BID_DAY_CHOICES = (1, 3, 7, 30, 60, 90, 180)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass
class Settings:
    kream_id: str = field(default_factory=lambda: os.environ.get("KREAM_ID", "").strip())
    kream_pw: str = field(default_factory=lambda: os.environ.get("KREAM_PW", "").strip())

    lookback_days: int = field(default_factory=lambda: _int("LOOKBACK_DAYS", 30))
    min_fast_sales: int = field(default_factory=lambda: _int("MIN_FAST_SALES", 15))
    min_margin_rate: float = field(default_factory=lambda: _float("MIN_MARGIN_RATE", 0.10))
    bid_days: int = field(default_factory=lambda: _int("BID_DAYS", 7))
    max_products: int = field(default_factory=lambda: _int("MAX_PRODUCTS", 30))
    # 이미지/동영상/폰트를 받지 않아 페이지를 빨리 띄운다. 화면 확인이 필요하면 .env 에 BLOCK_IMAGES=0
    block_images: bool = field(
        default_factory=lambda: os.environ.get("BLOCK_IMAGES", "1").strip().lower() not in ("0", "false", "no"))

    # 실행 모드
    dry_run: bool = False          # 판단만 하고 입찰 폼은 건드리지 않는다
    stop_before_submit: bool = False  # 입찰 화면까지 채우되 마지막 "입찰하기"는 누르지 않는다
    force: bool = False            # 거래량/마진 조건을 무시하고 진행 (점검용)
    inspect: bool = False          # 화면마다 접근성 스냅샷을 dumps/ 에 남긴다

    def validate(self) -> None:
        if self.bid_days not in BID_DAY_CHOICES:
            raise ValueError(f"BID_DAYS 는 {BID_DAY_CHOICES} 중 하나여야 합니다: {self.bid_days}")
        if not 0 <= self.min_margin_rate < 1:
            raise ValueError(f"MIN_MARGIN_RATE 는 0 이상 1 미만이어야 합니다: {self.min_margin_rate}")


DATA_DIR = ROOT / "data"
DUMP_DIR = ROOT / "dumps"
LOG_DIR = ROOT / "logs"

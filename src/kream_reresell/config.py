"""실행 설정 (.env + 명령행)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import pacing
from .rules import BidRules

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DUMP_DIR = ROOT / "dumps"
LOG_DIR = ROOT / "logs"
RULES_PATH = DATA_DIR / "bid_rules.json"   # GUI [입찰 기준] 에서 저장한 금액 구간별 마진율 / 상품 금액 상한

BID_DAY_CHOICES = (1, 3, 7, 30, 60, 90, 180)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in ("1", "true", "yes") if raw else default


@dataclass
class Settings:
    kream_id: str = field(default_factory=lambda: os.environ.get("KREAM_ID", "").strip())
    kream_pw: str = field(default_factory=lambda: os.environ.get("KREAM_PW", "").strip())

    lookback_days: int = field(default_factory=lambda: _int("LOOKBACK_DAYS", 30))
    min_fast_sales: int = field(default_factory=lambda: _int("MIN_FAST_SALES", 15))
    # 기본 마진율 (.env). data/bid_rules.json 이 없을 때 금액 구간 하나짜리 기준으로 쓴다
    min_margin_rate: float = field(default_factory=lambda: _float("MIN_MARGIN_RATE", 0.10))
    # 금액 구간별 최소 마진율 + 상품 금액 상한. GUI 에서 고치면 data/bid_rules.json 에 저장된다
    rules: BidRules = field(default_factory=lambda: BidRules.load(RULES_PATH, _float("MIN_MARGIN_RATE", 0.10)))
    bid_days: int = field(default_factory=lambda: _int("BID_DAYS", 7))
    max_products: int = field(default_factory=lambda: _int("MAX_PRODUCTS", 30))
    # [재입찰] 사이클 시작 간격(분). 구매 입찰 목록을 한 바퀴 돈 뒤 다음 바퀴를 이 간격으로 시작한다 (봇 탐지 대비, 1분 이상)
    rebid_interval_min: float = field(default_factory=lambda: _float("REBID_INTERVAL_MIN", 5))
    # 이미지/동영상/폰트를 받지 않아 페이지를 빨리 띄운다. 화면 확인이 필요하면 .env 에 BLOCK_IMAGES=0
    block_images: bool = field(default_factory=lambda: _bool("BLOCK_IMAGES", True))
    # 사이트 스로틀(IP 단위) 대응 - pacing 참고. 10분 창 안에 상품 API(sales 등) 요청을 이만큼까지만 보내고 넘으면 쉰다.
    # 0 이면 예산을 안 센다. TRIM_API=0 이면 안 보는 asks·bids·chart 요청도 그대로 보낸다 (점검용)
    api_budget_per_10min: int = field(default_factory=lambda: _int("API_BUDGET_PER_10MIN", 60))
    trim_api: bool = field(default_factory=lambda: _bool("TRIM_API", True))
    # 크롬 창을 화면에 보이게 둘지. 기본은 화면 밖으로 치워 두고 GUI 상태창으로만 진행을 본다.
    # 직접 로그인이 필요하면 자동으로 불러온다. 화면을 보며 점검하려면 .env 에 SHOW_CHROME=1 또는 --show-chrome
    show_chrome: bool = field(default_factory=lambda: _bool("SHOW_CHROME", False))

    # 실행 모드
    dry_run: bool = False          # 판단만 하고 입찰 폼은 건드리지 않는다
    stop_before_submit: bool = False  # 입찰 화면까지 채우되 마지막 "입찰하기"는 누르지 않는다
    force: bool = False            # 거래량/마진 조건을 무시하고 진행 (점검용)
    inspect: bool = False          # 화면마다 접근성 스냅샷을 dumps/ 에 남긴다
    options: tuple[str, ...] = ()  # 옵션(사이즈) 상품에서 이 옵션들만 본다 (점검용, 화면 표기: W240 / M ...). 비우면 전부

    def validate(self) -> None:
        pacing.configure(self.api_budget_per_10min)
        if self.stop_before_submit:
            self.show_chrome = True  # 마지막 버튼 직전에 멈추는 점검은 사람이 화면을 보는 게 목적
        if self.bid_days not in BID_DAY_CHOICES:
            raise ValueError(f"BID_DAYS 는 {BID_DAY_CHOICES} 중 하나여야 합니다: {self.bid_days}")
        if not 0 <= self.min_margin_rate < 1:
            raise ValueError(f"MIN_MARGIN_RATE 는 0 이상 1 미만이어야 합니다: {self.min_margin_rate}")
        if self.rebid_interval_min < 1:
            raise ValueError(f"재입찰 사이클 간격은 1분 이상이어야 합니다 (사이트 차단 방지): {self.rebid_interval_min}")
        self.rules.validate()

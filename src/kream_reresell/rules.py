"""입찰 기준: S(예상 판매가) 금액 구간별 최소 마진율 + 상품 금액 상한.

- 금액 구간: S 가 [부터, 미만) 에 들어가는 구간의 마진율을 쓴다. 예) 0~50,000원 10%, 50,000원~ 11%
  어느 구간에도 들지 않는 S 는 입찰하지 않는다. S = min(A 빠른배송 가격, R 최근 빠른배송 체결 15건 최저가) - 2026-09-17 부터
  (그 전에는 A). 판정: (S − B) > S × 마진율 (report.ProductResult.price_s, pipeline.judge_margin).
- 상품 금액 상한: A 가 이 금액을 넘는 상품은 B 를 읽지 않고 바로 건너뛴다 (예: 300,000원). 비우면 제한 없음.

GUI 의 [입찰 기준] 에서 고치면 data/bid_rules.json 에 저장되고, 명령행 실행도 같은 파일을 읽는다.
파일이 없으면 .env 의 MIN_MARGIN_RATE 하나로 된 구간 하나를 기준으로 쓴다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Tier:
    lo: int                 # A 가 이 금액 이상
    hi: int | None          # A 가 이 금액 미만 (None = 상한 없음)
    margin_pct: float       # 최소 마진율 (%). (S - B) 가 S 의 이 % 를 넘어야 입찰

    def contains(self, price_s: int) -> bool:
        return price_s >= self.lo and (self.hi is None or price_s < self.hi)

    @property
    def label(self) -> str:
        if self.hi is None:
            return f"S {self.lo:,}원 이상"
        return f"S {self.lo:,}~{self.hi:,}원"

    @property
    def margin_rate(self) -> float:
        """0.10 = 10%"""
        return self.margin_pct / 100.0

    def describe(self) -> str:
        return f"{self.label}: {self.margin_pct:g}%"


@dataclass
class BidRules:
    tiers: list[Tier] = field(default_factory=list)
    max_price_a: int | None = None   # A 가 이 금액(원)을 넘으면 건너뜀. None = 제한 없음

    # ---------------------------------------------------------------- 판정
    def tier_for(self, price_s: int) -> Tier | None:
        """S(예상 판매가)가 들어가는 구간."""
        for t in self.tiers:
            if t.contains(price_s):
                return t
        return None

    def over_limit(self, price_a: int) -> bool:
        """A 가 상품 금액 상한을 넘는가."""
        return self.max_price_a is not None and price_a > self.max_price_a

    # ---------------------------------------------------------------- 검사/설명
    def validate(self) -> None:
        if not self.tiers:
            raise ValueError("금액 구간이 하나도 없습니다")
        tiers = sorted(self.tiers, key=lambda t: t.lo)
        for t in tiers:
            if t.lo < 0:
                raise ValueError(f"구간 시작 금액이 0 보다 작습니다: {t.lo:,}")
            if t.hi is not None and t.hi <= t.lo:
                raise ValueError(f"구간 끝 금액이 시작 금액보다 커야 합니다: {t.lo:,} ~ {t.hi:,}")
            if not 0 <= t.margin_pct < 100:
                raise ValueError(f"마진율은 0 이상 100 미만이어야 합니다: {t.margin_pct}")
        for a, b in zip(tiers, tiers[1:]):
            if a.hi is None or a.hi > b.lo:
                raise ValueError(f"금액 구간이 겹칩니다: {a.label} / {b.label}")
        if self.max_price_a is not None and self.max_price_a <= 0:
            raise ValueError(f"상품 금액 상한은 0 보다 커야 합니다: {self.max_price_a:,}")
        self.tiers = tiers

    def describe(self) -> str:
        tiers = " / ".join(t.describe() for t in self.tiers)
        limit = (f"A {self.max_price_a:,}원 넘으면 건너뜀" if self.max_price_a is not None
                 else "상품 금액 상한 없음")
        return f"마진 (S−B) > S×[{tiers}] (S = 예상 판매가 = min(A 빠른배송가, R 최근 체결가)), {limit}"

    # ---------------------------------------------------------------- 저장
    def to_dict(self) -> dict:
        return {
            "tiers": [{"lo": t.lo, "hi": t.hi, "margin_pct": t.margin_pct} for t in self.tiers],
            "max_price_a": self.max_price_a,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> BidRules:
        tiers = [Tier(lo=int(t["lo"]), hi=None if t.get("hi") in (None, "") else int(t["hi"]),
                      margin_pct=float(t["margin_pct"])) for t in raw.get("tiers", [])]
        limit = raw.get("max_price_a")
        return cls(tiers=tiers, max_price_a=None if limit in (None, "") else int(limit))

    def save(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, default_margin_rate: float = 0.10) -> BidRules:
        """파일이 없거나 깨졌으면 .env 마진율 하나로 된 기본 기준."""
        if path.exists():
            try:
                rules = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
                rules.validate()
                return rules
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        return cls.default(default_margin_rate)

    @classmethod
    def default(cls, margin_rate: float = 0.10) -> BidRules:
        return cls(tiers=[Tier(lo=0, hi=None, margin_pct=round(margin_rate * 100, 2))], max_price_a=None)

"""실행 결과를 엑셀 보고서(바탕화면\\KREAM 결과\\KREAM 입찰결과 날짜.xlsx) 로 정리한다."""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import DATA_DIR

log = logging.getLogger(__name__)

# 보고서는 바탕화면의 "KREAM 결과" 폴더에 쌓는다 (.env REPORT_DIR 로 바꿀 수 있다)
def _default_report_dir() -> Path:
    custom = os.environ.get("REPORT_DIR", "").strip()
    if custom:
        return Path(custom)
    desktop = Path.home() / "Desktop"
    if not desktop.exists():  # OneDrive 등으로 바탕화면이 옮겨진 경우
        one = Path.home() / "OneDrive" / "Desktop"
        desktop = one if one.exists() else DATA_DIR
    return desktop / "KREAM 결과"


REPORT_DIR = _default_report_dir()


@dataclass
class ProductResult:
    rank: int
    product_id: int
    name: str
    url: str
    category: str = ""          # 랭킹 (가방, 신발 ...)
    option: str = ""            # 옵션(사이즈) 화면 표기 (W240, M ...). ONE SIZE 상품은 비움
    size: str = ""              # 구매 페이지 주소의 size 값 (240). 입찰 기록(bids.json)에 남긴다
    status: str = ""            # 입찰완료 / 입찰대상(dry-run) / 건너뜀 / 중단 / 오류 / 확인필요
    detail: str = ""            # 사유
    fast_sales: int | None = None      # 기간 내 빠른배송 체결 수
    total_sales: int | None = None     # 기간 내 전체 체결 수
    price_a: int | None = None         # A = 빠른배송 가격 (지금 가장 싼 빠른배송 판매 호가)
    price_r: int | None = None         # R = 최근 30일 안 빠른배송 체결 15건의 최저가 (product.SalesStats.recent_price). 체결 표를 읽기 전엔 None
    price_b: int | None = None         # B = 1순위가 되는 입찰가 (market.price_b: 즉시 판매가 + 1,000원, 내 입찰이 이미 1순위면 내 희망가)
    margin_min: float | None = None    # 이 상품(S 금액 구간)에 적용된 최소 마진율 (0.10 = 10%)
    bid_price: int | None = None       # 입찰가 (= B). [판매]에서는 처리 뒤 판매 희망가
    bid_days: int | None = None
    buy_price: int | None = None       # [판매] 매입가 (수수료 포함 결제금액) - 하한의 기준
    time: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def price_s(self) -> int | None:
        """S = 예상 판매가 = min(A, R). 마진 판정(S − B > S × 기준)의 기준 금액 (2026-09-17 사용자 결정, product 머리글).

        A 는 판매자 호가라 실제로 팔리는 값보다 높은 때가 많고, R 은 최근 실제 체결가 - 둘 중 낮은 쪽을 예상 판매가로 본다.
        R 을 아직 못 읽었으면(시세 API 로만 거른 단계) A 그대로."""
        if self.price_a is None:
            return self.price_r
        if self.price_r is None:
            return self.price_a
        return min(self.price_a, self.price_r)

    @property
    def margin(self) -> int | None:
        if self.price_s is None or self.price_b is None:
            return None
        return self.price_s - self.price_b

    @property
    def margin_rate(self) -> float | None:
        if self.margin is None or not self.price_s:
            return None
        return self.margin / self.price_s


COLUMNS = [
    ("랭킹", 10), ("순위", 6), ("상품명", 46), ("옵션", 9), ("상품ID", 10), ("판정", 12), ("사유 / 결과", 46),
    ("30일 빠른배송", 13), ("30일 전체", 10), ("A 빠른배송가", 14), ("R 최근체결가", 14), ("S 예상판매가", 14), ("B 1순위입찰가", 14),
    ("S−B", 11), ("마진율", 9), ("기준마진", 9), ("입찰가", 12), ("입찰기한", 9), ("처리시각", 20), ("링크", 40),
]
_COL = {title: i for i, (title, _) in enumerate(COLUMNS, start=1)}   # 제목 -> 열 번호
_MONEY_COLS = [_COL[t] for t in ("A 빠른배송가", "R 최근체결가", "S 예상판매가", "B 1순위입찰가", "S−B", "입찰가")]

# [판매] 보고서는 열이 다르다 (sell 머리글): 경쟁 최저가 / 매입가 / 하한 / 이전·새 판매가
SELL_COLUMNS = [
    ("회차", 8), ("순서", 6), ("상품명", 46), ("옵션", 9), ("상품ID", 10), ("판정", 10), ("사유 / 결과", 60),
    ("경쟁 최저가", 12), ("매입가", 12), ("하한", 12), ("이전 판매가", 12), ("새 판매가", 12), ("하한 마진", 9),
    ("처리시각", 20), ("링크", 40),
]
_SELL_COL = {title: i for i, (title, _) in enumerate(SELL_COLUMNS, start=1)}
_SELL_MONEY_COLS = [_SELL_COL[t] for t in ("경쟁 최저가", "매입가", "하한", "이전 판매가", "새 판매가")]

STATUS_FILL = {
    "입찰완료": "C6EFCE",
    "입찰대상": "FFEB9C",
    "입찰취소": "C6EFCE",
    "취소대상": "FFEB9C",
    "변경완료": "C6EFCE",
    "변경대상": "FFEB9C",
    "변경안함": "DDEBF7",
    "확인필요": "F8CBAD",
    "오류": "FFC7CE",
    "중단": "FFC7CE",
    "가격변경": "C6EFCE",
    "하한대기": "DDEBF7",
    "유지": "FFFFFF",
}

BID_LEGEND = ("판정: 입찰완료 = 실제 입찰됨 / 입찰대상 = dry-run에서 조건 충족 / "
              "건너뜀 = 이미 입찰 중, 조건 미달, 또는 입찰을 시도했지만 넣지 못함 / 확인필요 = 마이페이지에서 입찰 여부 확인")
CANCEL_LEGEND = ("판정: 입찰취소 = 조건 미달이라 입찰을 지움 / 취소대상 = dry-run에서 조건 미달 / 입찰유지 = 조건 충족 / "
                 "확인필요 = 판단 불가 또는 지웠는지 불확실 - 마이페이지에서 확인")
REBID_LEGEND = ("판정: 변경완료 = 밀린 입찰의 희망가를 B(즉시 판매가+1,000원) 로 올림 / 변경대상 = dry-run에서 올릴 조건 충족 / "
                "순위유지 = 즉시 판매가가 내 희망가 이하라 밀리지 않음 / "
                "입찰취소 = 밀렸는데 기준 미달이거나, 즉시 판매가가 없거나, 변경 화면이 예상과 달라 못 올려 입찰을 지움 / "
                "취소대상 = dry-run에서 지울 대상 / 변경안함 = 기준은 충족하나 A 가 상품 금액 상한을 넘어 그대로 둠 / "
                "변경못함 = 희망가·옵션을 못 읽어 변경 화면까지 못 감 / 확인필요 = 판단 불가 또는 올렸는지 불확실 - 마이페이지에서 확인. "
                "랭킹 열 = 몇 번째 사이클인지, 입찰가 열 = 처리 뒤 내 희망가")
SELL_LEGEND = ("판정: 가격변경 = 판매 희망가를 바꿈 (경쟁 최저가 − 1,000원, 하한 위) / 변경대상 = dry-run에서 바꿀 조건 충족 / "
               "유지 = 내가 최저가이거나 바꿀 이유 없음 / 하한대기 = 경쟁 최저가가 하한 아래라 하한에 걸어 두고 기다림 / "
               "건너뜀 = 매입 내역이 없어 하한을 못 정함 / 확인필요 = 시세를 못 읽었거나 사이트가 변경을 거절 - 마이페이지 보관 판매에서 확인. "
               "하한 = 매입가 × (1 + 하한 마진) ÷ (1 − 판매 수수료율), 1,000원 단위 올림")
LEGENDS = {"입찰": BID_LEGEND, "입찰취소": CANCEL_LEGEND, "재입찰": REBID_LEGEND, "판매": SELL_LEGEND}


def summarize(results: list[ProductResult], unit: str = "건", empty: str = "처리한 상품 없음") -> str:
    """판정별 개수 한 줄: '변경완료 1건, 순위유지 5건'."""
    counts = Counter(r.status for r in results)
    return ", ".join(f"{k} {v}{unit}" for k, v in sorted(counts.items())) or empty


def sections(results: list[ProductResult]) -> list[tuple[str, list[ProductResult]]]:
    """랭킹(category) 열이 바뀌는 곳마다 잘라 (구분 이름, 그 구간 결과) 목록으로. [재입찰]은 category 가 'N회차' 라 회차별 구간이 된다."""
    out: list[tuple[str, list[ProductResult]]] = []
    for r in results:
        if not out or out[-1][0] != r.category:
            out.append((r.category, []))
        out[-1][1].append(r)
    return out


def section_title(name: str, rs: list[ProductResult], unit: str = "건") -> str:
    """구간(비어 있지 않음) 한 줄 요약: '1회차 (10:05~10:13): 변경완료 1건, 순위유지 5건 - 6건'."""
    return f"{name} ({rs[0].time[11:16]}~{rs[-1].time[11:16]}): {summarize(rs, unit)} - {len(rs)}{unit}"


def section_lines(results: list[ProductResult], unit: str = "건") -> list[str]:
    return [section_title(name, rs, unit) for name, rs in sections(results)]


_BOLD = Font(bold=True)
_SECTION_FILL = PatternFill("solid", fgColor="BDD7EE")
_STATUS_FILLS = {st: PatternFill("solid", fgColor=color) for st, color in STATUS_FILL.items()}   # 셀마다 새로 만들지 않게


def _write_sell_row(ws, i: int, r: ProductResult) -> None:
    values = [
        r.category, r.rank, r.name, r.option or None, r.product_id, r.status, r.detail,
        r.price_a, r.buy_price, r.price_r, r.price_b, r.bid_price, r.margin_min, r.time, r.url,
    ]
    for col, v in enumerate(values, start=1):
        ws.cell(row=i, column=col, value=v)
    for col in _SELL_MONEY_COLS:
        ws.cell(row=i, column=col).number_format = "#,##0"
    ws.cell(row=i, column=_SELL_COL["하한 마진"]).number_format = "0.0%"
    link = ws.cell(row=i, column=_SELL_COL["링크"])
    link.hyperlink = r.url
    link.font = Font(color="0563C1", underline="single")
    fill = _STATUS_FILLS.get(r.status)
    if fill:
        for col in range(1, len(SELL_COLUMNS) + 1):
            ws.cell(row=i, column=col).fill = fill


def _write_row(ws, i: int, r: ProductResult) -> None:
    values = [
        r.category, r.rank, r.name, r.option or None, r.product_id, r.status, r.detail,
        r.fast_sales, r.total_sales, r.price_a, r.price_r, r.price_s, r.price_b,
        r.margin, r.margin_rate, r.margin_min, r.bid_price,
        f"{r.bid_days}일" if r.bid_days else None, r.time, r.url,
    ]
    for col, v in enumerate(values, start=1):
        ws.cell(row=i, column=col, value=v)
    for col in _MONEY_COLS:
        ws.cell(row=i, column=col).number_format = "#,##0"
    ws.cell(row=i, column=_COL["마진율"]).number_format = "0.0%"
    ws.cell(row=i, column=_COL["기준마진"]).number_format = "0.0%"
    link = ws.cell(row=i, column=_COL["링크"])
    link.hyperlink = r.url
    link.font = Font(color="0563C1", underline="single")
    fill = _STATUS_FILLS.get(r.status)
    if fill:
        for col in range(1, len(COLUMNS) + 1):
            ws.cell(row=i, column=col).fill = fill


def write_report(results: list[ProductResult], settings_line: str, mode: str,
                 path: Path | None = None, kind: str = "입찰", section_label: str | None = None) -> Path:
    """kind 는 파일 이름과 판정 설명에 쓴다: '입찰' / '입찰취소' / '재입찰' / '판매' (판매는 열 구성이 다르다 - SELL_COLUMNS).
    section_label 을 주면 (재입찰: '회차') 결과 시트에서 랭킹 열 값이 바뀌는 곳마다 구분 줄(이름·시각·요약)을 넣고
    요약 시트의 구간 표 머리글에 쓴다 - 한 파일에 여러 회차가 쌓이는 재입찰용 (사용자 요청 2026-09-13)."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = path or REPORT_DIR / f"KREAM {kind}결과 {datetime.now():%Y-%m-%d %H%M}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "결과"

    ws["A1"] = f"KREAM 리리셀 {kind} 결과 - {mode}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = settings_line
    ws["A3"] = f"실행 시각: {datetime.now():%Y-%m-%d %H:%M}  |  {len(results)}줄 (옵션이 있는 상품은 옵션마다 한 줄)"
    ws["A4"] = LEGENDS.get(kind, BID_LEGEND)
    ws["A4"].font = Font(color="666666", size=9)

    header_row = 6
    columns = SELL_COLUMNS if kind == "판매" else COLUMNS
    write_row = _write_sell_row if kind == "판매" else _write_row
    bold = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="222222")
    for col, (title, width) in enumerate(columns, start=1):
        c = ws.cell(row=header_row, column=col, value=title)
        c.font = bold
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    i = header_row
    for name, rs in (sections(results) if section_label else [(None, results)]):
        if name is not None:
            # 구분 줄: 셀을 합치지 않는다 (합치면 엑셀에서 필터 정렬이 막힘). 오른쪽 셀이 비어 있어 글이 그대로 넘쳐 보인다
            i += 1
            ws.cell(row=i, column=1, value=f"▶ {section_title(name, rs)}").font = _BOLD
            for col in range(1, len(columns) + 1):
                ws.cell(row=i, column=col).fill = _SECTION_FILL
        for r in rs:
            i += 1
            write_row(ws, i, r)
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(columns))}{max(i, header_row + 1)}"

    # 요약 시트: 판정별 합계 + 랭킹(재입찰은 회차)별 판정 수. 한 번 세어 두고 조회만 한다 (100회차 재입찰이면 수천 줄)
    ss = wb.create_sheet("요약")
    by_cat_status = Counter((r.category, r.status) for r in results)
    counts: Counter[str] = Counter()
    for (_, st), n in by_cat_status.items():
        counts[st] += n
    ss["A1"] = "판정"
    ss["B1"] = "상품 수"
    ss["A1"].font = ss["B1"].font = _BOLD
    row = 2
    for k, v in sorted(counts.items()):
        ss.cell(row=row, column=1, value=k)
        ss.cell(row=row, column=2, value=v)
        row += 1

    categories = list(dict.fromkeys(cat for cat, _ in by_cat_status if cat))   # 나온 순서대로, 중복 없이
    if len(categories) > 1:
        row += 1
        statuses = sorted(counts)
        ss.cell(row=row, column=1, value=section_label or "랭킹").font = _BOLD
        for j, st in enumerate(statuses, start=2):
            ss.cell(row=row, column=j, value=st).font = _BOLD
        for cat in categories:
            row += 1
            ss.cell(row=row, column=1, value=cat)
            for j, st in enumerate(statuses, start=2):
                ss.cell(row=row, column=j, value=by_cat_status.get((cat, st), 0))
    ss.column_dimensions["A"].width = 14

    wb.save(path)
    log.info("엑셀 보고서 저장: %s", path)
    return path


def open_file(path: Path) -> None:
    """윈도우 기본 프로그램(엑셀)으로 연다."""
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        log.warning("파일을 열지 못했습니다(%s): %s", e, path)

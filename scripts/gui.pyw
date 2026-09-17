"""KREAM 리리셀 - 더블클릭으로 실행하는 창 (송장 자동화 GUI 와 같은 방식).

[입찰] 을 누르면 랭킹(또는 검색 결과, SHOP 카테고리 목록) → 상품 → 입찰까지 자동으로 진행한다. '상품 고르기' 에서
랭킹 / 검색 / SHOP 을 고르면 그쪽 설정만 보인다 (검색: 검색어 + 검색 결과 앞에서부터 시도할 상품 수, SHOP: 카테고리 체크 +
카테고리마다 시도할 상품 수). 크롬은 화면 밖에서 돌아가고
(작업표시줄에만 남음) 진행 상황은 이 창에만 표시된다. [크롬 창 보기] 를 켜면 실행 중에도 불러올 수 있다.
[입찰취소] 는 마이페이지 > 구매 내역 > 구매 입찰 목록을 순서대로 다시 판정해 기준 미달 입찰을 지운다.
[재입찰] 은 같은 목록을 설정칸에 정한 횟수만큼 돌며(기본 1회, 0 이면 [중지] 까지 계속), 즉시 판매가가 내 희망가보다
높아진(밀린) 입찰을 상품 페이지에서 처음 입찰 때 기준으로 다시 판정하고 충족하면 [입찰 변경하기] 로 희망가를 최신 B 로
올리며, 기준 미달이라 올릴 수 없으면 그 입찰을 지운다. A·B 는 페이지를 열지 않고 시세 API 로 읽으며(입찰 하나에 호출 하나),
호출 간격은 설정칸의 '시세 조회 간격' (기본 6초, 3~60초) - 회차 사이에 따로 쉬지 않는다. [입찰]도 같은 API 로 가격을 먼저 걸러
체결 내역 조회를 줄인다.
[입찰 기준] 표에서 S(예상 판매가 = min(A 빠른배송 가격, R 최근 빠른배송 체결 15건 최저가)) 금액 구간별 최소 마진율과 상품 금액 상한(A 가 넘으면 바로 건너뜀)을 정한다.
[입찰]/[입찰취소]/[기준 저장] 을 누르면 data/bid_rules.json 에 저장돼 다음 실행과 명령행에도 쓰인다.
끝나면 바탕화면\\KREAM 결과\\ 에 엑셀 보고서가 저장된다 (자동으로 열지는 않는다).
[내역] 은 달을 고르면 보관 판매(종료) 에서 그 달에 거래된 판매를 구매 내역(종료) 과 짝지어
정산 시트 모양의 엑셀(바탕화면\\KREAM 내역 YYYY-MM.xlsx) 로 저장한다.
[중지] 는 지금 보고 있는 상품(입찰)을 끝낸 뒤 멈춘다 (여러 작업이 돌고 있으면 어느 것을 멈출지 고른다).
[판매] 는 "판매 관리" 창(sellwin)을 연다 - 보관 판매 목록 표에서 행을 골라 경쟁에 넣고 [경쟁 시작] 을 누르면 하한(매입가 + 마진) 위에서
최저가 경쟁으로 판매 희망가를 맞춘다 (README '판매 규칙').

버튼은 서로 독립이다 (2026-09-17, 사용자 요청): [재입찰] 이 도는 동안 [입찰]·[내역]·[판매] 를 같이 돌릴 수 있다. 작업마다 자기 스레드가
같은 크롬에 따로 붙어 자기 탭만 쓴다 (browser 머리글). 같은 버튼은 끝날 때까지 다시 누를 수 없고, 상태줄에 도는 작업이 전부 보인다.
사이트에 보내는 요청·접속 예산과 시세 API 틱은 프로세스가 하나를 나눠 쓰므로 동시에 돌아도 사이트 쪽 속도는 그대로다 (대신 각 작업은 그만큼 느려진다).
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

if sys.platform == "win32" and sys.stdout is not None:
    # pythonw.exe(콘솔 없는 실행)에서는 stdout/stderr 가 None 이라 건드리면 안 된다.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kream_reresell import browser, pacing  # noqa: E402
from kream_reresell.app import normalize_keywords, run_cancel_job, run_history_job, run_job, run_rebid_job  # noqa: E402
from kream_reresell.sellwin import SellWindow  # noqa: E402
from kream_reresell.config import LOG_DIR, RULES_PATH, Settings  # noqa: E402
from kream_reresell.ranking import ALL_CATEGORIES, DEFAULT_CATEGORY  # noqa: E402
from kream_reresell.report import REPORT_DIR, section_lines, summarize  # noqa: E402
from kream_reresell.shop import ALL_SHOP_CATEGORIES, DEFAULT_SHOP_CATEGORY  # noqa: E402
from kream_reresell.rules import BidRules, Tier  # noqa: E402

WINDOW_WIDTH = 720  # '상품 고르기' 라디오 세 개(약 670px)가 한 줄에 다 보이는 폭
WINDOW_HEIGHT = 900
RIGHT_MARGIN = 40

MAX_SECTION_LINES_IN_POPUP = 20   # [재입찰] 완료 창에 보여줄 회차별 요약 줄 수 (100회면 창이 화면을 넘어감 - 나머지는 로그에)

# 랭킹 체크박스는 랭킹 칩 순서(ALL_CATEGORIES)대로 나열하고, 체크한 것을 그 순서대로 실행한다.
CATEGORY_COLUMNS = 6


# 같은 것을 손대는 작업 쌍 - 같이 돌리려 하면 한 번 더 묻는다
CONFLICTS = {
    frozenset({"입찰취소", "재입찰"}): "둘 다 마이페이지 구매 입찰 목록을 순서대로 손대므로 같은 입찰을 서로 바꾸거나 지우려다\n"
                                    "한쪽이 오류로 남을 수 있습니다 ([재입찰] 은 기준 미달 입찰도 지우므로 보통 [입찰취소] 를 따로 돌릴 필요가 없습니다).",
}


class QueueHandler(logging.Handler):
    """로그를 GUI 스레드로 넘기기 위한 핸들러 (큐 메시지는 모두 (종류, 작업 이름, 내용) 세 짝)."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", None, self.format(record)))


def tag_job_logs(job_names: tuple[str, ...]) -> None:
    """작업 스레드(이름 = 버튼 이름)에서 난 로그 레코드에 '[재입찰] ' 같은 머리(record.job)를 붙인다 - 여러 작업이 동시에 돌 때 구분용."""
    make = logging.getLogRecordFactory()

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = make(*args, **kwargs)
        record.job = f"[{record.threadName}] " if record.threadName in job_names else ""
        return record

    logging.setLogRecordFactory(factory)


@dataclass
class Job:
    """돌고 있는 작업 하나 (버튼 하나)."""
    name: str
    status: str = ""
    stop: threading.Event | None = None     # [중지] 로 멈추는 방법. None = 판매 관리 창처럼 자기 창 안에서만 멈춘다
    on_done: Callable[[str, object], None] | None = None   # ("done", 이름, 결과) 가 오면 부른다 - 기본은 App._finish

    @property
    def stoppable(self) -> bool:
        return self.stop is not None and not self.stop.is_set()


def _place_right_center(root: tk.Tk) -> None:
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{sw - WINDOW_WIDTH - RIGHT_MARGIN}+{(sh - WINDOW_HEIGHT) // 2}")


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("KREAM 리리셀")
        _place_right_center(root)
        root.minsize(700, 620)  # 세 화면(랭킹·검색·SHOP) 모두 700px 을 요구 - 더 좁히면 라디오·버튼 행이 잘림

        self.q: queue.Queue = queue.Queue()
        self.jobs: dict[str, Job] = {}          # 돌고 있는 작업 (이름 → Job). 버튼은 자기 작업이 도는 동안만 잠긴다
        self.last_status = "대기 중"            # 도는 작업이 없을 때 상태줄에 보일 문구 (마지막 완료 결과)
        self.last_report: Path | None = None
        self.base = Settings()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        pad = {"padx": 12, "pady": 4}

        # ---- 설정 영역
        frame = tk.LabelFrame(root, text="실행 설정")
        frame.pack(fill="x", padx=12, pady=(12, 4))

        # 상품을 어디서 고를지: 랭킹(순위대로) / 검색(검색 결과 순서대로) / SHOP(카테고리 목록 순서대로). 고른 쪽의 설정만 보인다
        src_row = tk.Frame(frame)
        src_row.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(src_row, text="상품 고르기:", font=("맑은 고딕", 9, "bold")).pack(side="left")
        self.source = tk.StringVar(value="ranking")
        for text, value in (("랭킹 (랭킹 탭의 순위대로)", "ranking"), ("검색 (검색 결과 순서대로)", "search"),
                            ("SHOP (카테고리 목록 순서대로)", "shop")):
            # anchor="w": 창이 좁아 잘려도 동그라미는 남고 글자 끝만 잘리게 (기본 center 는 양끝이 함께 잘림)
            tk.Radiobutton(src_row, text=text, variable=self.source, value=value, command=self._show_source,
                           anchor="w").pack(side="left", padx=(10, 0))
        self.source_area = tk.Frame(frame)
        self.source_area.pack(fill="x")

        # ---- 랭킹 화면: 전부 나열해 두고 체크한 것을 위→아래, 왼쪽→오른쪽(랭킹 칩 순서) 순으로 실행한다
        self.ranking_frame = tk.Frame(self.source_area)
        cat_frame = tk.LabelFrame(self.ranking_frame, text="랭킹 (체크한 것을 나열된 순서대로 실행)")
        cat_frame.pack(fill="x", padx=12, pady=(6, 2))
        self.category_vars: dict[str, tk.BooleanVar] = {}
        grid = tk.Frame(cat_frame)
        grid.pack(fill="x", padx=6, pady=(4, 2))
        for i, name in enumerate(ALL_CATEGORIES):
            var = tk.BooleanVar(value=(name == DEFAULT_CATEGORY))
            self.category_vars[name] = var
            cb = tk.Checkbutton(grid, text=name, variable=var, anchor="w")
            cb.grid(row=i // CATEGORY_COLUMNS, column=i % CATEGORY_COLUMNS, sticky="w", padx=(0, 6), pady=1)
        for col in range(CATEGORY_COLUMNS):
            grid.columnconfigure(col, weight=1)
        sel = tk.Frame(cat_frame)
        sel.pack(fill="x", padx=6, pady=(0, 4))
        tk.Button(sel, text="전체 선택", command=lambda: self._set_all_categories(True)).pack(side="left")
        tk.Button(sel, text="전체 해제", command=lambda: self._set_all_categories(False)).pack(side="left", padx=(6, 0))
        tk.Label(sel, text="※ 신발·의류처럼 사이즈 옵션이 있는 상품은 옵션마다 따로 판정해 입찰합니다 (보고서에 옵션마다 한 줄)",
                 fg="#888", anchor="w", justify="left", wraplength=500).pack(side="left", padx=(12, 0))

        row1 = tk.Frame(self.ranking_frame)
        row1.pack(fill="x", **pad)
        tk.Label(row1, text="랭킹마다 볼 상품 수").pack(side="left")
        self.limit = tk.Spinbox(row1, from_=1, to=200, width=6)
        self.limit.delete(0, "end")
        self.limit.insert(0, str(self.base.max_products))
        self.limit.pack(side="left", padx=(6, 0))
        tk.Label(row1, text="([입찰] 에만 쓰임)", fg="#888").pack(side="left", padx=(6, 0))

        # ---- 검색 화면: 검색어 + 시도할 상품 수 (+ 빠른배송 필터)
        self.search_frame = tk.Frame(self.source_area)
        s_frame = tk.LabelFrame(self.search_frame, text="검색 (검색 결과에 나온 순서대로 한 개씩 판정)")
        s_frame.pack(fill="x", padx=12, pady=(6, 2))
        s_row1 = tk.Frame(s_frame)
        s_row1.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(s_row1, text="검색어").pack(side="left")
        self.keyword_entry = tk.Entry(s_row1, width=34, font=("맑은 고딕", 10))
        self.keyword_entry.pack(side="left", padx=(6, 0))
        tk.Label(s_row1, text="(여러 개면 쉼표로 구분 - 적은 순서대로 검색)", fg="#888").pack(side="left", padx=(8, 0))
        s_row2 = tk.Frame(s_frame)
        s_row2.pack(fill="x", padx=6, pady=2)
        tk.Label(s_row2, text="시도할 상품 수").pack(side="left")
        self.search_limit = tk.Spinbox(s_row2, from_=1, to=500, width=6)
        self.search_limit.delete(0, "end")
        self.search_limit.insert(0, str(self.base.max_products))
        self.search_limit.pack(side="left", padx=(6, 0))
        tk.Label(s_row2, text="(입찰 성공 수가 아니라, 검색 결과 앞에서부터 판정해 볼 상품 수 - 검색어마다)",
                 fg="#888").pack(side="left", padx=(8, 0))
        s_row3 = tk.Frame(s_frame)
        s_row3.pack(fill="x", padx=6, pady=(2, 4))
        self.search_quick = tk.BooleanVar(value=self.base.search_quick_only)
        tk.Checkbutton(s_row3, text="빠른배송 판매자가 있는 상품만 (검색 결과의 '빠른배송' 필터, 권장)",
                       variable=self.search_quick, anchor="w").pack(side="left")
        tk.Label(s_frame, text="※ 빠른배송 판매자가 없는 상품은 A(빠른배송 가격)를 읽을 수 없어 어차피 건너뛰므로, 필터를 켜면 "
                               "그런 상품에 낭비되는 요청(사이트 스로틀 대상)을 아낍니다. 판정·입찰 규칙은 랭킹과 똑같습니다.",
                 fg="#888", anchor="w", justify="left", wraplength=580).pack(fill="x", padx=6, pady=(0, 6))

        # ---- SHOP 화면: 카테고리 체크(SHOP 탭 순서, 전체 제외) + 카테고리마다 시도할 상품 수 (+ 빠른배송 필터)
        self.shop_frame = tk.Frame(self.source_area)
        sh_frame = tk.LabelFrame(self.shop_frame, text="SHOP (체크한 카테고리를 나열된 순서대로, 목록에 나온 순서대로 한 개씩 판정)")
        sh_frame.pack(fill="x", padx=12, pady=(6, 2))
        self.shop_vars: dict[str, tk.BooleanVar] = {}
        sh_grid = tk.Frame(sh_frame)
        sh_grid.pack(fill="x", padx=6, pady=(4, 2))
        for i, name in enumerate(ALL_SHOP_CATEGORIES):
            var = tk.BooleanVar(value=(name == DEFAULT_SHOP_CATEGORY))
            self.shop_vars[name] = var
            cb = tk.Checkbutton(sh_grid, text=name, variable=var, anchor="w")
            cb.grid(row=i // CATEGORY_COLUMNS, column=i % CATEGORY_COLUMNS, sticky="w", padx=(0, 6), pady=1)
        for col in range(CATEGORY_COLUMNS):
            sh_grid.columnconfigure(col, weight=1)
        sh_sel = tk.Frame(sh_frame)
        sh_sel.pack(fill="x", padx=6, pady=(0, 4))
        tk.Button(sh_sel, text="전체 선택", command=lambda: self._set_all_shop(True)).pack(side="left")
        tk.Button(sh_sel, text="전체 해제", command=lambda: self._set_all_shop(False)).pack(side="left", padx=(6, 0))
        sh_row2 = tk.Frame(sh_frame)
        sh_row2.pack(fill="x", padx=6, pady=2)
        tk.Label(sh_row2, text="시도할 상품 수").pack(side="left")
        self.shop_limit = tk.Spinbox(sh_row2, from_=1, to=500, width=6)
        self.shop_limit.delete(0, "end")
        self.shop_limit.insert(0, str(self.base.max_products))
        self.shop_limit.pack(side="left", padx=(6, 0))
        tk.Label(sh_row2, text="(입찰 성공 수가 아니라, 목록 앞에서부터 판정해 볼 상품 수 - 카테고리마다)",
                 fg="#888").pack(side="left", padx=(8, 0))
        sh_row3 = tk.Frame(sh_frame)
        sh_row3.pack(fill="x", padx=6, pady=(2, 4))
        self.shop_quick = tk.BooleanVar(value=self.base.shop_quick_only)
        tk.Checkbutton(sh_row3, text="빠른배송 판매자가 있는 상품만 (목록의 '빠른배송' 필터, 권장)",
                       variable=self.shop_quick, anchor="w").pack(side="left")
        tk.Label(sh_frame, text="※ 사이트 상단 SHOP 의 카테고리 탭(전체 제외)을 그대로 나열했습니다. 목록에는 빠른배송이 없는 상품도 섞여 있어 "
                                "필터를 켜면 그런 상품에 낭비되는 요청(사이트 스로틀 대상)을 아낍니다. 판정·입찰 규칙은 랭킹과 똑같습니다.",
                 fg="#888", anchor="w", justify="left", wraplength=580).pack(fill="x", padx=6, pady=(0, 6))
        self._show_source()

        # 재입찰: 몇 바퀴 돌지 + 바퀴 시작 간격
        row_rebid = tk.Frame(frame)
        row_rebid.pack(fill="x", **pad)
        tk.Label(row_rebid, text="재입찰 횟수(회)").pack(side="left")
        self.rebid_cycles = tk.Spinbox(row_rebid, from_=0, to=999, width=5)
        self.rebid_cycles.delete(0, "end")
        self.rebid_cycles.insert(0, str(self.base.rebid_cycles))
        self.rebid_cycles.pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text="(0 = [중지]까지 계속)", fg="#888").pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text="판매 하한 마진(%)").pack(side="left", padx=(18, 0))
        self.sell_margin = tk.Spinbox(row_rebid, from_=0, to=99, increment=1, width=5)
        self.sell_margin.delete(0, "end")
        self.sell_margin.insert(0, f"{self.base.sell_margin_rate * 100:g}")
        self.sell_margin.pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text="(하한 = 매입가 × (1 + 이 %), 그 아래로는 안 내림)", fg="#888").pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text="시세 조회 간격(초)").pack(side="left", padx=(18, 0))
        self.api_tick = tk.Spinbox(row_rebid, from_=int(pacing.API_TICK_MIN_SEC), to=int(pacing.API_TICK_MAX_SEC), width=5)
        self.api_tick.delete(0, "end")
        self.api_tick.insert(0, f"{self.base.api_tick_sec:g}")
        self.api_tick.pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text=f"({pacing.API_TICK_MIN_SEC:g}~{pacing.API_TICK_MAX_SEC:g}초, 상품·입찰 하나에 호출 하나)",
                 fg="#888").pack(side="left", padx=(6, 0))

        row2 = tk.Frame(frame)
        row2.pack(fill="x", **pad)
        self.mode = tk.StringVar(value="real")
        tk.Radiobutton(row2, text="실제 실행 (입찰 / 입찰취소 / 재입찰)", variable=self.mode, value="real").pack(side="left")
        tk.Radiobutton(row2, text="판단만 (입찰·취소·변경 안 함)", variable=self.mode, value="dry").pack(side="left", padx=(12, 0))
        self.show_chrome = tk.BooleanVar(value=self.base.show_chrome)
        tk.Checkbutton(row2, text="크롬 창 보기", variable=self.show_chrome,
                       command=self.toggle_chrome_window).pack(side="right")

        cond = (f"조건: 최근 {self.base.lookback_days}일 빠른배송 {self.base.min_fast_sales}건 이상 · "
                f"마진 (S−B) > S×[아래 입찰 기준의 구간별 %], S = 예상 판매가 = min(A 빠른배송가, R 최근 체결 15건 최저가) · 입찰 {self.base.bid_days}일 · 창고보관 · 포인트 최대 사용")
        tk.Label(frame, text=cond, fg="#555", anchor="w", justify="left", wraplength=580).pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(frame, text="(거래량·기간·입찰기한은 프로젝트 폴더의 .env 에서 바꿉니다. 랭킹·검색어·SHOP 카테고리·상품 수는 [입찰]에만, "
                             "재입찰 횟수는 [재입찰]에만, 시세 조회 간격은 [입찰]·[재입찰]에 쓰입니다)",
                 fg="#888", anchor="w", justify="left", wraplength=600).pack(fill="x", padx=12, pady=(0, 6))

        # ---- 입찰 기준 (금액 구간별 마진율 + 입찰가 상한)
        self.tier_rows: list[dict] = []
        self._build_rules_panel(root)

        # ---- 버튼
        buttons = tk.Frame(root)
        buttons.pack(fill="x", padx=12, pady=4)
        def big_button(text: str, bg: str, active_bg: str, command, first: bool = False) -> tk.Button:
            b = tk.Button(buttons, text=text, width=10, height=2, font=("맑은 고딕", 11, "bold"),
                          bg=bg, fg="white", activebackground=active_bg, activeforeground="white", command=command)
            b.pack(side="left", padx=(0 if first else 8, 0))
            return b

        # 작업 이름 → 버튼. 이름은 그대로 작업 스레드 이름·로그 머리·상태줄에 쓰인다 (sell.SellEngine 의 스레드 이름 "판매" 도 여기와 같아야 함)
        self.buttons: dict[str, tk.Button] = {
            "입찰": big_button("입찰", "#222", "#444", self.start, first=True),
            "입찰취소": big_button("입찰취소", "#8B0000", "#B22222", self.start_cancel),
            "재입찰": big_button("재입찰", "#B36B00", "#D98C1F", self.start_rebid),
            "내역": big_button("내역", "#1F4E79", "#2E75B6", self.start_history),
            "판매": big_button("판매", "#2E7D32", "#43A047", self.start_sell),
        }
        self.stop_button = tk.Button(buttons, text="중지 (지금 것까지만)", width=18, height=2, state="disabled",
                                     command=self.request_stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        # 상태줄은 버튼 아래 한 줄 - 여러 작업이 동시에 돌면 "[재입찰] … | [내역] …" 처럼 다 보인다
        self.status = tk.Label(root, text="대기 중", fg="#333", anchor="w", justify="left", wraplength=WINDOW_WIDTH - 40)
        self.status.pack(fill="x", padx=12, pady=(0, 2))

        # ---- 로그
        log_frame = tk.LabelFrame(root, text="진행 상황")
        log_frame.pack(fill="both", expand=True, padx=12, pady=4)
        self.log_box = tk.Text(log_frame, height=10, wrap="word", state="disabled", font=("맑은 고딕", 9))
        scroll = tk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # ---- 하단
        bottom = tk.Frame(root)
        bottom.pack(fill="x", padx=12, pady=(4, 12))
        tk.Button(bottom, text="결과 폴더 열기", command=self.open_report_dir).pack(side="left")
        self.open_report_button = tk.Button(bottom, text="이번 보고서 열기", state="disabled", command=self.open_last_report)
        self.open_report_button.pack(side="left", padx=(8, 0))
        tk.Label(bottom, text=f"보고서: {REPORT_DIR}", fg="#888").pack(side="right")

        self._setup_logging()
        self.root.after(200, self._poll)

    # ------------------------------------------------------------ 로깅
    def _setup_logging(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        tag_job_logs(tuple(self.buttons))
        fmt = "%(asctime)s %(levelname)s %(job)s%(message)s"
        handler = QueueHandler(self.q)
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
        file_handler = logging.FileHandler(LOG_DIR / f"gui_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt))
        rootlog = logging.getLogger()
        rootlog.setLevel(logging.INFO)
        rootlog.addHandler(handler)
        rootlog.addHandler(file_handler)
        logging.getLogger("kream_reresell").setLevel(logging.INFO)

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------ 입찰 기준
    def _build_rules_panel(self, parent: tk.Misc) -> None:
        panel = tk.LabelFrame(parent, text="입찰 기준 (A = 빠른배송 가격, B = 즉시 판매가)")
        panel.pack(fill="x", padx=12, pady=4)

        self.tier_grid = tk.Frame(panel)
        self.tier_grid.pack(fill="x", padx=12, pady=(6, 2))
        for col, text in enumerate(("S 부터 (원)", "S 미만 (원, 비우면 끝없음)", "최소 마진율 (%)")):
            tk.Label(self.tier_grid, text=text, fg="#555").grid(row=0, column=col, sticky="w", padx=(0, 10))

        row = tk.Frame(panel)
        row.pack(fill="x", padx=12, pady=(2, 2))
        tk.Button(row, text="구간 추가", command=self._add_tier_row_after_last).pack(side="left")
        tk.Label(row, text="상품 금액 상한: A 가").pack(side="left", padx=(16, 4))
        self.limit_entry = tk.Entry(row, width=10, justify="right")
        self.limit_entry.pack(side="left")
        tk.Label(row, text="원을 넘으면 바로 건너뜀 (비우면 제한 없음)").pack(side="left", padx=(4, 0))
        tk.Button(row, text="기준 저장", command=self.save_rules).pack(side="right")

        tk.Label(panel, text="※ A 가 어느 구간에도 없으면 건너뜁니다. 상품 금액 상한을 넘는 상품은 B 도 읽지 않고 넘어갑니다 "
                             "(이미 넣은 입찰을 다시 판정하는 [입찰취소] 에는 상한을 쓰지 않습니다). "
                             "[입찰]/[입찰취소] 를 누를 때 자동 저장되어 명령행 실행에도 쓰입니다.",
                 fg="#888", anchor="w", justify="left", wraplength=600).pack(fill="x", padx=12, pady=(0, 6))
        self._load_rules_into_panel(self.base.rules)

    def _load_rules_into_panel(self, rules: BidRules) -> None:
        for r in list(self.tier_rows):
            self._remove_tier_row(r)
        for t in rules.tiers:
            self._add_tier_row(str(t.lo), "" if t.hi is None else str(t.hi), f"{t.margin_pct:g}")
        self.limit_entry.delete(0, "end")
        if rules.max_price_a is not None:
            self.limit_entry.insert(0, str(rules.max_price_a))

    def _add_tier_row(self, lo: str = "", hi: str = "", pct: str = "") -> None:
        widgets = {}
        for key, val, width in (("lo", lo, 12), ("hi", hi, 12), ("pct", pct, 7)):
            e = tk.Entry(self.tier_grid, width=width, justify="right")
            e.insert(0, val)
            widgets[key] = e
        row = {"widgets": widgets}
        widgets["del"] = tk.Button(self.tier_grid, text="삭제", command=lambda: self._remove_tier_row(row))
        self.tier_rows.append(row)
        self._regrid_tier_rows()

    def _add_tier_row_after_last(self) -> None:
        """새 구간의 '부터' 는 마지막 구간의 '미만' 값으로 채운다."""
        lo = self.tier_rows[-1]["widgets"]["hi"].get().strip() if self.tier_rows else "0"
        self._add_tier_row(lo, "", "")

    def _remove_tier_row(self, row: dict) -> None:
        for w in row["widgets"].values():
            w.destroy()
        self.tier_rows.remove(row)
        self._regrid_tier_rows()

    def _regrid_tier_rows(self) -> None:
        for i, row in enumerate(self.tier_rows, start=1):
            w = row["widgets"]
            w["lo"].grid(row=i, column=0, sticky="w", padx=(0, 10), pady=1)
            w["hi"].grid(row=i, column=1, sticky="w", padx=(0, 10), pady=1)
            w["pct"].grid(row=i, column=2, sticky="w", padx=(0, 10), pady=1)
            w["del"].grid(row=i, column=3, sticky="w", pady=1)

    def _read_rules(self) -> BidRules:
        """표의 값을 읽어 검사한다. 잘못됐으면 ValueError (메시지는 사용자에게 보여줄 문장)."""
        tiers = []
        for i, row in enumerate(self.tier_rows, start=1):
            w = row["widgets"]
            lo, hi, pct = (w[k].get().replace(",", "").strip() for k in ("lo", "hi", "pct"))
            if not lo and not hi and not pct:
                continue
            try:
                tiers.append(Tier(lo=int(lo or 0), hi=int(hi) if hi else None, margin_pct=float(pct)))
            except ValueError as e:
                raise ValueError(f"{i}번째 구간의 숫자를 확인해 주세요 (부터 {lo!r}, 미만 {hi!r}, 마진 {pct!r})") from e
        limit = self.limit_entry.get().replace(",", "").replace("원", "").strip()
        try:
            max_price_a = int(limit) if limit else None
        except ValueError as e:
            raise ValueError(f"상품 금액 상한은 숫자(원)로 넣어주세요: {limit!r}") from e
        rules = BidRules(tiers=tiers, max_price_a=max_price_a)
        rules.validate()
        return rules

    def _apply_rules(self) -> BidRules | None:
        """표를 읽어 저장하고 돌려준다. 잘못됐으면 안내창을 띄우고 None."""
        try:
            rules = self._read_rules()
            rules.save(RULES_PATH)
        except ValueError as e:
            messagebox.showerror("입찰 기준 오류", str(e))
            return None
        self._load_rules_into_panel(rules)   # 정렬된 순서로 다시 보여준다
        return rules

    def save_rules(self) -> None:
        rules = self._apply_rules()
        if rules:
            self._log(f"입찰 기준 저장: {rules.describe()}  ({RULES_PATH})")

    # ------------------------------------------------------------ 실행
    def _show_source(self) -> None:
        """'상품 고르기' 에서 고른 쪽(랭킹 / 검색 / SHOP)의 설정만 보인다."""
        frames = {"ranking": self.ranking_frame, "search": self.search_frame, "shop": self.shop_frame}
        chosen = self.source.get()
        for key, fr in frames.items():
            if key != chosen:
                fr.pack_forget()
        frames.get(chosen, self.ranking_frame).pack(fill="x")
        if chosen == "search":
            self.keyword_entry.focus_set()

    def _set_all_categories(self, value: bool) -> None:
        for var in self.category_vars.values():
            var.set(value)

    def _set_all_shop(self, value: bool) -> None:
        for var in self.shop_vars.values():
            var.set(value)

    def selected_shop_categories(self) -> list[str]:
        """체크된 SHOP 카테고리를 나열된(SHOP 탭) 순서대로."""
        return [name for name in ALL_SHOP_CATEGORIES if self.shop_vars[name].get()]

    def selected_categories(self) -> list[str]:
        """체크된 랭킹을 나열된(랭킹 칩) 순서대로."""
        return [name for name in ALL_CATEGORIES if self.category_vars[name].get()]

    def start(self) -> None:
        if "입찰" in self.jobs:
            return
        source = self.source.get()
        searching = source == "search"
        shopping = source == "shop"
        limit_box = self.search_limit if searching else self.shop_limit if shopping else self.limit
        try:
            limit = int(limit_box.get())
        except ValueError:
            messagebox.showerror("입력 오류", "상품 수는 숫자로 넣어주세요.")
            return
        if limit < 1:
            messagebox.showerror("입력 오류", "상품 수는 1 이상이어야 합니다.")
            return
        categories: list[str] = []
        keywords: list[str] = []
        shop_categories: list[str] = []
        if shopping:
            shop_categories = self.selected_shop_categories()
            if not shop_categories:
                messagebox.showerror("입력 오류", "SHOP 카테고리를 하나 이상 체크해 주세요.")
                return
            quick = self.shop_quick.get()
            src_text = " → ".join(shop_categories)
            what = (f"SHOP 카테고리 {len(shop_categories)}개를 순서대로 돌며 각각 목록 앞에서부터 {limit}개"
                    f"{' (빠른배송 판매자가 있는 상품만)' if quick else ''} 중 조건에 맞는 상품에")
            log_head = f"SHOP {src_text} / 카테고리마다 {limit}개 시도{' (빠른배송 필터)' if quick else ' (필터 없음)'}"
        elif searching:
            keywords = normalize_keywords(self.keyword_entry.get())
            if not keywords:
                messagebox.showerror("입력 오류", "검색어를 넣어주세요.")
                self.keyword_entry.focus_set()
                return
            quick = self.search_quick.get()
            src_text = " → ".join(keywords)
            what = (f"검색 {len(keywords)}개를 순서대로 돌며 각각 검색 결과 앞에서부터 {limit}개"
                    f"{' (빠른배송 판매자가 있는 상품만)' if quick else ''} 중 조건에 맞는 상품에")
            log_head = f"검색 {src_text} / 검색어마다 {limit}개 시도{' (빠른배송 필터)' if quick else ' (필터 없음)'}"
        else:
            categories = self.selected_categories()
            if not categories:
                messagebox.showerror("입력 오류", "랭킹을 하나 이상 체크해 주세요.")
                return
            src_text = " → ".join(categories)
            what = f"랭킹 {len(categories)}개를 순서대로 돌며 각각 상위 {limit}개 중 조건에 맞는 상품에"
            log_head = f"{src_text} / 랭킹마다 상위 {limit}개"
        tick = self._read_tick_sec()
        if tick is None:
            return
        rules = self._apply_rules()
        if rules is None:
            return
        dry = self.mode.get() == "dry"
        if not dry and not messagebox.askyesno(
                "실제 입찰", f"{what} 실제로 구매 입찰을 넣습니다.\n\n{src_text}\n\n{rules.describe()}\n\n"
                             "배송방법은 창고보관, 포인트는 최대 사용입니다.\n\n진행할까요?"):
            return

        settings = self._make_settings(dry, rules, tick)
        settings.max_products = limit
        if searching:
            settings.search_quick_only = self.search_quick.get()
        if shopping:
            settings.shop_quick_only = self.shop_quick.get()
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {log_head}, "
                  f"{'판단만' if dry else '실제 입찰'} =====\n입찰 기준: {rules.describe()}")
        self._start_job("입찰", "실행 중...",
                        lambda stop, status: run_job(settings, categories, keywords=keywords, shop_categories=shop_categories,
                                                     should_stop=stop.is_set, on_status=status))

    # ------------------------------------------------------------ 작업(스레드) 공통
    def _start_job(self, name: str, status: str, target, on_done: Callable[[str, object], None] | None = None) -> None:
        """target(stop, on_status) 를 이름이 name 인 스레드에서 돌린다. 끝나면 ("done", name, 결과) 또는 ("error", name, 문구) 가 큐로 온다.

        같은 이름의 작업은 하나만 돈다 (버튼이 잠긴다). 다른 이름의 작업과는 동시에 돈다 - 크롬은 browser 가 나눠 쓴다.
        """
        job = Job(name, status, stop=threading.Event(), on_done=on_done or self._finish)
        if not self._add_job(job):
            return

        def run() -> None:
            try:
                result = target(job.stop, lambda text: self.q.put(("status", name, text)))
                self.q.put(("done", name, result))
            except Exception as e:  # noqa: BLE001
                logging.getLogger("gui").exception("%s 중 오류", name)
                self.q.put(("error", name, f"{type(e).__name__}: {e}"))

        threading.Thread(target=run, name=name, daemon=True).start()

    def _add_job(self, job: Job) -> bool:
        """작업을 장부에 올리고 버튼을 잠근다. 같은 이름이 이미 돌고 있으면 False."""
        if job.name in self.jobs:
            return False
        self.jobs[job.name] = job
        self._refresh_buttons()
        return True

    def _end_job(self, name: str, text: str) -> None:
        """작업이 끝났다 - 버튼을 풀고 상태줄에 결과를 남긴다."""
        if self.jobs.pop(name, None):
            self.last_status = f"{name} {text}"
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        """버튼 잠금·[중지]·상태줄을 지금 도는 작업들에 맞춘다 (작업이 시작·종료·중지될 때)."""
        for name, button in self.buttons.items():
            button.configure(state="disabled" if name in self.jobs else "normal")
        self.stop_button.configure(state="normal" if self._stoppable() else "disabled")
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self.jobs:
            self.status.configure(text=" | ".join(f"[{j.name}] {j.status}" for j in self.jobs.values()))
        else:
            self.status.configure(text=self.last_status)

    def _stoppable(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.stoppable]

    def _confirm_conflicts(self, name: str) -> bool:
        """name 과 같은 것을 손대는 작업(CONFLICTS)이 돌고 있으면 같이 돌릴지 묻는다."""
        for other in self.jobs:
            why = CONFLICTS.get(frozenset({name, other}))
            if why and not messagebox.askyesno(name, f"[{other}] 이 지금 돌고 있습니다. {why}\n\n그래도 [{name}] 을 같이 돌릴까요?"):
                return False
        return True

    def start_cancel(self) -> None:
        """마이페이지 구매 입찰 목록을 순서대로 다시 판정해 기준 미달 입찰을 지운다."""
        tick = self._read_tick_sec()
        if tick is None:
            return
        rules = self._apply_rules()
        if rules is None:
            return
        dry = self.mode.get() == "dry"
        if not dry and not messagebox.askyesno(
                "입찰취소", "마이페이지 > 구매 내역 > 구매 입찰 목록을 순서대로 보며, 상품마다 입찰할 때와 같은 기준으로\n"
                          f"다시 판정합니다 (최근 {self.base.lookback_days}일 빠른배송 {self.base.min_fast_sales}건 이상,\n"
                          f"{rules.describe()}).\n\n"
                          "기준에 못 미치는 입찰은 실제로 지웁니다 (되돌릴 수 없음).\n\n진행할까요?"):
            return
        if not self._confirm_conflicts("입찰취소"):
            return
        settings = self._make_settings(dry, rules, tick)
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 입찰취소 시작: 구매 입찰 목록 전체, "
                  f"{'판단만' if dry else '기준 미달 입찰 지움'} =====\n입찰 기준: {rules.describe()}")
        self._start_job("입찰취소", "입찰취소 실행 중...", lambda stop, _status: run_cancel_job(settings, should_stop=stop.is_set))

    def start_rebid(self) -> None:
        """[재입찰]: 구매 입찰 목록을 정한 횟수만큼(0 이면 [중지] 까지) 돌며 밀린 입찰의 희망가를 [입찰 변경하기] 로 최신 B 로 올린다."""
        try:
            cycles = int(self.rebid_cycles.get())
        except ValueError:
            messagebox.showerror("입력 오류", "재입찰 횟수는 정수(회)로 넣어주세요. 0 이면 [중지] 를 누를 때까지 계속 돕니다.")
            return
        if cycles < 0:
            messagebox.showerror("입력 오류", "재입찰 횟수는 0(계속) 또는 1 이상이어야 합니다.")
            return
        tick = self._read_tick_sec()
        if tick is None:
            return
        rules = self._apply_rules()
        if rules is None:
            return
        dry = self.mode.get() == "dry"
        if cycles == 0:
            repeat = f"[중지] 를 누를 때까지 계속 반복 (입찰 하나에 {tick:g}초, 회차 사이 쉼 없음)"
        elif cycles == 1:
            repeat = f"구매 입찰 목록을 한 바퀴만 돌고 끝 (입찰 하나에 {tick:g}초)"
        else:
            repeat = f"{cycles}회 돌고 끝 (입찰 하나에 {tick:g}초, 회차 사이 쉼 없음)"
        if not dry and not messagebox.askyesno(
                "재입찰", "마이페이지 > 구매 내역 > 구매 입찰 목록을 순서대로 보며, 입찰마다 시세 API 로 최신 A·B 를 읽어\n"
                        "즉시 판매가가 내 희망가보다 높아진(밀린) 입찰을 상품 페이지에서 처음 입찰 때와 같은 기준으로 다시 판정하고\n"
                        f"(최근 {self.base.lookback_days}일 빠른배송 {self.base.min_fast_sales}건 이상, {rules.describe()}),\n"
                        f"충족하면 [입찰 변경하기] 로 희망가를 최신 즉시 판매가로 올립니다 (마감 {self.base.bid_days}일, 창고보관).\n"
                        "기준에 못 미쳐 올릴 수 없는 입찰과, 밀렸는데 변경 화면이 예상과 달라 못 올린 입찰은 실제로 지웁니다 (되돌릴 수 없음).\n\n"
                        f"{repeat}합니다 (횟수는 설정의 '재입찰 횟수' 칸, 도는 중에도 [중지] 로 멈출 수 있음).\n\n진행할까요?"):
            return
        if not self._confirm_conflicts("재입찰"):
            return
        settings = self._make_settings(dry, rules, tick)
        settings.rebid_cycles = cycles
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 재입찰 시작: {repeat} "
                  f"({'판단만' if dry else '밀린 입찰의 희망가를 올림'}) =====\n입찰 기준: {rules.describe()}")
        self._start_job("재입찰", "재입찰 실행 중...",
                        lambda stop, status: run_rebid_job(settings, should_stop=stop.is_set, on_status=status))

    def start_sell(self) -> None:
        """[판매]: 판매 관리 창을 연다 - 보관 판매 항목의 희망가를 하한(매입가 + 마진) 위에서 최저가 경쟁으로 맞춘다."""
        try:
            margin = float(self.sell_margin.get()) / 100.0
        except ValueError:
            messagebox.showerror("입력 오류", "판매 하한 마진은 숫자(%)로 넣어주세요.")
            return
        if not 0 <= margin < 1:
            messagebox.showerror("입력 오류", "판매 하한 마진은 0 이상 100 미만(%)이어야 합니다.")
            return
        tick = self._read_tick_sec()
        if tick is None:
            return
        rules = self._apply_rules()
        if rules is None:
            return
        dry = self.mode.get() == "dry"
        settings = self._make_settings(dry, rules, tick)
        settings.sell_margin_rate = margin
        settings.sell_cycles = 0
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 판매 관리 창 열림: 하한 마진 {margin * 100:g}%, "
                  f"{'판단만' if dry else '경쟁 시작을 누르면 판매 희망가를 실제로 바꿈'} =====")
        # 창이 자기 스레드로 크롬에 붙는다 - 창 안의 [정지] 로 멈추고, 창을 닫으면 작업이 끝난다 ([중지] 대상이 아님)
        if self._add_job(Job("판매", "판매 관리 창 열림")):
            SellWindow(self.root, settings, on_close=lambda: self._end_job("판매", "관리 창 닫힘"))

    def start_history(self) -> None:
        """[내역]: 달을 고르면 보관 판매 거래일시가 그 달인 판매를 구매 내역과 짝지어 엑셀로 저장한다."""
        choice = MonthDialog(self.root).show()
        if choice is None:
            return
        year, month = choice
        settings = Settings(show_chrome=self.show_chrome.get())
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 내역 정리 시작: {year}년 {month}월 "
                  f"(보관 판매 거래일시 기준, 구매 내역과 짝 맞춤) =====")
        self._start_job("내역", f"{year}년 {month}월 내역 정리 중...",
                        lambda stop, _status: run_history_job(settings, year, month, should_stop=stop.is_set),
                        on_done=self._finish_history)

    def _finish_history(self, name: str, job) -> None:
        self.last_report = job.report_path
        self.open_report_button.configure(state="normal")
        r = job.result
        summary = f"{r.year}년 {r.month}월 판매 {len(r.sales)}건"
        if r.unmatched:
            summary += f" (매입 내역 못 찾음 {len(r.unmatched)}건 - 엑셀의 노란 줄)"
        self._end_job(name, f"완료: {summary}")
        self._log(f"===== 완료 - {summary}\n엑셀: {job.report_path}")
        messagebox.showinfo("내역 정리 완료", f"{summary}\n\n엑셀이 저장되었습니다:\n{job.report_path}")

    def _make_settings(self, dry: bool, rules: BidRules, tick_sec: float) -> Settings:
        settings = Settings(dry_run=dry, show_chrome=self.show_chrome.get(), rules=rules)
        settings.api_tick_sec = tick_sec
        return settings

    def _read_tick_sec(self) -> float | None:
        """설정칸의 시세 조회 간격(초). 잘못 넣었으면 알리고 None."""
        try:
            tick = float(self.api_tick.get())
        except ValueError:
            messagebox.showerror("입력 오류", "시세 조회 간격은 숫자(초)로 넣어주세요.")
            return None
        if not pacing.API_TICK_MIN_SEC <= tick <= pacing.API_TICK_MAX_SEC:
            messagebox.showerror("입력 오류", f"시세 조회 간격은 {pacing.API_TICK_MIN_SEC:g}~{pacing.API_TICK_MAX_SEC:g}초 사이여야 합니다 "
                                          "(너무 빠르면 사이트가 막을 수 있습니다).")
            return None
        return tick

    def toggle_chrome_window(self) -> None:
        """실행 중이면 크롬 창을 바로 불러오거나 치운다. 대기 중이면 다음 실행에만 반영된다."""
        show = self.show_chrome.get()
        moved = browser.show_window() if show else browser.hide_window()
        if moved:
            self._log("크롬 창을 화면으로 불러왔습니다 (창을 조작하지는 마세요)" if show else "크롬 창을 화면 밖으로 치웠습니다")

    def request_stop(self) -> None:
        """[중지]: 도는 작업이 하나면 그것을, 여럿이면 어느 것을 멈출지 물어서 멈춘다 (판매 관리 창은 창 안의 [정지] 로)."""
        stoppable = self._stoppable()
        if not stoppable:
            return
        if len(stoppable) == 1:
            self._stop_job(stoppable[0])
            return
        chosen = StopDialog(self.root, [j.name for j in stoppable]).show()
        for job in stoppable:
            if chosen in ("전부", job.name):
                self._stop_job(job)

    def _stop_job(self, job: Job) -> None:
        job.stop.set()
        job.status = "지금 것까지 보고 멈춥니다..."
        self._refresh_buttons()

    def _on_close(self) -> None:
        """창 닫기. 실행 중이면 확인을 받고 끝낸다 - 작업 스레드는 daemon 이라 창이 닫히면 같이 사라지고, 크롬은 Job Object 로
        묶여 있어 이 프로세스가 끝나면 같이 닫힌다 (browser.py 참고). 멈춰 버린 실행도 이 경로로 끝낼 수 있다."""
        if self.jobs and not messagebox.askyesno(
                "종료", f"아직 실행 중입니다 ({', '.join(self.jobs)}). 지금 끝내면 진행 중인 작업이 끊기고 크롬도 같이 닫힙니다.\n\n"
                        "([중지] 를 누르면 지금 보는 상품까지 마치고 멈춥니다)\n\n그래도 끝낼까요?"):
            return
        for job in self._stoppable():
            job.stop.set()
        self.root.destroy()

    # ------------------------------------------------------------ 큐 처리
    def _poll(self) -> None:
        try:
            while True:
                kind, name, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    job = self.jobs.get(name)
                    if job and job.stoppable:   # 중지를 눌렀으면 "멈춥니다..." 표시를 유지
                        job.status = payload
                        self._refresh_status()
                elif kind == "done":
                    self.jobs[name].on_done(name, payload)
                elif kind == "error":
                    self._end_job(name, "오류로 중단")
                    messagebox.showerror(f"{name} 오류", payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _finish(self, name: str, job) -> None:
        self.last_report = job.report_path
        self.open_report_button.configure(state="normal")
        summary = summarize(job.results, unit="개")
        self._end_job(name, f"완료: {summary}")
        # [재입찰]은 회차별로도 나눠 보여준다 (사용자 요청 2026-09-13). 회차가 많으면 완료 창에는 마지막 몇 회차만, 로그에는 전부
        lines = section_lines(job.results, unit="개") if job.section_label else []
        self._log(f"===== 완료 ({job.mode}) - 전체: {summary}" + "".join(f"\n  {line}" for line in lines)
                  + f"\n보고서: {job.report_path}")
        popup = ""
        if lines:
            hidden = len(lines) - MAX_SECTION_LINES_IN_POPUP
            head = [f"... (앞 {hidden}{job.section_label}는 아래 로그와 엑셀 참고)"] if hidden > 0 else []
            popup = f"\n\n{job.section_label}별:\n" + "\n".join(head + lines[-MAX_SECTION_LINES_IN_POPUP:])
        messagebox.showinfo("완료", f"{job.mode}\n전체: {summary}{popup}\n\n보고서가 저장되었습니다:\n{job.report_path}")

    # ------------------------------------------------------------ 파일 열기
    def open_report_dir(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(REPORT_DIR))  # type: ignore[attr-defined]

    def open_last_report(self) -> None:
        if self.last_report and self.last_report.exists():
            os.startfile(str(self.last_report))  # type: ignore[attr-defined]


class Dialog:
    """부모 창 위에 뜨는 작은 확인 창의 공통 뼈대: 크기 고정, 부모에 묶임(transient·grab), Escape·창 닫기 = 취소, show() 로 결과."""

    def __init__(self, parent: tk.Tk, title: str) -> None:
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.resizable(False, False)
        self.top.transient(parent)
        self.top.grab_set()
        self.top.bind("<Escape>", lambda _e: self._cancel())
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

    def _center_on(self, parent: tk.Tk) -> None:
        """위젯을 다 붙인 뒤 부른다 - 부모 창 가운데(조금 위)에 띄운다."""
        self.top.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.top.winfo_width(), self.top.winfo_height()
        self.top.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")

    def _pick(self, result) -> None:
        self.result = result
        self.top.destroy()

    def _cancel(self) -> None:
        self._pick(None)

    def show(self):
        self.top.wait_window()
        return self.result


class StopDialog(Dialog):
    """[중지] 를 눌렀는데 작업이 여럿 돌고 있을 때: 어느 것을 멈출지 고른다. 작업 이름 / "전부" / 취소면 None."""

    def __init__(self, parent: tk.Tk, names: list[str]) -> None:
        super().__init__(parent, "중지 - 어느 작업을 멈출까요?")
        tk.Label(self.top, text="지금 보는 것까지 마치고 멈춥니다.", anchor="w").pack(fill="x", padx=16, pady=(14, 8))
        row = tk.Frame(self.top)
        row.pack(fill="x", padx=16, pady=(0, 14))
        for i, name in enumerate([*names, "전부"]):
            tk.Button(row, text=name, width=9, font=("맑은 고딕", 10, "bold" if name == "전부" else "normal"),
                      command=lambda n=name: self._pick(n)).pack(side="left", padx=(0 if i == 0 else 6, 0))
        tk.Button(row, text="취소", width=7, command=self._cancel).pack(side="left", padx=(12, 0))
        self._center_on(parent)


class MonthDialog(Dialog):
    """[내역] 을 누르면 뜨는 창: 연도와 달을 고르고 [실행] 으로 확인한다. 취소하면 None."""

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent, "내역 정리 - 달 선택")
        today = datetime.now()
        # 보통 지난달을 정리하므로 지난달을 기본으로 둔다
        last_year, last_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)

        tk.Label(self.top, text="보관 판매 > 보관 상세의 거래일시가 고른 달인 판매를 정리합니다.\n"
                                "구매 내역(종료) 에서 매입 건을 찾아 짝짓고, 바탕화면에 엑셀로 저장합니다.",
                 justify="left", anchor="w").pack(fill="x", padx=16, pady=(14, 8))

        row = tk.Frame(self.top)
        row.pack(fill="x", padx=16)
        tk.Label(row, text="연도").pack(side="left")
        self.year = tk.Spinbox(row, from_=2020, to=today.year + 1, width=6, justify="center")
        self.year.delete(0, "end")
        self.year.insert(0, str(last_year))
        self.year.pack(side="left", padx=(6, 0))

        grid = tk.LabelFrame(self.top, text="달")
        grid.pack(fill="x", padx=16, pady=(8, 4))
        self.month = tk.IntVar(value=last_month)
        for m in range(1, 13):
            tk.Radiobutton(grid, text=f"{m}월", variable=self.month, value=m, width=5, anchor="w") \
                .grid(row=(m - 1) // 6, column=(m - 1) % 6, sticky="w", padx=4, pady=2)

        buttons = tk.Frame(self.top)
        buttons.pack(fill="x", padx=16, pady=(8, 14))
        tk.Button(buttons, text="실행", width=12, font=("맑은 고딕", 10, "bold"),
                  bg="#1F4E79", fg="white", activebackground="#2E75B6", activeforeground="white",
                  command=self._ok).pack(side="left")
        tk.Button(buttons, text="취소", width=10, command=self._cancel).pack(side="left", padx=(8, 0))
        self.top.bind("<Return>", lambda _e: self._ok())
        self._center_on(parent)

    def _ok(self) -> None:
        try:
            year = int(self.year.get())
        except ValueError:
            messagebox.showerror("입력 오류", "연도는 숫자로 넣어주세요.", parent=self.top)
            return
        month = self.month.get()
        if not messagebox.askyesno("내역 정리", f"{year}년 {month}월 판매 내역을 정리해 바탕화면에 엑셀로 저장합니다.\n\n"
                                            "실행할까요?", parent=self.top):
            return
        self._pick((year, month))


def main() -> None:
    root = tk.Tk()
    app = App(root)
    root.mainloop()
    if app.jobs:
        # 실행 중에 창을 닫은 것. Playwright 를 붙든 daemon 스레드가 인터프리터 종료를 붙잡을 수 있어 바로 끝낸다 (크롬은 Job 으로 같이 죽음)
        logging.shutdown()
        os._exit(0)


if __name__ == "__main__":
    main()

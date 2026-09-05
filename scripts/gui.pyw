"""KREAM 리리셀 - 더블클릭으로 실행하는 창 (송장 자동화 GUI 와 같은 방식).

[입찰] 을 누르면 랭킹 → 상품 → 입찰까지 자동으로 진행한다. 크롬은 화면 밖에서 돌아가고
(작업표시줄에만 남음) 진행 상황은 이 창에만 표시된다. [크롬 창 보기] 를 켜면 실행 중에도 불러올 수 있다.
[입찰취소] 는 마이페이지 > 구매 내역 > 구매 입찰 목록을 순서대로 다시 판정해 기준 미달 입찰을 지운다.
[재입찰] 은 같은 목록을 설정칸에 정한 횟수만큼 돌며(기본 1회, 0 이면 [중지] 까지 계속), 즉시 판매가가 내 희망가보다
높아진(밀린) 입찰을 상품 페이지에서 처음 입찰 때 기준으로 다시 판정하고 충족하면 [입찰 변경하기] 로 희망가를 최신 B 로
올리며, 기준 미달이라 올릴 수 없으면 그 입찰을 지운다 (사이클 간격은 설정칸, 기본 5분 - 너무 빠르면 사이트가 막을 수 있다).
[입찰 기준] 표에서 A(빠른배송 가격) 금액 구간별 최소 마진율과 상품 금액 상한(A 가 넘으면 바로 건너뜀)을 정한다.
[입찰]/[입찰취소]/[기준 저장] 을 누르면 data/bid_rules.json 에 저장돼 다음 실행과 명령행에도 쓰인다.
끝나면 바탕화면\\KREAM 결과\\ 에 엑셀 보고서가 저장된다 (자동으로 열지는 않는다).
[내역] 은 달을 고르면 보관 판매(종료) 에서 그 달에 거래된 판매를 구매 내역(종료) 과 짝지어
정산 시트 모양의 엑셀(바탕화면\\KREAM 내역 YYYY-MM.xlsx) 로 저장한다.
[중지] 는 지금 보고 있는 상품(입찰)을 끝낸 뒤 멈춘다.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

if sys.platform == "win32" and sys.stdout is not None:
    # pythonw.exe(콘솔 없는 실행)에서는 stdout/stderr 가 None 이라 건드리면 안 된다.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kream_reresell import browser  # noqa: E402
from kream_reresell.app import run_cancel_job, run_history_job, run_job, run_rebid_job  # noqa: E402
from kream_reresell.config import LOG_DIR, RULES_PATH, Settings  # noqa: E402
from kream_reresell.ranking import ALL_CATEGORIES, DEFAULT_CATEGORY  # noqa: E402
from kream_reresell.report import REPORT_DIR  # noqa: E402
from kream_reresell.rules import BidRules, Tier  # noqa: E402

WINDOW_WIDTH = 660
WINDOW_HEIGHT = 900
RIGHT_MARGIN = 40

# 상품군 체크박스는 랭킹 칩 순서(ALL_CATEGORIES)대로 나열하고, 체크한 것을 그 순서대로 실행한다.
CATEGORY_COLUMNS = 6


class QueueHandler(logging.Handler):
    """로그를 GUI 스레드로 넘기기 위한 핸들러."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record)))


def _place_right_center(root: tk.Tk) -> None:
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{sw - WINDOW_WIDTH - RIGHT_MARGIN}+{(sh - WINDOW_HEIGHT) // 2}")


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("KREAM 리리셀")
        _place_right_center(root)
        root.minsize(560, 620)

        self.q: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_report: Path | None = None
        self.base = Settings()

        pad = {"padx": 12, "pady": 4}

        # ---- 설정 영역
        frame = tk.LabelFrame(root, text="실행 설정")
        frame.pack(fill="x", padx=12, pady=(12, 4))

        # 상품군: 전부 나열해 두고 체크한 것을 위→아래, 왼쪽→오른쪽(랭킹 칩 순서) 순으로 실행한다
        cat_frame = tk.LabelFrame(frame, text="상품군 (체크한 것을 나열된 순서대로 실행)")
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
                 fg="#888").pack(side="left", padx=(12, 0))

        row1 = tk.Frame(frame)
        row1.pack(fill="x", **pad)
        tk.Label(row1, text="상품군마다 볼 상품 수").pack(side="left")
        self.limit = tk.Spinbox(row1, from_=1, to=200, width=6)
        self.limit.delete(0, "end")
        self.limit.insert(0, str(self.base.max_products))
        self.limit.pack(side="left", padx=(6, 0))
        tk.Label(row1, text="([입찰] 에만 쓰임)", fg="#888").pack(side="left", padx=(6, 0))

        # 재입찰: 몇 바퀴 돌지 + 바퀴 시작 간격
        row_rebid = tk.Frame(frame)
        row_rebid.pack(fill="x", **pad)
        tk.Label(row_rebid, text="재입찰 횟수(회)").pack(side="left")
        self.rebid_cycles = tk.Spinbox(row_rebid, from_=0, to=999, width=5)
        self.rebid_cycles.delete(0, "end")
        self.rebid_cycles.insert(0, str(self.base.rebid_cycles))
        self.rebid_cycles.pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text="(0 = [중지]까지 계속)", fg="#888").pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text="사이클 시작 간격(분)").pack(side="left", padx=(18, 0))
        self.rebid_interval = tk.Spinbox(row_rebid, from_=1, to=120, width=5)
        self.rebid_interval.delete(0, "end")
        self.rebid_interval.insert(0, f"{self.base.rebid_interval_min:g}")
        self.rebid_interval.pack(side="left", padx=(6, 0))
        tk.Label(row_rebid, text="(1분 이상)", fg="#888").pack(side="left", padx=(6, 0))

        row2 = tk.Frame(frame)
        row2.pack(fill="x", **pad)
        self.mode = tk.StringVar(value="real")
        tk.Radiobutton(row2, text="실제 실행 (입찰 / 입찰취소 / 재입찰)", variable=self.mode, value="real").pack(side="left")
        tk.Radiobutton(row2, text="판단만 (입찰·취소·변경 안 함)", variable=self.mode, value="dry").pack(side="left", padx=(12, 0))
        self.show_chrome = tk.BooleanVar(value=self.base.show_chrome)
        tk.Checkbutton(row2, text="크롬 창 보기", variable=self.show_chrome,
                       command=self.toggle_chrome_window).pack(side="right")

        cond = (f"조건: 최근 {self.base.lookback_days}일 빠른배송 {self.base.min_fast_sales}건 이상 · "
                f"마진 (A−B) > A×[아래 입찰 기준의 구간별 %] · 입찰 {self.base.bid_days}일 · 창고보관 · 포인트 최대 사용")
        tk.Label(frame, text=cond, fg="#555", anchor="w", justify="left", wraplength=580).pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(frame, text="(거래량·기간·입찰기한은 프로젝트 폴더의 .env 에서 바꿉니다. 상품군·상품 수는 [입찰]에만, "
                             "재입찰 횟수·사이클 간격은 [재입찰]에만 쓰입니다)",
                 fg="#888", anchor="w", justify="left", wraplength=600).pack(fill="x", padx=12, pady=(0, 6))

        # ---- 입찰 기준 (금액 구간별 마진율 + 입찰가 상한)
        self.tier_rows: list[dict] = []
        self._build_rules_panel(root)

        # ---- 버튼
        buttons = tk.Frame(root)
        buttons.pack(fill="x", padx=12, pady=4)
        self.run_button = tk.Button(buttons, text="입찰", width=12, height=2, font=("맑은 고딕", 11, "bold"),
                                    bg="#222", fg="white", activebackground="#444", activeforeground="white",
                                    command=self.start)
        self.run_button.pack(side="left")
        self.cancel_button = tk.Button(buttons, text="입찰취소", width=12, height=2, font=("맑은 고딕", 11, "bold"),
                                       bg="#8B0000", fg="white", activebackground="#B22222", activeforeground="white",
                                       command=self.start_cancel)
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.rebid_button = tk.Button(buttons, text="재입찰", width=12, height=2, font=("맑은 고딕", 11, "bold"),
                                      bg="#B36B00", fg="white", activebackground="#D98C1F", activeforeground="white",
                                      command=self.start_rebid)
        self.rebid_button.pack(side="left", padx=(8, 0))
        self.history_button = tk.Button(buttons, text="내역", width=12, height=2, font=("맑은 고딕", 11, "bold"),
                                        bg="#1F4E79", fg="white", activebackground="#2E75B6", activeforeground="white",
                                        command=self.start_history)
        self.history_button.pack(side="left", padx=(8, 0))
        self.stop_button = tk.Button(buttons, text="중지 (지금 것까지만)", width=18, height=2, state="disabled",
                                     command=self.request_stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        self.status = tk.Label(buttons, text="대기 중", fg="#333")
        self.status.pack(side="left", padx=(16, 0))

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
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
        handler = QueueHandler(self.q)
        handler.setFormatter(fmt)
        file_handler = logging.FileHandler(LOG_DIR / f"gui_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
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
        for col, text in enumerate(("A 부터 (원)", "A 미만 (원, 비우면 끝없음)", "최소 마진율 (%)")):
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
    def _set_all_categories(self, value: bool) -> None:
        for var in self.category_vars.values():
            var.set(value)

    def selected_categories(self) -> list[str]:
        """체크된 상품군을 나열된(랭킹 칩) 순서대로."""
        return [name for name in ALL_CATEGORIES if self.category_vars[name].get()]

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            limit = int(self.limit.get())
        except ValueError:
            messagebox.showerror("입력 오류", "상품 수는 숫자로 넣어주세요.")
            return
        categories = self.selected_categories()
        if not categories:
            messagebox.showerror("입력 오류", "상품군을 하나 이상 체크해 주세요.")
            return
        cat_text = " → ".join(categories)
        rules = self._apply_rules()
        if rules is None:
            return
        dry = self.mode.get() == "dry"
        if not dry and not messagebox.askyesno(
                "실제 입찰", f"상품군 {len(categories)}개를 순서대로 돌며 각각 상위 {limit}개 중 조건에 맞는 상품에 "
                             f"실제로 구매 입찰을 넣습니다.\n\n{cat_text}\n\n{rules.describe()}\n\n"
                             "배송방법은 창고보관, 포인트는 최대 사용입니다.\n\n진행할까요?"):
            return

        settings = self._make_settings(dry, rules)
        settings.max_products = limit
        self.stop_flag.clear()
        self.last_report = None
        self.open_report_button.configure(state="disabled")
        self._set_busy(True, "실행 중...")
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {cat_text} / 상품군마다 상위 {limit}개, "
                  f"{'판단만' if dry else '실제 입찰'} =====\n입찰 기준: {rules.describe()}")

        self.worker = threading.Thread(target=self._worker, args=(settings, categories), daemon=True)
        self.worker.start()

    def _worker(self, settings: Settings, categories: list[str]) -> None:
        try:
            job = run_job(settings, categories, should_stop=self.stop_flag.is_set,
                          on_status=lambda text: self.q.put(("status", text)))
            self.q.put(("done", job))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("실행 중 오류")
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def start_cancel(self) -> None:
        """마이페이지 구매 입찰 목록을 순서대로 다시 판정해 기준 미달 입찰을 지운다."""
        if self.worker and self.worker.is_alive():
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
        settings = self._make_settings(dry, rules)
        self.stop_flag.clear()
        self.last_report = None
        self.open_report_button.configure(state="disabled")
        self._set_busy(True, "입찰취소 실행 중...")
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 입찰취소 시작: 구매 입찰 목록 전체, "
                  f"{'판단만' if dry else '기준 미달 입찰 지움'} =====\n입찰 기준: {rules.describe()}")
        self.worker = threading.Thread(target=self._cancel_worker, args=(settings,), daemon=True)
        self.worker.start()

    def _cancel_worker(self, settings: Settings) -> None:
        try:
            job = run_cancel_job(settings, should_stop=self.stop_flag.is_set)
            self.q.put(("done", job))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("입찰취소 중 오류")
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def start_rebid(self) -> None:
        """[재입찰]: 구매 입찰 목록을 정한 횟수만큼(0 이면 [중지] 까지) 돌며 밀린 입찰의 희망가를 [입찰 변경하기] 로 최신 B 로 올린다."""
        if self.worker and self.worker.is_alive():
            return
        try:
            cycles = int(self.rebid_cycles.get())
        except ValueError:
            messagebox.showerror("입력 오류", "재입찰 횟수는 정수(회)로 넣어주세요. 0 이면 [중지] 를 누를 때까지 계속 돕니다.")
            return
        if cycles < 0:
            messagebox.showerror("입력 오류", "재입찰 횟수는 0(계속) 또는 1 이상이어야 합니다.")
            return
        try:
            interval = float(self.rebid_interval.get())
        except ValueError:
            messagebox.showerror("입력 오류", "재입찰 사이클 간격은 숫자(분)로 넣어주세요.")
            return
        if interval < 1:
            messagebox.showerror("입력 오류", "재입찰 사이클 간격은 1분 이상이어야 합니다 (너무 빠르면 사이트가 막을 수 있습니다).")
            return
        rules = self._apply_rules()
        if rules is None:
            return
        dry = self.mode.get() == "dry"
        if cycles == 0:
            repeat = f"[중지] 를 누를 때까지 {interval:g}분 간격으로 계속 반복"
        elif cycles == 1:
            repeat = "구매 입찰 목록을 한 바퀴만 돌고 끝"
        else:
            repeat = f"{interval:g}분 간격으로 {cycles}회 돌고 끝"
        if not dry and not messagebox.askyesno(
                "재입찰", "마이페이지 > 구매 내역 > 구매 입찰 목록을 순서대로 보며, 즉시 판매가가 내 희망가보다 높아진(밀린) 입찰을\n"
                        "상품 페이지에서 처음 입찰 때와 같은 기준으로 다시 판정하고\n"
                        f"(최근 {self.base.lookback_days}일 빠른배송 {self.base.min_fast_sales}건 이상, {rules.describe()}),\n"
                        f"충족하면 [입찰 변경하기] 로 희망가를 최신 즉시 판매가로 올립니다 (마감 {self.base.bid_days}일, 창고보관).\n"
                        "기준에 못 미쳐 올릴 수 없는 입찰과, 밀렸는데 변경 화면이 예상과 달라 못 올린 입찰은 실제로 지웁니다 (되돌릴 수 없음).\n\n"
                        f"{repeat}합니다 (횟수는 설정의 '재입찰 횟수' 칸, 도는 중에도 [중지] 로 멈출 수 있음).\n\n진행할까요?"):
            return
        settings = self._make_settings(dry, rules)
        settings.rebid_interval_min = interval
        settings.rebid_cycles = cycles
        self.stop_flag.clear()
        self.last_report = None
        self.open_report_button.configure(state="disabled")
        self._set_busy(True, "재입찰 실행 중...")
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 재입찰 시작: {repeat} "
                  f"({'판단만' if dry else '밀린 입찰의 희망가를 올림'}) =====\n입찰 기준: {rules.describe()}")
        self.worker = threading.Thread(target=self._rebid_worker, args=(settings,), daemon=True)
        self.worker.start()

    def _rebid_worker(self, settings: Settings) -> None:
        try:
            job = run_rebid_job(settings, should_stop=self.stop_flag.is_set,
                                on_status=lambda text: self.q.put(("status", text)))
            self.q.put(("done", job))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("재입찰 중 오류")
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def start_history(self) -> None:
        """[내역]: 달을 고르면 보관 판매 거래일시가 그 달인 판매를 구매 내역과 짝지어 엑셀로 저장한다."""
        if self.worker and self.worker.is_alive():
            return
        choice = MonthDialog(self.root).show()
        if choice is None:
            return
        year, month = choice
        settings = Settings(show_chrome=self.show_chrome.get())
        self.stop_flag.clear()
        self.last_report = None
        self.open_report_button.configure(state="disabled")
        self._set_busy(True, f"{year}년 {month}월 내역 정리 중...")
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 내역 정리 시작: {year}년 {month}월 "
                  f"(보관 판매 거래일시 기준, 구매 내역과 짝 맞춤) =====")
        self.worker = threading.Thread(target=self._history_worker, args=(settings, year, month), daemon=True)
        self.worker.start()

    def _history_worker(self, settings: Settings, year: int, month: int) -> None:
        try:
            job = run_history_job(settings, year, month, should_stop=self.stop_flag.is_set)
            self.q.put(("history_done", job))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("내역 정리 중 오류")
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _finish_history(self, job) -> None:
        self.last_report = job.report_path
        self.open_report_button.configure(state="normal")
        r = job.result
        summary = f"{r.year}년 {r.month}월 판매 {len(r.sales)}건"
        if r.unmatched:
            summary += f" (매입 내역 못 찾음 {len(r.unmatched)}건 - 엑셀의 노란 줄)"
        self._set_busy(False, f"완료: {summary}")
        self._log(f"===== 완료 - {summary}\n엑셀: {job.report_path}")
        messagebox.showinfo("내역 정리 완료", f"{summary}\n\n엑셀이 저장되었습니다:\n{job.report_path}")

    def _make_settings(self, dry: bool, rules: BidRules) -> Settings:
        return Settings(dry_run=dry, show_chrome=self.show_chrome.get(), rules=rules)

    def toggle_chrome_window(self) -> None:
        """실행 중이면 크롬 창을 바로 불러오거나 치운다. 대기 중이면 다음 실행에만 반영된다."""
        show = self.show_chrome.get()
        moved = browser.show_window() if show else browser.hide_window()
        if moved:
            self._log("크롬 창을 화면으로 불러왔습니다 (창을 조작하지는 마세요)" if show else "크롬 창을 화면 밖으로 치웠습니다")

    def request_stop(self) -> None:
        self.stop_flag.set()
        self.status.configure(text="지금 것까지 보고 멈춥니다...")
        self.stop_button.configure(state="disabled")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.run_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="disabled" if busy else "normal")
        self.rebid_button.configure(state="disabled" if busy else "normal")
        self.history_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.status.configure(text=text or ("대기 중" if not busy else ""))

    # ------------------------------------------------------------ 큐 처리
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    if not self.stop_flag.is_set():   # 중지를 눌렀으면 "멈춥니다..." 표시를 유지
                        self.status.configure(text=payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "history_done":
                    self._finish_history(payload)
                elif kind == "error":
                    self._set_busy(False, "오류로 중단")
                    messagebox.showerror("오류", payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _finish(self, job) -> None:
        self.last_report = job.report_path
        self.open_report_button.configure(state="normal")
        counts: dict[str, int] = {}
        for r in job.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        summary = ", ".join(f"{k} {v}개" for k, v in sorted(counts.items())) or "처리한 상품 없음"
        self._set_busy(False, f"완료: {summary}")
        self._log(f"===== 완료 ({job.mode}) - {summary}\n보고서: {job.report_path}")
        messagebox.showinfo("완료", f"{job.mode}\n{summary}\n\n보고서가 저장되었습니다:\n{job.report_path}")

    # ------------------------------------------------------------ 파일 열기
    def open_report_dir(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(REPORT_DIR))  # type: ignore[attr-defined]

    def open_last_report(self) -> None:
        if self.last_report and self.last_report.exists():
            os.startfile(str(self.last_report))  # type: ignore[attr-defined]


class MonthDialog:
    """[내역] 을 누르면 뜨는 창: 연도와 달을 고르고 [실행] 으로 확인한다. 취소하면 None."""

    def __init__(self, parent: tk.Tk) -> None:
        self.result: tuple[int, int] | None = None
        today = datetime.now()
        # 보통 지난달을 정리하므로 지난달을 기본으로 둔다
        last_year, last_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)

        self.top = tk.Toplevel(parent)
        self.top.title("내역 정리 - 달 선택")
        self.top.resizable(False, False)
        self.top.transient(parent)
        self.top.grab_set()

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
        self.top.bind("<Escape>", lambda _e: self._cancel())
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

        # 부모 창 가운데에 띄운다
        self.top.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.top.winfo_width(), self.top.winfo_height()
        self.top.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")

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
        self.result = (year, month)
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self) -> tuple[int, int] | None:
        self.top.wait_window()
        return self.result


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

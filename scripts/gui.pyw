"""KREAM 자동입찰 - 더블클릭으로 실행하는 창 (송장 자동화 GUI 와 같은 방식).

[실행] 을 누르면 크롬 창이 뜨고 랭킹 → 상품 → 입찰까지 자동으로 진행한다.
끝나면 바탕화면\\KREAM 결과\\ 에 엑셀 보고서가 저장된다 (자동으로 열지는 않는다).
[중지] 는 지금 보고 있는 상품을 끝낸 뒤 멈춘다.
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

from kream_reresell.app import run_job  # noqa: E402
from kream_reresell.config import LOG_DIR, Settings  # noqa: E402
from kream_reresell.ranking import ALL_CATEGORIES, DEFAULT_CATEGORY  # noqa: E402
from kream_reresell.report import REPORT_DIR  # noqa: E402

WINDOW_WIDTH = 660
WINDOW_HEIGHT = 700
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
        root.title("KREAM 자동입찰")
        _place_right_center(root)
        root.minsize(560, 480)

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
        tk.Label(sel, text="※ 신발/러닝화/부츠도 고를 수 있지만, 사이즈 옵션이 있는 상품은 건너뜁니다",
                 fg="#888").pack(side="left", padx=(12, 0))

        row1 = tk.Frame(frame)
        row1.pack(fill="x", **pad)
        tk.Label(row1, text="상품군마다 볼 상품 수").pack(side="left")
        self.limit = tk.Spinbox(row1, from_=1, to=200, width=6)
        self.limit.delete(0, "end")
        self.limit.insert(0, str(self.base.max_products))
        self.limit.pack(side="left", padx=(6, 0))

        row2 = tk.Frame(frame)
        row2.pack(fill="x", **pad)
        self.mode = tk.StringVar(value="real")
        tk.Radiobutton(row2, text="실제 입찰", variable=self.mode, value="real").pack(side="left")
        tk.Radiobutton(row2, text="판단만 (입찰 안 함)", variable=self.mode, value="dry").pack(side="left", padx=(12, 0))

        cond = (f"조건: 최근 {self.base.lookback_days}일 빠른배송 {self.base.min_fast_sales}건 이상 · "
                f"마진 (A−B) > A×{self.base.min_margin_rate*100:.0f}% · 입찰 {self.base.bid_days}일 · 창고보관 · 포인트 최대 사용")
        tk.Label(frame, text=cond, fg="#555", anchor="w", justify="left", wraplength=580).pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(frame, text="(숫자는 프로젝트 폴더의 .env 에서 바꿉니다)", fg="#888", anchor="w").pack(fill="x", padx=12, pady=(0, 6))

        # ---- 버튼
        buttons = tk.Frame(root)
        buttons.pack(fill="x", padx=12, pady=4)
        self.run_button = tk.Button(buttons, text="실행", width=14, height=2, font=("맑은 고딕", 11, "bold"),
                                    bg="#222", fg="white", activebackground="#444", activeforeground="white",
                                    command=self.start)
        self.run_button.pack(side="left")
        self.stop_button = tk.Button(buttons, text="중지 (이 상품까지만, 남은 상품군 안 함)", width=30, height=2, state="disabled",
                                     command=self.request_stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        self.status = tk.Label(buttons, text="대기 중", fg="#333")
        self.status.pack(side="left", padx=(16, 0))

        # ---- 로그
        log_frame = tk.LabelFrame(root, text="진행 상황")
        log_frame.pack(fill="both", expand=True, padx=12, pady=4)
        self.log_box = tk.Text(log_frame, height=16, wrap="word", state="disabled", font=("맑은 고딕", 9))
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
        dry = self.mode.get() == "dry"
        if not dry and not messagebox.askyesno(
                "실제 입찰", f"상품군 {len(categories)}개를 순서대로 돌며 각각 상위 {limit}개 중 조건에 맞는 상품에 "
                             f"실제로 구매 입찰을 넣습니다.\n\n{cat_text}\n\n"
                             "배송방법은 창고보관, 포인트는 최대 사용입니다.\n\n진행할까요?"):
            return

        settings = Settings(dry_run=dry)
        settings.max_products = limit
        self.stop_flag.clear()
        self.last_report = None
        self.open_report_button.configure(state="disabled")
        self._set_busy(True, "실행 중... (크롬 창을 건드리지 마세요)")
        self._log(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작: {cat_text} / 상품군마다 상위 {limit}개, "
                  f"{'판단만' if dry else '실제 입찰'} =====")

        self.worker = threading.Thread(target=self._worker, args=(settings, categories), daemon=True)
        self.worker.start()

    def _worker(self, settings: Settings, categories: list[str]) -> None:
        try:
            job = run_job(settings, categories, should_stop=self.stop_flag.is_set)
            self.q.put(("done", job))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("gui").exception("실행 중 오류")
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def request_stop(self) -> None:
        self.stop_flag.set()
        self.status.configure(text="지금 상품까지 보고 멈춥니다...")
        self.stop_button.configure(state="disabled")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.run_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.status.configure(text=text or ("대기 중" if not busy else ""))

    # ------------------------------------------------------------ 큐 처리
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._finish(payload)
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


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

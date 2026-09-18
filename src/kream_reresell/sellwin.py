"""판매 관리 창 - GUI [판매] 를 누르면 뜨는 tkinter 창 (No1 Seller Center 의 보관관리 화면을 본떠 필요한 것만, 사용자 요청 2026-09-17).

구성:
  위 줄: [새로고침] · [전체 선택] [선택 해제] · 보기(전체/입찰중/판매대기) · 하한 마진(%) · [판매 하한가 다시 계산] (체크한 행, 없으면 전부: 매입가 × 마진) ·
         보관 N일부터 하한 없이 경쟁 · [경쟁 등록] [경쟁 해제] · [경쟁 시작] [정지] · 상태
  표: 선택(☐/☑) / 순번 / 제품명 / 옵션 / 상태 / 보관일수 / 판매 희망가 / 경쟁 최저가 / 매입가 / 판매 하한가 / 경쟁 / 처리 결과 / 시각
      - 선택 칸을 누르면 체크. [전체 선택]/[선택 해제]. 체크한 행에 [경쟁 등록]/[해제]/[판매 하한가 다시 계산] 이 적용된다.
      - 판매 하한가 칸을 두 번 누르면 직접 고친다. 경쟁 칸을 두 번 누르면 켜고 끈다.
      - 보관 일수가 '보관 N일부터 하한 없이' 를 넘긴 행은 주황 배경, 경쟁 칸 '자동' - 등록과 관계없이 하한 없이 경쟁한다 (sell 머리글 4).
  새로고침: 경쟁 중에도 언제든 누를 수 있다 (엔진이 지금 항목을 본 뒤 목록을 다시 읽고 새 사이클을 시작).
  새 입고 알림: 엔진이 보내는 목록(쉬는 동안은 sell.NEW_STOCK_CHECK_SEC 마다 읽음)에 이 창이 처음 보는 보관번호가 있으면 그 행을 노란 배경으로
      그리고 [새로고침] 버튼이 주황색 '새 입고 N건' 으로 바뀌며 소리가 난다. [새로고침] 을 누르면 표시가 지워진다 (사용자 요청 2026-09-17).
  아래: 가격 로그 (판정 결과가 쌓인다)
동작은 sell.SellEngine (작업 스레드가 크롬에 자기 연결로 붙어 명령 큐로 움직임 - 다른 버튼과 동시에 돈다). 창을 닫으면 경쟁을 멈추고 연결을 끊고
(크롬은 다른 작업이 없을 때만 닫힘), 결과가 있으면 엑셀 보고서를 남긴다.
항목별 하한·경쟁 여부는 data/sell_rules.json 에 저장되어 다음에 창을 열어도 남는다.
"""

from __future__ import annotations

import logging
import queue
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from . import report, sell
from .config import Settings
from .report import ProductResult

log = logging.getLogger(__name__)

FONT = ("맑은 고딕", 9)
COLUMNS = [
    ("check", "선택", 40, "center"), ("no", "순번", 40, "center"), ("name", "제품명", 300, "w"), ("option", "옵션", 84, "center"),
    ("status", "상태", 60, "center"), ("days", "보관일수", 62, "center"),
    ("price", "판매 희망가", 86, "e"), ("lowest", "경쟁 최저가", 86, "e"), ("buy", "매입가", 82, "e"), ("floor", "판매 하한가", 90, "e"),
    ("compete", "경쟁", 48, "center"), ("result", "처리 결과", 280, "w"), ("time", "시각", 56, "center"),
]
WIDTH, HEIGHT = 1360, 760
CHECKED, UNCHECKED = "☑", "☐"
# [새로고침] 버튼의 새 입고 표시 모양 - 평소 모양은 같은 키로 만들 때 떠 둔다 (text 는 건수를 넣어 채움)
REFRESH_ALERT = {"text": "새로고침 ★ 새 입고 {n}건", "width": 0, "bg": "#FF8F00", "fg": "white",
                 "activebackground": "#FFA726", "activeforeground": "white", "font": ("맑은 고딕", 9, "bold")}


def _won(v: int | None) -> str:
    return f"{v:,}" if v else ""


class SellWindow:
    def __init__(self, parent: tk.Tk, settings: Settings, on_close: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.on_close = on_close
        self.rules = sell.load_sell_rules()
        self.q: queue.Queue = queue.Queue()
        self.items: list[sell.StockItem] = []
        self.last: dict[int, tuple[int | None, str, str]] = {}   # 보관번호 → (경쟁 최저가, 처리 결과, 시각)
        self.checked: set[int] = set()                            # 선택 칸을 체크한 보관번호
        self.seen_ids: set[int] | None = None                     # 지금까지 목록에서 본 보관번호 (None = 아직 첫 목록 전) - 없던 것이 오면 새 입고
        self.new_ids: set[int] = set()                            # 새 입고로 알렸는데 아직 [새로고침] 을 안 누른 보관번호
        self.running = False
        self.closing = False
        self.engine = sell.SellEngine(settings, self.rules, lambda kind, payload: self.q.put((kind, payload)))

        top = self.top = tk.Toplevel(parent)
        top.title("판매 관리 - 보관 판매 최저가 경쟁" + (" (판단만 - 가격을 바꾸지 않음)" if settings.dry_run else ""))
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        w, h = min(WIDTH, sw - 40), min(HEIGHT, sh - 80)
        top.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        top.minsize(900, 520)
        top.protocol("WM_DELETE_WINDOW", self.close)

        self._build_toolbar(top)
        self._build_table(top)
        self._build_log(top)
        top.after(200, self._poll)
        self.engine.start()

    # ------------------------------------------------------------ 화면
    def _build_toolbar(self, top: tk.Toplevel) -> None:
        bar = tk.Frame(top)
        bar.pack(fill="x", padx=10, pady=(10, 4))
        self.refresh_button = tk.Button(bar, text="새로고침", width=9, command=self._refresh_click)
        self.refresh_button.pack(side="left")
        self._refresh_plain = {k: self.refresh_button.cget(k) for k in REFRESH_ALERT}   # 평소 모양 (새 입고 표시를 지울 때 되돌림)
        tk.Button(bar, text="전체 선택", command=lambda: self._check_all(True)).pack(side="left", padx=(8, 0))
        tk.Button(bar, text="선택 해제", command=lambda: self._check_all(False)).pack(side="left", padx=(4, 0))
        tk.Label(bar, text="보기:", font=FONT).pack(side="left", padx=(14, 2))
        self.view = tk.StringVar(value="all")
        for text, value in (("전체", "all"), ("입찰중", "live"), ("판매대기", "in_storage")):
            tk.Radiobutton(bar, text=text, variable=self.view, value=value, command=self._render, font=FONT).pack(side="left")
        tk.Label(bar, text="하한 마진(%)", font=FONT).pack(side="left", padx=(14, 2))
        self.margin = tk.Spinbox(bar, from_=0, to=99, width=4)
        self.margin.delete(0, "end")
        self.margin.insert(0, f"{self.settings.sell_margin_rate * 100:g}")
        self.margin.pack(side="left")
        tk.Button(bar, text="판매 하한가 다시 계산", command=self._floor_from_buy).pack(side="left", padx=(6, 0))
        tk.Label(bar, text="보관", font=FONT).pack(side="left", padx=(14, 2))
        self.free_after = tk.Spinbox(bar, from_=1, to=30, width=3, command=self._apply_free_after)
        self.free_after.delete(0, "end")
        self.free_after.insert(0, str(self.settings.sell_free_after_days + 1))
        self.free_after.pack(side="left")
        self.free_after.bind("<FocusOut>", lambda _e: self._apply_free_after())
        tk.Label(bar, text="일부터 하한 없이 경쟁", font=FONT).pack(side="left", padx=(2, 0))
        tk.Button(bar, text="경쟁 등록", command=lambda: self._set_compete(True)).pack(side="left", padx=(14, 0))
        tk.Button(bar, text="경쟁 해제", command=lambda: self._set_compete(False)).pack(side="left", padx=(4, 0))
        self.start_button = tk.Button(bar, text="경쟁 시작", width=9, bg="#2E7D32", fg="white", activebackground="#43A047",
                                      activeforeground="white", font=("맑은 고딕", 9, "bold"), command=self._start)
        self.start_button.pack(side="left", padx=(14, 0))
        self.stop_button = tk.Button(bar, text="정지", width=7, state="disabled", command=self._stop)
        self.stop_button.pack(side="left", padx=(4, 0))
        self.status = tk.Label(bar, text="크롬 여는 중...", fg="#555", font=FONT, anchor="w")
        self.status.pack(side="left", padx=(14, 0), fill="x", expand=True)
        hint = tk.Label(top, text="판매 하한가 = 이 프로그램이 내리는 최저선 (즉시 판매가와 무관) = 매입가 × (1 + 하한 마진) ÷ (1 − 판매 수수료). "
                                  "경쟁은 그 위에서만 경쟁 최저가 − 1,000원으로 맞춥니다. 빨간 글씨 = 지금 판매 희망가가 판매 하한가보다 낮음, 회색 = 매입 내역이 없어 하한 없음, "
                                  "주황 배경 = 보관 기한이 지나 하한 없이 자동 경쟁 (창고 보관료는 첫 30일만 무료). "
                                  "노란 배경 = 새로 입고된 항목 ([새로고침] 이 주황색으로 바뀜, 누르면 지워짐 - 쉬는 동안 "
                                  f"{sell.NEW_STOCK_CHECK_SEC // 60}분마다 확인). "
                                  "선택 칸 누름 = 체크, 판매 하한가 칸 두 번 누름 = 직접 입력, 경쟁 칸 두 번 누름 = 켜기/끄기. "
                                  f"시세 조회 간격 {self.settings.api_tick_sec:g}초, 사이클 사이 {sell.CYCLE_GAP_SEC}초. "
                                  "※ No1 Seller Center 의 최저가 경쟁은 같은 항목에서 꺼 두세요.",
                        fg="#777", font=("맑은 고딕", 8), anchor="w", justify="left", wraplength=WIDTH - 40)
        hint.pack(fill="x", padx=12)

    def _build_table(self, top: tk.Toplevel) -> None:
        frame = tk.Frame(top)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        style = ttk.Style(top)
        style.configure("Sell.Treeview", font=FONT, rowheight=22)
        style.configure("Sell.Treeview.Heading", font=("맑은 고딕", 9, "bold"))
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS], show="headings", selectmode="extended", style="Sell.Treeview")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=key in ("name", "result"))
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tree.tag_configure("new", background="#FFF59D")        # 새로 입고된 항목 ([새로고침] 을 누르면 지워짐) - 먼저 붙여 다른 배경보다 우선
        self.tree.tag_configure("compete", background="#E8F5E9")
        self.tree.tag_configure("free", background="#FFE0B2")       # 보관 기한 지나 하한 없이 자동 경쟁
        self.tree.tag_configure("below", foreground="#B00020")      # 지금 가격이 하한 아래
        self.tree.tag_configure("nofloor", foreground="#888888")    # 하한 없음
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-1>", self._on_click)

    def _build_log(self, top: tk.Toplevel) -> None:
        frame = tk.LabelFrame(top, text="가격 로그")
        frame.pack(fill="x", padx=10, pady=(0, 10))
        self.log_box = tk.Text(frame, height=7, font=("맑은 고딕", 9), state="disabled", wrap="word")
        ys = tk.Scrollbar(frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=ys.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

    def _log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{datetime.now():%H:%M:%S} {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------ 표
    def _render(self) -> None:
        selected = {int(i) for i in self.tree.selection()}
        self.tree.delete(*self.tree.get_children())
        view = self.view.get()
        for n, item in enumerate(self.items, start=1):
            if view != "all" and item.status != view:
                continue
            rule = self.rules.get(item.ask_id) or sell.SellRule()
            lowest, result, when = self.last.get(item.ask_id, (None, "", ""))
            free = item.is_free_mode(self.settings)
            tags = ["new"] if item.ask_id in self.new_ids else []
            if free:
                tags.append("free")
            elif rule.compete:
                tags.append("compete")
            if rule.floor is None:
                tags.append("nofloor")
            elif item.price and item.price < rule.floor and not free:
                tags.append("below")
            days = item.stored_days
            self.tree.insert("", "end", iid=str(item.ask_id), tags=tags, values=(
                CHECKED if item.ask_id in self.checked else UNCHECKED,
                n, item.name, item.option if not item.is_one_size else "", item.status_text, f"{days}일" if days else "?",
                _won(item.price), _won(lowest), _won(item.buy_price), _won(rule.floor) or "(없음)",
                "자동" if free else ("●" if rule.compete else ""), result, when,
            ))
        keep = [str(a) for a in selected if self.tree.exists(str(a))]
        if keep:
            self.tree.selection_set(keep)

    def _selected_items(self) -> list[sell.StockItem]:
        """체크한 행. 하나도 체크하지 않았으면 파란 선택(클릭한 행)으로 대신한다."""
        ids = self.checked or {int(i) for i in self.tree.selection()}
        return [i for i in self.items if i.ask_id in ids]

    def _check_all(self, on: bool) -> None:
        shown = {int(i) for i in self.tree.get_children()}
        self.checked = (self.checked | shown) if on else (self.checked - shown)
        self._render()

    def _on_click(self, event) -> str | None:
        """선택 칸을 누르면 체크를 바꾼다 (다른 칸은 기본 동작)."""
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#1":
            return None
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle_check(int(row))
        return "break"

    def _toggle_check(self, ask_id: int) -> None:
        """선택 칸 체크를 켜고 끈다 (같은 칸을 다시 누르면 해제)."""
        self.checked.symmetric_difference_update({ask_id})
        row = str(ask_id)
        if self.tree.exists(row):
            self.tree.set(row, "check", CHECKED if ask_id in self.checked else UNCHECKED)

    def _apply_free_after(self) -> None:
        try:
            day = int(self.free_after.get())
        except ValueError:
            return
        if day < 1:
            return
        self.settings.sell_free_after_days = day - 1
        self._render()

    def _on_double_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row:
            return
        key = COLUMNS[int(col[1:]) - 1][0]
        item = next((i for i in self.items if str(i.ask_id) == row), None)
        if item is None:
            return
        if key == "check":
            # 빠르게 두 번 누르면 Button-1 이 두 번(켬→끔) 온 뒤 여기로 온다 - 한 번 더 바꿔 두 번 누름도 한 번 누른 것과 같게
            self._toggle_check(item.ask_id)
            return
        rule = self.rules.setdefault(item.ask_id, sell.SellRule())
        if key == "floor":
            value = simpledialog.askinteger("판매 하한가", f"{item.label}\n\n판매 하한가(원, 이 아래로는 안 내림)를 넣어주세요.\n"
                                                    f"매입가 {_won(item.buy_price) or '모름'} / 지금 판매가 {item.price:,}",
                                            parent=self.top, initialvalue=rule.floor or item.price or None, minvalue=1000)
            if value:
                rule.floor = int(round(value / 1000.0)) * 1000
                self._save()
        elif key == "compete":
            if not rule.compete and rule.floor is None:
                messagebox.showwarning("경쟁 등록", "하한이 없는 항목은 경쟁에 넣을 수 없습니다. 하한 칸을 두 번 눌러 넣거나 [하한 다시 계산] 을 쓰세요.", parent=self.top)
                return
            rule.compete = not rule.compete
            self._save()

    def _floor_from_buy(self) -> None:
        try:
            margin = float(self.margin.get()) / 100.0
        except ValueError:
            messagebox.showerror("입력 오류", "하한 마진은 숫자(%)로 넣어주세요.", parent=self.top)
            return
        items = self._selected_items() or self.items
        done = skipped = 0
        for item in items:
            if not item.buy_price:
                skipped += 1
                continue
            self.rules.setdefault(item.ask_id, sell.SellRule()).floor = sell.floor_price(item.buy_price, margin, item.fee_rate)
            done += 1
        self.settings.sell_margin_rate = margin
        self._save()
        self._log(f"하한을 매입가 × {1 + margin:.2f} 로 다시 계산: {done}건" + (f", 매입가 없어 건너뜀 {skipped}건" if skipped else ""))

    def _set_compete(self, on: bool) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showinfo("경쟁 등록" if on else "경쟁 해제", "표의 선택 칸을 눌러 행을 먼저 체크해 주세요 ([전체 선택] 도 됩니다).", parent=self.top)
            return
        skipped = 0
        for item in items:
            rule = self.rules.setdefault(item.ask_id, sell.SellRule())
            if on and rule.floor is None:
                skipped += 1
                continue
            rule.compete = on
        self._save()
        self._log(f"경쟁 {'등록' if on else '해제'}: {len(items) - skipped}건" + (f" (하한 없어 제외 {skipped}건)" if skipped else ""))

    def _save(self) -> None:
        sell.save_sell_rules(self.rules)
        self._render()

    # ------------------------------------------------------------ 경쟁
    def _start(self) -> None:
        self._apply_free_after()
        free = [i for i in self.items if i.is_free_mode(self.settings)]
        targets = [i for i in self.items if self.engine.is_target(i)]
        if not targets:
            messagebox.showinfo("경쟁 시작", "경쟁에 넣은 항목이 없습니다. 행을 체크하고 [경쟁 등록] 을 누르세요.", parent=self.top)
            return
        if not self.settings.dry_run and not messagebox.askyesno(
                "경쟁 시작", f"경쟁에 넣은 {len(targets)}건의 판매 희망가를 실제로 바꿉니다 (하한 위에서 경쟁 최저가 − 1,000원).\n"
                            f"그중 보관 {self.settings.sell_free_after_days + 1}일째 이상인 {len(free)}건은 하한 없이 경쟁합니다.\n\n"
                            "※ No1 Seller Center 의 최저가 경쟁이 이 항목들에 켜져 있으면 서로 가격을 바꿉니다 - 꺼져 있는지 확인하세요.\n\n"
                            "시작할까요?", parent=self.top):
            return
        self._log(f"경쟁 시작: {len(targets)}건" + (" (판단만)" if self.settings.dry_run else ""))
        self.engine.request("start")

    def _stop(self) -> None:
        self.stop_button.configure(state="disabled")
        self.status.configure(text="지금 항목까지 보고 멈춥니다...")
        self.engine.request("stop")

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    # ------------------------------------------------------------ 새로고침 · 새 입고 알림
    def _refresh_click(self) -> None:
        """언제든 누를 수 있다. 새 입고 표시를 지우고 엔진에 목록을 다시 읽으라고 한다 (경쟁 중이면 지금 항목을 본 뒤)."""
        if self.new_ids:
            self.new_ids.clear()
            self._paint_refresh()
            self._render()
        if self.running:
            self.status.configure(text="새로고침 요청 - 지금 항목을 본 뒤 목록을 다시 읽고 새 사이클을 시작합니다")
        self.engine.request("refresh")

    def _paint_refresh(self) -> None:
        """[새로고침] 버튼 모양 - 새 입고가 남아 있으면 주황색에 건수, 아니면 평소 모양."""
        if self.new_ids:
            self.refresh_button.configure(**{**REFRESH_ALERT, "text": REFRESH_ALERT["text"].format(n=len(self.new_ids))})
        else:
            self.refresh_button.configure(**self._refresh_plain)

    def _note_new_stock(self, items: list[sell.StockItem]) -> list[sell.StockItem]:
        """받은 목록에서 이 창이 처음 보는 항목을 골라 새 입고로 알린다 (첫 목록은 기준만 잡음). 표는 부르는 쪽이 다시 그린다."""
        ids = {i.ask_id for i in items}
        new = [i for i in items if i.ask_id not in self.seen_ids] if self.seen_ids is not None else []
        self.seen_ids = (self.seen_ids or set()) | ids
        if not new:
            return new
        self.new_ids |= {i.ask_id for i in new}
        self._paint_refresh()
        names = ", ".join(i.label[:30] for i in new)
        log.info("[판매 관리 창] 새 입고 %d건: %s", len(new), names)
        self._log(f"새 입고 {len(new)}건: {names} - 하한을 확인하고 [경쟁 등록] 하세요")
        self.status.configure(text=f"새 입고 {len(self.new_ids)}건 - 노란 행을 확인하세요")
        try:
            self.top.bell()
        except tk.TclError:
            pass
        return new

    # ------------------------------------------------------------ 이벤트
    def _poll(self) -> None:
        render = False   # 쌓인 이벤트를 다 처리한 뒤 표는 한 번만 그린다
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "items":
                    self._note_new_stock(payload)
                    self.items = payload
                    render = True
                elif kind == "result":
                    item, r = payload
                    self.last[item.ask_id] = (r.price_a, f"{r.status}: {r.detail}", r.time[11:16])
                    render = True
                    self._log(f"[{item.label[:30]}] {r.status} - {r.detail}")
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "running":
                    self._set_running(bool(payload))
                elif kind == "error":
                    self._log(f"오류: {payload}")
                    self.status.configure(text=f"오류: {payload}"[:120])
                elif kind == "closed":
                    self._finish()
                    return
        except queue.Empty:
            pass
        if render:
            self._render()
        if not self.closing or self.top.winfo_exists():
            self.top.after(200, self._poll)

    # ------------------------------------------------------------ 닫기
    def close(self) -> None:
        if self.closing:
            return
        if self.running and not messagebox.askyesno("닫기", "경쟁이 돌고 있습니다. 창을 닫으면 멈춥니다.\n\n닫을까요?", parent=self.top):
            return
        self.closing = True
        self.status.configure(text="닫는 중...")
        self.engine.request("stop")
        self.engine.request("close")

    def _finish(self) -> None:
        results: list[ProductResult] = self.engine.results
        if results:
            try:
                path = report.write_report(results, f"판매 관리 창 (하한 마진 {self.settings.sell_margin_rate * 100:g}%)",
                                           "DRY-RUN (판단만)" if self.settings.dry_run else "실제 실행", kind="판매", section_label="회차")
                log.info("판매 보고서: %s", path)
            except Exception:  # noqa: BLE001
                log.exception("판매 보고서 저장 실패")
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        # Tk 변수는 여기(GUI 스레드)서 놓는다 - 창 객체가 순환 참조로 남았다가 작업 스레드의 GC 에서 지워지면 Variable.__del__ 이
        # 그 스레드에서 Tk 를 불러 GUI 스레드가 한가해질 때까지 작업이 멈춘다 (2026-09-18 실측: [입찰] 이 보고서를 쓰다 거기서 기다림)
        self.view = None
        if self.on_close:
            self.on_close()

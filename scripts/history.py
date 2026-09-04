"""판매 내역 정리 (명령행): 보관 판매 거래일시가 지정한 달인 주문을 구매 내역과 짝지어 바탕화면에 엑셀로 저장한다.

    .venv\\Scripts\\python scripts\\history.py --month 8            # 올해 8월 (8월이 아직 안 왔으면 작년 8월)
    .venv\\Scripts\\python scripts\\history.py --month 8 --year 2026
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

if sys.platform == "win32" and sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kream_reresell.app import run_history_job  # noqa: E402
from kream_reresell.config import LOG_DIR, Settings  # noqa: E402
from kream_reresell.report import open_file  # noqa: E402


def default_year(month: int) -> int:
    today = datetime.now()
    return today.year if month <= today.month else today.year - 1


def main() -> int:
    ap = argparse.ArgumentParser(description="KREAM 판매 내역 정리 (보관 판매 + 구매 내역 → 엑셀)")
    ap.add_argument("--month", type=int, required=True, choices=range(1, 13), metavar="1-12", help="정리할 달")
    ap.add_argument("--year", type=int, help="연도 (기본: 그 달이 이미 지났으면 올해, 아니면 작년)")
    ap.add_argument("--show-chrome", action="store_true", help="크롬 창을 화면에 보이게 실행")
    ap.add_argument("--open", action="store_true", help="끝나면 엑셀을 연다")
    args = ap.parse_args()
    year = args.year or default_year(args.month)

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_DIR / f"history_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")])

    settings = Settings(show_chrome=args.show_chrome or Settings().show_chrome)
    job = run_history_job(settings, year, args.month)
    print(f"\n{year}년 {args.month}월 판매 {len(job.result.sales)}건 (매입 못 찾음 {len(job.result.unmatched)}건)")
    print(f"엑셀: {job.report_path}")
    if args.open:
        open_file(job.report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

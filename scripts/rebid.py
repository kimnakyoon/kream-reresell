"""KREAM 재입찰 (명령행) - 밀린 구매 입찰의 희망가를 [입찰 변경하기] 로 올린다.

마이페이지 > 구매 내역 > 구매 입찰 목록을 순서대로 보며, 즉시 판매가(B) 가 내 희망가보다 높아진(누가 더 비싸게 입찰한)
입찰은 상품 페이지에서 처음 입찰 때와 같은 기준으로 다시 판정하고, 충족하면 희망가를 최신 B 로 올린다.
밀리지 않은 입찰도 최신 A(빠른배송 가격)·B 로 마진을 다시 판정해 기준 미달이면 지운다. 빠른배송(판매자)이 없는 상품의 입찰도 지운다.
횟수는 --cycles (기본: .env REBID_CYCLES, 기본 1회. 0 이면 Ctrl+C 까지 계속), 사이클 간격은 .env REBID_INTERVAL_MIN 또는 --interval.

예)
  python scripts/rebid.py --dry-run                 # 판단만 (올리지 않음), 설정한 횟수만큼
  python scripts/rebid.py --once --dry-run          # 한 바퀴만 판단
  python scripts/rebid.py --cycles 5                # 실제로 올림, 5회 돌고 끝
  python scripts/rebid.py --cycles 0                # Ctrl+C 로 중지할 때까지 계속
  python scripts/rebid.py --interval 3              # 사이클 간격 3분
  python scripts/rebid.py --once --stop-before-submit   # 마지막 '입찰하기' 직전에 멈춤 (점검용)
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

from kream_reresell import report  # noqa: E402
from kream_reresell.app import run_rebid_job  # noqa: E402
from kream_reresell.config import LOG_DIR, Settings  # noqa: E402


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"rebid_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(path, encoding="utf-8")])
    logging.getLogger("kream_reresell").setLevel(logging.DEBUG)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="판단만 하고 희망가는 올리지 않음")
    p.add_argument("--once", action="store_true", help="구매 입찰 목록을 한 바퀴만 돌고 끝")
    p.add_argument("--cycles", type=int, help="이만큼 돌고 끝 (기본: .env REBID_CYCLES 또는 1. 0 이면 Ctrl+C 까지 계속, --once 는 1)")
    p.add_argument("--interval", type=float, help="사이클 시작 간격(분, 1 이상. 기본: .env REBID_INTERVAL_MIN 또는 5)")
    p.add_argument("--stop-before-submit", action="store_true", help="마지막 '입찰하기' 직전에 멈춤 (점검용)")
    p.add_argument("--inspect", action="store_true", help="화면마다 dumps/ 에 스냅샷 저장")
    p.add_argument("--show-chrome", action="store_true",
                   help="크롬 창을 화면에 보이게 둔다 (기본: 화면 밖에서 실행. --stop-before-submit 이면 자동으로 켜짐)")
    p.add_argument("--open", action="store_true", help="끝나고 엑셀 보고서를 자동으로 연다 (기본: 저장만)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()
    settings = Settings(dry_run=args.dry_run, stop_before_submit=args.stop_before_submit, inspect=args.inspect)
    if args.show_chrome:
        settings.show_chrome = True
    if args.interval:
        settings.rebid_interval_min = args.interval
    if args.once:
        settings.rebid_cycles = 1
    elif args.cycles is not None:
        settings.rebid_cycles = args.cycles
    job = run_rebid_job(settings)
    if args.open:
        report.open_file(job.report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

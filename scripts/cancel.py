"""KREAM 구매 입찰 취소 (명령행).

마이페이지 > 구매 내역 > 구매 입찰 목록을 순서대로 보며, 상품마다 입찰할 때와 같은 기준
(최근 N일 빠른배송 건수, 마진 (A−B) > A×기준) 으로 다시 판정하고, 기준에 못 미치면 입찰을 지운다.

예)
  python scripts/cancel.py --dry-run              # 판단만 (지우지 않음)
  python scripts/cancel.py                        # 기준 미달 입찰을 실제로 지움
  python scripts/cancel.py --stop-before-submit   # 확인창의 [입찰 지우기] 직전에 멈춤 (점검용)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kream_reresell import report  # noqa: E402
from kream_reresell.app import run_cancel_job  # noqa: E402
from kream_reresell.config import LOG_DIR, Settings  # noqa: E402


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"cancel_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(path, encoding="utf-8")])
    logging.getLogger("kream_reresell").setLevel(logging.DEBUG)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="판단만 하고 입찰은 지우지 않음")
    p.add_argument("--stop-before-submit", action="store_true", help="확인창의 [입찰 지우기] 직전에 멈춤")
    p.add_argument("--inspect", action="store_true", help="화면마다 dumps/ 에 스냅샷 저장")
    p.add_argument("--open", action="store_true", help="끝나고 엑셀 보고서를 자동으로 연다 (기본: 저장만)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging()
    settings = Settings(dry_run=args.dry_run, stop_before_submit=args.stop_before_submit, inspect=args.inspect)
    job = run_cancel_job(settings)
    if args.open:
        report.open_file(job.report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

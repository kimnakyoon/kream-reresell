"""KREAM 리리셀 자동 입찰 실행 (명령행).

예)
  python scripts/run.py --category 가방 --dry-run          # 판단만
  python scripts/run.py --category 가방                    # 조건 맞으면 실제 입찰
  python scripts/run.py --category 가방 지갑 신발          # 여러 상품군을 준 순서대로
  python scripts/run.py --all-categories --dry-run        # 랭킹의 모든 상품군을 칩 순서대로
  python scripts/run.py --list-categories                 # 상품군 이름 목록
  python scripts/run.py --product 385408 --force --stop-before-submit --inspect
                                                            # 특정 상품으로 화면 점검
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
from kream_reresell.app import run_job  # noqa: E402
from kream_reresell.config import LOG_DIR, Settings  # noqa: E402
from kream_reresell.ranking import ALL_CATEGORIES, DEFAULT_CATEGORY  # noqa: E402


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(path, encoding="utf-8")])
    logging.getLogger("kream_reresell").setLevel(logging.DEBUG)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--category", nargs="+", default=[DEFAULT_CATEGORY],
                   help=f"랭킹 상품군 이름 (여러 개면 준 순서대로 실행, 기본: {DEFAULT_CATEGORY})")
    p.add_argument("--all-categories", action="store_true", help="랭킹의 모든 상품군을 칩 순서대로 실행")
    p.add_argument("--list-categories", action="store_true", help="상품군 이름 목록을 보여주고 끝")
    p.add_argument("--limit", type=int, help="상품군마다 볼 상품 수 (기본: .env MAX_PRODUCTS)")
    p.add_argument("--product", type=int, nargs="*", help="랭킹 대신 이 상품 ID 만 처리")
    p.add_argument("--dry-run", action="store_true", help="판단만 하고 입찰 폼은 건드리지 않음")
    p.add_argument("--stop-before-submit", action="store_true", help="마지막 '입찰하기' 직전에 멈춤")
    p.add_argument("--force", action="store_true", help="거래량/마진 조건 무시 (점검용)")
    p.add_argument("--inspect", action="store_true", help="화면마다 dumps/ 에 스냅샷 저장")
    p.add_argument("--show-chrome", action="store_true",
                   help="크롬 창을 화면에 보이게 둔다 (기본: 화면 밖에서 실행. --stop-before-submit 이면 자동으로 켜짐)")
    p.add_argument("--open", action="store_true", help="끝나고 엑셀 보고서를 자동으로 연다 (기본: 저장만)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_categories:
        print("\n".join(ALL_CATEGORIES))
        return 0
    categories = ALL_CATEGORIES if args.all_categories else args.category
    unknown = [c for c in categories if c not in ALL_CATEGORIES]
    if unknown:
        print(f"모르는 상품군: {', '.join(unknown)} (랭킹 칩을 눌러 찾아봅니다. 목록: --list-categories)")
    setup_logging()
    settings = Settings(dry_run=args.dry_run, stop_before_submit=args.stop_before_submit,
                        force=args.force, inspect=args.inspect)
    if args.show_chrome:
        settings.show_chrome = True
    if args.limit:
        settings.max_products = args.limit
    job = run_job(settings, categories, product_ids=args.product)
    if args.open:
        report.open_file(job.report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""KREAM 판매 (명령행) - 보관 판매 항목의 희망가를 하한(매입가 + 마진) 위에서 최저가 경쟁으로 맞춘다 (src/kream_reresell/sell.py 머리글).

마이페이지 > 보관 판매의 입찰중·판매대기 항목마다 구매 내역에서 매입가(수수료 포함)를 찾아 하한 = 매입가 × (1 + 마진) 을 정하고,
시세 API 로 빠른배송 최저가를 읽어 판매 희망가를 max(하한, 최저가 − 1,000원) 으로 바꾼다. 최저가가 내 가격과 같으면 잠깐 올려 2등을 확인한다.
※ No1 Seller Center 의 최저가 경쟁이 같은 항목에 켜져 있으면 서로 가격을 바꾼다 - 그쪽을 끄고 실행할 것.

예)
  python scripts/sell.py --dry-run --once        # 한 바퀴 판단만 (가격 안 바꿈)
  python scripts/sell.py --margin 5              # 하한 마진 5% 로 Ctrl+C 까지 반복
  python scripts/sell.py --cycles 3 --no-probe   # 3회 돌고 끝, 탐침 없이
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
from kream_reresell.app import run_sell_job  # noqa: E402
from kream_reresell.config import LOG_DIR, Settings  # noqa: E402


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"sell_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(path, encoding="utf-8")])
    logging.getLogger("kream_reresell").setLevel(logging.DEBUG)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="판단만 하고 판매 희망가는 바꾸지 않음")
    p.add_argument("--once", action="store_true", help="보관 목록을 한 바퀴만 돌고 끝")
    p.add_argument("--cycles", type=int, help="이만큼 돌고 끝 (기본: .env SELL_CYCLES 또는 0 = Ctrl+C 까지 계속, --once 는 1)")
    p.add_argument("--margin", type=float, help="하한 마진 %% (기본: .env SELL_MARGIN_RATE 또는 5)")
    p.add_argument("--no-probe", action="store_true", help="최저가가 내 가격과 같을 때 올려 보지 않고 그대로 둠")
    p.add_argument("--tick", type=float, help="시세 API 조회 간격(초, 3~60. 기본: .env API_TICK_SEC 또는 6)")
    p.add_argument("--show-chrome", action="store_true", help="크롬 창을 화면에 보이게 둔다")
    p.add_argument("--open", action="store_true", help="끝나고 엑셀 보고서를 자동으로 연다 (기본: 저장만)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()
    settings = Settings(dry_run=args.dry_run, show_chrome=args.show_chrome)
    if args.margin is not None:
        settings.sell_margin_rate = args.margin / 100.0
    if args.no_probe:
        settings.sell_probe = False
    if args.tick is not None:
        settings.api_tick_sec = args.tick
    cycles = 1 if args.once else args.cycles
    job = run_sell_job(settings, max_cycles=cycles)
    print(f"\n결과: {report.summarize(job.results, empty='처리한 항목 없음')}\n보고서: {job.report_path}")
    if args.open:
        import os
        os.startfile(str(job.report_path))  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()

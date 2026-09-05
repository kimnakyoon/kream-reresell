"""data/run_log.csv 의 마지막 실행분(순위 1부터 이어지는 마지막 블록)을 엑셀 보고서로 만든다.

실행 중간에 프로그램이 죽어 보고서가 안 만들어졌을 때 쓴다.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kream_reresell import report  # noqa: E402
from kream_reresell.store import RUN_LOG_PATH  # noqa: E402


def _int(v: str) -> int | None:
    v = (v or "").strip()
    return int(float(v)) if v else None


def main() -> int:
    paths = sorted(ROOT.joinpath("data").glob("run_log*.csv"))
    if not paths:
        print("run_log.csv 가 없습니다"); return 1
    src = RUN_LOG_PATH if RUN_LOG_PATH.exists() else paths[-1]
    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    # 마지막 블록: 뒤에서부터 1위 행을 만날 때마다 상품군 하나가 끝난 것.
    # 같은 상품군의 1위가 두 번째로 나오면 그 앞은 이전 실행이므로 거기서 멈춘다.
    start = 0
    seen_cats: set[str] = set()
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("rank") == "1":
            cat = rows[i].get("category", "")
            if cat in seen_cats:
                break
            seen_cats.add(cat)
            start = i
    block = rows[start:]
    results = []
    for row in block:
        res = row.get("result") or f"{row.get('status','')}: {row.get('detail','')}"
        status, _, detail = res.partition(": ")
        status = {"skip": "건너뜀", "dry-run": "입찰대상", "bid": "입찰완료", "stopped": "중단",
                  "abort": "중단", "error": "오류", "uncertain": "확인필요"}.get(status, status)
        pid = _int(row["product_id"]) or 0
        r = report.ProductResult(
            rank=_int(row["rank"]) or 0, product_id=pid, name=row["name"], option=row.get("option", "") or "",
            url=f"https://kream.co.kr/products/{pid}", category=row.get("category", ""),
            status=status, detail=detail,
            fast_sales=_int(row.get("fast_sales", "")), price_a=_int(row.get("price_a", "")),
            price_b=_int(row.get("price_b", "")), time=row["time"],
        )
        if status == "입찰대상" and r.price_b:
            r.bid_price, r.bid_days = r.price_b, 7
        results.append(r)
    mode = "DRY-RUN (판단만)" if any(r.status == "입찰대상" for r in results) else "실행 기록"
    path = report.write_report(results, f"{src.name} 의 마지막 실행분 ({block[0]['time']} ~ {block[-1]['time']})", mode)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

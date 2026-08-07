"""CLI 엔트리포인트.

  python -m ludis.cli --offline          # 합성 데이터로 파이프라인 검증
  python -m ludis.cli                    # 실제 수집
  python -m ludis.cli --history 2024-01-01   # 시계열 CSV 동시 저장
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

from .pipeline import Registry, build
from .render import write_all


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ludis", description="Macro Daily Monitor")
    p.add_argument("-c", "--config", default="config/indicators.yaml")
    p.add_argument("-o", "--outdir", default="output")
    p.add_argument("--offline", action="store_true", help="합성 데이터로 실행 (네트워크 불필요)")
    p.add_argument("--save-panel", action="store_true", help="원계열 CSV 저장")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    if a.quiet:
        warnings.simplefilter("ignore")

    reg = Registry.load(a.config)
    res = build(reg, offline=a.offline)
    paths = write_all(res, a.outdir)

    if a.save_panel:
        Path(a.outdir).mkdir(parents=True, exist_ok=True)
        res["panel"].to_csv(Path(a.outdir) / "panel.csv", encoding="utf-8-sig")

    c = res["composite"]
    print(f"기준일 {res['asof']} | 지표 {len(res['stats'])}종 "
          f"| 스코어 {c['score']:+.2f} ({c['regime']}) "
          f"| 2σ 초과 {len(res['sigma_flags'])} | 괴리 {len(res['divergences'])}")
    for k, v in paths.items():
        print(f"  {k:<12} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

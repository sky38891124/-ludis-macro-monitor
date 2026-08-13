"""수집 → 파생 → 통계 파이프라인."""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from . import sources


# ── 레지스트리 ──────────────────────────────────────────────────────
@dataclass
class Registry:
    meta: dict[str, Any]
    groups: list[dict[str, Any]]
    divergences: list[dict[str, Any]] = field(default_factory=list)
    composite: dict[str, Any] = field(default_factory=dict)
    manual: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(
            meta=cfg.get("meta", {}),
            groups=cfg.get("groups", []),
            divergences=cfg.get("divergences", []),
            composite=cfg.get("composite", {}),
            manual=cfg.get("manual", []),
        )

    def scales(self) -> dict[str, float]:
        """원단위 보정 계수. FRED 백만달러 계열을 십억달러로 맞추는 데 쓴다."""
        return {
            ind["id"]: float(ind["scale"])
            for g in self.groups
            for ind in g.get("indicators", [])
            if ind.get("scale") is not None
        }

    def raw_specs(self, source: str) -> dict[str, str]:
        return {
            ind["id"]: ind["code"]
            for g in self.groups
            for ind in g.get("indicators", [])
            if ind["source"] == source
        }

    @property
    def all_ids(self) -> list[str]:
        ids = []
        for g in self.groups:
            ids += [i["id"] for i in g.get("indicators", [])]
            ids += [d["id"] for d in g.get("derived", [])]
        return ids

    def spec(self, iid: str) -> dict[str, Any]:
        for g in self.groups:
            for item in g.get("indicators", []) + g.get("derived", []):
                if item["id"] == iid:
                    return {**item, "group": g["id"], "group_label": g["label"]}
        return {"id": iid, "label": iid, "unit": "idx", "risk_dir": "none",
                "group": "misc", "group_label": "기타"}


# ── 수집 ────────────────────────────────────────────────────────────
def collect(reg: Registry, offline: bool = False) -> pd.DataFrame:
    start = reg.meta.get("start", "2018-01-01")
    if offline:
        raw_ids = [
            i["id"] for g in reg.groups for i in g.get("indicators", [])
        ]
        panel = sources.fetch_synthetic(raw_ids, start)
    else:
        frames = [
            sources.fetch_fred(reg.raw_specs("fred"), start),
            sources.fetch_yahoo(reg.raw_specs("yahoo"), start),
            sources.fetch_ecos(reg.raw_specs("ecos"), start),
        ]
        frames = [f for f in frames if not f.empty]
        if not frames:
            raise RuntimeError("모든 소스 수집 실패 — 네트워크/키 설정 확인")
        panel = pd.concat(frames, axis=1)

    # 파생지표 계산 전에 원단위를 통일한다.
    for iid, k in reg.scales().items():
        if iid in panel.columns:
            panel[iid] = panel[iid] * k

    panel = panel.sort_index()
    panel = panel[~panel.index.duplicated(keep="last")]
    # 영업일 격자에만 정렬한다. 전진충전은 하지 않는다.
    # ffill 을 하면 미체결일이 직전 종가로 채워져 "1D +0.00%" 라는 허위 신호가 생긴다.
    grid = pd.bdate_range(panel.index.min(), panel.index.max())
    return panel.reindex(grid)


_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def add_derived(reg: Registry, panel: pd.DataFrame, ffill_limit: int = 10) -> pd.DataFrame:
    """expr 는 지표 id 와 산술연산만 허용하는 제한 네임스페이스에서 평가.

    계산에는 전진충전본을 쓰되(주간 지표와 일간 지표의 시점 정렬),
    결과는 구성요소 중 하나라도 실제 관측이 있었던 날짜에만 남긴다.
    그렇지 않으면 파생지표가 휴장일까지 값을 갖게 되어 변화율이 0으로 왜곡된다.
    """
    df = panel.copy()
    filled = panel.ffill(limit=ffill_limit)
    for g in reg.groups:
        for d in g.get("derived", []):
            ns = {c: filled[c] for c in filled.columns}
            try:
                series = eval(d["expr"], {"__builtins__": {}}, ns)  # noqa: S307
            except Exception as exc:  # noqa: BLE001
                warnings.warn(f"[derived] {d['id']} 계산 실패: {exc}", RuntimeWarning)
                continue
            comps = [c for c in _ID_RE.findall(d["expr"]) if c in panel.columns]
            if comps:
                native = panel[comps].notna().any(axis=1)
                series = series.where(native)
            df[d["id"]] = series
    return df


# ── 통계 ────────────────────────────────────────────────────────────
def compute_stats(reg: Registry, df: pd.DataFrame) -> pd.DataFrame:
    zw = int(reg.meta.get("z_window", 252))
    pw = int(reg.meta.get("pct_window", 756))
    lbs = reg.meta.get("lookbacks", [1, 5, 20])

    rows = []
    for iid in df.columns:
        s = df[iid].dropna()
        if len(s) < 30:
            continue
        spec = reg.spec(iid)
        unit = spec.get("unit", "idx")
        last, prev = s.iloc[-1], s.iloc[-2]

        # 금리·스프레드류는 bp 차분, 그 외는 % 변화율
        diff_mode = unit in ("pct", "bp")
        rec: dict[str, Any] = {
            "id": iid,
            "label": spec.get("label", iid),
            "group": spec["group"],
            "group_label": spec["group_label"],
            "unit": unit,
            "risk_dir": spec.get("risk_dir", "none"),
            "note": spec.get("note"),
            "last": float(last),
            "asof": s.index[-1].date().isoformat(),
            "stale": int(len(pd.bdate_range(s.index[-1], df.index[-1])) - 1),
        }
        for lb in lbs:
            if len(s) > lb:
                base = s.iloc[-1 - lb]
                rec[f"chg_{lb}d"] = float(
                    (last - base) * (100 if unit == "pct" else 1) if diff_mode
                    else (last / base - 1) * 100
                )
            else:
                rec[f"chg_{lb}d"] = np.nan

        # 일간 변동의 표준화 (20일 변동성 대비 몇 σ인가)
        chg = s.diff() if diff_mode else s.pct_change()
        sd20 = chg.rolling(20).std().iloc[-1]
        rec["sigma"] = float(chg.iloc[-1] / sd20) if sd20 and not np.isnan(sd20) else np.nan

        # 레벨 z-score / 백분위
        mu, sd = s.rolling(zw).mean().iloc[-1], s.rolling(zw).std().iloc[-1]
        rec["z"] = float((last - mu) / sd) if sd and not np.isnan(sd) else np.nan
        win = s.iloc[-pw:]
        rec["pctile"] = float((win < last).mean() * 100)

        # 임계 레벨 판정
        th = spec.get("thresholds") or {}
        rec["state"] = _state(last, th, spec.get("risk_dir", "none"))
        rec["warn"], rec["alert"] = th.get("warn"), th.get("alert")
        rows.append(rec)

    out = pd.DataFrame(rows).set_index("id")
    out["abs_sigma"] = out["sigma"].abs()
    return out


def _state(v: float, th: dict, risk_dir: str) -> str:
    if not th:
        return "n/a"
    w, a = th.get("warn"), th.get("alert")
    if risk_dir == "down":  # 값이 내려갈수록 위험
        if a is not None and v <= a:
            return "alert"
        if w is not None and v <= w:
            return "warn"
    else:
        if a is not None and v >= a:
            return "alert"
        if w is not None and v >= w:
            return "warn"
    return "ok"


# ── 상관·컴포짓·괴리 ────────────────────────────────────────────────
def stock_bond_corr(df: pd.DataFrame, window: int = 60) -> float | None:
    if not {"SPY", "TLT"} <= set(df.columns):
        return None
    r = df[["SPY", "TLT"]].pct_change().dropna()
    if len(r) < window:
        return None
    return float(r["SPY"].rolling(window).corr(r["TLT"]).iloc[-1])


def composite_score(reg: Registry, stats: pd.DataFrame) -> dict[str, Any]:
    w = (reg.composite or {}).get("weights", {})
    parts, total, wsum = [], 0.0, 0.0
    for iid, wt in w.items():
        if iid in stats.index and not np.isnan(stats.at[iid, "z"]):
            z = float(stats.at[iid, "z"])
            total += wt * z
            wsum += abs(wt)
            parts.append({"id": iid, "label": stats.at[iid, "label"],
                          "z": round(z, 2), "weight": wt,
                          "contrib": round(wt * z, 3)})
    coverage = wsum / sum(abs(v) for v in w.values()) if w else 0.0
    score = total / wsum if wsum else np.nan
    if score >= 1.0:
        regime = "리스크오프"
    elif score >= 0.3:
        regime = "경계"
    elif score <= -1.0:
        regime = "리스크온"
    elif score <= -0.3:
        regime = "완화적"
    else:
        regime = "중립"
    if coverage < 0.7:
        regime += " (참고용·구성지표 결손)"
    parts.sort(key=lambda x: -abs(x["contrib"]))
    missing = [i for i in w if i not in stats.index or np.isnan(stats.at[i, "z"])]
    return {"score": round(float(score), 2), "regime": regime, "parts": parts,
            "coverage": round(coverage, 2), "missing": missing}


def find_divergences(reg: Registry, df: pd.DataFrame, stats: pd.DataFrame) -> list[dict]:
    hits = []
    for d in reg.divergences:
        a, b, lb = d["a"], d["b"], int(d.get("lookback", 5))
        if a not in df.columns or b not in df.columns:
            continue
        sa, sb = df[a].dropna(), df[b].dropna()
        if len(sa) <= lb or len(sb) <= lb:
            continue
        ra = (sa.iloc[-1] / sa.iloc[-1 - lb] - 1) * 100
        rb = (sb.iloc[-1] / sb.iloc[-1 - lb] - 1) * 100
        gap = abs(ra - rb)
        same_sign = np.sign(ra) == np.sign(rb)
        mode = d.get("mode", "sign")
        if mode == "sign":          # 방향이 엇갈릴 때만
            triggered = (not same_sign) and gap >= d["min_gap"]
        elif mode == "gap":         # 방향 무관, 폭 격차
            triggered = gap >= d["min_gap"]
        elif mode == "same_sign":   # 통상 반대여야 할 둘이 동행할 때
            triggered = same_sign and min(abs(ra), abs(rb)) >= d["min_gap"]
        else:
            triggered = False
        if triggered:
            hits.append({
                "a": a, "b": b, "a_label": stats.at[a, "label"] if a in stats.index else a,
                "b_label": stats.at[b, "label"] if b in stats.index else b,
                "lookback": lb, "ra": round(float(ra), 2), "rb": round(float(rb), 2),
                "gap": round(float(gap), 2), "msg": d["msg"],
            })
    return hits


def diagnose(reg: Registry, stats: pd.DataFrame) -> dict[str, Any]:
    """수집 결손과 데이터 지연을 명시한다. 조용히 빠진 지표가 가장 위험하다."""
    expected = reg.all_ids
    got = set(stats.index)
    missing = [
        {"id": i, "label": reg.spec(i).get("label", i),
         "source": reg.spec(i).get("source", "derived"),
         "group": reg.spec(i)["group_label"]}
        for i in expected if i not in got
    ]
    stale = stats[stats["stale"] > 0][["label", "group_label", "stale", "asof"]]
    by_source: dict[str, list[str]] = {}
    for m in missing:
        by_source.setdefault(m["source"], []).append(m["label"])
    return {
        "expected": len(expected), "collected": len(got),
        "coverage": round(len(got) / len(expected), 2) if expected else 0.0,
        "missing": missing, "missing_by_source": by_source,
        "stale": stale.reset_index().to_dict(orient="records"),
    }


def build(reg: Registry, offline: bool = False) -> dict[str, Any]:
    panel = collect(reg, offline=offline)
    df = add_derived(reg, panel)
    stats = compute_stats(reg, df)
    return {
        "diag": diagnose(reg, stats),
        "asof": df.index[-1].date().isoformat(),
        "panel": df,
        "stats": stats,
        "composite": composite_score(reg, stats),
        "divergences": find_divergences(reg, df, stats),
        "sb_corr": stock_bond_corr(df),
        "sigma_flags": stats[stats["abs_sigma"] >= float(reg.meta.get("sigma_flag", 2.0))]
        .sort_values("abs_sigma", ascending=False),
        "manual": reg.manual,
    }

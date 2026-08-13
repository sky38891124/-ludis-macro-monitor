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
def _roll_to_bday(df: pd.DataFrame) -> pd.DataFrame:
    """주말 날짜 관측을 다음 영업일로 이동.

    실업수당 청구(ICSA/CCSA)는 토요일 날짜로 발표된다. 영업일 격자에 그냥
    재색인하면 전 계열이 통째로 사라진다.
    """
    if df.empty:
        return df
    idx = pd.DatetimeIndex(df.index)
    wk = idx.dayofweek >= 5
    if not wk.any():
        return df
    new = idx.where(~wk, idx + pd.offsets.BDay(1))
    out = df.copy()
    out.index = pd.DatetimeIndex(new).normalize()
    # 굴러온 토요일 행은 기존 월요일 행과 인덱스가 겹친다.
    # 행 단위로 버리면 한쪽 계열이 통째로 사라지므로, 열별로 병합한다.
    if out.index.has_duplicates:
        out = out.groupby(level=0).first()
    return out.sort_index()


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
        frames = [_roll_to_bday(f) for f in frames if not f.empty]
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
                # 구성요소가 '모두' 실제 관측된 날에만 값을 남긴다.
                # any 로 두면 순유동성처럼 주간 계열이 섞인 지표가 매일 갱신된 것처럼
                # 보이면서, 실제로는 6일 묵은 값으로 1D 변동을 계산하게 된다.
                native = panel[comps].notna().all(axis=1)
                series = series.where(native)
            df[d["id"]] = series
    return df


# ── 통계 ────────────────────────────────────────────────────────────
def compute_stats(reg: Registry, df: pd.DataFrame) -> pd.DataFrame:
    zw = int(reg.meta.get("z_window", 252))
    pw = int(reg.meta.get("pct_window", 756))
    lbs = reg.meta.get("lookbacks", [1, 5, 20])
    # 주간·월간 계열에 일간 기준 창(252영업일)을 그대로 쓰면 5년치를 보게 된다.
    # 관측 빈도로 나눠 실제 기간을 맞춘다.
    per_year = {"daily": 252, "weekly": 52, "monthly": 12}

    rows = []
    for iid in df.columns:
        s = df[iid].dropna()
        if len(s) < 30:
            continue
        spec = reg.spec(iid)
        unit = spec.get("unit", "idx")
        last, prev = s.iloc[-1], s.iloc[-2]

        # 금리·스프레드류는 bp 차분, 그 외는 % 변화율
        # chg_mode: diff 를 주면 % 대신 절대 차분으로 계산한다.
        # 역레포 잔고처럼 0 근처를 오가는 계열은 % 변화가 노이즈가 된다.
        diff_mode = spec.get("chg_mode") == "diff" or unit in ("pct", "bp")
        if not diff_mode and float(s.tail(504).min()) <= 0:
            # 음수를 오가는 계열(NFCI 등)에 퍼센트를 씌우면 부호가 뒤집힌다.
            # -0.65 → -0.55 는 실제로 +0.10 인데 퍼센트로는 -15.4% 로 나온다.
            diff_mode = True
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
            # 변화량이 % 인지 절대 차분인지. 렌더러가 접미사를 고를 때 쓴다.
            "chg_unit": ("bp" if unit in ("pct", "bp") else unit) if diff_mode else "%",
        }
        for lb in lbs:
            base = _lookback(s, lb)
            if base is None or (not diff_mode and base == 0):
                rec[f"chg_{lb}d"] = np.nan
                continue
            rec[f"chg_{lb}d"] = float(
                (last - base) * (100 if unit == "pct" else 1) if diff_mode
                else (last / base - 1) * 100
            )

        # 일간 변동의 표준화 (20일 변동성 대비 몇 σ인가)
        chg = s.diff() if diff_mode else s.pct_change()
        sd20 = chg.rolling(20).std().iloc[-1]
        rec["sigma"] = float(chg.iloc[-1] / sd20) if sd20 and not np.isnan(sd20) else np.nan

        # 레벨 z-score / 백분위 (발표 주기에 맞춘 창)
        n = per_year.get(spec.get("freq", "daily"), 252)
        zw_i = max(20, round(zw * n / 252))
        pw_i = max(30, round(pw * n / 252))
        mu, sd = s.rolling(zw_i).mean().iloc[-1], s.rolling(zw_i).std().iloc[-1]
        rec["z"] = float((last - mu) / sd) if sd and not np.isnan(sd) else np.nan
        win = s.iloc[-pw_i:]
        rec["pctile"] = float((win < last).mean() * 100)

        # 임계 레벨 판정
        th = spec.get("thresholds") or {}
        rec["state"] = _state(last, th, spec.get("risk_dir", "none"))
        rec["warn"], rec["alert"] = th.get("warn"), th.get("alert")
        rows.append(rec)

    out = pd.DataFrame(rows).set_index("id")
    out["abs_sigma"] = out["sigma"].abs()
    return out


def _lookback(s: pd.Series, lb: int):
    """lb 영업일 전 시점의 값. 없으면 그 이전 마지막 관측.

    위치 기반(iloc[-1-lb])은 중간에 하루가 비면 기준점이 통째로 밀린다.
    Yahoo 일괄 다운로드가 실행마다 특정 티커의 하루치를 빠뜨리는 일이 있어
    같은 데이터로도 5D/20D 값이 달라졌다. 날짜를 기준으로 잡으면 안정된다.
    """
    if len(s) < 2:
        return None
    target = s.index[-1] - pd.offsets.BDay(lb)
    prior = s.loc[:target]
    if len(prior):
        return float(prior.iloc[-1])
    return None


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
        base_a, base_b = _lookback(sa, lb), _lookback(sb, lb)
        if not base_a or not base_b:
            continue
        # 음수 구간을 오가는 계열은 비율 비교가 부호를 뒤집는다. 건너뛴다.
        if base_a <= 0 or base_b <= 0 or sa.iloc[-1] <= 0 or sb.iloc[-1] <= 0:
            continue
        ra = (sa.iloc[-1] / base_a - 1) * 100
        rb = (sb.iloc[-1] / base_b - 1) * 100
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
                "gap": round(float(gap), 2),
                # 방향이 뒤집힌 경우 문구도 뒤집는다. 고정 문구만 쓰면
                # 데이터와 반대로 말하는 설명이 나온다.
                "msg": (d.get("msg_rev") or d["msg"]) if ra < rb else d["msg"],
            })
    return hits


# 발표 주기별 허용 지연(영업일). 이 안쪽이면 정상 게시 시차다.
STALE_BUDGET = {"daily": 3, "weekly": 8, "monthly": 25}


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
    # 구조적 시차(미국장 마감·주간 발표)와 진짜 이상 지연을 구분한다.
    rows = []
    for iid, r in stats[stats["stale"] > 0].iterrows():
        sp = reg.spec(iid)
        budget = sp.get("stale_budget")
        if budget is None:
            budget = STALE_BUDGET.get(sp.get("freq", "daily"), 3)
        rows.append({"id": iid, "label": r["label"], "group_label": r["group_label"],
                     "stale": int(r["stale"]), "asof": r["asof"],
                     "freq": sp.get("freq", "daily"), "budget": budget,
                     "over": int(r["stale"]) > budget})
    stale = [x for x in rows if x["over"]]
    stale.sort(key=lambda x: -x["stale"])
    by_source: dict[str, list[str]] = {}
    for m in missing:
        by_source.setdefault(m["source"], []).append(m["label"])

    # 시점 혼재 탐지: 장중에 수동 실행하면 아시아 시장·FX 만 당일 값이 잡히고
    # 미국 지표는 전일 종가라, 두 블록의 1D 를 나란히 비교하면 어긋난다.
    daily = stats[stats.index.map(lambda i: reg.spec(i).get("freq", "daily") == "daily")]
    intraday: list[dict[str, Any]] = []
    if len(daily):
        newest = daily["asof"].max()
        at_newest = daily[daily["asof"] == newest]
        # 최신 시점을 소수 지표만 갖고 있으면 그 시장은 아직 열려 있을 가능성이 크다.
        # 절반 이상이 공유하는 시점이면 정상 마감이므로 경고하지 않는다.
        if 0 < len(at_newest) < len(daily) * 0.5:
            intraday = [{"id": i, "label": r["label"], "asof": r["asof"]}
                        for i, r in at_newest.iterrows()]
    return {
        "intraday": intraday,
        "expected": len(expected), "collected": len(got),
        "coverage": round(len(got) / len(expected), 2) if expected else 0.0,
        "missing": missing, "missing_by_source": by_source,
        "stale": stale, "stale_normal": len(rows) - len(stale),
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

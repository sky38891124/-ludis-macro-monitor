"""리포트 렌더링: Markdown(노션용) / JSON(대시보드용) / HTML(정적배포용)."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

STATE_KO = {"alert": "경보", "warn": "주의", "ok": "정상", "n/a": "-"}


def _fmt(v: float, unit: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if unit == "pct":
        return f"{v:,.3f}%"
    if unit == "bp":
        return f"{v:,.1f}bp"
    if unit == "krw":
        return f"{v:,.1f}"
    if unit == "bn":
        return f"{v:,.0f}"
    if unit == "ratio":
        return f"{v:,.4f}"
    return f"{v:,.2f}"


def _chg(v: float, unit: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    suffix = "bp" if unit in ("pct", "bp") else "%"
    return f"{v:+,.1f}{suffix}" if abs(v) >= 10 else f"{v:+,.2f}{suffix}"


def _sig(v: float) -> str:
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.1f}σ"


# ── Markdown ────────────────────────────────────────────────────────
def to_markdown(res: dict[str, Any]) -> str:
    st: pd.DataFrame = res["stats"]
    comp = res["composite"]
    L: list[str] = []
    L.append(f"# Macro Daily Monitor — {res['asof']}")
    L.append("")
    L.append(f"**리스크 스코어 {comp['score']:+.2f} ({comp['regime']})** "
             f"· 주식-채권 60일 상관 {res['sb_corr']:+.2f}" if res["sb_corr"] is not None
             else f"**리스크 스코어 {comp['score']:+.2f} ({comp['regime']})**")
    L.append("")

    # 1) 오늘 실제로 움직인 것
    L.append("## 1. 오늘 유의미하게 움직인 지표 (|일간변동| ≥ 2σ)")
    flags: pd.DataFrame = res["sigma_flags"]
    if flags.empty:
        L.append("- 없음. 전 지표 일간 변동이 20일 변동성 2σ 이내 = 노이즈 구간.")
    else:
        for iid, r in flags.head(12).iterrows():
            L.append(f"- **{r['label']}** {_fmt(r['last'], r['unit'])} "
                     f"({_chg(r['chg_1d'], r['unit'])}, {_sig(r['sigma'])}, "
                     f"{r['pctile']:.0f}분위)")
    L.append("")

    # 2) 레벨 경보
    L.append("## 2. 레벨 경보")
    al = st[st["state"].isin(["warn", "alert"])].sort_values("state")
    if al.empty:
        L.append("- 임계 레벨 도달 지표 없음.")
    else:
        for iid, r in al.iterrows():
            L.append(f"- [{STATE_KO[r['state']]}] {r['label']} = {_fmt(r['last'], r['unit'])} "
                     f"(주의 {_fmt(r['warn'], r['unit'])} · 경보 {_fmt(r['alert'], r['unit'])})")
    L.append("")

    # 3) 지표 간 괴리
    L.append("## 3. 정합성 위반 (괴리)")
    if not res["divergences"]:
        L.append("- 감시 대상 쌍 모두 정합적.")
    else:
        for d in res["divergences"]:
            L.append(f"- **{d['a_label']} {d['ra']:+.2f}% vs {d['b_label']} {d['rb']:+.2f}%** "
                     f"({d['lookback']}일, 격차 {d['gap']:.2f}%p) — {d['msg']}")
    L.append("")

    # 4) 전체 패널
    L.append("## 4. 전체 패널")
    for gid in st["group"].unique():
        sub = st[st["group"] == gid]
        L.append("")
        L.append(f"### {sub.iloc[0]['group_label']}")
        L.append("| 지표 | 종가 | 1D | 5D | 20D | σ | z | 분위 |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for iid, r in sub.iterrows():
            L.append(
                f"| {r['label']} | {_fmt(r['last'], r['unit'])} | {_chg(r['chg_1d'], r['unit'])} "
                f"| {_chg(r['chg_5d'], r['unit'])} | {_chg(r['chg_20d'], r['unit'])} "
                f"| {_sig(r['sigma'])} | {r['z']:+.1f} | {r['pctile']:.0f} |"
            )
    L.append("")

    # 5) 컴포짓 기여도
    L.append("## 5. 리스크 스코어 기여도")
    for p in comp["parts"][:6]:
        L.append(f"- {p['label']}: z {p['z']:+.2f} × w {p['weight']:+.2f} = {p['contrib']:+.3f}")
    L.append("")

    # 6) 수동 입력
    L.append("## 6. 수동 확인 항목")
    for m in res["manual"]:
        L.append(f"- [ ] {m}")
    L.append("")
    L.append("## 7. 해석 (직접 작성)")
    L.append("- 가설:")
    L.append("- 반증 조건:")
    L.append("- 포지션 함의:")
    L.append("- 내일 확인할 것:")
    return "\n".join(L)


# ── JSON ────────────────────────────────────────────────────────────
def to_json(res: dict[str, Any]) -> str:
    st = res["stats"].replace({np.nan: None})
    payload = {
        "asof": res["asof"],
        "composite": res["composite"],
        "stock_bond_corr": res["sb_corr"],
        "divergences": res["divergences"],
        "sigma_flags": list(res["sigma_flags"].index),
        "manual": res["manual"],
        "indicators": st.reset_index().to_dict(orient="records"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ── HTML ────────────────────────────────────────────────────────────
CSS = """
:root{
  --ground:#E9ECE6; --panel:#F4F6F1; --rule:#CBD2C7; --rule-strong:#A9B3A6;
  --ink:#151A16; --ink-2:#4A554C; --ink-3:#7C877E;
  --up:#1D6E5F; --down:#B23A2E; --axis:#3A4FA0; --amber:#B77A16;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,monospace;
  --sans:"Pretendard Variable",Pretendard,-apple-system,system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:14px;line-height:1.5;
  background-image:linear-gradient(var(--rule) .5px,transparent .5px),
                   linear-gradient(90deg,var(--rule) .5px,transparent .5px);
  background-size:28px 28px;background-position:-1px -1px}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 80px}
header{display:flex;align-items:baseline;justify-content:space-between;
  gap:16px;flex-wrap:wrap;border-bottom:2px solid var(--ink);padding-bottom:10px}
h1{font-family:var(--mono);font-size:15px;font-weight:600;letter-spacing:.22em;
  text-transform:uppercase;margin:0}
.asof{font-family:var(--mono);font-size:12px;color:var(--ink-2);letter-spacing:.06em}
.readout{display:grid;grid-template-columns:minmax(220px,300px) 1fr;gap:24px;
  margin:24px 0 32px;align-items:start}
.score{background:var(--panel);border:1px solid var(--rule-strong);padding:18px 20px}
.score .num{font-family:var(--mono);font-size:44px;line-height:1;font-weight:600;
  letter-spacing:-.02em}
.score .regime{font-family:var(--mono);font-size:12px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--ink-2);margin-top:8px}
.score dl{margin:16px 0 0;display:grid;grid-template-columns:auto 1fr;gap:4px 12px;
  font-size:12px;border-top:1px solid var(--rule);padding-top:12px}
.score dt{color:var(--ink-3)} .score dd{margin:0;font-family:var(--mono);text-align:right}
.alerts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-content:start}
.card{background:var(--panel);border-left:3px solid var(--rule-strong);padding:9px 13px}
.card.warn{border-left-color:var(--amber)} .card.alert{border-left-color:var(--down)}
.card.div{border-left-color:var(--axis)}
.card b{font-weight:600}
.card .k{font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  color:var(--ink-3);display:block;margin-bottom:2px}
.card p{margin:2px 0 0;font-size:13px;color:var(--ink-2)}
h2{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.2em;
  text-transform:uppercase;color:var(--ink-2);margin:34px 0 8px;
  border-bottom:1px solid var(--rule-strong);padding-bottom:6px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
thead th{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.12em;
  color:var(--ink-3);text-align:right;padding:6px 8px;
  border-bottom:1px solid var(--rule-strong)}
thead th:first-child{text-align:left}
tbody td{padding:5px 8px;text-align:right;font-family:var(--mono);font-size:13px;
  border-bottom:1px solid var(--rule)}
tbody td:first-child{text-align:left;font-family:var(--sans);color:var(--ink)}
tbody tr:hover{background:var(--panel)}
.pos{color:var(--up)} .neg{color:var(--down)} .mut{color:var(--ink-3)}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px;
  vertical-align:middle;background:transparent}
.dot.warn{background:var(--amber)} .dot.alert{background:var(--down)}
/* 시그니처: σ 편차 바 — 0을 중심으로 좌우, ±1σ/±2σ 눈금 */
.sig{width:132px;padding:0 8px!important}
.bar{position:relative;height:16px}
.bar::before{content:"";position:absolute;left:50%;top:0;bottom:0;width:1px;
  background:var(--rule-strong)}
.bar i{position:absolute;top:0;bottom:0;width:1px;background:var(--rule);opacity:.9}
.bar i.t1l{left:37.5%} .bar i.t1r{left:62.5%}
.bar i.t2l{left:25%} .bar i.t2r{left:75%}
.bar b{position:absolute;top:4px;height:8px;background:var(--ink-3);
  transition:width .5s cubic-bezier(.2,.7,.3,1),left .5s cubic-bezier(.2,.7,.3,1)}
.bar b.pos{background:var(--up)} .bar b.neg{background:var(--down)}
.bar b.big{background:var(--axis);height:10px;top:3px}
.note{font-size:12px;color:var(--ink-3);margin:6px 0 0}
ul.manual{list-style:none;padding:0;margin:8px 0;columns:2;column-gap:28px}
ul.manual li{font-size:13px;color:var(--ink-2);padding:3px 0;break-inside:avoid}
ul.manual li::before{content:"☐ ";color:var(--ink-3)}
footer{margin-top:44px;padding-top:12px;border-top:1px solid var(--rule-strong);
  font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:.08em}
@media (max-width:900px){.alerts{grid-template-columns:1fr}}
@media (max-width:760px){
  .readout{grid-template-columns:1fr} ul.manual{columns:1}
  .score .num{font-size:38px}
  .hide-sm{display:none} .wrap{padding:20px 14px 60px}
}
@media (prefers-reduced-motion:reduce){.bar b{transition:none}}
"""


def _bar(sigma: float) -> str:
    if sigma is None or (isinstance(sigma, float) and math.isnan(sigma)):
        return '<div class="bar"><i class="t1l"></i><i class="t1r"></i>' \
               '<i class="t2l"></i><i class="t2r"></i></div>'
    cl = "pos" if sigma > 0 else "neg"
    if abs(sigma) >= 2:
        cl += " big"
    half = min(abs(sigma), 4.0) / 4.0 * 50  # ±4σ 를 폭 전체로
    left = 50 - half if sigma < 0 else 50
    return (f'<div class="bar"><i class="t1l"></i><i class="t1r"></i>'
            f'<i class="t2l"></i><i class="t2r"></i>'
            f'<b class="{cl}" style="left:{left:.1f}%;width:{half:.1f}%"></b></div>')


def to_html(res: dict[str, Any]) -> str:
    st: pd.DataFrame = res["stats"]
    comp = res["composite"]
    sb = res["sb_corr"]

    cards = []
    for iid, r in res["sigma_flags"].head(3).iterrows():
        cards.append(
            f'<div class="card div"><span class="k">이례적 변동 {_sig(r["sigma"])}</span>'
            f'<b>{r["label"]}</b> {_fmt(r["last"], r["unit"])} '
            f'({_chg(r["chg_1d"], r["unit"])})<p>{r["pctile"]:.0f}분위 · 20일 변동성 대비 '
            f'{abs(r["sigma"]):.1f}배</p></div>')
    lv = st[st["state"].isin(["alert", "warn"])].sort_values("state")
    for iid, r in lv.head(3).iterrows():
        cards.append(
            f'<div class="card {r["state"]}"><span class="k">레벨 {STATE_KO[r["state"]]}</span>'
            f'<b>{r["label"]}</b> {_fmt(r["last"], r["unit"])}'
            f'<p>주의 {_fmt(r["warn"], r["unit"])} · 경보 {_fmt(r["alert"], r["unit"])}</p></div>')
    for d in res["divergences"][:2]:
        cards.append(
            f'<div class="card div"><span class="k">정합성 위반 {d["lookback"]}일</span>'
            f'<b>{d["a_label"]} {d["ra"]:+.2f}%</b> vs '
            f'<b>{d["b_label"]} {d["rb"]:+.2f}%</b><p>{d["msg"]}</p></div>')
    rest = (max(0, len(res["sigma_flags"]) - 3) + max(0, len(lv) - 3)
            + max(0, len(res["divergences"]) - 2))
    if rest:
        cards.append(f'<div class="card"><span class="k">그 외</span>'
                     f'<b>{rest}건</b><p>아래 패널에서 확인</p></div>')
    if not cards:
        cards.append('<div class="card"><span class="k">상태</span>'
                     '<b>전 지표 노이즈 구간</b><p>2σ 초과 변동·임계 도달·괴리 모두 없음. '
                     '기록만 남기고 판단은 보류.</p></div>')

    tables = []
    for gid in st["group"].unique():
        sub = st[st["group"] == gid]
        rows = []
        for iid, r in sub.iterrows():
            dot = f'<span class="dot {r["state"]}"></span>' if r["state"] in ("warn", "alert") else '<span class="dot"></span>'
            def c(v, unit, extra=""):
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return f'<td class="mut {extra}">—</td>'
                k = "pos" if v > 0 else ("neg" if v < 0 else "mut")
                return f'<td class="{k} {extra}">{_chg(v, unit)}</td>'
            z = "—" if math.isnan(r["z"]) else f'{r["z"]:+.1f}'
            rows.append(
                f'<tr><td>{dot}{r["label"]}</td>'
                f'<td>{_fmt(r["last"], r["unit"])}</td>'
                f'{c(r["chg_1d"], r["unit"])}{c(r["chg_5d"], r["unit"])}'
                f'{c(r["chg_20d"], r["unit"], "hide-sm")}'
                f'<td class="sig">{_bar(r["sigma"])}</td>'
                f'<td class="hide-sm">{z}</td>'
                f'<td class="hide-sm mut">{r["pctile"]:.0f}</td></tr>')
        tables.append(
            f'<h2>{sub.iloc[0]["group_label"]}</h2><table><thead><tr>'
            f'<th>지표</th><th>종가</th><th>1D</th><th>5D</th>'
            f'<th class="hide-sm">20D</th><th>일간 σ 편차</th>'
            f'<th class="hide-sm">z</th><th class="hide-sm">분위</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table>')

    manual = "".join(f"<li>{m}</li>" for m in res["manual"])
    sb_txt = f"{sb:+.2f}" if sb is not None else "—"
    top = comp["parts"][0]["label"] if comp["parts"] else "—"

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ludis Macro Daily Monitor — {res['asof']}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css">
<style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <h1>Ludis · Macro Daily Monitor</h1>
  <div class="asof">기준일 {res['asof']} · 지표 {len(st)}종</div>
</header>

<section class="readout">
  <div class="score">
    <div class="num">{comp['score']:+.2f}</div>
    <div class="regime">{comp['regime']}</div>
    <dl>
      <dt>주식-채권 60일 상관</dt><dd>{sb_txt}</dd>
      <dt>2σ 초과 변동</dt><dd>{len(res['sigma_flags'])}종</dd>
      <dt>정합성 위반</dt><dd>{len(res['divergences'])}건</dd>
      <dt>최대 기여</dt><dd>{top}</dd>
    </dl>
  </div>
  <div class="alerts">{''.join(cards)}</div>
</section>

{''.join(tables)}

<h2>수동 확인</h2>
<ul class="manual">{manual}</ul>
<p class="note">σ 편차 바는 당일 변동을 20일 표준편차로 나눈 값이다. 눈금은 ±1σ, ±2σ.
2σ를 넘어야 노이즈와 구분된다.</p>

<footer>자동 생성 · 해석과 포지션 판단은 별도 기록</footer>
</div></body></html>"""


def write_all(res: dict[str, Any], outdir: str | Path = "output") -> dict[str, str]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    day = res["asof"]
    paths = {
        "markdown": out / f"{day}.md",
        "json": out / f"{day}.json",
        "html": out / "index.html",
        "latest_json": out / "latest.json",
    }
    paths["markdown"].write_text(to_markdown(res), encoding="utf-8")
    js = to_json(res)
    paths["json"].write_text(js, encoding="utf-8")
    paths["latest_json"].write_text(js, encoding="utf-8")
    paths["html"].write_text(to_html(res), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}

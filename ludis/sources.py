"""데이터 소스 어댑터.

각 fetch_* 함수는 DatetimeIndex(영업일) × 지표 id 컬럼의 DataFrame을 반환한다.
개별 지표 실패가 전체 파이프라인을 중단시키지 않는다 (경고 후 컬럼 누락).
"""
from __future__ import annotations

import io
import os
import time
import warnings
from typing import Iterable

import numpy as np
import pandas as pd
import requests

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
ECOS_API = "https://ecos.bok.or.kr/api/StatisticSearch"
UA = {"User-Agent": "ludis-macro-monitor/1.0"}


def _warn(msg: str) -> None:
    warnings.warn(msg, RuntimeWarning, stacklevel=2)


def _get(url: str, *, params=None, timeout: int = 30, tries: int = 3):
    """일시적 네트워크 오류를 재시도로 흡수한다.

    ECOS 는 해외 러너에서 간헐적으로 연결이 지연된다. 한 번 실패했다고
    지표를 통째로 버리면 리포트가 매일 달라진다.
    """
    last = None
    for i in range(tries):
        try:
            return requests.get(url, params=params, headers=UA, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < tries - 1:
                time.sleep(2 ** i)
    raise last


# ── FRED ────────────────────────────────────────────────────────────
def fetch_fred(specs: dict[str, str], start: str) -> pd.DataFrame:
    """specs: {indicator_id: fred_series_code}

    FRED_API_KEY 가 있으면 공식 API, 없으면 키 없이 동작하는 fredgraph.csv 사용.
    """
    if not specs:
        return pd.DataFrame()
    key = os.environ.get("FRED_API_KEY")
    out: dict[str, pd.Series] = {}
    for iid, code in specs.items():
        try:
            if key:
                r = requests.get(
                    FRED_API,
                    params={
                        "series_id": code,
                        "api_key": key,
                        "file_type": "json",
                        "observation_start": start,
                    },
                    headers=UA,
                    timeout=30,
                )
                r.raise_for_status()
                obs = r.json()["observations"]
                s = pd.Series(
                    {pd.Timestamp(o["date"]): pd.to_numeric(o["value"], errors="coerce") for o in obs}
                )
            else:
                r = requests.get(
                    FRED_CSV,
                    params={"id": code, "cosd": start},
                    headers=UA,
                    timeout=30,
                )
                if r.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {r.status_code} — 키 없는 CSV 경로가 차단됨. "
                        f"FRED_API_KEY 등록 권장"
                    )
                df = pd.read_csv(io.StringIO(r.text))
                if df.shape[1] < 2:
                    raise ValueError(f"예상 밖 응답 형식: {list(df.columns)}")
                # 컬럼명은 DATE / observation_date 등으로 바뀌어 왔다. 위치로 잡는다.
                s = pd.Series(
                    pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                    index=pd.to_datetime(df.iloc[:, 0], errors="coerce"),
                )
                s = s[s.index.notna()]
            out[iid] = s.dropna()
        except Exception as exc:  # noqa: BLE001
            _warn(f"[fred] {iid}({code}) 실패: {exc}")
    return pd.DataFrame(out).sort_index() if out else pd.DataFrame()


# ── Yahoo Finance ───────────────────────────────────────────────────
def fetch_yahoo(specs: dict[str, str], start: str) -> pd.DataFrame:
    """code 에 "A|B|C" 로 대체 티커를 나열하면 앞에서부터 순차 시도한다.

    ^VIX3M, CNH=X 처럼 간헐적으로 응답이 끊기는 심볼 대비.
    """
    if not specs:
        return pd.DataFrame()
    import yfinance as yf

    alts = {iid: [x.strip() for x in code.split("|") if x.strip()]
            for iid, code in specs.items()}
    tickers = list(dict.fromkeys(t for v in alts.values() for t in v))
    try:
        raw = yf.download(
            tickers, start=start, auto_adjust=True, progress=False,
            group_by="column", threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        _warn(f"[yahoo] 일괄 다운로드 실패: {exc}")
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw.xs(
            "Close", axis=1, level=-1
        )
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})

    out: dict[str, pd.Series] = {}
    for iid, cands in alts.items():
        for i, tk in enumerate(cands):
            if tk not in close.columns:
                continue
            s = close[tk].dropna()
            if len(s):
                out[iid] = s
                if i:
                    _warn(f"[yahoo] {iid}: {cands[0]} 실패 → 대체 {tk} 사용")
                break
        else:
            # 일괄 다운로드는 간헐적으로 특정 티커를 누락한다. 개별로 한 번 더.
            for tk in cands:
                try:
                    s = yf.Ticker(tk).history(start=start, auto_adjust=True)["Close"].dropna()
                    if len(s):
                        s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
                        out[iid] = s
                        _warn(f"[yahoo] {iid}: 일괄 실패 → 개별 재조회 성공({tk})")
                        break
                except Exception:  # noqa: BLE001, S110
                    pass
            else:
                _warn(f"[yahoo] {iid} 전 티커 실패: {'|'.join(cands)}")
    return pd.DataFrame(out).sort_index() if out else pd.DataFrame()


# ── ECOS (한국은행) ─────────────────────────────────────────────────
def fetch_ecos(specs: dict[str, str], start: str, end: str | None = None) -> pd.DataFrame:
    """specs: {indicator_id: "STAT_CODE/CYCLE/ITEM1"}  예) "817Y002/D/010200000" """
    if not specs:
        return pd.DataFrame()
    key = os.environ.get("ECOS_API_KEY")
    if not key:
        _warn("[ecos] ECOS_API_KEY 미설정 — 한국 금리 지표 생략")
        return pd.DataFrame()
    end = end or pd.Timestamp.today().strftime("%Y%m%d")
    s0 = pd.Timestamp(start).strftime("%Y%m%d")
    out: dict[str, pd.Series] = {}
    for iid, spec in specs.items():
        try:
            stat, cycle, item = spec.split("/")
            url = f"{ECOS_API}/{key}/json/kr/1/100000/{stat}/{cycle}/{s0}/{end}/{item}"
            r = _get(url, timeout=60)
            r.raise_for_status()
            js = r.json()
            if "StatisticSearch" not in js:
                raise ValueError(js.get("RESULT", js))
            rows = js["StatisticSearch"]["row"]
            s = pd.Series(
                {pd.Timestamp(x["TIME"]): pd.to_numeric(x["DATA_VALUE"], errors="coerce") for x in rows}
            )
            out[iid] = s.dropna()
        except Exception as exc:  # noqa: BLE001
            _warn(f"[ecos] {iid}({spec}) 실패: {exc} — 통계표/항목코드 재확인 필요")
    return pd.DataFrame(out).sort_index() if out else pd.DataFrame()


# ── 오프라인 합성 데이터 (파이프라인 검증·CI 스모크테스트용) ────────
def fetch_synthetic(ids: Iterable[str], start: str, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, pd.Timestamp.today().normalize())
    anchors = {
        "UST3M": 3.9, "UST2Y": 3.87, "UST5Y": 4.0, "UST10Y": 4.35, "UST30Y": 4.9,
        "REAL10Y": 1.95, "BEI10": 2.4, "FWD5Y5Y": 2.4,
        "HY_OAS": 2.77, "IG_OAS": 0.85, "CCC_OAS": 7.2,
        # FRED 원단위 그대로 둔다(백만달러). scale 보정 경로까지 테스트하기 위함.
        "WALCL": 6_748_567, "TGA": 907_324, "RRP": 15, "RESERVES": 2_993_349,
        "SOFR": 3.9, "EFFR": 3.88, "NFCI": -0.55, "BROAD_USD": 119.06, "USDCNH": 6.75,
        "VIX": 17.83, "VIX3M": 19.15, "MOVE": 70.63, "SKEW": 141,
        "USDKRW": 1450, "USDCNH": 7.05, "USDJPY": 152, "EURUSD": 1.09,
        "DXY": 97.845, "BROAD_USD": 121,
        "WTI": 95.67, "BRENT": 101.83, "NATGAS": 2.715,
        "GOLD": 4714.89, "SILVER": 78.448, "COPPER": 6.1835,
        "KTB3Y": 2.6, "KTB10Y": 3.0, "CORP3Y": 3.2, "CD91": 2.7, "BASERATE": 2.5,
        "CLAIMS": 225000, "CONT_CLAIMS": 1900000,
    }
    # 주간 발표 계열과 게시 지연을 재현한다. 지연이 0인 완벽한 표본만 만들면
    # diagnose() 의 지연 판정 경로가 테스트에서 한 번도 실행되지 않는다.
    lag = {"WALCL": 6, "TGA": 6, "RESERVES": 6, "NFCI": 4, "BROAD_USD": 4,
           "CLAIMS": 5, "CONT_CLAIMS": 5, "BASERATE": 3,
           "UST2Y": 2, "UST10Y": 2, "HY_OAS": 2, "CCC_OAS": 2, "SPY": 1, "VIX": 1}
    out = {}
    for i, iid in enumerate(ids):
        base = anchors.get(iid, 100.0)
        vol = 0.008 if base > 10 else 0.02
        path = base * np.exp(np.cumsum(rng.normal(0, vol, len(idx))) - 0.0)
        s = pd.Series(path, index=idx)
        n = lag.get(iid, 0)
        if n:
            s.iloc[-n:] = np.nan
        out[iid] = s
    return pd.DataFrame(out)

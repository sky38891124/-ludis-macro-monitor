# Ludis Macro Daily Monitor

매일 수기로 하던 매크로 체크를 자동 수집 → 표준화 → 규칙 판정 → 리포트 생성까지 처리한다.
출력은 세 가지: 노션 붙여넣기용 Markdown, 대시보드용 JSON, 정적 배포용 HTML.

## 설계 원칙

1. **"소폭 하락"을 쓰지 않는다.** 모든 일간 변동을 20일 표준편차로 나눈 σ 단위로 표기한다.
   2σ 미만은 노이즈로 분류하고 해석하지 않는다.
2. **레벨과 변화를 분리한다.** 레벨은 z-score와 3년 백분위로, 변화는 1/5/20일 구간으로 본다.
   HY 스프레드 2.77처럼 "레벨이 낮아 의미가 없는" 상황은 백분위가 자동으로 알려준다.
3. **인과를 서술하지 않고 정합성을 검사한다.** 자동 판정은 "A와 B가 엇갈렸다"까지만 하고,
   원인 추정은 사람이 별도 항목에 기록한다.
4. **수집 불가 항목을 숨기지 않는다.** 유료 단말·수기 확인이 필요한 항목은
   리포트 하단에 체크리스트로 남긴다.

## 설치

```bash
git clone <repo> && cd ludis-macro-monitor
pip install -r requirements.txt
```

## 실행

```bash
python -m ludis.cli --offline      # 합성 데이터로 파이프라인 검증 (네트워크 불필요)
python -m ludis.cli               # 실제 수집
python -m ludis.cli --save-panel  # 원계열 CSV(output/panel.csv) 동시 저장
```

출력:

| 파일 | 용도 |
|---|---|
| `output/YYYY-MM-DD.md` | 노션·링크드인 붙여넣기 |
| `output/YYYY-MM-DD.json`, `latest.json` | 대시보드 프론트엔드 |
| `output/index.html` | Vercel 정적 배포 |
| `output/panel.csv` | 백테스트·회귀분석 원계열 |

## API 키

| 환경변수 | 필요 여부 | 발급 |
|---|---|---|
| `FRED_API_KEY` | 선택 | 미설정 시 키가 필요 없는 `fredgraph.csv` 경로로 자동 폴백 |
| `ECOS_API_KEY` | 한국 금리 지표에 필요 | ecos.bok.or.kr 오픈API |

Yahoo Finance는 키가 필요 없다.

## 지표 추가

`config/indicators.yaml` 만 수정한다. 코드 변경 불필요.

```yaml
- {id: TIPS5Y, label: 미국 5Y 실질금리, source: fred, code: DFII5,
   unit: pct, risk_dir: up, thresholds: {warn: 2.0, alert: 2.5},
   note: "판정 근거를 여기에 남기면 리포트에 함께 출력된다"}
```

파생지표는 같은 그룹의 `derived` 에 산술식으로 쓴다. 지표 id 와 사칙연산만 허용된다.

```yaml
derived:
  - {id: TIPS_CURVE, label: 5s10s 실질, expr: "REAL10Y - TIPS5Y", unit: pct, risk_dir: none}
```

괴리 규칙은 `divergences` 에 추가한다.

| mode | 발동 조건 |
|---|---|
| `sign` | 두 계열의 수익률 부호가 엇갈리고 격차가 `min_gap`(%p) 이상 |
| `gap` | 부호 무관, 격차가 `min_gap` 이상 |
| `same_sign` | 통상 반대로 움직여야 할 둘이 같은 방향으로 `min_gap` 이상 |

## 자동화

`.github/workflows/daily.yml` 이 평일 06:30 KST(미국 마감 후)에 실행되어
`output/` 을 리포지토리에 커밋한다. Vercel 프로젝트를 이 리포에 연결하고
Output Directory 를 `output` 으로 지정하면 커밋마다 대시보드가 갱신된다.

리포지토리 Settings → Secrets 에 `FRED_API_KEY`, `ECOS_API_KEY` 를 등록한다.

## 읽는 법

- **리스크 스코어**: 설정된 지표들의 z-score 가중합. +1.0 이상 리스크오프, -1.0 이하 리스크온.
  절대 수준보다 **전일 대비 이동 방향**이 정보량이 크다.
- **σ 편차 바**: 가운데가 0, 눈금은 ±1σ·±2σ. 남색으로 굵게 표시된 막대만 해석 대상이다.
- **분위**: 3년 창 기준 백분위. 레벨이 낮은데 분위가 높으면 "낮지만 상대적으로는 올라온" 상태다.
- **1D/5D/20D 색상**: 값의 증감 방향일 뿐 좋고 나쁨이 아니다. 위험 방향은 `risk_dir` 로 별도 관리된다.

## 한계

- ECOS 통계표·항목 코드(`verify: true` 표시 항목)는 ECOS 통계코드 검색에서 반드시 재확인해야 한다.
- Yahoo Finance는 비공식 경로다. 티커 응답이 끊기면 경고만 남기고 해당 지표를 건너뛴다.
- MOVE(`^MOVE`), SKEW(`^SKEW`)는 배포 시점에 따라 응답하지 않을 수 있다. 실패 시 경고 로그를 확인한다.
- 컴포짓 가중치는 사후검증되지 않은 임의값이다. `output/panel.csv` 로 직접 검증한 뒤 조정할 것.

# 다른 컴퓨터에 설치하기 — 재현 체크리스트 (v2.4)

이 스킬은 **원고 + `_작업설정.json` + `_AI판단.json` 세 파일**만 있으면 어느 컴퓨터에서든 같은 PDF 를
만들도록 설계돼 있다(판단은 JSON 한 곳에만 모이고 렌더는 그것만 읽는다). 나머지는 실행 환경 문제라
아래 순서대로 맞추면 된다. 막히면 `--doctor` 와 `selftest.py` 가 어디가 다른지 알려준다.

## 1. 스킬 폴더 통째로 복사

`sp-audiobook-script/` 폴더 전체(SKILL.md · CLAUDE.md · references/ · scripts/ · assets/ · requirements.txt)를
옮긴다. **위치는 어디든 상관없다** — 스크립트는 자기 위치를 스스로 알고(`assets/fonts` 탐색), 산출물은
원고 옆에 만든다.

- Claude Code 에서 스킬로 쓰려면: 개인 스킬 폴더 `~/.claude/skills/sp-audiobook-script/` 에 두거나,
  claude.ai 의 스킬 업로드로 계정에 올리면 어느 세션이든 자동 동기화된다(현재 대표 계정이 쓰는 방식).
- 원고 작업 폴더에는 `CLAUDE.md` 를 복사해 둔다(Claude Code 운영 지침).

## 2. 파이썬과 라이브러리

파이썬 3.9 이상. 스킬 폴더에서:

```bash
pip install -r requirements.txt        # pymupdf, anthropic
```

방법 1(구독제 판단)만 쓰면 `anthropic` 은 없어도 된다. PyMuPDF 는 1.24 이상 — 옛 버전은 `import fitz`
만 되는데 코드가 둘 다 처리한다.

## 3. 한글 폰트 (가장 자주 막히는 곳)

폰트가 없으면 **렌더가 중단된다.** 내장 CJK 폰트는 자간이 벌어져("오 디 오 북") 지침상 금지라,
조용히 폴백하지 않는다. 탐색 순서:

1. `.env` 의 `SP_KFONT=/경로/폰트.ttf` 또는 환경변수 `SP_KFONT` 또는 작업설정 `"font"`
2. 스킬의 `assets/fonts/` 에 넣어 둔 폰트 (**옮길 때 같이 가므로 가장 확실**)
3. OS 기본 경로 (Apple SD Gothic Neo · 나눔고딕 · Noto Sans CJK · 맑은고딕)
4. 시스템 폰트 폴더 전체를 한글 폰트 이름으로 탐색

| OS | 보통 되는 방법 |
|---|---|
| macOS | 기본 내장 Apple SD Gothic Neo 가 자동으로 잡힌다 |
| Windows | 맑은고딕(`C:/Windows/Fonts/malgun.ttf`)이 자동으로 잡힌다 |
| Linux | `sudo apt install fonts-nanum` 또는 `fonts-noto-cjk`. 서버·컨테이너는 대개 없다 |
| 어디서나 | 나눔고딕 TTF(OFL)를 `assets/fonts/` 에 넣는다 — `assets/fonts/README.md` |

## 4. `.env` (방법 2 · API 판단을 쓸 때만)

**원고가 있는 폴더**에 `.env` 파일:

```
ANTHROPIC_API_KEY=sk-ant-...
# 선택
SP_MODEL=claude-sonnet-4-6        # 판단 모델을 바꿀 때
SP_KFONT=/경로/NanumGothic.ttf    # 폰트를 직접 지정할 때
```

`export ANTHROPIC_API_KEY=...` 는 쓰지 않는다 — Claude Code 자체가 구독제가 아닌 종량 과금으로 전환된다.
`--doctor` 가 환경변수에 키가 잡혀 있으면 경고한다.

## 5. 점검 — `--doctor` 와 자가 테스트

```bash
python3 <스킬>/scripts/sp_pipeline.py --doctor     # 파이썬·라이브러리·폰트·.env·모델
python3 <스킬>/scripts/selftest.py                 # 임시 원고로 전 단계(방법 1) 실행, API 호출 없음
```

`selftest.py` 가 `✓ 전부 통과` 를 내면 이 컴퓨터에서 원본 컴퓨터와 같은 결과가 나온다.
(감지 → 요청서 → 판단 JSON → check → render → 캐스팅 → 마스터 → 음향 전용 → 캐시 없음 중단 →
highlight-only 마스터까지 8단계.)

## 6. 실행 명령 형태

```bash
python3 /경로/sp-audiobook-script/scripts/sp_pipeline.py "/원고폴더/원고.pdf" [옵션]
```

- 스크립트는 **절대경로**로, 원고도 경로로. 어느 폴더에서 실행해도 결과는 같다(v2.4).
- 작업설정 · 판단 캐시 · 산출물 · `.env` 는 전부 **원고 옆**에서 읽고 쓴다.
  로그 첫 줄 `[작업폴더] …` 가 그 폴더다.

## 7. 원고 폴더에 무엇이 생기나

| 파일 | 만드는 단계 | 옮길 때 |
|---|---|---|
| `원고.pdf` | 입력 | **필수** — 깨끗한 원본(처리된 출력본 금지) |
| `원고_작업설정.json` | `--init` + 인테이크 | **필수** |
| `원고_AI판단.json` | 판단(방법 1/2) | **필수** — 이것만 있으면 `--render` 로 재현 |
| `원고_캐스팅.json` | `--cast-suggest` + 검토 | 마스터 대본을 다시 뽑을 때 |
| `원고_판단요청.md` | `--export-tasks` | 불필요(재생성 가능) |
| `원고_대사표시.pdf` 등 산출물 | `--render` / `--master` | 불필요(재생성 가능) |

## 8. 버전 확인

스크립트 머리말의 `v2.4` 와 `references/troubleshooting.md` 의 마지막 표(v2.4)가 같은 판인지 본다.
문서와 코드가 어긋나면 코드가 기준이고, 어긋난 곳은 troubleshooting 에 기록한다.

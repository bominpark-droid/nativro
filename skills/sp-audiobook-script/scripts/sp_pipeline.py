#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================================
 SP미디어텍 오디오북 원고 전처리 파이프라인  v2.4
----------------------------------------------------------------------------
 두 가지 산출 버전
   [버전 1] 전체 작업   : 화자표시 + 하이라이트 + 음향 + 출판사 문의 + 인물표
   [버전 2] 음향 전용   : 엔지니어용 씬 분위기·BGM·효과음·페이드아웃만  (--sound-only)

 두 가지 판단 방식 (버전과 무관하게 조합 가능)
   [방법 1] 구독제 토큰 : --export-tasks → Claude Code가 판단 → --check → --render
   [방법 2] API        : 옵션 없이 실행

 v2.0 핵심 변경
   1) 배치 엔진: 본문 글자·삽화를 스캔해 '빈 자리'에만 메모를 놓는다.
      자리가 없으면 페이지 오른쪽에 흰 여백띠(gutter)를 물리적으로 덧붙인다.
      → 원본 콘텐츠는 단 1pt도 가려지지 않는다.
   2) 씬 종료(FADE OUT) 표시: 음악·효과음이 빠져야 할 지점을 본문에 표시.
   3) 음향 메모 확장: 장소 / 상황 / 분위기 / BGM / 앰비언스 / 효과음 / 특이점.
   4) 라벨 모드: quote(따옴표) / narrative(따옴표 없는 대사) / mixed /
      highlight-only(원고에 화자명이 이미 있는 책)
   5) 작업 전 인테이크: <원고>_작업설정.json 으로 도서별 특이점을 고정.

 v2.4 변경 (2026-09-03 · 지침-코드 정합성 손질)
   1) 설정·판단 캐시·산출물·.env 는 **원고 PDF 가 있는 폴더** 기준으로 읽고 쓴다(실행 위치 무관).
   2) --cast-suggest / --master 는 판단 캐시가 없으면 API 를 부르지 않고 중단한다.
   3) 따옴표 없는 대사(narrative)의 화자는 항목 안의 speaker 로 배정한다(id 밀림 방지).
      방법 2(API)가 저장한 캐시에는 assign_ids="adopted" 표식이 붙어 기존 배정을 그대로 쓴다.
   4) --check 가 narrative 항목의 채택/폐기와 speaker 누락을 검사한다.
   5) [최종검증]이 '배정 존재'가 아니라 실제로 칠해진 대사 수와 파일 안의 하이라이트 주석 수를 센다.
   6) --master 도 label_mode=highlight-only 를 지킨다(이름 중복 방지).
   7) 한글 폰트: SP_KFONT → 작업설정 font → 동봉 폰트(assets/fonts) → 시스템 탐색. 못 찾으면 렌더 중단.
   8) 모델은 작업설정 model 또는 .env 의 SP_MODEL 로 바꾼다. --doctor 로 설치 상태를 점검한다.

 필요: pip install -r requirements.txt   (pymupdf · anthropic)
       .env 파일에 ANTHROPIC_API_KEY=sk-ant-...   (환경변수 export 금지)
       한글 폰트 파일 (없으면 --doctor 가 알려준다)
============================================================================
"""
import sys, os, re, json, csv, hashlib, glob
from collections import Counter

# ------------------------- 설정 -------------------------
MODEL     = "claude-sonnet-4-6"      # 작업설정 "model" 또는 .env 의 SP_MODEL 로 덮어쓴다.
                                      # 절감안 "claude-haiku-4-5" 는 컨텍스트 200K — 장편 원고 전문을
                                      # 한 번에 넣으면 넘칠 수 있다(정확도 비교 검증 후 전환할 것).
CHUNK     = 40                        # "응답 잘림" 에러 시 30 → 25
LABEL_FS  = 10                        # 화자 라벨 글자 크기
MARGIN    = 50                        # 브리핑 페이지 여백(pt)

SOUND_RGB = (0.45, 0.10, 0.70)        # 음향 메모 = 보라 (화자 메모와 반드시 구분)
FADE_RGB  = (0.10, 0.35, 0.75)        # 페이드아웃 = 파랑
SND_FS    = 7.2                       # 음향 메모 글자 크기
GUTTER_W  = 150                       # 여백띠 기본 폭(pt)

# ------------------------- 작업 폴더 · .env -------------------------
# 모든 입출력(작업설정·판단 캐시·산출물·.env)은 **원고 PDF 가 있는 폴더** 기준이다(v2.4).
# 예전엔 현재 디렉터리(CWD) 기준이라, 원고 폴더 밖에서 실행하면 설정을 못 읽고 산출물이
# 엉뚱한 곳에 쌓였다. run() 이 원고 경로로 WORKDIR 를 정한다.
WORKDIR    = os.getcwd()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _dotenv_paths():
    """`.env` 탐색 순서: 원고 폴더 → 현재 디렉터리."""
    seen, out = set(), []
    for d in (WORKDIR, os.getcwd()):
        p = os.path.join(d, ".env")
        if p not in seen and os.path.exists(p):
            seen.add(p); out.append(p)
    return out

def dotenv_get(key):
    """`.env` 에서 KEY=값 한 줄을 읽는다. 없으면 ''."""
    for p in _dotenv_paths():
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return ""

def load_api_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip() or dotenv_get("ANTHROPIC_API_KEY")
    if k: return k
    sys.exit(f"[중단] API 키가 없습니다. 원고 폴더({WORKDIR})에 .env 파일을 만들고\n"
             "  ANTHROPIC_API_KEY=sk-ant-... 한 줄을 넣어주세요.\n"
             "  (환경변수 export 는 Claude Code 를 종량 과금으로 전환시키므로 쓰지 않는다)")

try:
    import pymupdf as fitz          # PyMuPDF 1.24+ 의 정식 이름
except ImportError:                 # 옛 PyMuPDF 는 fitz 로만 import 된다
    import fitz

# ------------------------- PDF 읽기 (결정적) -------------------------
# 지면을 읽는 순서. 작업설정의 `reading_order` 로 바꾼다.
#   "page"    : PDF가 내놓는 블록 순서 그대로(기본 — 기존 원고의 대사 id를 보존한다)
#   "columns" : 블록을 x로 열(column)을 갈라 왼→오, 열 안에서 위→아래로 읽는다.
# 그림책·펼침면 조판처럼 한 지면에 단이 둘 이상이면 PDF 블록 순서가 읽는 순서와 달라서,
# 단 끝에서 열린 따옴표가 같은 지면 안에서 닫히는데도 '안 닫혔다'고 보고 다음 쪽 문장을
# 끌어와 붙인다(「하늘고래의 노래」에서 9건 중 8건이 서로 다른 화자의 대사끼리 붙었다).
# 그때 "columns" 로 바꾼다. ★ 이미 판단(_AI판단.json)이 있는 원고에서 이 값을 바꾸면
# 대사 id가 밀려 배정이 어긋나므로, 반드시 대사 텍스트 기준으로 재매핑한 뒤 쓸 것.
READING_ORDER = "page"

def _column_lines(page, gap_ratio=0.06):
    """블록을 열 단위로 묶어 왼→오, 열 안에서 위→아래 순서로 읽는다."""
    blocks = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0: continue
        txt = ["".join(s["text"] for s in l["spans"]) for l in b["lines"]]
        txt = [t for t in txt if t.strip()]
        if txt: blocks.append((fitz.Rect(b["bbox"]), txt))
    if not blocks: return []
    W = page.rect.width
    bounds, prev = [], None
    for x in sorted(b[0].x0 for b in blocks):
        if prev is None or x - prev > W * gap_ratio: bounds.append(x)
        prev = x
    def col_of(r):
        c = 0
        for i, bx in enumerate(bounds):
            if r.x0 >= bx - 1: c = i
        return c
    blocks.sort(key=lambda b: (col_of(b[0]), b[0].y0))
    return [t for _, txt in blocks for t in txt]

# 같은 지면의 텍스트를 한 번만 뽑아 재사용한다.
#   진단·문맥·감지·쪽번호 수집이 각각 전 쪽을 훑어 get_text 를 네 번 돌리는데,
#   삽화가 큰 그림책은 한 쪽 추출에 0.2초가 걸려 그것만으로 70초가 샌다(실측).
#   추출 결과는 파일이 안 바뀌는 한 항상 같으므로 캐시해도 결과가 달라지지 않는다.
_TEXT_CACHE = {}

def _cache_get(page, kind, make):
    key = (id(page.parent), page.number, kind, READING_ORDER)
    if key not in _TEXT_CACHE:
        _TEXT_CACHE[key] = make()
    return _TEXT_CACHE[key]

def clean_lines(page):
    raw = _cache_get(page, "raw", lambda: (
        _column_lines(page) if READING_ORDER == "columns"
        else page.get_text("text").split("\n")))
    out = []
    for l in raw:
        if ".indd" in l: continue
        if re.match(r"^\s*\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.", l.strip()): continue
        out.append(l)
    return out

def diagnose_fonts(doc):
    def ratio(t):
        core = [c for c in t if not c.isspace()]
        if not core: return 0, 0.0
        h = sum(1 for c in core if '가' <= c <= '힣')
        return len(core), h / len(core)
    rep = {"ok": [], "broken": [], "empty": []}
    for pno in range(len(doc)):
        body = "\n".join(clean_lines(doc[pno])); n, r = ratio(body)
        if n < 15: rep["empty"].append(pno + 1)
        elif r > 0.5: rep["ok"].append(pno + 1)
        else: rep["broken"].append(pno + 1)
    return rep

QUOTE_PAIRS = [("\u201c", "\u201d"), ('"', '"'), ("\u300c", "\u300d")]  # “” / "" / 「」

def pick_quote_style(doc, skip):
    counts = []
    for qo, qc in QUOTE_PAIRS:
        c = 0
        for pno in range(1, len(doc) + 1):
            if pno in skip: continue
            c += doc[pno - 1].get_text("text").count(qo)
        counts.append(c)
    i = counts.index(max(counts))
    return QUOTE_PAIRS[i], counts[i]

def _is_standalone_paragraph(doc, pno, frag_text, qo, qc):
    """이 따옴표 쌍이 **그 줄(문단) 전체**를 차지하는 독립 대사인지 판정한다.
    이 책은 대사 한 줄이 곧 문단 하나다(들여쓰기로 시작해 그 줄에서 끝난다).
    반대로 사람‘들’을·‘촉새끼’라는 같은 강조·별명 인용은 훨씬 긴 서술 문장
    **중간에** 끼어 있다 — 글자 수로는 이 둘을 구분할 수 없다(예전 버그: 사람'들'을
    거르려다 '응.'·'네.'·'왜?' 같은 정상 짧은 대사까지 같이 사라졌다. 2026-08-01
    실측 25건 유실). 그래서 길이 대신 **그 줄 전체 글자수 대비 따옴표 안 글자수
    비율**을 본다 — 한 줄을 거의 다 차지하면 독립 대사, 일부만 차지하면 강조.
    ★ 페이지 전체가 아니라 **이 따옴표가 실제 놓인 위치의 줄**만 봐야 한다 —
    안 그러면 같은 페이지 다른 곳의 짧은 줄과 헷갈린다(예: p127의 독립된 '뭐야?'
    문단과, 전혀 다른 줄에 있는 '그러니 ‘뭐야’라는 식의' 속 인용 '뭐야'가 혼동됨)."""
    page = doc[pno - 1]
    quads = page.search_for(qo + frag_text.strip(qo + qc) + qc, quads=True) \
            or page.search_for(frag_text.strip(qo + qc), quads=True)
    if not quads:
        return False
    y0, y1 = quads[0].rect.y0, quads[0].rect.y1
    for x0, ly0, ly1, txt in page_lines(page):
        if ly0 <= (y0 + y1) / 2 <= ly1:
            want = _norm(frag_text.strip(qo + qc))
            line_norm = _norm(txt)
            return bool(want) and want in line_norm and len(line_norm) <= len(want) + 6
    return False

def detect_extra_quote_dialogues(doc, skip, pairs, min_len=1):
    """주 따옴표 외에 **보조 따옴표**로도 대사를 표기하는 책을 위한 감지.
    예: 「아무도 오지 않는 곳에서」는 “”(발화)와 ‘’(무전·회상 속 대사)를 함께 쓴다.
    ‘’ 는 강조(사람‘들’)나 인용(‘촉새끼’라는 별명)으로도 쓰이므로 그건 걸러야
    하는데, **글자 수로 거르면 안 된다**(위 설명 참조). 대신 그 줄을 통째로
    차지하는지로 판정한다.
    `작업설정.json` 의 extra_quote_pairs 로 명시한 책에서만 동작한다(기본 off)."""
    out = []
    for qo, qc in pairs:
        for d in detect_dialogues(doc, skip, qo, qc):
            core = d["text"].strip().strip(qo + qc).strip()
            if len(core) < min_len:
                continue
            if not _is_standalone_paragraph(doc, d["start_page"], d["text"], qo, qc):
                continue
            d["src"] = "quote2"
            out.append(d)
    return out

def detect_dialogues(doc, skip, qo, qc):
    """따옴표 대사 감지. 페이지 걸침 처리 포함. 완전히 결정적."""
    dialogues, open_d = [], None
    for pno in range(1, len(doc) + 1):
        if pno in skip: continue
        for line in clean_lines(doc[pno - 1]):
            # 대사가 열린 채로 단·쪽 경계를 넘으면 그 사이에 있는 **쪽번호 줄**이 대사
            # 안으로 딸려 들어온다('누구로 대결' + '6362' + '하지?'). 쪽번호는 어떤
            # 경우에도 대사가 아니므로 열려 있는 동안에는 숫자만 있는 줄을 건너뛴다.
            if open_d is not None and line.strip().isdigit() and len(line.strip()) <= 4:
                continue
            i = 0
            while i < len(line):
                if open_d is None:
                    j = line.find(qo, i)
                    if j < 0: break
                    k = line.find(qc, j + 1)
                    if k >= 0:
                        dialogues.append({"start_page": pno,
                            "parts": [{"page": pno, "frags": [line[j:k+1]]}],
                            "text": line[j:k+1], "src": "quote"}); i = k + 1
                    else:
                        open_d = {"start_page": pno,
                            "parts": [{"page": pno, "frags": [line[j:]]}],
                            "text": line[j:], "src": "quote"}; i = len(line)
                else:
                    k = line.find(qc, i)
                    frag = line[i:k+1] if k >= 0 else line[i:]
                    if open_d["parts"][-1]["page"] == pno:
                        open_d["parts"][-1]["frags"].append(frag)
                    else:
                        open_d["parts"].append({"page": pno, "frags": [frag]})
                    open_d["text"] += frag
                    if k >= 0:
                        dialogues.append(open_d); open_d = None; i = k + 1
                    else:
                        i = len(line)
    return dialogues

def find_quads(page, frags, cont, qo, qc):
    """대사 한 조각의 위치. 앞 12자 폴백은 '끊긴 하이라이트'의 원인이므로 쓰지 않는다."""
    raw = "".join(frags)
    if cont: raw = re.sub(r"^\s*\d{1,3}", "", raw)
    raw = raw.strip()
    target = raw.replace(qo, "").replace(qc, "").strip()
    for a in (raw, target):
        if not a: continue
        q = page.search_for(a, quads=True)
        if q: return q
    return None

_NORM = re.compile(r"[\s\u200b\u201c\u201d\u2018\u2019\"'\u300c\u300d"
                   r"\-\u2010-\u2015\u2212\u2500\uff0d]+")
def _norm(s):
    return _NORM.sub("", str(s))

def _word_order_key(page, words):
    """단어를 '읽는 순서'로 정렬하는 키. 기본은 PDF가 가진 (블록, 줄, 단어) 인덱스다.
    READING_ORDER='columns' 이면 블록을 열 단위로 다시 세워(왼→오, 열 안에서 위→아래)
    그 순서를 앞에 둔다 — 감지(clean_lines)와 하이라이트가 같은 순서를 봐야 대사 문구가
    단어 열과 어긋나지 않는다. 어긋나면 단 경계를 넘는 대사가 통째로 안 칠해진다."""
    if READING_ORDER != "columns":
        return lambda w: (w[5], w[6], w[7])
    box = {}
    for w in words:                      # 블록별 좌상단 (단어 좌표로 직접 계산)
        b = w[5]
        x0, y0 = box.get(b, (w[0], w[1]))
        box[b] = (min(x0, w[0]), min(y0, w[1]))
    W = page.rect.width
    bounds, prev = [], None
    for x in sorted(v[0] for v in box.values()):
        if prev is None or x - prev > W * 0.06: bounds.append(x)
        prev = x
    def col(x0):
        c = 0
        for i, bx in enumerate(bounds):
            if x0 >= bx - 1: c = i
        return c
    rank = {b: (col(x0), y0) for b, (x0, y0) in box.items()}
    return lambda w: (rank.get(w[5], (0, 0)), w[6], w[7])

def highlight_span(page, text, qo=None, qc=None, used=None):
    """따옴표 여는 곳부터 닫는 곳까지 대사 전체를 빠짐없이 하이라이트한다.
    page.search_for 는 줄바꿈을 넘는 긴 문장을 자주 놓치므로, 단어 박스를 직접
    이어붙여 span 전체를 덮는 quad 목록을 만든다. (앞 몇 단어만 칠하고 끊기는 버그 해결)
    _norm 이 따옴표·공백을 제거하므로 단어에 따옴표가 붙어 있어도 매칭이 끊기지 않는다.

    used: 이 페이지에서 **이미 다른 대사가 차지한 단어**의 (block,line,wordno) 집합.
    없으면 항상 맨 처음 매치만 찾으므로, 같은 짧은 대사('알아.' 등)가 그 페이지에
    여러 번 나오면 전부 첫 번째 자리 위로 겹쳐 그려지고 뒤엣것들은 사라진다
    (2026-08-01 「아무도 오지 않는 곳에서」 p106 '알아.' 3회에서 실측 — 세 번째까지
    전부 같은 자리에 겹쳐졌다). used를 넘기면 이미 쓴 단어는 건너뛰고 다음 자리를 찾는다."""
    want = _norm(text)
    if len(want) < 2:
        return []
    words = page.get_text("words")   # (x0,y0,x1,y1, word, block, line, wordno)
    if not words:
        q = page.search_for(text, quads=True)
        return [x.rect for x in q] if q else []
    ar = annot_rects(page)
    if ar:
        words = [w for w in words if not _in_any(fitz.Rect(w[:4]), ar)]
    # ★ 정렬은 y좌표가 아니라 **(블록, 줄, 단어) 인덱스** = PDF가 가진 읽기 순서로 한다.
    #   y좌표로 정렬하면 한자 병기·번역자 괄호처럼 기준선이 미세하게 다른 글자가 섞인
    #   조판에서 단어 순서가 뒤집힌다(예: '없어 (책임을) 회피하는' → '없어회(책임을)피하는').
    #   그러면 긴 인용문일수록 매칭이 깨진다. 「아주 개인적인 한국사」에서 244개 중 66개
    #   (200자 이상 긴 사료 인용문 43개 포함)가 이 때문에 통째로 하이라이트되지 않았고,
    #   이 정렬로 바꾸자 66개 전부 복구되고 깨진 것은 0개였다.
    words = sorted(words, key=_word_order_key(page, words))
    norms = [_norm(w[4]) for w in words]
    n = len(words)
    used_keys = used if used is not None else set()

    def match_from(start):
        """start 단어부터 want 를 글자 단위로 소진. 사이에 낀 여백 라벨 등
        비매칭 단어(노이즈)는 몇 개까지 건너뛴다. 이미 다른 대사가 쓴 단어는 매치로
        치지 않는다(같은 문구 중복 시 재사용 방지). 성공 시 매칭 단어 목록."""
        pos, k, matched, skips = 0, start, [], 0
        while k < n and pos < len(want):
            w = norms[k]
            key = (words[k][5], words[k][6], words[k][7])
            if key in used_keys:
                if matched:
                    break
                k += 1; continue
            if w and want.startswith(w, pos):          # 단어 전체가 다음 구간과 일치
                matched.append(words[k]); pos += len(w); skips = 0
            elif w and want[pos:] and w.startswith(want[pos:]):  # 마지막 단어 부분 일치
                matched.append(words[k]); pos = len(want); break
            elif matched:                              # 매칭 중 낀 노이즈 단어 → 건너뜀
                skips += 1
                if skips > 3:
                    break
            k += 1
        return matched if pos >= len(want) else None

    for start in range(n):
        w0 = norms[start]
        key0 = (words[start][5], words[start][6], words[start][7])
        if not w0 or key0 in used_keys or not want.startswith(w0[:1]):
            continue
        span = match_from(start)
        if span:
            lines = {}
            for w in span:
                used_keys.add((w[5], w[6], w[7]))
                key = round(w[1], 0)
                r = fitz.Rect(w[0], w[1], w[2], w[3])
                lines[key] = lines[key] | r if key in lines else r
            return list(lines.values())

    if used is None:
        q = page.search_for(text, quads=True)
        return [x.rect for x in q] if q else []
    return []

def build_context(doc, skip):
    chunks = []
    for pno in range(1, len(doc) + 1):
        if pno in skip: continue
        body = "\n".join(l for l in clean_lines(doc[pno - 1]) if l.strip())
        if body.strip(): chunks.append(f"[p{pno}]\n{body}")
    return "\n\n".join(chunks)

# ------------------------- 쪽번호·러닝헤더 떼어내기 -------------------------
def page_furniture(doc):
    """러닝헤더(각 편 제목처럼 여러 쪽에 반복되는 머리말)를 찾아낸다.

    대사가 페이지를 넘어가면 다음 쪽 조각 앞에 쪽번호와 러닝헤더가 달라붙어
    (예: '95살(殺)투동생분 실종이랑…') 하이라이트 매칭이 통째로 깨진다.
    매칭 전에 반드시 떼어내야 한다."""
    c = Counter()
    for i in range(len(doc)):
        lines = page_lines(doc[i])
        if not lines:
            continue
        for row in (lines[0], lines[-1]):          # 머리말·꼬리말 위치만
            t = row[3].strip()
            if 0 < len(t) <= 14:
                c[t] += 1
    return {t for t, n in c.items() if n >= 5 and not t.isdigit()}

_LEAD_NUM = re.compile(r"^\s*\d{1,4}\s*")

def strip_furniture(frag, furn):
    """조각 앞에 붙은 쪽번호·러닝헤더를 반복적으로 떼어낸다."""
    s = str(frag)
    for _ in range(4):
        before = s
        s = _LEAD_NUM.sub("", s)
        for h in (furn or ()):
            if s.startswith(h):
                s = s[len(h):]
        s = s.lstrip()
        if s == before:
            break
    return s

# ------------------------- 대시(-) 대사 결정적 감지 -------------------------
# 하이픈·대시 변형 전체 (대사용). 대시 뒤 공백은 있어도 없어도 됨.
_DASH = re.compile(r"^[\s\u00a0]*[\-\u2010-\u2015\u2212\u2500\uff0d][\s\u00a0]*(?=\S)")
_DASH_HEAD = re.compile(r"^[\s\u00a0]*[\-\u2010-\u2015\u2212\u2500\uff0d][\s\u00a0]*")

# 텍스트 마크업 주석(형광·밑줄·취소선·물결) — 원본 글자 **위에 덧칠**된 것이라
# 그 사각형 안에는 주석 글자가 아니라 **본문 글자**가 있다. 제외하면 안 된다.
_MARKUP_ANNOTS = {8, 9, 10, 11}   # Highlight / Underline / Squiggly / StrikeOut

def annot_rects(page):
    """페이지의 주석(사용자 메모 등) 사각형 목록. 텍스트 분석에서 제외하기 위함.
    단 형광펜류 마크업은 본문 위에 덧칠된 것이므로 제외 대상이 아니다 —
    이걸 빼지 않으면 담당자가 형광으로 짚어둔 대사가 통째로 감지·하이라이트에서
    사라진다(「아무도 오지 않는 곳에서」 p11·28·40·253에서 실측)."""
    out = []
    try:
        for a in (page.annots() or []):
            if a.type[0] in _MARKUP_ANNOTS:
                continue
            out.append(fitz.Rect(a.rect))
    except Exception:
        pass
    return out

def _in_any(r, rects, tol=1.0):
    cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
    for a in rects:
        if (a.x0 - tol) <= cx <= (a.x1 + tol) and (a.y0 - tol) <= cy <= (a.y1 + tol):
            return True
    return False

def page_lines(page):
    """페이지 본문 라인 목록 [(x0,y0,y1,text)] — 주석 영역은 제외."""
    return _cache_get(page, "lines", lambda: _page_lines_uncached(page))

def _page_lines_uncached(page):
    ar = annot_rects(page)
    lines = []
    try:
        d = page.get_text("dict")
    except Exception:
        return lines
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            txt = "".join(s["text"] for s in l.get("spans", []))
            if not txt.strip():
                continue
            r = fitz.Rect(l["bbox"])
            if _in_any(r, ar):
                continue
            lines.append((r.x0, r.y0, r.y1, txt))
    lines.sort(key=lambda t: (round(t[1], 1), t[0]))
    return lines

def detect_dash_dialogues(doc, skip):
    """대시(- / — / ―)로 시작하는 대사를 **라인 기하**로 잡는다.
    대사가 여러 줄로 감기면(들여쓰기 없이 왼쪽 여백으로 되돌아오는 줄) 끝까지 흡수한다.
    다음 문단(들여쓰기된 첫 줄)·새 대시·따옴표가 나오면 멈춘다. 주석은 무시한다."""
    made = []
    for pno in range(1, len(doc) + 1):
        if pno in skip:
            continue
        lines = page_lines(doc[pno - 1])
        if not lines:
            continue
        base_x = min(l[0] for l in lines)
        indent_thresh = base_x + 8
        i = 0
        while i < len(lines):
            x0, y0, y1, txt = lines[i]
            if not _DASH.match(txt):
                i += 1
                continue
            frags = [_DASH_HEAD.sub("", txt).strip()]
            last_y1 = y1
            lh = max(6.0, y1 - y0)
            j = i + 1
            while j < len(lines):
                nx0, ny0, ny1, ntxt = lines[j]
                gap = ny0 - last_y1
                head = ntxt.lstrip()[:1]
                if gap < lh * 1.2 and nx0 <= indent_thresh \
                        and not _DASH.match(ntxt) and head not in ("\u201c", '"', "\u300c"):
                    frags.append(ntxt.strip()); last_y1 = ny1; j += 1
                else:
                    break
            text = re.sub(r"[ \u00a0]+", " ", " ".join(frags)).strip()
            if len(text) >= 2:
                made.append({"start_page": pno,
                             "parts": [{"page": pno, "frags": [text]}],
                             "text": text, "src": "dash"})
            i = j
    return made

# ------------------------- 따옴표 없는 대사 (narrative 모드, AI) -------------------------
def attach_narrative_dialogues(doc, skip, ai_lines, dropped=None):
    """AI가 지목한 '따옴표 없는 대사 문구'를 본문에서 찾아 대사 객체로 만든다.
    ai_lines: [{"page":정수,"anchor":"본문에 그대로 있는 문구","speaker":...}]
    anchor를 원고에서 못 찾으면 버린다(지어낸 문구 방지). dropped 리스트를 주면 버린 항목을
    (번호, page, anchor, 이유) 로 채워 준다(--check 보고용).
    ★ 최소 길이를 4자로 두면 '응.'·'네.'·'숨.' 같은 정상적인 한두 글자 대사까지
    통째로 버려진다(2026-08-01 실측 12건 유실 — 짧은 대답이 잦은 원고에서 특히 큼).
    대신 **그 페이지에 이 문구가 한 번만 나오는지**로 안전성을 확인한다 — 여러 번
    나오면(어디를 가리키는지 모호해서) 버리고, 한 번뿐이면 아무리 짧아도 채택한다."""
    made = []
    for idx, it in enumerate(ai_lines or []):
        d, why = _narrative_item(doc, skip, idx, it)
        if d is None:
            if dropped is not None:
                dropped.append((idx, it.get("page"), str(it.get("anchor", ""))[:30], why))
            continue
        made.append(d)
    return made

def _narrative_item(doc, skip, idx, it):
    """narrative 항목 하나를 검증해 (대사 객체, 폐기 이유) 를 돌려준다. 채택이면 이유는 ''."""
    try:
        pno = int(it.get("page", 0))
    except (TypeError, ValueError):
        return None, "page 가 정수가 아님"
    if not (1 <= pno <= len(doc)) or pno in skip:
        return None, "페이지 범위 밖 또는 깨진 쪽"
    anchor = str(it.get("anchor", "")).strip()
    if len(anchor) < 1:
        return None, "anchor 비어 있음"
    hits = doc[pno - 1].search_for(anchor)
    if not hits:
        return None, "원고에 없는 문구"
    if len(anchor) < 4 and len(hits) > 1:
        return None, "짧은 문구가 그 쪽에 여러 번 나와 위치가 모호함"
    return {"start_page": pno,
            "parts": [{"page": pno, "frags": [anchor]}],
            "text": anchor, "src": "narrative",
            # cont=true → 바로 앞 항목에서 이어지는 같은 발화의 다음 줄.
            #   긴 발화를 지면의 줄 단위로 나눠 넣을 때(따옴표가 없어 한 항목으로
            #   잡을 수 없는 경우) 줄마다 화자 라벨이 붙는 것을 막는다. 색은 모두
            #   칠하되 라벨은 첫 줄에만 — 「하늘고래의 노래」 p42 지혜의 숲 목소리.
            "cont": bool(it.get("cont")),
            # 항목 안의 판단 — 방법 1 에서는 이것이 그 대사의 화자 배정이다(v2.4).
            "ai_speaker": str(it.get("speaker", "") or "").strip(),
            "ai_confidence": str(it.get("confidence", "") or "").strip().lower(),
            "ai_reason": str(it.get("reason", "") or "").strip(),
            "nar_idx": idx}, ""

def narrative_assigns(assigns, dials, n0, positional_ok):
    """따옴표 없는 대사(id ≥ n0)의 화자 배정을 확정한다(v2.4).

    id 는 **채택된 순서**다. 방법 1 의 판단요청서는 narrative 항목 안에 speaker 를 적으라고
    지시하므로 항목 값이 원본이고, assigns 에 id ≥ n0 로 들어온 항목은(예전 요청서 호환용으로
    받되) 무시한다 — 예전엔 'narrative i번째 = id n0+i' 였는데, anchor 를 못 찾은 항목이
    버려지면 그 뒤 배정이 한 칸씩 밀려 다른 대사에 다른 화자가 붙었다.
    방법 2(API)는 채택이 끝난 뒤에 배정하므로 assigns 가 원본이다(positional_ok=True —
    캐시의 assign_ids="adopted" 표식)."""
    base = [a for a in (assigns or []) if isinstance(a.get("id"), int) and a["id"] < n0]
    pos = {a["id"]: a for a in (assigns or []) if isinstance(a.get("id"), int) and a["id"] >= n0}
    out = list(base)
    for k in range(n0, len(dials)):
        d = dials[k]
        if positional_ok and k in pos:
            out.append(pos[k]); continue
        out.append({"id": k, "speaker": d.get("ai_speaker") or "미상",
                    "confidence": "high" if d.get("ai_confidence") == "high" else "low",
                    "reason": d.get("ai_reason") or "따옴표 없는 대사(항목 내 판단)"})
    if pos and not positional_ok:
        print(f"[배정] assigns 의 id ≥ {n0} 항목 {len(pos)}건은 무시하고, 따옴표 없는 대사 "
              f"{len(dials) - n0}건은 narrative 항목 안의 speaker 로 배정합니다(밀림 방지)")
    return out

# ============================================================================
#  배치 엔진 — 원본을 절대 가리지 않는다
# ----------------------------------------------------------------------------
#  1) 페이지에서 글자·삽화·도형이 차지한 사각형을 전부 모은다.
#  2) 메모 크기가 들어갈 '완전히 빈 자리'를 앵커 근처에서 찾는다.
#  3) 못 찾으면 그 페이지는 실패로 기록한다.
#  4) 실패가 있으면 문서 오른쪽에 흰 여백띠(gutter)를 덧대고 전부 다시 배치한다.
#     여백띠는 원본 위에 겹치는 게 아니라 캔버스를 넓히는 것이라 무손상이다.
# ============================================================================
def occupancy(page):
    """이 페이지에서 실제 내용이 있는 영역 전부(글자줄·이미지·도형)."""
    rs = []
    try:
        d = page.get_text("dict")
    except Exception:
        d = {"blocks": []}
    for b in d.get("blocks", []):
        if b.get("type") == 0:
            for l in b.get("lines", []):
                rs.append(fitz.Rect(l["bbox"]))
        else:
            rs.append(fitz.Rect(b["bbox"]))
    # 도형: 페이지 절반을 넘게 덮는 것은 배경·재단 프레임이므로 내용으로 치지 않는다.
    # (이걸 안 걸러내면 페이지 전체가 '점유됨'이 되어 배치가 전부 실패한다)
    parea = page.rect.width * page.rect.height
    try:
        for dr in page.get_drawings():
            r = fitz.Rect(dr["rect"])
            if r.width > 2 and r.height > 2 and (r.width * r.height) < parea * 0.5:
                rs.append(r)
    except Exception:
        pass
    return [r for r in rs if (not r.is_empty) and r.width > 0 and r.height > 0]

def _clear(r, occ, pad):
    rr = fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)
    for o in occ:
        if rr.intersects(o):
            return False
    return True

def _y_scan(near_y, H, h, step=5.0):
    ys, k = [], 0
    while k * step < H:
        for s in (1, -1):
            y = near_y - h / 2 + s * k * step
            if 2 <= y <= H - h - 2:
                ys.append(y)
        k += 1
        if len(ys) > 400: break
    return ys

def find_slot(occ, prect, w, h, near_y, pad=3.5, xs=None, max_dy=None):
    """겹치지 않는 자리를 앵커 y에 가장 가깝게 찾는다. 없으면 None.
    xs      : x 후보를 직접 지정(라벨을 대사 바로 왼쪽에 붙일 때 사용)
    max_dy  : 앵커에서 이 거리 안에만 놓는다(라벨이 엉뚱한 줄로 가는 것 방지)"""
    W, H = prect.width, prect.height
    if w > W - 4 or h > H - 4:
        return None
    if xs is None:
        xs = [W - w - 3, 3, (W - w) / 2]
    best = None
    for x in xs:
        if x < 0 or x > W - w:
            continue
        for y in _y_scan(near_y, H, h):
            d = abs((y + h / 2) - near_y)
            if max_dy is not None and d > max_dy:
                continue
            r = fitz.Rect(x, y, x + w, y + h)
            if _clear(r, occ, pad):
                if best is None or d < best[0]:
                    best = (d, r)
                break
    return best[1] if best else None

def make_gutter_doc(src_path, gw):
    """원본 각 페이지 오른쪽에 여백을 덧댄다. show_pdf_page 로 다시 그리면 **주석이 사라지므로**
    (사용자가 원고에 남긴 빨강 메모 등), fitz.open 으로 열어 mediabox 만 오른쪽으로 넓힌다.
    → 본문·삽화·주석이 좌표 그대로 보존되고 오른쪽에 빈 여백만 생긴다."""
    out = fitz.open(src_path)
    for p in out:
        r = p.rect
        p.set_mediabox(fitz.Rect(r.x0, r.y0, r.x1 + gw, r.y1))
    return out

# ------------------------- 텍스트 박스 그리기 (폰트 임베드 · 확실히 렌더됨) ---
# 한글 폰트 — PyMuPDF 내장 CJK 폰트("korea")는 글자 간격이 벌어져("오 디 오 북") 쓰지 않는다.
# 탐색 순서 (v2.4):
#   1) 명시 지정: 환경변수 SP_KFONT → .env 의 SP_KFONT → 작업설정 "font"
#   2) 스킬에 동봉한 폰트: <스킬>/assets/fonts/*.ttf|*.otf|*.ttc
#   3) OS 별로 잘 알려진 경로
#   4) 시스템 폰트 폴더 전체를 한글 폰트 이름 패턴으로 탐색
# 후보는 실제로 한글 글리프('가')가 있는지 확인한 것만 채택한다. 예전엔 경로 6개만 보고
# 없으면 내장 CJK 로 **조용히** 폴백해 다른 컴퓨터에서 품질이 떨어졌다.
_FONT_KNOWN = ("/System/Library/Fonts/AppleSDGothicNeo.ttc",
               "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
               "/Library/Fonts/NanumGothic.ttf",
               "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
               "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
               "C:/Windows/Fonts/malgun.ttf")
_FONT_ROOTS = ("/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts",
               "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts",
               "~/.local/share/fonts", "C:/Windows/Fonts",
               os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"))
_FONT_PATTERNS = ("*Nanum*Gothic*", "*NanumBarunGothic*", "*NotoSansCJK*", "*NotoSansKR*",
                  "*NotoSerifCJK*", "*NotoSerifKR*", "malgun*", "*AppleSDGothicNeo*",
                  "AppleGothic*", "*Pretendard*", "*Gowun*", "*SpoqaHanSans*", "gulim*",
                  "batang*", "*Nanum*Myeongjo*")

def _has_hangul(path):
    try:
        return fitz.Font(fontfile=path).has_glyph(ord("가"))
    except Exception:
        return False

def _find_korean_fontfile(explicit=""):
    for p in (explicit, os.environ.get("SP_KFONT", ""), dotenv_get("SP_KFONT")):
        p = os.path.expanduser(str(p or "").strip())
        if p:
            if os.path.exists(p) and _has_hangul(p):
                return p
            print(f"[폰트] 지정한 폰트를 쓸 수 없습니다(없거나 한글 글리프 없음): {p}")
    for pat in ("*.ttf", "*.otf", "*.ttc"):
        for p in sorted(glob.glob(os.path.join(SCRIPT_DIR, "..", "assets", "fonts", pat))):
            if _has_hangul(p):
                return os.path.abspath(p)
    for p in _FONT_KNOWN:
        if os.path.exists(p) and _has_hangul(p):
            return p
    for root in _FONT_ROOTS:
        root = os.path.expanduser(root)
        if not root.strip() or not os.path.isdir(root):
            continue
        for pat in _FONT_PATTERNS:
            for ext in (".ttf", ".otf", ".ttc", ".TTF", ".OTF", ".TTC"):
                for p in sorted(glob.glob(os.path.join(root, "**", pat + ext), recursive=True)):
                    if _has_hangul(p):
                        return p
    return None

KFONT = None                # resolve_kfont() 가 채운다
MUSIC = "\u25a0"            # 폰트 확정 전 기본값(■). resolve_kfont() 가 ♪ 로 바꿔 준다
DASH  = "-"

def _glyph(ch, fallback):
    """폰트에 없는 기호는 두부(⊠)로 깨지므로 미리 확인해 대체한다."""
    try:
        f = fitz.Font(fontfile=KFONT) if KFONT else fitz.Font("cjk")
        return ch if f.has_glyph(ord(ch)) else fallback
    except Exception:
        return fallback

def resolve_kfont(explicit="", quiet=False):
    """한글 폰트를 확정하고 기호(♪·─)의 대체 여부를 다시 계산한다. run() 이 작업설정을 읽은 뒤 부른다."""
    global KFONT, MUSIC, DASH
    KFONT = _find_korean_fontfile(explicit)
    MUSIC = _glyph("\u266a", "\u25a0")      # ♪ 없으면 ■
    DASH  = _glyph("\u2500", "-")
    if not quiet:
        print(f"[폰트] 한글 폰트: {KFONT}" if KFONT else
              "[폰트] ✗ 한글 폰트를 찾지 못했습니다 — 렌더 단계에서 중단됩니다 (--doctor 참고)")
    return KFONT

def _require_kfont():
    """렌더 직전에 부른다. 폰트가 없으면 내장 CJK 로 조용히 폴백하지 않고 중단한다(지침: 자간 벌어짐 금지)."""
    if KFONT:
        return
    sys.exit("[중단] 한글 폰트를 찾지 못해 렌더할 수 없습니다. 내장 CJK 폰트는 자간이 벌어져 쓰지 않습니다.\n"
             "  해결: ① 한글 TTF/OTF 를 설치하거나(나눔고딕 · Noto Sans CJK · 맑은고딕 등)\n"
             "        ② <스킬>/assets/fonts/ 에 폰트 파일을 넣거나\n"
             "        ③ .env 에 SP_KFONT=/폰트/경로.ttf 또는 작업설정에 \"font\" 를 지정한다.\n"
             "  python3 sp_pipeline.py --doctor 로 확인할 수 있다.")

def _font():
    return fitz.Font(fontfile=KFONT) if KFONT else fitz.Font("cjk")

def wrap_lines(font, s, width, size):
    out = []
    for para in str(s).split("\n"):
        cur = ""
        for ch in para:
            t = cur + ch
            if font.text_length(t, size) > width and cur:
                out.append(cur); cur = ch
            else:
                cur = t
        out.append(cur)
    return out or [""]

def box_size(font, s, max_w, size, lh=1.32, pad=3.5):
    lines = wrap_lines(font, s, max_w - 2 * pad, size)
    w = min(max_w, max(font.text_length(l, size) for l in lines) + 2 * pad + 1)
    return w, len(lines) * size * lh + 2 * pad, lines

def draw_note(page, rect, lines, size, rgb, lh=1.32, pad=3.5, border=True, fill=(1,1,1)):
    if fill:
        page.draw_rect(rect, fill=fill, color=(rgb if border else None), width=0.6)
    y = rect.y0 + pad + size * 0.86
    for l in lines:
        page.insert_text(fitz.Point(rect.x0 + pad, y), l, fontsize=size,
                         fontname=("kf" if KFONT else "korea"),
                         fontfile=KFONT, color=rgb)
        y += size * lh

# ============================================================================
#  AI 판단 (프롬프트) — 결정적 코드와 분리. 결과는 <원고>_AI판단.json 한 곳에만.
# ============================================================================
def get_text(msg):
    return "".join(b.text for b in msg.content if b.type == "text")

def extract_json(t, kind="auto"):
    t = re.sub(r"^```(json)?|```$", "", t.strip(), flags=re.M).strip()
    pat = r"\{.*\}" if kind == "obj" else r"\[.*\]" if kind == "arr" else r"[\[{].*[\]}]"
    m = re.search(pat, t, re.S)
    return json.loads(m.group(0) if m else t)

def api_call(client, system, user, max_tokens=8000):
    msg = client.messages.create(model=MODEL, max_tokens=max_tokens,
        system=system, messages=[{"role": "user", "content": user}])
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("응답 잘림: CHUNK를 낮추거나 max_tokens를 올리세요")
    return get_text(msg)

ANALYZE_SYSTEM = """너는 오디오북 제작사의 수석 편집자다. 원고 전체를 읽고 성우 캐스팅과 녹음 디렉팅에 필요한 '제작 브리핑'을 만든다. 아래 항목을 빠짐없이 분석하라.

1. cast(등장 화자): 대사가 한 번이라도 있는 인물 전원. 단역도 반드시 포함.
 - name: 표준 이름(이름 없으면 역할명, 예: '소방관아줌마'). 화자당 하나로 통일.
 - short: 라벨용 축약(4자 이내 권장, 예: '소방관아저씨'→'소방관').
 - gender: '남'/'여'/'미상'. 본문 근거(호칭·지문) 없이 이름만으로 단정하지 말 것. 미상이면 publisher_questions에 추가.
 - gender_suggest: gender가 '미상'이어도 **성우를 어느 성별로 쓸지 우리 제안을 반드시 채운다**('남' 또는 '여'). 캐릭터의 말투·역할·나이로 판단한다(예: 위협적인 악역 상어→'남', 감정 없는 중성적 어른→'여', 아이→'여'). 이 값이 비면 캐스팅이 엉뚱한 성우에게 붙는다. gender가 '남'/'여'면 같은 값을 넣는다.
 - age_inline: 라벨용 짧은 나이(예: '10세','40대','노년'). age_full: 상세(예: '10세(초등 3학년)'). age_basis: 본문 근거. 본문에 나이 단서가 있으면 반드시 그 단서로 정확한 나이를 쓰고, 없으면 추정임을 명시.
 - voice_note: 성우 연기 톤 가이드 1문장(말투·성격·특이점).
 - casting_note: 캐스팅 참고. [제작 관례] 아동 캐릭터는 성별과 무관하게 여성 성우가 연기하므로, 아동이면 '여성 성우 캐스팅(아동 관례)'을 명시.
 - same_person: 동일 인물의 다른 연령대 항목이 있으면 그룹명. ★동일 인물이 시간 경과로 여러 연령대로 등장하면 연령대별로 별도 cast 항목으로 분리(예: '영수·아동기', '영수·노년기')하고 same_person으로 묶은 뒤, 각 연령대의 톤 차이를 voice_note에 쓸 것.

2. summary: 성우·엔지니어가 **녹음 직전에 30초 안에 읽고 작품을 파악**할 수 있는 줄거리·톤 요약 4~6문장. 누가 주인공이고 무엇을 하려 하며 어떤 정서로 끝나는지, 그리고 낭독 톤의 기본 방향(예: 따뜻하고 느리게, 긴장을 끌고 가는 속도)을 포함한다. 스포일러를 피하지 말 것 — 성우는 결말을 알고 연기해야 한다.

3. pov: {"type": "1인칭/3인칭/혼합", "notes": "화자가 누구인지, 전환이 있으면 어디서 어떻게"}. 내레이터 캐스팅에 영향 주는 정보 포함.

4. special_notes: 성우·엔지니어가 미리 알아야 할 작품 특이사항 목록 [{"topic","detail"}]. 예: 시점 전환, 인물 관계의 반전, 사투리(지역·해당 인물), 외국인 캐릭터(억양 처리), 시간 구조(회상·시간 도약), 노래/시 낭송 구간, 의성어가 많은 구간. 없으면 빈 배열.

5. publisher_questions: 출판사에 확인이 필요한 포인트 [{"question","context","our_suggestion"}]. 우리가 자체 판단으로 제안은 하되 최종 확인이 필요한 것들. 예: 성별 미상 캐릭터의 성우 성별, 사투리를 실제 사투리로 연기할지 표준어로 순화할지, 욕설·비속어 수위, 외국어 문장을 원어로 읽을지, 노래 구간 처리(낭독/노래), 이름·용어의 공식 발음. our_suggestion에는 우리의 추천안과 근거를 쓸 것.

6. pronunciations(외국어 읽기): 원고 본문에 **한글이 아닌 문자로 적혀 있고, 읽는 법이 원고에 안 적힌** 표기만 [{"term","kind","reading","note"}].
 - 대상: (a) 로마자 표기(외국어 단어·인명·지명·약어·브랜드), (b) 한자, (c) 그 외 비한글 문자.
 - **절대 넣지 마라**: 한글로 적힌 말(고유명사·인명·지명·의성어·사투리·고어 포함), 원고에 이미 괄호·루비로 읽는 법이 병기된 표기, 원고에 등장하지 않는 단어.
 - reading: 성우가 그대로 소리 내면 되는 한글 표기 하나(예: 'Seattle'→'시애틀'). IPA 금지.
 - 해당 표기가 없으면 반드시 빈 배열 []. 억지로 채우지 말 것.

출력은 반드시 위 6개 키(cast, summary, pov, special_notes, publisher_questions, pronunciations)를 가진 하나의 JSON 객체만. 설명 금지."""

SOUND_SYSTEM = """너는 오디오북 음향감독이다. 원고를 씬 단위로 끊어, 엔지니어가 원고를 한 문장씩 읽지 않고도 곡을 고를 수 있도록 씬 카드를 만든다.
원고에서 각 페이지는 [p12] 처럼 표시돼 있다. 페이지 번호는 반드시 본문에 실제로 있는 [pN]의 N 값만 쓴다.

씬은 엔지니어가 '이 문단 덩어리는 어떤 음악'인지 바로 잡을 수 있게 **촘촘히** 끊는다.
 - **거의 한 페이지당 하나, 또는 1~3문단마다 하나씩** 씬 카드를 만든다. 놓치는 구간이 없게 한다.
 - 장소·상황·분위기가 바뀌면 반드시 새 씬. 감정이 고조되거나 전환되는 지점도 새 씬.
 - **바로 앞 씬과 장소·상황·분위기가 완전히 똑같을 때만** 이어간다(같은 대화가 몇 페이지
   계속되는 경우). 조금이라도 달라지면 새 씬으로 쪼갠다.
 - 결과적으로 씬 수는 원고 길이에 비례해 넉넉하게 나온다(짧은 그림책도 10개 이상,
   장편은 수십~100개 이상도 정상). 적게 뽑는 것보다 촘촘히 뽑는 게 낫다.

scenes 배열의 각 원소:
 - page: 씬 시작 페이지(정수). end_page: 씬 끝 페이지(정수).
 - start_anchor: 씬이 시작되는 지점의 **본문에 그대로 있는 8~25자 문구**를 복사. 위치 표시에 쓴다.
 - end_anchor: 씬이 끝나는 지점의 **본문에 그대로 있는 8~25자 문구**를 복사. 그 문구를 읽고 나면 음악이 빠져야 한다.
 - scene: 씬을 한눈에 알아볼 짧은 이름(예: '학교 앞 나무에 식빵이 걸린 아침').
 - place: 장소를 구체적으로(예: '학교 앞 운동장', '한밤중 바닷속', '병실').
 - situation: 그 장면에서 무슨 일이 일어나는지 한 문장.
 - mood: 감정·분위기를 쉼표로 2~4개(예: '흥미로운, 웃긴', '과거 회상, 쓸쓸한', '추격, 긴박한').
 - note: 연출 특이점. 있을 때만(예: '회상 진입 — 톤을 한 단계 낮춤', '아이들 여럿이 겹쳐 떠드는 소란', '낭송 구간'). 없으면 빈 문자열.
 - bgm: 음악 제안을 악기·템포·정서로 구체적으로(예: '느린 첼로 솔로 + 낮은 현 패드, 60bpm, 상실감').
 - ambience: 길게 깔 환경음(예: '파도 + 바닷새', '교실 웅성거림'). 없으면 빈 문자열.
 - fade_out: 이 씬이 끝나는 지점에서 음악·효과음이 확실히 빠져야 하면 true, 다음 씬으로 그대로 이어지면 false.
 - fade_note: fade_out이 true일 때 어떻게 뺄지 짧게(예: '3초 페이드아웃 후 무음 1초', '효과음만 컷, 앰비언스는 유지').
 - sfx: 포인트 효과음 [{"page":정수,"cue":"효과음 설명","anchor":"본문에 그대로 있는 8~20자 문구"}]. 씬당 0~4개, 꼭 필요한 것만.

anchor(start_anchor·end_anchor·sfx의 anchor)는 반드시 원고에 **글자 그대로** 있는 문구를 복사해야 한다. 지어내면 위치를 못 잡아 버려진다.

출력은 {"scenes":[...]} 형태의 JSON 객체만. 설명 금지."""

NARRATIVE_SYSTEM = """너는 오디오북 대본 편집자다. 이 원고는 따옴표 없이 대사가 쓰인 구간이 있다.
본문에서 **인물이 실제로 말하는 대사인데 따옴표가 없는 부분**만 찾아 목록으로 만든다.

 - page: 그 대사가 있는 페이지(정수, [pN]의 N).
 - anchor: 그 대사의 **본문에 그대로 있는 문구**를 복사(8~40자). 지어내지 말 것.
 - speaker: 그 대사를 말한 인물 이름. **반드시 채운다** — 이 값이 그 대사의 화자 배정이 된다(assigns 에 따로 넣지 않는다).
 - confidence: 지문 근거가 명확하면 "high", 문맥 추론이면 "low".
 - reason: 근거를 짧게(어느 단서인지).

속마음·서술·지문은 넣지 않는다. 소리 내어 말한 대사만.
해당 구간이 없으면 빈 배열 []. 출력은 JSON 배열만. 설명 금지."""

def _profile_hint(profile):
    """인테이크(작업설정)를 프롬프트에 주입할 문자열로."""
    if not profile: return ""
    parts = []
    for k, label in (("book_type","도서 유형"), ("dialogue_style","대사 표기 방식"),
                     ("speaker_shown","원고에 화자명 표기"), ("special","이 원고의 특이점"),
                     ("publisher_note","출판사 요청사항"), ("sound_direction","음향 방향")):
        v = str(profile.get(k, "")).strip()
        if v: parts.append(f"- {label}: {v}")
    if not parts: return ""
    return ("\n\n[이 원고에 대해 담당자가 미리 알려준 사항 — 반드시 반영할 것]\n"
            + "\n".join(parts))

def analyze_sound(client, ctx, profile=None):
    sysmsg = SOUND_SYSTEM + _profile_hint(profile)
    for attempt in (0, 1):
        s = sysmsg + ("\n반드시 유효한 JSON 객체만 출력." if attempt else "")
        t = api_call(client, s, f"[원고 전문]\n{ctx}", max_tokens=16000)
        try:
            return extract_json(t, "obj").get("scenes", [])
        except Exception:
            if attempt: raise
    return []

def analyze_book(client, ctx, profile=None):
    sysmsg = ANALYZE_SYSTEM + _profile_hint(profile)
    for attempt in (0, 1):
        s = sysmsg + ("\n반드시 유효한 JSON 객체만 출력." if attempt else "")
        t = api_call(client, s, f"[원고 전문]\n{ctx}", max_tokens=12000)
        try:
            return extract_json(t, "obj")
        except Exception:
            if attempt: raise
    return {}

def find_narrative(client, ctx, profile=None):
    sysmsg = NARRATIVE_SYSTEM + _profile_hint(profile)
    t = api_call(client, sysmsg, f"[원고 전문]\n{ctx}", max_tokens=12000)
    try:
        return extract_json(t, "arr")
    except Exception:
        return []

def assign_chunk(client, ctx, dials, cast_names, id_list, profile=None):
    numbered = "\n".join(f"{i}: {dials[i]['text'].strip()}" for i in id_list)
    system = ("너는 오디오북 성우 대본을 준비하는 편집자다. 본문 전체(지문 포함)를 근거로 "
              "번호가 매겨진 각 대사의 화자를 **최대한 정확히** 판단하라. 화자 파악이 이 작업의 "
              "핵심이므로, 아래 단서를 순서대로 꼼꼼히 활용한다.\n"
              "\n[화자 판단 단서 — 이 순서로 따진다]\n"
              "1. **직접 지문**: 대사 바로 앞/뒤의 '~가 말했다', '~가 물었다', '~가 소리쳤다', "
              "'~의 말이었다' 등. 가장 강한 근거.\n"
              "2. **행동 지문**: 대사 주변에서 어떤 인물이 행동/표정을 보이면 그 인물이 화자일 가능성이 높다 "
              "(예: '민수가 고개를 저었다. \"싫어.\"' → 민수).\n"
              "3. **대화 turn(주고받기)**: 두 사람 대화에서는 화자가 번갈아 나온다. 앞 대사가 A면 "
              "다음은 B, 그다음 A … 흐름을 추적한다. 단, 지문이 이 규칙을 깨면 지문을 따른다.\n"
              "4. **호칭·이인칭**: 대사 안에서 상대를 부르는 이름('민지야, ~')은 화자가 아니라 "
              "*듣는 사람*이다. 이걸 화자와 혼동하지 마라.\n"
              "5. **말투·인칭·내용**: 나이/성별/성격에 맞는 말투, 1인칭 화자('나는~'), 아는 정보의 차이 등.\n"
              "6. **장면 등장인물**: 그 장면에 실제로 있는 인물 중에서만 고른다. 그 자리에 없는 인물 배정 금지.\n"
              "\n[규칙]\n"
              f"- 화자는 되도록 이 명단에서 고른다: {cast_names}\n"
              "- 동일 인물이 연령대별로 분리돼 있으면(예: '영수·아동기') 장면 시점에 맞는 항목을 고른다.\n"
              "- 명단에 없는 인물의 대사라면 억지로 비슷한 이름으로 때우지 말고 본문의 실제 "
              "화자 이름(또는 역할)을 새로 써라. 엉뚱한 사람 배정 금지.\n"
              "- 이름 없는 군중 대사는 '아무아이1','아무아이2'처럼 구분 번호를 붙여도 된다.\n"
              "- **confidence 판정을 정직하게**: 1~2번(직접·행동 지문)으로 확정되면 'high'. "
              "3~5번(turn·말투 추론)만으로 정했거나, 두 인물 중 어느 쪽인지 애매하면 'low'. "
              "low는 사람이 검수하도록 ★로 표시되니, 억지로 high를 주지 말고 애매하면 반드시 low로 둔다.\n"
              "- 모든 번호에 빠짐없이 답하고, 대사 내용은 절대 바꾸지 않는다.\n"
              '- JSON 배열만 출력. 원소: {"id":정수,"speaker":"이름","confidence":"high|low","reason":"근거 짧게(어느 단서인지)"}'
              ) + _profile_hint(profile)
    t = api_call(client, system, f"[본문]\n{ctx}\n\n[대사]\n{numbered}", max_tokens=8000)
    try:
        return extract_json(t, "arr")
    except Exception:
        t = api_call(client, system + "\n반드시 유효한 JSON 배열만. 문자열 안 큰따옴표는 \\\" 로 이스케이프.",
                     f"[본문]\n{ctx}\n\n[대사]\n{numbered}", max_tokens=8000)
        return extract_json(t, "arr")

def assign_speakers(client, ctx, dials, cast_names, profile=None):
    out, ids = [], list(range(len(dials)))
    for s in range(0, len(ids), CHUNK):
        part = ids[s:s + CHUNK]
        print(f"  ...대사 {part[0]}~{part[-1]} 화자 판단 중")
        out += assign_chunk(client, ctx, dials, cast_names, part, profile)
    return out

# ------------------------- 외국어 발음 필터 (결정적) -------------------------
_HANGUL  = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
_FOREIGN = re.compile(r"[A-Za-z\u4e00-\u9fff\u3040-\u30ff\u0370-\u03ff]")

def filter_pronunciations(analysis, ctx):
    """AI가 과하게 뽑은 발음 항목을 결정적 코드로 걸러낸다."""
    kept, dropped, seen = [], [], set()
    for p in analysis.get("pronunciations", []) or []:
        term = str(p.get("term", "")).strip()
        reading = str(p.get("reading", "")).strip()
        if not term or not reading:
            dropped.append((term, "읽기 표기 없음")); continue
        if _HANGUL.search(term):
            dropped.append((term, "한글 표기 → 대상 아님")); continue
        if not _FOREIGN.search(term):
            dropped.append((term, "외국 문자 아님")); continue
        if term not in ctx:
            dropped.append((term, "원고에 없는 표기")); continue
        i = ctx.find(term)
        tail = ctx[i + len(term): i + len(term) + 2]
        if tail[:1] in ("(", "（") and _HANGUL.search(ctx[i + len(term): i + len(term) + 12] or ""):
            dropped.append((term, "원고에 읽는 법 병기됨")); continue
        if term in seen:
            dropped.append((term, "중복")); continue
        seen.add(term)
        kept.append({"term": term, "kind": p.get("kind", "기타"),
                     "reading": reading, "note": p.get("note", "")})
    analysis["pronunciations"] = kept
    return kept, dropped

# ============================================================================
#  렌더 — 좌표 계산은 원본(src), 그리기는 대상(tgt). 둘의 좌표계는 동일하다.
# ============================================================================
PALETTE = [(1,.92,.23),(.55,.8,1),(.6,.9,.6),(1,.65,.75),(.8,.65,1),(1,.75,.45),
(.85,.72,.55),(.55,.9,.88),(1,.7,.62),(.62,.72,.9),(.88,.85,.6),(.8,.88,.7),
(.95,.8,.9),(.7,.85,.95),(.9,.9,.75),(.75,.78,.82),(.95,.7,.4),(.6,.95,.8),
(.9,.6,.6),(.7,.7,.95)]

def short_name(info):
    s = info.get("short")
    if not s:
        s = re.split(r"[(（]", info.get("name", "?"), maxsplit=1)[0].strip()
    return s or info.get("name", "?")

def label_parts_multi(cast_map, sp, star):
    """동시 발화(A+B)면 두 캐릭터 이름을 '+'로 이어 붙이고, (성별,나이)는 생략한다.
    단독 화자는 기존과 같이 (성별, 나이) + 이름."""
    names = split_speakers(sp)
    if len(names) < 2:
        return label_parts(cast_map.get(names[0], {"name": names[0], "short": names[0]}), star)
    joined = "+".join(short_name(cast_map.get(n, {"name": n, "short": n})) for n in names)
    return (("\u2605" if star else "") + joined, "(동시)")

def label_parts(info, star):
    """라벨: 이름 앞에 (성별, 나이)를 작게 괄호로. 예: (남성, 20대) 홍길동.
    이름은 잘 보이게(검정+대사색 하이라이트), (성별, 나이)는 작게 회색."""
    name = short_name(info)
    if star:
        name = "\u2605" + name
    g = info.get("gender", "")
    gfull = {"남": "남성", "여": "여성"}.get(g, "")
    a = info.get("age_inline", "")
    if gfull and a:
        sub = f"({gfull}, {a})"
    elif gfull:
        sub = f"({gfull})"
    elif a:
        sub = f"({a})"
    else:
        sub = ""
    return name, sub

class Renderer:
    """배치 실패를 세면서 렌더한다. dry=True면 자리만 계산하고 그리지 않는다."""
    def __init__(self, src, tgt, font, dry=False):
        self.src, self.tgt, self.font, self.dry = src, tgt, font, dry
        self.occ = {}
        self.fail = []          # (종류, 페이지, 내용)
        self.placed = 0
        self.rendered = set()   # 본문에 실제로 하이라이트 위치를 잡은 대사 id (최종검증용, v2.4)
        self.furn = page_furniture(src)   # 쪽번호·러닝헤더 (페이지 걸침 조각 정리용)

    def _occ(self, pno):
        if pno not in self.occ:
            self.occ[pno] = occupancy(self.src[pno - 1])
        return self.occ[pno]

    def _prect(self, pno):
        return self.tgt[pno - 1].rect

    def place(self, pno, text, size, rgb, near_y, max_w, kind, xs=None,
              border=True, leader_to=None, max_dy=None, shrink=True, fill=(1,1,1)):
        occ = self._occ(pno)
        # 넓은 박스부터 시도하고, 자리가 없으면 폭을 줄여(세로로 길게) 다시 찾는다.
        r = lines = None
        scales = (1.0, 0.78, 0.60, 0.46, 0.36) if shrink else (1.0,)
        for scale in scales:
            w, h, ln = box_size(self.font, text, max(52, max_w * scale), size)
            cand = find_slot(occ, self._prect(pno), w, h, near_y, xs=xs, max_dy=max_dy)
            if cand is not None:
                r, lines = cand, ln
                break
        if r is None:
            self.fail.append((kind, pno, text.split("\n")[0][:34]))
            return None
        occ.append(r)                       # 다음 메모가 이 위에 겹치지 않게
        if not self.dry:
            page = self.tgt[pno - 1]
            draw_note(page, r, lines, size, rgb, border=border, fill=fill)
            if leader_to is not None:
                self._leader(page, r, leader_to, occ, rgb)
        self.placed += 1
        return r

    def _leader(self, page, box, target_rect, occ, rgb):
        """메모와 본문 위치를 잇는 얇은 선. 글자를 가로지르면 그리지 않는다."""
        ty = min(max(target_rect.y0 + target_rect.height / 2,
                     box.y0 + 3), box.y1 - 3)
        if box.x1 <= target_rect.x0:
            p0, p1 = fitz.Point(box.x1, ty), fitz.Point(target_rect.x0 - 1, ty)
        elif box.x0 >= target_rect.x1:
            p0, p1 = fitz.Point(box.x0, ty), fitz.Point(target_rect.x1 + 1, ty)
        else:
            return
        span = fitz.Rect(min(p0.x, p1.x), ty - 1.2, max(p0.x, p1.x), ty + 1.2)
        if not _clear(span, [o for o in occ if o is not box], 0.5):
            return
        page.draw_line(p0, p1, color=rgb, width=0.5, dashes="[1 2] 0")

SUB_FS = 7.0                           # 성별·나이 작은 글자
SUB_RGB = (0.50, 0.45, 0.45)           # 성별·나이 색(회색조 — 이름보다 눈에 덜 띄게)
LABEL_GAP = 5                          # 라벨과 대사 사이 간격

def _label_dims(font, name, sub, fs):
    """한 줄 라벨: [성별·나이 작게] [이름 진하게]. 이름이 오른쪽(대사에 붙는 쪽)."""
    sfs = fs * (SUB_FS / LABEL_FS)
    nw = font.text_length(name, fs)
    gap = fs * 0.30
    sw = (font.text_length(sub, sfs) + gap) if sub else 0
    w = sw + nw + 3
    h = fs * 1.28 + 3
    return w, h, sfs, sw, nw

def draw_label(page, box, name, sub, hl_color, fs, sub_w, name_w):
    """이름은 검은 글씨 + 대사와 같은 색 하이라이트 배경. 성별·나이는 왼쪽에 작게 회색."""
    sfs = fs * (SUB_FS / LABEL_FS)
    fn = ("kf" if KFONT else "korea")
    baseline = box.y0 + 2 + fs * 0.82
    if sub:
        page.insert_text(fitz.Point(box.x0 + 1, baseline), sub, fontsize=sfs,
                         fontname=fn, fontfile=KFONT, color=SUB_RGB)
    nx0 = box.x0 + sub_w + 1
    hl = fitz.Rect(nx0 - 1.5, box.y0 + 1.5, nx0 + name_w + 1.5, box.y0 + 1.5 + fs * 1.18)
    page.draw_rect(hl, fill=hl_color, color=None)                 # 대사와 같은 색 배경
    page.insert_text(fitz.Point(nx0, baseline), name, fontsize=fs,
                     fontname=fn, fontfile=KFONT, color=(0, 0, 0))  # 검은 글씨

def place_label(rd, pno, name, sub, rect, hl_color, snippet=""):
    """화자 라벨은 대사 첫 줄 **왼쪽 여백**에, 이름이 대사에 붙게 놓는다.
    이름은 검은 글씨 + 대사와 같은 색 하이라이트. 한 줄이라 연속 대사에서도 안 겹친다.
    절대 페이지 좌/우 끝으로 밀지 않는다."""
    prect = rd._prect(pno)
    occ = rd._occ(pno)
    near = rect.y0 + rect.height / 2

    for fs in (LABEL_FS, LABEL_FS - 1, LABEL_FS - 2, LABEL_FS - 3):
        w, h, sfs, sub_w, nw = _label_dims(rd.font, name, sub, fs)
        x = rect.x0 - LABEL_GAP - w
        if x < 1:
            continue
        r = find_slot(occ, prect, w, h, near, xs=[x], max_dy=rect.height * 0.6 + 3)
        if r is not None:
            occ.append(r)
            if not rd.dry:
                draw_label(rd.tgt[pno - 1], r, name, sub, hl_color, fs, sub_w, nw)
            rd.placed += 1
            return r

    for fs in (LABEL_FS, LABEL_FS - 1, LABEL_FS - 2):
        w, h, sfs, sub_w, nw = _label_dims(rd.font, name, "", fs)
        x = rect.x0 - LABEL_GAP - w
        if x < 1:
            continue
        r = find_slot(occ, prect, w, h, near, xs=[x], max_dy=rect.height * 0.9 + 4)
        if r is not None:
            occ.append(r)
            if not rd.dry:
                draw_label(rd.tgt[pno - 1], r, name, "", hl_color, fs, 0, nw)
            rd.placed += 1
            return r

    w, h, sfs, sub_w, nw = _label_dims(rd.font, name, sub, LABEL_FS - 1)
    r = find_slot(occ, prect, w, h, rect.y0 - h * 0.55,
                  xs=[max(1, rect.x0)], max_dy=h + 3)
    if r is not None:
        occ.append(r)
        if not rd.dry:
            draw_label(rd.tgt[pno - 1], r, name, sub, hl_color, LABEL_FS - 1, sub_w, nw)
        rd.placed += 1
        return r

    # 마지막 폴백 — 대사 위쪽에 **이름만** 작게. (성별, 나이)까지 넣을 폭이 없을 때만
    # 여기까지 온다. 라벨을 통째로 잃는 것보다 이름만이라도 남기는 편이 낫다
    # (성별·나이는 표지 인물표·검수목록 CSV·하이라이트 툴팁에 그대로 있다).
    for fs in (LABEL_FS - 1, LABEL_FS - 2, LABEL_FS - 3):
        w, h, sfs, sub_w, nw = _label_dims(rd.font, name, "", fs)
        r = find_slot(occ, prect, w, h, rect.y0 - h * 0.55,
                      xs=[max(1, rect.x0)], max_dy=h + 6)
        if r is not None:
            occ.append(r)
            if not rd.dry:
                draw_label(rd.tgt[pno - 1], r, name, "", hl_color, fs, 0, nw)
            rd.placed += 1
            return r

    # 최후 폴백 — 여백띠(또는 페이지의 남은 빈 자리)에 놓는다.
    #   앞의 폴백들은 전부 '대사 왼쪽' 또는 '대사 바로 위'만 뒤지므로, 삽화가 지면을 꽉 채운
    #   그림책에서는 여백띠를 붙여도 라벨이 통째로 버려졌다(「하늘고래의 노래」 419개 중 34개).
    #   ★ 여백띠 라벨은 대사에서 멀어지므로 **대사 앞부분을 함께 적는다**. 펼침면 왼쪽 쪽의
    #     대사는 오른쪽 여백띠까지 거리가 700pt를 넘어, 이름만 있으면 어느 줄인지 못 찾는다
    #     (2026-08-02 대표 피드백). 세로 위치(y)는 대사 줄에 맞추고, 색은 화자색 그대로.
    snip = re.sub(r"\s+", " ", str(snippet or "")).strip()
    snip = snip[:16] + ("\u2026" if len(snip) > 16 else "")
    txt = f"{sub} {name}".strip() + (f'\n\u201c{snip}' if snip else "")
    for fs in (LABEL_FS, LABEL_FS - 1, LABEL_FS - 2):
        w, h, lines = box_size(rd.font, txt, 150, fs)
        r = find_slot(occ, prect, w, h, near)
        if r is None:
            continue
        occ.append(r)
        if not rd.dry:
            draw_note(rd.tgt[pno - 1], r, lines, fs, (0, 0, 0), fill=hl_color)
        rd.placed += 1
        return r

    rd.fail.append(("화자라벨", pno, name))
    return None

# 대사가 아니라고 판정된 항목의 화자 자리에 이 값을 넣으면 표시에서 완전히 빠진다.
#   (예: 좀비가 되어 소리를 못 내는 인물의 “….” 처럼 지면에는 있으나 읽을 것이 없는 자리)
#   감지 자체는 그대로 두므로 id·캐시가 어긋나지 않고, 검수 CSV에는 '제외'로 남는다.
NOT_DIALOGUE = "(대사아님)"

# 화자가 없는 항목(지문 속 의성어·효과음 등)을 알아보는 표식.
#   따옴표 안에 있어도 사람이 '말한' 것이 아니면 성우가 읽을 대사가 아니다
#   (예: 소소리의 회상 중 “펑!” — 폭발음). 이런 자리에 하이라이트와 화자 라벨을 달면
#   성우가 대사로 착각한다. 감지 id는 그대로 두고 표시에서만 뺀다(캐시·검수 CSV는 유지).
_NOT_DIAL_PAT = re.compile(r"대사아님|효과음|의성어|화자\s*없음|지문")

def is_not_dialogue(sp):
    return bool(_NOT_DIAL_PAT.search(str(sp or "")))

def split_speakers(sp):
    """동시 발화 화자 표기 'A+B' 를 이름 목록으로. 단독이면 원소 1개.
    지문이 '둘이 동시에 말했다/입을 모아 따졌다'라고 못박은 자리는 한 사람으로 적으면
    안 된다 — 두 성우가 겹쳐 녹음해야 하므로 대본에 둘 다 보여야 한다(대표 확정)."""
    return [x.strip() for x in str(sp or "").split("+") if x.strip()] or [str(sp or "")]

def render_speakers(rd, dials, assigns, cast_map, colors, qo, qc, label_on=True):
    """대사 전체 span 형광 하이라이트 + 화자 라벨(이름 + 작은 성별·나이)."""
    amap = {a["id"]: a for a in assigns}
    used_by_page = {}   # pno -> 이미 하이라이트에 쓰인 단어 집합(같은 문구 중복 대비)
    for idx, d in enumerate(dials):
        a = amap.get(idx, {"speaker": "미상", "confidence": "low"})
        sp, conf = a["speaker"], a.get("confidence", "low")
        if is_not_dialogue(sp):
            continue
        # cont 항목(= 앞 줄에서 이어지는 같은 발화)에는 라벨을 달지 않는다. 색은 그대로
        # 칠하므로 발화 전체가 이어져 보이고, 라벨만 첫 줄에 한 번 붙는다.
        label_this = label_on and not d.get("cont")
        first = split_speakers(sp)[0]          # 동시 발화면 대표 캐릭터(색·인물정보 기준)
        col = colors.get(first, (.9, .9, .9))
        info = cast_map.get(first, {"name": first, "short": first})
        first_rect = None
        for pi, part in enumerate(d["parts"]):
            spage = rd.src[part["page"] - 1]
            frag = strip_furniture("".join(part["frags"]), rd.furn)
            used = used_by_page.setdefault(part["page"], set())
            rects = highlight_span(spage, frag, qo, qc, used=used)   # ★ span 전체
            if not rects:
                q = find_quads(spage, part["frags"], pi > 0, qo, qc)
                rects = [x.rect for x in q] if q else []
            if not rects:
                continue
            if first_rect is None:
                first_rect = min(rects, key=lambda r: (round(r.y0), r.x0))
                first_page = part["page"]
                rd.rendered.add(idx)
            if not rd.dry:
                tp = rd.tgt[part["page"] - 1]
                h = tp.add_highlight_annot(rects); h.set_colors(stroke=col)
                full = (f"{info.get('name', sp)} / {info.get('gender','?')} / "
                        f"{info.get('age_full', info.get('age_inline','?'))}")
                h.set_info(content=("[확인필요] " if conf == "low" else "") + full)
                h.update()
        if first_rect is None:
            rd.fail.append(("대사위치", d["start_page"], d["text"][:34]))
            continue
        if not label_this:
            continue
        name, sub = label_parts_multi(cast_map, sp, conf == "low")
        place_label(rd, first_page, name, sub, first_rect, col,
                    snippet=d.get("text", ""))

def scene_card(sc):
    """본문에 얹는 카드는 엔지니어가 '문단 덩어리 분위기'를 한눈에 잡게 하는 게 목적.
    씬·장소·상황·분위기만. 구체적 BGM·앰비언스·효과음은 브리핑 표와 CSV에만 둔다."""
    L = [f"{MUSIC} 씬: {sc.get('scene','')}"]
    if sc.get("place"):     L.append(f"장소: {sc['place']}")
    if sc.get("situation"): L.append(f"상황: {sc['situation']}")
    if sc.get("mood"):      L.append(f"분위기: {sc['mood']}")
    return "\n".join(L)

def _anchor_rect(page, anchor):
    a = str(anchor or "").strip()
    if len(a) < 4: return None
    for probe in (a, a[:20], a[:12]):
        hit = page.search_for(probe)
        if hit: return hit[0]
    return None

def render_sound(rd, scenes):
    """씬 카드 · 효과음 · 페이드아웃. 전부 빈 자리에만."""
    for sc in scenes:
        try:
            pno = int(sc.get("page", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= pno <= len(rd.src)):
            rd.fail.append(("씬-페이지범위", sc.get("page"), sc.get("scene", ""))); continue
        ar = _anchor_rect(rd.src[pno - 1], sc.get("start_anchor"))
        near = ar.y0 if ar else 30
        rd.place(pno, scene_card(sc), SND_FS, SOUND_RGB, near_y=near,
                 max_w=min(260, rd._prect(pno).width * 0.45), kind="씬카드",
                 leader_to=ar)

        for s in sc.get("sfx", []) or []:
            try:
                spno = int(s.get("page", pno))
            except (TypeError, ValueError):
                spno = pno
            if not (1 <= spno <= len(rd.src)): continue
            sr = _anchor_rect(rd.src[spno - 1], s.get("anchor"))
            cue = f"{MUSIC} 효과음: {s.get('cue','')}"
            if not sr: cue += "  (위치 추정)"
            rd.place(spno, cue, SND_FS, SOUND_RGB,
                     near_y=(sr.y0 if sr else rd._prect(spno).height * 0.5),
                     max_w=min(200, rd._prect(spno).width * 0.38),
                     kind="효과음", leader_to=sr)

        if sc.get("fade_out"):
            try:
                epno = int(sc.get("end_page", pno))
            except (TypeError, ValueError):
                epno = pno
            if not (1 <= epno <= len(rd.src)): continue
            er = _anchor_rect(rd.src[epno - 1], sc.get("end_anchor"))
            t = f"{MUSIC}{DASH} FADE OUT: {sc.get('scene','')}"
            if sc.get("fade_note"): t += f"\n{sc['fade_note']}"
            if not er: t += "\n(위치 추정 — 씬 끝)"
            rd.place(epno, t, SND_FS, FADE_RGB,
                     near_y=(er.y1 if er else rd._prect(epno).height - 60),
                     max_w=min(230, rd._prect(epno).width * 0.42),
                     kind="페이드아웃", leader_to=er)
class Briefing:
    def __init__(self, size_rect):
        self.doc = fitz.open()
        self.w, self.h = size_rect.width, size_rect.height
        self.fontfile = KFONT
        if self.fontfile:
            self.fn = "kfont"
            self.font = fitz.Font(fontfile=self.fontfile)
        else:                      # 폴백: 내장 CJK(글자 간격이 벌어짐 — 최후 수단)
            self.fn = "korea"
            self.font = None
        self.page = None; self.y = 0
        self._new_page()
    def _new_page(self):
        self.page = self.doc.new_page(width=self.w, height=self.h)
        self.y = MARGIN
    def _ensure(self, need):
        if self.y + need > self.h - MARGIN: self._new_page()
    def text(self, s, size=9.5, indent=0, gap=4, swatch=None):
        width = self.w - 2 * MARGIN - indent
        est_lines = 1
        for para in s.split("\n"):
            est_lines += max(1, int(len(para) * size * 1.02 / width) + 1) - 1 + 1
        need = est_lines * (size * 1.45) + gap
        self._ensure(min(need, self.h - 2 * MARGIN))
        rect = fitz.Rect(MARGIN + indent, self.y, self.w - MARGIN, self.h - MARGIN)
        leftover = self.page.insert_textbox(rect, s, fontname=self.fn,
                                            fontfile=self.fontfile, fontsize=size,
                                            lineheight=1.45, align=fitz.TEXT_ALIGN_LEFT)
        if leftover < 0:  # 안 들어감 → 새 페이지에서 재시도
            self._new_page()
            rect = fitz.Rect(MARGIN + indent, self.y, self.w - MARGIN, self.h - MARGIN)
            leftover = self.page.insert_textbox(rect, s, fontname=self.fn,
                                                fontfile=self.fontfile, fontsize=size,
                                                lineheight=1.45, align=fitz.TEXT_ALIGN_LEFT)
        used = (rect.height - max(leftover, 0))
        if swatch:
            self.page.draw_rect(fitz.Rect(MARGIN + indent - 14, self.y + 1.5,
                                          MARGIN + indent - 3, self.y + 11.5),
                                fill=swatch, color=None)
        self.y += used + gap
    def h1(self, s): self.text(s, size=16, gap=10)
    def h2(self, s): self.y += 6; self.text("■ " + s, size=12, gap=6)

    # --- 표 (등장인물용). 폰트 실측으로 줄수를 계산해 행 높이를 정확히 잡는다 ---
    def _wrap(self, s, width, size):
        lines = []
        for para in str(s).split("\n"):
            cur = ""
            for ch in para:
                trial = cur + ch
                w = (self.font.text_length(trial, size) if self.font
                     else len(trial) * size * 0.62)
                if w > width and cur:
                    lines.append(cur); cur = ch
                else:
                    cur = trial
            lines.append(cur)
        return lines or [""]

    def _row_h(self, cells, col_w, fs, pad, lh, swatch_col):
        n = 1
        for ci, txt in enumerate(cells):
            if ci == swatch_col: continue
            n = max(n, len(self._wrap(txt, col_w[ci] - 2 * pad, fs)))
        return n * fs * lh + 2 * pad

    def _draw_row(self, cells, xs, col_w, fs, h, pad, lh, swatch_col, fill=None):
        y, grid = self.y, (0.72, 0.72, 0.72)
        if fill:
            self.page.draw_rect(fitz.Rect(xs[0], y, xs[-1], y + h), fill=fill, color=None)
        for ci, txt in enumerate(cells):
            if ci == swatch_col and isinstance(txt, (tuple, list)):
                self.page.draw_rect(fitz.Rect(xs[ci] + 2, y + 2, xs[ci + 1] - 2, y + h - 2),
                                    fill=tuple(txt), color=None)
                continue
            ty = y + pad + fs * 0.88
            for ln in self._wrap(txt, col_w[ci] - 2 * pad, fs):
                self.page.insert_text(fitz.Point(xs[ci] + pad, ty), ln, fontname=self.fn,
                                      fontfile=self.fontfile, fontsize=fs)
                ty += fs * lh
        self.page.draw_line(fitz.Point(xs[0], y), fitz.Point(xs[-1], y), color=grid, width=0.5)
        self.page.draw_line(fitz.Point(xs[0], y + h), fitz.Point(xs[-1], y + h), color=grid, width=0.5)
        for x in xs:
            self.page.draw_line(fitz.Point(x, y), fitz.Point(x, y + h), color=grid, width=0.5)
        self.y = y + h

    def table(self, headers, rows, col_w, swatch_col=0, size=8, hsize=8.5,
              pad=3, lh=1.25, header_fill=(0.90, 0.90, 0.90)):
        xs = [MARGIN]
        for w in col_w: xs.append(xs[-1] + w)
        hh = self._row_h(headers, col_w, hsize, pad, lh, -1)
        if self.y + hh > self.h - MARGIN: self._new_page()
        self._draw_row(headers, xs, col_w, hsize, hh, pad, lh, -1, fill=header_fill)
        for r in rows:
            rh = self._row_h(r, col_w, size, pad, lh, swatch_col)
            if self.y + rh > self.h - MARGIN:      # 페이지 넘김 → 헤더 반복
                self._new_page()
                self._draw_row(headers, xs, col_w, hsize, hh, pad, lh, -1, fill=header_fill)
            self._draw_row(r, xs, col_w, size, rh, pad, lh, swatch_col)
        self.y += 8
def build_sound_section(b, scenes):
    """음향 디자인 표 — 본문 보라색 메모와 같은 내용. 엔지니어가 이 표만 봐도 선곡 가능."""
    b.h2("음향 디자인 (씬별) — 본문에는 보라색 카드, 종료 지점은 파란 FADE OUT 으로 표시")
    if not scenes:
        b.text("음향 제안 없음", indent=6); return
    W = b.w - 2 * MARGIN
    col_w = [W * .08, W * .20, W * .15, W * .57]
    rows = []
    for sc in scenes:
        end = sc.get("end_page", sc.get("page"))
        rng = (f"p{sc.get('page')}" if str(end) == str(sc.get("page"))
               else f"p{sc.get('page')}~{end}")
        head = sc.get("scene", "")
        if sc.get("place"): head += f"\n({sc['place']})"
        cell = ""
        if sc.get("situation"): cell += f"상황: {sc['situation']}\n"
        cell += f"BGM: {sc.get('bgm','')}"
        if sc.get("ambience"): cell += f"\nAMB: {sc['ambience']}"
        if sc.get("note"):     cell += f"\n특이: {sc['note']}"
        for s in sc.get("sfx", []) or []:
            cell += f"\n\u266a SFX p{s.get('page','')} {s.get('cue','')} — \"{s.get('anchor','')}\""
        if sc.get("fade_out"):
            cell += (f"\n\u2500 FADE OUT p{end}: {sc.get('fade_note','') or '페이드아웃'}"
                     f"  (\"{sc.get('end_anchor','')}\")")
        rows.append([rng, head, sc.get("mood", ""), cell])
    b.table(["페이지", "씬 · 장소", "분위기", "상황 · BGM · 앰비언스 · 효과음 · 종료"],
            rows, col_w, swatch_col=-1)

def build_pron_section(b, prons):
    """외국어 읽기 표 — 원고에 읽는 법이 없는 로마자·한자만. 없으면 섹션을 만들지 않는다."""
    if not prons:
        return
    b.h2("외국어 읽기 (원고에 읽는 법이 없는 표기만)")
    W = b.w - 2 * MARGIN
    col_w = [W * .26, W * .10, W * .24, W * .40]      # 표기·종류·읽기·비고
    rows = [[p.get("term", ""), p.get("kind", ""), p.get("reading", ""), p.get("note", "")]
            for p in prons]
    b.table(["원고 표기", "종류", "이렇게 읽는다", "비고"], rows, col_w, swatch_col=-1)

def build_briefing(size_rect, stem, analysis, dial_stats, colors, low_count, brief_pages_note,
                   scenes=None):
    b = Briefing(size_rect)
    b.h1(f"오디오북 제작 브리핑 — {stem}")
    b.text(f"AI 분석 초안 · ★(확인필요)는 담당자 검수 후 확정 | 원본 원고 무수정 | "
           f"브리핑 {brief_pages_note}쪽이 앞에 붙어 뷰어 쪽번호가 원본보다 {brief_pages_note} 밀리니 "
           f"지면 인쇄 쪽번호로 소통", size=8)

    if str(analysis.get("summary", "")).strip():
        b.h2("작품 한눈에 (녹음 전 30초 브리핑)")
        b.text(analysis["summary"].strip(), size=9.5, indent=6)
    cast_sorted = sorted(analysis.get("cast", []),
                         key=lambda c: dial_stats.get(c.get("name", ""), 0), reverse=True)
    W = b.w - 2 * MARGIN

    # 1) 한눈에 표 — 전원이 한 쪽에 들어오도록 노트 없이 압축.
    #    폭에 비례 배분해 긴 이름·나이도 한 줄에 담아 행 높이를 낮춘다.
    b.h2("등장인물 한눈에 (대사 많은 순 · 색 = 본문 하이라이트)")
    inner = W - 16
    col_w = [16, inner * .32, inner * .09, inner * .45, inner * .14]   # 색·이름·성별·나이·대사
    rows = [[colors.get(c.get("name", "?"), (1, 1, 1)), c.get("name", "?"),
             c.get("gender", "?"), c.get("age_inline", c.get("age_full", "?")),
             str(dial_stats.get(c.get("name", ""), 0))] for c in cast_sorted]
    b.table(["", "이름", "성별", "나이", "대사"], rows, col_w,
            size=7.5, hsize=8, pad=2.5, lh=1.2)

    # 2) 상세 디렉팅 노트
    b.h2("보이스 · 캐스팅 노트 (주요 인물 순)")
    col_w2 = [16, inner * .18, W - 16 - inner * .18]
    rows2 = []
    for c in cast_sorted:
        nm = c.get("name", "?")
        note = c.get("voice_note", "")
        if c.get("casting_note"): note += f"\n· 캐스팅: {c['casting_note']}"
        if c.get("same_person"): note += f"\n· 동일인물군: {c['same_person']}"
        rows2.append([colors.get(nm, (1, 1, 1)), nm, note])
    b.table(["", "이름", "보이스 · 캐스팅 디렉팅"], rows2, col_w2)

    # 음향 씬 표는 앞 브리핑 지면에 넣지 않는다(2026-07-31 대표 확정 — 불필요).
    # 음향 정보는 본문의 보라색 씬 카드와 `_음향목록.csv` 로만 전달한다.
    # (--sound-only 엔지니어용 브리핑에는 그대로 들어간다 — 그건 그 산출물의 본체다)
    build_pron_section(b, analysis.get("pronunciations", []))

    pov = analysis.get("pov", {})
    b.h2("시점(POV)")
    b.text(f"{pov.get('type','?')} — {pov.get('notes','')}")

    notes = analysis.get("special_notes", [])
    b.h2("작품 특이사항")
    if notes:
        for n in notes: b.text(f"· [{n.get('topic','')}] {n.get('detail','')}", indent=6)
    else:
        b.text("특이사항 없음", indent=6)

    qs = analysis.get("publisher_questions", [])
    b.h2("출판사 확인 필요 사항 (우리 제안 포함)")
    if qs:
        for i, q in enumerate(qs, 1):
            b.text(f"{i}. {q.get('question','')}\n   상황: {q.get('context','')}\n"
                   f"   제안: {q.get('our_suggestion','')}", indent=6, gap=7)
    else:
        b.text("확인 필요 사항 없음", indent=6)

    b.h2("검수 안내")
    b.text(f"★(확인필요) 대사 {low_count}건 — 지문 근거 없이 문맥으로 추정한 대사입니다. "
           f"검수목록 CSV에서 ★ 행만 원문과 대조하면 됩니다.")
    return b.doc
def write_briefing_md(path, stem, analysis, dial_stats, low_count, scenes=None):
    L = [f"# 오디오북 제작 브리핑 — {stem}", ""]
    if str(analysis.get("summary", "")).strip():
        L += ["## 작품 한눈에 (녹음 전 30초 브리핑)", "", analysis["summary"].strip(), ""]
    pov = analysis.get("pov", {})
    L += [f"**시점**: {pov.get('type','?')} — {pov.get('notes','')}", "", "## 등장인물 · 캐스팅", "",
          "| 이름 | 성별 | 나이 | 대사수 | 보이스 노트 | 캐스팅 노트 |", "|---|---|---|---|---|---|"]
    for c in sorted(analysis.get("cast", []),
                    key=lambda c: dial_stats.get(c.get("name", ""), 0), reverse=True):
        L.append(f"| {c.get('name','')} | {c.get('gender','')} | "
                 f"{c.get('age_full', c.get('age_inline',''))} | {dial_stats.get(c.get('name',''),0)} | "
                 f"{c.get('voice_note','')} | {c.get('casting_note','')} |")
    # 음향 씬 표는 제작 브리핑에 넣지 않는다(PDF 브리핑과 동일 규칙). `_음향목록.csv` 참조.

    prons = analysis.get("pronunciations", [])
    if prons:
        L += ["", "## 외국어 읽기 (원고에 읽는 법이 없는 표기만)", "",
              "| 원고 표기 | 종류 | 이렇게 읽는다 | 비고 |", "|---|---|---|---|"]
        for p in prons:
            L.append(f"| {p.get('term','')} | {p.get('kind','')} | "
                     f"{p.get('reading','')} | {p.get('note','')} |")

    L += ["", "## 작품 특이사항", ""]
    for n in analysis.get("special_notes", []):
        L.append(f"- **{n.get('topic','')}**: {n.get('detail','')}")
    L += ["", "## 출판사 확인 필요 사항", ""]
    for i, q in enumerate(analysis.get("publisher_questions", []), 1):
        L += [f"{i}. **{q.get('question','')}**",
              f"   - 상황: {q.get('context','')}", f"   - 우리 제안: {q.get('our_suggestion','')}"]
    L += ["", f"> ★ 확인필요 대사 {low_count}건 — 검수목록 CSV 참조"]
    open(path, "w", encoding="utf-8").write("\n".join(L))

# ------------------------- 음향 전용 산출물 -------------------------
def sound_md_lines(scenes):
    L = ["", "## 음향 디자인 (본문 PDF: 보라색 씬카드 / 파란 FADE OUT)", "",
         "| 페이지 | 씬 | 장소 | 분위기 | 상황 | BGM | 앰비언스 | 효과음 | 종료(페이드) |",
         "|---|---|---|---|---|---|---|---|---|"]
    for sc in (scenes or []):
        end = sc.get("end_page", sc.get("page"))
        rng = (f"p{sc.get('page')}" if str(end) == str(sc.get("page"))
               else f"p{sc.get('page')}~{end}")
        sfx = "<br>".join(f"p{s.get('page','')} {s.get('cue','')} (\"{s.get('anchor','')}\")"
                          for s in (sc.get("sfx") or [])) or "—"
        fade = (f"p{end} {sc.get('fade_note','') or '페이드아웃'} "
                f"(\"{sc.get('end_anchor','')}\")") if sc.get("fade_out") else "이어짐"
        L.append(f"| {rng} | {sc.get('scene','')} | {sc.get('place','')} | "
                 f"{sc.get('mood','')} | {sc.get('situation','')} | {sc.get('bgm','')} | "
                 f"{sc.get('ambience','') or '—'} | {sfx} | {fade} |")
    return L

def write_sound_md(path, stem, scenes, notes=None):
    L = [f"# 오디오북 음향 디자인 — {stem}", "",
         "> 엔지니어용. 본문 PDF에는 씬 시작 지점에 보라색 카드, "
         "음악이 빠져야 할 지점에 파란색 FADE OUT 으로 표시돼 있습니다.", ""]
    L += sound_md_lines(scenes)
    if notes:
        L += ["", "## 작업 메모", ""] + [f"- {n}" for n in notes]
    open(path, "w", encoding="utf-8").write("\n".join(L))

def write_sound_csv(path, scenes):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["씬번호","시작페이지","끝페이지","씬","장소","분위기","상황",
                    "BGM","앰비언스","특이점","효과음","페이드아웃","페이드메모",
                    "시작문구","끝문구"])
        for i, sc in enumerate(scenes or [], 1):
            sfx = " / ".join(f"p{s.get('page','')} {s.get('cue','')}"
                             for s in (sc.get("sfx") or []))
            w.writerow([i, sc.get("page",""), sc.get("end_page",""), sc.get("scene",""),
                        sc.get("place",""), sc.get("mood",""), sc.get("situation",""),
                        sc.get("bgm",""), sc.get("ambience",""), sc.get("note",""),
                        sfx, "O" if sc.get("fade_out") else "", sc.get("fade_note",""),
                        sc.get("start_anchor",""), sc.get("end_anchor","")])

def build_sound_briefing(size_rect, stem, scenes):
    b = Briefing(size_rect)
    b.h1(f"오디오북 음향 디자인 — {stem}")
    b.text("AI 초안 · 엔지니어 검토 후 확정 | 원본 원고 무수정 | "
           "본문: 보라색 씬카드 / 파란색 FADE OUT = 음악·효과음이 빠지는 지점", size=8)
    build_sound_section(b, scenes or [])
    return b.doc

# ------------------------- 인테이크(작업설정) -------------------------
PROFILE_TEMPLATE = {
    "book_type": "",        # 그림책 / 어린이문학 / 청소년 / 일반문학 / 논픽션 / 에세이 …
    "dialogue_style": "",   # 큰따옴표 / 대시 / 따옴표 없음 / 혼재 / 대본형식
    "speaker_shown": "",    # 원고에 화자명이 이미 인쇄돼 있는가 (예/아니오/일부)
    "label_mode": "quote",  # quote | dash | mixed | narrative | highlight-only
    "extra_quote_pairs": [],    # 보조 따옴표 목록(예: ["‘’"]). 주 따옴표 외의 기호로도 대사를 적는 책만
    "extra_quote_min_len": 7,   # 보조 따옴표 안 글자 수가 이 미만이면 강조로 보고 제외
    "special": "",          # 이 원고만의 특이점 (자유 서술)
    "publisher_note": "",   # 출판사 요청사항
    "sound_direction": "",  # 음향 방향 (예: 음악 최소, 앰비언스 위주)
    "gutter": "auto",       # auto | off | 숫자(pt)
    "actors": "auto",       # 성우 수(메인 포함). "auto" 면 코드가 필요한 인원과 배역별 성별을 정한다
    "reading_order": "page",# page | columns (한 지면에 단이 둘 이상이면 columns)
    "font": "",             # 한글 폰트 파일 경로. 비우면 자동 탐색(SP_KFONT → 동봉 폰트 → 시스템)
    "model": ""             # 판단 모델 ID. 비우면 .env 의 SP_MODEL → 코드 기본값(MODEL)
}

def load_profile(stem):
    p = f"{stem}_작업설정.json"
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            print(f"[설정] {p} 적용 "
                  f"(라벨모드={d.get('label_mode','quote')}, gutter={d.get('gutter','auto')}"
                  f", 읽기순서={d.get('reading_order','page')})")
            return d
        except Exception as e:
            print(f"[설정] {p} 읽기 실패({e}) → 기본값 사용")
    return {}

def write_profile_template(stem):
    p = f"{stem}_작업설정.json"
    if os.path.exists(p):
        print(f"[설정] 이미 있습니다: {p}")
        return p
    json.dump(PROFILE_TEMPLATE, open(p, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"[설정] 템플릿 생성 → {p}  (담당자 확인 답변을 채운 뒤 실행하세요)")
    return p

# ------------------------- 방법 1: 구독제 판단 요청서 -------------------------
def export_tasks(stem, src_md5, ctx, dials, version="full", label_mode="quote",
                 profile=None):
    path = f"{stem}_판단요청.md"
    hint = _profile_hint(profile)
    L = [f"# AI 판단 요청서 — {os.path.basename(stem)}", "",
         f"> `--export-tasks`로 자동 생성. **API 호출 없이** Claude Code가 아래 과제를",
         f"> 수행하고 결과를 `{stem}_AI판단.json` 으로 저장한다.",
         f"> 버전: **{'음향 전용' if version=='sound' else '전체 작업'}**", ""]
    if hint:
        L += ["## 이 원고의 확인된 특이점 (반드시 반영)", "", "```", hint.strip(), "```", ""]

    if version == "sound":
        L += ["## 저장할 파일", "", "```json", "{",
              f'  "src_md5": "{src_md5}",',
              '  "scenes": [ 아래 과제의 scenes 배열 ]', "}", "```", "",
              "- `src_md5`는 위 값을 **그대로 복사**.",
              "- 저장 후 `--check --sound-only` 로 검증, `--render --sound-only` 로 렌더.", "",
              "---", "", "## 과제 — 음향 디자인 (scenes)", "", "```", SOUND_SYSTEM, "```", "",
              "---", "", "## 원고 본문 (페이지 표시 포함)", "", "```", ctx, "```", ""]
        open(path, "w", encoding="utf-8").write("\n".join(L))
        return path

    numbered = "\n".join(f"{i}: {d['text'].strip()}" for i, d in enumerate(dials))
    has_narrative = label_mode in ("narrative", "mixed")
    n0 = len(dials)
    L += ["## 저장할 파일", "", "```json", "{",
          f'  "src_md5": "{src_md5}",',
          '  "analysis": { 과제1의 JSON 객체 },',
          '  "assigns":  [ 과제2의 JSON 배열 ],',
          '  "scenes":   [ 과제3의 scenes 배열 ]'
          + (',' if has_narrative else ''),
          *(['  "narrative": [ 과제4의 JSON 배열 ]'] if has_narrative else []),
          "}", "```", "",
          "- `src_md5`는 위 값을 **그대로 복사**할 것.",
          f"- `assigns`는 id 0 ~ {max(n0-1,0)} 까지 **{n0}개 전부** 있어야 한다"
          + (" — **따옴표 없는 대사(narrative)는 assigns 에 넣지 않는다.**" if has_narrative else "") + ".",
          *([f"- ★ `narrative` 항목은 **항목 안의 `speaker`·`confidence`·`reason`** 이 곧 그 대사의 "
             f"화자 배정이다. 코드가 anchor 를 원고에서 확인한 항목만 채택해 id {n0}부터 순서대로 "
             f"붙이므로, 번호를 직접 매기거나 assigns 에 넣지 않는다(예전 방식은 anchor 를 못 찾은 "
             f"항목이 버려지면 뒤 배정이 한 칸씩 밀렸다)."]
            if has_narrative else []),
          "- 저장 후 `--check` → `--render` 순으로 진행한다.", "",
          "---", "", "## 과제 1 — 작품 분석 (analysis)", "", "```", ANALYZE_SYSTEM, "```", "",
          "---", "", "## 과제 2 — 화자 배정 (assigns)", "",
          "본문 전체(맨 아래)를 근거로 각 번호 대사의 화자를 **최대한 정확히** 판단한다.",
          "화자 파악이 이 작업의 핵심이므로 아래 단서를 순서대로 꼼꼼히 따진다:",
          "1. 직접 지문('~가 말했다/물었다')이 가장 강한 근거.",
          "2. 행동 지문(대사 주변에서 행동·표정을 보인 인물이 화자일 가능성 높음).",
          "3. 대화 turn: 두 사람 대화는 화자가 번갈아 나온다(지문이 깨면 지문 우선).",
          "4. 호칭: 대사 안에서 부르는 이름은 화자가 아니라 *듣는 사람*이다(혼동 금지).",
          "5. 말투·인칭·내용, 그리고 그 장면에 실제 있는 인물 중에서만 고른다.",
          "6. **뒤따르는 지문의 이름 순서 = 말한 순서.** 'A와 B가 ~했다' 지문 바로 앞에 "
          "대사가 2줄이면 첫 줄이 A, 둘째 줄이 B다. 이걸로 풀리는 자리는 low 로 두지 말 것.",
          "",
          "★ 반드시 지킬 세 가지 (대표 확정):",
          "- **지문 속 의성어·효과음은 대사가 아니다.** 따옴표 안이어도 사람이 말한 게 아니면"
          "(예: 폭발음 “펑!”, “우르르 쾅쾅!”) speaker 를 \"(대사아님)\" 으로 둔다. "
          "성우가 대사로 착각하지 않도록 표시에서 뺀다.",
          "- **동시 발화는 두 이름을 함께.** 지문이 '둘이 동시에 말했다/입을 모아 따졌다/"
          "미리 입을 맞추기라도 한 듯' 이라고 하면 speaker 를 \"A+B\" 로 적는다"
          "(두 성우가 겹쳐 녹음한다). 그런 지문 앞에 대사가 1줄이면 동시, 2줄이면 순서대로다.",
          "- **정체가 나중에 밝혀지는 인물은 처음부터 최종 이름으로.** 뒤에서 '소소리'로 "
          "밝혀지는 백상아리는 첫 등장부터 speaker 를 \"소소리\" 로 적는다. 성우는 한 사람이 "
          "연기하므로 대본에 두 이름이 있으면 혼란만 생긴다.",
          "- 화자는 되도록 과제1의 cast 명단에서 고른다.",
          "- 동일 인물이 연령대별로 분리돼 있으면 장면 시점에 맞는 항목을 고른다.",
          "- 명단에 없는 인물이면 억지로 때우지 말고 본문의 실제 화자 이름을 쓴다.",
          "- 이름 없는 군중은 '아무아이1','아무아이2'처럼 번호를 붙여도 된다.",
          "- confidence를 정직하게: 1~2번으로 확정되면 'high', 3~5번 추론이거나 두 인물 중 "
          "애매하면 'low'(★로 표시되어 사람이 검수). 억지로 high 주지 말 것.",
          "- 모든 번호에 빠짐없이 답하고, 대사 내용은 절대 바꾸지 않는다.",
          '- 원소: {"id":정수,"speaker":"이름","confidence":"high|low","reason":"근거 짧게(어느 단서인지)"}', "",
          f"### 대사 목록 (총 {len(dials)}개)", "", "```", numbered, "```", "",
          "---", "", "## 과제 3 — 음향 디자인 (scenes)", "", "```", SOUND_SYSTEM, "```", ""]
    if has_narrative:
        L += ["---", "", "## 과제 4 — 따옴표 없는 대사 찾기 (narrative)", "",
              f"이 원고는 `label_mode={label_mode}` 다. 위 대사 목록은 따옴표·대시로 표기된 "
              "대사만 결정적으로 감지한 것이라, **따옴표 없이 쓰인 대사**는 빠져 있다. "
              "본문에서 그것들을 찾아 아래 형식으로 낸다.", "",
              "```", NARRATIVE_SYSTEM, "```", "",
              "※ 각 항목의 `speaker` 를 반드시 채울 것(비면 '미상'이 된다). `assigns` 에는 넣지 않는다.", ""]
    L += ["---", "", "## 원고 본문 (페이지 표시 포함)", "", "```", ctx, "```", ""]
    open(path, "w", encoding="utf-8").write("\n".join(L))
    return path

def check_cache(stem, src_md5, n_dials, version="full", doc=None, skip=(), label_mode="quote"):
    cache = f"{stem}_AI판단.json"
    if not os.path.exists(cache):
        print(f"[검증] ✗ {cache} 가 없습니다."); return False
    try:
        cd = json.load(open(cache, encoding="utf-8"))
    except Exception as e:
        print(f"[검증] ✗ JSON 형식 오류: {e}"); return False
    ok = True
    if cd.get("src_md5") != src_md5:
        print(f"[검증] ✗ src_md5 불일치 — 요청서의 {src_md5} 로 고쳐주세요."); ok = False
    scenes = cd.get("scenes") or []
    if version == "sound":
        if not scenes:
            print("[검증] ✗ scenes 가 비어 있습니다."); ok = False
        print(f"[검증] 씬 {len(scenes)}개 / 페이드아웃 {sum(1 for s in scenes if s.get('fade_out'))}건 / "
              f"효과음 {sum(len(s.get('sfx') or []) for s in scenes)}건")
    else:
        a = cd.get("analysis") or {}
        if not a.get("cast"):
            print("[검증] ✗ analysis.cast 가 비어 있습니다."); ok = False
        ids = {x.get("id") for x in cd.get("assigns", [])}
        missing = [i for i in range(n_dials) if i not in ids]
        if missing:
            print(f"[검증] ✗ assigns 누락 {len(missing)}건 → {missing[:20]}"
                  f"{'…' if len(missing) > 20 else ''}"); ok = False
        # 따옴표 없는 대사(narrative) — 채택/폐기와 speaker 누락 (v2.4)
        nar = cd.get("narrative") or []
        if label_mode in ("narrative", "mixed") or nar:
            if doc is not None:
                dropped = []
                adopted = attach_narrative_dialogues(doc, skip, nar, dropped=dropped)
                print(f"[검증] 따옴표 없는 대사 {len(nar)}건 중 채택 {len(adopted)} / 폐기 {len(dropped)}")
                for idx, pno, anc, why in dropped[:10]:
                    print(f"        · #{idx} p{pno} “{anc}” — {why}")
                if len(dropped) > 10:
                    print(f"        · … 외 {len(dropped) - 10}건 (폐기 항목은 anchor 를 원문 그대로 고치면 채택된다)")
            nospk = [i for i, it in enumerate(nar) if not str(it.get("speaker", "") or "").strip()]
            if nospk:
                print(f"[검증] ✗ narrative 항목에 speaker 가 없음 {len(nospk)}건 → {nospk[:20]}"); ok = False
            extra = sorted(i for i in ids if isinstance(i, int) and i >= n_dials)
            if extra and cd.get("assign_ids") != "adopted":
                print(f"[검증] · assigns 의 id ≥ {n_dials} 항목 {len(extra)}건은 렌더에서 무시됩니다"
                      f"(따옴표 없는 대사는 narrative 항목의 speaker 로 배정)")
        if not scenes:
            print("[검증] · scenes 가 비어 있습니다(음향 메모 없이 렌더됩니다).")
        print(f"[검증] 인물 {len(a.get('cast', []))}명 / 배정 {len(ids)}건 / "
              f"씬 {len(scenes)}개 / 외국어 {len(a.get('pronunciations') or [])}건")
    print("[검증] " + ("✓ 렌더 가능 — --render 로 진행하세요."
                      if ok else "✗ 위 항목을 고친 뒤 다시 --check"))
    return ok

# ============================================================================
#  메인
# ============================================================================
def run(input_pdf, mode="api", version="full"):
    if not os.path.exists(input_pdf):
        sys.exit(f"[중단] 파일 없음: {input_pdf}")
    global WORKDIR, READING_ORDER, MODEL
    input_pdf = os.path.abspath(input_pdf)
    WORKDIR = os.path.dirname(input_pdf)                        # 모든 입출력의 기준 폴더
    name = os.path.splitext(os.path.basename(input_pdf))[0]     # 표시용 이름
    stem = os.path.join(WORKDIR, name)                           # 산출물 경로 접두(원고 옆)
    src_md5 = hashlib.md5(open(input_pdf, "rb").read()).hexdigest()
    print(f"[작업폴더] {WORKDIR}  (작업설정·판단 캐시·산출물·.env 는 이 폴더 기준)")

    if mode == "init":
        write_profile_template(stem); return

    profile = load_profile(stem)
    label_mode = (profile.get("label_mode") or "quote").strip()
    gutter_opt = str(profile.get("gutter", "auto")).strip()
    READING_ORDER = (profile.get("reading_order") or "page").strip()
    MODEL = (str(profile.get("model", "") or "").strip() or dotenv_get("SP_MODEL")
             or os.environ.get("SP_MODEL", "").strip() or MODEL)
    if mode in ("api", "render", "master"):
        resolve_kfont(str(profile.get("font", "") or ""))
        if mode == "api":
            _require_kfont()        # 과금 전에 먼저 막는다 — 판단 뒤 렌더에서 멈추면 API 비용만 나간다

    src = fitz.open(input_pdf)
    fonts = diagnose_fonts(src)
    print(f"[진단] 정상 {len(fonts['ok'])}p / 깨짐 {fonts['broken']} / 빈 {len(fonts['empty'])}p")
    if not fonts["ok"]:
        sys.exit("[중단] 텍스트 추출 가능한 페이지가 없습니다(스캔본 추정). OCR이 먼저 필요합니다.")
    skip = set(fonts["broken"])
    ctx = build_context(src, skip)

    dials = []
    if version == "full" and label_mode != "narrative":
        (qo, qc), qcount = pick_quote_style(src, skip)
        print(f"[감지] 따옴표 스타일: {qo}{qc} ({qcount}회)")
        dials = detect_dialogues(src, skip, qo, qc)
        print(f"[감지] 따옴표 대사 {len(dials)}개 "
              f"(페이지 걸침 {sum(1 for d in dials if len(d['parts']) > 1)}건)")
        # 대시(-) 대사도 결정적으로 추가 — 따옴표가 없어 지나치는 대화체를 잡는다
        if label_mode in ("dash", "mixed"):
            dash = detect_dash_dialogues(src, skip)
            dials += dash
            print(f"[감지] 대시(-) 대사 {len(dash)}개 추가")
        # 보조 따옴표(작업설정에 명시한 책만) — 예: “” 와 ‘’ 를 함께 쓰는 원고
        eqp = profile.get("extra_quote_pairs") or []
        pairs = [(s[0], s[-1]) for s in eqp if isinstance(s, str) and len(s) >= 2]
        if pairs:
            emin = int(profile.get("extra_quote_min_len", 7))
            extra = detect_extra_quote_dialogues(src, skip, pairs, emin)
            dials += extra
            print(f"[감지] 보조 따옴표 {''.join(a+b for a,b in pairs)} 대사 "
                  f"{len(extra)}개 추가 (안쪽 {emin}자 미만은 강조로 보고 제외)")
        # ★ 렌더와 **같은 방법**으로 센다. 예전엔 find_quads(=page.search_for)만으로 셌는데,
        #   search_for 는 줄바꿈을 넘는 긴 대사를 자주 놓쳐 실제로는 멀쩡히 칠해지는 대사까지
        #   '매칭 실패'로 보고했다(「하늘고래의 노래」 419개 중 122건이 이렇게 잡혔으나
        #   highlight_span 으로는 419개 전부 위치를 찾았다). 렌더는 highlight_span 을 먼저
        #   쓰고 실패할 때만 find_quads 로 넘어가므로, 진단도 같은 순서로 해야 숫자가 맞는다.
        furn = page_furniture(src)
        miss = []
        for i, d in enumerate(dials):
            for pi, p in enumerate(d["parts"]):
                pg = src[p["page"] - 1]
                frag = strip_furniture("".join(p["frags"]), furn)
                if highlight_span(pg, frag, qo, qc, used=set()):
                    continue
                if d.get("src") == "quote" and find_quads(pg, p["frags"], pi > 0, qo, qc):
                    continue
                miss.append((i, p["page"]))
        print(f"[매칭] 실패 {len(miss)}건" + (f" → {miss[:20]}" if miss else ""))
    else:
        qo, qc = "\u201c", "\u201d"
        if version == "full":
            dials = detect_dash_dialogues(src, skip)
            print(f"[감지] 대시(-) 대사 {len(dials)}개")

    if mode == "detect":
        print(f"[완료] 감지만 수행 (API 호출 0회 · 과금 없음). 대사 {len(dials)}개")
        return
    if mode == "export":
        p = export_tasks(stem, src_md5, ctx, dials, version, label_mode, profile)
        print(f"\n✓ 판단요청서 : {p}  (API 호출 0회)")
        so = " --sound-only" if version == "sound" else ""
        print(f"  다음 → Claude Code에게: \"{p} 를 읽고 지시대로 {stem}_AI판단.json 을 작성해줘\"")
        print(f"  그다음 → python3 sp_pipeline.py \"{input_pdf}\" --check{so}")
        print(f"  마지막 → python3 sp_pipeline.py \"{input_pdf}\" --render{so}")
        return
    if mode == "check":
        check_cache(stem, src_md5, len(dials), version, doc=src, skip=skip, label_mode=label_mode)
        return

    # ---------- 판단 확보 (캐시 → API) ----------
    cache = f"{stem}_AI판단.json"
    analysis = assigns = scenes = narrative = None
    cache_meta = {}
    if os.path.exists(cache):
        try:
            cd = json.load(open(cache, encoding="utf-8"))
            if cd.get("src_md5") == src_md5:
                analysis, assigns = cd.get("analysis"), cd.get("assigns")
                scenes, narrative = cd.get("scenes"), cd.get("narrative")
                cache_meta = cd
                print(f"[캐시] AI 판단 재사용 → {cache} (재판단하려면 이 파일을 지우고 재실행)")
            else:
                print("[캐시] 원고가 바뀌었습니다(MD5 불일치) → 무시하고 재판단")
        except Exception as e:
            print(f"[캐시] 읽기 실패({e}) → 재판단")

    need_api = (scenes is None) if version == "sound" else \
               (analysis is None or assigns is None)
    # 캐스팅·마스터는 판단 캐시만 쓴다 — 캐시가 없다고 API 를 부르지 않는다(v2.4).
    #   예전엔 이 검사가 API 블록 **뒤에** 있어서, 캐시 없이 --cast-suggest 를 돌리면 작품 분석·
    #   화자 배정·음향까지 전부 과금된 뒤에야 캐스팅으로 넘어갔다(.env 가 없으면 키 없음 에러로 죽어
    #   안내문도 못 봤다).
    if mode in ("cast", "master"):
        if version == "sound":
            sys.exit("[중단] --cast-suggest / --master 는 --sound-only 와 함께 쓸 수 없습니다.")
        if not analysis or not assigns:
            sys.exit(f"[중단] {cache} 의 인물·화자 배정이 필요합니다(API 호출 없이 중단). "
                     f"먼저 전체 작업을 실행하세요:\n"
                     f"  python3 sp_pipeline.py \"{input_pdf}\"  (또는 --export-tasks → --check → --render)")
    if need_api and mode == "render":
        sys.exit(f"[중단] --render 는 API를 호출하지 않습니다. {cache} 가 없거나 원고와 맞지 않습니다.\n"
                 f"  구독제로 만들려면: python3 sp_pipeline.py \"{input_pdf}\" --export-tasks"
                 + (" --sound-only" if version == "sound" else "") + "\n"
                 f"  API로 만들려면  : python3 sp_pipeline.py \"{input_pdf}\""
                 + (" --sound-only" if version == "sound" else ""))

    if need_api:
        import anthropic
        client = anthropic.Anthropic(api_key=load_api_key())
        if version == "full":
            print("[AI] 작품 분석 중 (인물·특이사항·발음·출판사 문의)...")
            analysis = analyze_book(client, ctx, profile)
            cast_names = [c["name"] for c in analysis.get("cast", [])]
            print(f"[명단] {len(cast_names)}명: {cast_names}")
            if label_mode in ("narrative", "mixed"):
                print("[AI] 따옴표 없는 대사 탐색 중...")
                narrative = find_narrative(client, ctx, profile)
                add = attach_narrative_dialogues(src, skip, narrative)
                print(f"[감지] 따옴표 없는 대사 {len(add)}개 채택 "
                      f"(AI 제시 {len(narrative or [])}개 중 본문 확인된 것만)")
                dials += add
            print("[AI] 화자 배정 중...")
            assigns = assign_speakers(client, ctx, dials, cast_names, profile)
        print("[AI] 음향 디자인 중 (씬·장소·분위기·BGM·효과음·페이드아웃)...")
        scenes = analyze_sound(client, ctx, profile)
        print(f"[음향] 씬 {len(scenes)}개 / 효과음 "
              f"{sum(len(s.get('sfx') or []) for s in scenes)}건 / "
              f"페이드아웃 {sum(1 for s in scenes if s.get('fade_out'))}건")
        json.dump({"src_md5": src_md5, "analysis": analysis, "assigns": assigns,
                   "scenes": scenes, "narrative": narrative,
                   # 방법 2: 따옴표 없는 대사를 채택한 **뒤에** 배정했으므로 assigns 의 id 가 원본이다.
                   "assign_ids": "adopted"},
                  open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[캐시] AI 판단 저장 → {cache} (다음 재실행부터 API 비용 없이 재렌더)")
    elif version == "full" and label_mode in ("narrative", "mixed") and narrative:
        n0, dropped = len(dials), []
        add = attach_narrative_dialogues(src, skip, narrative, dropped=dropped)
        dials += add
        print(f"[감지] 따옴표 없는 대사 {len(add)}개 (캐시)"
              + (f" — 폐기 {len(dropped)}건 {[(i, w) for i, _, _, w in dropped[:5]]}" if dropped else ""))
        assigns = narrative_assigns(assigns, dials, n0,
                                    positional_ok=(cache_meta.get("assign_ids") == "adopted"))

    scenes = scenes or []

    # ---------- 캐스팅 모드: 판단 캐시(cast+assigns)만 쓴다 (캐시 없음은 위에서 이미 중단) ----------
    if mode in ("cast", "master"):
        assigns = list(assigns)
        got = {a["id"] for a in assigns}
        for i in range(len(dials)):
            if i not in got:
                assigns.append({"id": i, "speaker": "미상", "confidence": "low", "reason": "누락"})
        cast_map = {c["name"]: c for c in analysis.get("cast", [])}
        amap = {a["id"]: a for a in assigns}
        dial_stats = Counter(x for i in range(len(dials))
                             if not is_not_dialogue(amap[i]["speaker"])
                             for x in split_speakers(amap[i]["speaker"]))
        adjacency = build_adjacency(dials, assigns, src)
        _act = profile.get("actors", "auto")
        n_actors = None if str(_act).strip().lower() in ("", "auto", "0") else int(_act)

        if mode == "cast":
            casting = load_casting(stem)
            if casting:
                print(f"[캐스팅] 기존 {stem}_캐스팅.json 이 있습니다. 덮어쓰지 않습니다.\n"
                      f"  새로 제안받으려면 그 파일을 지우고 다시 --cast-suggest 하세요.")
            else:
                casting = suggest_casting(cast_map, dial_stats, adjacency, n_actors)
                write_casting(stem, casting, cast_map, dial_stats, adjacency)
            return

        # mode == "master": 성우별 색으로 칠한 1장짜리 마스터 대본
        casting = load_casting(stem)
        if not casting:
            sys.exit(f"[중단] {stem}_캐스팅.json 이 없습니다. 먼저 캐스팅을 만들고 검토하세요:\n"
                     f"  python3 sp_pipeline.py \"{input_pdf}\" --cast-suggest")
        assigned = {c for r in casting.values() for c in r["characters"]}
        unassigned = [c for c in dial_stats
                      if c not in assigned and c != "미상" and not is_not_dialogue(c)]
        if unassigned:
            print(f"[경고] 어느 배역에도 없는 캐릭터 {len(unassigned)}명: {unassigned}\n"
                  f"  → 이 대사는 색이 칠해지지 않습니다(회색). 캐스팅.json 에 추가하세요.")
        render_master(input_pdf, stem, src, src_md5, dials, assigns, cast_map,
                      casting, scenes, qo, qc, gutter_opt, analysis,
                      label_on=(label_mode != "highlight-only"))
        return

    render_pipeline(input_pdf, stem, src, src_md5, dials, analysis, assigns, scenes,
                    qo, qc, version, label_mode, gutter_opt, profile)


# ============================================================================
#  성우 캐스팅 — 캐릭터를 성우 배역(메인/서브1/서브2/서브3)으로 묶는다
#  녹음은 성우 개별 세션으로 진행하므로, 승인된 캐스팅으로 '성우별 단독 PDF'를 뽑는다.
# ----------------------------------------------------------------------------
#  설계 원칙(박보민 대표 확정)
#   · 캐릭터 식별과 캐스팅 배정은 다른 층이다. 배정은 사람이 승인한 뒤에만 색이 된다.
#   · 성우별 단독 PDF에서도 캐릭터 이름 라벨은 유지(색=성우, 라벨=캐릭터 → 이중 안전장치).
#   · ★ 불확실 대사는 특정 성우 색으로 강제하지 않고 '확인' 표시로 남긴다(녹음 누락 방지).
# ============================================================================
ROLE_ORDER  = ["메인", "서브1", "서브2", "서브3", "서브4"]
ROLE_COLORS = {"메인":  (1.00, .92, .23),   # 노랑
               "서브1": (.53, .81, .98),    # 하늘색
               "서브2": (.78, .68, .92),    # 연보라
               "서브3": (1.00, .65, .79),    # 핑크
               "서브4": (.60, .90, .60)}    # 초록

_BRACKETS = [("어린이", ("어린이", "아동", "유아", "초등")),
             ("10대",  ("10대", "청소년", "중학", "고등", "12세", "13세", "14세",
                        "15세", "16세", "17세")),
             ("젊은성인", ("20대", "30대", "청년", "젊은", "대학")),
             ("중년",  ("40대", "50대", "중년", "장년")),
             ("노년",  ("60대", "70대", "80대", "노년", "노인", "할머니", "할아버지"))]

_AGE_NUM = re.compile(r"(\d{1,3})\s*(?:세|살)")

def _bracket_of_age(n):
    return ("어린이" if n < 10 else "10대" if n < 20 else "젊은성인" if n < 40
            else "중년" if n < 60 else "노년")

def age_bracket(info):
    """나이 구간. 구간이 다르면 한 성우가 톤을 갈라 소화할 수 있다고 본다.

    ★ 숫자 나이('3세 남짓')와 뭉뚱그린 '성인'을 못 읽으면 '어린이 vs 어른'조차 구별하지
    못해, 메인 성우에게 몰아줄 수 있는 배역이 전부 서브로 흩어진다(2026-08-02 실측:
    「하늘고래의 노래」에서 성우가 5명까지 늘어났다). 그래서 숫자와 '성인'을 먼저 읽는다.
    필드는 age_inline → age_full → age_basis 순으로 따로 훑는다(근거문에 적힌 남의 나이가
    새어 들어오지 않게)."""
    for key in ("age_inline", "age_full", "age_basis"):
        t = str(info.get(key, ""))
        if not t.strip():
            continue
        m = _AGE_NUM.search(t)
        if m:
            return _bracket_of_age(int(m.group(1)))
        for name, keys in _BRACKETS:
            if any(k in t for k in keys):
                return name
        if "성인" in t or "어른" in t:      # 나이는 모르지만 '어른'인 건 확실한 경우
            return "성인"
    return ""

def distinguishable(a, b, cast_map):
    """한 성우가 대화로 주고받아도 헷갈리지 않는가(성별 다르거나 나이 구간 다르면 O)."""
    ia, ib = cast_map.get(a, {}), cast_map.get(b, {})
    if ia.get("gender") and ib.get("gender") and ia["gender"] != ib["gender"] \
            and "미상" not in (ia["gender"], ib["gender"]):
        return True
    ba, bb = age_bracket(ia), age_bracket(ib)
    if ba and bb and ba != bb:
        return True
    return False

def reading_order(doc, dials):
    """대사를 **원고에 실린 순서**로 정렬한 인덱스 목록.
    ★ id 순서를 읽는 순서로 착각하면 안 된다. dials 는 따옴표 블록 → 대시 블록 →
    보조따옴표 블록 → 따옴표 없는 대사 블록 순으로 이어붙여 만들기 때문에,
    label_mode 가 mixed 인 책에서는 id 순서가 지면 순서와 전혀 다르다.
    (「아무도 오지 않는 곳에서」에서 옥주(따옴표)와 키사(대시)가 한 쪽에서 번갈아
     말하는데도 id 상으로는 200개 넘게 떨어져 있어, 주고받는 대화로 잡히지 않았다.)"""
    pos = []
    for i, d in enumerate(dials):
        pno = d.get("start_page", 1)
        y = x = 1e9
        try:
            frag = "".join(d["parts"][0]["frags"])[:24].strip()
            r = doc[pno - 1].search_for(frag) if frag else None
            if r:
                y, x = r[0].y0, r[0].x0
        except Exception:
            pass
        pos.append((pno, y, x, i))
    pos.sort()
    return [p[3] for p in pos]


def build_adjacency(dials, assigns, doc=None):
    """연속한 다른 화자 = 주고받는 대화. 쌍별 빈도.
    doc 이 있으면 지면 순서로 정렬해 센다(id 순서는 읽는 순서가 아니다)."""
    amap = {a["id"]: a["speaker"] for a in assigns}
    order = reading_order(doc, dials) if doc is not None else range(len(dials))
    seq = [amap.get(i, "미상") for i in order]
    adj = Counter()
    for x, y in zip(seq, seq[1:]):
        if x != y and "미상" not in (x, y) and not (is_not_dialogue(x) or is_not_dialogue(y)):
            adj[frozenset((x, y))] += 1
    return adj

def voice_gender(info):
    """이 캐릭터를 맡을 성우의 성별. 어린이=여, 여자=여, 남자=남.
    본문에 성별 근거가 없으면 `gender_suggest`(우리 제안)를 쓴다 — 이게 비어 있으면
    캐스팅이 '아무 배역에나' 들어가 남성 서브에 아줌마가, 여성 메인에 악당 상어가
    섞인다(2026-08-02 대표 지적). 그래서 판단 단계에서 성별 미상이어도 **연기 기준
    성별 제안은 반드시** 받아 둔다. 그래도 없으면 None."""
    if age_bracket(info) == "어린이":
        return "여"
    for key in ("gender", "gender_suggest"):
        g = str(info.get(key, "")).strip()
        if g.startswith("남"):
            return "남"
        if g.startswith("여"):
            return "여"
    return None

# 잠깐 스치는 씬에서 한두 번 주고받은 정도는 한 성우가 소화한다(대표 확정).
# 이 횟수 미만이면 '계속 주고받는 대화'로 보지 않는다.
ADJ_MIN = 3
# 대사가 이만큼 이하인 단역은 티키타카가 있어도 배역을 나누지 않는다.
# 지문이 매번 누구인지 밝혀 주므로 한 성우가 톤을 갈라 소화하는 게 현장 관행이고,
# 이걸 안 두면 대사 3개짜리 단역 때문에 성우가 한 명 더 붙는다(대표 확정).
MINOR_MAX = 5

def suggest_casting(cast_map, dial_stats, adjacency, n_actors=None):
    """규칙 기반 캐스팅 '제안'(사람이 검토·수정). 대표 확정 규칙:
    ① **한 성우는 한 성별만.** 어린이·여자 역할=여성 성우, 남자 역할=남성 성우.
       남성 서브에 여자·어린이·아줌마 배역을 절대 섞지 않는다.
    ② **메인에 최대한 몰아준다.** 나이 구간이 달라 톤이 구별되면 티키타카라도 메인이 소화.
    ③ 나이 구간이 같아도 **잠깐 나오는 씬**(주고받은 횟수 < ADJ_MIN)이면 메인이 소화.
    ④ 위로도 안 되는 경우에만 같은 성별의 서브로 뺀다.
    n_actors 가 None 이면 **필요한 성우 수를 코드가 정한다**(규칙을 지킬 수 있는 최소 인원).
    """
    chars = [c for c in dial_stats if dial_stats[c] > 0]
    if not chars:
        n = max(2, n_actors or 2)
        return {r: {"color": ROLE_COLORS[r], "gender": "", "characters": []}
                for r in ROLE_ORDER[:n]}
    chars.sort(key=lambda c: -dial_stats[c])
    vg = {c: voice_gender(cast_map.get(c, {})) for c in chars}

    def build(n):
        roles = {r: [] for r in ROLE_ORDER[:max(2, n)]}
        role_vg, overflow = {}, []

        def conflict(c, members):
            if dial_stats.get(c, 0) <= MINOR_MAX:
                return False                      # 단역은 나누지 않는다
            for m in members:
                if dial_stats.get(m, 0) <= MINOR_MAX:
                    continue
                if adjacency.get(frozenset((c, m)), 0) >= ADJ_MIN \
                        and not distinguishable(c, m, cast_map):
                    return True
            return False

        def eligible(c, role):
            rvg = role_vg.get(role)
            if rvg is None or vg[c] is None:
                return True
            return rvg == vg[c]

        def place(c, role):
            roles[role].append(c)
            if role_vg.get(role) is None and vg[c] is not None:
                role_vg[role] = vg[c]

        place(chars[0], "메인")
        subs = [r for r in ROLE_ORDER[1:] if r in roles]
        for c in chars[1:]:
            if eligible(c, "메인") and not conflict(c, roles["메인"]):
                place(c, "메인"); continue
            done = False
            for r in subs:
                if eligible(c, r) and not conflict(c, roles[r]):
                    place(c, r); done = True; break
            if not done:
                # 성별이 맞는 배역이 있으면 거기에(규칙 유지), 없으면 '인원 부족'으로 기록
                same = [r for r in subs if role_vg.get(r) == vg[c]]
                if same:
                    place(c, same[-1])
                else:
                    overflow.append(c)
                    place(c, subs[-1] if subs else "메인")
        # 실패 조건 ① 한 배역에 남녀가 섞임 ② 인원 부족으로 성별을 못 지킴
        #           ③ 같은 배역 안에 '계속 주고받는데 구별 안 되는' 쌍이 남음
        mixed = any(len({vg[c] for c in mem if vg[c]}) > 1 for mem in roles.values())
        clash = any(adjacency.get(frozenset((a, b)), 0) >= ADJ_MIN
                    and dial_stats.get(a, 0) > MINOR_MAX
                    and dial_stats.get(b, 0) > MINOR_MAX
                    and not distinguishable(a, b, cast_map)
                    for mem in roles.values()
                    for i, a in enumerate(mem) for b in mem[i + 1:])
        return roles, role_vg, (bool(overflow) or mixed or clash)

    if n_actors:
        roles, role_vg, _ = build(n_actors)
    else:                                   # 규칙을 지킬 수 있는 최소 인원을 찾는다
        for n in range(2, len(ROLE_ORDER) + 1):
            roles, role_vg, bad = build(n)
            if not bad:
                break
    out = {}
    for r, mem in roles.items():
        g = role_vg.get(r) or next((vg[c] for c in mem if vg[c]), "")
        out[r] = {"color": ROLE_COLORS[r], "gender": g or "", "characters": mem}
    return out

def role_label(role, casting):
    """표지·로그에 쓰는 배역 이름. 성별을 함께 보여준다 — '서브1(남)'."""
    g = (casting.get(role) or {}).get("gender", "")
    return f"{role}({g})" if g else role

def write_casting(stem, casting, cast_map, dial_stats, adjacency):
    path = f"{stem}_캐스팅.json"
    json.dump(casting, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[캐스팅 제안] → {path}  (검토·수정 후 --master 로 마스터 대본 생성)")
    print("-" * 60)
    for r in ROLE_ORDER:
        if r not in casting:
            continue
        print(f"■ {role_label(r, casting)}  (대사 "
              f"{sum(dial_stats.get(c,0) for c in casting[r]['characters'])}개)")
        for c in casting[r]["characters"]:
            info = cast_map.get(c, {})
            print(f"   · {c} ({info.get('gender','?')}·{info.get('age_inline','?')})  "
                  f"대사 {dial_stats.get(c,0)}개")
        if not casting[r]["characters"]:
            print("   (없음)")
    top = sorted(adjacency.items(), key=lambda kv: -kv[1])[:6]
    if top:
        print("-" * 60)
        print("주고받는 대화가 잦은 쌍:")
        for pair, n in top:
            a, b = tuple(pair)
            same = any(a in casting[r]["characters"] and b in casting[r]["characters"]
                       for r in casting)
            # 같은 성우라도 성별·나이로 구별되면 문제없음. 구별 안 되는데 같은 성우일 때만 경고.
            # 단역(대사 ≤ MINOR_MAX)은 suggest_casting 이 일부러 나누지 않으므로 경고도 내지 않는다(v2.4).
            minor = dial_stats.get(a, 0) <= MINOR_MAX or dial_stats.get(b, 0) <= MINOR_MAX
            bad = same and n >= ADJ_MIN and not distinguishable(a, b, cast_map) and not minor
            tag = ("  [경고] 같은 성우인데 구별 어려움!" if bad
                   else "  (같은 성우 — 단역이라 한 성우가 톤을 갈라 소화)" if same and minor and not distinguishable(a, b, cast_map)
                   else "  (같은 성우, 성별·나이로 구별됨)" if same else "")
            print(f"   · {a} <-> {b} : {n}회{tag}")
    print("-" * 60)
    return path

def load_casting(stem):
    p = f"{stem}_캐스팅.json"
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print(f"[캐스팅] {p} 읽기 실패: {e}"); return None

def render_master(input_pdf, stem, src, src_md5, dials, assigns, cast_map,
                  casting, scenes, qo, qc, gutter_opt, analysis=None, label_on=True):
    """1장짜리 마스터 대본. 모든 대사를 담당 성우 색으로 칠하고, **음향 씬 카드도 항상**
    함께 넣는다(보라색, 페이지/2~3문단마다). 메인=노랑 / 서브1=하늘 / 서브2=연보라 /
    서브3=핑크 / 서브4=초록. 이름 라벨(검정 + 성우색 배경)도 함께. 원본·메모는 보존."""
    _require_kfont()
    font = _font()
    name = os.path.basename(stem)
    scenes = scenes or []
    amap = {a["id"]: a for a in assigns}
    char_color, char_role = {}, {}
    for role in ROLE_ORDER:
        if role in casting:
            col = tuple(casting[role]["color"])
            for c in casting[role]["characters"]:
                char_color[c] = col
                char_role[c] = role
    GRAY = (.80, .80, .80)

    def do(tgt, dry):
        rd = Renderer(src, tgt, font, dry=dry)
        used_by_page = {}   # pno -> 이미 하이라이트에 쓰인 단어 집합(같은 문구 중복 대비)
        for idx, d in enumerate(dials):
            a = amap.get(idx, {"speaker": "미상", "confidence": "low"})
            sp = a["speaker"]; conf = a.get("confidence", "low")
            if is_not_dialogue(sp):
                continue
            # 이어지는 줄이면 라벨 생략(색은 그대로). 원고에 화자명이 이미 인쇄된 책
            # (label_mode=highlight-only)은 마스터에도 라벨을 달지 않는다 — 이름이 두 번 나온다(v2.4).
            label_this = label_on and not d.get("cont")
            first = split_speakers(sp)[0]      # 동시 발화면 대표 캐릭터 색으로 칠한다
            col = char_color.get(first, GRAY)
            first_rect = None
            for pi, part in enumerate(d["parts"]):
                spage = rd.src[part["page"] - 1]
                frag = strip_furniture("".join(part["frags"]), rd.furn)
                used = used_by_page.setdefault(part["page"], set())
                rects = highlight_span(spage, frag, qo, qc, used=used)
                if not rects:
                    q = find_quads(spage, part["frags"], pi > 0, qo, qc)
                    rects = [x.rect for x in q] if q else []
                if not rects:
                    continue
                if first_rect is None:
                    first_rect = min(rects, key=lambda r: (round(r.y0), r.x0))
                    first_page = part["page"]
                    rd.rendered.add(idx)
                if not rd.dry:
                    tp = rd.tgt[part["page"] - 1]
                    h = tp.add_highlight_annot(rects); h.set_colors(stroke=col)
                    role = " + ".join(char_role.get(x, "미배정")
                                      for x in split_speakers(sp))
                    h.set_info(content=("[확인필요] " if conf == "low" else "")
                               + f"{role} / {sp}")
                    h.update()
            if first_rect is None:
                rd.fail.append(("대사위치", d["start_page"], d["text"][:30])); continue
            if not label_this:
                continue
            name, sub = label_parts_multi(cast_map, sp, conf == "low")
            place_label(rd, first_page, name, sub, first_rect, col,
                        snippet=d.get("text", ""))
        render_sound(rd, scenes)        # 음향 씬 카드 항상 함께
        return rd

    trial = do(src, dry=True)
    need = 0
    if trial.fail and gutter_opt != "off":
        need = GUTTER_W if gutter_opt == "auto" else int(float(gutter_opt))
    tgt = make_gutter_doc(input_pdf, need) if need else fitz.open(input_pdf)
    rd = do(tgt, dry=False)

    body = Counter((round(p.rect.width, 1), round(p.rect.height, 1)) for p in tgt)
    (bw, bh), _ = body.most_common(1)[0]
    b = Briefing(fitz.Rect(0, 0, bw, bh))
    b.h1(f"성우별 마스터 대본 - {name}")
    b.text("각 대사는 담당 성우 색으로 칠해져 있습니다(색=성우, 라벨=캐릭터). "
           "보라색은 음향 씬 카드, 파란색은 FADE OUT 지점입니다. "
           "원본·메모는 그대로 보존됩니다.", size=9)
    # 1) 작품 요약 — 엔지니어·성우가 30초 안에 읽고 바로 부스에 들어갈 수 있게.
    ana = analysis or {}
    summ = str(ana.get("summary", "")).strip()
    if summ:
        b.h2("작품 한눈에")
        b.text(summ, size=9.5, indent=6)
    pov = ana.get("pov", {}) or {}
    if pov.get("type") or pov.get("notes"):
        b.text(f"· 시점: {pov.get('type','')} — {pov.get('notes','')}", size=8.5, indent=6)
    sn = ana.get("special_notes") or []
    if sn:
        b.text("· 녹음 전 알아둘 것: "
               + " / ".join(f"{x.get('topic','')}" for x in sn[:6]), size=8.5, indent=6)

    # 2) 색 범례 — 배역 이름에 성우 성별을 함께 적는다(대표 확정).
    b.h2("색 범례 (성우별) — 색 = 성우, 라벨 = 캐릭터")
    for role in ROLE_ORDER:
        if role not in casting or not casting[role]["characters"]:
            continue
        col = tuple(casting[role]["color"])
        chs = casting[role]["characters"]
        n = sum(1 for i in range(len(dials))
                if any(x in chs for x in split_speakers(amap.get(i, {}).get("speaker", ""))))
        cname = {"메인": "노랑", "서브1": "하늘", "서브2": "연보라",
                 "서브3": "핑크", "서브4": "초록"}.get(role, "")
        b.text(f"{role_label(role, casting)} ({cname}) — {', '.join(chs)}  · 대사 {n}개",
               size=10, indent=18, swatch=col)
    b.text("· 색이 칠해지지 않은 본문 = 내레이션(메인 성우)", size=8.5, indent=18)

    # 3) 등장인물표 — 대사 수·성별·나이·특이사항. 성우가 이 표만 보고 인물을 잡는다.
    dstats = Counter(x for i in range(len(dials))
                     for x in split_speakers(amap.get(i, {}).get("speaker", ""))
                     if not is_not_dialogue(amap.get(i, {}).get("speaker", "")))
    cast_rows = []
    for c in sorted(ana.get("cast", []), key=lambda c: -dstats.get(c.get("name", ""), 0)):
        nm = c.get("name", "?")
        if not dstats.get(nm) and not c.get("voice_note"):
            continue
        note = c.get("voice_note", "")
        if c.get("same_person"):
            note += f" (동일인물군: {c['same_person']})"
        cast_rows.append([char_color.get(nm, (1, 1, 1)),
                          short_name(c), c.get("gender", "?"),
                          c.get("age_inline", "?"), str(dstats.get(nm, 0)),
                          char_role.get(nm, "-"), note])
    if cast_rows:
        b.h2("등장인물 (대사 많은 순)")
        W = b.w - 2 * MARGIN
        inner = W - 16
        col_w = [16, inner * .13, inner * .05, inner * .10, inner * .05,
                 inner * .09, inner * .58]
        b.table(["", "이름", "성별", "나이", "대사", "성우", "보이스 노트 · 특이사항"],
                cast_rows, col_w, size=7.5, hsize=8, pad=2.5, lh=1.2)
    low = [(i, amap[i]) for i in range(len(dials))
           if amap.get(i, {}).get("confidence") == "low"
           and not is_not_dialogue(amap[i].get("speaker"))]
    if low:
        b.h2(f"★ 녹음 전 확인 필요 ({len(low)}건) - 화자 배정이 맞는지 검수")
        for i, a in low[:50]:
            roles = "+".join(char_role.get(x, "미배정") for x in split_speakers(a['speaker']))
            b.text(f"★ p{dials[i]['start_page']} ({roles}/"
                   f"{a['speaker']}) {dials[i]['text'].strip()[:38]}", size=8, indent=4)
    # ★ 음향 디자인 표는 여기 넣지 않는다(대표 확정 사항). 마스터 대본은 성우용이라
    # 구체 BGM·앰비언스·효과음은 필요 없다 — 본문의 씬 카드(보라)·FADE OUT(파랑)
    # 표시만으로 충분하다. 엔지니어용 상세 표는 `_음향목록.csv`·`_제작브리핑.md`에만 둔다.
    n_brief = len(b.doc)
    tgt.insert_pdf(b.doc, start_at=0); b.doc.close()

    out = f"{stem}_마스터대본.pdf"
    tgt.save(out, garbage=4, deflate=True)
    assert hashlib.md5(open(input_pdf, "rb").read()).hexdigest() == src_md5, "원본 변경 감지!"
    print(f"\n✓ 마스터 대본: {out}  (앞 {n_brief}쪽=범례 · 씬 {len(scenes)}개 · "
          f"★확인 {len(low)}건 · 자리못잡음 {len(rd.fail)}건)")
    print(f"✓ 원본·메모 무손상 (MD5 {src_md5[:8]}…)")
    tgt.close()
    final_check(out, input_pdf, n_brief, dials, assigns, casting, rendered=rd.rendered)
    return out


def render_pipeline(input_pdf, stem, src, src_md5, dials, analysis, assigns, scenes,
                    qo, qc, version, label_mode, gutter_opt, profile):
    _require_kfont()
    font = _font()
    name = os.path.basename(stem)
    label_on = (label_mode != "highlight-only") and version == "full"

    def do(tgt, dry):
        rd = Renderer(src, tgt, font, dry=dry)
        if version == "full":
            render_speakers(rd, dials, assigns or [], cast_map, colors, qo, qc, label_on)
        render_sound(rd, scenes)
        return rd

    if version == "full":
        analysis = analysis or {}
        assigns = assigns or []
        got = {a["id"] for a in assigns}
        for i in range(len(dials)):
            if i not in got:
                assigns.append({"id": i, "speaker": "미상", "confidence": "low",
                                "reason": "AI 누락"})
        cast_names = [c["name"] for c in analysis.get("cast", [])]
        prons, dropped = filter_pronunciations(analysis, build_context(src, set()))
        if prons or dropped:
            print(f"[외국어] 읽기 안내 {len(prons)}건 채택" +
                  (f" / 제외 {len(dropped)}건 {[d[0] for d in dropped][:6]}" if dropped else ""))
        low = [a for a in assigns if a.get("confidence") == "low"
               and not is_not_dialogue(a.get("speaker"))]
        speakers = list(dict.fromkeys(x for a in sorted(assigns, key=lambda x: x["id"])
                                      for x in split_speakers(a["speaker"])))
        allnames = cast_names + [s for s in speakers if s not in cast_names]
        colors = {sp: PALETTE[i % len(PALETTE)] for i, sp in enumerate(allnames)}
        cast_map = {c["name"]: c for c in analysis.get("cast", [])}
        amap = {a["id"]: a for a in assigns}
        dial_stats = Counter(x for i in range(len(dials))
                             for x in split_speakers(amap[i]["speaker"]))
    else:
        colors, cast_map, low, dial_stats = {}, {}, [], Counter()

    # ---------- 1차: 자리 계산만 (원본 크기 그대로) ----------
    trial = do(src, dry=True)
    need_gutter = 0
    if trial.fail:
        kinds = Counter(f[0] for f in trial.fail)
        print(f"[배치] 원본 여백만으로는 {len(trial.fail)}건을 놓을 자리가 없습니다 {dict(kinds)}")
        if gutter_opt != "off":
            need_gutter = GUTTER_W if gutter_opt == "auto" else int(float(gutter_opt))
    elif gutter_opt not in ("auto", "off"):
        need_gutter = int(float(gutter_opt))

    # ---------- 2차: 실제 렌더 ----------
    if need_gutter:
        print(f"[배치] 페이지 오른쪽에 여백띠 {need_gutter}pt 추가 "
              f"(원본은 이동·축소 없이 그대로 유지)")
        tgt = make_gutter_doc(input_pdf, need_gutter)
    else:
        tgt = fitz.open(input_pdf)

    rd = do(tgt, dry=False)
    print(f"[렌더] 메모 {rd.placed}개 배치" +
          (f" / 자리 못 잡음 {len(rd.fail)}건" if rd.fail else " / 누락 0건"))
    for k, p, t in rd.fail[:12]:
        print(f"   · [{k}] p{p} {t}")

    # ---------- 브리핑 ----------
    body_size = Counter((round(p.rect.width, 1), round(p.rect.height, 1)) for p in tgt)
    (bw, bh), _ = body_size.most_common(1)[0]
    size_rect = fitz.Rect(0, 0, bw, bh)

    suffix = "음향표시" if version == "sound" else "대사표시"
    out_pdf = f"{stem}_{suffix}.pdf"
    if version == "sound":
        brief = build_sound_briefing(size_rect, name, scenes)
        n_brief = len(brief)
        tgt.insert_pdf(brief, start_at=0); brief.close()
        tgt.save(out_pdf, garbage=4, deflate=True)
        write_sound_md(f"{stem}_음향브리핑.md", name, scenes)
        write_sound_csv(f"{stem}_음향목록.csv", scenes)
        outs = [out_pdf, f"{stem}_음향목록.csv", f"{stem}_음향브리핑.md"]
    else:
        brief = build_briefing(size_rect, name, analysis, dial_stats, colors,
                               len(low), "?", scenes)
        n_brief = len(brief); brief.close()
        brief = build_briefing(size_rect, name, analysis, dial_stats, colors,
                               len(low), str(n_brief), scenes)
        tgt.insert_pdf(brief, start_at=0); brief.close()
        tgt.save(out_pdf, garbage=4, deflate=True)
        with open(f"{stem}_검수목록.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["대사번호","원본페이지","확인필요","화자","성별","나이","근거","대사","감지방식"])
            for i, d in enumerate(dials):
                a = amap[i]; sp = a["speaker"]; info = cast_map.get(sp, {})
                w.writerow([i, d["start_page"],
                            "제외" if is_not_dialogue(sp) else
                            ("★" if a.get("confidence") == "low" else ""),
                            sp, info.get("gender", ""),
                            info.get("age_full", info.get("age_inline", "")),
                            a.get("reason", ""), d["text"].strip(),
                            "따옴표" if d.get("src") == "quote" else "지문"])
        write_briefing_md(f"{stem}_제작브리핑.md", name, analysis, dial_stats, len(low), scenes)
        write_sound_csv(f"{stem}_음향목록.csv", scenes)
        outs = [out_pdf, f"{stem}_검수목록.csv", f"{stem}_제작브리핑.md", f"{stem}_음향목록.csv"]

    assert hashlib.md5(open(input_pdf, "rb").read()).hexdigest() == src_md5, "원본 변경 감지!"
    print(f"\n✓ 결과 PDF : {out_pdf}  (앞 {n_brief}쪽 = 브리핑)")
    for o in outs[1:]:
        print(f"✓ 산출물   : {o}")
    if version == "full":
        print(f"✓ ★ 확인필요 대사 {len(low)}건 — 검수목록 CSV에서 그 행만 대조")
    print(f"✓ 원본 무손상 확인 (MD5 {src_md5[:8]}…)")
    tgt.close()
    final_check(out_pdf, input_pdf, n_brief, dials, assigns,
                rendered=(rd.rendered if version == "full" else None))


# ============================================================================
#  최종 검증 — 렌더가 끝난 뒤 **산출물 자체를 다시 열어** 확인한다.
#  코드가 "놓았다"고 말하는 것과 파일에 실제로 들어 있는 것은 다를 수 있다.
#  (2026-08-02 대표 피드백: 한 쪽이 통째로 비어 보이는 일이 있었다 → 눈으로만 보지 말고
#   기계로 전수 확인할 것)
# ============================================================================
def final_check(out_path, src_path, n_brief, dials=None, assigns=None, casting=None,
                rendered=None):
    print("\n[최종검증] 산출물을 다시 열어 전수 확인합니다…")
    ok = True
    src = fitz.open(src_path)
    out = fitz.open(out_path)
    try:
        if len(out) != len(src) + n_brief:
            print(f"  ✗ 쪽수 불일치: 원본 {len(src)} + 브리핑 {n_brief} ≠ 산출물 {len(out)}")
            ok = False

        # 1) 원본 글자가 한 자도 빠지지 않았는가 (텍스트 레이어 전수 비교)
        lost = []
        for i in range(min(len(src), max(0, len(out) - n_brief))):
            a = _norm(src[i].get_text())
            b = _norm(out[n_brief + i].get_text())
            if a and a not in b:
                miss = sum(1 for ch in a if ch not in b)
                lost.append((i + 1, len(a), miss))
        if lost:
            print(f"  ✗ 원본 글자 누락 의심 {len(lost)}쪽 → {lost[:5]}")
            ok = False
        else:
            print(f"  ✓ 원본 글자 {len(src)}쪽 전부 산출물에 그대로 있음")

        # 2) 원본 그림이 지워지지 않았는가 (저해상도 잉크량 비교)
        thin = []
        for i in range(min(len(src), max(0, len(out) - n_brief))):
            ps = src[i].get_pixmap(dpi=24)
            po = out[n_brief + i].get_pixmap(dpi=24,
                                             clip=fitz.Rect(0, 0, src[i].rect.width,
                                                            src[i].rect.height))
            si = sum(1 for k in range(0, len(ps.samples), ps.n) if ps.samples[k] < 240)
            oi = sum(1 for k in range(0, len(po.samples), po.n) if po.samples[k] < 240)
            if si > 50 and oi < si * 0.9:
                thin.append((i + 1, si, oi))
        if thin:
            print(f"  ✗ 원본 내용이 옅어지거나 사라진 쪽 {len(thin)}건 → {thin[:5]}")
            ok = False
        else:
            print("  ✓ 원본 그림·글자 잉크량 이상 없음(쪽별 대조)")

        # 3) 표시 대상 대사가 **실제로 본문에 칠해졌는가** (v2.4)
        #    예전엔 '배정이 있는가'만 봤는데, 렌더 직전에 누락 배정을 '미상'으로 전부 채우므로
        #    그 검사는 구조적으로 실패할 수 없었다. 이제는 렌더러가 위치를 잡은 대사 집합과
        #    산출물 파일 안의 하이라이트 주석 수를 직접 센다.
        if dials is not None and assigns is not None:
            amap = {a["id"]: a for a in assigns}
            need = [i for i in range(len(dials))
                    if not is_not_dialogue(amap.get(i, {}).get("speaker", "미상"))]
            miss = []
            if rendered is not None:
                miss = [i for i in need if i not in rendered]
                if miss:
                    print(f"  ✗ 하이라이트 위치를 못 잡은 대사 {len(miss)}건 → {miss[:10]}"
                          f"  (검수 CSV·브리핑에는 남아 있음 — 원문 특수문자·줄바꿈 확인)")
                    ok = False
                else:
                    print(f"  ✓ 표시 대상 대사 {len(need)}건 전부 본문에 칠해짐")
            def _n_hl(doc, start):
                return sum(1 for i in range(start, len(doc))
                           for a in (doc[i].annots() or []) if a.type[0] == 8)
            added = _n_hl(out, n_brief) - _n_hl(src, 0)
            expect = len(need) - len(miss)
            if expect and added < expect:
                print(f"  ✗ 산출물 파일의 하이라이트 주석 {added}개 < 칠해야 할 대사 {expect}건")
                ok = False
            else:
                print(f"  ✓ 산출물 파일에 하이라이트 주석 {added}개 확인(대사 {expect}건)")

        # 4) 캐스팅 누락
        if casting:
            assigned = {c for r in casting.values() for c in r["characters"]}
            amap = {a["id"]: a for a in (assigns or [])}
            unc = sorted({x for a in amap.values()
                          for x in split_speakers(a.get("speaker", ""))
                          if x not in assigned and x != "미상" and not is_not_dialogue(x)})
            if unc:
                print(f"  ✗ 배역 미배정 캐릭터 {len(unc)}명 → {unc}")
                ok = False
            else:
                print("  ✓ 모든 캐릭터가 배역에 배정됨")

        print("[최종검증] " + ("✓ 이상 없음" if ok else "✗ 위 항목을 확인하세요"))
    finally:
        src.close(); out.close()
    return ok

USAGE = """사용법  (버전 × 판단방식 조합)  — 모든 파일은 **원고 PDF 가 있는 폴더** 기준으로 읽고 쓴다

  [버전 1 · 전체 작업]  화자표시 + 하이라이트 + 음향 + 출판사 문의 + 인물표
    python3 sp_pipeline.py <원고.pdf>                  API로 판단하고 바로 렌더
    python3 sp_pipeline.py <원고.pdf> --export-tasks   구독제 판단용 요청서 생성
    python3 sp_pipeline.py <원고.pdf> --check          작성된 판단 검증
    python3 sp_pipeline.py <원고.pdf> --render         렌더만 (API 호출 없음)

  [버전 2 · 음향 전용]  엔지니어용 씬 카드 + 페이드아웃만
    python3 sp_pipeline.py <원고.pdf> --sound-only
    python3 sp_pipeline.py <원고.pdf> --sound-only --export-tasks
    python3 sp_pipeline.py <원고.pdf> --sound-only --render

  [성우별 마스터 대본]  성우별 색으로 칠한 1장짜리 전체 대본(메인=노랑/서브1=하늘/서브2=연보라/서브3=핑크/서브4=초록)
    python3 sp_pipeline.py <원고.pdf> --cast-suggest   캐스팅 제안(검토용 캐스팅.json 생성)
    python3 sp_pipeline.py <원고.pdf> --master         승인된 캐스팅으로 마스터 대본 생성
    (전체 작업으로 _AI판단.json 이 먼저 있어야 함 — 없으면 API 를 부르지 않고 중단한다)

  [공통]
    python3 sp_pipeline.py --doctor                    설치 상태 점검(파이썬·라이브러리·한글 폰트·.env)
    python3 sp_pipeline.py <원고.pdf> --init           작업설정 템플릿 생성(먼저 실행)
    python3 sp_pipeline.py <원고.pdf> --detect-only    대사 감지만 (완전 무료)

  작업설정 <원고>_작업설정.json 으로 도서별 특이점을 지정한다:
    label_mode        : quote(따옴표) | dash(대시-) | mixed | narrative | highlight-only
    extra_quote_pairs : 보조 따옴표 목록(예: ["‘’"]). 기본 없음
    reading_order     : page | columns(한 지면에 단이 둘 이상)
    gutter            : auto(자리 없으면 여백띠 자동) | off | 숫자(pt)
    actors            : "auto"(기본 — 코드가 최소 인원과 배역별 성별을 정한다) | 숫자(메인 포함, 예: 메인1+서브2 = 3)
    font              : 한글 폰트 파일 경로(비우면 자동 탐색). .env 의 SP_KFONT 로도 지정 가능
    model             : 판단 모델 ID(비우면 .env 의 SP_MODEL → 코드 기본값)"""

def doctor():
    """다른 컴퓨터에서 처음 돌리기 전 점검. 원고 없이 실행한다(원고 폴더에서 실행하면 .env 도 본다)."""
    import platform
    print(f"[doctor] 파이썬 {platform.python_version()}  ({sys.executable})")
    print(f"[doctor] PyMuPDF {getattr(fitz, 'version', ('?',))[0]}  (import 이름: {fitz.__name__})")
    try:
        import anthropic
        print(f"[doctor] anthropic SDK {anthropic.__version__}  (방법 2·API 에만 필요)")
    except ImportError:
        print("[doctor] anthropic SDK 없음 — 방법 2(API) 를 쓰려면 pip install anthropic (방법 1 은 불필요)")
    kf = resolve_kfont(quiet=True)
    if kf:
        print(f"[doctor] 한글 폰트: {kf}")
        print(f"[doctor] 기호 확인: 음표 {_glyph(chr(0x266a), '■')} / 선 {_glyph(chr(0x2500), '-')}")
    else:
        print("[doctor] ✗ 한글 폰트 없음 — 나눔고딕·Noto Sans CJK·맑은고딕 설치, 또는 assets/fonts/ 에 파일 추가, "
              "또는 .env 에 SP_KFONT=경로")
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("[doctor] ⚠ ANTHROPIC_API_KEY 가 환경변수로 잡혀 있음 — Claude Code 가 종량 과금으로 전환될 수 있다. .env 로 옮길 것")
    envs = _dotenv_paths()
    print(f"[doctor] .env: {envs[0] if envs else '없음 (방법 2 를 쓰려면 원고 폴더에 ANTHROPIC_API_KEY=... 를 넣는다)'}")
    print(f"[doctor] 모델: {dotenv_get('SP_MODEL') or os.environ.get('SP_MODEL', '').strip() or MODEL}")
    print(f"[doctor] 스크립트: {os.path.abspath(__file__)}")
    print("[doctor] " + ("✓ 렌더 가능" if kf else "✗ 폰트를 해결한 뒤 다시 확인"))
    return bool(kf)

if __name__ == "__main__":
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    files = [a for a in args if not a.startswith("--")]
    known = {"--export-tasks", "--check", "--render", "--detect-only",
             "--sound-only", "--init", "--cast-suggest", "--master", "--doctor"}
    unknown = flags - known
    if unknown:
        print(f"[중단] 모르는 옵션: {sorted(unknown)}\n")
        sys.exit(USAGE)
    if "--doctor" in flags:
        sys.exit(0 if doctor() else 1)
    if not files:
        sys.exit(USAGE)
    mode = ("init"   if "--init" in flags else
            "cast"   if "--cast-suggest" in flags else
            "master" if "--master" in flags else
            "export" if "--export-tasks" in flags else
            "check"  if "--check" in flags else
            "render" if "--render" in flags else
            "detect" if "--detect-only" in flags else "api")
    version = "sound" if "--sound-only" in flags else "full"
    run(files[0], mode=mode, version=version)

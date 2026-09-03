#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sp_pipeline.py 자가 테스트 — 다른 컴퓨터에 설치한 뒤 "여기서도 똑같이 도는지" 확인한다.
API 호출 없음(방법 1 흐름만). 임시 폴더에 한글 원고 픽스처를 만들어 전 단계를 돌린다.

  python3 scripts/selftest.py        # 전부 통과하면 exit 0, 실패가 있으면 exit 1, 폰트 없으면 exit 2

확인하는 것 (v2.4 손질 항목이 실제로 지켜지는지):
  · 산출물이 원고 폴더에 생기고 현재 디렉터리는 건드리지 않는다
  · 따옴표 없는 대사(narrative)는 항목 안 speaker 로 배정되고, 폐기 항목이 있어도 밀리지 않는다
  · --check 가 채택/폐기를 보고한다
  · [최종검증]이 실제로 칠해진 대사 수와 하이라이트 주석 수를 센다
  · 캐시 없는 --cast-suggest 는 API 를 부르지 않고 중단한다
  · highlight-only 책의 마스터 대본에는 라벨이 없다
  · 음향 전용 렌더가 같은 캐시로 돈다
"""
import sys, os, json, hashlib, subprocess, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SP = os.path.join(HERE, "sp_pipeline.py")
sys.path.insert(0, HERE)
import sp_pipeline as spp
fitz = spp.fitz

FAILS = []
def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FAILS.append(msg)

def sh(*args, cwd, env=None):
    r = subprocess.run([sys.executable, SP, *args], cwd=cwd, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    return r.returncode, r.stdout

# ---------------------------------------------------------------- 픽스처
PAGES = [
 [(48, "하늘 마을 이야기"),
  (60, "민수는 아침 일찍 학교 앞 운동장으로 달려갔다. 바람이"),
  (48, "살랑살랑 불어와 머리카락을 흔들었다."),
  (60, "“지훈아, 오늘 축구 할래?”"),
  (60, "지훈이가 고개를 저었다."),
  (60, "“아니, 오늘은 숙제가 많아.”"),
  (60, "민수는 실망한 얼굴로 나무 아래 앉았다."),
  (60, "할머니가 천천히 다가와 물었다."),
  (60, "“민수야, 왜 그렇게 시무룩하니?”"),
  (60, "“그냥요.”"),
  (60, "민수가 작게 대답했다.")],
 [(60, "다음 날, 교실은 시끌벅적했다. 창밖으로는 벚꽃이"),
  (48, "바람에 흩날리고 있었다."),
  (60, "선생님이 들어오자 아이들이 조용해졌다."),
  (60, "“오늘은 봄 소풍 계획을 세워 볼까요?”"),
  (60, "아이들이 환호했다."),
  (60, "지훈이는 민수에게 속삭였다. 소풍 가면 축구 하자."),
  (60, "민수는 웃으며 고개를 끄덕였다. 그래, 꼭 하자."),
  (60, "─ 그런데 비가 오면 어떡해?"),
  (60, "지훈이가 창밖을 보며 말했다."),
  (60, "“비 오면 실내에서 놀면 되지.”")],
 [(60, "소풍 날, 하늘은 맑았다. 운동장 가득 아이들의 웃음소리가"),
  (48, "울려 퍼졌다."),
  (60, "“펑!”"),
  (60, "폭죽 소리가 운동장에 울렸다."),
  (60, "민수와 지훈이가 동시에 외쳤다."),
  (60, "“우리가 이겼다!”"),
  (60, "할머니는 멀리서 손을 흔들었다."),
  (60, "“잘했다, 우리 강아지들.”")],
]

def make_fixture(path, font):
    doc = fitz.open()
    W, H = 420, 595
    for lines in PAGES:
        p = doc.new_page(width=W, height=H)
        y = 80
        for x, t in lines:
            p.insert_text(fitz.Point(x, y), t, fontsize=11, fontname="tk", fontfile=font)
            y += 22
        p.insert_text(fitz.Point(W / 2, H - 30), str(p.number + 1), fontsize=9, fontname="tk", fontfile=font)
    doc.save(path); doc.close()

def cast_entry(name, g, age, full, note, extra=""):
    return {"name": name, "short": name, "gender": g, "gender_suggest": g, "age_inline": age,
            "age_full": full, "age_basis": "본문", "voice_note": note, "casting_note": extra, "same_person": ""}

CAST = [cast_entry("민수", "남", "10세", "10세(초등 3학년)", "밝고 급한 말투", "아동 — 여성 성우 캐스팅(아동 관례)"),
        cast_entry("지훈", "남", "10세", "10세(초등 3학년)", "차분하고 조심스러움", "아동 — 여성 성우 캐스팅(아동 관례)"),
        cast_entry("할머니", "여", "70대", "70대", "느리고 따뜻한 말투"),
        cast_entry("선생님", "여", "30대", "30대", "또박또박, 밝게")]
def A(i, s, cf="high", r="직접 지문"):
    return {"id": i, "speaker": s, "confidence": cf, "reason": r}
ANALYSIS = {"cast": CAST, "summary": "민수와 지훈이 소풍 날 축구 경기에서 이기는 이야기. 따뜻하고 밝게.",
            "pov": {"type": "3인칭", "notes": "전지적 서술"}, "special_notes": [],
            "publisher_questions": [], "pronunciations": []}
SCENES = [{"page": 1, "end_page": 1, "start_anchor": "민수는 아침 일찍", "end_anchor": "작게 대답했다",
           "scene": "학교 앞 운동장의 아침", "place": "학교 앞 운동장", "situation": "민수가 축구를 하자고 했다가 거절당한다",
           "mood": "아침, 서운함", "note": "", "bgm": "가벼운 피아노, 80bpm", "ambience": "바람, 새소리",
           "fade_out": True, "fade_note": "3초 페이드아웃", "sfx": [{"page": 1, "cue": "바람 소리", "anchor": "바람이"}]},
          {"page": 2, "end_page": 3, "start_anchor": "다음 날, 교실은", "end_anchor": "우리 강아지들",
           "scene": "소풍 계획과 소풍 날", "place": "교실 → 운동장", "situation": "소풍을 계획하고 소풍 날 경기에서 이긴다",
           "mood": "들뜸, 환희", "note": "", "bgm": "밝은 우쿨렐레", "ambience": "교실 웅성거림",
           "fade_out": False, "fade_note": "", "sfx": [{"page": 3, "cue": "폭죽 터지는 소리", "anchor": "폭죽 소리가"}]}]

def write_cache_method1(pdf, out):
    """방법 1 로 Claude 가 썼을 법한 판단 — 따옴표 9 + 대시 1 = id 0~9. narrative 3건 중 1건은 지어낸 문구.
    예전 방식(v2.3)의 위치 기반 배정(id 10~12)도 일부러 넣어 둔다 — 새 코드는 이것을 무시해야 한다."""
    assigns = [A(0, "민수"), A(1, "지훈"), A(2, "할머니"), A(3, "민수", "low", "행동 지문"), A(4, "선생님"),
               A(5, "지훈"), A(6, "(대사아님)", "high", "폭죽 효과음"), A(7, "민수+지훈", "high", "동시 발화 지문"),
               A(8, "할머니"), A(9, "지훈"),
               A(10, "지훈"), A(11, "선생님", "low", "(폐기될 항목의 배정)"), A(12, "민수")]
    narrative = [{"page": 2, "anchor": "소풍 가면 축구 하자", "speaker": "지훈", "confidence": "high", "reason": "속삭였다 지문"},
                 {"page": 2, "anchor": "우리 내일 만나자", "speaker": "선생님", "confidence": "low", "reason": "지어낸 문구(테스트)"},
                 {"page": 2, "anchor": "그래, 꼭 하자", "speaker": "민수", "confidence": "high", "reason": "끄덕였다 지문"}]
    json.dump({"src_md5": hashlib.md5(open(pdf, "rb").read()).hexdigest(), "analysis": ANALYSIS,
               "assigns": assigns, "scenes": SCENES, "narrative": narrative},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def write_cache_quote_only(pdf, out):
    assigns = [A(0, "민수"), A(1, "지훈"), A(2, "할머니"), A(3, "민수"), A(4, "선생님"), A(5, "지훈"),
               A(6, "(대사아님)"), A(7, "민수+지훈"), A(8, "할머니")]
    json.dump({"src_md5": hashlib.md5(open(pdf, "rb").read()).hexdigest(), "analysis": ANALYSIS,
               "assigns": assigns, "scenes": [], "narrative": []},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def csv_rows(path):
    import csv
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.reader(f))

# ---------------------------------------------------------------- 실행
def main():
    print("[selftest] sp_pipeline 자가 테스트 — API 호출 없음")
    kf = spp.resolve_kfont(quiet=True)
    if not kf:
        print("[selftest] ✗ 한글 폰트가 없어 테스트할 수 없습니다. python3 scripts/sp_pipeline.py --doctor 를 보세요.")
        return 2
    print(f"[selftest] 폰트: {kf}")
    env = dict(os.environ, SP_KFONT=kf)
    env.pop("ANTHROPIC_API_KEY", None)          # 어떤 경로로도 API 를 부르면 안 된다

    root = tempfile.mkdtemp(prefix="sp_selftest_")
    try:
        work = os.path.join(root, "원고")
        os.makedirs(work)
        pdf = os.path.join(work, "하늘마을.pdf")
        make_fixture(pdf, kf)
        cwd = root                              # 원고 폴더 **밖**에서 실행한다

        print("\n[1] --init / --detect-only")
        rc, out = sh(pdf, "--init", cwd=cwd, env=env)
        cfg = os.path.join(work, "하늘마을_작업설정.json")
        check(rc == 0 and os.path.exists(cfg), "작업설정 템플릿이 원고 옆에 생성됨")
        d = json.load(open(cfg, encoding="utf-8"))
        check(all(k in d for k in ("extra_quote_pairs", "font", "model", "reading_order")),
              "템플릿에 extra_quote_pairs·font·model·reading_order 키가 있음")
        d.update({"label_mode": "mixed", "book_type": "어린이문학"})
        json.dump(d, open(cfg, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        rc, out = sh(pdf, "--detect-only", cwd=cwd, env=env)
        check(rc == 0 and "대사 10개" in out, "감지: 따옴표 9 + 대시 1 = 10개")

        print("\n[2] --export-tasks")
        rc, out = sh(pdf, "--export-tasks", cwd=cwd, env=env)
        req = os.path.join(work, "하늘마을_판단요청.md")
        txt = open(req, encoding="utf-8").read() if os.path.exists(req) else ""
        check(rc == 0 and "assigns 에 넣지 않는다" in txt, "요청서가 narrative 를 assigns 에 넣지 말라고 지시함")

        print("\n[3] --check (방법 1 판단, 폐기 항목 1건 포함)")
        write_cache_method1(pdf, os.path.join(work, "하늘마을_AI판단.json"))
        rc, out = sh(pdf, "--check", cwd=cwd, env=env)
        check(rc == 0 and "채택 2 / 폐기 1" in out, "--check 가 narrative 채택 2 / 폐기 1 을 보고함")
        check("원고에 없는 문구" in out, "--check 가 폐기 이유(원고에 없는 문구)를 보여줌")
        check("렌더 가능" in out, "--check 통과")

        print("\n[4] --render")
        rc, out = sh(pdf, "--render", cwd=cwd, env=env)
        check(rc == 0 and "[최종검증] ✓ 이상 없음" in out, "렌더 + 최종검증 ✓")
        check("전부 본문에 칠해짐" in out and "하이라이트 주석 11개" in out,
              "최종검증이 실제 칠해진 대사 11건과 하이라이트 주석 11개를 셈")
        check(os.path.exists(os.path.join(work, "하늘마을_대사표시.pdf")), "산출물이 원고 폴더에 생김")
        check(not any(n.startswith("하늘마을") for n in os.listdir(root)), "현재 디렉터리에는 아무것도 안 생김")
        rows = csv_rows(os.path.join(work, "하늘마을_검수목록.csv"))
        by_id = {r[0]: r for r in rows[1:]}
        check(by_id.get("10", [""] * 8)[3] == "지훈" and "소풍 가면" in by_id.get("10", [""] * 8)[7],
              "narrative id 10 = 지훈 (소풍 가면 축구 하자)")
        check(by_id.get("11", [""] * 8)[3] == "민수" and "꼭 하자" in by_id.get("11", [""] * 8)[7],
              "narrative id 11 = 민수 (그래, 꼭 하자) — 폐기 항목 때문에 밀리지 않음")
        check("12" not in by_id, "폐기 항목은 대사로 만들어지지 않음")

        print("\n[5] --cast-suggest → --master (캐시 있음)")
        rc, out = sh(pdf, "--cast-suggest", cwd=cwd, env=env)
        check(rc == 0 and os.path.exists(os.path.join(work, "하늘마을_캐스팅.json")), "캐스팅 제안 생성")
        rc, out = sh(pdf, "--master", cwd=cwd, env=env)
        check(rc == 0 and "[최종검증] ✓ 이상 없음" in out, "마스터 대본 + 최종검증 ✓")

        print("\n[6] --sound-only --render (같은 캐시)")
        rc, out = sh(pdf, "--sound-only", "--render", cwd=cwd, env=env)
        check(rc == 0 and os.path.exists(os.path.join(work, "하늘마을_음향표시.pdf")), "음향 전용 렌더")

        print("\n[7] 캐시 없이 --cast-suggest → API 없이 중단")
        nc = os.path.join(root, "캐시없음"); os.makedirs(nc)
        shutil.copy(pdf, nc)
        rc, out = sh(os.path.join(nc, "하늘마을.pdf"), "--cast-suggest", cwd=cwd, env=env)
        check(rc != 0 and "API 호출 없이 중단" in out, "판단 캐시 없으면 API 를 부르지 않고 안내 후 중단")
        check("API 키가 없습니다" not in out, "키 없음 에러가 아니라 안내문이 나옴")

        print("\n[8] highlight-only 책의 마스터 대본에는 라벨이 없다")
        ho = os.path.join(root, "대본형식"); os.makedirs(ho)
        shutil.copy(pdf, ho)
        hpdf = os.path.join(ho, "하늘마을.pdf")
        json.dump({"label_mode": "highlight-only"}, open(os.path.join(ho, "하늘마을_작업설정.json"), "w", encoding="utf-8"))
        write_cache_quote_only(hpdf, os.path.join(ho, "하늘마을_AI판단.json"))
        sh(hpdf, "--cast-suggest", cwd=cwd, env=env)
        rc, out = sh(hpdf, "--master", cwd=cwd, env=env)
        check(rc == 0, "highlight-only 마스터 렌더")
        src = fitz.open(hpdf); m = fitz.open(os.path.join(ho, "하늘마을_마스터대본.pdf"))
        nb = len(m) - len(src)
        extra = [m[nb + i].get_text().replace(src[i].get_text(), "").strip() for i in range(len(src))]
        check(not any(extra), "본문 쪽에 라벨 글자가 추가되지 않음")
        n_hl = sum(1 for i in range(nb, len(m)) for a in m[i].annots() if a.type[0] == 8)
        check(n_hl == 8, f"하이라이트 8개(따옴표 9 − 대사아님 1) = {n_hl}")
        src.close(); m.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n[selftest] " + ("✓ 전부 통과" if not FAILS else f"✗ 실패 {len(FAILS)}건:\n  - " + "\n  - ".join(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())

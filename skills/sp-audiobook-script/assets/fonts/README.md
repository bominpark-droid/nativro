# 동봉 폰트 자리

이 폴더에 **한글 글리프가 있는 TTF/OTF/TTC** 를 넣으면 `sp_pipeline.py` 가 시스템 폰트보다 먼저 쓴다
(탐색 순서: `SP_KFONT` → 작업설정 `font` → **이 폴더** → OS 기본 경로 → 시스템 폰트 전체 탐색).
다른 컴퓨터로 옮길 때 폰트까지 같이 가므로 산출물 글씨가 어디서나 같아진다.

권장: 나눔고딕 `NanumGothic.ttf` (SIL Open Font License — 재배포 가능. 폰트와 함께 `OFL.txt` 도 넣을 것).
Noto Sans CJK KR 도 OFL 이라 가능하다. 맑은고딕(Windows)·Apple SD Gothic Neo(macOS)는 OS 에 딸린
폰트라 **여기 복사해 배포하지 말고** 시스템 경로에서 자동으로 잡히게 둔다.

폰트가 하나도 없으면 렌더가 중단된다(내장 CJK 폰트는 자간이 벌어져 지침상 금지).
`python3 scripts/sp_pipeline.py --doctor` 로 어느 폰트가 잡히는지 확인한다.

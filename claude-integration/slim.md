---
description: 🔻 슬림 — gccfork 설정대로 자동 (mode/턴/reload)
allowed-tools: []
---

`/slim` 호출 = gccfork TUI 에 슬림 위임 신호.

옵션 일체 없음. 모드/보호 턴/hot-reload 여부 모두 **gccfork 의 설정** 을 따릅니다 (gccfork TUI → ⚙ 설정 → 🔻 슬림 탭 상단 "🪴 /slim 기본값").

처리 흐름:
1. UserPromptSubmit hook (`slim-reload-intercept.sh`) 가로채서 `~/.claude/gccfork-tui-requests/` 에 `action=slim-default` 페이로드 publish
2. gccfork TUI 의 `TuiRequestPollerMixin` 이 1초 polling 으로 받아 prefs 조회 → `slim-and-reload` (reload=true) 또는 `slim-inplace` (reload=false) dispatch
3. gccfork TUI 가 안 떠있으면 — 안내만 출력하고 중단 (절대 규칙 §A-1)

claude 자체는 어떤 텍스트도 출력 안 함 (hook 의 block 응답이 사용자에게 표시됨).

**다른 변형 명령은 더 이상 없음** (`/slim:medium`, `/slim:reload`, `/slim:weak-reload`, `/slim:medium-reload` 전부 제거됨). 변형이 필요하면 gccfork 설정 변경 후 `/slim` 호출.

dry-run 만 별도 유지: `/slim:dry` (실제 변경 X, 통계만).

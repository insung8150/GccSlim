---
description: 🔍 슬림 드라이런 — 통계만, 변경 없음 (gccfork 설정 따름)
allowed-tools: []
---

`/slim:dry` 호출 = gccfork TUI 에 **드라이런 시뮬레이션** 위임 신호.

옵션 일체 없음. 모드/보호 턴 모두 **gccfork 의 설정** 을 따릅니다 (`/slim` 과 동일 설정 — 미리보기 일관성). 실제 jsonl 변경은 **없음**.

처리 흐름:
1. UserPromptSubmit hook 가 `~/.claude/gccfork-tui-requests/<id>.json` 에 `action=slim-dry` 페이로드 publish
2. gccfork TUI 의 `TuiRequestPollerMixin` 이 받아 prefs 조회 → `gccfork slim-inplace --mode <m> --keep-recent-turns <t> --dry-run` 실행
3. TUI 가 결과를 `<id>.json.result` 에 write back
4. hook 이 폴링으로 읽어 KEEP/STUB/DROP/REBIND 통계 + 부피 변화를 block reason 으로 표시

claude 자체에는 어떤 슬림 로직도 없음 (모드/보호 턴 결정, 명령 실행, 출력 포맷 모두 gccfork 책임).

실제 적용은 `/slim` (같은 설정 + reload 여부도 prefs 따름).

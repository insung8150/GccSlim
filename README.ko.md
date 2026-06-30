# GccSlim

현재 배포 버전: `v2026.05.26.1`

Claude Code나 Codex CLI를 쓰면서, 이런 적 없으셨나요?

- 세션 이름이 알 수 없는 ID라서 **"그 대화 어디 갔더라?"** 하고 한참 헤맨 적
- 지금 세션에 **제대로 된 이름**을 붙여두고 싶었던 적
- 지금 세션을 **그대로 복제**해서, 원본은 두고 다른 방향으로 실험해보고 싶었던 적
- Claude Code에서 하던 세션을 **그대로 Codex로 넘겨서** 이어가고 싶었던 적
- 프로젝트마다 흩어진 세션을 **한 화면에서 보고 정리**하고 싶었던 적

**GccSlim은 바로 그걸 해주는 터미널 앱입니다.** 흩어진 Claude Code·Codex 세션을 한곳에서 관리하세요 — 이름 붙이기, 복제, 이동, 병합·분할, 그리고 Claude ↔ Codex 넘기기까지.

> 💡 기본 터미널에는 GccSlim을 세션 관리자로 띄워두고, 고른 세션은 VS Code 터미널에서 바로 열 수 있습니다. 한쪽에선 세션을 고르고, 다른 쪽에선 그 세션으로 작업하세요.

_(덤으로, 너무 커진 세션은 "슬림"으로 가볍게 줄일 수도 있습니다 — 최근 작업은 그대로 두고 오래된 무거운 부분만 잘라내, 새로 시작하지 않아도 다시 가벼워집니다.)_

![GccSlim](assets/screenshot-ko.png)

<sub>모든 기능을 한 화면에:</sub>

![GccSlim 기능 가이드](assets/gccslim-guide-ko-white.png)

## 설치

GccSlim은 Claude Code·Codex 사용자를 위한 도구입니다 — 그러니 에이전트에게 맡기세요. Claude Code나 Codex에 이렇게 붙여넣으세요:

> github.com/insung8150/GccSlim 를 클론해서, 스크립트에 위험한 게 없는지 확인하고, install.sh 를 실행해줘.

에이전트가 클론하고, 내용을 살펴본 뒤 `~/.local/bin`에 설치합니다. 그다음 실행:

```bash
gccslim
```

영어·한국어 UI 둘 다 포함됩니다. 직접 설치하거나 빌드된 tarball을 원하면 [최신 release](https://github.com/insung8150/GccSlim/releases/latest)를 참고하세요.

English readme: [README.md](README.md)

## Claude Code와 어떻게 연동되나

GccSlim은 Claude Code의 공식 확장 지점만 사용하고, 바꾸는 것은 모두 선택적·되돌림 가능합니다:

- `/slim` 명령은 Claude Code의 공식 훅으로 동작합니다 — 설치하면 `~/.claude/settings.json`에 한 줄만 추가되고, 언제든 제거할 수 있습니다.
- 슬림은 **본인의 세션 파일만** 고칩니다. Claude Code 본체는 건드리지 않으며, 원본은 복원 가능한 휴지통으로 갑니다.
- 원하면 슬림 직후 실행 중인 세션을 Claude의 공식 resume 명령으로 새로고침할 수 있습니다. 선택 사항이고 되돌릴 수 있으며, 이 기능 없이도 슬림은 동작합니다.
- 모든 처리는 로컬에서 이뤄집니다. 로그인·사용량 한도·과금을 건드리지 않고, 외부로 아무것도 전송하지 않습니다.

## 배포본 안내

이상한 코드는 전혀 없습니다. Rust 코어를 바이너리로 배포하는 건 Claude Code를 패치하는 부분을 공개 소스에서 빼두기 위해서일 뿐입니다 — Anthropic 약관에 안전하게 맞추려는 것이지, 코드가 해로운 일을 해서가 아닙니다. 그 외 모든 것(Python 계층, 설치 스크립트, 통합 코드)은 이 저장소에 공개돼 있습니다.

## 라이선스

MIT License. 자세한 내용은 `LICENSE`를 확인하세요.

# Agent Plugin 패키징

LocalCrab 을 Agent Plugins 1.0.0 표준 패키지로 배포하기 위한 소스와 빌드 도구다.
런타임(`opencrab`)은 이 패키지에 포함되지 않는다 — discovery·설정만 공급한다.
개념·계약 상세는 저장소 루트 [`docs/agent-plugin-packaging.md`](../../docs/agent-plugin-packaging.md) 참고.

## 디렉터리 구조

```
packaging/agent-plugin/
├── src/                          # 커밋되는 패키지 소스 (빌드 시 그대로 스테이징)
│   ├── plugin.json
│   ├── mcp.json
│   ├── README.md                 # 최종 사용자용 동봉 문서
│   └── skills/localcrab-query/SKILL.md
├── schemas/                      # 벤더링한 canonical 1.0.0 스키마 (오프라인 검증용)
│   ├── plugin.schema.json
│   └── mcp.schema.json
├── tools/                        # 빌더·검증기·env 계약 정본 — wheel 에는 포함되지 않는다
└── README.md                     # 이 문서
```

빌드 산출물은 `dist/`(gitignore 대상)에 생성되고 저장소에 커밋되지 않는다.

## 빌드

```bash
python scripts/build_agent_plugin.py
```

`src/` 를 allowlist 기준으로 스테이징하고, `plugin.json` 의 version 이 pyproject
버전과 일치하는지 확인한 뒤, 검증(스키마+텍스트층+시크릿/개인 경로 스캔)을 통과해야
산출물을 만든다. 검증에 실패하면 `dist/` 를 남기지 않는다.

## 검증·테스트 재현

```bash
pytest tests/test_agent_plugin_packaging.py tests/test_agent_plugin_smoke.py
```

`test_agent_plugin_packaging.py` 는 authoring 게이트·allowlist·시크릿 스캔·재빌드
멱등성·환경 변수 문서-코드 동기화 가드를 다룬다. `test_agent_plugin_smoke.py` 는
레퍼런스 클라이언트로 빌드 산출물을 실제 기동해 프로비저닝부터 `tools/call` 까지
확인한다(clean 성격 — sanitize 된 최소 base env 사용).

## 벤더링 스키마 출처와 무결성

`schemas/plugin.schema.json`, `schemas/mcp.schema.json` 은
[https://agent-plugins.org/schemas/1.0.0/](https://agent-plugins.org/schemas/1.0.0/)
에서 받은 canonical 스키마의 오프라인 사본이다. 값을 이 문서에 박지 않고 아래
명령으로 그때그때 확인한다.

```bash
sha256sum schemas/*.json
```

## 산출물 위치

```
dist/
├── localcrab-plugin/             # plugin root — 클라이언트가 설치하는 전부
│   ├── plugin.json  mcp.json  README.md  LICENSE
│   └── skills/localcrab-query/SKILL.md
└── localcrab-plugin.SHA256SUMS   # 사이드카 — plugin root 밖, 정렬된 상대경로 해시
```

CI 재현은 `.github/workflows/agent-plugin.yml` 참고(`ci.yml` 은 이 워크플로에
무관하게 유지된다).

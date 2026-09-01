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
산출물을 만든다. 격리된 조립 단계(신규 산출물을 임시 디렉터리에 완성하는 동안)의
실패는 기존 게시 세트에 전혀 영향을 주지 않는다(재빌드라면 기존 게시 세트가 그대로
남는다). 반면 그 뒤의 게시(publish) 단계 실패는 기존 세트를 보존한다고 보장하지
않는다 -- 게시는 기존 RELEASE.SHA256SUMS 마커를 먼저 지워 out_dir 를 "미게시" 상태로
만든 뒤 신규 산출물을 옮기므로, 도중 실패하면 부분 상태가 남을 수 있다. 다만 이 경우
RELEASE 마커가 없거나 세트가 불완전하므로 `verify_release`/`--verify` 가 검증을
거부한다(fail-closed) -- "이전 상태가 보존된다"가 아니라 "손상된 상태를 안전 쪽으로
드러낸다"는 보장이다.

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
├── localcrab-plugin/                          # plugin root — 클라이언트가 설치하는 전부 (로컬 편의 산출물, 릴리스 첨부 대상 아님)
│   ├── plugin.json  mcp.json  README.md  LICENSE
│   └── skills/localcrab-query/SKILL.md
├── localcrab-plugin.SHA256SUMS                # 사이드카 — plugin root 밖, 정렬된 상대경로 해시
├── localcrab-plugin-<v>.tar.gz                # 릴리스 아카이브(결정론, top prefix localcrab-plugin/)
├── localcrab-plugin-<v>.COMPATIBILITY.md      # compat report — docs/agent-plugin-compatibility.md 정본에서 생성
└── localcrab-plugin-<v>.RELEASE.SHA256SUMS    # 릴리스 세트 해시(위 3파일 — staged 디렉터리 제외)
```

`localcrab-plugin/` 스테이징 디렉터리는 파일을 직접 들여다보기 위한 로컬
편의 산출물이다. GitHub Release 에 첨부하는 것은 나머지 4파일(패키지
사이드카 `localcrab-plugin.SHA256SUMS` + 버전 부착 3파일)이며, staged
디렉터리는 첨부 대상이 아니다.

검증 명령:

```bash
python scripts/build_agent_plugin.py --verify --out dist
cd dist && sha256sum -c localcrab-plugin-*.RELEASE.SHA256SUMS
```

CI 재현은 `.github/workflows/agent-plugin.yml` 참고(`ci.yml` 은 이 워크플로에
무관하게 유지된다). 릴리스 공표 절차·수령자 검증·신뢰 모델은
[`docs/agent-plugin-release-policy.md`](../../docs/agent-plugin-release-policy.md),
compat report 의 정본은
[`docs/agent-plugin-compatibility.md`](../../docs/agent-plugin-compatibility.md)
참고.

# 팩 계약 계층 (`opencrab/pack/`)

이 문서는 `opencrab/pack/` 아래에 있는 모듈들이 왜 한 패키지에 모여 있고 각자 무엇을 책임지는지 정리한다. 개별 모듈의 세부 동작은 각 파일의 docstring이 정본이다 — 이 문서는 지도(map) 역할만 한다.

## 왜 이 계층이 존재하는가

교환 포맷(`{nodes,edges,chunks}.jsonl` 3파일)은 팩을 **만드는 쪽**(생산자)과 그 팩을 **읽어 스토어에 반영하는 쪽**(적재기)이 공유하는 계약이다. 이 계층이 만들어지기 전에는 생산자와 소비자가 서로 다른 리포지토리에 있어 아무도 둘을 대조하지 못했고, 그 결과 실제로 두 번의 사고가 났다.

1. 노드 커스텀 필드 91만 건이 라이브에 도달하지 못했다 — 생산자는 필드를 노드 최상위에 펼쳤는데 소비자는 중첩 `properties`만 읽었다. 어느 게이트도 이 불일치를 잡지 못했다.
2. 엣지 방향 정규화 규칙이 생산 쪽에 없어서, 파일과 라이브의 엣지 수가 다를 때 그게 유실인지 정상인지 판정하려면 스토어 스키마를 역공학해야 했다.

계약(포맷 정의)·생산자(빌더)·정규화(해석)·소비자(적재기)를 한 패키지로 모으면 빌드부터 적재까지 하나의 스위트로 왕복 검증할 수 있다. 이것이 이 계층의 존재 이유다.

## 모듈 지도

| 모듈 | 책임 | 비고 |
|---|---|---|
| `schema.py` | 교환 포맷의 계약 정본 — 레코드 한 건의 모양(노드/엣지/청크 구조, 예약 키), grammar 편집표(`ALLOWED`/`FIX`/`KEEP`/`TRACE_SRC`), 로더 remap표(`NODE_TYPE_OVERRIDE`/`SPACE_DEFAULT_TYPE`) | `ALLOWED`는 `opencrab.grammar.manifest`에서 **유도**되므로 grammar 원본과의 드리프트가 구조적으로 불가능 |
| `jsonl_io.py` | 물리 레이아웃 — 단일 파일 vs 40MB 초과 시 자동 분할(shard) 읽기/쓰기 | 미개조 소비자가 분할 파일의 base만 읽고 일부만 얻는 silent partial read를 `FileNotFoundError`로 즉사시키는 loud-fail 설계 |
| `normalize.py` | 순수 정규화 함수 — 라벨·공간 해석(`resolve_node_space_type`, `resolve_edge`), grammar 판정(`fits`류) | 부작용 없음(스토어 쓰기·env·파일 I/O 없음). 적재 경로·grammar 게이트·리포트가 **같은 함수**를 호출해야 판정이 갈라지지 않는다 |
| `build.py` | 생산자 헬퍼 — `Pack(slug, title)` 클래스. uid 네임스페이싱, evidence/청크 생성, 빌드타임 검증 | `schema.py`/`jsonl_io.py` 계약대로 쓰는 쪽이며 집합·표를 재선언하지 않는다 |
| `load.py` | 적재기 — 3-jsonl을 그래프/문서/SQL/벡터 4스토어에 반영 | 쓰기 함수는 각자 `live_data.require_live_data()`를 호출한다(진입점 한 곳에서만 부르면 진입점을 우회하는 직접 호출 경로에서 가드가 빠진다) |
| `live_data.py` | 쓰기 경로 공통 가드 — 대상 데이터 디렉터리가 실재하는지 확인 | 값을 어떤 방식으로도 정규화(expanduser 등)하지 않는다 — 정규화는 항상 수용 집합을 넓히고, 넓어진 만큼이 엉뚱한 저장소로 잘못 쓸 위험이다 |
| `gates/` | 구조 품질 게이트 3종 — `dangling.py`(참조 무결성 + evidence/chunk 대사), `grammar_fit.py`(적재 시 grammar로 튕길 엣지 사전 예측), `score.py`(100점 루브릭 채점 `grade_pack`) | **판정만 하고 출력하지 않는다** — 형식·argv·종료코드는 호출자 CLI 몫. 팩 종류별 도메인 지식(어느 팩이 어떤 출처를 가져야 하는가 등)은 여기 두지 않는다 — 그러면 패키지가 특정 팩 컬렉션을 알게 되어 단방향 의존이 깨진다 |
| `cloud.py` | 3-jsonl → `opencrab-cloud-pack-v1` ZIP (OpenCrab Cloud 업로드용) | `assembler.py`와는 **다른 산출물** — 아래 참조 |
| `assembler.py` | 스테이징 디렉토리 → `opencrab-pack-v1` ZIP ([`opencrab-pack-v1.md`](./opencrab-pack-v1.md) 형식, 로컬 그래프 스토어·Neo4j 재현용) | `manifest.json`의 `format_version` 키(`cloud.py`는 `format` 키) |
| `neo4j_export.py` | 그래프 스냅샷 → Pack v1의 Neo4j ingest 아티팩트 | `assembler.py`가 소비 |

## `cloud.py` vs `assembler.py` — 혼동 금지

이름이 비슷하고 둘 다 "3-jsonl 디렉토리를 ZIP으로 만든다"는 점에서 헷갈리기 쉽지만 소비자·manifest 키·산출물이 전혀 다르다.

| | `cloud.py` (`build_cloud_zip`) | `assembler.py` (`assemble_pack_v1`) |
|---|---|---|
| 소비자 | OpenCrab Cloud 업로드 파이프라인 | 로컬 그래프 스토어 / Neo4j 임포트 경로 |
| manifest 키 | `format` = `"opencrab-cloud-pack-v1"` | `format_version` = `"opencrab-pack-v1"` |
| 입력 | `nodes/edges/chunks.jsonl` 3파일 평면 디렉터리 | Neo4j ingest jsonl·evidence 인덱스를 갖춘 스테이징 디렉터리 |
| 산출물 | `graph/*.jsonl`(정규화 노드·엣지) + `cloud/*.jsonl`(documents·chunks) | Neo4j 아티팩트·해시·품질 리포트 포함 |

교차 참조는 0건이다 — 통합하지 않는다.

## 패키지 경계

이 계층은 호출자(어느 리포에서 이 패키지를 쓰든)의 저장 디렉터리 레이아웃이나 보유한 팩 목록을 모른다. 파일 경로는 항상 호출자가 인자로 넘긴다. `opencrab/pack/__init__.py`는 이 원칙에 따라 가벼운 모듈(`assembler`, `cloud`, `neo4j_export`)만 재노출하고, 스토어·임베딩 의존이 무거운 `load`/`gates`는 최상위로 끌어오지 않는다 — 필요한 쪽이 하위 모듈을 직접 import한다.

## 검출력 측정 — 계약 테스트가 실제로 무엇을 잡는가

**"테스트를 늘렸다"는 검출력의 증거가 아니다.** 이 계층은 그것을 수치로 잰다.
`scripts/qa/mutate_module.py`가 모듈 소스에 기계적으로 변이를 심고, 계약 테스트가
그 변이를 죽이는지 센다. 살아남은 변이 하나하나가 **아직 아무도 안 보는 축**이다.

```bash
# 모듈 하나 (클론에서만 — 아래 주의 참조)
python scripts/qa/mutate_module.py <리포루트> \
    opencrab/pack/gates/score.py tests/test_pack_gates_score.py /tmp/sweep.json

# 전 모듈
python scripts/qa/mutate_module.py <리포루트> --all /tmp/sweep.json
```

판정은 넷으로 갈린다: `KILLED`(테스트가 잡음) · `SURVIVED`(못 잡음) ·
`BROKEN`(모듈이 뜨지도 못함 — 검출이지만 계약 검증은 아님) · `HUNG`(시간 초과).
셋을 뭉뚱그리면 "N종 KILLED"가 과대평가된다.

> **주의 — 이 도구는 클론을 스스로 만들지 않는다.** 넘긴 경로를 **제자리에서 변형**한다.
> 반드시 작업용 클론(`cp -Rc` 후 `.git` 제거)을 넘겨라. 실제 작업 트리를 넘기면
> 소스가 변이된 채로 남을 수 있다.

### 종료 조건 — 생존 0, 아니면 등가 증명

생존을 0으로 만들거나, 남는 변이마다 **왜 동작이 같은지**를 증명한다. 추론으로
"등가"라고 쓰지 않는다 — 입력 격자로 차분 0을 재거나 도달 불가를 보인다.
그리고 그 증명의 **전제 자체를 테스트로 건다**. 전제가 깨지면 거기서 빨간불이 나야
"등가였는데 이제 아니다"를 알 수 있다(`tests/test_pack_gates_score.py`의
`TestProvenEquivalences`, `tests/test_pack_cloud.py`의 `TestNodeIdFiltering`이 그 예다).

현재 수치(2026-08-10):

| 모듈 | 총 | KILLED | 생존 | 생존의 성격 |
|---|---|---|---|---|
| `gates/score.py` | 326 | 317 | 9 | 전량 등가 증명(엣지 label 미사용 5 · s6 하한 도달불가 2 · `most_common(1)` 1 · `min(1.0)` 1) |
| `cloud.py` | 202 | 198 | 4 | 전량 등가 증명(kwarg 상수 1 · dangling 선점검 흡수 3) |

### 새 모듈은 `PACK_SUITES`에 등록해야 한다

`--all`의 대상은 사람이 고르지 않는다. `mutate_module.py`의 `PACK_SUITES`가
모듈 → 그 모듈의 계약 테스트 대응표이고, `opencrab/pack/` **하위 전체**(`rglob`)에서
등록되지 않은 모듈이 발견되면 `--all`이 **죽는다**. 반대로 표에 있는데 파일이 없어도 죽는다.

이 양방향 검사가 실제 결함을 잡았다: 게이트가 `gates/` 하위 패키지로 들어왔을 때
탐색이 한 층(`glob`)만 봐서 세 모듈이 **조용히 스윕 밖에 있었다**(2026-08-10).
등록돼 있으므로 탐색을 한 층으로 되돌리면 이제 "파일이 없다"로 죽는다 —
**등록 그 자체가 실명 방지 장치다.**

대응이 맞는지도 부분적으로 강제된다(`_assert_tests_import_the_module`: 테스트가 그
모듈을 import하는가). 다만 그 검사는 import문 존재만 보므로 **실제로 검사하는가**는
보증하지 않는다 — 그래서 `sweep()`이 "KILLED 0이면 배선을 의심하라"는 경험적 게이트를
따로 둔다. 도구 docstring에 한계가 전부 적혀 있으니 수치를 인용하기 전에 읽어라.

**복합(두 위치 동시) 변이는 하지 않는다.** n²/2라 현실적이지 않다.
스윕 통과를 "전부 훑었다"로 읽지 마라 — 도구가 만드는 축(비교 연산자·not·and/or·
기본값·상수·문장 삭제·표 리터럴·호출 대상) 밖에는 여전히 열린 축이 있다.
예로 `ZIP_DEFLATED`→`ZIP_STORED` 같은 **상수 교체**나 `writestr` **순서 재배열**은
그 축에 없어서 스윕이 초록인 채로 적대 검증이 손으로 뚫었고,
그래서 `tests/test_pack_cloud.py`에 물리 표현 계약을 따로 두었다.

## 전형적 사용 순서

호출자마다 구체적인 스크립트·CLI 이름은 다르지만, 이 계층을 쓰는 흐름은 공통적으로 아래를 따른다.

1. **빌드**: 호출자의 빌더가 `build.Pack`으로 `{nodes,edges,chunks}.jsonl` 3파일을 생성.
2. **게이트**: `gates.dangling` / `gates.grammar_fit` / `gates.score`로 구조를 검증(호출자가 이 판정 위에 CLI 출력·종료코드를 얹는다).
3. **적재**: `load`의 함수들로 4스토어에 반영. 재적재 대비 삭제(`delete_pack`), 증분 적재(`load_nodes_incremental`/`load_chunks_incremental`) 경로도 포함.
4. **배포(선택)**: 목적에 따라 `cloud.build_zip` 또는 `assembler.assemble_pack_v1`으로 ZIP 조립.

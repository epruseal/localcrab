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

## 호출자가 반드시 아는 계약 — `jsonl_io.py`

### `shard_paths`는 "모르는 shard 이름"을 조용히 무시하지 않는다

두 자리(`00`~`99`) zero-pad 밖의 숫자 꼬리(`.100`, 유니코드 숫자 등)나 `.gz` 인접 파일
(`{stem}{suffix}.gz`, `{stem}.NN{suffix}.gz`)을 만나면 `RuntimeError`로 죽는다. 이 스킴이
읽을 줄 모르는 형태의 파일이 조용히 "부재"로 읽히는 것을 막는 설계다 — base+shard 공존을
막는 기존 loud-fail(위 모듈 지도 참고)과 같은 이유다.

발견은 `scandir` + 정규식 한 벌로 한다(`glob`이 아니다) — `Path.glob`은 stem에 `[`·`*`
같은 메타문자가 있으면 실재하는 shard를 못 찾는다. 발견은 넓게(`\d+`, 유니코드 숫자
포함) 잡고 지원 판정은 좁게(ASCII `[0-9]{2}` fullmatch) 하므로, 두 자리 밖 숫자는
"발견됐지만 이 스킴은 지원 안 함"으로 갈린다 — 못 찾아서 안전한 것과 찾았는데 거부하는
것은 다른 안전성이다.

`_shard_path`의 shard 개수 상한 검사(`idx >= _MAX_SHARDS`)는 `assert`가 아니라
`raise RuntimeError`다 — `python -O`로 실행되는 프로세스에서도 사라지지 않는다.

재현:
```
PYTHONPATH=<이 리포> python -c "
from opencrab.pack.jsonl_io import shard_paths
from pathlib import Path
Path('/tmp/probe.100.jsonl').write_text('{}')
shard_paths(Path('/tmp/probe.jsonl'))"   # RuntimeError: 지원 밖 shard 이름 발견
```

pytest 대상: `tests/test_pack_jsonl_io.py`의 `TestShardPathsSingleScandirPass`·
`TestOptimizedModeDoesNotDisableTheGuard`.

## 호출자가 반드시 아는 계약 — `load.py`

이 셋은 **틀리면 조용히 망가진다**. 예외도 안 나고 수치만 거짓이 된다.

### 1. 회수(reclaim)와 대사(reconcile)는 **일부러** 다른 술어를 쓴다

| | 어디 | 팩 소속 판정 키 |
|---|---|---|
| **회수** — 지운다 | `delete_pack` 의 graph_nodes·doc_nodes | `pack_id` |
| | `delete_pack` 의 doc_sources | `pack_id` \| `source` |
| | `delete_pack`·`live_pack_state`·`pack_live_counts` 의 chroma | `pack_id` |
| **대사** — 센다·비교한다 | `COUNT_SQL` 노드·엣지 · `live_pack_state` 노드·엣지·`doc_node_spaces` | `pack_id` |
| | `COUNT_SQL["docs"]` · `live_pack_state` 청크 | `pack_id` \| `source` |
| | `pack_live_counts` **벡터축** | sql·sqlalchemy: `pack_id` 컬럼 · chroma: `pack_id` |

**두 축이 갈리는 자리는 청크축 하나뿐이다.** `doc_sources` 는 회수도 대사도 `pack_id | source`
를 본다 — `transform_chunk_meta` 가 `source = pack_name` 을 **강제**하고 원본 `source` 는
`source_doc` 으로 옮기므로, 청크에서 `source` 는 소유 태그가 맞다. 그 계약을 지키는 것이
청크를 쓰는 **모든** 경로가 `transform_chunk_meta` 를 통과한다는 조건이다.

### 노드·엣지축에서 `source`·`source_id` 를 회수 키로 쓰지 마라

`transform_node` 는 `NODE_STRUCT_KEYS` 밖의 입력 키를 properties 에 **그대로 병합한다.**
입력 노드가 `source: "<다른 팩>"` 을 갖고 있으면 그 값이 살아남는다. 그것을 회수 키로 두면
`pack_id` 가 B 인 행이 `delete_pack("A")` 에 걸린다. **소유 태그가 아니다.**

    PYTHONPATH=<이 리포> python -c "from opencrab.pack.normalize import transform_node; \
      print(transform_node('owner', {'id':'n1','properties':{'source':'foreign'}})[3])"

### `pack` 은 폐기됐다 (#159, #171)

`properties.pack` 은 `pack_id` 의 **사본**이었다. 생산자가 언제나 같은 값으로 썼고,
읽는 자리는 `ontology.builder.add_node` 가 노드 벡터 메타의 `source` 를 만들 때 하나뿐이었다.
그런데 `pack_id` 만 덮고 `pack` 은 보존하는 writer 들이 있어 한 행이 서로 다른 두 소유
태그를 갖는 상태가 생길 수 있었고, 그 행이 builder 를 지나면 벡터 `source` 가 **옛 이름**
으로 찍혔다. 축 자체를 없앴다.

- **벡터 `source` 는 `pack_id` 에서 나온다.** `pack_id` 가 없는 행은 빈 문자열이다 —
  그 행에 source 를 주던 유일한 근거가 그 별칭이었다.
- **소유 태그를 쓰는 자리는 `opencrab/common/pack_tags.py` 를 지난다.** 팩 권위 writer
  (`normalize`, `load_edges`, mcp 팩 스탬핑, provenance backfill, 이관 스크립트)는
  `apply_pack_tag` 로 정규화하고, 범용 진입점(`builder.add_node`/`add_edge`,
  `HybridQuery.ingest`)은 `canonicalize_pack_alias` 로 **불일치를 `ValueError` 로 거부**한다.
  한 행이 `pack` 과 truthy `pack_id` 를 동시에 갖고 값이 다를 수 없다는 것이 그 불변식이다.
- **`pack_fork`(#201)는 doc·엣지 축에서 복사 전에 폐기 별칭을 버린다.** 복사본은 새 팩
  소유이므로 fork 는 preflight 의 소스·엣지 루프에서 `canonicalize_pack_alias` **다음에**
  `strip_retired_keys` 를 부른다. 순서가 계약이다 — canonicalize 를 먼저 불러야
  "truthy `pack_id` + 다른 별칭" 이 지금처럼 Tier 1 데이터 결함으로 잡히고, strip 을 먼저
  하면 그 모순이 조용히 삼켜진다. 완화를 writer 쪽에 넣지 않는 이유도 같다: 범용 funnel 에서
  버리면 모든 호출자에 대해 fail-open 이 된다. 벡터 축의 `validate_import_records` 가 같은
  이유로 이미 같은 선택을 하고 있어, fork 는 그 선례를 doc·엣지 축에 맞춘 것이다. 자세한
  근거는 `opencrab/pack/fork.py` 모듈 docstring 의 "RETIRED ALIASES" 절에 있다.
- **`pack` 만 있고 `pack_id` 가 없는 행은 보존한다.** 모순이 아니고, 임의 속성을 그대로
  저장한다는 진입점 계약을 깰 이유가 없다. 읽는 코드가 0곳이라 무해하다.
- **증분 대조는 `pack` 을 무시한다**(`load.INCREMENTAL_IGNORED_KEYS`, 노드축·청크축 둘 다).
  빼지 않으면 그 키를 가진 라이브 행이 매 증분 전량 chg 로 잡히는데, neo4j 의 upsert 는
  전달된 키만 SET 하므로 재기록해도 사라지지 않아 그 재기록이 영구히 반복된다.
- **properties 형상이 바뀌면 다음 증분 한 번은 전량 chg 다**(#279). 라이브 행의 properties 가
  파일 파생 properties 와 다르면 그 행은 chg 로 잡힌다. 그 런의 CAS 갱신이 properties 를
  전량 치환하므로 **그 다음 런은 same 으로 복귀한다.** 전량 chg 를 한 번 보는 것 자체는
  결함이 아니라 로더가 파일 상태로 되돌리는 중이라는 뜻이다. 결함은 그 다음 런도 전량
  chg 일 때다 — 증분 모드가 매 런 전량 재임베딩으로 퇴화한다. 확인 방법은 같은 입력으로
  증분을 한 번 더 돌려 same 이 복귀하는지 보는 것이다. 회귀 고정은
  `tests/test_pack_load.py` 의 `test_live_property_drift_converges_in_one_run` 이다.

  이 수렴 문장은 **그래프 노드 properties 축에만** 걸고 **그래프 CAS 쓰기와 doc 쓰기가
  성공했음을 전제**로 한다. doc 쓰기가 계속 실패하면 그래프 properties 가 수렴해도
  `doc_row_missing` 검사가 매 런 chg 를 만드는데, 그것은 doc 행 회수 축의 증상이지
  properties 드리프트의 재발이 아니다(그 축은 `test_pack_load_r12_selfheal_gates.py`).
  등록부·벡터 쓰기 실패도 같은 이유로 이 문장의 대상이 아니다. "전체 적재 상태가 한 런에
  수렴한다"로 읽으면 안 된다.

  **이 수렴은 CAS 갱신이 properties 를 전량 치환한다는 데 기댄다.** SQL 백엔드는 properties
  열을 통째로 쓴다. neo4j 는 사전 검사를 라이브 속성 재계산으로 하고 실제 쓰기는 저장된
  `node_digest` 속성으로 CAS 를 걸어 **출처가 둘**이다. 두 값이 갈리면 갱신이 0행을 잡아
  드리프트가 해소되지 않는다. 그 축은 #298 이 추적한다.
- **라이브 잔여분은 청소하지 않는다.** 읽는 코드가 없어 무해하다. 확인하고 싶으면
  실행 중인 백엔드에 대해 읽기 전용으로 센다(아래는 로컬 SQLite 예시 — 다른 백엔드·다른
  축은 같은 형태로 각자 질의해야 한다):

```sql
-- graph.db
SELECT COUNT(*) FROM graph_nodes WHERE json_extract(properties,'$.pack') IS NOT NULL;
SELECT COUNT(*) FROM graph_nodes
 WHERE json_extract(properties,'$.pack') IS NOT NULL
   AND json_extract(properties,'$.pack_id') IS NOT NULL
   AND json_extract(properties,'$.pack') <> json_extract(properties,'$.pack_id');
```

### 통일하려는 시도를 조심하라

과거에 둘을 맞추려는 시도가 반복해서 실패했다. 남아 있는 근거:

- **대사를 넓히면**: `load_edges` 가 팩 파일의 임의 속성을 엣지 props 에 병합하므로, 어떤 엣지가
  `source: "<다른 팩>"` 을 갖고 있으면 그 다른 팩의 증분이 그것을 `live_edges` 에 담고
  자기 `applied_edges` 에 없으니 stale 로 판정해 **타 팩 엣지를 지운다.**
  그리고 `graph_nodes` 는 `pack_id` 에만 인덱스가 있어 넓히면 커버링 인덱스를 잃는다.
- **doc 축만 넓히면**: `pack` 으로만 태그된 행의 doc 은 지워지는데 graph 는 `live_pack_state`
  에 안 잡혀 **영영 남는다.** 축 하나를 넓히면 그 축과 짝인 축도 같이 넓어져야 한다.
- **한쪽만 넓히면**: `incremental_finalize` 의 엣지 DELETE 는 팩 절이 좁다. 대사를 넓히고
  이 DELETE 를 안 넓히면 그 엣지가 **매 증분마다 stale 로 뽑혀 0행을 지운다** —
  조용한 과다계상이 조용한 영구 무동작이 된다. 두 술어는 **짝**이다.

`pack_id` 없이 `source`/`source_id` 로만 태그된 `graph_nodes`/`graph_edges`/`doc_nodes`
행은 회수 술어와 증분 대사 술어 어느 쪽에도 직접 안 걸린다(`pack`-only 잔여는 위 "`pack`
만 있고..." 판정대로 무해, 이 사각지대와 안 섞인다). **단 `graph_edges` 와 `doc_nodes` 는
예외가 있다** — 그런 엣지도 양 끝 노드 중 하나가 `pack_id` 로 회수되면 `graph.delete_node()`
의 cascade 로 함께 지워진다(엣지 자신의 태그와 무관). `doc_nodes` 트윈도 마찬가지다 —
`delete_pack` 은 `pack_id` 로 고른 각 graph 노드의 `node_id` 로
`docs.delete_node_doc(space, node_id)` 를 그 doc_nodes 행 자신의 태그와 무관하게
호출하므로, `source`/`source_id`-only 로만 태그된 doc_nodes 트윈도 같은 node_id 의 graph
노드가 `pack_id` 로 회수되면 함께 지워진다. 이 문단이 말하는 "안 걸린다"는 양 끝/짝
노드가 어느 팩에도 안 걸린 경우다. `load.fallback_tag_without_pack_id_counts()` 가 그
잔여의 존재를 전역(팩 비한정)으로 탐지한다(localcrab #164) — 실제로 그런 행이 나오면
어떻게 닫을지(생산자 `pack_id` 필수화 vs 주기적 sweep), 그리고 이 함수를 실제 진단
표면에 연결할지는 localcrab #325 가 다룬다(이 함수를 호출하는 프로덕션 코드는 현재
저장소에 없다 — `pack_live_counts`/`incremental_finalize` 계열 전부 테스트 전용).

### 2. `pack_live_counts()` 는 `int | None` 을 돌려준다

`None` 은 **"셀 방법이 없다"**(백엔드가 팩 단위 열거를 지원 안 함), `0` 은 **"세어 보니 없다"** 다.
다른 사실이라 섞으면 안 된다 — 종전에는 둘 다 `0` 이라 Chroma·pgvector 에서 **항상 결손처럼
보였다**. 호출자는 **산술 전에 `None` 을 걸러라.** 그냥 빼면 `TypeError` 다.

### 3. `load_nodes_incremental(..., doc_node_spaces=)` 는 **필수**다

`live_pack_state` 가 돌려주는 `{node_id: {space, ...}}` 를 그대로 넘겨라. 기본값을 두지 않은
것이 의도다 — 기본값이 있으면 안 넘긴 호출자에서 doc 잔재 정리가 **조용히 꺼지고** 그 사실이
어디에도 안 남는다.

이 인자가 닫는 것은 **타입 변경 잔재**다. 노드 타입이 바뀌면 새 타입 행이 저장된 뒤 구 행을
지우는데, graph 는 지워지고 doc 이 남으면 다음 실행의 `live` 조회가 새 타입만 보고 `same` 으로
끝나 **재시도조차 안 한다.** 그래서 `same` 경로에서도 이 정리가 돈다.

`incremental_finalize` 가 이 누락을 대신 잡지 못한다. 그쪽 doc 축 후보는
`set(doc_node_spaces) - bypack_node_ids` 라 **입력에 아직 있는** 노드는 후보가 아니다.
타입 변경 잔재는 정의상 입력에 있는 노드의 것이므로 이 자리에서만 걷힌다.

빈 dict 는 유효한 입력이다(대사할 doc 행이 없다는 사실). `None` 과 다르다.

### 4. 앵커 판정은 **한 곳에서만** 정의한다

Python 술어와 SQL 조각을 같은 정의에서 낸다. 두 벌로 두면 갈린다. 두 함정이 실측됐다.

- SQLite `LIKE` 는 ASCII 대소문자를 **무시**한다. `LIKE 'dataset:%'` 는 `DATASET:x` 를 앵커로
  보고 Python `startswith("dataset:")` 는 안 본다. 그러면 graph 는 삭제 후보인데 doc 은
  제외돼 두 축이 갈린다. **`GLOB 'dataset:*'` 을 써라.**
- `json_extract(...) <> 'title-backfill'` 은 키가 없을 때 NULL 비교라 **UNKNOWN** 이 되고
  그 행이 통째로 빠진다. 라이브 doc 노드에는 `created_by` 가 없으므로 이 형태를 쓰면
  후보가 전량 사라진다. **`COALESCE(..., '')` 로 감싸라.**

재현: 같은 node_id 격자(`dataset:` · `DATASET:` · `DaTaSeT:` · `created_by` 없음)를 두 구현에
태워 판정이 일치하는지 본다.

### 5. `_vec_meta_update`의 chroma 분기는 delete+add **치환**이다

chromadb의 `update`/`upsert`는 메타데이터를 **병합**한다(겹치는 키만 갱신, 그 외 키는
존속) — 실측(2026-08-12, chromadb 1.5.7, `EphemeralClient`). 청크 스키마가 줄어들어
없어진 옛 메타 키(스테일 키)를 지우려면 병합이 아니라 치환이 필요해서, 이 분기는
`delete`+`add`로 레코드를 처음부터 다시 짓는다.

URI가 붙은 레코드(`uris` API로 만들어진 레코드 — 이 시스템은 그런 레코드를 생산하지
않는다)는 **치환하지 않는다.** 외부 기록으로 보고 `False`를 돌려 호출자가 upsert
병합으로 우회하게 한다. 치환 후에는 4축을 검증한다 — ID 동일성, 메타 정확 일치, 문서
일치, 임베딩 존재·차원·허용오차(상대+절대 `1e-6`) 값 비교. 하나라도 어긋나면 `False`
(재임베딩으로 우회).

**닫지 않은 창**: `delete`가 실패하고 호출자가 upsert로 우회하면, 겹치는 메타 키는
갱신되지만 그 외 스테일 키는 살아남는다(병합이므로). 이 창은 `localcrab#175`로
위임돼 있다 — `_vec_meta_update`가 닫는 범위가 아니다.

재현: `tests/test_pack_load.py`의 `TestVecMetaUpdateChromaReplace`(결함주입
`_FakeChromaCollection`으로 get/delete/add 각 실패 축과 후검증 축을 개별로 확인).

### 왜 "세는 집합"과 "지키는 집합"이 다른가

`ok` 카운터는 *이번 실행이 실제로 저장한 수*이고, `applied_edges`·`bypack_ids` 는
*삭제 보호 집합*이다. **실패해도 보호 집합에는 넣는다** — 파일에 있는 한 고아가 아니다.

한 집합으로 겸용하면 "카운트를 정확하게 고치면 삭제가 바뀌는" 결합이 생긴다. 실측 반례:

    live_edges={('f','r','t')}   # 라이브에 이전 값이 있다
    applied_edges=set()          # 이번 저장이 실패해 빠졌다
    stale_delete_would_run=True  # 그래서 멀쩡한 이전 값이 지워진다

`add_node`/`add_edge` 는 스토어 실패를 **예외로 안 올리고 반환 dict 에 적는다**
(`ontology.builder.store_write_failures()` docstring 이 "호출자가 이것을 불러야 실제 성공
여부를 안다"고 명시한다). 그 영수증을 봐야 `ok` 가 참이 된다.

**이 분리가 "저장 실패 시 정리 보류" 안전핀이 불필요한 이유다.** 네 삭제 축의 보호 집합이
전부 저장 **전에** 파일에서 만들어진다.

| 축 | 보호 집합이 채워지는 시점 |
|---|---|
| 노드 | `transform_node` 직후, `add_node` 전 |
| 청크 | 중복 제외 직후, 저장 전 |
| 엣지 | `resolve_edge` 직후, `add_edge` 전 |
| 벡터 고아 | 위 두 집합의 합에서 유도 |

따라서 **저장에 실패한 행은 이미 삭제 후보가 아니다.** 그 위에 "실패가 있으면 팩 전체 정리를
건너뛴다"는 핀을 얹으면 보호가 늘지 않고 정상 정리만 막는다. 결정적 실패(문법위반 등)가 있는
팩은 그 정리가 **영구히** 꺼진다. 그래서 그 핀을 제거했다.

원본에 이미 있던 **0건 핀**(by-pack 이 비었는데 라이브에 데이터가 있으면 중단)과
**비율 핀**(삭제 후보 비율 초과 시 중단)은 성격이 다르다 — 그것들은 "입력 자체가 수상하다"를
보는 것이고 그대로 남아 있다.

## 호출자가 반드시 아는 계약 — `gates/score.py`

### 소스 커버리지(3번 항목)는 `document_id` 검증형 폴백을 쓴다

청크의 `source`가 결측(`None`)이거나 빈 문자열이면 **절대 그대로 계수하지 않는다.**
그 청크에 `document_id`가 있고 그 값이 실제 resource 노드 id 집합(`res_ids`)에 있을
때만 그 resource를 폴백으로 계수한다. `document_id`가 무관한(어느 resource도 아닌)
값이면 여전히 미계수다 — 검증 없는 폴백은 합성 반례(무관 `document_id`)에서 0점이어야
할 팩에 점수를 준다.

`source`가 있는 청크는(값이 무엇이든) 이 폴백과 무관하게 종전처럼 그대로 계수된다 —
폴백은 **source 결측(빈 문자열 포함)일 때만** 켜진다. `source` 문자열이 우연히
resource id와 같으면 같은 원소로 뭉쳐 dedup이 **과소** 방향으로만 틀어진다(과다
계수는 없다).

측정(2026-08-12, `by-pack` 129팩 전수 재채점 — `grade_pack` 대상은 그 중 128팩뿐이다,
아래 "129/128 구분" 참고):

| 팩 | 이전 | 이후 |
|---|---|---|
| claude | 68 | 88 |
| codex | 71 | 90 |
| fable-mac | 71 | 91 |
| fable-rpi | 73 | 93 |
| openclaw | 71 | 91 |

이 5팩만 바뀌었다. 나머지 123팩은 diff 0.

재현(이 커밋의 `score.py`와 임의 이전 커밋을 대조):
```
PYTHONPATH=<이 리포> python -c "
from pathlib import Path
from opencrab.pack.gates.score import grade_pack
root = Path('<by-pack 경로>')
for d in sorted(root.iterdir()):
    if d.is_dir():
        r = grade_pack(d)
        if r: print(d.name, r['total'])"
```
이전 버전과 대조하려면 `git show <이전 커밋>:opencrab/pack/gates/score.py`를 별도
모듈로 로드해 같은 순회를 두 번 돌리고 `total`을 비교한다.

pytest 대상: `tests/test_pack_gates_score.py`의 `TestSourceCoverageResourceFallback`.

### 129/128 구분

`by-pack`에는 팩 디렉터리가 129개 있지만 `grade_pack`이 판정을 내는 것은 128개뿐이다
— `honda-parts-vision`은 `nodes.jsonl`이 없어 `grade_pack`이 `None`을 돌려준다
(`None`은 "검사 불가"이지 "0점"이 아니다, `TestMissingNodesFileIsNotAVerdict` 참고).
"128팩 전수"라는 표현이 이 문서·커밋 메시지에 나오면 이 1건이 빠진 수라는 뜻이다.

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

### 이 문서에 **생존자 수를 적지 않는다**

수치는 커밋마다 바뀐다. 문서에 박는 순간 썩기 시작하고, 다음 사람은 그것을 기준선으로
인용한다. 이 리포에서 실제로 네 번 났다 — `score.py` 격자 전을 58 로 적었으나 참값은 162,
전체 스위트를 3,078 로 인용했으나 3,092, `test_pack_load` 를 75 로 인용했으나 84,
라운드 커밋 수를 9 로 적었으나 10. 매번 **어느 시점엔 참이었던 값**이다.

한때 처방이 "표에 커밋 열을 둔다"였다. 그건 **스테일을 탐지 가능하게 만들 뿐 썩는 값을
그대로 둔다.** 그래서 기록 대상을 바꾼다 — **변경에 안정한 것만 적는다.**

| 적는다 | 안 적는다 |
|---|---|
| 모듈별 **상태**: `해소`(생존 0 또는 전량 등가 증명) / `미해소` | 생존자 **개수** |
| 등가 증명의 **내용**(무엇이 왜 등가인가) | 총 변이 수·KILLED 수 |
| **재현 명령** — 읽는 사람이 지금 값을 얻는다 | 측정 시점의 스냅샷 |

수치가 필요한 자리는 **커밋 메시지와 이슈**다. 둘 다 날짜가 박히고 나중에 고쳐지지 않으므로
"그때 그랬다"로 읽힌다. 살아 있는 문서에 두면 "지금 그렇다"로 읽혀서 위험하다.

| 모듈 | 상태 | 근거 |
|---|---|---|
| `gates/dangling.py` | **해소** | 생존 0 |
| `gates/score.py` | **해소** | 전량 등가 증명 — 엣지 label 미사용 · s6 하한 도달불가 · `most_common(1)` · `min(1.0)` |
| `gates/grammar_fit.py` | **해소** | 등가 증명 — `node_type` 기본값은 9-space 전수에서 도달 불가 |
| `cloud.py` | **해소** | 전량 등가 증명 — kwarg 상수 · dangling 선점검 흡수 |
| **`load.py`** | **미해소** | 아래 |

재측정(수치가 필요하면 **여기서 얻어라**, 문서에서 읽지 마라):

```
cd <클론>   # mutate_module.py 는 제자리 변형이다 — 워크트리에서 돌리지 마라
PYTHONPATH=. python scripts/qa/mutate_module.py . opencrab/pack/load.py \
    tests/test_pack_load.py /tmp/sweep.json
```

> **`load.py` 는 미해소다.** 전량 스윕을 재실행해 생존 0 또는 전량 등가 증명이 나오기
> 전에는 상태가 바뀌지 않는다. 이 모듈은 **유일하게 삭제 권한을 가진** 자리다.
>
> 원인의 상당수는 **음성 테스트만 있고 양성 테스트가 없는 것**이었다. `edge_del == 0` 만
> 확인하면 "아무것도 안 지운다"도 통과한다. 적대 검증 두 곳이 각각 재현했다(2026-08-11).
>
> 2026-08-12 수정 라운드가 닫은 클래스(각각 양성 테스트 + 변형 red 재측정으로 고정):
> 삭제 4축 양성 삭제(stale 엣지 루프 통삭제 포함) · 비율 핀 3축 격리와 경계 연산자 ·
> doc 축 분모와 앵커 술어 · FTS 그림자 경로별 삭제 · 같은-space 타입 변경 가드.
> 재현은 위 스윕 명령과 `tests/test_pack_load.py` 의 폐쇄 게이트 변형 목록으로 한다.
> **잔여 생존자는 #166 이 추적한다** — 수치는 그 이슈와 스윕 산출물에서 읽어라.
> 상태가 `해소` 로 바뀌기 전에는 이 표가 **초록이 아니다.**

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
스윕 통과를 "전부 훑었다"로 읽지 마라 — 도구가 만드는 축 밖에는 여전히 열린 축이 있다.
**축 목록은 여기 다시 적지 않는다**: 한번 옮겨 적었더니 `kwarg` 교환·데코레이터 제거·
`raise` 변형·산술 연산자가 빠졌고, 정작 같은 문서의 생존자 표는 빠진 축(`kwarg`)을
인용하고 있었다(자기모순). 정본은 `scripts/qa/mutate_module.py` docstring 의
"변이 대상" 절이다 — 거기를 보라.
예로 `ZIP_DEFLATED`→`ZIP_STORED` 같은 **상수 교체**나 `writestr` **순서 재배열**은
그 축에 없어서 스윕이 초록인 채로 적대 검증이 손으로 뚫었고,
그래서 `tests/test_pack_cloud.py`에 물리 표현 계약을 따로 두었다.

## 전형적 사용 순서

호출자마다 구체적인 스크립트·CLI 이름은 다르지만, 이 계층을 쓰는 흐름은 공통적으로 아래를 따른다.

1. **빌드**: 호출자의 빌더가 `build.Pack`으로 `{nodes,edges,chunks}.jsonl` 3파일을 생성.
2. **게이트**: `gates.dangling` / `gates.grammar_fit` / `gates.score`로 구조를 검증(호출자가 이 판정 위에 CLI 출력·종료코드를 얹는다).
3. **적재**: `load`의 함수들로 4스토어에 반영. 재적재 대비 삭제(`delete_pack`), 증분 적재(`load_nodes_incremental`/`load_chunks_incremental`) 경로도 포함.

   **적재 호출자의 계약(#148, #205).** 적재는 인가를 지난다. 진입점에서 `principal_scope(...)`를 열어야 하고(로더는 principal을 스스로 고르지 않는다), 청크 로더 두 개는 등록부 스토어를 **키워드 전용 필수 인자 `sql`**로 받는다. 노드·엣지는 `OntologyBuilder` 안에서 인가되므로 별도 인자가 없다.

   `sql` 없이 부른 구 호출은 첫 호출에서 `TypeError`로 죽는다 — 아무것도 쓰기 전이다. 등록부를 들 수 없는 원격 도구의 경로는 서버측에서 인가가 도는 `pack_ingest_chunks` MCP 도구이며(`[[ingestion-via-mcp-plan]]`), 아직 구현되지 않았다.

   재현: `pytest tests/test_pack_load_chunk_authz.py`(비소유자 거부와 원본 팩 불변), `pytest tests/test_write_sink_inventory.py`(스토어 쓰기 지점 전량이 writer이거나 선언된 예외인지).
4. **배포(선택)**: 목적에 따라 `cloud.build_zip` 또는 `assembler.assemble_pack_v1`으로 ZIP 조립.

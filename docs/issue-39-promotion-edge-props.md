# #39 승격 적용이 엣지 속성을 그래프에 보존한다

흡수: #40(dry_run 엣지 문법 검증), #44 중 BM25 캐시 무효화. #44의 과금 부분은 흡수하지 않는다(#115가 맡는다).

## 근본원인

`harness_promotion_apply`(`opencrab/mcp/tools/harness.py`)와 그 CLI 쌍둥이 `apply_promotion_package`(`crabharness/crabharness/apply.py`)는 `PromotionEdge.properties`(`crabharness/crabharness/models.py`)를 읽고도 `builder.add_edge()`에 넘기지 않는다. 호출자 전량은 `git grep -n -B2 -A10 "\.add_edge(" -- opencrab apps crabharness scripts`로 다시 얻는다.

| 호출자 | properties 전달 |
|---|---|
| `ontology_add_edge`(MCP) | 전달(`_clean_meta` 적용) |
| `_ingest_into_pack`(pack_ingest) | 전달(`_clean_meta` 적용) |
| REST `/api/edges`, `opencrab` CLI, 팩 fork와 로더, 옵시디언 가져오기 스크립트 | 전달 |
| 시드 스크립트 | 넘기지 않음. 시드 입력에 속성이 없다(유실 아님) |
| `harness_promotion_apply` | 누락(수정 대상) |
| `apply_promotion_package` | 누락(수정 대상) |

## 결정

1. `harness.py`의 엣지 쓰기가 `properties=_clean_meta(edge.properties or {})`를 넘긴다. `ontology_add_edge`와 같은 호출 형태다. 예약 키, JSON 값, digest 정규화는 `builder.add_edge`와 그래프 스토어(`normalize_edge_properties`)가 맡는다. 키 중복 검출은 이 정규화에 없다(중복을 거부하는 `parse_properties_object`는 저장값을 읽는 별도 경로다). 속성 크기 상한은 이 경로에도 `ontology_add_edge`에도 없다. 이 변경은 상한을 만들지 않는다. 승격 경로는 `ontology_add_edge`와 같은 `builder.add_edge` 진입점을 지나고 검증 코드를 새로 만들지 않는다.
2. MCP 호출은 `dispatch_tool`이 `package.edges[*].properties` 안의 서버 소유 신원 키(`owner_id`, `created_by`, `tenant_id`, `subject_id`, `user_id`)를 핸들러 전에 거부한다(`boundary_identity_violations`). CLI 경로에는 그 단계가 없으므로 `apply.py`가 엣지마다 같은 함수의 거부 판정(`reject_boundary_identity`)을 `add_edge` 앞에서 부른다. 거부된 엣지는 `errors`에 오르고 쓰이지 않는다.
3. `apply.py`의 엣지 쓰기가 `properties=edge.properties or {}`를 넘긴다. `opencrab/cli.py`의 같은 호출과 같은 형태다. `apply.py`는 `opencrab.mcp.tools`를 import하지 않으므로 `_clean_meta`를 쓰지 않는다. 결과 차이는 `_clean_meta`가 바꾸는 입력에서만 난다. 값 안의 단독 surrogate 문자를 MCP는 `?`로 바꿔 저장하고, CLI는 스토어가 `GraphPropertyValidationError`로 거부해 `errors`에 오른다. `_clean_meta`는 키도 바꾼다. 서로 다른 단독 surrogate 키가 모두 `?`가 되면 MCP는 앞의 값을 잃는다. 이 동작은 `ontology_add_edge`에 이미 있다. CLI는 같은 입력을 스토어 거부로 `errors`에 올린다. 이 차이는 허용한다. 근거는 MCP 쪽이 `ontology_add_edge`와 같은 동작이어야 한다는 요구와 CLI 쪽이 `opencrab/cli.py`와 같아야 한다는 선례이며, 이 변경이 새로 만드는 유실이 아니라 두 기존 경로의 동작이다. 입력 단계별 키 타입 변환(Python 객체 직접 호출과 JSON 입력의 차이)은 이 설계가 정하지 않는다. list 안의 문자열은 `_clean_meta`가 순회하지 않으므로 양쪽 모두 스토어가 거부한다.
4. dry_run 분기가 `promo.edges`를 순회하며 `validate_edge(from_space, to_space, relation)`을 호출한다. 통과하면 `edge_receipts`에 `{"from_id", "relation", "to_id", "status": "dry_run_valid"}`를, 실패하면 `errors`에 `{"edge": "<from>-[<relation>]-><to>", "error": ...}`를 넣는다. 노드 루프와 같은 모양이다. `dry_run_valid`는 문법 검증 성공만 뜻한다. 속성 규칙, 엔드포인트 존재, 팩 권한, 신원 충돌은 보지 않는다(노드의 dry_run도 스토어를 읽지 않는다). 응답에 최상위 `valid` 키는 없다.
5. 실쓰기 경로가 노드와 엣지 루프가 끝난 뒤, `node_receipts` 또는 `edge_receipts`가 하나라도 있으면 `ctx["hybrid"].invalidate_bm25_cache()`를 한 번 부른다. 영수증이 없는 경우(빈 패키지, 모든 빌더 호출이 예외)는 부르지 않는다. `invalidate()`가 재구축 세대를 올려 진행 중인 재구축의 후보를 버리게 하므로(`opencrab/ontology/query.py`), 아무것도 쓰지 않은 요청이 이를 일으키지 않게 한다. 영수증이 있으면 스토어 상태 문자열이 실패여도 부른다(`ontology_add_node`가 예외 없는 반환에 무조건 부르는 것과 같다). 호출이 예외를 내면 경고 로그에 예외 타입 이름만 남기고 영수증 반환을 막지 않는다.

## 상태 관계

- 엣지 슬롯이 비어 있으면 properties가 든 엣지가 생긴다.
- 같은 슬롯에 같은 properties를 다시 쓰면 멱등이다.
- 같은 슬롯에 다른 properties를 쓰면 `EdgeIdentityConflict`다. 병합도 덮어쓰기도 없다. `ontology_add_edge`와 같다(시험 5번이 이 결과를 고정한다).
- 이 변경 전에 승격된 엣지는 properties가 비어 있다. properties가 든 같은 패키지를 다시 적용하면 그 엣지는 충돌로 `errors`에 오르고 저장값은 안 바뀐다. 이 변경은 그 기존 엣지를 고치는 경로를 만들지 않는다. 클라이언트 origin에는 `replace_own` 같은 교체 경로가 없다.
- 이 변경 전에 일부만 적용된 패키지를 이 변경 뒤 다시 적용하면, 이미 있는 엣지는 충돌로 오르고 아직 없는 엣지만 properties와 함께 생긴다. 패키지 전체를 되돌리는 트랜잭션은 없다.
- properties가 없는 엣지는 종전과 같이 쓰인다.

## 시험과 역변이 대응표

| # | 시험(`tests/test_tools_handlers_direct.py::TestHarnessPromotionApply`, 별도 표기는 예외) | 수정 전 | 검출하는 변이 |
|---|---|---|---|
| 1 | `test_normal_edge_properties_persist_in_graph`: 실제 빌더와 로컬 그래프로 properties가 든 엣지를 적용한 뒤 스토어에서 읽으면 properties가 있다 | 실패(RED) | M1: `harness.py`의 `properties=` 인자 삭제 |
| 2 | `test_normal_edge_properties_match_ontology_add_edge`: 같은 입력(surrogate 문자 포함)을 승격과 `ontology_add_edge`로 쓰면 저장된 properties가 같다 | 실패(RED) | M1, M2: `_clean_meta` 삭제 |
| 3 | `test_error_edge_reserved_property_is_rejected_by_store_rule`: 스토어 예약 키를 가진 엣지 properties는 `errors`에 오르고 엣지가 저장되지 않는다 | 실패(RED) | M1 |
| 4 | `test_normal_edge_without_properties_matches_ontology_add_edge`: properties가 없는 엣지는 `ontology_add_edge`로 쓴 엣지와 같은 키 집합으로 저장된다(대조군) | 통과 | M3: 비었을 때 값을 지어내는 변이 |
| 5 | `test_edge_reapply_same_properties_is_idempotent_different_conflicts`: 같은 properties 재적용은 오류가 없고, 다른 properties는 `errors`에 오르며 저장값이 안 바뀐다 | 실패(RED) | M1 |
| 6 | `test_normal_add_edge_receives_properties_kwarg`: 목 빌더의 `add_edge` kwargs에 `properties`가 있다 | 실패(RED) | M1 |
| 7 | `test_normal_dry_run_validates_edge_grammar`: dry_run에 문법상 무효한 relation 엣지를 넣으면 `errors`에 오르고 유효 엣지는 `edge_receipts`에 오른다. 방향이 있는 공간 쌍의 정방향은 유효하고 역방향은 무효다. 스토어 컨텍스트는 열지 않는다 | 실패(RED) | M4: 엣지 검증 결과 무시, M10: 공간 인자 교환 |
| 8 | `test_normal_apply_invalidates_bm25_cache_once_per_write_and_dry_run_does_not`: 실쓰기마다 `invalidate_bm25_cache`가 한 번 불리고(노드만 있는 패키지와 엣지만 있는 패키지도 같다) dry_run에서는 안 불린다 | 실패(RED) | M5: 호출 삭제, M11: 노드 영수증이 있을 때만 호출, M12: 엣지 영수증이 있을 때만 호출, M13: 인자를 붙여 호출 |
| 9 | `test_edge_no_receipts_means_no_bm25_invalidation`: 빈 패키지와 모든 쓰기가 예외인 패키지는 무효화를 부르지 않는다(대조군) | 통과 | M8: 조건 없이 호출 |
| 10 | `test_error_bm25_invalidation_failure_keeps_receipts`: `invalidate_bm25_cache`가 예외를 내도 영수증을 반환하고 경고를 남긴다 | 실패(RED) | M6: 가드 제거 |
| 11 | `tests/test_crabharness_promotion_billing.py::TestApplyPromotionPackageEdgeProperties::test_normal_edge_properties_reach_add_edge`: `apply_promotion_package`가 `add_edge`에 properties를 넘긴다 | 실패(RED) | M7: `apply.py`의 `properties=` 인자 삭제 |
| 12 | 같은 클래스의 `test_error_reserved_identity_key_in_edge_properties_is_rejected`: 신원 키가 든 엣지는 `add_edge` 앞에서 거부된다 | 실패(RED) | M9: 거부 호출 삭제 |
| 13 | `tests/test_auth_boundary.py::...::test_dispatch_rejects_and_writes_nothing`의 `harness_promotion_apply` 사례: MCP 디스패치가 승격 패키지의 엣지 신원 키를 거부한다(대조군) | 통과 | 해당 없음(기존 거부 규칙을 승격 도구에 고정) |

변이 M1부터 M13은 대상 파일을 직접 편집해 해당 시험이 실패함을 확인한 뒤 원본으로 되돌린다(`docs/gate-recipes.md` 역변이 절의 `opencrab/pack/` 밖 절차).

## 하지 않는 일

- 승격 엣지 과금: #115가 맡는다.
- 이 변경 전에 properties 없이 저장된 엣지의 보정 경로.
- `apply_promotion_package`의 dry_run 엣지 검증과 노드 properties 처리.
- 스토어 쓰기 실패가 반환값으로 올 때 `edges_written`이 영수증 수를 센다는 기존 동작.
- 노드 승격 경로의 변경.

## 관측 경계 판정

`docs/observability-rules.md` 대조: 새 사용자 작업 경계나 trace를 만들지 않는다. 기존 반환 영수증과 오류 모양을 그대로 쓴다. 새 로그는 무효화 실패 경고 한 줄이며 예외 타입 이름만 담고 입력 원문을 담지 않는다.

## SOLID 판정

- 단일 책임, 개방 폐쇄, 리스코프, 인터페이스 분리, 의존 역전: 기존 함수 안에서 인자 하나와 루프 하나와 호출 둘을 더한다. 새 타입과 인자가 없어 걸리지 않는다. `apply.py`와 `harness.py`의 중복 엣지 루프는 이 변경이 만들지 않은 구조이며 합치지 않는다.

## 가정

- 로컬 그래프 스토어가 속성을 저장하고 읽을 때 `ontology_add_edge`와 같은 정규화를 거친다는 가정은 시험 2번과 4번이 실측한다.
- 영수증이 하나라도 있으면 무효화하는 판정은 빌더가 쓰기 전에 거부하는 경우(신원 충돌, 권한)를 예외로 올린다고 가정한다. 빌더가 일부 저장소에 쓴 뒤 예외를 올리는 경로가 있으면 그 호출의 쓰기는 무효화를 놓친다. 이 경우 검색 캐시 갱신은 다음 무효화나 `bm25_fingerprint` 검사까지 늦어질 수 있다. 이 변경은 그 검사에 기대지 않는다.
- 라이브 서비스는 이 변경을 반영하려면 MCP 프로세스를 재시작해야 한다. 재시작은 리드가 한다.

## 받아들임 기준

- properties가 든 승격 엣지를 적용한 뒤 그래프에서 읽으면 속성이 있다는 시험이 있고, 수정 전에 실패한다.
- properties가 없는 엣지와 기존 노드 승격 동작은 바뀌지 않는다.
- 승격 경로가 `ontology_add_edge`와 같은 `builder.add_edge` 진입점을 지난다. 속성 검증 코드를 새로 만들지 않는다.
- 린트가 깨끗하고 전량 회귀 대사에서 base 대비 의도되지 않은 상태 변화가 없다. PR CI가 통과한다.

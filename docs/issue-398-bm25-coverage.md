# #398 BM25 커버리지 경고 설계

## 범위와 판정

이 변경은 전역 recent-first BM25 상한의 결과 선택을 유지한다. 상한 밖의 오래된 팩은 여전히 BM25 hit가 없을 수 있다. 같은 BM25 cache generation이 어떤 팩과 행을 실제로 색인했는지 기록하고, 요청 범위가 빠졌거나 행 일부만 색인됐다는 사실을 기존 `QueryOutcome.warnings`와 MCP `spaces_filter_warnings`로 전달한다.

이 변경은 팩별 배분, SQL 표현식 인덱스, scope cache, 지연 최적화, 상한 조정, 저장소 데이터 정제를 하지 않는다. 이 항목은 #411 범위다. `scope_pack_id()`의 Python 계약을 재사용한다. 이 함수가 반환하는 값만 `covered_pack_ids`에 넣으므로 warning 판단과 BM25 scope 판단은 같은 pack ID 규칙을 쓴다.

## 근본원인

`list_nodes(limit=...)`는 모든 팩에서 최신 노드부터 전역 상한만큼 반환한다. `BM25Index.search()`는 그 뒤에 pack filter를 적용한다. 오래된 팩의 노드가 모두 상한 밖이면 정확한 `pack_ids`를 전달해도 BM25가 복구할 후보가 없다. 현재 cache는 index와 fingerprint만 보관하므로 호출자가 이 누락을 결과 없음과 구별할 수 없다.

## 자료형과 관측

`query.py`는 다음 frozen 자료형을 둔다.

- `ProbeResult(fingerprint, total_rows, failed)`: store fingerprint를 먼저 읽은 결과다. native `bm25_fingerprint()`가 성공할 때만 tuple의 첫 값을 `total_rows`로 쓴다. legacy fallback은 capped build input의 generation marker로 계산한 fingerprint를 비교에만 쓰고 `total_rows=None`, `failed=False`로 남긴다. 이 marker는 정확한 store total fingerprint가 아니다. probe 호출 예외는 `fingerprint=None`, `total_rows=None`, `failed=True`으로 표현한다. 따라서 capped fallback count를 전역 total로 오인하지 않는다.
- `Observation(probe, nodes, covered_pack_ids, indexed_rows)`: 한 build 후보가 사용한 nodes와 같은 nodes에서 계산한 커버리지다. native probe는 fingerprint를 먼저 읽고 nodes를 읽는다. legacy fallback은 capped nodes를 한 번만 읽고, 그 같은 nodes에서 fallback fingerprint, covered IDs, row count, index를 만든다. legacy는 두 번째 `list_nodes()`를 호출하지 않는다.
- `Bm25CacheState(index, probe_fingerprint, indexed_rows, total_rows, covered_pack_ids, generation)`: 검색과 warning이 함께 잡는 immutable publish 단위다.

`covered_pack_ids`는 `frozenset[str]`다. `indexed_rows`는 `len(nodes)`다. `total_rows`는 성공한 probe만 제공한다. out-of-band write는 probe와 `list_nodes()` 사이에 일어날 수 있으므로 total과 covered가 단일 DB snapshot이라는 주장을 하지 않는다.

기존 `_bm25_cache`와 `ensure_built()` 호환 표면은 `state.index`를 계속 보이고 반환한다. 새 worker의 내부 `state`만 immutable publish 단위다. `cache_size`는 state의 indexed rows를 반영한다. 이전 테스트가 검사하는 index identity와 cache identity는 state 내부 index에 유지한다.

## lifecycle

### Cold build

Empty 상태의 query와 첫 wake는 기존 cold-build lock을 공유한다. lock winner는 lock 아래 probe 전에 epoch `e0`를 기록하고 nodes를 읽은 뒤 observation에서 index와 state를 만들고 `Published(S0)`를 한 번 발행한다. cold cache에는 계속 검색할 기존 state가 없으므로 `e0`와 현재 epoch가 다르더라도 첫 S0는 발행한다. mismatch이면 worker는 즉시 wake를 다시 설정한다. loser는 같은 state의 index를 반환한다.

cold probe가 실패하면 nodes는 계속 읽는다. build는 fingerprint 없이 index를 만든다. state는 `total_rows=None`과 failed probe를 보존한다. legacy fallback은 nodes를 한 번 읽고 그 같은 nodes로 fallback fingerprint와 index를 만든 total-unknown state를 발행한다. 검색은 차단하지 않는다. 이 상태의 warning은 coverage total unknown이다. nodes 읽기 실패는 기존처럼 검색의 빈 hit 처리 또는 worker의 기존 cache 유지로 간다.

### Ready probe와 rebuild

Ready hot path는 state를 한 번 잡고 probe를 수행한다. probe가 성공하고 `probe_fingerprint`가 state의 fingerprint와 다르면 invalidate가 wake를 예약한다. probe가 실패하면 state를 교체하거나 metadata를 unknown으로 덮지 않는다. 그러므로 ready probe failure는 기존 search와 기존 warning을 함께 반환한다.

wake마다 worker는 first mover가 `ensure_built()`를 마친 뒤에도 second probe를 한다. second probe가 기존 state fingerprint와 같으면 worker는 state object 전체를 유지하고 dirty만 해제한다. worker는 nodes를 읽거나 index를 만들지 않는다.

Ready S0에서 second probe가 다르면 worker는 짧은 lock 구간에서 P0 probe 이전 epoch `e0`를 기록하고 즉시 lock을 해제한다. `invalidate()`는 같은 lock 안에서 dirty 표시, epoch 증가, wake 예약을 하나의 원자 단계로 수행한다. worker는 lock 없이 native P0 probe, nodes, observation, index build 순서를 수행한다. legacy fallback은 한 번 읽은 nodes에서 observation과 index build를 함께 만든다. build 뒤 worker는 lock을 다시 얻어 현재 epoch와 `e0`를 비교하고, 같을 때만 immutable S1 state를 단일 참조로 교체한 뒤 lock을 해제한다. invalidate가 publish 비교보다 먼저 lock을 얻으면 worker는 epoch mismatch를 보고 candidate S1을 버리며 S0를 유지하고 wake를 다시 설정한다. publish가 먼저 lock을 얻으면 worker는 그 시점에 유효한 S1을 발행한다. 뒤따른 invalidate는 dirty 표시, epoch 증가, wake 예약을 적용해 S2로 수렴한다. 이 ready-state 규칙은 invalidate를 거친 내부 write가 오래된 index와 metadata를 발행하지 못하게 한다. cold S0 발행은 기존 state가 없으므로 이 규칙의 예외이며 앞 절에서 별도로 다룬다.

fingerprint-first observation은 probe 뒤 외부 write가 있을 때 P0 metadata로 만든 새 state를 임시 발행할 수 있다. 다음 hot-path P1 mismatch는 rebuild를 예약한다. 이 허용 경로는 out-of-band write가 epoch를 올리지 못하기 때문에 필요하다.

## warning 계약

공통 private `_bm25_search_state()`는 state를 정확히 한 번 잡고 `state.index.search()`와 coverage warning을 같은 state에서 계산해 `(hits, warnings)`를 반환한다. 새 private `_bm25_search_with_warnings()`와 기존 `_bm25_search()`는 모두 이 공통 helper만 호출한다. 전자는 tuple을 `HybridQuery.query()`에 전달하고, 후자는 hits만 반환한다. 따라서 두 public behavior가 state를 각각 읽지 않는다. `HybridQuery.query()`만 새 wrapper를 호출해 자신의 지역 `warnings`에 더한다. pack registry와 기존 fake hybrid는 기존 hit-list 반환 계약과 시그니처를 유지한다.

- 요청한 pack ID가 `covered_pack_ids`에 없으면 missing warning을 추가한다.
- `total_rows`가 알려져 있고 `indexed_rows < total_rows`이면 partial rows warning을 추가한다. 모든 요청 팩이 covered여도 이 warning을 추가한다.
- cold probe 실패로 `total_rows`가 없으면 coverage total unknown warning을 추가한다.
- ready probe 실패는 기존 state를 쓰므로 기존 warning을 그대로 유지한다.

MCP handler는 기존 `outcome.warnings` 배선을 변경하지 않는다. 따라서 새 구조화 응답 필드 없이 `spaces_filter_warnings`에 이 warning이 나타난다. CLI는 기존 outcome warning 출력으로 같은 문구를 보인다.

## 호출자와 형제 경로

`HybridQuery.query()`만 `QueryOutcome.warnings`를 만든다. 새 `_bm25_search_with_warnings()`만 `(hits, warnings)`를 반환하고 query가 이를 소비한다. 기존 `_bm25_search()`는 같은 `_bm25_search_state()` helper의 hits만 반환한다. 다른 private caller인 pack registry probe와 its fake hybrid는 변경하지 않는다. FTS, vector, graph path는 BM25 cache state를 읽지 않으므로 변경하지 않는다.

MCP `ontology_query()`는 `outcome.warnings`를 이미 `spaces_filter_warnings`에 복사한다. CLI도 `outcome.warnings`를 표준 오류로 출력한다. 이 두 경로의 기존 시험에 BM25 warning 전달을 추가한다.

## TDD와 검증 전략

MVP는 immutable state와 probe-observe-build 분리를 추가한다. legacy fallback은 build generation마다 `list_nodes(limit)`를 정확히 한 번 호출한다. 그 같은 nodes 객체에서 capped-input generation marker, `indexed_rows`, `covered_pack_ids`, BM25 index를 모두 만든다. `total_rows`는 `None`이며 indexed rows를 total로 승격하지 않는다. 시험은 mock 호출 횟수와 nodes object identity를 단언해 probe/build 이중 read를 막는다. native fingerprint backend의 fingerprint-first second probe와 legacy fallback을 같은 호출 순서로 취급하지 않는다. 성공 게이트는 cold build가 한 번만 실행되고, same fingerprint가 index와 metadata를 포함한 같은 state 참조를 유지하며, native fingerprint-first order가 유지되는 것이다. invalidate의 dirty 표시와 epoch 증가는 publish 비교와 같은 lock에서 원자적으로 일어난다. ready S0에서 invalidate가 publish 비교보다 먼저 선형화하면 candidate S1은 발행되지 않고 S0를 유지한 채 worker가 다시 깨어난다. publish가 먼저 선형화하면 S1은 발행되고 뒤따른 invalidate의 dirty wake가 S2로 수렴한다. cold cache의 epoch mismatch는 첫 검색 가능한 S0를 발행하고 worker를 다시 깨운다.

MLP는 coverage warning을 `QueryOutcome`과 MCP에 배선한다. 성공 게이트는 capped old pack의 BM25 hit가 계속 비어 있고 missing warning이 있으며, all-covered partial state도 warning을 내고, malformed 또는 composite pack ID가 기존 `scope_pack_id()` 대조군과 같은 판정을 내는 것이다. 정확한 total capability가 없는 legacy fallback은 검색을 유지하고 unknown warning만 내며 complete 또는 partial을 단정한 경우가 없다.

MMP는 failure와 epoch 경합을 고정한다. 성공 게이트는 cold probe failure가 검색과 unknown warning을 함께 내고, ready probe failure가 기존 state와 warning을 보존하며, search 중 swap이 일어나도 한 세대의 hit와 warning만 사용한다. invalidate의 dirty 표시와 epoch 증가는 worker publish 비교와 같은 lock에서 원자적이다. ready build에서 invalidate가 publish 비교보다 먼저 선형화하면 candidate S1은 버려지고 S0를 유지한 채 re-wake한다. publish가 먼저 선형화하면 S1을 발행한 뒤 invalidate가 dirty wake로 S2를 예약한다. cold build 중 epoch 변경은 첫 S0를 발행하고 re-wake한다. 두 적대 시험은 이 경계를 분리한다. 기존 `_bm25_search()`와 `ensure_built()`는 각각 hit list와 index를 계속 반환하며, content fallback과 direct private caller가 같은 대조군을 통과한다.

각 테스트는 RED를 먼저 확인한다. 수정 뒤 표적 test와 ruff를 실행한다. 역변이는 gate recipe의 절차로 이 변경을 되돌린 뒤 신규 검출 테스트가 실패함을 보인다. 전체 suite는 실행하지 않는다.

## 설계 검증과 정지 조건

독립 설계 비평가는 이 문서를 source와 대조하고 첫 줄에 PASS 또는 FAIL을 쓴다. 검증자는 state identity, epoch lock, probe failure, legacy caller contract, warning generation consistency를 반박 우선으로 검사한다. 설계 검증과 구현 뒤 이중 적대 검증은 각각 최대 세 라운드다. 같은 차단이 재발하거나 세 번째 라운드가 FAIL이면 리드에게 쟁점, 양쪽 근거, 시도 결과와 추천안을 올리고 구현 또는 추가 수정을 멈춘다.

각 설계 검증 요청은 이 문서와 변경 대상만 제공한다. 판정, 차단 항목, 근거, 필요한 최소 수정만 요구한다. 검증자는 새 범위나 전체 suite를 요구하지 않는다. 이 출력 예산은 검증 결과를 비교 가능한 형태로 제한한다.

# OpenClaw heartbeat 정리 런북

이 런북은 이미 적재한 OpenClaw heartbeat 레코드를 네 SQLite 저장소에서 지우는 절차를 설명한다. 리드만 라이브 apply와 rollback을 수행한다.

## 범위와 선택 계약

도구는 다음 조건을 모두 만족하는 `evidence:turn:` source만 선택한다.

- `metadata.pack_id`가 문자열 `openclaw`다.
- 본문이 `Q: [OpenClaw heartbeat poll]` 또는 `Q: Read HEARTBEAT.md if it exists`로 시작한다.
- source, FTS, evidence-space `doc_nodes`, `Evidence` graph node, sqlite-vec row가 각각 정확히 하나다.
- graph, doc node, vector metadata의 `pack_id`도 문자열 `openclaw`다.

본문 중간에 heartbeat 단어가 있는 일반 대화는 선택하지 않는다. `metadata.source`는 대체 판정에 쓰지 않는다. `Document`, `Claim`, `Community`, foundation node는 삭제하지 않는다. 도구는 후보 source와 graph metadata의 `pack_id=openclaw`, `evidence|Evidence` registry node, 후보 endpoint의 registry edge를 함께 frozen manifest로 묶는다. apply는 그 정확한 후보 registry node와 named incident edge만 조건 삭제한다. 비후보 registry node와 edge, 후보에 닿지 않는 orphan edge는 삭제하지 않는다. registry report는 pre-existing orphan, expected-after orphan, apply 뒤 actual-after를 계속 기록한다.

## 사전 중지와 cold 상태

1. writer 서비스 셋과 reingest timer를 중지한다.
2. 네 DB의 열린 FD, migration journal, delete journal, rename journal을 확인한다.
3. `graph.db`, `doc_store.db`, `vectors.db`, `opencrab.db`의 WAL checkpoint를 실행한다.
4. 네 `-wal` 파일이 비어 있는지 확인한다.
5. registry candidate node와 incident edge가 frozen manifest의 source와 graph ownership, space, type, raw identity와 일치하는지 확인한다. 불일치하면 apply 전에 중단한다.

도구는 non-empty WAL, holder, 스키마 불일치, sqlite-vec 부재를 거부한다. 도구는 store constructor, MCP, 임베딩, source pack을 사용하지 않는다. registry write는 승인 후보의 조건 삭제만 수행한다.

## Dry-run과 승인

존재하지 않는 새 run path를 지정하고 dry-run을 실행한다. 도구가 그 directory를 만든다. 비어 있는 기존 directory도 지정하면 안 된다.

```bash
python scripts/cleanup_openclaw_heartbeat_493.py \
  --local-data-dir <cold-data-dir> \
  --run-dir <new-run-dir> \
  --vector-table <existing-vector-table>
```

dry-run은 `candidates.jsonl`을 먼저 쓰고, 오류가 없을 때만 frozen `manifest.json`을 마지막에 쓴다. 후보 파일에는 ID와 축별 digest만 있으며 source 본문, 경로, session metadata는 담지 않는다.

리드는 manifest의 digest와 ordered ID digest를 정확히 한 번 포함한 승인 파일을 만든다.

```json
{
  "manifest_sha256": "<manifest JSON digest>",
  "ordered_ids_sha256": "<manifest ordered ID digest>"
}
```

실행 직전에 source PR #283의 병합된 filter가 만든 제거 집합과 후보 ID 집합을 양방향 차집합 및 digest로 대사한다. 정상 대화 보존 집합도 기록하고 apply 뒤 전량이 남았는지 확인한다.

## Apply와 backup

첫 apply는 run directory 밖의 새 backup directory를 요구한다.

```bash
python scripts/cleanup_openclaw_heartbeat_493.py \
  --local-data-dir <cold-data-dir> \
  --run-dir <run-dir> \
  --vector-table <existing-vector-table> \
  --apply \
  --approved-manifest <approval.json> \
  --backup-dir <new-backup-dir>
```

도구는 SQLite backup API로 네 파일 사본을 만들고, mode `0600`, `integrity_check`, SHA-256, receipt directory fsync를 확인한다. receipt는 backup set ID, 네 파일 hash, manifest digest, canonical data directory identity를 묶는다. 첫 apply는 후보와 보호 집합이 frozen full snapshot과 일치할 때만 backup을 만든다. 재개 전에도 도구가 backup hash, integrity, canonical data directory identity를 다시 확인한다. 유효한 receipt가 있는 재개는 이미 삭제한 승인 registry node 또는 named incident edge만 target-absent로 허용한다. 변경, 추가, 다른 space tuple, source 또는 graph ownership 불일치는 모든 write 전에 거부한다.

도구는 후보마다 vector, graph와 incident edge, source와 FTS와 evidence doc, 승인 후보 registry node와 incident edge 순서로 삭제한다. 후보끼리 공유한 graph edge는 component target로 한 번만 삭제한다. registry incident edge는 frozen `evidence|Evidence` 후보 endpoint에 닿는 named target만 삭제한다. 반대쪽 endpoint가 비후보여도 named target이면 삭제한다. 다른 space의 같은 ID endpoint와 후보에 닿지 않는 orphan registry edge는 삭제하지 않는다. 각 축은 `BEGIN IMMEDIATE` 안에서 recorded raw value, owner, node, edge set을 다시 대조한 뒤 조건부 DELETE rowcount를 확인한다. 축 완료 기록은 매 줄 fsync한다. DB target state가 progress journal보다 우선한다.

`Claim`은 승인 후보 전체를 뺀 뒤에도 graph와 registry 각각에서 실제 비후보 `evidence|Evidence` support가 남아야 한다. 도구는 frozen과 현재 source metadata, graph metadata의 `pack_id=openclaw`을 다시 확인하고, frozen과 현재 registry Evidence raw identity도 대조한다. `claim|Claim` target만 affected Claim이다. 남는 근거가 없는 affected Claim이 하나라도 있으면 apply 전체를 거부한다. 후보와 무관하게 원래 support가 없는 Claim과 다른 space의 `Claim` target은 이 검사를 적용하지 않는다. 비후보 Evidence, Document, Claim과 비후보 edge의 digest가 바뀌면 중단한다.

## 보호 집합 표현과 확인 시점

manifest의 `protected`는 비후보 행 전체가 아니라 섹션별 (행 수, 순서 무관 합 해시)를 담는다. 섹션은 `nodes`(Document, Claim 그래프 노드), `evidence_graph`와 `evidence_graph_raw`(비후보 Evidence 노드의 정규화 항목과 원문 properties), `edges`(후보에 닿지 않는 그래프 edge), `evidence_doc`, `evidence_source`, `evidence_vector`(비후보 Evidence id의 doc_nodes, doc_sources, vector 행), `registry_nodes`, `registry_edges`이다. 비후보 행 하나가 바뀌거나 추가되거나 사라지면 해당 섹션이 달라져 apply가 쓰기 전에 거부한다. 보호 범위는 기존과 같다. 비Evidence 노드 id의 doc_nodes 행과 비후보 FTS 행은 보호 범위 밖이다. 합 해시는 사고와 비적대적 편집을 검출하는 용도이며 적대적 충돌에는 약하다.

`protected`의 `nodes`(Claim 항목), `evidence`(영향 Claim을 지지하는 비후보 Evidence 항목), `registry_nodes`(claim 공간 Claim), `evidence_ids`(동결한 비후보 Evidence id)는 Claim 판정과 인라인 검사의 입력이다. `evidence`는 manifest 본문이 아니라 run directory의 `evidence.jsonl`에 줄 단위로 두고 manifest에는 파일 이름, 항목 수, 전체 sha256만 적는다. apply는 줄마다 길이와 sha256을 고정해 다시 읽을 때 대조한다. 후보 레코드도 `candidates.jsonl`에서 같은 방식으로 읽는다.

apply는 네 DB마다 쓰기 연결을 유지하고 그 연결의 `PRAGMA data_version`으로 이 도구 밖의 commit을 감시한다. 가정: 네 DB에 쓰는 다른 주체는 SQLite 연결이다. 파일을 통째로 교체하면 감시가 보지 못한다. 변화가 없으면 후보 밖 상태가 상수이므로 후보 전체 대상 검사(보호 대조, Claim support, registry 보호 대조)는 첫 후보에서만 실행하고 결과를 재사용한다. 각 쓰기 트랜잭션은 `BEGIN IMMEDIATE` 직후 네 DB의 값을 다시 확인한다. 변화가 보이면 rollback하고, trigger를 다시 확인하고, 후보 전체 R/D/S를 확인하고, 위치 색인(FTS rowid와 registry 행 id)을 다시 만든 뒤 트랜잭션을 다시 시작한다. 전체 보호 digest 대조는 변화가 보인 축의 끝과 모든 후보 처리 뒤에 실행한다. apply는 DB에 trigger가 있으면 쓰기 전에 거부한다.

FTS 조회와 삭제는 `rowid`로 한다. 삭제는 같은 트랜잭션에서 읽은 `source_id`와 `text`를 조건에 다시 넣고 rowcount를 확인한다. vector 신원은 vec0 PK 열 `node_id`이다. metadata에 `node_id` 키가 있으면 후보 id와 같아야 하고 없으면 PK로 대신한다.

재현 명령: 성능과 메모리는 `docs/gate-recipes.md`의 격리 규칙 아래에서 후보 부분집합으로 재고, 보호 집합 경로는 읽기 전용 사본에 `_protection_sections`를 한 번 실행해 최대 RSS를 잰다. 시험은 `tests/test_cleanup_openclaw_heartbeat_493.py`의 `test_protected_row_change_is_refused_before_any_write`, `test_candidate_loop_issues_no_full_scan_statements`가 이 성질을 지킨다.

## Apply 뒤 대사

서비스를 시작하기 전에 네 primary 축의 후보 ID가 전부 제거됐는지 확인한다. 보호 집합과 정상 대화 보존 집합도 전량 확인한다. registry 잔존은 검색 결과 제거와 재적재 방지의 증거가 아니다. registry가 입력이 아님을 별도로 재확인한다.

네 DB에 `integrity_check`를 수행한다. heartbeat 질의 결과와 일반 대화 검색 표본을 독립적으로 확인한다. 이 확인이 끝난 뒤에만 writer 서비스와 timer를 시작한다.

## Rollback

rollback 전에 서비스를 다시 중지하고 holder와 cold 상태를 확인한다. 리드는 먼저 `--prepare-rollback --backup-dir <backup-dir>`를 실행해 final tombstone을 발행한다. 이 단계는 write lock, holder, receipt, manifest, backup set, 현재 data directory identity를 대조한다. 이 단계는 현재 DB file store-set identity를 다시 계산하지 않는다. 이후 `--rollback --backup-dir <backup-dir>`만 네 DB 복원을 시작한다.

rollback marker 상태는 네 가지다.

- `absent`: tombstone, completion, temporary marker가 없다.
- `incomplete artifact`: temporary marker, 빈 marker, JSON 또는 digest 오류 marker, tombstone 없는 completion, 이름 충돌, 읽기 실패가 있다.
- `complete tombstone`: 결합이 일치하는 tombstone만 있다.
- `complete tombstone plus completion`: 결합이 일치하는 tombstone과 completion이 있다.

apply는 `absent` 외 상태를 거부한다. rollback은 `complete tombstone`만 복원을 시작한다. `complete tombstone plus completion`이면 파일 복원 없이 receipt와 복원 대사만 다시 한다. incomplete artifact와 결합 불일치는 자동으로 고치지 않는다. 모든 marker는 증거로 보존한다.

tombstone과 completion은 temporary `O_CREAT|O_EXCL` 생성, 파일 fsync, Linux `renameat2(RENAME_NOREPLACE)`, run directory fsync, readback 순서로만 발행한다. final marker는 덮어쓰지 않는다.

rollback은 네 DB를 같은 backup set에서 함께 복원한다. 도구는 먼저 하나의 successor UUID directory와 그 successor marker를 fsync해 발행한다. 각 파일의 metadata와 replacement는 그 directory와 UUID에만 결합한다. WAL, SHM, journal sidecar를 제거할 때마다 data directory fsync를 수행한다. binding mismatch artifact는 보존하고 fail-closed로 처리한다. 중단 뒤 exact binding과 complete tombstone이 있으면 도구는 이전 artifact를 보존하고 새 unique attempt로 네 파일 전체 복원을 다시 시작한다. 네 DB integrity와 receipt hash가 일치한 뒤 completion marker를 발행한다. completion verification-only도 성공 전에 run directory fsync를 다시 수행한다. rollback 중에는 서비스를 시작하지 않는다.

## 금지와 회수

이 절차는 live apply 실행, 서비스 제어, rollback 실행을 자동으로 수행하지 않는다. source pack, MCP, 임베딩은 수정하지 않는다. 도구는 승인 후보 registry node와 named incident edge만 조건 삭제한다. 그 밖의 registry row는 수정하지 않는다. tombstone, manifest, approval, receipt, backup set은 보존한다. rollback 뒤 기존 approval은 다시 유효하지 않다.

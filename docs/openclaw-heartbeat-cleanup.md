# OpenClaw heartbeat 정리 런북

이 런북은 이미 적재한 OpenClaw heartbeat 레코드를 네 SQLite 저장소에서 지우는 절차를 설명한다. 리드만 라이브 apply와 rollback을 수행한다.

## 범위와 선택 계약

도구는 다음 조건을 모두 만족하는 `evidence:turn:` source만 선택한다.

- `metadata.pack_id`가 문자열 `openclaw`다.
- 본문이 `Q: [OpenClaw heartbeat poll]` 또는 `Q: Read HEARTBEAT.md if it exists`로 시작한다.
- source, FTS, evidence-space `doc_nodes`, `Evidence` graph node, sqlite-vec row가 각각 정확히 하나다.
- graph, doc node, vector metadata의 `pack_id`도 문자열 `openclaw`다.

본문 중간에 heartbeat 단어가 있는 일반 대화는 선택하지 않는다. `metadata.source`는 대체 판정에 쓰지 않는다. `Document`, `Claim`, `Community`, foundation node는 삭제하지 않는다. `opencrab.db`의 registry는 후보 node와 edge, pre-existing orphan, expected-after orphan, apply 뒤 actual-after를 읽기 전용 report로 기록한다. 이 도구는 registry를 삭제하거나 수정하지 않는다.

## 사전 중지와 cold 상태

1. writer 서비스 셋과 reingest timer를 중지한다.
2. 네 DB의 열린 FD, migration journal, delete journal, rename journal을 확인한다.
3. `graph.db`, `doc_store.db`, `vectors.db`, `opencrab.db`의 WAL checkpoint를 실행한다.
4. 네 `-wal` 파일이 비어 있는지 확인한다.
5. registry가 ontology 검색이나 재적재 흐름의 입력인지 독립적으로 확인한다. 입력이면 apply 전에 중단한다.

도구는 non-empty WAL, holder, 스키마 불일치, sqlite-vec 부재를 거부한다. 도구는 store constructor, MCP, 임베딩, source pack, registry 쓰기를 사용하지 않는다.

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

도구는 SQLite backup API로 네 파일 사본을 만들고, mode `0600`, `integrity_check`, SHA-256, receipt directory fsync를 확인한다. receipt는 backup set ID, 네 파일 hash, manifest digest, canonical data directory identity를 묶는다. 첫 apply는 후보와 보호 집합이 frozen full snapshot과 일치할 때만 backup을 만든다. 재개 전에도 도구가 backup hash, integrity, canonical data directory identity를 다시 확인한다.

도구는 후보마다 vector, graph와 incident edge, source와 FTS와 evidence doc 순서로 삭제한다. 후보끼리 공유한 graph edge는 component target로 한 번만 삭제한다. 각 축은 `BEGIN IMMEDIATE` 안에서 recorded raw value, owner, node, edge set을 다시 대조한 뒤 조건부 DELETE rowcount를 확인한다. 축 완료 기록은 매 줄 fsync한다. DB target state가 progress journal보다 우선한다.

`Claim`은 승인 후보 전체를 뺀 뒤에도 `supports`가 남아야 한다. 남는 근거가 없는 Claim이 하나라도 있으면 apply 전체를 거부한다. 비후보 Evidence, Document, Claim과 비후보 edge의 digest가 바뀌면 중단한다.

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

이 절차는 live apply 실행, 서비스 제어, rollback 실행을 자동으로 수행하지 않는다. source pack, MCP, 임베딩, registry row는 수정하지 않는다. tombstone, manifest, approval, receipt, backup set은 보존한다. rollback 뒤 기존 approval은 다시 유효하지 않다.

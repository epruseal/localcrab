# 버전별 마이그레이션 절차

이 문서는 패키지 버전과 저장소 배치의 대응, 버전 규칙, 배치를 바꾸는 이관 절차의 색인이다. 이관 절차는 모두 같은 틀(사전 조건, 정지 범위, 명령, 검증, 롤백)로 적는다. 값(건수, 크기, 소요 시간)은 적지 않는다. 값은 읽는 사람이 각 명령으로 지금 얻는다.

## 목차

1. [버전 규칙](#1-버전-규칙)
2. [버전과 저장소 배치](#2-버전과-저장소-배치)
3. [이관 절차 색인](#3-이관-절차-색인)
4. [0.1.0에서 1.0.0으로 올리기](#4-010에서-100으로-올리기)
5. [다섯 파일에서 단일 파일로](#5-다섯-파일에서-단일-파일로)
6. [Chroma에서 sqlite-vec로](#6-chroma에서-sqlite-vec로)
7. [SQLite에서 PostgreSQL로](#7-sqlite에서-postgresql로)
8. [docker 모드에서 local 모드로](#8-docker-모드에서-local-모드로)
9. [그 밖의 일회성 스크립트](#9-그-밖의-일회성-스크립트)
10. [배포 절차와 editable 설치](#10-배포-절차와-editable-설치)

## 1. 버전 규칙

이 저장소의 버전은 [Semantic Versioning 2.0.0](https://semver.org/)을 따른다. 버전은 `MAJOR.MINOR.PATCH`다.

공개 API는 아래 다섯이다. 여기에 속하지 않는 내부 함수와 모듈 구조는 공개 API가 아니므로 버전을 올리지 않고 바꾼다.

| 범위 | 공개 API에 속하는 것 |
|---|---|
| MCP 도구 | 도구 이름, 인자 이름과 형태, 반환 필드, 오류 형태 |
| REST 경로 | 경로, 메서드, 요청과 응답 필드, 인증 규칙 |
| CLI 명령 | `opencrab` 명령과 하위 명령, 옵션 이름, 종료 코드 계약 |
| 설정 키 | 환경변수 이름과 의미, 기본값 |
| 저장소 배치 | 데이터 디렉터리와 PostgreSQL의 파일, 표, 열 구성과 그 의미(2절의 대응표) |

| 올리는 자리 | 기준 |
|---|---|
| MAJOR | 공개 API를 호환되지 않게 바꾼다. 이관 없이는 읽을 수 없게 저장소 배치를 바꾼다 |
| MINOR | 호환되는 기능을 더한다. 기본이 꺼진 새 배치를 더하는 것도 여기에 든다 |
| PATCH | 호환되는 결함 수정만 한다 |

버전을 정하는 변경은 아래 위치를 한 번에 맞춘다. 맞는지는 이 명령으로 센다.

```bash
grep -rnI --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=tests -e '"version"' -e '^version' -e '__version__' -e 'version=' -e 'mcp_server_version' pyproject.toml opencrab/__init__.py opencrab/config.py apps/api/main.py packaging/agent-plugin/src/plugin.json
```

위 다섯 파일의 값이 모두 같아야 한다. `scripts/localcrab`과 `scripts/localcrab-harness`의 `MCP_SERVER_VERSION` 기본값(`<버전>-localcrab`)도 같은 버전으로 맞춘다. `uv.lock`의 `opencrab` 항목, `.env.example`과 `opencrab/cli.py`의 `MCP_SERVER_VERSION` 예시도 같은 값으로 맞춘다. 별도 패키지(`crabharness/pyproject.toml`, `crabharness/package.json`, `crabharness/worker_runtime/package.json`, `apps/web/package.json`)는 자기 버전을 따로 갖는다. 태그는 `v<버전>` 형식(예: `v1.0.0`)으로 릴리스할 커밋에 붙인다(`docs/agent-plugin-release-policy.md`의 수동 릴리스 절차가 태그를 릴리스할 커밋에 붙이라고 정한다).

## 2. 버전과 저장소 배치

| 패키지 버전 | 저장소 배치 | 선택 설정 |
|---|---|---|
| 0.1.0 | 다섯 파일 배치(`STORAGE_MODE=local`): `graph.db`, `doc_store.db`, `vectors.db`, `opencrab.db`, `billing.db`와 `write.lock`. 벡터가 Chroma이면 `chroma/` 디렉터리가 `vectors.db`를 대신한다. PostgreSQL 통합(`STORAGE_MODE=pg`)과 docker 혼합(`STORAGE_MODE=docker`)도 같은 버전의 배치다 | `VECTOR_BACKEND`, `STORAGE_MODE` |
| 1.0.0 | 위 배치 전부와 단일 파일 배치. 단일 파일 배치는 root 디렉터리 하나에 `single.db`와 `billing.db`가 있다. 기본은 꺼짐이다 | `SINGLE_SQLITE_FILE`(root 안 `single.db`의 절대 경로) |

0.1.0 표기 아래에는 여러 커밋이 있었다. 이 표는 배치의 종류만 가른다. 같은 0.1.0 안의 개별 변경(예: 등록부 복구, 백필)은 각 이슈 문서가 다룬다.

### 단일 root와 다섯 파일 디렉터리를 가르는 법

패키지 버전은 어느 데이터 파일에도 기록되지 않는다. `manifest.json`의 `version` 필드는 단일 root 형식의 버전이고 패키지 버전이 아니다. 그래서 데이터 디렉터리가 어느 배치인지는 파일 이름으로 가른다.

| 디렉터리에 있는 것 | 배치 |
|---|---|
| `ledger.json`, `manifest.json`, `marker.json`, `single.db` 넷 | 단일 root. `ledger.json`의 `state`가 `authorized`일 때만 쓰기를 열 수 있다 |
| `graph.db`, `doc_store.db`, `opencrab.db`가 있고 위 넷이 없음 | 다섯 파일 배치 |
| `chroma/` 디렉터리 | 벡터가 Chroma인 다섯 파일 배치 |

단일 root를 가리키는데 `SINGLE_SQLITE_FILE`이 비어 있는 프로세스는 root를 바꾸기 전에 거부된다(`docs/issue-671-single-db-conversion.md` 7.2의 7번). 반대로 다섯 파일 디렉터리에 `SINGLE_SQLITE_FILE`을 설정하면 결속 검증에서 거부된다.

## 3. 이관 절차 색인

| 절차 | 바꾸는 것 | 절 |
|---|---|---|
| 0.1.0에서 1.0.0 | 코드와 설치 메타데이터. 배치는 그대로 | 4 |
| 다섯 파일에서 단일 파일 | 데이터 디렉터리를 단일 root로 | 5 |
| Chroma에서 sqlite-vec | 벡터 저장소 | 6 |
| SQLite에서 PostgreSQL | 문서, SQL, 벡터 저장소(그래프는 아직 아님) | 7 |
| docker 모드에서 local 모드 | 외부 서비스의 데이터를 로컬 SQLite로 | 8 |

모든 절차는 이 틀을 따른다. 틀에 없는 항목은 각 절에서 따로 적는다.

- 사전 조건: 시작 전에 참이어야 하는 것.
- 정지 범위: 멈춰야 하는 프로세스. 멈추지 않아도 되는 것은 그렇게 적는다.
- 명령: 한 줄에 단순 명령 하나.
- 검증: 이관이 끝났다는 증거를 얻는 방법. 모든 절차의 읽기 검증은 `scripts/check_read_endpoints.py`를 같이 쓴다.
- 롤백: 되돌리는 방법과 되돌릴 수 없는 시점.

읽기 검증 도구의 사용법은 다음과 같다. 토큰은 환경변수나 클라이언트 설정 파일에서만 읽고 출력하지 않는다. 쓰기 호출은 보내지 않는다(`ontology_query`는 billing 행을 쓴다).

```bash
python scripts/check_read_endpoints.py <포트> mcp --token-env <토큰 변수> --save-expected <기준 파일>
python scripts/check_read_endpoints.py <포트> mcp --token-env <토큰 변수> --expected <기준 파일>
```

첫 명령은 이관 전 인스턴스의 팩 목록을 기준 파일로 저장한다. 둘째 명령은 이관 뒤 인스턴스에서 `RESULT: PASS`와 `expected_minus_got: []`를 확인한다. REST 앱은 `mcp` 대신 `api`를 쓴다.

## 4. 0.1.0에서 1.0.0으로 올리기

저장소 배치가 바뀌지 않는 코드 상향이다. 단일 파일 배치는 기본이 꺼져 있으므로 `SINGLE_SQLITE_FILE`을 설정하지 않으면 동작이 이전과 같다.

- 사전 조건: 데이터 백업이 있다(`python -m opencrab.stores.backup --to <백업 경로>`와 `--verify`). 올릴 커밋(`v1.0.0` 태그)을 가져와 둔다.
- 정지 범위: 코드를 바꾸는 동안 서비스를 멈춘다. 서비스 매니저가 관리하는 프로세스는 매니저로 멈춘다.

```bash
git fetch origin --tags
git merge --ff-only v1.0.0
python -m pip install -e . --no-deps
python -m pip show opencrab
python -c "import opencrab; print(opencrab.__version__)"
opencrab --version
```

- 검증: `pip show`의 `Version`, `opencrab.__version__`, `opencrab --version` 셋이 모두 1.0.0이다. 서비스를 시작해 `opencrab status`가 모든 저장소 `OK`를 낸다. 읽기 검증 도구가 `RESULT: PASS`를 낸다. editable 설치를 쓰면 `pip install -e .`로 재설치하기 전까지 설치 메타데이터의 버전이 옛 값으로 남는다(10절).
- 롤백: 서비스를 멈추고 `git reset --keep <이전 커밋>`으로 코드를 되돌린 뒤 같은 재설치 명령을 다시 실행한다. 이 절차는 데이터를 바꾸지 않으므로 데이터 롤백은 없다.
- 이 상향 뒤에 단일 파일 배치로 옮기려면 5절을 따른다. 선택 사항이다.

## 5. 다섯 파일에서 단일 파일로

운영 데이터 디렉터리를 root 하나로 바꾼다. 실행표 전체는 `docs/issue-671-single-db-conversion.md` 8.9절에 있다. 아래는 이 색인의 틀에 맞춘 요약이다.

- 사전 조건: 서비스 매니저로 모든 쓰기 주체를 멈출 수 있다. 데이터 디렉터리와 그 부모가 같은 파일시스템이고 마운트 지점이 아니다. 디스크 여유가 다섯 DB 크기 합의 5.3배에 30 GiB를 더한 값 이상이다. 독립 검증 PASS와 사본 시험 PASS 두 파일이 있다(8.8절). 병합이 쓰는 임시 디렉터리가 tmpfs가 아니다. 전환 전에 운영 데이터의 일관된 사본으로 같은 표를 끝까지 한 번 돌려 사본 시험 PASS를 만든다.
- 정지 범위: 데이터 디렉터리를 여는 모든 서비스와 재인입 타이머. 정지부터 쓰기 개방까지다. 읽기 질의도 이 구간에는 받지 못한다.
- 명령: 8.9절의 0단계부터 9단계. 보조 도구는 `scripts/unpack_backup_set.py`(백업 세트에서 DB 다섯만 푼다), `scripts/make_publication_evidence.py`(게시 증거 파일), `scripts/check_read_endpoints.py`(읽기 검증)다. 병합과 게시는 `scripts/merge_sqlite_stores.py`, 쓰기 개방 전 승인은 같은 도구의 `authorize` 하위 명령이다.
- 검증: 3단계 `--verify` 출력의 행 수 일치와 FTS, 벡터 조회 일치, 4단계 `authorize applied` 줄의 해시가 PASS 해시와 같음, 6단계 임시 인스턴스의 `RESULT: PASS`, 9단계 개방 직후 `RESULT: PASS`.
- 롤백: 쓰기 개방 전에는 8.9절의 갈래 B로 이름을 되돌린다. 쓰기 개방 뒤에는 갈래 D로 롤백 지점 세트(`python -m opencrab.stores.single_restore`)나 다섯 파일 복귀를 고른다. 개방 뒤의 쓰기는 다섯 파일 복귀에서 사라진다.

## 6. Chroma에서 sqlite-vec로

벡터를 Chroma에서 sqlite-vec(`vectors.db`)로 옮긴다. 재임베딩하지 않고 저장된 벡터를 그대로 복사한다. 스크립트는 Chroma 쪽을 읽기만 하고 `vectors.db`만 쓴다.

- 사전 조건: 로컬 Chroma(`<LOCAL_DATA_DIR>/chroma`)가 있다. 백업이 있다. 대상 `vectors.db`가 없거나 비어 있다(비어 있지 않으면 `--force` 없이는 거부한다). 임베딩 차원이 `EMBED_DIM`과 같다.
- 정지 범위: 로컬 Chroma를 연 프로세스를 모두 멈춘다(MCP 서버, REST 앱, 벡터를 쓰는 CLI 명령). 스크립트는 `chroma.lock`을 쥔다.

```bash
python -m opencrab.stores.backup --to <백업 경로>
python scripts/migrate_chroma_to_sqlite_vec.py --dry-run
python scripts/migrate_chroma_to_sqlite_vec.py
```

- 옵션: `--batch <건수>`는 한 번에 복사하는 건수이고 `--force`는 기존 `vectors.db`를 덮는다.
- 검증: 스크립트가 `RESULT: PASS`를 낸다. 출력의 `chroma vectors` 건수와 `vec0 count`가 같다. 서비스를 `EMBEDDING_BACKEND=openai VECTOR_BACKEND=sqlite-vec`로 시작하고 읽기 검증 도구가 `RESULT: PASS`를 낸다.
- 롤백: `VECTOR_BACKEND=chroma`로 되돌리고 서비스를 다시 시작한다. Chroma 디렉터리는 지워지지 않았으므로 즉시 복귀한다. 이관 뒤 sqlite-vec에만 쓴 데이터는 Chroma에 없다.
- 이관 뒤 정리: 롤백 기간이 끝나기 전에 `chroma/`를 지우지 않는다.

## 7. SQLite에서 PostgreSQL로

`STORAGE_MODE=pg`로 가기 위해 로컬 SQLite의 데이터를 PostgreSQL로 복사한다. 스크립트는 원본 SQLite에 쓰는 문을 보내지 않는다. 연결을 읽기 전용으로 열지는 않으므로 WAL 모드 파일에는 곁파일(`-wal`, `-shm`)이 생길 수 있다.

- 한계: 그래프 저장소(`graph.db`)는 이 스크립트로 옮기지 못한다. 그래프 적용은 자격 검증 전까지 거부되고(`graph apply is fixture-only`) `--dry-run`만 된다. 지원하는 범위는 `--only doc,sql,vector`다. 그래프를 옮겨야 하는 운영에는 이 절차를 쓰지 않는다.
- 사전 조건: 새 PostgreSQL 데이터베이스(pgvector 확장을 쓸 수 있다)와 그 접속 문자열을 담은 `POSTGRES_URL` 설정. `pip install ".[pg]"`. 백업이 있다. 대상에 소스가 모르는 `users`와 `api_tokens` 행이 있으면 거부된다(`--allow-target-only-auth`로 허용). 소스에 이 이관이 복사하지 않는 SQLStore 표가 있으면 거부된다(`--allow-unmigrated`로 허용하고 요약에 제외 목록이 나온다).
- 정지 범위: 이관 중 원본 SQLite에 쓰는 프로세스를 멈춘다. 복사 도중 원본이 바뀌면 사본이 일관되지 않으므로 쓰기 주체를 멈추는 것이 전제다.

```bash
pip install ".[pg]"
python scripts/migrate_sqlite_to_pg.py --only doc,sql,vector --dry-run
python scripts/migrate_sqlite_to_pg.py --only doc,sql,vector --verify
```

- 옵션: `--data-dir`와 `--graph-db`, `--doc-db`, `--sql-db`, `--vector-db`는 원본 경로를 바꾼다. `--pg-schema`는 그래프와 문서 대상 스키마다. 접속 문자열은 `POSTGRES_URL` 설정(환경변수나 환경 파일)에서 읽는다. `--pg-url`로 명령줄에 쓰면 프로세스 목록에 노출되므로 쓰지 않는다. `--vector-src-table`과 `--vector-dst-table`은 벡터 표 이름이다. `--batch`와 `--limit`은 복사 단위와 시험용 상한이다.
- 검증: `--verify`가 표마다 `source`와 `pg` 건수를 비교하고(자연 키가 있는 SQL 표는 `missing_keys`도 낸다) `RESULT: PASS`를 낸다. 다시 돌리는 동작은 표마다 다르다(스크립트 docstring의 IDEMPOTENCY 절이 정본이다). 자연 키가 있는 SQL 표는 건수가 같아도 건너뛰지 않고 매번 모든 행을 upsert한다. 그래서 서비스가 PostgreSQL에 쓴 뒤에 다시 돌리면 대상 행이 원본 값으로 덮인다. 서비스가 PostgreSQL을 쓰기 시작한 뒤에는 이 스크립트를 다시 돌리지 않는다. `doc_nodes`, `audit_log`, 벡터는 건수가 같고 0보다 크면 건너뛰고, `doc_sources`는 건너뛰지 않는다. 서비스를 `STORAGE_MODE=pg POSTGRES_URL=<접속 문자열>`로 시작하고 읽기 검증 도구가 `RESULT: PASS`를 낸다.
- 롤백: `STORAGE_MODE=local`로 되돌리고 서비스를 다시 시작한다. 원본 SQLite 파일은 바뀌지 않았다. PostgreSQL에만 쓴 데이터는 SQLite에 없다. 대상 데이터베이스는 필요하면 통째로 지운다.

## 8. docker 모드에서 local 모드로

외부 서비스(Neo4j, MongoDB, Chroma HTTP, PostgreSQL)의 데이터를 로컬 SQLite와 Chroma로 옮긴다. 이 절차는 합성 데이터로 끝까지 돌리지 못했다. 네 서비스를 모두 띄울 수 없어서다. 명령과 인자는 `--help`와 코드로 대조했다.

- 사전 조건: 네 서비스에 접속할 수 있다. `--dry-run`도 원본 서비스에 접속해 건수를 읽으므로 서비스가 닫혀 있으면 `마이그레이션을 중단합니다. 소스 서비스를 확인하세요.`로 끝난다. 로컬 데이터 디렉터리가 비어 있거나 이관 스크립트가 백업할 수 있다.
- 정지 범위: 원본 서비스에 쓰는 프로세스를 멈춘다.

```bash
python scripts/migrate_to_local.py --dry-run
python scripts/migrate_to_local.py
```

- 옵션: `--skip-graph`, `--skip-docs`, `--skip-vectors`, `--skip-sql`은 해당 저장소를 건너뛴다. `--batch-size`, `--local-data-dir`, 접속 인자(`--neo4j-uri`, `--neo4j-user`, `--neo4j-pass`, `--mongo-uri`, `--mongo-db`, `--chroma-host`, `--chroma-port`, `--chroma-collection`, `--pg-url`)가 있다. `--allow-target-only-auth`와 `--allow-unmigrated`는 7절과 같은 뜻이다.
- 검증: `--dry-run`이 각 저장소 건수를 출력한다. 이관 뒤 읽기 검증 도구가 `RESULT: PASS`를 낸다.
- 롤백: 원본 서비스의 데이터는 바뀌지 않는다. 기존 로컬 DB 파일은 이관 전에 백업 세트로 복사된다. `STORAGE_MODE=docker`로 되돌린다.

## 9. 그 밖의 일회성 스크립트

배치를 바꾸지 않는 데이터 보정 스크립트다. 모두 먼저 `--help`로 인자를 읽고 `--dry-run`이나 `--apply` 없는 기본 모드로 계획을 본다.

| 스크립트 | 용도 |
|---|---|
| `scripts/migrate_pack_ownership.py` | 팩 등록부가 없던 시기의 데이터를 부트스트랩 소유자에게 귀속한다 |
| `scripts/migrate_graph_identity.py` | SQL 그래프를 전역 노드 식별 방식으로 계획하고 적용한다 |
| `scripts/migrate_add_binary_quantization.py` | 기존 vec0 표에 binary 양자화 벡터를 채운다 |
| `scripts/migrate_graph_to_ladybug.py` | `graph.db`를 Ladybug로 옮긴다(적용은 자격 검증 전까지 fixture 전용이다) |
| `scripts/backfill_space.py` | 벡터와 문서 소스 기록에 `space`를 채운다 |
| `scripts/reconcile_doc_graph_nodes.py` | 그래프와 문서 저장소의 노드 키 집합을 진단하고 보정한다 |

## 10. 배포 절차와 editable 설치

editable 설치(`pip install -e .`)를 쓰는 환경에서는 코드가 체크아웃을 직접 가리킨다. 체크아웃을 새 커밋으로 옮기면 소스의 `opencrab.__version__`은 바로 바뀐다. 설치 메타데이터(`pip show opencrab`의 `Version`과 `opencrab --version`의 출력)는 재설치 전까지 옛 값으로 남는다. 서비스 단위나 모니터링이 이 값을 읽으면 어긋난다.

배포 뒤 아래 세 값이 같은지 확인한다.

```bash
python -m pip show opencrab
python -c "import opencrab; print(opencrab.__version__)"
opencrab --version
```

다르면 재설치한다.

```bash
python -m pip install -e <체크아웃 경로> --no-deps
```

`--no-deps`는 의존성을 건드리지 않고 메타데이터만 새로 쓴다. 의존성이 바뀐 배포는 `--no-deps` 없이 설치한다. 재설치는 서비스를 멈춘 구간에 한다.

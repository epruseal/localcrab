"""§9.1 환경 계약: opencrab 런타임(opencrab/ + apps/, tests/·scripts/ 제외)이
실제로 읽는 환경변수 전수 목록.

이 딕셔너리는 살아있는 문서가 아니라 이 패키징 도구의 검증 대상 데이터다.
tests/ 의 AST 기반 동기화 가드가 opencrab/+apps/ 를 재스캔해 이 집합과
양방향 동치(코드에만 있는 이름도, 문서에만 있는 이름도 실패)를 assert 한다.
새 환경변수가 코드에 추가되면 이 딕셔너리를 갱신할 때까지 그 가드가 실패한다
(의도된 성질 -- 문서-코드 드리프트를 원천 차단한다).

재현 명령(수치는 커밋마다 바뀌므로 이 파일에는 박지 않는다. 필요하면 직접 실행):
    .venv/bin/python -c "
    import ast, os
    # tests/test_agent_plugin_packaging.py 의 AST 추출기 참고 (동일 로직)
    "
    (구현은 tests/test_agent_plugin_packaging.py 의 env 동기화 가드를 참조)

분류 어휘: "설정 소스 선택" | "외부 전송 결정" | "상태 위치" | "기동 거부" | "튜너블"
| "상태 생성 opt-in"
(+ ", serve 비도달" 접미 -- opencrab serve 가 실제로 띄우는 stdio MCP 서버
프로세스의 코드 경로(opencrab/mcp/**)가 임포트하지 않는 모듈에서만 읽히는
이름. apps/ 의 FastAPI 전용 이름, opencrab/pack/build.py·jsonl_io.py 처럼
opencrab/mcp/tools/pack.py 가 임포트하지 않는 팩 저작 라이브러리 경로,
opencrab extract 같은 별도 CLI 커맨드 전용 이름이 여기 해당한다.
실측(2026-08-31 워크트리, import-chain grep 로 교차 확인):
    grep -rln "pack[.]build|jsonl_io" opencrab/mcp opencrab/services opencrab/ontology
    -> 무매치 (PACK_OUT_ROOT/PACK_LIB_STRICT/JSONL_SHARD_LIMIT 는 serve 비도달)
"""

from __future__ import annotations

ENV_CONTRACT: dict[str, str] = {
    # --- opencrab/config.py Settings alias ---
    "STORAGE_MODE": "설정 소스 선택",
    "LOCAL_DATA_DIR": "상태 위치",
    "NEO4J_URI": "외부 전송 결정",
    "NEO4J_USER": "외부 전송 결정",
    "NEO4J_PASSWORD": "외부 전송 결정",
    "NEO4J_DATABASE": "외부 전송 결정",
    "MONGODB_URI": "외부 전송 결정",
    "MONGODB_DB": "외부 전송 결정",
    "POSTGRES_URL": "외부 전송 결정",
    "CHROMA_HOST": "외부 전송 결정",
    "CHROMA_PORT": "외부 전송 결정",
    "CHROMA_COLLECTION": "튜너블",
    "EMBEDDING_BACKEND": "외부 전송 결정",
    "OPENAI_API_BASE": "외부 전송 결정",
    "OPENAI_EMBED_MODEL": "튜너블",
    "OPENAI_API_KEY": "외부 전송 결정",
    "EMBED_DIM": "튜너블",
    "OPENAI_TIMEOUT": "튜너블",
    "CHROMA_LOCK_TIMEOUT": "튜너블",
    # #69: file_lock()/acquire_file_lock() 의 timeout 생략 기본값. write.lock
    # 을 포함해 opencrab/mcp/tools 의 모든 쓰기 도구가 거치는 _write_lock() 이
    # 이 경로를 타므로 serve 도달 경로다.
    "WRITE_LOCK_TIMEOUT": "튜너블",
    "EMBED_COLLECTION": "튜너블",
    "LOCAL_GGUF_PATH": "상태 위치",
    "VECTOR_BACKEND": "설정 소스 선택",
    "VECTOR_DB_FILE": "상태 위치",
    "VECTOR_COLLECTION": "튜너블",
    "VECTOR_ANN": "튜너블",
    "VECTOR_ANN_COARSE_K": "튜너블",
    "PG_EF_SEARCH": "튜너블",
    "MCP_SERVER_NAME": "튜너블",
    "MCP_SERVER_VERSION": "튜너블",
    # opencrab serve --transport http 전용 경로에서만 읽힌다(기본 stdio 는 미도달).
    # 이 패키지의 mcp.json 은 stdio 만 구성하므로 이 프로세스 경로에서는 읽히지
    # 않지만, apps/ 전용은 아니라서(같은 opencrab serve 커맨드의 다른 분기이므로)
    # "serve 비도달" 접미는 붙이지 않고 튜너블로만 남긴다.
    "MCP_HTTP_HOST": "튜너블",
    "MCP_HTTP_PORT": "튜너블",
    # #136(#243): dual-era 프로토콜 게이트. 값이 malformed 면(미지 버전,
    # bare-origin 이 아닌 항목, 빈 항목) 기동을 거부하는 튜너블이다.
    # MCP_PROTOCOL_VERSIONS 는 MCPServer.__init__(stdio 포함) 경로에서,
    # MCP_ALLOWED_ORIGINS 는 http transport 조립(install_mcp_origin_guard)
    # 에서만 읽히지만 MCP_HTTP_HOST 와 같은 이유(같은 serve 커맨드의 다른
    # 분기)로 "serve 비도달" 접미는 붙이지 않는다.
    "MCP_PROTOCOL_VERSIONS": "튜너블",
    "MCP_ALLOWED_ORIGINS": "튜너블",
    "LOG_LEVEL": "튜너블",
    # --- 직접 읽기 (alias 밖, opencrab/·apps/ AST 스캔으로 발견) ---
    # opencrab extract CLI 커맨드 전용(cli.py:584,614). opencrab serve 의 도구
    # 실행 경로(opencrab/mcp/**)는 이 커맨드 함수를 호출하지 않는다.
    "ANTHROPIC_API_KEY": "외부 전송 결정, serve 비도달",
    "LOCALCRAB_ENV_FILE": "설정 소스 선택",
    # #245: stdio 최초 기동 자동 부트스트랩 opt-in (opencrab/auth.py 가
    # os.environ.get 리터럴로 읽는다). "1" 이면 빈 데이터 루트에서 로컬 유저와
    # 빈 스토어를 생성하고, "1"/"0"/빈 값 외의 malformed 값은 기동을 거부한다.
    # 스토어 생성 여부(상태 생성)를 좌우하므로 전용 분류를 둔다.
    "OPENCRAB_BOOTSTRAP_ON_EMPTY": "상태 생성 opt-in",
    "PORT": "튜너블, serve 비도달",
    "OPENCRAB_TIER": "설정 소스 선택, serve 비도달",
    "OPENCRAB_CORS_ORIGINS": "튜너블, serve 비도달",
    "OPENCRAB_BM25_NODE_LIMIT": "튜너블",
    "OPENCRAB_BM25_DEBOUNCE": "튜너블",
    "OPENCRAB_AUTO_PACK_MIN_SCORE": "튜너블",
    # opencrab/pack/jsonl_io.py 전용. opencrab/mcp/tools/pack.py 는 이 모듈을
    # 임포트하지 않는다(위 재현 명령으로 확인) -- 팩 CLI 저작 경로에서만 읽힌다.
    "JSONL_SHARD_LIMIT": "튜너블, serve 비도달",
    "EMBED_WINDOW_CHARS": "튜너블",
    # opencrab/pack/build.py 전용(Pack.__init__/validate). opencrab/mcp/tools/pack.py
    # 는 opencrab.pack.build 를 임포트하지 않는다(위 재현 명령으로 확인).
    "PACK_OUT_ROOT": "튜너블, serve 비도달",
    "PACK_LIB_STRICT": "튜너블, serve 비도달",
    # #128: 백업이 write.lock 과 경합 소스를 기다리는 상한(초). 값은 대기 시간만
    # 바꾸고 외부 전송 대상이나 상태 위치를 바꾸지 않는다. opencrab/stores/backup.py
    # 에서만 읽히고 그 모듈은 serve 경로가 임포트하지 않는다(재현:
    # `grep -rln "stores[.]backup" opencrab/mcp opencrab/services opencrab/ontology`
    # 무매치).
    "OPENCRAB_BACKUP_LOCK_TIMEOUT": "튜너블, serve 비도달",
    # --- 간접 접근(auth.py 튜플 순회, AST 정적 해석 불가 -- INDIRECT_ENV_ACCESS 참조) ---
    "OPENCRAB_API_KEY": "기동 거부",
    "LOCALCRAB_MCP_TOKEN": "기동 거부",
    "LOCALCRAB_MCP_TOKEN_FILE": "기동 거부",
}

INDIRECT_ENV_ACCESS: dict[str, str] = {
    "OPENCRAB_API_KEY": (
        "opencrab/auth.py:47 의 _STALE_SECRET_ENV_VARS 튜플 리터럴을 "
        "opencrab/auth.py:62 의 list comprehension"
        "(`[name for name in _STALE_SECRET_ENV_VARS if os.environ.get(name, ...).strip()]`)"
        "이 순회하며 os.environ.get(name) 을 호출한다. AST 추출기는 comprehension "
        "루프 변수의 iterable 원본을 값 자체로 해석하지 않으므로(Name 참조까지는 "
        "따라가지만 그 결과를 다시 함수 인자 자리에 대입하는 재귀 해석은 하지 않음) "
        "문자열 리터럴로 완전히 해석되지 않는다."
    ),
    "LOCALCRAB_MCP_TOKEN": (
        "opencrab/auth.py:47 _STALE_SECRET_ENV_VARS 튜플 순회, 근거는 "
        "OPENCRAB_API_KEY 항목과 동일(opencrab/auth.py:62)."
    ),
    "LOCALCRAB_MCP_TOKEN_FILE": (
        "opencrab/auth.py:47 _STALE_SECRET_ENV_VARS 튜플 순회, 근거는 "
        "OPENCRAB_API_KEY 항목과 동일(opencrab/auth.py:62)."
    ),
}

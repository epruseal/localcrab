"""
OpenCrab configuration via Pydantic Settings.

All values can be overridden via environment variables or a .env file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Storage mode: "local" (no Docker) or "docker" (full services)
    # ------------------------------------------------------------------
    storage_mode: Literal["local", "docker", "kuzu"] = Field(
        default="local", alias="STORAGE_MODE"
    )
    local_data_dir: str = Field(default="/home/asdf/.openclaw/workspace/data/localcrab", alias="LOCAL_DATA_DIR")

    # ------------------------------------------------------------------
    # Neo4j (docker mode only)
    # ------------------------------------------------------------------
    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(default="opencrab", alias="NEO4J_PASSWORD")
    neo4j_database: str | None = Field(default=None, alias="NEO4J_DATABASE")

    # ------------------------------------------------------------------
    # MongoDB (docker mode only)
    # ------------------------------------------------------------------
    mongodb_uri: str = Field(
        default="mongodb://root:opencrab@localhost:27017",
        alias="MONGODB_URI",
    )
    mongodb_db: str = Field(default="opencrab", alias="MONGODB_DB")

    # ------------------------------------------------------------------
    # PostgreSQL (docker mode) / SQLite (local mode)
    # ------------------------------------------------------------------
    postgres_url: str = Field(
        default="postgresql://opencrab:opencrab@localhost:5432/opencrab",
        alias="POSTGRES_URL",
    )

    # ------------------------------------------------------------------
    # ChromaDB (docker mode uses HttpClient; local mode uses PersistentClient)
    # ------------------------------------------------------------------
    chroma_host: str = Field(default="localhost", alias="CHROMA_HOST")
    chroma_port: int = Field(default=8000, alias="CHROMA_PORT")
    chroma_collection: str = Field(
        default="opencrab_vectors", alias="CHROMA_COLLECTION"
    )

    # ------------------------------------------------------------------
    # 임베딩 백엔드 (EMBEDDING_BACKEND 환경변수)
    #
    # 옵션:
    #   "openai" (기본) — OpenAI 호환 임베딩 서버(LM Studio 등) primary + 로컬 GGUF
    #              자동다운로드 fallback (ResilientEmbeddingFunction). 서버가 죽어도
    #              GGUF 폴백으로 계속 동작하므로 외부 서버가 필수 의존성은 아니다.
    #              EMBED_COLLECTION("opencrab_vectors_kure") 컬렉션 사용.
    #              실측(KURE-v1): top-1 5/5, MRR 1.000 vs minilm top-1 0/5, MRR 0.285.
    #   "local"  — ChromaDB 기본 EF (all-MiniLM-L6-v2, ONNX, 384d, 영어특화).
    #              CHROMA_COLLECTION("opencrab_vectors") 사용. llama-cpp-python /
    #              LM Studio 불필요. 명시적 롤백 옵션(한국어 변별 실패 수준).
    #
    # 변경 이유: 한국어 검색 품질 개선. minilm 은 한국어 변별 실패 수준이라 기본값을
    # openai(KURE)로 전환. GGUF 폴백이 있어 로컬 운영에서도 외부 서버 없이 동작한다.
    # 롤백: EMBEDDING_BACKEND=local 로 명시하면 기존 minilm 컬렉션 그대로 사용.
    # ------------------------------------------------------------------
    embedding_backend: str = Field(
        default="openai",
        alias="EMBEDDING_BACKEND",
        # Literal["local", "openai"] — pydantic-settings 호환을 위해 str 사용
    )

    # OpenAI 호환 임베딩 서버 설정 (EMBEDDING_BACKEND=openai 시 사용)
    # LM Studio, Ollama, vLLM, 실제 OpenAI 등 /v1/embeddings 구현 서버 모두 호환.
    # 대안: openai 패키지 미설치라 httpx 직접 호출 방식 채택.
    # 기본값은 LM Studio 로컬 기본 포트. 원격 서버는 OPENAI_API_BASE 로 지정.
    # 서버 미가동이어도 로컬 GGUF 폴백(ResilientEmbeddingFunction)으로 동작한다.
    #
    # 다중 원격 지원(변경 이유): 원격 GPU 서버가 1대뿐이면 재부팅/점검 중
    # GGUF 폴백(CPU, 느림)으로 떨어진다. 콤마로 여러 URL 을 나열하면
    # ResilientEmbeddingFunction 이 순서대로 시도해 GGUF 로 내려가기 전에
    # 다른 원격 서버를 우선 시도한다. 환경변수 이름은 그대로 두어(하위호환)
    # 단일 URL 만 넣는 기존 배포는 동작 변화가 없다.
    # 예: OPENAI_API_BASE="http://embed-host-1:1234/v1,http://embed-host-2:1234/v1"
    openai_api_base: str = Field(
        default="http://localhost:1234/v1",
        alias="OPENAI_API_BASE",
    )
    # 서버에 로드된 임베딩 모델 id. /v1/models 로 확인.
    # 예: "text-embedding-kure-v1" (KURE-v1), "text-embedding-3-small" 등
    openai_embed_model: str = Field(
        default="text-embedding-kure-v1",
        alias="OPENAI_EMBED_MODEL",
    )
    # OpenAI API key. 실제 OpenAI / 인증 게이트웨이 사용 시 설정.
    # 미설정(빈 문자열)이면 Authorization 헤더 없이 호출(LM Studio 등 무인증 서버).
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    # 임베딩 차원. 사용 모델에 맞게 설정. 변경 시 컬렉션 재적재 필요.
    # KURE-v1 = 1024, multilingual-e5-small = 384, text-embedding-3-small = 1536.
    embed_dim: int = Field(default=1024, alias="EMBED_DIM")

    # OpenAI 호환 서버 HTTP 타임아웃(초). 기본 8s.
    # 로컬 네트워크 기준 정상 응답은 1-3s이므로 8s면 충분.
    # 느린 원격 네트워크나 대형 배치 요청이라면 OPENAI_TIMEOUT 환경변수로 늘릴 것.
    openai_timeout: float = Field(default=8.0, alias="OPENAI_TIMEOUT")

    # openai 백엔드 전용 Chroma 컬렉션명. minilm("opencrab_vectors")와 분리해
    # 차원 비호환 문제를 방지한다. 롤백 시 기존 컬렉션은 보존됨.
    embed_collection: str = Field(
        default="opencrab_vectors_kure",
        alias="EMBED_COLLECTION",
    )

    # 로컬 GGUF 경로 (EMBEDDING_BACKEND=openai 시 폴백용).
    # 미설정 시 _ensure_local_gguf() 가 자동 다운로드(KURE-v1-Q8_0, ~635MB).
    # 다른 모델을 쓰려면 LOCAL_GGUF_PATH 로 직접 경로 지정.
    local_gguf_path: str = Field(default="", alias="LOCAL_GGUF_PATH")

    # ------------------------------------------------------------------
    # 벡터 스토어 백엔드 (VECTOR_BACKEND 환경변수) — 임베딩 백엔드와 독립 축.
    #
    # 옵션:
    #   ""(미설정, 기본) : 조건부 스마트 기본값 — vector_backend_resolved 참고.
    #                      is_local(local/kuzu) AND EMBEDDING_BACKEND=openai 이면
    #                      "sqlite-vec", 그 외(docker 모드 또는 EMBEDDING_BACKEND=local)
    #                      는 "chroma". local 운영의 기본 조합(openai+local)이 곧
    #                      sqlite-vec 이 되어 Chroma 다중프로세스 쓰기 제약을 피한다.
    #   "chroma"           : ChromaDB. 기존 동작 100% 보존(명시 롤백 옵션).
    #   "sqlite-vec"       : sqlite-vec(vec0). 4스토어를 단일 SQLite WAL 규율로 통일해
    #                        Chroma 다중프로세스 쓰기 제약/flock 층 제거. KURE(1024d) 표준
    #                        — 앱이 EMBEDDING_BACKEND=openai 의 KURE EF 로 직접 임베딩 후
    #                        INSERT(vec0 는 원시벡터 저장). VECTOR_DB_FILE 의 vec0 테이블 사용.
    #   "pgvector"         : (예약) 추후 PG-unified 경로. 미구현 시 명시적 오류.
    #
    # 설계: docs/pgvector-migration-plan.md §3.6 / §9. embedding 은 백엔드와 무관하게
    #       동일(ResilientEmbeddingFunction, KURE). 바뀌는 것은 저장/검색 백엔드뿐.
    # 롤백: VECTOR_BACKEND=chroma 로 명시하면 조건부 기본값과 무관하게 기존 Chroma
    #       스택 그대로 사용(항상 명시 설정이 최우선).
    # ------------------------------------------------------------------
    vector_backend: str = Field(
        default="",
        alias="VECTOR_BACKEND",
        # Literal["", "chroma", "sqlite-vec", "pgvector"] — pydantic-settings 호환 str
        # ""(빈 문자열) = 미설정 센티널. vector_backend_resolved 가 조건부로 해석.
    )
    # sqlite-vec 백엔드 벡터 DB 파일명(LOCAL_DATA_DIR 하위). graph.db/doc_store.db 와
    # 같은 디렉터리라 단일 LOCAL_DATA_DIR 백업이 벡터까지 포함.
    vector_db_file: str = Field(default="vectors.db", alias="VECTOR_DB_FILE")
    # sqlite-vec vec0 테이블명(KURE 1024d 단일 표준).
    vector_collection: str = Field(
        default="vectors_kure", alias="VECTOR_COLLECTION"
    )

    # ------------------------------------------------------------------
    # MCP server
    # ------------------------------------------------------------------
    mcp_server_name: str = Field(default="opencrab", alias="MCP_SERVER_NAME")
    mcp_server_version: str = Field(default="0.1.0", alias="MCP_SERVER_VERSION")
    # HTTP transport (opencrab serve --transport http). Bind host defaults to
    # loopback; expose on a trusted network (e.g. Tailscale) via --host 0.0.0.0.
    # The bearer token is NOT read from config — it comes from --auth-token(-file)
    # or LOCALCRAB_MCP_TOKEN(_FILE) to keep secrets out of the settings object.
    mcp_http_host: str = Field(default="127.0.0.1", alias="MCP_HTTP_HOST")
    mcp_http_port: int = Field(default=8765, alias="MCP_HTTP_PORT")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def chroma_url(self) -> str:
        return f"http://{self.chroma_host}:{self.chroma_port}"

    @property
    def is_local(self) -> bool:
        return self.storage_mode in ("local", "kuzu")

    @property
    def openai_api_bases(self) -> list[str]:
        """openai_api_base 를 콤마 기준으로 split 한 리스트.

        예: "http://a:1234/v1, http://b:1234/v1" -> ["http://a:1234/v1", "http://b:1234/v1"]
        각 항목 strip, 빈 항목(연속 콤마·trailing 콤마 등) 제거. 단일 URL(콤마 없음)이면
        길이 1 리스트 — 기존 단일 엔드포인트 동작과 100% 동일하다.
        """
        return [b.strip() for b in self.openai_api_base.split(",") if b.strip()]

    @property
    def vector_backend_resolved(self) -> str:
        """VECTOR_BACKEND 가 명시 설정되면 그대로, 미설정("")이면 조건부 기본값을
        반환한다. local 운영(is_local) + KURE 임베딩(openai) 조합에서만 sqlite-vec
        을 기본으로 골라, docker 모드나 minilm(local) 임베딩에서는 기존 chroma
        경로를 그대로 유지한다. 자세한 규칙은 vector_backend 필드 주석 참고."""
        if self.vector_backend:
            return self.vector_backend
        if self.is_local and self.embedding_backend == "openai":
            return "sqlite-vec"
        return "chroma"

    @property
    def sqlite_url(self) -> str:
        return f"sqlite:///{self.local_data_dir}/opencrab.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()

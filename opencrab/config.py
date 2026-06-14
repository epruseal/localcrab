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
    #   "local"  — ChromaDB 기본 EF (all-MiniLM-L6-v2, ONNX, 384d, 영어특화).
    #              기존 동작 그대로. CHROMA_COLLECTION("opencrab_vectors") 사용.
    #              llama-cpp-python / LM Studio 불필요. 롤백 기본값.
    #   "kure"   — KURE-v1 (한국어 SOTA, 1024d).
    #              LM Studio GPU(주력) + 로컬 GGUF(폴백) 자동 전환.
    #              새 컬렉션 CHROMA_COLLECTION_KURE("opencrab_vectors_kure") 사용.
    #              실측: top-1 5/5, MRR 1.000 vs minilm top-1 0/5, MRR 0.285.
    #
    # 변경 이유: 한국어 검색 품질 개선. minilm 은 한국어 변별 실패 수준.
    # 롤백: EMBEDDING_BACKEND 미설정 또는 "local" 로 되돌리면 기존 컬렉션 그대로.
    # ------------------------------------------------------------------
    embedding_backend: str = Field(
        default="local",
        alias="EMBEDDING_BACKEND",
        # Literal["local", "kure"] — pydantic-settings 호환을 위해 str 사용
    )

    # LM Studio 임베딩 서버 설정 (EMBEDDING_BACKEND=kure 시 사용)
    # 대안: openai 패키지 미설치라 httpx 직접 호출 방식 채택.
    # 기본값은 현재 운용중인 GTX 3090 LM Studio 주소.
    lmstudio_api_base: str = Field(
        default="http://100.77.10.49:1234/v1",
        alias="LMSTUDIO_API_BASE",
    )
    # LM Studio 에 로드된 KURE 모델 id. /v1/models 로 확인.
    # 현재 확인된 id: "text-embedding-kure-v1"
    lmstudio_embed_model: str = Field(
        default="text-embedding-kure-v1",
        alias="LMSTUDIO_EMBED_MODEL",
    )
    # KURE 임베딩 차원. KURE-v1 = 1024. 변경 시 컬렉션 재적재 필요.
    embed_dim: int = Field(default=1024, alias="EMBED_DIM")

    # LM Studio HTTP 타임아웃(초). 기본 8s.
    # 30s로 설정하면 장애 감지가 너무 느림(실측: 폴백까지 32s).
    # 로컬 네트워크 기준 정상 응답은 1-3s이므로 8s면 충분.
    # 느린 원격 네트워크나 대형 배치 요청이라면 LMSTUDIO_TIMEOUT 환경변수로 늘릴 것.
    lmstudio_timeout: float = Field(default=8.0, alias="LMSTUDIO_TIMEOUT")

    # KURE 전용 Chroma 컬렉션명. minilm("opencrab_vectors")와 분리해
    # 차원 비호환 문제를 방지한다. 롤백 시 기존 컬렉션은 보존됨.
    chroma_collection_kure: str = Field(
        default="opencrab_vectors_kure",
        alias="CHROMA_COLLECTION_KURE",
    )

    # 로컬 KURE GGUF 경로 (EMBEDDING_BACKEND=kure, 폴백용).
    # mykor/KURE-v1-gguf Q8_0 를 다운로드 후 경로 지정.
    # 비어있으면 폴백 모델 로드 실패 → LM Studio 장애 시 검색 불가.
    # 예: "/home/asdf/models/KURE-v1-Q8_0.gguf"
    kure_gguf_path: str = Field(default="", alias="KURE_GGUF_PATH")

    # ------------------------------------------------------------------
    # MCP server
    # ------------------------------------------------------------------
    mcp_server_name: str = Field(default="opencrab", alias="MCP_SERVER_NAME")
    mcp_server_version: str = Field(default="0.1.0", alias="MCP_SERVER_VERSION")

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
    def sqlite_url(self) -> str:
        return f"sqlite:///{self.local_data_dir}/opencrab.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()

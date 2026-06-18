"""
ChromaDB vector store adapter.

LocalCrab uses ChromaDB PersistentClient by default, so no Chroma server is
required. HttpClient remains available for direct adapter use, but the
LocalCrab factory always selects persistent local mode.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class ChromaStore:
    """ChromaDB adapter — persistent local by default, HttpClient optional."""

    def __init__(
        self,
        host: str,
        port: int,
        collection_name: str,
        local_mode: bool = False,
        local_path: str = "./opencrab_data/chroma",
        embedding_function: Any = None,
        # embedding_function: ChromaDB EmbeddingFunction 인스턴스.
        # None 이면 ChromaDB 기본 EF(all-MiniLM-L6-v2 ONNX, 384d) 사용 — 기존 동작.
        # ResilientEmbeddingFunction(KURE-v1) 을 주입하면 KURE 로 전환.
        # 변경 이유: 임베딩 모델을 외부에서 주입받아 교체 가능하게 함.
    ) -> None:
        self._host = host
        self._port = port
        self._collection_name = collection_name
        self._local_mode = local_mode
        self._local_path = local_path
        self._embedding_function = embedding_function
        self._client: Any = None
        self._collection: Any = None
        self._available = False
        # Chroma 자체는 프로세스 내 스레드 안전(공식 System Constraints)이라 add/upsert/
        # delete 에는 별도 락이 불필요하다. 이 락은 앱 레벨 공유 상태인 self._collection
        # 핸들 교체(reset_collection)를 원자화하고, 읽기/쓰기가 교체 도중의 핸들을 보지
        # 않도록 짧게 스냅샷하기 위한 것이다.
        self._lock = threading.Lock()
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            import chromadb  # type: ignore[import]

            if self._local_mode:
                import os
                os.makedirs(self._local_path, exist_ok=True)
                self._client = chromadb.PersistentClient(path=self._local_path)
                logger.info("ChromaDB local mode at %s", self._local_path)
            else:
                self._client = chromadb.HttpClient(host=self._host, port=self._port)
                self._client.heartbeat()
                logger.info("ChromaDB connected at %s:%s", self._host, self._port)

            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
                # embedding_function=None 이면 Chroma 기본 EF(minilm) 적용.
                # ResilientEF(KURE) 주입 시 해당 EF 로 add/query 자동 수행.
                embedding_function=self._embedding_function,
            )
            self._available = True
        except Exception as exc:
            if self._local_mode:
                logger.warning("ChromaDB local init failed: %s", exc)
            else:
                logger.warning(
                    "ChromaDB unavailable (%s:%s): %s", self._host, self._port, exc
                )
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        """Return True if ChromaDB is reachable."""
        try:
            self._client.heartbeat()
            return True
        except Exception:
            return False

    def _collection_handle(self) -> Any:
        """Return the current collection handle, snapshotted under the lock so a
        concurrent reset_collection() swap is never observed half-applied."""
        with self._lock:
            return self._collection

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def add_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """
        Add text chunks to the vector store.

        Parameters
        ----------
        texts:
            List of text strings to embed and store.
        metadatas:
            Parallel list of metadata dicts for each text.
        ids:
            Optional stable IDs; auto-generated from content hash if omitted.

        Returns
        -------
        list[str]
            The IDs of the inserted documents.
        """
        if not self._available:
            raise RuntimeError("ChromaDB is not available.")

        if ids is None:
            ids = [
                hashlib.sha256(f"{t}{time.time_ns()}".encode()).hexdigest()[:16]
                for t in texts
            ]

        if metadatas is None:
            metadatas = [{} for _ in texts]

        # Sanitize metadata — ChromaDB requires string/int/float/bool values
        clean_meta = [_sanitize_metadata(m) for m in metadatas]

        self._collection_handle().add(documents=texts, metadatas=clean_meta, ids=ids)
        logger.debug("ChromaDB: added %d documents", len(texts))
        return ids

    def upsert_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[str] | None = None,
    ) -> list[str]:
        """Upsert (add or update) text chunks."""
        if not self._available:
            raise RuntimeError("ChromaDB is not available.")

        if ids is None:
            ids = [
                hashlib.sha256(t.encode()).hexdigest()[:16] for t in texts
            ]
        if metadatas is None:
            metadatas = [{} for _ in texts]

        clean_meta = [_sanitize_metadata(m) for m in metadatas]
        self._collection_handle().upsert(documents=texts, metadatas=clean_meta, ids=ids)
        return ids

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def query(
        self,
        query_text: str,
        n_results: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Semantic similarity search.

        Parameters
        ----------
        query_text:
            Natural language query string.
        n_results:
            Maximum number of results to return.
        where:
            Optional metadata filter (ChromaDB `where` clause).

        Returns
        -------
        list of dicts with keys: id, document, metadata, distance.
        """
        if not self._available:
            raise RuntimeError("ChromaDB is not available.")

        kwargs: dict[str, Any] = {
            "query_texts": [query_text],
            "n_results": n_results,
        }
        if where:
            kwargs["where"] = where

        result = self._collection_handle().query(**kwargs)

        hits: list[dict[str, Any]] = []
        if result["ids"]:
            for idx in range(len(result["ids"][0])):
                hits.append(
                    {
                        "id": result["ids"][0][idx],
                        "document": result["documents"][0][idx],
                        "metadata": result["metadatas"][0][idx] if result["metadatas"] else {},
                        "distance": result["distances"][0][idx] if result.get("distances") else None,
                    }
                )
        return hits

    def get_by_id(self, doc_id: str) -> dict[str, Any] | None:
        """Retrieve a document by its ID."""
        if not self._available:
            raise RuntimeError("ChromaDB is not available.")

        result = self._collection_handle().get(ids=[doc_id])
        if result["ids"]:
            return {
                "id": result["ids"][0],
                "document": result["documents"][0],
                "metadata": result["metadatas"][0] if result["metadatas"] else {},
            }
        return None

    def delete(self, ids: list[str]) -> None:
        """Delete documents by their IDs."""
        if not self._available:
            raise RuntimeError("ChromaDB is not available.")
        self._collection_handle().delete(ids=ids)

    def count(self) -> int:
        """Return the number of documents in the collection."""
        if not self._available:
            return 0
        return self._collection_handle().count()

    def reset_collection(self) -> None:
        """Delete and recreate the collection (destructive)."""
        if not self._available:
            raise RuntimeError("ChromaDB is not available.")
        # delete→재생성으로 self._collection 핸들을 교체하므로 락으로 직렬화한다.
        # 락이 없으면 동시 reset 시 두 스레드가 같은 컬렉션을 delete 하여 '이미 삭제됨'
        # 에러가 나거나, 읽기가 삭제된 컬렉션을 가리키는 손상 핸들을 볼 수 있다.
        with self._lock:
            self._client.delete_collection(self._collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=self._embedding_function,
            )
        logger.info("ChromaDB: collection '%s' reset.", self._collection_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Convert metadata values to ChromaDB-compatible types."""
    clean: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool)):
            clean[k] = v
        elif v is None:
            clean[k] = ""
        else:
            clean[k] = str(v)
    return clean

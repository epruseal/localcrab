"""
로컬 KURE-v1 GGUF 임베딩 EF (ChromaDB EmbeddingFunction 프로토콜)

변경 이유:
  - LM Studio(원격 GPU) 다운/미응답 시 검색이 완전히 불가하지 않도록
    라즈베리파이 CPU에서 같은 모델(KURE-v1)의 GGUF 를 폴백으로 운용.
  - 동일 모델 + 동일 양자화(Q8_0) → 적재 벡터(LM Studio)와 폴백 검색 벡터가
    코사인 거리 보존 (실측 cosine≈1.000 확인 후 운용).

대안:
  - multilingual-e5-small (384d, ~0.2s): 더 빠르지만 주력(1024d)과 차원 불일치.
    단일 컬렉션 유지를 위해 동일 KURE 선택.
  - CPU 속도: RPi5 Cortex-A76×4 NEON, 0.6B BERT 1회 forward ~0.6–0.9s.
    폴백은 LM Studio 장애 시에만 발동되므로 허용.

모델 준비:
  - 다운로드: huggingface-cli download mykor/KURE-v1-gguf --include "*Q8_0*"
    또는 wget/curl 로 직접 수동 다운로드 후 KURE_GGUF_PATH 지정.
  - 양자화는 LM Studio 와 동일하게 Q8_0 사용 (호환 필수).

롤백:
  - EMBEDDING_BACKEND=local 으로 기존 minilm 컬렉션 즉시 복귀.
"""

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

_EMBEDDING_FUNCTION_NAME = "kure_v1"
# LMStudioEmbeddingFunction.name() 과 동일 문자열 유지.
# ChromaDB 는 컬렉션 메타데이터에 EF name 을 저장하므로, LM Studio ↔ 로컬
# 전환 시 같은 name 을 반환해야 같은 컬렉션을 재사용할 수 있다.


class LlamaCppEmbeddingFunction:
    """llama-cpp-python 으로 로컬 KURE-v1 GGUF 를 실행하는 EF.

    Parameters
    ----------
    gguf_path : str
        KURE-v1 Q8_0 GGUF 파일 경로.
        예: "/home/asdf/models/KURE-v1-Q8_0.gguf"
    dim : int
        임베딩 차원. KURE = 1024. LM Studio 측과 동일해야 함.
    n_threads : int
        CPU 스레드 수. RPi5 4코어 → 기본 4.
        n_threads=4 가 2, 3 보다 약간 빠름 (실측 필요).
    n_ctx : int
        최대 컨텍스트 길이. 512 는 localcrab 청크 크기 대비 충분.
        KURE 원본 max 8192 지만 폴백 검색은 짧은 쿼리가 대부분이라 절약.
    """

    def __init__(
        self,
        gguf_path: str,
        dim: int = 1024,
        n_threads: int = 4,
        n_ctx: int = 512,
    ) -> None:
        self._gguf_path = gguf_path
        self._dim = dim
        self._n_threads = n_threads
        self._n_ctx = n_ctx
        self._llm: Any = None  # lazy load — 폴백 최초 호출 시 로드

    # ------------------------------------------------------------------
    # ChromaDB EmbeddingFunction 프로토콜
    # ------------------------------------------------------------------

    def __call__(self, input: list[str]) -> list[list[float]]:
        """텍스트 리스트 → L2 정규화된 임베딩 리스트."""
        if not input:
            return []
        llm = self._get_llm()
        result = []
        for text in input:
            # create_embedding 은 단건씩 호출 (llama-cpp 내부 배치 없음).
            # KURE 는 쿼리/패시지 프리픽스 불필요 (bge-m3 계열, 대칭 임베딩).
            resp = llm.create_embedding(text)
            vec = resp["data"][0]["embedding"]
            result.append(_l2_normalize(vec))
        return result

    def name(self) -> str:
        """LMStudioEmbeddingFunction 과 동일한 고정 이름 반환."""
        return _EMBEDDING_FUNCTION_NAME

    def embed_query(self, input: list[str]) -> list[list[float]]:
        """ChromaDB 1.5+ 가 query 경로에서 호출하는 메서드.
        KURE 는 쿼리/패시지 임베딩이 대칭이므로 __call__ 과 동일 처리."""
        return self.__call__(input)

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _get_llm(self) -> Any:
        """최초 폴백 호출 시 모델 로드(lazy). 이후 캐시.

        GGUF 파일이 없으면 huggingface_hub 로 자동 다운로드를 시도한다.
        다운로드 실패 시 안내 메시지와 함께 RuntimeError 를 발생시킨다.
        """
        if self._llm is None:
            import os
            # ── GGUF 파일 존재 확인 / 자동 다운로드 ──────────────────────
            if not self._gguf_path or not os.path.exists(self._gguf_path):
                self._gguf_path = _ensure_kure_gguf(self._gguf_path)

            try:
                from llama_cpp import Llama  # type: ignore[import]
            except ImportError as exc:
                raise RuntimeError(
                    "llama-cpp-python 이 설치되지 않았습니다. "
                    "pip install llama-cpp-python 으로 설치하세요."
                ) from exc
            logger.info("로컬 KURE GGUF 로드 중: %s", self._gguf_path)
            self._llm = Llama(
                model_path=self._gguf_path,
                embedding=True,
                n_ctx=self._n_ctx,
                n_threads=self._n_threads,
                verbose=False,
            )
            logger.info("로컬 KURE GGUF 로드 완료 (dim=%d)", self._dim)
        return self._llm


def _l2_normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm < 1e-9:
        return v
    return [x / norm for x in v]


# ---------------------------------------------------------------------------
# GGUF 자동 다운로드
# ---------------------------------------------------------------------------

_DEFAULT_GGUF_DIR = "/home/asdf/models"
_HF_REPO = "mykor/KURE-v1-gguf"
_HF_FILENAME = "KURE-v1-Q8_0.gguf"
# Q8_0 선택 이유: 사실상 무손실(F16 대비 cosine≈0.9999)이면서
# LM Studio 쪽과 동일 양자화 → 적재/검색 벡터 호환 보장.
# 더 빠르게 쓰려면 Q4_K_M(438MB)으로 변경 가능하나 LM Studio도 동일 변경 필요.


def _ensure_kure_gguf(requested_path: str) -> str:
    """GGUF 파일이 없으면 HuggingFace 에서 자동 다운로드.

    Parameters
    ----------
    requested_path : str
        KURE_GGUF_PATH 설정값. 비어있거나 파일이 없으면 기본 경로에 다운로드.

    Returns
    -------
    str
        사용 가능한 GGUF 파일 경로.

    Raises
    ------
    RuntimeError
        다운로드 실패 시 안내 메시지 포함.
    """
    import os

    default_path = os.path.join(_DEFAULT_GGUF_DIR, _HF_FILENAME)
    target = requested_path if requested_path else default_path

    if os.path.exists(target):
        return target

    logger.warning(
        "로컬 KURE GGUF 파일이 없습니다: %s\n"
        "  HuggingFace(%s)에서 자동 다운로드를 시도합니다...\n"
        "  수동 다운로드: huggingface-cli download %s %s --local-dir %s\n"
        "  또는 환경변수 KURE_GGUF_PATH 에 기존 GGUF 경로를 지정하세요.",
        target, _HF_REPO, _HF_REPO, _HF_FILENAME, _DEFAULT_GGUF_DIR,
    )

    try:
        from huggingface_hub import hf_hub_download  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            f"KURE GGUF 자동 다운로드 실패: huggingface_hub 미설치.\n"
            f"  pip install huggingface_hub 후 재시도하거나\n"
            f"  huggingface-cli download {_HF_REPO} {_HF_FILENAME} "
            f"--local-dir {_DEFAULT_GGUF_DIR} 로 수동 다운로드하세요."
        ) from exc

    try:
        os.makedirs(os.path.dirname(target) or _DEFAULT_GGUF_DIR, exist_ok=True)
        downloaded = hf_hub_download(
            repo_id=_HF_REPO,
            filename=_HF_FILENAME,
            local_dir=os.path.dirname(target) or _DEFAULT_GGUF_DIR,
        )
        # hf_hub_download 가 다른 이름으로 저장할 수 있으므로 확인
        final = downloaded if os.path.exists(downloaded) else target
        logger.info("KURE GGUF 다운로드 완료: %s", final)
        return final
    except Exception as exc:
        raise RuntimeError(
            f"KURE GGUF 자동 다운로드 실패: {exc}\n"
            f"  수동 다운로드:\n"
            f"    huggingface-cli download {_HF_REPO} {_HF_FILENAME} "
            f"--local-dir {_DEFAULT_GGUF_DIR}\n"
            f"  또는 KURE_GGUF_PATH 환경변수에 기존 경로를 지정하세요."
        ) from exc

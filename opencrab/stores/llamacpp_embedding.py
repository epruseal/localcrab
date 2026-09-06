"""
로컬 GGUF 임베딩 EF (ChromaDB EmbeddingFunction 프로토콜) — 폴백용

변경 이유:
  - LM Studio(원격 GPU) 다운/미응답 시 검색이 완전히 불가하지 않도록
    라즈베리파이 CPU에서 로컬 GGUF 를 폴백으로 운용.

기본 모델(자동 다운로드):
  - KURE-v1-Q8_0 (mykor/KURE-v1-gguf, ~635MB)
  - Q8_0 선택(변경 이유): 폴백이라도 primary(GPU FP16)와 최대한 동일한 검색
    품질을 유지하기 위해 품질 우선. Q4_K_M(438MB, ~0.45s/건) 대비 크기 +197MB,
    속도 ~0.7s/건으로 느리지만 품질 손실이 거의 없다(벡터 일치도 cosine ~0.9999).
  - 저사양/저장공간 제약 환경은 LOCAL_GGUF_PATH 로 Q4_K_M 등 다른 양자화를
    직접 지정하는 대안이 남아 있다(동일 모델·차원이므로 컬렉션 호환).

대안:
  - 더 빠른 소형 모델(e5-small 등): 384d라 EMBED_COLLECTION 과 차원 불일치.
    같은 컬렉션을 재사용하려면 primary 와 동일 모델·차원 필요.
  - CPU 속도: RPi5 Cortex-A76×4 NEON, Q8_0 ~0.7s/건 (Q4_K_M ~0.45s/건).
    폴백은 LM Studio 장애 시에만 발동되므로 허용.

롤백:
  - EMBEDDING_BACKEND=local 으로 기존 minilm 컬렉션 즉시 복귀.
"""

import logging
import threading
from pathlib import Path
from typing import Any

from opencrab.stores._embedding_utils import EMBEDDING_FUNCTION_NAME, l2_normalize

logger = logging.getLogger(__name__)

# OpenAIEmbeddingFunction.name() 과 동일 문자열 유지.
# ChromaDB 는 컬렉션 메타데이터에 EF name 을 저장하므로, LM Studio ↔ 로컬
# 전환 시 같은 name 을 반환해야 같은 컬렉션을 재사용할 수 있다.


class LlamaCppEmbeddingFunction:
    """llama-cpp-python 으로 로컬 KURE-v1 GGUF 를 실행하는 EF.

    Parameters
    ----------
    gguf_path : str
        KURE-v1 Q8_0 GGUF 파일 경로.
        예: "~/.cache/localcrab/models/KURE-v1-Q8_0.gguf"
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
        # #302: 동시에 첫 호출한 두 스레드가 둘 다 로드를 실행해 Llama
        # 인스턴스가 두 개 만들어지는 것을 막는 인스턴스 레벨 락. 소유권
        # 마커는 opencrab/mcp/tools/__init__.py 의 _context_init_owner
        # (#192)와 같은 이유로 Thread 객체 자체를 담는다 — OS 가 종료된
        # 스레드의 ident 를 재사용하면 무관한 스레드가 소유자로 오인될 수
        # 있어서다. 같은 스레드의 재진입은 plain Lock 이면 영구 데드락이
        # 되므로 이 마커로 즉시 RuntimeError 를 낸다.
        self._llm_lock = threading.Lock()
        self._llm_init_owner: threading.Thread | None = None

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
            result.append(l2_normalize(vec))
        return result

    def name(self) -> str:
        """OpenAIEmbeddingFunction 과 동일한 고정 이름 반환."""
        return EMBEDDING_FUNCTION_NAME

    def embed_query(self, input: list[str]) -> list[list[float]]:
        """ChromaDB 1.5+ 가 query 경로에서 호출하는 메서드.
        KURE 는 쿼리/패시지 임베딩이 대칭이므로 __call__ 과 동일 처리."""
        return self.__call__(input)

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _get_llm(self) -> Any:
        """최초 폴백 호출 시 모델 로드(lazy). 이후 캐시.

        직렬화(#302): REST API 앱의 요청 핸들러는 스레드풀로 디스패치되므로
        (FastAPI 의 plain ``def`` 라우트, ``run_in_threadpool``), 공유
        임베딩 함수 인스턴스 하나에 여러 요청 스레드가 동시에 첫 호출을
        할 수 있다. 락 없이는 둘 다 ``if self._llm is None`` 을 통과해
        모델을 두 번 로드한다(GGUF 로드는 초 단위로 느려 창이 넓다).
        아래 빠른 읽기는 락 없이 수행한다 — CPython 에서 전역 필드 읽기와
        마지막의 단일 대입은 각각 GIL 아래 원자적이고, 완성된 객체를
        한 번에 배정하므로(부분 완성 상태를 락 없는 읽기에 노출하지
        않는다) 안전하다.

        재진입 가드: 같은 스레드가 로드 도중 자기 자신을 통해
        ``_get_llm()`` 을 다시 부르면(오늘 그런 호출자는 없다) plain
        ``Lock`` 은 영구 데드락이 된다. 소유권 마커로 그 경우를 즉시
        ``RuntimeError`` 로 실패시킨다 — ``opencrab/mcp/tools/__init__.py``
        의 ``_context_init_owner``(#192)와 동일한 패턴과 동일한 제약:
        같은 스레드의 재진입만 잡고, 자식 스레드에 위임하고 기다리는
        경우는 여전히 데드락이다(이 클래스에는 그런 호출자가 없다).

        GGUF 파일이 없으면 huggingface_hub 로 자동 다운로드를 시도한다.
        다운로드 실패 시 안내 메시지와 함께 RuntimeError 를 발생시킨다.
        """
        if self._llm is not None:
            return self._llm

        if self._llm_init_owner is threading.current_thread():
            raise RuntimeError(
                "reentrant _get_llm() call: something called back into the "
                "embedding model initialiser on the same thread while it was "
                "still loading. Break the cycle in the caller."
            )

        with self._llm_lock:
            if self._llm is not None:
                return self._llm
            try:
                self._llm_init_owner = threading.current_thread()
                import os
                # ── GGUF 파일 존재 확인 / 자동 다운로드 ──────────────────
                if not self._gguf_path or not os.path.exists(self._gguf_path):
                    self._gguf_path = _ensure_local_gguf(self._gguf_path)

                try:
                    from llama_cpp import Llama  # type: ignore[import]
                except ImportError as exc:
                    raise RuntimeError(
                        "llama-cpp-python 이 설치되지 않았습니다. "
                        "pip install llama-cpp-python 으로 설치하세요."
                    ) from exc
                logger.info("로컬 GGUF 로드 중: %s", self._gguf_path)
                llm = Llama(
                    model_path=self._gguf_path,
                    embedding=True,
                    n_ctx=self._n_ctx,
                    n_threads=self._n_threads,
                    verbose=False,
                )
                logger.info("로컬 GGUF 로드 완료 (dim=%d)", self._dim)
                self._llm = llm
            finally:
                self._llm_init_owner = None
        return self._llm


# ---------------------------------------------------------------------------
# GGUF 자동 다운로드
# ---------------------------------------------------------------------------

def _default_gguf_dir() -> str:
    """LOCAL_GGUF_PATH 미설정 시 기본 다운로드 디렉터리: 실행 사용자 홈 하위.

    호출 시점마다 평가(모듈 임포트 시 고정 아님)해 HOME 변경(테스트의
    monkeypatch 등)이 다음 호출부터 즉시 반영되도록 한다.
    """
    return str(Path.home() / ".cache" / "localcrab" / "models")


_HF_REPO = "mykor/KURE-v1-gguf"
_HF_FILENAME = "KURE-v1-Q8_0.gguf"
# Q8_0 선택 이유(품질 우선): primary(GPU)와 거의 동일한 검색 품질 유지
# (벡터 일치도 cosine ~0.9999). 크기 ~635MB·속도 ~0.7s/건으로
# Q4_K_M(438MB, ~0.45s/건)보다 무겁지만 폴백 품질 저하를 없앤다.
# 저사양 환경은 LOCAL_GGUF_PATH 로 Q4_K_M 등 다른 양자화를 직접 지정 가능.


def _ensure_local_gguf(requested_path: str) -> str:
    """GGUF 파일이 없으면 HuggingFace 에서 자동 다운로드.

    Parameters
    ----------
    requested_path : str
        LOCAL_GGUF_PATH 설정값. 비어있거나 파일이 없으면 기본 경로에 다운로드.

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

    gguf_dir = _default_gguf_dir()
    default_path = os.path.join(gguf_dir, _HF_FILENAME)
    target = requested_path if requested_path else default_path

    if os.path.exists(target):
        return target

    logger.warning(
        "로컬 KURE GGUF 파일이 없습니다: %s\n"
        "  HuggingFace(%s)에서 자동 다운로드를 시도합니다...\n"
        "  수동 다운로드: huggingface-cli download %s %s --local-dir %s\n"
        "  또는 환경변수 LOCAL_GGUF_PATH 에 기존 GGUF 경로를 지정하세요.",
        target, _HF_REPO, _HF_REPO, _HF_FILENAME, gguf_dir,
    )

    try:
        from huggingface_hub import hf_hub_download  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            f"KURE GGUF 자동 다운로드 실패: huggingface_hub 미설치.\n"
            f"  pip install huggingface_hub 후 재시도하거나\n"
            f"  huggingface-cli download {_HF_REPO} {_HF_FILENAME} "
            f"--local-dir {gguf_dir} 로 수동 다운로드하세요."
        ) from exc

    try:
        os.makedirs(os.path.dirname(target) or gguf_dir, exist_ok=True)
        downloaded = hf_hub_download(
            repo_id=_HF_REPO,
            filename=_HF_FILENAME,
            local_dir=os.path.dirname(target) or gguf_dir,
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
            f"--local-dir {gguf_dir}\n"
            f"  또는 LOCAL_GGUF_PATH 환경변수에 기존 경로를 지정하세요."
        ) from exc

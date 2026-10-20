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

상주 메모리(#508):
  - 적재한 모델은 마지막 사용 뒤 idle_unload_seconds 가 지나면 내려놓는다
    (Llama.close() 다음 malloc_trim). 다음 호출이 모델을 다시 올린다.
    알고리즘과 상태 전이: docs/issue-508-gguf-fallback-release.md
"""

import ctypes
import logging
import threading
import time
from collections.abc import Callable
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
    idle_unload_seconds : float
        마지막 사용 뒤 이 시간(초)이 지나면 모델을 내려놓는다(``Llama.close()``
        뒤 ``malloc_trim`` 으로 힙 반환을 시도한다, #508). 다음 호출이 모델을 다시 올린다. 0 이하이면 해제하지
        않는다(프로세스 수명 동안 상주, 종전 동작). 설계: docs/issue-508-gguf-fallback-release.md
    clock : Callable[[], float]
        유휴 시간 판정에 쓰는 단조 시계. 시험이 대체한다.
    """

    def __init__(
        self,
        gguf_path: str,
        dim: int = 1024,
        n_threads: int = 4,
        n_ctx: int = 512,
        idle_unload_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
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
        # 적재 중이거나 해제 중인(= _llm_lock 을 쥔) 스레드. 같은 스레드의 재진입
        # 검사에 쓴다.
        self._llm_init_owner: threading.Thread | None = None

        # #508: 유휴 해제 상태. _state_lock 은 아래 필드를 읽고 쓰는 짧은 임계
        # 구역의 잠금이며, 이 잠금을 쥔 채 적재/해제/임베딩을 하지 않는다.
        self._idle_unload_seconds = idle_unload_seconds
        self._clock = clock
        self._state_lock = threading.Lock()
        self._inflight = 0
        self._last_used = 0.0
        self._timer: Any = None
        self._timer_gen = 0
        self._timer_factory: Callable[[float, Callable[[], None]], Any] = _daemon_timer
        self.load_count = 0
        self.release_count = 0
        self.release_error_count = 0

    # ------------------------------------------------------------------
    # ChromaDB EmbeddingFunction 프로토콜
    # ------------------------------------------------------------------

    def __call__(self, input: list[str]) -> list[list[float]]:
        """텍스트 리스트 → L2 정규화된 임베딩 리스트."""
        if not input:
            return []
        llm = self._acquire(True)
        try:
            result = []
            for text in input:
                # create_embedding 은 단건씩 호출 (llama-cpp 내부 배치 없음).
                # KURE 는 쿼리/패시지 프리픽스 불필요 (bge-m3 계열, 대칭 임베딩).
                resp = llm.create_embedding(text)
                vec = resp["data"][0]["embedding"]
                result.append(l2_normalize(vec))
            return result
        finally:
            self._return_lease()

    @property
    def loaded(self) -> bool:
        """모델이 지금 적재돼 있으면 True."""
        return self._llm is not None

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
        """모델을 돌려준다(없으면 적재). 임대(lease)는 잡지 않는다.

        ``__call__`` 은 ``_acquire(True)`` 로 임대를 잡는다. 이 메서드는 임대 없이
        같은 적재 경로를 쓰는 형태이며 시험이 직접 부른다.
        """
        return self._acquire(False)

    def _acquire(self, lease: bool) -> Any:
        """최초 폴백 호출 시 모델 로드(lazy). 이후 캐시. ``lease`` 가 참이면 모델을
        돌려주는 모든 분기가 ``_inflight`` 를 올린다(#508, 반납은 ``_return_lease``).

        직렬화(#302): REST API 앱의 요청 핸들러는 스레드풀로 디스패치되므로
        (FastAPI 의 plain ``def`` 라우트, ``run_in_threadpool``), 공유
        임베딩 함수 인스턴스 하나에 여러 요청 스레드가 동시에 첫 호출을
        할 수 있다. 락 없이는 둘 다 ``if self._llm is None`` 을 통과해
        모델을 두 번 로드한다(GGUF 로드는 초 단위로 느려 창이 넓다).
        ``lease=False`` 의 빠른 읽기는 락 없이 수행한다 — CPython 에서 필드
        읽기와 단일 대입은 GIL 아래 원자적이고, 완성된 객체를 한 번에
        배정하므로 안전하다. ``lease=True`` 는 모델 참조 취득과 ``_inflight``
        증가를 ``_state_lock`` 한 구역에서 처리한다(해제 판정과 같은 잠금).

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
        if lease:
            with self._state_lock:
                if self._llm is not None:
                    self._inflight += 1
                    self._last_used = self._clock()
                    return self._llm
        elif self._llm is not None:
            return self._llm

        if self._llm_init_owner is threading.current_thread():
            raise RuntimeError(
                "reentrant _get_llm() call: something called back into the "
                "embedding model initialiser on the same thread while it was "
                "still loading. Break the cycle in the caller."
            )

        with self._llm_lock:
            try:
                self._llm_init_owner = threading.current_thread()
                with self._state_lock:
                    if self._llm is not None:
                        # 다른 스레드가 먼저 적재했다. 이 분기도 임대를 잡는다.
                        if lease:
                            self._inflight += 1
                            self._last_used = self._clock()
                        return self._llm
                llm = self._load_model()
                with self._state_lock:
                    self._llm = llm
                    self.load_count += 1
                    if lease:
                        self._inflight += 1
                        self._last_used = self._clock()
                return llm
            finally:
                self._llm_init_owner = None

    def _load_model(self) -> Any:
        """GGUF 를 읽어 ``Llama`` 객체를 만든다(``_llm_lock`` 아래에서만 부른다)."""
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
        return llm

    # ------------------------------------------------------------------
    # 유휴 해제 (#508)
    # ------------------------------------------------------------------

    def _return_lease(self) -> None:
        """임대를 반납한다. 해제 타이머가 없으면 하나 만든다."""
        with self._state_lock:
            self._inflight -= 1
            self._last_used = self._clock()
            self._arm_timer_locked(self._idle_unload_seconds)

    def _arm_timer_locked(self, delay: float) -> None:
        """``_state_lock`` 을 쥔 채 부른다. 타이머는 ``_timer`` 가 비었을 때만 만든다."""
        if self._idle_unload_seconds <= 0 or self._timer is not None:
            return
        self._timer_gen += 1
        gen = self._timer_gen
        timer = self._timer_factory(max(delay, 0.0), lambda: self._on_timer(gen))
        self._timer = timer
        timer.start()

    def _on_timer(self, gen: int) -> None:
        try:
            self.release_if_idle(from_timer=True)
        except Exception:
            logger.warning("로컬 GGUF 유휴 해제 타이머 실패", exc_info=True)
        finally:
            with self._state_lock:
                if self._timer_gen == gen:
                    self._timer = None

    def release_if_idle(self, from_timer: bool = False) -> bool:
        """유휴 시간이 지났고 사용 중인 호출이 없으면 모델을 내려놓고 True.

        타이머가 부르는 경로 말고 시험이 직접 부르는 경로도 같은 판정을 쓴다.
        """
        if self._idle_unload_seconds <= 0:
            return False
        me = threading.current_thread()
        if self._llm_init_owner is me:
            return False  # 적재나 해제 중인 스레드가 되돌아온 호출
        with self._llm_lock:
            try:
                self._llm_init_owner = me
                with self._state_lock:
                    if from_timer:
                        self._timer = None
                    llm = self._llm
                    if llm is None:
                        return False
                    if self._inflight > 0:
                        self._arm_timer_locked(self._idle_unload_seconds)
                        return False
                    remaining = self._idle_unload_seconds - (self._clock() - self._last_used)
                    if remaining > 0:
                        self._arm_timer_locked(remaining)
                        return False
                    self._llm = None
                try:
                    close = getattr(llm, "close", None)
                    if close is not None:
                        close()
                except Exception:
                    self.release_error_count += 1
                    logger.warning("로컬 GGUF 해제 중 close() 실패", exc_info=True)
                    llm = None
                    _trim_heap()
                    return True
                llm = None
                _trim_heap()
                self.release_count += 1
                logger.info("로컬 GGUF 해제 (유휴 %.0fs)", self._idle_unload_seconds)
                return True
            finally:
                self._llm_init_owner = None


def _daemon_timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    return timer


def _trim_heap() -> None:
    """glibc 가 free 한 아레나 페이지를 운영체제에 돌려주게 한다(best-effort).

    ``Llama.close()`` 만으로는 익명 메모리 일부가 아레나에 남아 RSS 가 적재
    전 수준까지 내려오지 않는다(#508 측정). glibc 가 아닌 libc 에서는 건너뛴다.
    """
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


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

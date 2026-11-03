"""
ResilientEmbeddingFunction — 다중 원격(LM Studio 등) ↔ 로컬 GGUF 자동 폴백 EF

변경 이유:
  - LM Studio(원격 GPU)가 재시작/점검 중일 때도 검색이 멈추지
    않도록, 동일 모델(KURE-v1)의 로컬 GGUF 를 폴백으로 자동 사용.
  - 원격 GPU 서버가 한 대뿐이면 그 서버가 죽는 순간 바로 GGUF(CPU, 느림)로
    떨어진다. primary 를 "리스트"로 받아 순서대로 시도하게 확장해, 2번째
    원격 서버가 있으면 GGUF 로 내려가기 전에 그것부터 시도한다
    (OPENAI_API_BASE="http://a:1234/v1,http://b:1234/v1" 콤마 다중 지정).
  - 한 번 ping 실패 시 health_ttl 동안 해당 primary 를 건너뛰어 불필요한
    타임아웃 대기를 방지. 각 primary 는 "독립" TTL 을 가진다 — 1번이 죽어
    있어도 2번은 매 호출마다 정상적으로 우선 시도된다(1번의 장애가 2번
    시도까지 지연시키지 않음).

동작 원칙:
  - primary 는 단일 EF 또는 EF 리스트 모두 허용(단일이면 길이 1 리스트로
    정규화 — 기존 호출부/테스트 100% 하위호환).
  - healthy 한 primary 를 리스트 순서대로 시도. 첫 성공 즉시 반환.
  - 각 primary 에서 예외 발생 → 경고 로그(어느 엔드포인트인지 포함) 후
    해당 primary 만 unhealthy 마킹, 다음 primary 로 계속.
  - 모든 primary 가 unhealthy 이거나 전부 실패 → fallback 호출.
  - health_ttl(기본 15s) 동안 해당 primary 장애 캐시 → 불필요한 재시도 방지.
    연속 실패하면 건너뜀 간격을 두 배씩 늘려 max_backoff(기본 120s)까지 키운다
    (15, 30, 60, 120, 120...). 성공하면 연속 실패 횟수가 0으로 돌아간다.
  - attempt_budget 이 있으면 한 호출에서 첫 시도 이후 경과 시간이 예산 이상일 때
    이미 실패한 적이 있는 primary(재시도 대상)를 더 시도하지 않는다(호출당 실패
    시도 시간 상한). 한 번도 실패하지 않은 primary 는 예산과 무관하게 시도한다.
    예산에 걸려 건너뛴 첫 재시도 대상은 다음 호출의 첫 후보가 된다(굶김 방지).
    설계: docs/issue-581-embed-backoff.md
  - 이름(name())은 첫 primary 와 동일 반환 → 컬렉션 재사용 보장(모든
    엔드포인트가 동일 모델 KURE-v1 이라는 전제, name()="kure_v1").

대안:
  - Circuit breaker 패턴: health_ttl 대신 실패 횟수 기반.
    구현 복잡도 대비 이득이 적어 TTL 방식 채택.
  - primary 를 동시(병렬) 호출 후 first-success 채택: 불필요한 API 사용량
    증가(모든 요청이 N배) 및 레이스 컨디션 복잡도 대비 이득이 적어 제외.
    순차 시도가 대부분의 장애(서버 다운)에서 충분히 빠르다(ping 실패는
    보통 즉시 커넥션 에러).
  - 프로세스 재시작 시 헬스 캐시 초기화됨 → primary 가 살아있으면
    다음 호출에서 바로 복귀.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from opencrab.stores._embedding_utils import l2_normalize

logger = logging.getLogger(__name__)


def _label(ef: Any, index: int) -> str:
    """로그용 primary 식별 라벨. api_base 속성이 있으면 그것을, 없으면
    인덱스 기반 이름을 사용."""
    api_base = getattr(ef, "api_base", None)
    if api_base:
        return str(api_base)
    return f"primary[{index}]"


# ── 장문 윈도우 분할 (KURE n_ctx 8192 초과 방지, 2026-07-22) ─────────────
# 서버(llama.cpp)는 초과 입력을 head 절단해 임베딩하므로 후반부 신호가 유실된다.
# 한도 초과 텍스트는 윈도우로 나눠 각각 임베딩한 뒤 mean pooling + L2 정규화로
# 벡터 1개를 복원한다. 문자 기준 근사(한국어 XLM-R ~2자/토큰, 보수 마진).
EMBED_WINDOW_CHARS = int(os.environ.get("EMBED_WINDOW_CHARS", "9000"))
_WINDOW_MIN_TAIL = 1500  # 꼬리 윈도우가 이보다 짧으면 직전에 병합(평균 희석 방지)

def _split_windows(text: str) -> list[str]:
    if len(text) <= EMBED_WINDOW_CHARS:
        return [text]
    ws = [text[i:i + EMBED_WINDOW_CHARS] for i in range(0, len(text), EMBED_WINDOW_CHARS)]
    if len(ws) > 1 and len(ws[-1]) < _WINDOW_MIN_TAIL:
        ws[-2] += ws[-1]
        ws.pop()
    return ws


class ResilientEmbeddingFunction:
    """primary EF(들) 장애 시 fallback EF 로 자동 전환.

    Parameters
    ----------
    primary : Any | list[Any]
        정상 운용 EF. OpenAIEmbeddingFunction 권장. 리스트로 여러 개
        넘기면 순서대로 시도(다중 원격 엔드포인트 체인). 단일 EF 를 넘기면
        길이 1 리스트로 정규화된다(기존 호출부 하위호환).
    fallback : Any
        모든 primary 장애 시 사용하는 EF. LlamaCppEmbeddingFunction 권장.
    health_ttl : float
        각 primary 첫 장애 후 재시도 금지 시간(초). 기본 15s. primary 별로
        독립적으로 추적된다(죽은 1번이 매 호출마다 2번 시도까지 지연시키지
        않음). 낮추면 빠른 복귀, 높이면 타임아웃 낭비 감소. 연속 실패하면
        두 배씩 늘어난다.
    max_backoff : float
        건너뜀 간격의 상한(초). 기본 120s. 실제 상한은 max(max_backoff,
        health_ttl). health_ttl 과 같게 두면 백오프가 꺼진다.
    attempt_budget : float | None
        한 호출에서 첫 시도 이후 경과 시간이 이 값(초) 이상이면 이미 실패한
        적이 있는 primary(재시도 대상)를 더 시도하지 않는다. 한 번도 실패하지
        않은 primary 는 예산과 무관하게 시도한다. None 이면 상한 없음. 양수여야
        한다. 호출당 실패 시도 시간은 attempt_budget + D + F 미만이다(D: 마지막
        재시도 대상 시도 하나의 소요 시간, F: 그 호출에서 처음 실패한 primary
        들의 실패 시간 합. 모든 primary 가 이미 실패한 적이 있으면 F=0).
    clock : Callable[[], float]
        시계. 기본 time.monotonic. 시험이 가상 시계를 넣는다.
    """

    def __init__(
        self,
        primary: Any | list[Any],
        fallback: Any,
        health_ttl: float = 15.0,
        max_backoff: float = 120.0,
        attempt_budget: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primaries: list[Any] = list(primary) if isinstance(primary, list) else [primary]
        if not self._primaries:
            raise ValueError("primary 리스트가 비어있습니다 — 최소 1개 필요")
        if attempt_budget is not None and attempt_budget <= 0:
            raise ValueError("attempt_budget 은 양수여야 합니다")
        self._fallback = fallback
        self._health_ttl = health_ttl
        self._max_backoff = max_backoff
        self._attempt_budget = attempt_budget
        self._clock = clock
        # primary 별 독립 unhealthy TTL. 인덱스로 추적(동일 EF 인스턴스
        # 중복 등록 엣지케이스에도 안전).
        self._unhealthy_until: list[float] = [0.0] * len(self._primaries)
        # primary 별 연속 실패 횟수(성공하면 0). 건너뜀 간격 계산에 쓴다.
        self._failures: list[int] = [0] * len(self._primaries)
        # 다음 호출이 스캔을 시작할 위치. 예산에 걸려 건너뛴 첫 재시도 대상의
        # 위치를 기록해 그 대상이 굶지 않게 한다. 평소 0.
        self._resume = 0
        # _unhealthy_until/_failures/_resume 의 읽기·쓰기를 보호한다(#577 이후
        # 도구가 여러 스레드에서 같은 인스턴스를 쓴다). 쥔 채 원격 호출은 하지 않는다.
        self._state_lock = threading.Lock()
        # 폴백 사용 횟수. 폴백은 설계상 정상 동작이지만 CPU 라 10배 이상 느리다.
        # 2026-08-04 적재에서 primary 가 통째로 오배선돼 전량이 이 경로로 갔는데,
        # WARNING 154줄 외에 집계가 없어 43분이 지나서야 발견됐다. 호출부가
        # 종료 요약에 쓸 수 있도록 카운터를 노출한다.
        self._fallback_calls = 0
        self._primary_calls = 0

    # ------------------------------------------------------------------
    # ChromaDB EmbeddingFunction 프로토콜
    # ------------------------------------------------------------------

    def _embed_chain(self, input: list[str]) -> list[list[float]]:
        """시도 대상 primary 를 순서대로 시도 → 전부 실패/건너뜀 시 fallback.

        시도 대상: 한 번도 실패하지 않았거나 건너뜀 기한이 지난 primary.
        스캔은 _resume 위치부터 시작해 한 바퀴 돈다. 예산은 이미 실패한 적이
        있는 primary(재시도 대상)에만 걸리고, 호출의 첫 시도는 예산 검사를
        받지 않는다. 예산에 걸린 재시도 대상은 건너뛰고 스캔을 계속한다(뒤에
        한 번도 실패하지 않은 primary 가 있을 수 있다)."""
        if not input:
            return []

        count = len(self._primaries)
        t0 = self._clock()
        with self._state_lock:
            start = self._resume
        attempted = False
        first_skipped: int | None = None
        for k in range(count):
            i = (start + k) % count
            ef = self._primaries[i]
            if not self._is_healthy(i):
                continue
            if (
                attempted
                and self._attempt_budget is not None
                and self._clock() - t0 >= self._attempt_budget
                and self._has_failed(i)
            ):
                if first_skipped is None:
                    first_skipped = i
                continue
            attempted = True
            try:
                out = ef(input)
            except Exception as exc:
                logger.warning(
                    "임베딩 primary 실패 (%s): %s → 다음 엔드포인트 시도",
                    _label(ef, i),
                    exc,
                )
                self._mark_unhealthy(i)
                continue
            self._primary_calls += 1
            with self._state_lock:
                self._failures[i] = 0
                self._unhealthy_until[i] = 0.0
                # 건너뛴 재시도 대상이 있으면 성공해도 그 차례를 남긴다.
                self._resume = 0 if first_skipped is None else first_skipped
            return out

        with self._state_lock:
            self._resume = 0 if first_skipped is None else first_skipped

        # 모든 primary 가 unhealthy 이거나 실패 → fallback 경로
        self._fallback_calls += 1
        if first_skipped is not None:
            logger.info(
                "임베딩 폴백: 로컬 GGUF (시도 예산 %.0fs 소진, 재시도 대상 primary 미시도)",
                self._attempt_budget,
            )
        else:
            logger.info("임베딩 폴백: 로컬 GGUF (모든 primary 장애 또는 unhealthy)")
        return self._fallback(input)

    @property
    def fallback_calls(self) -> int:
        """폴백(로컬 GGUF, CPU)으로 처리한 배치 수."""
        return self._fallback_calls

    @property
    def primary_calls(self) -> int:
        """primary(원격 GPU)로 처리한 배치 수."""
        return self._primary_calls

    def __call__(self, input: list[str]) -> list[list[float]]:
        """healthy 한 primary 를 순서대로 시도 → 전부 실패/unhealthy 시 fallback.
        장문은 윈도우 분할→개별 임베딩→mean pooling+L2 정규화로 벡터 1개 복원."""
        if not input:
            return []
        expanded: list[str] = []
        spans: list[tuple[int, int]] = []
        for text in input:
            ws = _split_windows(text)
            spans.append((len(expanded), len(expanded) + len(ws)))
            expanded.extend(ws)

        vecs = self._embed_chain(expanded)

        if len(expanded) == len(input):
            return vecs
        out: list[list[float]] = []
        for a, b in spans:
            if b - a == 1:
                out.append(vecs[a])
            else:
                n = b - a
                dim = len(vecs[a])
                pooled = [sum(vecs[j][k] for j in range(a, b)) / n for k in range(dim)]
                out.append(l2_normalize(pooled))
        return out

    def name(self) -> str:
        """ChromaDB persistence 식별 이름. 첫 primary 와 동일("kure_v1") 반환.
        모든 primary 엔드포인트가 동일 모델(KURE-v1)이라는 전제이므로 어느
        엔드포인트를 쓰든 컬렉션 재사용이 보장된다."""
        return self._primaries[0].name()

    def embed_query(self, input: list[str]) -> list[list[float]]:
        """ChromaDB 1.5+ 가 query 경로에서 호출하는 메서드.
        KURE 는 쿼리/패시지 임베딩이 대칭이므로 __call__ 과 동일 처리."""
        return self.__call__(input)

    # ------------------------------------------------------------------
    # 헬스 TTL 관리 (primary 별 독립)
    # ------------------------------------------------------------------

    def _is_healthy(self, index: int) -> bool:
        """건너뜀 기한이 지났으면(또는 실패한 적이 없으면) True."""
        with self._state_lock:
            return self._clock() >= self._unhealthy_until[index]

    def _has_failed(self, index: int) -> bool:
        """연속 실패 횟수가 양수이면(재시도 대상) True."""
        with self._state_lock:
            return self._failures[index] > 0

    def _mark_unhealthy(self, index: int) -> None:
        """연속 실패 횟수를 올리고 건너뜀 기한을 정한다. 간격은 health_ttl 에서
        시작해 실패마다 두 배가 되며 max(max_backoff, health_ttl) 에서 멈춘다.
        지수는 20 에서 자른다(큰 횟수에서 부동소수 넘침 방지)."""
        with self._state_lock:
            self._failures[index] += 1
            failures = self._failures[index]
            cap = max(self._max_backoff, self._health_ttl)
            interval = min(self._health_ttl * 2 ** min(failures - 1, 20), cap)
            self._unhealthy_until[index] = self._clock() + interval
        logger.info(
            "primary(%s) 를 %.0fs 동안 건너뜀 (연속 실패 %d회)",
            _label(self._primaries[index], index),
            interval,
            failures,
        )

    def force_check(self) -> bool:
        """모든 primary 를 핑해 성공한 것의 TTL 을 해제한다(선택). 운영
        스크립트에서 사용 가능. 하나라도 healthy 해지면 True 반환."""
        any_healthy = False
        for i, ef in enumerate(self._primaries):
            if hasattr(ef, "ping") and ef.ping():
                with self._state_lock:
                    self._unhealthy_until[i] = 0.0
                    self._failures[i] = 0
                any_healthy = True
                logger.info("primary(%s) 헬스체크 성공 → 폴백 해제", _label(ef, i))
        return any_healthy

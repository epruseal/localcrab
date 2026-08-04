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
import time
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
        각 primary 장애 후 재시도 금지 시간(초). 기본 15s. primary 별로
        독립적으로 추적된다(죽은 1번이 매 호출마다 2번 시도까지 지연시키지
        않음). 낮추면 빠른 복귀, 높이면 타임아웃 낭비 감소.
    """

    def __init__(
        self,
        primary: Any | list[Any],
        fallback: Any,
        health_ttl: float = 15.0,
    ) -> None:
        self._primaries: list[Any] = list(primary) if isinstance(primary, list) else [primary]
        if not self._primaries:
            raise ValueError("primary 리스트가 비어있습니다 — 최소 1개 필요")
        self._fallback = fallback
        self._health_ttl = health_ttl
        # primary 별 독립 unhealthy TTL. 인덱스로 추적(동일 EF 인스턴스
        # 중복 등록 엣지케이스에도 안전).
        self._unhealthy_until: list[float] = [0.0] * len(self._primaries)
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
        """healthy 한 primary 를 순서대로 시도 → 전부 실패/unhealthy 시 fallback."""
        if not input:
            return []

        for i, ef in enumerate(self._primaries):
            if not self._is_healthy(i):
                continue
            try:
                out = ef(input)
                self._primary_calls += 1
                return out
            except Exception as exc:
                logger.warning(
                    "임베딩 primary 실패 (%s): %s → 다음 엔드포인트 시도",
                    _label(ef, i),
                    exc,
                )
                self._mark_unhealthy(i)

        # 모든 primary 가 unhealthy 이거나 실패 → fallback 경로
        self._fallback_calls += 1
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
        return time.monotonic() >= self._unhealthy_until[index]

    def _mark_unhealthy(self, index: int) -> None:
        self._unhealthy_until[index] = time.monotonic() + self._health_ttl
        logger.info(
            "primary(%s) 를 %.0fs 동안 건너뜀 (health_ttl)",
            _label(self._primaries[index], index),
            self._health_ttl,
        )

    def force_check(self) -> bool:
        """모든 primary 를 핑해 성공한 것의 TTL 을 해제한다(선택). 운영
        스크립트에서 사용 가능. 하나라도 healthy 해지면 True 반환."""
        any_healthy = False
        for i, ef in enumerate(self._primaries):
            if hasattr(ef, "ping") and ef.ping():
                self._unhealthy_until[i] = 0.0
                any_healthy = True
                logger.info("primary(%s) 헬스체크 성공 → 폴백 해제", _label(ef, i))
        return any_healthy

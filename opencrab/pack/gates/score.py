"""팩 품질 채점 — 루브릭 100점.

루브릭: 9-space 커버리지(25) · 공간 균형(10) · 소스 커버리지(20) ·
        근거 추적성(20) · 참조 무결성(15) · 규모/풍부도(10).

**이름이 `grade_pack` 인 이유.** 이 패키지에는 이미 `opencrab/ontology/pack_registry.py`
의 `_score_pack`(질의 적합도 랭킹)이 있고, 그쪽은 `float` 를 돌려주며 pack_id 완전일치에
`+100.0` 을 준다 — 이 루브릭의 "100점 만점"과 **숫자까지 같다.** 같은 이름이면 리뷰어와
미래의 자신을 확실히 속인다. 신규 진입자가 양보한다.

`opencrab/pack/assembler.py` 의 `_quality_report` 와도 다르다. 그쪽은 조립 산출물의
자체 점검(status pass/warn, checks 6종)이고, 이쪽은 **교환 포맷 팩의 품질 등급**이다.

**판정만 하고 출력하지 않는다.** 형식·argv·종료코드는 호출자 CLI 몫이다. 이전 판은
파일 전체가 모듈 최상위라 import 즉시 argv 를 읽고 `sys.exit(2)` 했다 — 라이브러리로
쓸 수 없었고, import 만으로 죽는 형태였다.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from opencrab.pack.jsonl_io import iter_jsonl

NSPACES = ["subject", "community", "policy", "claim", "concept",
           "resource", "evidence", "lever", "outcome"]


def grade_pack(pack_dir: Path | str, expected_sources: int | None = None) -> dict[str, Any]:
    """팩 하나를 채점한다. `nodes.jsonl` 이 없으면 `None`.

    반환 dict: `total`(0~100) · `sections`(항목별 점수) · `report`(표시용 줄 목록) ·
    `gaps`(감점 사유) · `counts`.

    `expected_sources` 생략 시 resource 노드 수로 자동 산정한다.
    """
    d = Path(pack_dir)

    def load(name):
        return list(iter_jsonl(d / name, missing_ok=True))  # shard-aware

    nodes = load("nodes.jsonl")
    edges = load("edges.jsonl")
    chunks = load("chunks.jsonl")
    if not nodes:
        return None

    force_src = expected_sources
    # 엣지 포맷 정규화: 대량 import 포맷(from_id/to_id/relation) → 채점 포맷(source_id/target_id/label)
    for e in edges:
        if "source_id" not in e and "from_id" in e:
            e['source_id'] = e['from_id']
        if "target_id" not in e and "to_id"   in e:
            e['target_id'] = e['to_id']
        if "label" not in e and "relation" in e:
            e['label'] = e['relation']

    nid = {n["id"]: n for n in nodes}
    by_space = defaultdict(list)
    for n in nodes:
        by_space[n['space']].append(n)
    res_ids = {n["id"] for n in nodes if n["space"] == "resource"}
    ev_ids  = {n["id"] for n in nodes if n["space"] == "evidence"}

    report, gaps = [], []
    def line(s):
        report.append(s)

    # ── 1. 9-space 커버리지 (25) ──
    s1 = 25
    for sp in NSPACES:
        cnt = len(by_space.get(sp, []))
        if cnt == 0:
            s1 -= 8
            gaps.append(f"[space] '{sp}' 공간 비어있음")
        elif cnt < 3:
            s1 -= 3
            gaps.append(f"[space] '{sp}' 노드 {cnt}개(<3)")
    s1 = max(0, s1)
    line(f"1. 9-space 커버리지     : {s1}/25   ({ {sp:len(by_space.get(sp,[])) for sp in NSPACES} })")

    # ── 2. 공간 균형 (10) — evidence 제외 ──
    non_ev = [n for n in nodes if n["space"] != "evidence"]
    sp_cnt = Counter(n["space"] for n in non_ev)
    total_ne = max(1, len(non_ev))
    top_sp, top_n = sp_cnt.most_common(1)[0]
    ratio = top_n / total_ne
    if ratio <= 0.60:
        s2 = 10
    else:
        s2 = max(0, round(10 - (ratio - 0.60) * 50))
        gaps.append(f"[balance] '{top_sp}'가 비-evidence의 {ratio:.0%} 점유(>60%)")
    line(f"2. 공간 균형            : {s2}/10   (최다 비-evidence space '{top_sp}' {ratio:.0%})")

    # ── 3. 소스 커버리지 (20) — 청크 보유 소스 / 기대 소스 수 ──
    expected_src = force_src if force_src else max(1, len(res_ids))
    src_with_chunk = {c.get("source") for c in chunks}
    got = len(src_with_chunk)
    cov = min(1.0, got / expected_src)
    s3 = round(20 * cov)
    if got < expected_src:
        gaps.append(f"[source] 청크 보유 소스 {got}/{expected_src}")
    line(f"3. 소스 커버리지        : {s3}/20   (청크 보유 소스 {got}/{expected_src})")

    # ── 4. 근거 추적성 (20) — claim/lever/outcome/policy → evidence 연결 ──
    # 2026-07-16 전수감사: 황금률(추적 타깃=evidence, resource 금지 — generation-rules §③)을
    # 코드로 강제. 종전엔 res_ids도 인정해 claim→resource 팩이 만점을 받았으나 적재 시 해당
    # 엣지는 grammar skip되어 추적성이 실제로는 깨져 있었다(실물: 공공데이터품질관리 CITES→resource).
    trace_spaces = {"claim","lever","outcome","policy"}
    trace_targets = ev_ids
    linked_from = defaultdict(set)
    for e in edges:
        if e["source_id"] in nid and e["target_id"] in trace_targets:
            linked_from[e["source_id"]].add(e["target_id"])
    need = [n for n in nodes if n["space"] in trace_spaces]
    linked = [n for n in need if linked_from.get(n["id"])]
    rate = len(linked) / max(1, len(need))
    s4 = 20 if rate >= 0.90 else max(0, round(20 * rate / 0.90))
    if rate < 0.90:
        unl = [n["label"] for n in need if not linked_from.get(n["id"])][:8]
        gaps.append(f"[trace] 추적성 {rate:.0%}(<90%) 미연결 예: {unl}")
    line(f"4. 근거 추적성          : {s4}/20   ({len(linked)}/{len(need)} = {rate:.0%})")

    # ── 5. 참조 무결성 (15) ──
    viol = 0
    details = []
    for e in edges:
        if e["source_id"] not in nid:
            viol += 1
            details.append(f"edge src 미존재 {e['id'][:8]}")
        if e["target_id"] not in nid:
            viol += 1
            details.append(f"edge tgt 미존재 {e['id'][:8]}")
    for c in chunks:
        if c.get("document_id") not in res_ids:
            viol += 1
            details.append(f"chunk doc_id≠resource {c['id'][:8]}")
        if c["id"] not in ev_ids:
            viol += 1
            details.append(f"chunk id≠evidence {c['id'][:8]}")
    # evidence_index 연속성 (없으면 미인덱싱으로 위반 처리)
    by_src = defaultdict(list)
    missing_idx = 0
    for c in chunks:
        ei = c.get("metadata", {}).get("evidence_index")
        if ei is None:
            missing_idx += 1
        else:
            by_src[c.get("source")].append(ei)
    if missing_idx:
        viol += 1
        details.append(f'evidence_index 미부여 {missing_idx}건')
    for src, idxs in by_src.items():
        idxs = sorted(idxs)
        if idxs != list(range(1, len(idxs)+1)):
            viol += 1
            details.append(f'evidence_index 불연속 @{src}')
    # 노드 커스텀 필드 위치 — 최상위에 펼쳐져 있으면 소비자가 못 읽어 라이브에서 소실된다.
    # 로더(load_local_packs.transform_node)·MCP pack_ingest·build_pack_zip·split_by_pack이
    # 모두 중첩 "properties"만 읽는다. 2026-08-03 실측에서 111개 팩 913,163개 필드가
    # 이 형태로 죽어 있었고 어떤 게이트도 잡지 못했다. 팩당 1건으로 집계한다.
    struct_keys = {"id", "workspace_id", "label", "node_type", "space",
                    "source_type", "created_at", "properties", "degree"}
    stray_keys = Counter()
    for n in nodes:
        for k in n:
            if k not in struct_keys:
                stray_keys[k] += 1
    s5 = max(0, 15 - viol * 3)
    # 감점하지 않고 경고만 한다. 감점하면 -3이 pack_gate의 SCORE_THRESHOLD(90)를 넘겨
    # brain-science 등 5팩을 신규 FAIL로 만들고, gate_check_packs가 그 팩들을 적재 대상에서
    # 뺀다. 로더가 레거시 최상위를 흡수하게 만든 목적과 정면으로 상쇄된다(2026-08-03 실측:
    # HEAD PASS 109 -> 104). 형태 이탈 차단은 생산자 계층(pack_lib.validate)이 맡는다.
    if stray_keys:
        gaps.append(f"[shape] 노드 최상위 비구조 키 {len(stray_keys)}종 {sum(stray_keys.values())}건 "
                    f"{sorted(stray_keys)[:5]} — 로더가 흡수하므로 라이브 도달은 되나 "
                    f"재빌드로 중첩 properties에 옮기는 것을 권장 (감점 없음)")
    if viol:
        gaps.append(f'[integrity] 위반 {viol}건: {details[:5]}')
    line(f"5. 참조 무결성          : {s5}/15   (위반 {viol}건)")

    # ── 6. 규모/풍부도 (10) ──
    s6 = 10
    for label, val, thr in [("nodes",len(nodes),250),("edges",len(edges),300),("chunks",len(chunks),120)]:
        if val < thr:
            s6 -= 3
            gaps.append(f'[scale] {label}={val}(<{thr})')
    s6 = max(0, s6)
    line(f"6. 규모/풍부도          : {s6}/10   (nodes={len(nodes)} edges={len(edges)} chunks={len(chunks)})")

    total = s1+s2+s3+s4+s5+s6
    line("─"*52)
    line(f"   node_type: {dict(Counter(n['node_type'] for n in nodes))}")
    if gaps:
        line("\n[갭 목록]")
        for g in gaps:
            line('  - ' + g)
    line("\n"+"="*52)
    return {
        "total": total,
        "sections": {"space": s1, "balance": s2, "source": s3,
                     "trace": s4, "integrity": s5, "scale": s6},
        "report": report,
        "gaps": gaps,
        "counts": {"nodes": len(nodes), "edges": len(edges), "chunks": len(chunks)},
    }

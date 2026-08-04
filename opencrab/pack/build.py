"""공용 9-space 팩 빌더. 각 build_*.py 에서 ``Pack(slug, title)`` 로 쓴다.

uid 는 팩 슬러그를 네임스페이스로 써서 팩 간 노드 id 충돌을 막는다.

출력 형식의 정본은 :mod:`opencrab.pack.schema`(레코드 모양)와
:mod:`opencrab.pack.jsonl_io`(물리 레이아웃)다. 이 모듈은 그 계약대로 쓰는 쪽이며
집합·표를 재선언하지 않는다 — 재선언이 두 번의 실사고를 만든 원인이다.
"""

import json
import os
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from opencrab.pack.jsonl_io import write_jsonl_sharded
from opencrab.pack.schema import (
    ALL_SPACES,
    ALLOWED,
    FIX,
    KEEP,
    NODE_STRUCT_KEYS,
    NODE_TYPE_OVERRIDE,
    RESERVED_NODE_KEYS,
    TRACE_SRC,
    validate_node_props,
)

# 아래 언더스코어 별칭은 이관 전 이름이다. 본문을 손대지 않고 계약을 schema 로
# 옮기기 위해 유지한다 — 이름 일괄 치환을 섞으면 "그대로 옮겼다"는 diff 검증이 무의미해진다.
_ALLOWED = ALLOWED
_FIX = FIX
_KEEP = KEEP
_TRACE_SRC = TRACE_SRC
_NTO = NODE_TYPE_OVERRIDE
_NODE_STRUCT_KEYS = NODE_STRUCT_KEYS
_RESERVED_NODE_KEYS = RESERVED_NODE_KEYS

_NS = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')

# 이 모듈 안에서는 안 쓰지만 **모듈 속성으로 노출돼야 하는** 이름들.
# 기존 빌더들이 `pack_lib.json.dumps(...)`, `pack_lib.ALL_SPACES`,
# `pack_lib._NODE_STRUCT_KEYS` 처럼 모듈을 통해 접근한다(실측: json 3곳,
# ALL_SPACES 2곳, _NODE_STRUCT_KEYS 1곳). __all__ 에 올려 린터가 미사용으로
# 지우지 못하게 하고, 지우면 무엇이 깨지는지 여기 남긴다.
__all__ = [
    'Pack', 'ALL_SPACES',
    'json', '_NODE_STRUCT_KEYS', '_RESERVED_NODE_KEYS',
]


class Pack:
    def __init__(self, slug, title, source_type='reference-public', out_root=None):
        # 출력 루트는 반드시 명시(인자 또는 PACK_OUT_ROOT env)한다.
        #
        # 이전에는 `Path(__file__).parents[2] / 'by-pack'` 으로 자동 유도했다. 그 코드가
        # opencrab-dump 안에 있는 동안에는 맞았지만, 이 모듈이 localcrab 으로 옮겨온
        # 지금은 localcrab 리포 안에 by-pack 을 만들고 mkdir(exist_ok=True) 라 **예외
        # 없이 조용히 성공**한다. 팩이 엉뚱한 곳에 쌓이는데 아무도 모른다.
        # opencrab-dump 의 shim 이 자기 __file__ 기준으로 PACK_OUT_ROOT 기본값을 넣어주므로
        # 기존 빌더 호출부는 그대로 돈다.
        if out_root is None:
            out_root = os.environ.get('PACK_OUT_ROOT')
        if not out_root:
            raise ValueError(
                'Pack(out_root=...) 또는 PACK_OUT_ROOT env 로 출력 루트를 지정하라. '
                '자동 유도는 리포를 넘는 순간 조용히 틀린 곳에 쓴다.')
        self.slug = slug
        self.title = title
        self.source_type = source_type
        self.out = Path(out_root) / slug
        self.out.mkdir(parents=True, exist_ok=True)
        self.now = datetime.now(UTC).isoformat()
        self.nodes, self.edges, self.chunks = [], [], []
        self._nid, self._ek = set(), set()
        self._evidx = {}
        self._space = {}                 # id → space (grammar 정합화용)
        self._ev_of = {}                 # resource id → [evidence id] (traceability 리타겟용)
        self._eskip = Counter()          # 정합 불가로 드롭된 엣지 (raw_label, ss, ts)

    def uid(self, *p):
        return str(uuid.uuid5(_NS, ':'.join([self.slug] + [str(x) for x in p])))

    def node(self, nid, label, node_type, space, props=None):
        """노드 1건 기록. props는 **평면 dict**로 넘긴다 (예: {'mst': '268103'}).

        props는 노드의 중첩 "properties" 키에 담긴다. 최상위로 펼치지 않는다.
        이것이 저장소 정본 형태다 — 로더(load_local_packs.transform_node),
        MCP pack_ingest, build_pack_zip, split_by_pack이 모두 중첩 "properties"만 읽는다.
        2026-08-03 이전 pack_lib은 최상위로 펼쳤고, 그 결과 pack_lib으로 만든 팩의
        노드 커스텀 필드가 라이브 그래프에 하나도 실리지 않았다(실측 확인).
        """
        if nid in self._nid:
            return nid
        p = dict(props) if props else {}
        # 규약 검사는 schema 가 정본이다(예전에는 여기 인라인이라 소비자 쪽 검사와
        # 어긋날 수 있었다). PackSchemaError 는 ValueError 하위라 기존 except 절이 그대로 잡는다.
        validate_node_props(nid, p)
        self._nid.add(nid)
        self._space[nid] = space
        # source_type은 최상위 구조 필드다. pop해서 중첩에 중복 기록되지 않게 한다
        # (남겨두면 라이브 노드에 의미 없는 중복 키가 생긴다).
        rec = {'id': nid, 'workspace_id': self.slug, 'label': label[:300],
               'node_type': node_type, 'space': space,
               'source_type': p.pop('source_type', self.source_type),
               'created_at': self.now}
        if p:
            rec['properties'] = p
        self.nodes.append(rec)
        return nid

    def edge(self, src, tgt, label, props=None):
        """grammar(localcrab manifest) 정합 자동화:
        공간쌍에 맞는 relation으로 치환(원본은 source_label 보존), 방향이 맞으면 reverse,
        정합 불가 공간쌍은 드롭(_eskip 기록). traceability(claim/lever/outcome/policy→evidence)는 정방향 유지."""
        ss, tt = self._space.get(src), self._space.get(tgt)
        # traceability(claim/lever/outcome/policy)가 resource를 가리키면 그 resource의 evidence로 리타겟
        if ss in _TRACE_SRC and tt == 'resource' and self._ev_of.get(tgt):
            tgt = self._ev_of[tgt][0]
            tt = 'evidence'
        raw, rel = label, label
        if ss and tt:
            allowed = _ALLOWED.get((ss, tt))
            if allowed and label.lower() in allowed:
                rel = label.lower()
            elif (ss, tt) in _KEEP:
                rel = _KEEP[(ss, tt)]                       # 로더가 reverse 처리 (claim→evidence)
            elif allowed:
                rel = _FIX.get((ss, tt)) or sorted(allowed)[0]
            elif _ALLOWED.get((tt, ss)) and not (ss in _TRACE_SRC and tt in ('evidence', 'resource')):
                src, tgt, ss, tt = tgt, src, tt, ss          # 공간쌍 없음 → 방향 반전(traceability는 정방향 유지)
                rel = _FIX.get((ss, tt)) or sorted(_ALLOWED[(ss, tt)])[0]
            else:
                self._eskip[(raw, ss, tt)] += 1              # 정합 불가 → 드롭
                return
        k = f'{src}|{rel}|{tgt}'
        if k in self._ek:
            return
        self._ek.add(k)
        p = dict(props or {})
        if rel != raw:
            p['source_label'] = raw
        self.edges.append({'id': self.uid('edge', src, rel, tgt), 'workspace_id': self.slug,
                           'source_id': src, 'target_id': tgt, 'label': rel,
                           'created_at': self.now, 'properties': p})

    def ev(self, eid, doc_id, label, text, extra=None, ntype='TextEvidence', node_props=None):
        """evidence 노드 + 청크 + contains 엣지를 함께 생성한다.
        extra: 청크(chunks.jsonl) metadata에만 병합되는 부가정보(예: evidence_index 옆 필드).
        node_props: evidence 노드(nodes.jsonl) properties에만 펼쳐지는 부가정보(예: mst, article).
        두 값은 서로 독립이며 자동으로 복사되지 않는다 — 노드와 청크 양쪽에 싣고 싶으면 호출자가
        같은 dict(또는 각각 dict)를 extra와 node_props 양쪽에 명시적으로 넘겨야 한다.
        node_props 생략 시(기본값 None) 노드 생성 동작은 이전과 동일하다.
        주의: eid가 이미 등록돼 있으면 아무것도 만들지 않고 그 id만 반환한다.
        이때 extra와 node_props는 조용히 무시되며 반환값으로는 구분할 수 없다.
        같은 eid에 뒤늦게 속성을 덧붙이려는 용도로 재호출하지 마라."""
        if eid in self._nid:
            return eid
        self._evidx[doc_id] = self._evidx.get(doc_id, 0) + 1
        self.node(eid, label, ntype, 'evidence', node_props)
        self._ev_of.setdefault(doc_id, []).append(eid)
        self.edge(doc_id, eid, 'contains')
        meta = {'evidence_index': self._evidx[doc_id], 'char_start': 0,
                'char_end': len(text), 'source_package_title': self.title, **(extra or {})}
        self.chunks.append({'id': eid, 'document_id': doc_id, 'workspace_id': self.slug,
                            'text': text.strip(), 'source': doc_id, 'source_type': self.source_type,
                            'created_at': self.now, 'metadata': meta})
        return eid

    def resource(self, slug, label, nt='Document', props=None):
        rid = self.uid('resource', slug)
        self.node(rid, label, nt, 'resource', props)
        return rid

    def subject(self, slug, label, nt='Org', props=None):
        sid = self.uid('subject', slug)
        self.node(sid, label, nt, 'subject', props)
        return sid

    def concept(self, slug, label, desc=''):
        nid = self.uid('concept', slug)
        self.node(nid, label, 'Concept', 'concept', {'description': desc} if desc else None)
        return nid

    def claim(self, slug, label, desc=''):
        nid = self.uid('claim', slug)
        self.node(nid, label, 'Claim', 'claim', {'description': desc} if desc else None)
        return nid

    def community(self, slug, label, desc=''):
        nid = self.uid('community', slug)
        self.node(nid, label, 'Community', 'community', {'description': desc} if desc else None)
        return nid

    def outcome(self, slug, label, desc=''):
        nid = self.uid('outcome', slug)
        self.node(nid, label, 'Outcome', 'outcome', {'description': desc} if desc else None)
        return nid

    def lever(self, slug, label, desc=''):
        nid = self.uid('lever', slug)
        self.node(nid, label, 'Lever', 'lever', {'description': desc} if desc else None)
        return nid

    def policy(self, slug, label, desc=''):
        nid = self.uid('policy', slug)
        self.node(nid, label, 'Policy', 'policy', {'description': desc} if desc else None)
        return nid


    def validate(self, strict=None):
        """빌드 결과 정합성 검증(build-time QA gate). 기본은 경고+요약 출력만 하고 빌드를 막지 않는다.
        strict=True 또는 env PACK_LIB_STRICT=1 이면 (a)grammar 위반·(c)dangling 참조에서 예외 발생.
        (b)evidence_refs 누락·(d)9-space 커버리지는 항상 경고(빌드 차단 안 함).
        self.nodes/edges/chunks 를 변경하지 않으므로 save() 출력 파일 내용에는 영향 없음."""
        if strict is None:
            strict = os.environ.get('PACK_LIB_STRICT') == '1'
        errors = []

        # (e) 노드 properties 형태 — 커스텀 필드가 최상위에 펼쳐져 있으면 로더가 통째로 버린다.
        # 이 검사가 없어서 pack_lib 팩 전량의 노드 커스텀 필드가 라이브에서 죽어 있었고
        # 어떤 게이트에서도 안 걸렸다(2026-08-03 발각). 생산자 이탈을 여기서 막는다.
        stray = Counter()
        for n in self.nodes:
            for k in n:
                if k not in _NODE_STRUCT_KEYS:
                    stray[k] += 1
        if stray:
            msg = (f'노드 최상위에 비구조 키 {len(stray)}종 {sum(stray.values())}건: '
                   f'{sorted(stray)[:8]}. 커스텀 필드는 중첩 "properties"에 넣어야 로더가 읽는다')
            print(f'  ⚠ {msg}')
            errors.append(msg)          # strict 여부와 무관하게 차단 — 조용한 데이터 유실이다

        # (c) dangling node refs — 엣지가 참조하는 노드가 실제 등록 안 됨
        dangling = [e for e in self.edges if e['source_id'] not in self._nid or e['target_id'] not in self._nid]
        if dangling:
            msg = f'dangling 노드 참조 엣지 {len(dangling)}건 (edge() 호출이 node() 등록 전에 일어났을 가능성)'
            print(f'  ⚠ {msg}')
            if strict:
                errors.append(msg)

        # (a) grammar 위반 + silent FIX 치환 집계 (edge()가 space 미확정 시 미검증으로 통과시킨 케이스 탐지)
        viol, fixed = Counter(), Counter()
        for e in self.edges:
            ss, tt = self._space.get(e['source_id']), self._space.get(e['target_id'])
            if ss is None or tt is None:
                continue
            label = e['label']
            ok = label in _ALLOWED.get((ss, tt), ()) or _KEEP.get((ss, tt)) == label
            if not ok:
                viol[(ss, tt, label)] += 1
            src_label = e.get('properties', {}).get('source_label')
            if src_label and src_label != label:
                fixed[(ss, tt, src_label, label)] += 1
        if viol:
            n = sum(viol.values())
            msg = f'grammar 위반 엣지 {n}건 (space쌍 검증 우회 — 호출 순서 확인 필요)'
            print(f'  ⚠ {msg}:')
            for (ss, tt, lab), c in viol.most_common(8):
                print(f'      {c:4}  {ss}→{tt}  {lab}')
            if strict:
                errors.append(msg)
        if fixed:
            n = sum(fixed.values())
            print(f'  ℹ grammar 자동치환(FIX) 엣지 {n}건 (원본 라벨 → 정합 라벨, 지금까지 조용히 처리되던 부분):')
            for (ss, tt, raw, rel), c in fixed.most_common(8):
                print(f'      {c:4}  {ss}→{tt}  {raw} → {rel}')

        # (b) claim/concept 중 evidence 연결(evidence_refs) 없는 노드 — 경고만
        ev_touch = set()
        for e in self.edges:
            ss, tt = self._space.get(e['source_id']), self._space.get(e['target_id'])
            if ss == 'evidence':
                ev_touch.add(e['target_id'])
            if tt == 'evidence':
                ev_touch.add(e['source_id'])
        need = [n for n in self.nodes if n['space'] in ('claim', 'concept')]
        unlinked = [n for n in need if n['id'] not in ev_touch]
        if unlinked:
            print(f'  ⚠ evidence_refs 없는 claim/concept {len(unlinked)}/{len(need)}건')

        # (d) 9-space 커버리지 리포트 — 경고만
        space_cnt = Counter(n['space'] for n in self.nodes)
        empty = [sp for sp in ALL_SPACES if space_cnt.get(sp, 0) == 0]
        if empty:
            print(f'  ⚠ 9-space 비어있음: {empty}')

        # (e) 로더 node_type remap 함정 — 선언 space와 로더 NODE_TYPE_OVERRIDE 후 space 불일치.
        #     적재 시 로더가 노드 space를 바꿔 연결 엣지가 대량 skip된다(예: evidence 노드에
        #     node_type='TextUnit' → 로더가 concept으로 remap → 엣지 grammar 위반 skip).
        #
        #     이관 전에는 이 표가 적재기에만 있어 `try: from scripts.ops.load_local_packs
        #     import NODE_TYPE_OVERRIDE / except: _NTO = None` 으로 가져왔다. 즉 적재기를
        #     import 할 수 없는 환경(=대부분의 빌드 환경)에서는 이 검사가 **통째로 조용히
        #     꺼져** 있었다. 이제 표가 schema 에 있으므로 항상 돈다.
        hazard = Counter()
        for n in self.nodes:
            nt = n.get('node_type')
            if nt in _NTO and _NTO[nt][0] != n['space']:
                hazard[(n['space'], nt, _NTO[nt][0])] += 1
        if hazard:
            total = sum(hazard.values())
            msg = (f'로더 node_type remap 함정 {total}건 — 선언 space와 로더 remap 후 space가 달라 '
                   f'적재 시 연결 엣지가 skip됨')
            print(f'  ⚠ {msg}:')
            for (sp, nt, rsp), c in hazard.most_common(8):
                print(f'      {c:4}  node_type={nt!r}: 선언 {sp} → 로더 {rsp}  (권장: {sp}에 맞는 타입, 예 evidence→TextEvidence)')
            if strict:
                errors.append(msg)

        if errors:
            # (e) 노드 properties 형태는 strict와 무관하게 항상 차단하므로 문구를 뭉뚱그리지 않는다.
            # 예전 문구가 'PACK_LIB_STRICT'만 언급해 env를 끄면 우회된다고 오해할 여지가 있었다.
            hint = ('PACK_LIB_STRICT=1 항목 또는 항상 차단 항목' if strict
                    else '항상 차단 항목(strict 무관)')
            raise ValueError(f'pack validate 실패 [{hint}]: ' + '; '.join(errors))

    def save(self):
        self.validate()
        # shard-aware rewrite: 총량이 SHARD_LIMIT(기본 40MB) 이하면 단일 파일 그대로,
        # 초과면 nodes.00.jsonl … 로 분할(base 제거) — GitHub 50MB 경고/100MB 리밋 대응.
        write_jsonl_sharded(self.out/'nodes.jsonl', self.nodes)
        write_jsonl_sharded(self.out/'edges.jsonl', self.edges)
        write_jsonl_sharded(self.out/'chunks.jsonl', self.chunks)
        print(f'FINAL {self.slug}: {len(self.nodes)} nodes, {len(self.edges)} edges, {len(self.chunks)} chunks')
        print('  spaces:', dict(Counter(n['space'] for n in self.nodes)))
        if self._eskip:
            drop = sum(self._eskip.values())
            print(f'  ⚠ grammar 드롭 엣지 {drop}건 (정합 공간쌍 없음):')
            for (lab, ss, ts), n in self._eskip.most_common(8):
                print(f'      {n:4}  {lab}  {ss}→{ts}')

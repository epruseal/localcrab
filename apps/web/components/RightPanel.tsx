'use client'

import { useEffect, useState } from 'react'
import type { MutableRefObject } from 'react'
import type { OcNode, SourceType } from '../lib/api'
import { query, ingestSource, UnauthorizedError, ApiError } from '../lib/api'
import type { AuthState } from '../hooks/useDataChannel'

const SPACES = ['subject','resource','concept','evidence','outcome','lever','policy','claim','community']
const SPACE_COLOR: Record<string, string> = {
  subject:'#f8c537', resource:'#83a598', concept:'#b8bb26', evidence:'#bdae93',
  outcome:'#fb4934', lever:'#d3869b', policy:'#fabd2f', claim:'#fe8019', community:'#8ec07c',
}

// §149 F1: the fetch-level AbortError is caught and re-wrapped into
// ApiError('Request aborted') inside api.ts's shared requestJson (so every
// caller sees one error taxonomy, not fetch's raw DOMException). The
// DOMException/`name === 'AbortError'` checks are kept too so this still
// recognizes an abort if it ever reaches here unwrapped.
function isAbortError(e: unknown): boolean {
  if (typeof DOMException !== 'undefined' && e instanceof DOMException && e.name === 'AbortError') return true
  if (e instanceof Error && e.name === 'AbortError') return true
  if (e instanceof ApiError && e.message === 'Request aborted') return true
  return false
}

interface GraphControls {
  nodeSize: number
  linkStrength: number
  centerForce: number
  repelForce: number
  searchTerm: string
  hiddenSpaces: string[]
}

interface Props {
  selectedNode: OcNode | null
  controls: GraphControls
  onControlChange: (c: Partial<GraphControls>) => void
  // The confirmed token (§149 design 4.4: prop renamed from apiKey, carries
  // useTokenSession's activeToken -- never the still-debouncing tokenInput).
  authToken: string
  authState: AuthState
  tokenPending: boolean
  // Not state -- read at call time and re-checked after each await so a
  // response from an identity that has since changed is discarded (design
  // 4.4: "모든 응답 처리에 epoch가드를 건다").
  authEpochRef: MutableRefObject<number>
  // §149 F1: acquires a 10s-timeout AbortController for one action request
  // (query or ingest), registered in useDataChannel's actionControllersRef
  // so an epoch rise aborts it too. Stable identity (useCallback in the
  // hook).
  acquireRequestController: () => { signal: AbortSignal; release: () => void }
  // §3.4's "401 판정" transition, reached here the same way a data-channel
  // 401 reaches it, since query/ingest are outside that bundle.
  onUnauthorized: () => void
  // §3.7's mutation-success procedure (seq++, abort+release, requery) --
  // NOT a plain refetch, so a successful ingest cannot be raced by a
  // response the graph poll had already sent before the ingest landed.
  onMutationSuccess: () => void
}

export default function RightPanel({
  selectedNode, controls, onControlChange,
  authToken, authState, tokenPending, authEpochRef, acquireRequestController, onUnauthorized, onMutationSuccess,
}: Props) {
  const [tab, setTab] = useState<'detail' | 'query' | 'ingest'>('detail')
  const [queryText, setQueryText] = useState('')
  const [queryResults, setQueryResults] = useState<{ node_id: string; score: number; text: string }[]>([])
  const [querying, setQuerying] = useState(false)
  const [ingestSourceType, setIngestSourceType] = useState<SourceType>('obsidian')
  const [ingestToken, setIngestToken] = useState('')
  const [ingestQuery, setIngestQuery] = useState('')
  const [ingesting, setIngesting] = useState(false)
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null)

  const actionsBlocked = authState !== 'ok' || tokenPending

  // §3.4: a token change (or a 401 verdict) clears query results too.
  useEffect(() => { setQueryResults([]) }, [authToken])
  useEffect(() => { if (authState === 'invalid') setQueryResults([]) }, [authState])

  function showToast(msg: string, type: 'success' | 'error' = 'success') {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 3000)
  }

  async function handleQuery() {
    if (!queryText.trim() || actionsBlocked) return
    const myEpoch = authEpochRef.current
    const { signal, release } = acquireRequestController()
    setQuerying(true)
    try {
      const res = await query(authToken, queryText, undefined, signal)
      if (myEpoch !== authEpochRef.current) return // stale identity, discard silently
      // Support both response formats
      setQueryResults(res.results ?? res.hits ?? res.chunks ?? [])
    } catch (e) {
      if (myEpoch !== authEpochRef.current) return
      if (e instanceof UnauthorizedError) { onUnauthorized(); return }
      if (isAbortError(e)) { showToast('요청 시간 초과 또는 취소', 'error'); return }
      showToast(String(e), 'error')
    } finally { setQuerying(false); release() }
  }

  async function handleIngest() {
    if (!ingestToken.trim() || actionsBlocked) return
    const myEpoch = authEpochRef.current
    const { signal, release } = acquireRequestController()
    setIngesting(true)
    try {
      await ingestSource(authToken, ingestSourceType, ingestToken, { query: ingestQuery || undefined }, signal)
      if (myEpoch !== authEpochRef.current) return
      showToast('인제스트 완료!')
      setIngestToken('')
      setIngestQuery('')
      onMutationSuccess()
    } catch (e) {
      if (myEpoch !== authEpochRef.current) return
      if (e instanceof UnauthorizedError) { onUnauthorized(); return }
      if (isAbortError(e)) { showToast('요청 시간 초과 또는 취소', 'error'); return }
      showToast(String(e), 'error')
    } finally { setIngesting(false); release() }
  }

  function toggleSpace(space: string) {
    const hidden = controls.hiddenSpaces
    onControlChange({
      hiddenSpaces: hidden.includes(space) ? hidden.filter(s => s !== space) : [...hidden, space],
    })
  }

  const S = (label: string, key: keyof GraphControls, min: number, max: number, step: number) => (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 11, color: '#bdae93' }}>{label}</span>
        <span style={{ fontSize: 11, color: '#f8c537', fontFamily: 'monospace' }}>
          {(controls[key] as number).toFixed(2)}
        </span>
      </div>
      <input
        type="range" min={min} max={max} step={step}
        value={controls[key] as number}
        onChange={e => onControlChange({ [key]: parseFloat(e.target.value) })}
        style={{ width: '100%', accentColor: '#f8c537', cursor: 'pointer' }}
      />
    </div>
  )

  return (
    <div style={{
      width: 260, minWidth: 260, background: '#1a1a1a',
      borderLeft: '1px solid rgba(248,197,55,0.15)',
      display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden',
    }}>
      {/* Tabs */}
      <div style={{ display: 'flex', borderBottom: '1px solid rgba(248,197,55,0.15)' }}>
        {(['detail','query','ingest'] as const).map(t => (
          <button key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}
            style={{ flex: 1, fontSize: 11 }}>
            {t === 'detail' ? '노드' : t === 'query' ? '쿼리' : '인제스트'}
          </button>
        ))}
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 14px' }}>

        {/* DETAIL TAB */}
        {tab === 'detail' && (
          selectedNode ? (
            <div>
              <div style={{ marginBottom: 10 }}>
                <div style={{ fontSize: 10, color: '#555', marginBottom: 4, letterSpacing: '0.06em' }}>NODE ID</div>
                <div className="mono" style={{ fontSize: 12, color: '#faf2d6', wordBreak: 'break-all' }}>{selectedNode.id}</div>
              </div>
              <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
                <span className="badge" style={{ background: `${SPACE_COLOR[selectedNode.space]}22`, color: SPACE_COLOR[selectedNode.space] }}>
                  {selectedNode.space}
                </span>
                <span className="badge">{selectedNode.node_type}</span>
                <span className="badge">{selectedNode.degree_in_view} links in view</span>
              </div>
              <hr className="gold-line" />
              <div style={{ fontSize: 10, color: '#555', marginBottom: 6, letterSpacing: '0.06em' }}>PROPERTIES</div>
              {Object.entries(selectedNode.properties).map(([k, v]) => (
                <div key={k} style={{
                  display: 'flex', gap: 8, padding: '4px 0',
                  borderBottom: '1px solid #222', fontSize: 11,
                }}>
                  <span style={{ color: '#7c6f64', minWidth: 80, flexShrink: 0 }}>{k}</span>
                  <span style={{ color: '#bdae93', wordBreak: 'break-all' }}>{String(v)}</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ color: '#555', fontSize: 12, marginTop: 20, textAlign: 'center' }}>
              그래프에서 노드를 클릭하면<br/>상세 정보가 표시돼
            </div>
          )
        )}

        {/* QUERY TAB */}
        {tab === 'query' && (
          <div>
            <textarea
              className="input-dark"
              value={queryText}
              onChange={e => setQueryText(e.target.value)}
              placeholder="무엇이든 물어봐... (e.g. 전략적 레버는 무엇인가?)"
              style={{ marginBottom: 8, height: 80, fontSize: 12 }}
              onKeyDown={e => { if (e.key === 'Enter' && e.metaKey) handleQuery() }}
            />
            <button className="btn-gold" style={{ width: '100%', marginBottom: 12 }}
              onClick={handleQuery} disabled={querying || actionsBlocked}>
              {querying ? '검색 중…' : '쿼리 ↵'}
            </button>
            <div>
              {queryResults.map((r, i) => (
                <div key={i} style={{
                  padding: '8px 10px', marginBottom: 6,
                  background: '#1f1f1f', borderRadius: 4,
                  border: '1px solid #2e2e2e', fontSize: 11,
                }}>
                  <div style={{ color: '#f8c537', marginBottom: 2 }}>{r.node_id ?? '—'}</div>
                  <div style={{ color: '#bdae93', fontSize: 10, marginBottom: 4 }}>{r.text?.slice(0, 100)}…</div>
                  <div style={{ color: '#555', fontSize: 10 }}>score: {r.score?.toFixed(3)}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* INGEST TAB */}
        {tab === 'ingest' && (
          <div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ fontSize: 11, color: '#7c6f64', display: 'block', marginBottom: 4 }}>소스 타입</label>
              <select className="input-dark" value={ingestSourceType}
                onChange={e => setIngestSourceType(e.target.value as SourceType)}
                style={{ fontSize: 12 }}>
                <option value="obsidian">📓 Obsidian</option>
                <option value="notion">📝 Notion</option>
                <option value="gdrive">📂 Google Drive</option>
                <option value="github">🐙 GitHub</option>
              </select>
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ fontSize: 11, color: '#7c6f64', display: 'block', marginBottom: 4 }}>
                액세스 토큰 {ingestSourceType === 'obsidian' && '(Obsidian Sync API Key)'}
              </label>
              <input className="input-dark mono" value={ingestToken}
                onChange={e => setIngestToken(e.target.value)}
                placeholder="API 토큰 입력…"
                type="password"
                style={{ fontSize: 11 }}
              />
            </div>
            <div style={{ marginBottom: 8 }}>
              <label style={{ fontSize: 11, color: '#7c6f64', display: 'block', marginBottom: 4 }}>검색어 (선택)</label>
              <input className="input-dark" value={ingestQuery}
                onChange={e => setIngestQuery(e.target.value)}
                placeholder="가져올 데이터 검색어…"
                style={{ fontSize: 11 }}
              />
            </div>
            <button className="btn-gold" style={{ width: '100%' }}
              onClick={handleIngest} disabled={ingesting || !ingestToken.trim() || actionsBlocked}>
              {ingesting ? '처리 중…' : '데이터 가져오기'}
            </button>
            <div style={{ marginTop: 8, fontSize: 10, color: '#555', lineHeight: 1.5 }}>
              선택한 소스의 데이터를 GraphRAG 온톨로지로 변환해 저장해
            </div>
          </div>
        )}

        {/* Graph Controls — always visible below */}
        <hr className="gold-line" style={{ marginTop: 16 }} />
        <div style={{ fontSize: 10, color: '#555', letterSpacing: '0.08em', marginBottom: 10 }}>그래프 설정</div>

        {/* Search */}
        <div style={{ marginBottom: 12 }}>
          <input className="input-dark" value={controls.searchTerm}
            onChange={e => onControlChange({ searchTerm: e.target.value })}
            placeholder="노드 검색…" style={{ fontSize: 11 }} />
        </div>

        {/* Space filters */}
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 10, color: '#7c6f64', marginBottom: 6 }}>스페이스 필터</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
            {SPACES.map(s => {
              const hidden = controls.hiddenSpaces.includes(s)
              return (
                <button key={s} onClick={() => toggleSpace(s)} style={{
                  padding: '2px 8px', fontSize: 10, borderRadius: 10, cursor: 'pointer',
                  background: hidden ? '#1f1f1f' : `${SPACE_COLOR[s]}22`,
                  color: hidden ? '#555' : SPACE_COLOR[s],
                  border: `1px solid ${hidden ? '#333' : SPACE_COLOR[s]}`,
                  textDecoration: hidden ? 'line-through' : 'none',
                }}>
                  {s}
                </button>
              )
            })}
          </div>
        </div>

        {S('노드 크기', 'nodeSize', 0.5, 3, 0.1)}
        {S('링크 두께', 'linkStrength', 0.1, 1, 0.05)}
        {S('중심 강력', 'centerForce', 0.01, 1, 0.01)}
        {S('반발력', 'repelForce', 50, 500, 10)}
      </div>

      {toast && (
        <div className={`toast toast-${toast.type}`}>{toast.msg}</div>
      )}
    </div>
  )
}

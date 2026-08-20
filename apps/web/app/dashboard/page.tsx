'use client'

import { useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import FileExplorer from '../../components/FileExplorer'
import RightPanel from '../../components/RightPanel'
import PackPanel from '../../components/PackPanel'
import type { OcNode } from '../../lib/api'
import { useTokenSession } from '../../hooks/useTokenSession'
import { useDataChannel } from '../../hooks/useDataChannel'

const GraphView = dynamic(() => import('../../components/GraphView'), { ssr: false })

interface GraphControls {
  nodeSize: number
  linkStrength: number
  centerForce: number
  repelForce: number
  searchTerm: string
  hiddenSpaces: string[]
}

function graphAgeText(graphLoadedAt: number | null): string {
  if (!graphLoadedAt) return '그래프를 아직 불러오지 못했어.'
  const t = new Date(graphLoadedAt).toLocaleTimeString()
  return `표시 중인 그래프는 ${t} 시점 스냅샷이며 최신이 아닐 수 있어.`
}

export default function DashboardPage() {
  const tokenSession = useTokenSession()
  const dataChannel = useDataChannel(tokenSession.activeToken, tokenSession.hydrated)

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [controls, setControls] = useState<GraphControls>({
    nodeSize: 1,
    linkStrength: 0.3,
    centerForce: 0.1,
    repelForce: 200,
    searchTerm: '',
    hiddenSpaces: [],
  })
  const [showIngest, setShowIngest] = useState(false)

  const visibleNodes = useMemo(
    () => dataChannel.nodes.filter(n => !controls.hiddenSpaces.includes(n.space)),
    [dataChannel.nodes, controls.hiddenSpaces]
  )

  const selectedNode = visibleNodes.find(n => n.id === selectedId) ?? null

  function handleNodeClick(node: OcNode) {
    setSelectedId(node.id)
  }

  function handleControlChange(partial: Partial<GraphControls>) {
    setControls(p => ({ ...p, ...partial }))
  }

  // §149 design 4.2: missing/invalid/checking all cover the canvas -- a
  // stale-looking empty graph during `checking` reads as "I have no data"
  // otherwise, and missing/invalid need the user's attention before
  // anything underneath is usable.
  const overlay = dataChannel.authState

  return (
    <div style={{
      display: 'flex', height: '100vh', width: '100vw',
      background: '#111', overflow: 'hidden',
    }}>
      {/* Left — File Explorer + Packs */}
      <div style={{ width: 260, minWidth: 260, height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <FileExplorer
          nodes={visibleNodes}
          selectedId={selectedId}
          onNodeSelect={id => setSelectedId(id)}
          onIngestClick={() => setShowIngest(true)}
          connected={dataChannel.connected}
          authToken={tokenSession.tokenInput}
          onAuthTokenChange={tokenSession.onTokenInputChange}
          storageNotice={tokenSession.storageNotice}
        />
        <PackPanel
          packs={dataChannel.packs}
          loading={dataChannel.authState === 'checking'}
          error={dataChannel.packError}
          onVisibilityChange={dataChannel.changePackVisibility}
        />
      </div>

      {/* Center — Graph */}
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
        {/* Top bar */}
        <div style={{
          position: 'absolute', top: 0, left: 0, right: 0, zIndex: 10,
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '8px 14px',
          background: 'rgba(17,17,17,0.9)',
          borderBottom: '1px solid rgba(248,197,55,0.12)',
        }}>
          <span style={{ fontSize: 12, color: '#555' }}>그래프 뷰</span>
          <span style={{ fontSize: 11, color: '#3a3a3a' }}>|</span>
          <span style={{ fontSize: 11, color: '#7c6f64' }}>
            {visibleNodes.length} nodes · {dataChannel.edges.length} edges
          </span>
          <div style={{ flex: 1 }} />
          <input
            className="input-dark"
            value={controls.searchTerm}
            onChange={e => handleControlChange({ searchTerm: e.target.value })}
            placeholder="검색…"
            style={{ width: 180, fontSize: 11, padding: '4px 10px' }}
          />
          <button className="btn-gold" style={{ fontSize: 11, padding: '4px 10px' }} onClick={() => dataChannel.refresh()}>
            ↺ 새로고침
          </button>
        </div>

        {/* Error banner — only surfaced once auth is confirmed ok; missing/
            invalid/checking are handled by the overlay below instead
            (design 3.6). */}
        {dataChannel.authState === 'ok' && (dataChannel.graphError || dataChannel.packError) && (
          <div style={{
            position: 'absolute', top: 42, left: 0, right: 0, zIndex: 15,
            padding: '6px 14px',
            background: 'rgba(251,73,52,0.12)',
            borderBottom: '1px solid rgba(251,73,52,0.3)',
            fontSize: 11, color: '#fb4934',
          }}>
            {dataChannel.graphError && <div>{graphAgeText(dataChannel.graphLoadedAt)}</div>}
            {dataChannel.packError && <div>팩 목록을 갱신하지 못했어.</div>}
          </div>
        )}

        {/* Graph canvas */}
        <div style={{ position: 'absolute', inset: 0, paddingTop: 42 }}>
          <GraphView
            nodes={visibleNodes}
            edges={dataChannel.edges}
            selectedId={selectedId}
            searchTerm={controls.searchTerm}
            nodeSize={controls.nodeSize}
            linkStrength={controls.linkStrength}
            centerForce={controls.centerForce}
            repelForce={controls.repelForce}
            onNodeClick={handleNodeClick}
          />
        </div>

        {/* Legend */}
        <div style={{
          position: 'absolute', top: 50, right: 10, zIndex: 10,
          background: 'rgba(17,17,17,0.85)',
          border: '1px solid rgba(248,197,55,0.15)',
          borderRadius: 6, padding: '8px 12px',
        }}>
          {[
            ['Landscape', '#5ea85b'],
            ['AI', '#e38b2c'],
            ['Alex', '#d97ab5'],
            ['Fallback', '#7c6f64'],
          ].map(([s, c]) => (
            <div key={s} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: c }} />
              <span style={{ fontSize: 10, color: '#bdae93' }}>{s}</span>
            </div>
          ))}
        </div>

        {/* missing/invalid/checking guidance — covers the canvas, above the
            top bar & legend, below the ingest modal (design 4.2). */}
        {(overlay === 'missing' || overlay === 'invalid' || overlay === 'checking') && (
          <div style={{
            position: 'absolute', inset: 0, zIndex: 50,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: 'rgba(17,17,17,0.96)',
          }}>
            <div style={{ maxWidth: 360, textAlign: 'center', padding: 24 }}>
              {overlay === 'checking' && (
                <>
                  <div style={{ color: '#f8c537', fontWeight: 700, marginBottom: 10 }}>토큰을 확인하는 중…</div>
                  <p style={{ color: '#7c6f64', fontSize: 12, lineHeight: 1.6 }}>
                    잠시만 기다려줘.
                  </p>
                </>
              )}
              {overlay === 'missing' && (
                <>
                  <div style={{ color: '#f8c537', fontWeight: 700, marginBottom: 10 }}>사용자 토큰이 필요해</div>
                  <p style={{ color: '#7c6f64', fontSize: 12, lineHeight: 1.6 }}>
                    왼쪽 사이드바에 발급받은 사용자 토큰을 입력해줘.
                  </p>
                </>
              )}
              {overlay === 'invalid' && (
                <>
                  <div style={{ color: '#fb4934', fontWeight: 700, marginBottom: 10 }}>토큰이 무효하거나 폐기됐어</div>
                  <p style={{ color: '#7c6f64', fontSize: 12, lineHeight: 1.6 }}>
                    왼쪽 사이드바에서 유효한 사용자 토큰을 다시 입력해줘.<br />
                    예전에 쓰던 공유 API 키는 더 이상 쓰이지 않아.
                  </p>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Right — Controls & Detail */}
      <RightPanel
        selectedNode={selectedNode}
        controls={controls}
        onControlChange={handleControlChange}
        authToken={tokenSession.activeToken}
        authState={dataChannel.authState}
        tokenPending={tokenSession.tokenPending}
        authEpochRef={dataChannel.authEpochRef}
        onUnauthorized={dataChannel.markInvalid}
        onMutationSuccess={dataChannel.notifyMutationSuccess}
      />

      {/* Ingest Modal */}
      {showIngest && (
        <div
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
          }}
          onClick={() => setShowIngest(false)}
        >
          <div
            style={{
              background: '#1a1a1a', border: '1px solid rgba(248,197,55,0.3)',
              borderRadius: 8, padding: 24, width: 480, maxWidth: '90vw',
            }}
            onClick={e => e.stopPropagation()}
          >
            <div style={{ color: '#f8c537', fontWeight: 700, marginBottom: 16 }}>데이터 인제스트</div>
            <p style={{ color: '#7c6f64', fontSize: 12, marginBottom: 16 }}>
              오른쪽 패널의 인제스트 탭을 사용하거나 여기서 빠르게 추가할 수 있어.
            </p>
            <button className="btn-gold" onClick={() => setShowIngest(false)}>닫기</button>
          </div>
        </div>
      )}
    </div>
  )
}

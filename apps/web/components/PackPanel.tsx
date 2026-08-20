'use client'

import { useState } from 'react'
import type { OcPack, PackVisibility } from '../lib/api'

const VISIBILITY_LABEL: Record<PackVisibility, string> = {
  'private': '비공개',
  'public-read': '공개(읽기)',
  'public-fork': '공개(포크)',
}

const VISIBILITY_COLOR: Record<PackVisibility, string> = {
  'private': '#7c6f64',
  'public-read': '#83a598',
  'public-fork': '#b8bb26',
}

const VISIBILITY_OPTIONS: PackVisibility[] = ['private', 'public-read', 'public-fork']

interface Props {
  packs: OcPack[]
  loading: boolean
  error: string | null
  // Parent owns the central data channel (design #149 §3.7/§5.6: mutationSeq,
  // single-flight, epoch guard) -- this component only asks it to perform the
  // change and reports success/failure back to the caller.
  onVisibilityChange: (packId: string, visibility: PackVisibility) => Promise<void>
}

export default function PackPanel({ packs, loading, error, onVisibilityChange }: Props) {
  // Per-row pending/error state, local to display -- not the packError this
  // panel's `error` prop carries (that one is for the list fetch itself).
  const [pending, setPending] = useState<Record<string, boolean>>({})
  const [rowError, setRowError] = useState<Record<string, string>>({})

  async function handleChange(packId: string, visibility: PackVisibility) {
    setPending(p => ({ ...p, [packId]: true }))
    setRowError(p => {
      const next = { ...p }
      delete next[packId]
      return next
    })
    try {
      await onVisibilityChange(packId, visibility)
    } catch (err) {
      setRowError(p => ({ ...p, [packId]: err instanceof Error ? err.message : '변경 실패' }))
    } finally {
      setPending(p => ({ ...p, [packId]: false }))
    }
  }

  return (
    <div style={{
      display: 'flex', flexDirection: 'column',
      borderTop: '1px solid rgba(248,197,55,0.15)',
    }}>
      <div style={{
        padding: '10px 14px 6px',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <span style={{ color: '#f8c537', fontWeight: 700, fontSize: 11, letterSpacing: '0.05em' }}>
          PACKS
        </span>
        {loading && <span style={{ fontSize: 10, color: '#7c6f64' }}>불러오는 중…</span>}
      </div>

      {error && (
        <div style={{ padding: '2px 14px 8px', fontSize: 11, color: '#fb4934' }}>
          {error}
        </div>
      )}

      <div style={{ maxHeight: 220, overflowY: 'auto', padding: '2px 0 8px' }}>
        {!loading && packs.length === 0 ? (
          <div style={{ padding: '4px 14px', fontSize: 11, color: '#555' }}>팩이 없습니다</div>
        ) : (
          packs.map(pack => (
            <div
              key={pack.pack_id}
              style={{
                display: 'flex', flexDirection: 'column', gap: 2,
                padding: '5px 14px',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{
                  flex: 1, fontSize: 12, color: '#bdae93',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {pack.title}
                </span>
                {pack.is_default && (
                  <span className="badge" style={{ fontSize: 9 }}>기본</span>
                )}
                {pack.is_owner && (
                  <span className="badge" style={{ fontSize: 9, color: '#f8c537' }}>내 것</span>
                )}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <div style={{
                  width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                  background: VISIBILITY_COLOR[pack.visibility],
                }} />
                {pack.is_owner ? (
                  <select
                    className="input-dark"
                    value={pack.visibility}
                    disabled={!!pending[pack.pack_id]}
                    onChange={e => handleChange(pack.pack_id, e.target.value as PackVisibility)}
                    style={{ fontSize: 10, padding: '2px 4px', flex: 1 }}
                  >
                    {VISIBILITY_OPTIONS.map(v => (
                      <option key={v} value={v}>{VISIBILITY_LABEL[v]}</option>
                    ))}
                  </select>
                ) : (
                  <span style={{ fontSize: 10, color: '#7c6f64' }}>
                    {VISIBILITY_LABEL[pack.visibility]}
                  </span>
                )}
              </div>
              {rowError[pack.pack_id] && (
                <span style={{ fontSize: 10, color: '#fb4934' }}>{rowError[pack.pack_id]}</span>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}

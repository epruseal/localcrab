const BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

function headers(apiKey?: string) {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  if (apiKey) h['Authorization'] = `Bearer ${apiKey}`
  return h
}

export interface OcNode {
  id: string
  space: string
  node_type: string
  properties: Record<string, unknown>
  // Links within the returned edge set, not the node total (see /api/nodes).
  degree_in_view: number
}

export interface OcEdge {
  from_id: string
  to_id: string
  relation: string
  from_space: string
  to_space: string
}

export interface QueryResult {
  node_id: string | null
  score: number
  text: string | null
  metadata: Record<string, unknown>
}

export type SourceType = 'obsidian' | 'notion' | 'gdrive' | 'github'

/* ── Status ──────────────────────────────────────────────── */
// #147: /api/status now requires a bearer token. This call is unauthenticated
// (no apiKey param) and is used purely as a connection indicator -- dashboard
// page.tsx reads only `ok.ok` -- so it points at /healthz instead, the
// auth-exempt probe apps/api/main.py added alongside locking /api/status
// down. /healthz returns only {"ok": true}, no storage_mode/store-state
// payload, so `version`/`vectorCount` are never populated; they stay in the
// return type as optional so callers reading them do not break.
export async function getStatus(): Promise<{ ok: boolean; version?: string; vectorCount?: number }> {
  try {
    const r = await fetch(`${BASE}/healthz`, { cache: 'no-store' })
    if (!r.ok) return { ok: false }
    const d = await r.json()
    return { ok: Boolean(d.ok) }
  } catch { return { ok: false } }
}

export async function getDetailedStatus(apiKey: string): Promise<Record<string, unknown>> {
  try {
    const r = await fetch(`${BASE}/status`, { headers: headers(apiKey), cache: 'no-store' })
    if (!r.ok) return {}
    return r.json()
  } catch { return {} }
}

/* ── Query ───────────────────────────────────────────────── */
export async function query(apiKey: string, question: string, topK = 5) {
  const r = await fetch(`${BASE}/api/query`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify({ question, limit: topK }),
  })
  if (!r.ok) {
    const err = await r.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail || 'Query failed')
  }
  return r.json()
}

/* ── Ingest (external sources) ───────────────────────────── */
export async function ingestSource(
  apiKey: string,
  sourceType: SourceType,
  accessToken: string,
  opts: {
    sourceId?: string
    sourceUrl?: string
    query?: string
    maxItems?: number
  } = {}
) {
  const r = await fetch(`${BASE}/api/ingest`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify({
      source_type: sourceType,
      access_token: accessToken,
      source_id: opts.sourceId,
      source_url: opts.sourceUrl,
      query: opts.query,
      max_items: opts.maxItems ?? 25,
    }),
  })
  if (!r.ok) {
    const err = await r.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail || 'Ingest failed')
  }
  return r.json()
}

/* ── Graph nodes/edges (new API — available after redeploy) ─ */
export async function getNodes(apiKey: string): Promise<OcNode[]> {
  try {
    const r = await fetch(`${BASE}/api/nodes`, { headers: headers(apiKey), cache: 'no-store' })
    if (!r.ok) return []
    const d = await r.json()
    return d.nodes ?? []
  } catch { return [] }
}

export async function getEdges(apiKey: string): Promise<OcEdge[]> {
  try {
    const r = await fetch(`${BASE}/api/edges`, { headers: headers(apiKey), cache: 'no-store' })
    if (!r.ok) return []
    const d = await r.json()
    return d.edges ?? []
  } catch { return [] }
}

// Base API origin. NEXT_PUBLIC_* is inlined at build time (Next.js), so this
// value must be baked into the web image via the Dockerfile builder stage's
// NEXT_PUBLIC_API_URL build arg (see apps/web/Dockerfile) -- it cannot be
// changed at container-runtime. The fallback below matches the port the
// default docker-compose stack publishes for the API service (#149 design 2).
const BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001'

function headers(authToken?: string) {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) h['Authorization'] = `Bearer ${authToken}`
  return h
}

/** Thrown for HTTP 401 responses only, so callers can distinguish "this
 *  token is invalid/revoked" from every other failure mode. */
export class UnauthorizedError extends Error {
  constructor(message = 'Unauthorized') {
    super(message)
    this.name = 'UnauthorizedError'
  }
}

/** Thrown for any non-401 abnormal response, network failure, or aborted
 *  request. */
export class ApiError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

/** Shared request path for every authenticated JSON call: performs the
 *  fetch, classifies failures into UnauthorizedError/ApiError, and returns
 *  the parsed body on success. Keeping this in one place is what lets
 *  4.1's "401 -> UnauthorizedError, everything else -> ApiError" rule stay
 *  a single decision instead of one per call site. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function requestJson(
  path: string,
  opts: { method?: string; authToken?: string; body?: unknown; signal?: AbortSignal } = {}
): Promise<any> {
  let r: Response
  try {
    r = await fetch(`${BASE}${path}`, {
      method: opts.method ?? 'GET',
      headers: headers(opts.authToken),
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      cache: 'no-store',
      signal: opts.signal,
    })
  } catch (err) {
    if (opts.signal?.aborted) throw new ApiError('Request aborted')
    throw new ApiError(err instanceof Error ? err.message : 'Network error')
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    const detail = (body as { detail?: string }).detail
    if (r.status === 401) throw new UnauthorizedError(detail || 'Unauthorized')
    throw new ApiError(detail || `Request failed (${r.status})`)
  }
  return r.json()
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

export type PackVisibility = 'private' | 'public-read' | 'public-fork'

export interface OcPack {
  pack_id: string
  title: string
  visibility: PackVisibility
  is_default: boolean
  is_owner: boolean
}

/* ── Status ──────────────────────────────────────────────── */
// #147: /api/status now requires a bearer token. This call is unauthenticated
// (no authToken param) and is used purely as a connection indicator -- dashboard
// page.tsx reads only `ok.ok` -- so it points at /healthz instead, the
// auth-exempt probe apps/api/main.py added alongside locking /api/status
// down. /healthz returns only {"ok": true}, no storage_mode/store-state
// payload, so `version`/`vectorCount` are never populated; they stay in the
// return type as optional so callers reading them do not break.
export async function getStatus(signal?: AbortSignal): Promise<{ ok: boolean; version?: string; vectorCount?: number }> {
  try {
    const r = await fetch(`${BASE}/healthz`, { cache: 'no-store', signal })
    if (!r.ok) return { ok: false }
    const d = await r.json()
    return { ok: Boolean(d.ok) }
  } catch { return { ok: false } }
}

/* ── Query ───────────────────────────────────────────────── */
// Return type intentionally left loose (matches pre-#149 behavior): callers
// currently probe multiple possible response shapes (see RightPanel.tsx,
// out of this change's scope) rather than a single `results` field.
export async function query(authToken: string, question: string, topK = 5, signal?: AbortSignal) {
  return requestJson('/api/query', {
    method: 'POST',
    authToken,
    body: { question, limit: topK },
    signal,
  })
}

/* ── Ingest (external sources) ───────────────────────────── */
export async function ingestSource(
  authToken: string,
  sourceType: SourceType,
  accessToken: string,
  opts: {
    sourceId?: string
    sourceUrl?: string
    query?: string
    maxItems?: number
  } = {},
  signal?: AbortSignal
) {
  return requestJson('/api/ingest', {
    method: 'POST',
    authToken,
    body: {
      source_type: sourceType,
      access_token: accessToken,
      source_id: opts.sourceId,
      source_url: opts.sourceUrl,
      query: opts.query,
      max_items: opts.maxItems ?? 25,
    },
    signal,
  })
}

/* ── Graph nodes/edges ───────────────────────────────────── */
export async function getNodes(authToken: string, signal?: AbortSignal): Promise<OcNode[]> {
  const d = (await requestJson('/api/nodes', { authToken, signal })) as { nodes?: OcNode[] }
  return d.nodes ?? []
}

export async function getEdges(authToken: string, signal?: AbortSignal): Promise<OcEdge[]> {
  const d = (await requestJson('/api/edges', { authToken, signal })) as { edges?: OcEdge[] }
  return d.edges ?? []
}

/* ── Packs ───────────────────────────────────────────────── */
export async function listPacks(authToken: string, signal?: AbortSignal): Promise<OcPack[]> {
  const d = (await requestJson('/api/packs', { authToken, signal })) as { packs?: OcPack[] }
  return d.packs ?? []
}

export async function setPackVisibility(
  authToken: string,
  packId: string,
  visibility: PackVisibility,
  signal?: AbortSignal
): Promise<OcPack> {
  return requestJson(`/api/packs/${encodeURIComponent(packId)}/visibility`, {
    method: 'POST',
    authToken,
    body: { visibility },
    signal,
  }) as Promise<OcPack>
}

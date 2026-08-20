'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { OcNode, OcEdge, OcPack, PackVisibility } from '../lib/api'
import {
  getNodes, getEdges, listPacks, setPackVisibility, getStatus, UnauthorizedError,
} from '../lib/api'

export type AuthState = 'unknown' | 'missing' | 'checking' | 'ok' | 'invalid'

const POLL_INTERVAL_MS = 30000
// No fixed number for the health probe in #149 design 3.5 beyond "runs on
// its own timer, always" -- 10s keeps the connection dot responsive without
// adding a second load-bearing interval to reason about.
const HEALTH_INTERVAL_MS = 10000
// Shorter than POLL_INTERVAL_MS so a stuck request cannot starve the next
// poll tick (design 3.5).
const REQUEST_TIMEOUT_MS = 10000

function errMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/**
 * Owns the data channel: polling, the epoch/single-flight/timeout/mutationSeq
 * guards, the §3.7 bundle processing order, and the (auth-independent)
 * health probe (#149 design 4.2). `activeToken`/`hydrated` come from
 * useTokenSession; this hook does not know about the token input box.
 */
export function useDataChannel(activeToken: string, hydrated: boolean) {
  const [authState, setAuthState] = useState<AuthState>('unknown')
  const [nodes, setNodes] = useState<OcNode[]>([])
  const [edges, setEdges] = useState<OcEdge[]>([])
  const [packs, setPacks] = useState<OcPack[]>([])
  const [graphError, setGraphError] = useState<string | null>(null)
  const [packError, setPackError] = useState<string | null>(null)
  const [graphLoadedAt, setGraphLoadedAt] = useState<number | null>(null)
  const [connected, setConnected] = useState(false)

  // Not rendered -> refs, not state (design 3.2).
  const authEpochRef = useRef(0)
  const mutationSeqRef = useRef(0)
  const authStateRef = useRef<AuthState>('unknown')
  const roundIdRef = useRef(0)
  const inFlightRoundRef = useRef<number | null>(null)
  const dataControllersRef = useRef<Set<AbortController>>(new Set())
  const healthInFlightRef = useRef(false)

  useEffect(() => { authStateRef.current = authState }, [authState])

  // 401 judged (design 3.4's "401 판정" row, reused by both the bundle
  // processor below and RightPanel's onUnauthorized for query/ingest).
  // graphLoadedAt is deliberately left untouched here -- it is tied to
  // identity (activeToken), not to auth validity (design 3.2), so it is
  // only ever cleared by the activeToken-change effect below.
  const markInvalid = useCallback(() => {
    authEpochRef.current += 1
    setAuthState('invalid')
    setNodes([])
    setEdges([])
    setPacks([])
    setGraphError(null)
    setPackError(null)
  }, [])

  // §3.4 "activeToken 변경" rows, folded together with the "하이드레이션(1회)"
  // rows: from this hook's point of view both are just (activeToken,
  // hydrated) changing, and the `hydrated` gate already keeps the very
  // first (pre-hydration) render from doing anything (design 3.4's
  // "마운트: 어떤 효과도 실행하지 않는다" / "hydrated=false: 아무 것도 하지
  // 않는다").
  useEffect(() => {
    if (!hydrated) return
    authEpochRef.current += 1
    dataControllersRef.current.forEach(c => c.abort())
    dataControllersRef.current.clear()
    inFlightRoundRef.current = null
    setNodes([])
    setEdges([])
    setPacks([])
    setGraphError(null)
    setPackError(null)
    setGraphLoadedAt(null)
    setAuthState(activeToken ? 'checking' : 'missing')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeToken, hydrated])

  // The central data query function (§3.5: polling and manual refresh both
  // go through this one function; §3.7: mutation success also calls it,
  // with skipInFlightCheck since it just released the lock itself).
  const fetchData = useCallback(async (opts: { skipInFlightCheck?: boolean } = {}) => {
    if (!activeToken) return
    if (authStateRef.current !== 'checking' && authStateRef.current !== 'ok') return
    if (!opts.skipInFlightCheck && inFlightRoundRef.current !== null) return

    const myRound = ++roundIdRef.current
    inFlightRoundRef.current = myRound
    const myEpoch = authEpochRef.current
    const myMutSeq = mutationSeqRef.current

    const nodesCtl = new AbortController()
    const edgesCtl = new AbortController()
    const packsCtl = new AbortController()
    const controllers = [nodesCtl, edgesCtl, packsCtl]
    controllers.forEach(c => dataControllersRef.current.add(c))
    const timers = controllers.map(c => setTimeout(() => c.abort(), REQUEST_TIMEOUT_MS))

    // §3.6: Promise.allSettled, not Promise.all -- a rejection on one leg
    // must not hide a 401 on another.
    const [nodesRes, edgesRes, packsRes] = await Promise.allSettled([
      getNodes(activeToken, nodesCtl.signal),
      getEdges(activeToken, edgesCtl.signal),
      listPacks(activeToken, packsCtl.signal),
    ])

    timers.forEach(clearTimeout)
    controllers.forEach(c => dataControllersRef.current.delete(c))
    // Lock release ownership (§3.7): only release if this round is still
    // the one currently tracked as in-flight. A mutation-success call may
    // already have released it and started a newer round.
    if (inFlightRoundRef.current === myRound) inFlightRoundRef.current = null

    // §3.7 bundle order, step 1: epoch guard. Discard everything -- data,
    // auth transition, errors, all of it -- if identity moved on.
    if (myEpoch !== authEpochRef.current) return

    // step 2: §3.3 auth judgement, once per bundle.
    const results = [nodesRes, edgesRes, packsRes]
    const anyUnauthorized = results.some(
      r => r.status === 'rejected' && r.reason instanceof UnauthorizedError
    )
    const anyOk = results.some(r => r.status === 'fulfilled')
    if (anyUnauthorized) {
      markInvalid()
      return // invalid: skip steps 3 and 4 entirely
    }
    if (anyOk) {
      setAuthState('ok')
    }
    // else: no 401 and no 2xx -- no verdict, current authState stands.

    // step 3: mutationSeq guard. A mutation that succeeded while this
    // round was in flight invalidates the whole bundle -- no error, it
    // was superseded, not failed.
    if (myMutSeq !== mutationSeqRef.current) return

    // step 4: §3.6 per-unit application.
    if (nodesRes.status === 'fulfilled' && edgesRes.status === 'fulfilled') {
      setNodes(nodesRes.value)
      setEdges(edgesRes.value)
      setGraphLoadedAt(Date.now())
      setGraphError(null)
    } else {
      const failure = nodesRes.status === 'rejected' ? nodesRes.reason : (edgesRes as PromiseRejectedResult).reason
      setGraphError(errMessage(failure))
    }

    if (packsRes.status === 'fulfilled') {
      setPacks(packsRes.value)
      setPackError(null)
    } else {
      setPackError(errMessage(packsRes.reason))
    }
  }, [activeToken, markInvalid])

  // §3.5: data polling only runs in checking/ok, and restarts (with an
  // immediate call) whenever authState flips into one of those -- which a
  // token change always does, so resumption is automatic.
  useEffect(() => {
    if (authState !== 'checking' && authState !== 'ok') return
    fetchData()
    const id = setInterval(() => fetchData(), POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [authState, fetchData])

  // §3.7: what a successful mutation does, in order -- bump the sequence,
  // abort whatever data request is in flight and free the lock, then
  // requery through the same central function (skipping the single-flight
  // check, which is safe because the lock was just freed above).
  const notifyMutationSuccess = useCallback(() => {
    mutationSeqRef.current += 1
    dataControllersRef.current.forEach(c => c.abort())
    dataControllersRef.current.clear()
    inFlightRoundRef.current = null
    fetchData({ skipInFlightCheck: true })
  }, [fetchData])

  // §5.6 pack visibility change: goes through set_visibility, replaces the
  // row with the server's response, then follows the same mutation-success
  // procedure as any other change.
  const changePackVisibility = useCallback(async (packId: string, visibility: PackVisibility) => {
    const myEpoch = authEpochRef.current
    let updated: OcPack
    try {
      updated = await setPackVisibility(activeToken, packId, visibility)
    } catch (err) {
      if (err instanceof UnauthorizedError) markInvalid()
      throw err
    }
    if (myEpoch !== authEpochRef.current) return // identity moved on, discard silently
    setPacks(prev => prev.map(p => (p.pack_id === packId ? updated : p)))
    notifyMutationSuccess()
  }, [activeToken, markInvalid, notifyMutationSuccess])

  // §3.5: the health probe is unauthenticated and runs on its own timer
  // regardless of authState, so the connection dot does not freeze when
  // data polling stops (missing/invalid). Single-flight, no epoch guard --
  // §3.5 explains a sequence guard would add state without adding
  // protection beyond what single-flight already gives it.
  useEffect(() => {
    let cancelled = false
    async function probe() {
      if (healthInFlightRef.current) return
      healthInFlightRef.current = true
      const ctl = new AbortController()
      const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS)
      try {
        const res = await getStatus(ctl.signal)
        if (!cancelled) setConnected(res.ok)
      } finally {
        clearTimeout(timer)
        healthInFlightRef.current = false
      }
    }
    probe()
    const id = setInterval(probe, HEALTH_INTERVAL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return {
    authState,
    nodes,
    edges,
    packs,
    graphError,
    packError,
    graphLoadedAt,
    connected,
    refresh: fetchData,
    notifyMutationSuccess,
    changePackVisibility,
    markInvalid,
    authEpochRef,
  }
}

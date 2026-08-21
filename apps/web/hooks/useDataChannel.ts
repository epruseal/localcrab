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
  // Separate from dataControllersRef: query/ingest/pack-visibility actions
  // (#149 F1), not the polled data channel. Kept apart so a mutation
  // success (notifyMutationSuccess) never aborts an in-flight *action* --
  // only the epoch-raising paths (abortInFlight below) abort both sets
  // together.
  const actionControllersRef = useRef<Set<AbortController>>(new Set())
  const healthInFlightRef = useRef(false)

  useEffect(() => { authStateRef.current = authState }, [authState])

  // v3.3: shared by both epoch-raising paths -- the activeToken-change
  // effect below and markInvalid. Raising the epoch orphans every
  // in-flight request, data rounds and actions alike, so both controller
  // sets are aborted and cleared together. Safe to call from the bundle
  // processor's own verdict path: fetchData removes its controllers from
  // the set *before* judging the bundle, so a markInvalid triggered by a
  // 401 verdict never aborts the round that produced it.
  // notifyMutationSuccess is NOT an epoch-raising path and deliberately
  // aborts only the data set (F1): killing actions there would abort an
  // unrelated in-flight POST/query that has nothing to do with the change
  // that just succeeded.
  const abortInFlight = useCallback(() => {
    dataControllersRef.current.forEach(c => c.abort())
    dataControllersRef.current.clear()
    actionControllersRef.current.forEach(c => c.abort())
    actionControllersRef.current.clear()
  }, [])

  // 401 judged (design 3.4's "401 판정" row, reused by both the bundle
  // processor below and RightPanel's onUnauthorized for query/ingest).
  // graphLoadedAt is deliberately left untouched here -- it is tied to
  // identity (activeToken), not to auth validity (design 3.2), so it is
  // only ever cleared by the activeToken-change effect below.
  // inFlightRoundRef is deliberately NOT reset here: only the token-change
  // effect owns that null assignment; the in-flight round it refers to
  // releases the lock itself (or the §3.7 ownership check skips it).
  const markInvalid = useCallback(() => {
    authEpochRef.current += 1
    abortInFlight()
    setAuthState('invalid')
    setNodes([])
    setEdges([])
    setPacks([])
    setGraphError(null)
    setPackError(null)
  }, [abortInFlight])

  // §3.4 "activeToken 변경" rows, folded together with the "하이드레이션(1회)"
  // rows: from this hook's point of view both are just (activeToken,
  // hydrated) changing, and the `hydrated` gate already keeps the very
  // first (pre-hydration) render from doing anything (design 3.4's
  // "마운트: 어떤 효과도 실행하지 않는다" / "hydrated=false: 아무 것도 하지
  // 않는다").
  useEffect(() => {
    if (!hydrated) return
    authEpochRef.current += 1
    // §3.7/F1/v3.3: identity changing orphans in-flight data rounds and
    // actions alike -- abort both sets, shared with markInvalid (the other
    // epoch-raising path).
    abortInFlight()
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

  // §3.5: data polling only runs in checking/ok. The immediate call fires
  // only on *entry* into the {checking, ok} zone (unknown/missing/invalid
  // -> checking, always via the token-change effect above, so resumption
  // is automatic) or when fetchData itself is recreated (activeToken
  // change). checking->ok is the result of a bundle completing, not a
  // reason to requery -- it used to also flip this effect's old
  // [authState, fetchData] deps, firing an immediate duplicate round right
  // after the one that just finished. That duplicate round held the
  // single-flight lock for as long as it ran, which is the root cause of
  // #149's 17b: a hung nodes leg let the duplicate round occupy the lock
  // long enough to silently swallow a manual refresh. Keying the effect on
  // this zone-membership boolean instead of authState itself makes
  // checking->ok a no-op for the effect (pollingActive stays true, deps
  // unchanged, no restart).
  const pollingActive = authState === 'checking' || authState === 'ok'
  useEffect(() => {
    if (!pollingActive) return
    fetchData()
    const id = setInterval(() => fetchData(), POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [pollingActive, fetchData])

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

  // §3.5/F1: acquired by any action request (query, ingest, pack-visibility
  // change) -- registers its controller in actionControllersRef (kept apart
  // from the data channel's dataControllersRef, see the ref declaration
  // above) and arms a REQUEST_TIMEOUT_MS abort timer. `release` clears the
  // timer and removes the controller from the set; it is safe to call more
  // than once (guarded by `released`) since both the success path and a
  // `finally` block call it.
  const acquireRequestController = useCallback((): { signal: AbortSignal; release: () => void } => {
    const ctl = new AbortController()
    actionControllersRef.current.add(ctl)
    const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS)
    let released = false
    const release = () => {
      if (released) return
      released = true
      clearTimeout(timer)
      actionControllersRef.current.delete(ctl)
    }
    return { signal: ctl.signal, release }
  }, [])

  // §5.6 pack visibility change: goes through set_visibility, replaces the
  // row with the server's response, then follows the same mutation-success
  // procedure as any other change.
  const changePackVisibility = useCallback(async (packId: string, visibility: PackVisibility) => {
    const myEpoch = authEpochRef.current
    const { signal, release } = acquireRequestController()
    let updated: OcPack
    try {
      updated = await setPackVisibility(activeToken, packId, visibility, signal)
    } catch (err) {
      // v3.4 F-a: epoch guard first, symmetric with the success path below.
      // Order matters: if the token changes in the window between the 401
      // response and this catch, markInvalid-first would let that stale 401
      // lock the NEW valid token as invalid with no natural recovery (§3.4).
      // Same contract as RightPanel's handleQuery/handleIngest catch; the
      // intended difference is that RightPanel consumes errors in-place
      // (toast) while this hook rethrows so PackPanel owns row display.
      if (myEpoch !== authEpochRef.current) return // identity moved on, discard like the success path
      if (err instanceof UnauthorizedError) markInvalid()
      throw err
    } finally {
      release()
    }
    if (myEpoch !== authEpochRef.current) return // identity moved on, discard silently
    setPacks(prev => prev.map(p => (p.pack_id === packId ? updated : p)))
    notifyMutationSuccess()
  }, [activeToken, markInvalid, notifyMutationSuccess, acquireRequestController])

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
    acquireRequestController,
    markInvalid,
    authEpochRef,
  }
}

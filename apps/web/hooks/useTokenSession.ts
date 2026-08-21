'use client'

import { useEffect, useRef, useState, useCallback } from 'react'

// New key wins on read; old key is kept as a fallback so a token saved by a
// pre-#149 build (or written back after a rollback) is not silently lost.
// Both keys are written on every commit and neither is ever deleted outright
// -- see the write/delete failure handling below for why. Retiring the old
// key is tracked as a follow-up: epruseal/localcrab#214.
const NEW_KEY = 'oc_user_token'
const OLD_KEY = 'oc_api_key'
const DEBOUNCE_MS = 400

function readKey(key: string): string {
  try {
    return localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

/**
 * Owns the token input box, its 400ms-debounced commit to `activeToken`,
 * and the dual-key localStorage read/write/delete with its failure/notice
 * handling (#149 design 4.2). Does not know about auth state or the network
 * -- that is useDataChannel's job.
 */
export function useTokenSession() {
  const [hydrated, setHydrated] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const [activeToken, setActiveToken] = useState('')
  // Single-line notice surfaced next to the token input: write failure,
  // delete failure, or "two different values are stored" -- design 4.2.
  const [storageNotice, setStorageNotice] = useState<string | null>(null)

  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // §149 F4: set by the input handler (the user's actual edit entry point),
  // never by the hydration migration write below -- distinguishes "user
  // typed this" from "hydration wrote this on the user's behalf" so the
  // same-value branch in the debounce effect below only fires for real edits.
  const userEditedRef = useRef(false)

  // Commits a confirmed token value to both storage keys (or clears both),
  // per-key try/catch so one failing does not mask the other, and reports
  // the failure kinds design 4.2 distinguishes (write vs delete). Declared
  // above the hydration effect below so that effect can call it directly.
  const persist = useCallback((value: string) => {
    if (value) {
      let failed = false
      try { localStorage.setItem(NEW_KEY, value) } catch { failed = true }
      try { localStorage.setItem(OLD_KEY, value) } catch { failed = true }
      setStorageNotice(failed ? '이 브라우저에 토큰을 저장하지 못했어. 다음에 값을 바꾸면 다시 시도돼.' : null)
    } else {
      let failed = false
      try { localStorage.removeItem(NEW_KEY) } catch { failed = true }
      try { localStorage.removeItem(OLD_KEY) } catch { failed = true }
      setStorageNotice(
        failed
          ? '이 브라우저에서 저장된 토큰을 완전히 지우지 못했어. 남은 값이 다음 접속에서 다시 쓰일 수 있어.'
          : null
      )
    }
  }, [])

  // Hydration (runs once). The only write here is the old-key-only
  // migration case (design 4.2 write rule applied to the hydration-time
  // setActiveToken): if the new key is empty and the old key has a value,
  // that value is persisted to both keys so it is not silently lost. The
  // mismatch case (both keys set, different values) is left alone here --
  // it is surfaced but not resolved until the user explicitly edits and
  // the debounce settles, per the §4.2 exception.
  useEffect(() => {
    const newVal = readKey(NEW_KEY)
    const oldVal = readKey(OLD_KEY)
    const initial = newVal || oldVal
    setTokenInput(initial)
    setActiveToken(initial)
    if (newVal && oldVal && newVal !== oldVal) {
      setStorageNotice('저장된 토큰 값이 두 개 서로 다르게 남아 있어. 아래에서 다시 저장하면 하나로 정리돼.')
    } else if (!newVal && oldVal) {
      persist(oldVal)
    }
    setHydrated(true)
    // persist has a [] useCallback identity, so adding it here does not
    // change the once-per-mount behavior of this effect.
  }, [persist])

  // Debounce: tokenInput -> activeToken after DEBOUNCE_MS of no further
  // edits. This is the only place activeToken changes after hydration, and
  // it is also the only place that persists -- so a hydration-time mismatch
  // between the two keys is left alone until the user actually edits.
  //
  // storageNotice is in the deps array because the equal-value branch below
  // reads it (§149 F4). That only matters while tokenInput === activeToken;
  // when they differ (the timer branch), a storageNotice change just before
  // this effect re-runs is a no-op re-run -- pending timer is cleared and an
  // identical one is set again, with the same tokenInput/activeToken still
  // driving it.
  useEffect(() => {
    if (!hydrated) return
    if (tokenInput === activeToken) {
      // Same-value re-entry (design-fix-v3.1 F4): activeToken is not
      // changing, so the timer branch below would never fire for a retyped
      // value identical to activeToken, leaving a stale two-keys-differ
      // notice stuck even after the user "fixes" it by retyping A. Only
      // resolve it when the user actually edited (not the hydration
      // migration write) and a notice is currently showing. No timer --
      // activeToken isn't moving, so there is nothing to debounce.
      // persist() itself clears storageNotice on success and sets a failure
      // notice on failure (§4.2 contract); do not set it here.
      if (storageNotice && userEditedRef.current) persist(tokenInput)
      return
    }
    debounceTimer.current = setTimeout(() => {
      setActiveToken(tokenInput)
      persist(tokenInput)
    }, DEBOUNCE_MS)
    return () => {
      if (debounceTimer.current) clearTimeout(debounceTimer.current)
    }
  }, [tokenInput, activeToken, hydrated, storageNotice, persist])

  const tokenPending = hydrated && tokenInput !== activeToken

  return {
    hydrated,
    tokenInput,
    activeToken,
    tokenPending,
    storageNotice,
    onTokenInputChange: useCallback((value: string) => {
      userEditedRef.current = true
      setTokenInput(value)
    }, []),
  }
}

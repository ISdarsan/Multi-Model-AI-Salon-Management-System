import { useCallback, useEffect, useRef, useState } from 'react'
import { supabase } from '../lib/supabase'

// Loads data and keeps it fresh. A screen gives it:
//   loader  - an async function that returns the data
//   tables  - the database tables to listen to (Supabase Realtime)
// The data reloads (1) when it first appears, (2) a moment after any change in those tables,
// and (3) every 5 seconds as a fallback, in case a live message is missed.
// It returns { data, loading, error, refresh }.
export function useLiveData(loader, tables = []) {
  const [state, setState] = useState({ data: null, loading: true, error: null })

  // Always call the newest loader without restarting the timers.
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  const refresh = useCallback(async () => {
    try {
      const data = await loaderRef.current()
      setState({ data, loading: false, error: null })
    } catch (error) {
      // Keep showing the old data (if any) and show the error above it.
      setState((old) => ({ ...old, loading: false, error: error.message || String(error) }))
    }
  }, [])

  const tableKey = tables.join(',')
  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 5000) // the 5-second fallback

    let wait
    const channel = supabase.channel('live-' + Math.random().toString(36).slice(2))
    tableKey.split(',').filter(Boolean).forEach((table) => {
      channel.on('postgres_changes', { event: '*', schema: 'public', table }, () => {
        clearTimeout(wait)
        wait = setTimeout(refresh, 300) // wait a moment so several changes cause one reload
      })
    })
    channel.subscribe()

    return () => {
      clearInterval(timer)
      clearTimeout(wait)
      supabase.removeChannel(channel)
    }
  }, [refresh, tableKey])

  return { ...state, refresh }
}

// Supabase returns { data, error }. This throws the error so the screen can show it.
export function must(result) {
  if (result.error) throw new Error(result.error.message)
  return result
}

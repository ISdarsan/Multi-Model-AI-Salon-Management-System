import { createClient } from '@supabase/supabase-js'

// These come from frontend/.env. Only the PUBLISHABLE (anon) key may be used in the browser.
// NEVER put the service role / secret key here.
const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

// If .env is missing, App.jsx shows a clear message instead of a blank page.
export const configMissing = !url || !anonKey

// One shared Supabase client. By default it keeps the login session in the browser's
// localStorage, so you stay signed in after a refresh.
export const supabase = createClient(url || 'http://localhost', anonKey || 'missing-key')

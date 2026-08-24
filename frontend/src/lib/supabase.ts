import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

if (!url || !anonKey) {
  console.warn('VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY not set — Controls/Approvals login will not work')
}

export const supabase = createClient(url ?? '', anonKey ?? '')

/** False when the build is missing Supabase config — sign-in cannot work at all. */
export const authConfigured = Boolean(url && anonKey)

/** Host of the project this app authenticates against, shown on the sign-in form
 *  so a user created in the wrong project is diagnosable without DevTools. */
export const authHost = url ? new URL(url).host : null

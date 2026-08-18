/**
 * Shared credential scrub for Desktop Electron child processes.
 *
 * Desktop often spreads `{ ...process.env }` into PTY / serve / updater /
 * bootstrap children. Provider and messaging secrets belong in
 * HERMES_HOME/.env for the backend, not in the parent Electron environment
 * forwarded wholesale to every child.
 *
 * Endpoint override variables (`*_BASE_URL`, e.g. OPENROUTER_BASE_URL) are
 * documented non-secret configuration and are deliberately NOT treated as
 * credentials: children must keep them so a user can point a provider at a
 * custom gateway.
 */

const CREDENTIAL_SUFFIXES = Object.freeze([
  '_API_KEY',
  '_TOKEN',
  '_SECRET',
  '_PASSWORD',
  '_CREDENTIALS',
  '_ACCESS_KEY',
  '_PRIVATE_KEY',
  '_OAUTH_TOKEN'
])

const CREDENTIAL_NAMES = new Set([
  'ANTHROPIC_TOKEN',
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'CUSTOM_API_KEY',
  'FAL_KEY'
])

export function isCredentialEnvVar(name: string): boolean {
  if (!name) {
    return false
  }

  // Env keys are case-insensitive on Windows (see backend-env.ts pathEnvKey);
  // normalize before matching so a mixed-case key is still scrubbed.
  const normalized = name.toUpperCase()

  if (CREDENTIAL_NAMES.has(normalized)) {
    return true
  }

  return CREDENTIAL_SUFFIXES.some(suffix => normalized.endsWith(suffix))
}

export type EnvMap = Record<string, string | undefined>

/**
 * Keys Desktop intentionally forwards to children even though some are
 * credential-shaped (HERMES_DASHBOARD_SESSION_TOKEN). The backend needs them
 * to boot; the scrub only removes secrets inherited from the PARENT Electron
 * process. Pass these as `overrides` to re-apply them after scrubbing.
 */
export const INTENTIONAL_OVERRIDES: ReadonlySet<string> = new Set([
  'HERMES_HOME',
  'HERMES_DASHBOARD_SESSION_TOKEN',
  'HERMES_DESKTOP',
  'HERMES_DESKTOP_TERMINAL',
  'HERMES_WEB_DIST',
  'HERMES_DESKTOP_READY_FILE',
  'TERMINAL_CWD',
  'PATH',
  'PYTHONPATH',
  'NO_COLOR'
])

/**
 * Copy `source` while dropping credential-shaped keys, then re-apply explicit
 * `overrides` (e.g. a minted dashboard session token, HERMES_HOME, PATH
 * overlays). Overrides pass through verbatim — they are the allowlist of
 * intentional values the backend genuinely needs.
 */
export function scrubDesktopChildEnv(source: EnvMap = {}, overrides: EnvMap = {}): Record<string, string> {
  const out: Record<string, string> = {}

  for (const [key, value] of Object.entries(source || {})) {
    if (value == null || value === '') {
      continue
    }

    if (isCredentialEnvVar(key)) {
      continue
    }

    out[key] = String(value)
  }

  for (const [key, value] of Object.entries(overrides || {})) {
    if (value == null || value === '') {
      delete out[key]
      continue
    }

    out[key] = String(value)
  }

  return out
}

export { CREDENTIAL_NAMES, CREDENTIAL_SUFFIXES }

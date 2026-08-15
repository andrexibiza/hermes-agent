

// #83565 child-process credential boundary. Keep aligned with the Python policy.
export function childEnvDestinationKey(key: string): string {
  let current = String(key)
  for (;;) {
    const upper = current.toUpperCase()
    if (upper.startsWith('_HERMES_FORCE_')) { current = current.slice('_HERMES_FORCE_'.length); continue }
    if (upper.startsWith('APPTAINERENV_')) { current = current.slice('APPTAINERENV_'.length); continue }
    if (upper.startsWith('SINGULARITYENV_')) { current = current.slice('SINGULARITYENV_'.length); continue }
    return current
  }
}

const HERMES_83565_ALWAYS_STRIP = new Set([
  'BWS_ACCESS_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
  'GITHUB_APP_ID', 'GITHUB_APP_PRIVATE_KEY_PATH', 'GITHUB_APP_INSTALLATION_ID',
  'HERMES_DASHBOARD_SESSION_TOKEN', 'GATEWAY_ALLOWED_USERS', 'GATEWAY_ALLOW_ALL_USERS', 'GATEWAY_RELAY_ID', 'GATEWAY_RELAY_SECRET',
  'GATEWAY_RELAY_DELIVERY_KEY', 'TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN',
  'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_SIGNING_SECRET', 'EMAIL_PASSWORD',
  'HASS_TOKEN', 'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET', 'DAYTONA_API_KEY',
  'AZURE_CLIENT_SECRET', 'AZURE_FEDERATED_TOKEN_FILE',
  'TLON_SHIP_URL', 'TLON_SHIP_NAME', 'TLON_SHIP_CODE'
])

function hermes83565CredentialName(key: string): boolean {
  const dest = childEnvDestinationKey(key).toUpperCase()
  if (HERMES_83565_ALWAYS_STRIP.has(dest)) return true
  if (dest === 'PWD' || dest === 'OLDPWD' || dest === 'PATH' || dest === 'PATHEXT') return false
  if (dest.startsWith('AUXILIARY_') && (dest.endsWith('_API_KEY') || dest.endsWith('_BASE_URL'))) return true
  if (dest.startsWith('GATEWAY_RELAY_') && /_(SECRET|KEY|TOKEN)$/.test(dest)) return true
  const parts = dest.split(/[^A-Z0-9]+/).filter(Boolean)
  const secret = new Set(['APIKEY','KEY','TOKEN','SECRET','PASSWORD','PASSWD','PASS','PWD','CREDENTIAL','CREDENTIALS','CRED','CREDS','BEARER','WEBHOOK','DSN','PRIVATEKEY'])
  if (parts.some(part => secret.has(part))) return true
  for (let i = 0; i + 1 < parts.length; i++) {
    if (parts[i] === 'API' && parts[i + 1] === 'KEY') return true
    if (parts[i] === 'PRIVATE' && parts[i + 1] === 'KEY') return true
  }
  return false
}

const HERMES_83565_CREDENTIAL_URI = /^[a-z][a-z0-9+.-]*:\/\/[^/@\s:]+:[^/@\s]+@[^\s]+$/i

export function scrubDesktopChildEnv83565(
  source: NodeJS.ProcessEnv | Record<string, string | undefined>
): NodeJS.ProcessEnv {
  const out: NodeJS.ProcessEnv = {}
  for (const [rawKey, rawValue] of Object.entries(source)) {
    if (rawValue == null) continue
    const key = childEnvDestinationKey(rawKey)
    if (hermes83565CredentialName(rawKey)) continue
    if (HERMES_83565_CREDENTIAL_URI.test(String(rawValue))) continue
    out[key] = String(rawValue)
  }
  return out
}

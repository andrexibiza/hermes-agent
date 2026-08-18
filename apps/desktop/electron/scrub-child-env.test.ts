import assert from 'node:assert/strict'

import { test } from 'vitest'

import { INTENTIONAL_OVERRIDES, isCredentialEnvVar, scrubDesktopChildEnv } from './scrub-child-env'

test('isCredentialEnvVar matches suffix and known names', () => {
  assert.equal(isCredentialEnvVar('OPENROUTER_API_KEY'), true)
  assert.equal(isCredentialEnvVar('HERMES_DESKTOP_REMOTE_TOKEN'), true)
  assert.equal(isCredentialEnvVar('AWS_SECRET_ACCESS_KEY'), true)
  assert.equal(isCredentialEnvVar('AWS_ACCESS_KEY_ID'), true)
  assert.equal(isCredentialEnvVar('FAL_KEY'), true)
  assert.equal(isCredentialEnvVar('PATH'), false)
  assert.equal(isCredentialEnvVar('HERMES_HOME'), false)
  assert.equal(isCredentialEnvVar('HERMES_DESKTOP'), false)
})

test('isCredentialEnvVar normalizes env-key case for Windows', () => {
  assert.equal(isCredentialEnvVar('openrouter_api_key'), true)
  assert.equal(isCredentialEnvVar('Anthropic_Token'), true)
  assert.equal(isCredentialEnvVar('fal_key'), true)
  assert.equal(isCredentialEnvVar('Path'), false)
})

test('non-secret endpoint override variables are retained', () => {
  assert.equal(isCredentialEnvVar('OPENROUTER_BASE_URL'), false)
  assert.equal(isCredentialEnvVar('OPENAI_BASE_URL'), false)
  assert.equal(isCredentialEnvVar('ANTHROPIC_BASE_URL'), false)
  assert.equal(isCredentialEnvVar('GEMINI_BASE_URL'), false)
  assert.equal(isCredentialEnvVar('OLLAMA_BASE_URL'), false)
  assert.equal(isCredentialEnvVar('GROQ_BASE_URL'), false)
  assert.equal(isCredentialEnvVar('XAI_BASE_URL'), false)
})

test('scrubDesktopChildEnv drops secrets and keeps operational keys', () => {
  const scrubbed = scrubDesktopChildEnv(
    {
      PATH: '/usr/bin',
      HERMES_HOME: '/home/u/.hermes',
      OPENROUTER_API_KEY: 'sk-live',
      OPENROUTER_BASE_URL: 'https://openrouter.ai/api/v1',
      TELEGRAM_BOT_TOKEN: '123:abc',
      FAL_KEY: 'fal-secret',
      HERMES_DESKTOP_REMOTE_TOKEN: 'remote-secret',
      EMPTY: ''
    },
    {
      HERMES_DESKTOP: '1',
      HERMES_DASHBOARD_SESSION_TOKEN: 'minted-session'
    }
  )

  assert.equal(scrubbed.PATH, '/usr/bin')
  assert.equal(scrubbed.HERMES_HOME, '/home/u/.hermes')
  assert.equal(scrubbed.OPENROUTER_BASE_URL, 'https://openrouter.ai/api/v1')
  assert.equal(scrubbed.HERMES_DESKTOP, '1')
  assert.equal(scrubbed.HERMES_DASHBOARD_SESSION_TOKEN, 'minted-session')
  assert.equal(scrubbed.OPENROUTER_API_KEY, undefined)
  assert.equal(scrubbed.TELEGRAM_BOT_TOKEN, undefined)
  assert.equal(scrubbed.FAL_KEY, undefined)
  assert.equal(scrubbed.HERMES_DESKTOP_REMOTE_TOKEN, undefined)
})

test('scrubDesktopChildEnv drops lower-case secrets on Windows-style keys', () => {
  const scrubbed = scrubDesktopChildEnv({
    openrouter_api_key: 'sk-live',
    fal_key: 'fal-secret',
    Path: 'C:\\Windows\\System32',
    OPENROUTER_BASE_URL: 'https://openrouter.ai/api/v1'
  })

  assert.equal(scrubbed.openrouter_api_key, undefined)
  assert.equal(scrubbed.fal_key, undefined)
  assert.equal(scrubbed.Path, 'C:\\Windows\\System32')
  assert.equal(scrubbed.OPENROUTER_BASE_URL, 'https://openrouter.ai/api/v1')
})

test('INTENTIONAL_OVERRIDES are re-applied after scrubbing even when credential-shaped', () => {
  const overrides: Record<string, string> = {
    HERMES_HOME: '/home/u/.hermes',
    HERMES_DASHBOARD_SESSION_TOKEN: 'minted-session',
    HERMES_DESKTOP: '1',
    HERMES_DESKTOP_TERMINAL: '1',
    HERMES_WEB_DIST: '/app/dist',
    HERMES_DESKTOP_READY_FILE: '/home/u/.hermes/.ready',
    TERMINAL_CWD: '/home/u',
    PATH: '/usr/bin',
    PYTHONPATH: '/opt/venv/lib',
    NO_COLOR: '1'
  }

  const scrubbed = scrubDesktopChildEnv(
    {
      OPENROUTER_API_KEY: 'sk-live',
      TELEGRAM_BOT_TOKEN: '123:abc'
    },
    overrides
  )

  for (const key of INTENTIONAL_OVERRIDES) {
    assert.equal(scrubbed[key], overrides[key], `intentional override ${key} preserved`)
  }

  assert.equal(scrubbed.OPENROUTER_API_KEY, undefined)
  assert.equal(scrubbed.TELEGRAM_BOT_TOKEN, undefined)
})

test('composed backend spawn env: serve child shape keeps pins and drops parent secrets', () => {
  // Mirrors the env construction in startHermes()/spawnPoolBackend(): parent
  // env scrubbed, then HERMES_HOME / session token / desktop pins / web dist /
  // ready file re-applied as explicit overrides.
  const scrubbed = scrubDesktopChildEnv(
    {
      PATH: '/usr/bin:/bin',
      HOME: '/home/u',
      OPENROUTER_API_KEY: 'sk-live',
      ANTHROPIC_API_KEY: 'sk-ant',
      AWS_SECRET_ACCESS_KEY: 'aws-secret',
      TELEGRAM_BOT_TOKEN: '123:abc',
      OPENROUTER_BASE_URL: 'https://openrouter.ai/api/v1'
    },
    {
      HERMES_HOME: '/home/u/.hermes',
      TERMINAL_CWD: '/home/u',
      HERMES_DASHBOARD_SESSION_TOKEN: 'minted-session',
      HERMES_DESKTOP: '1',
      HERMES_WEB_DIST: '/app/dist',
      HERMES_DESKTOP_READY_FILE: '/home/u/.hermes/.ready'
    }
  )

  assert.equal(scrubbed.HERMES_HOME, '/home/u/.hermes')
  assert.equal(scrubbed.TERMINAL_CWD, '/home/u')
  assert.equal(scrubbed.HERMES_DASHBOARD_SESSION_TOKEN, 'minted-session')
  assert.equal(scrubbed.HERMES_DESKTOP, '1')
  assert.equal(scrubbed.HERMES_WEB_DIST, '/app/dist')
  assert.equal(scrubbed.HERMES_DESKTOP_READY_FILE, '/home/u/.hermes/.ready')
  assert.equal(scrubbed.PATH, '/usr/bin:/bin')
  assert.equal(scrubbed.HOME, '/home/u')
  assert.equal(scrubbed.OPENROUTER_BASE_URL, 'https://openrouter.ai/api/v1')
  assert.equal(scrubbed.OPENROUTER_API_KEY, undefined)
  assert.equal(scrubbed.ANTHROPIC_API_KEY, undefined)
  assert.equal(scrubbed.AWS_SECRET_ACCESS_KEY, undefined)
  assert.equal(scrubbed.TELEGRAM_BOT_TOKEN, undefined)
})

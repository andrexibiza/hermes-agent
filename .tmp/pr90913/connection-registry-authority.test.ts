import assert from 'node:assert/strict'

import { test } from 'vitest'

import type { ConnectionRegistry, RegistryConnection } from './connection-registry'
import { LOCAL_CONNECTION_ID, REGISTRY_VERSION, resolvedConnectionId } from './connection-registry'

function registryWith(...connections: RegistryConnection[]): ConnectionRegistry {
  return {
    version: REGISTRY_VERSION,
    primary: connections[0]?.id ?? LOCAL_CONNECTION_ID,
    launchMode: 'primary',
    lastUsed: connections[0]?.id ?? LOCAL_CONNECTION_ID,
    connections
  }
}

const local: RegistryConnection = { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device' }

test('resolvedConnectionId accepts only a current explicit registration id', () => {
  const remoteA: RegistryConnection = {
    id: 'remote-a',
    kind: 'remote',
    label: 'Remote A',
    url: 'https://shared.example',
    authMode: 'token',
    token: { encoding: 'safeStorage', value: 'token-a' },
    headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-a' } }
  }
  const remoteB: RegistryConnection = {
    id: 'remote-b',
    kind: 'remote',
    label: 'Remote B',
    url: 'https://shared.example',
    authMode: 'oauth',
    headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-b' } }
  }
  const registry = registryWith(local, remoteA, remoteB)

  assert.equal(
    resolvedConnectionId(registry, {
      authMode: 'token',
      baseUrl: remoteA.url,
      connectionId: remoteB.id,
      headers: remoteA.headers,
      mode: 'remote',
      remoteKind: 'url',
      token: remoteA.token
    }),
    remoteB.id
  )

  const misleadingLegacyShape = {
    authMode: 'token',
    baseUrl: remoteA.url,
    headers: remoteA.headers,
    mode: 'remote' as const,
    remoteKind: 'url' as const,
    token: remoteA.token
  }
  const invalidIds: Array<null | string | undefined> = ['retired-source', '', null, undefined]

  for (const connectionId of invalidIds) {
    assert.equal(resolvedConnectionId(registry, { ...misleadingLegacyShape, connectionId }), null)
  }
})

test('resolvedConnectionId matches the complete URL and Cloud envelope', () => {
  const sharedUrl = 'https://shared.example/gateway'
  const remoteToken: RegistryConnection = {
    id: 'remote-token',
    kind: 'remote',
    label: 'Token remote',
    url: sharedUrl,
    authMode: 'token',
    token: { encoding: 'safeStorage', value: 'token-a' },
    headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-a' } }
  }
  const remoteOauth: RegistryConnection = {
    id: 'remote-oauth',
    kind: 'remote',
    label: 'OAuth remote',
    url: `${sharedUrl}/`,
    authMode: 'oauth',
    headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-b' } }
  }
  const cloudNous: RegistryConnection = {
    id: 'cloud-nous',
    kind: 'cloud',
    label: 'Nous cloud',
    url: sharedUrl,
    authMode: 'oauth',
    headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-cloud' } },
    org: 'nous'
  }
  const cloudLabs: RegistryConnection = {
    ...cloudNous,
    id: 'cloud-labs',
    label: 'Labs cloud',
    org: 'labs'
  }
  const registry = registryWith(local, remoteToken, remoteOauth, cloudNous, cloudLabs)

  assert.equal(
    resolvedConnectionId(registry, {
      authMode: 'token',
      baseUrl: sharedUrl,
      headers: { 'cf-access-client-id': { value: 'header-a', encoding: 'safeStorage' } },
      mode: 'remote',
      remoteKind: 'url',
      token: { value: 'token-a', encoding: 'safeStorage' }
    }),
    remoteToken.id
  )
  assert.equal(
    resolvedConnectionId(registry, {
      authMode: 'oauth',
      baseUrl: sharedUrl,
      headers: { 'CF-ACCESS-CLIENT-ID': { value: 'header-b', encoding: 'safeStorage' } },
      mode: 'remote',
      remoteKind: 'url'
    }),
    remoteOauth.id
  )
  assert.equal(
    resolvedConnectionId(registry, {
      authMode: 'oauth',
      baseUrl: sharedUrl,
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-cloud' } },
      mode: 'remote',
      org: 'nous',
      remoteKind: 'cloud'
    }),
    cloudNous.id
  )
  assert.equal(
    resolvedConnectionId(registry, {
      authMode: 'oauth',
      baseUrl: sharedUrl,
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-cloud' } },
      mode: 'remote',
      org: 'labs',
      remoteKind: 'cloud'
    }),
    cloudLabs.id
  )

  assert.equal(resolvedConnectionId(registry, { baseUrl: sharedUrl, mode: 'remote', remoteKind: 'url' }), null)
  assert.equal(
    resolvedConnectionId(registry, { baseUrl: sharedUrl, mode: 'remote', remoteKind: 'cloud' }),
    null
  )

  const duplicate = registryWith(
    ...registry.connections,
    { ...remoteToken, id: 'remote-token-copy', label: 'Token remote copy' }
  )

  assert.equal(
    resolvedConnectionId(duplicate, {
      authMode: 'token',
      baseUrl: sharedUrl,
      headers: remoteToken.headers,
      mode: 'remote',
      remoteKind: 'url',
      token: remoteToken.token
    }),
    null
  )
})

test('resolvedConnectionId preserves only unique default legacy inference', () => {
  const legacyRemote: RegistryConnection = {
    id: 'legacy-remote',
    kind: 'remote',
    label: 'Legacy remote',
    url: 'https://legacy.example:9443'
  }
  const legacySsh: RegistryConnection = {
    id: 'legacy-ssh',
    kind: 'ssh',
    label: 'Legacy SSH',
    host: 'work-host',
    user: 'root'
  }
  const registry = registryWith(local, legacyRemote, legacySsh)

  assert.equal(resolvedConnectionId(registry, { mode: 'local' }), local.id)
  assert.equal(
    resolvedConnectionId(registry, {
      baseUrl: 'https://legacy.example:9443/',
      mode: 'remote',
      remoteKind: 'url'
    }),
    legacyRemote.id
  )
  assert.equal(
    resolvedConnectionId(registry, {
      mode: 'remote',
      remoteHost: 'ROOT@WORK-HOST',
      remoteKind: 'ssh'
    }),
    legacySsh.id
  )

  const duplicateRemote: RegistryConnection = {
    ...legacyRemote,
    id: 'legacy-remote-copy',
    label: 'Legacy remote copy'
  }

  assert.equal(
    resolvedConnectionId(registryWith(local, legacyRemote, duplicateRemote), {
      baseUrl: legacyRemote.url,
      mode: 'remote',
      remoteKind: 'url'
    }),
    null
  )
  assert.equal(
    resolvedConnectionId(
      registryWith(local, { id: 'local-copy', kind: 'local', label: 'Local copy' }),
      { mode: 'local' }
    ),
    null
  )
})

test('resolvedConnectionId keeps complete SSH routes distinct', () => {
  const base = {
    host: 'work-host',
    keyPath: '/keys/a',
    kind: 'ssh' as const,
    remoteHermesPath: '/srv/hermes',
    remoteProfile: 'alpha',
    user: 'root'
  }
  const registry = registryWith(
    local,
    { ...base, id: 'ssh-base', label: 'SSH base' },
    { ...base, id: 'ssh-port', label: 'SSH port', port: 2222 },
    { ...base, id: 'ssh-key', keyPath: '/keys/b', label: 'SSH key' },
    { ...base, id: 'ssh-path', label: 'SSH path', remoteHermesPath: '/opt/hermes' },
    { ...base, id: 'ssh-profile', label: 'SSH profile', remoteProfile: 'beta' }
  )
  const resolve = (ssh: NonNullable<Parameters<typeof resolvedConnectionId>[1]['ssh']>) =>
    resolvedConnectionId(registry, { mode: 'remote', remoteKind: 'ssh', ssh })

  assert.equal(resolve(base), 'ssh-base')
  assert.equal(resolve({ ...base, port: 2222 }), 'ssh-port')
  assert.equal(resolve({ ...base, keyPath: '/keys/b' }), 'ssh-key')
  assert.equal(resolve({ ...base, remoteHermesPath: '/opt/hermes' }), 'ssh-path')
  assert.equal(resolve({ ...base, remoteProfile: 'beta' }), 'ssh-profile')
  assert.equal(
    resolvedConnectionId(registry, {
      mode: 'remote',
      remoteHost: 'ROOT@WORK-HOST',
      remoteKind: 'ssh'
    }),
    null
  )
  assert.equal(
    resolvedConnectionId(registry, {
      mode: 'remote',
      remoteHost: 'ROOT@WORK-HOST',
      remoteKind: 'ssh',
      ssh: undefined
    }),
    null
  )

  const duplicate = registryWith(
    ...registry.connections,
    { ...base, id: 'ssh-base-copy', label: 'SSH base copy' }
  )

  assert.equal(resolvedConnectionId(duplicate, { mode: 'remote', remoteKind: 'ssh', ssh: base }), null)
})

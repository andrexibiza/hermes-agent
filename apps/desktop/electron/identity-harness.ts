const assert = {
  equal(actual: unknown, expected: unknown): void {
    if (!Object.is(actual, expected)) {
      throw new Error(`Expected ${String(expected)}, got ${String(actual)}`)
    }
  }
}
import {
  LOCAL_CONNECTION_ID,
  REGISTRY_VERSION,
  resolvedConnectionId,
  type ConnectionRegistry
} from './connection-registry'

const sharedUrl = 'https://shared.example/gateway'
const urlRegistry: ConnectionRegistry = {
  version: REGISTRY_VERSION,
  primary: 'remote-token',
  launchMode: 'primary',
  lastUsed: 'remote-token',
  connections: [
    { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device' },
    {
      id: 'remote-token', kind: 'remote', label: 'Token', url: sharedUrl, authMode: 'token',
      token: { encoding: 'safeStorage', value: 'token-a' },
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-a' } }
    },
    {
      id: 'remote-oauth', kind: 'remote', label: 'OAuth', url: `${sharedUrl}/`, authMode: 'oauth',
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-b' } }
    },
    {
      id: 'cloud-nous', kind: 'cloud', label: 'Nous', url: sharedUrl, authMode: 'oauth', org: 'nous',
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-cloud' } }
    },
    {
      id: 'cloud-labs', kind: 'cloud', label: 'Labs', url: sharedUrl, authMode: 'oauth', org: 'labs',
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-cloud' } }
    }
  ]
}

assert.equal(resolvedConnectionId(urlRegistry, {
  connectionId: 'remote-oauth', mode: 'remote', remoteKind: 'url', baseUrl: sharedUrl,
  authMode: 'token', token: { encoding: 'safeStorage', value: 'token-a' },
  headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-a' } }
}), 'remote-oauth')
assert.equal(resolvedConnectionId(urlRegistry, {
  connectionId: 'retired-source', mode: 'remote', remoteKind: 'url', baseUrl: sharedUrl
}), 'retired-source')
assert.equal(resolvedConnectionId(urlRegistry, {
  mode: 'remote', remoteKind: 'url', baseUrl: sharedUrl, authMode: 'token',
  token: { encoding: 'safeStorage', value: 'token-a' },
  headers: { 'cf-access-client-id': { encoding: 'safeStorage', value: 'header-a' } }
}), 'remote-token')
assert.equal(resolvedConnectionId(urlRegistry, {
  mode: 'remote', remoteKind: 'url', baseUrl: sharedUrl, authMode: 'oauth',
  headers: { 'CF-ACCESS-CLIENT-ID': { encoding: 'safeStorage', value: 'header-b' } }
}), 'remote-oauth')
assert.equal(resolvedConnectionId(urlRegistry, {
  mode: 'remote', remoteKind: 'cloud', baseUrl: sharedUrl, authMode: 'oauth', org: 'nous',
  headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-cloud' } }
}), 'cloud-nous')
assert.equal(resolvedConnectionId(urlRegistry, {
  mode: 'remote', remoteKind: 'cloud', baseUrl: sharedUrl, authMode: 'oauth', org: 'labs',
  headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-cloud' } }
}), 'cloud-labs')
assert.equal(resolvedConnectionId(urlRegistry, { mode: 'remote', remoteKind: 'url', baseUrl: sharedUrl }), null)
assert.equal(resolvedConnectionId(urlRegistry, { mode: 'remote', remoteKind: 'cloud', baseUrl: sharedUrl }), null)

const duplicateUrl: ConnectionRegistry = {
  ...urlRegistry,
  connections: [...urlRegistry.connections, { ...urlRegistry.connections[1], id: 'remote-token-copy' }]
}
assert.equal(resolvedConnectionId(duplicateUrl, {
  mode: 'remote', remoteKind: 'url', baseUrl: sharedUrl, authMode: 'token',
  token: { encoding: 'safeStorage', value: 'token-a' },
  headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-a' } }
}), null)

const sshBase = {
  host: 'work-host', user: 'root', keyPath: '/keys/a', remoteHermesPath: '/srv/hermes', remoteProfile: 'alpha'
}
const sshRegistry: ConnectionRegistry = {
  version: REGISTRY_VERSION,
  primary: 'ssh-base',
  launchMode: 'primary',
  lastUsed: 'ssh-base',
  connections: [
    { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device' },
    { ...sshBase, id: 'ssh-base', kind: 'ssh', label: 'Base' },
    { ...sshBase, id: 'ssh-port', kind: 'ssh', label: 'Port', port: 2222 },
    { ...sshBase, id: 'ssh-key', kind: 'ssh', label: 'Key', keyPath: '/keys/b' },
    { ...sshBase, id: 'ssh-path', kind: 'ssh', label: 'Path', remoteHermesPath: '/opt/hermes' },
    { ...sshBase, id: 'ssh-profile', kind: 'ssh', label: 'Profile', remoteProfile: 'beta' }
  ]
}
const sshResolve = (ssh: any) => resolvedConnectionId(sshRegistry, { mode: 'remote', remoteKind: 'ssh', ssh })
assert.equal(sshResolve(sshBase), 'ssh-base')
assert.equal(sshResolve({ ...sshBase, port: 2222 }), 'ssh-port')
assert.equal(sshResolve({ ...sshBase, keyPath: '/keys/b' }), 'ssh-key')
assert.equal(sshResolve({ ...sshBase, remoteHermesPath: '/opt/hermes' }), 'ssh-path')
assert.equal(sshResolve({ ...sshBase, remoteProfile: 'beta' }), 'ssh-profile')
assert.equal(resolvedConnectionId(sshRegistry, {
  mode: 'remote', remoteKind: 'ssh', remoteHost: 'ROOT@WORK-HOST'
}), null)
const duplicateSsh: ConnectionRegistry = {
  ...sshRegistry,
  connections: [...sshRegistry.connections, { ...sshRegistry.connections[1], id: 'ssh-base-copy' }]
}
assert.equal(resolvedConnectionId(duplicateSsh, {
  mode: 'remote', remoteKind: 'ssh', ssh: sshBase
}), null)

const singleLegacy: ConnectionRegistry = {
  version: REGISTRY_VERSION,
  primary: 'one',
  launchMode: 'primary',
  lastUsed: 'one',
  connections: [
    { id: LOCAL_CONNECTION_ID, kind: 'local', label: 'This device' },
    { id: 'one', kind: 'remote', label: 'One', url: 'https://one.example', authMode: 'token' },
    { id: 'ssh-one', kind: 'ssh', label: 'SSH one', host: 'one-host', user: 'root' }
  ]
}
assert.equal(resolvedConnectionId(singleLegacy, {
  mode: 'remote', remoteKind: 'url', baseUrl: 'https://one.example/'
}), 'one')
assert.equal(resolvedConnectionId(singleLegacy, {
  mode: 'remote', remoteKind: 'ssh', remoteHost: 'root@one-host'
}), 'ssh-one')
assert.equal(resolvedConnectionId(singleLegacy, {
  mode: 'remote', remoteKind: 'url', baseUrl: 'https://none.example'
}), null)

console.log('PR #90048 exact route identity harness: all assertions passed')

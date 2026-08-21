from pathlib import Path
from textwrap import dedent

path = Path('apps/desktop/electron/connection-registry.ts')
text = path.read_text()
import_line = "import { matchingConnectionId, type StoredRoute } from './connection-route-identity'\n"
anchor = "} from './connection-config'\n"

if import_line not in text:
    if anchor not in text:
        raise SystemExit('connection-config import anchor not found')
    text = text.replace(anchor, anchor + import_line, 1)

start = text.index('export interface ResolvedConnectionDescriptor {')
end = text.index("/**\n * How the registry's 'local' entry", start)
replacement = dedent(
    '''\
    export interface ResolvedConnectionSshDescriptor {
      host?: string
      keyPath?: string
      port?: number
      remoteHermesPath?: string
      remoteProfile?: string
      user?: string
    }

    export interface ResolvedConnectionDescriptor {
      authMode?: unknown
      baseUrl?: string
      /** Present on registry-qualified routes. Presence is authoritative even
       * when the value is invalid: only descriptors with no such property may
       * use legacy transport inference. */
      connectionId?: null | string
      headers?: Record<string, unknown>
      mode?: 'local' | 'remote'
      org?: unknown
      remoteHost?: string
      remoteKind?: 'cloud' | 'ssh' | 'url'
      ssh?: null | ResolvedConnectionSshDescriptor
      token?: unknown
    }

    /**
     * Recover registry identity for a descriptor resolved through the legacy v1
     * profile path. Registry-scoped routes already carry `connectionId`; that
     * exact identity is authoritative only while it names a current registration.
     * Only genuinely unqualified legacy descriptors infer, and inference uses the
     * same complete pre-dial envelope as Desktop route selection.
     */
    export function resolvedConnectionId(
      registry: ConnectionRegistry,
      descriptor: ResolvedConnectionDescriptor
    ): null | string {
      if (Object.prototype.hasOwnProperty.call(descriptor, 'connectionId')) {
        const connectionId = descriptor.connectionId

        if (typeof connectionId !== 'string' || !connectionId) {
          return null
        }

        return registry.connections.some(connection => connection.id === connectionId) ? connectionId : null
      }

      if (descriptor.mode === 'local') {
        const localConnections = registry.connections.filter(connection => connection.kind === 'local')

        return localConnections.length === 1 ? localConnections[0].id : null
      }

      if (descriptor.mode !== 'remote') {
        return null
      }

      if (descriptor.remoteKind === 'ssh') {
        if (Object.prototype.hasOwnProperty.call(descriptor, 'ssh')) {
          if (!descriptor.ssh || typeof descriptor.ssh !== 'object') {
            return null
          }

          return matchingConnectionId(registry, { ...descriptor.ssh, kind: 'ssh' }, 'unique') ?? null
        }

        // Old descriptors expose only user@host after the tunnel has discarded
        // port/key/path/profile. That weak shape is compatible only when exactly
        // one registered SSH source shares the target and its defaulted route
        // still satisfies the canonical full-envelope matcher.
        const ssh = normalizeSshConfig({ mode: 'ssh', host: descriptor.remoteHost })

        if (!ssh) {
          return null
        }

        const target = normalizedSshTarget(ssh)
        const coarseMatches = registry.connections.filter(
          connection => connection.kind === 'ssh' && normalizedSshTarget(connection) === target
        )

        if (!target || coarseMatches.length !== 1) {
          return null
        }

        return matchingConnectionId(registry, { kind: 'ssh', ...ssh }, 'unique') ?? null
      }

      const kind = descriptor.remoteKind === 'cloud' ? 'cloud' : descriptor.remoteKind === 'url' ? 'remote' : null

      if (!kind) {
        return null
      }

      let url = ''

      try {
        url = normalizeRemoteBaseUrl(descriptor.baseUrl)
      } catch {
        return null
      }

      const authMode = normAuthMode(descriptor.authMode)
      const route: StoredRoute = {
        authMode,
        headers: descriptor.headers,
        kind,
        org: descriptor.org,
        token: descriptor.token,
        url
      }
      const hasExactEnvelope =
        Object.prototype.hasOwnProperty.call(descriptor, 'authMode') &&
        Object.prototype.hasOwnProperty.call(descriptor, 'headers') &&
        (authMode === 'oauth' || Object.prototype.hasOwnProperty.call(descriptor, 'token')) &&
        (kind === 'remote' || Object.prototype.hasOwnProperty.call(descriptor, 'org'))

      if (!hasExactEnvelope) {
        // A URL alone cannot choose among legal registrations that differ by
        // auth, headers, Cloud organization, or account. Require one coarse
        // candidate before the shared full-envelope matcher may accept legacy
        // defaults; zero or multiple candidates fail closed.
        const coarseMatches = registry.connections.filter(connection => {
          if (connection.kind !== kind) {
            return false
          }

          try {
            return normalizeRemoteBaseUrl(connection.url) === url
          } catch {
            return false
          }
        })

        if (coarseMatches.length !== 1) {
          return null
        }
      }

      return matchingConnectionId(registry, route, 'unique') ?? null
    }

    function normalizedSshTarget(route: {
      host?: unknown
      port?: unknown
      user?: unknown
    }): null | string {
      const ssh = normalizeSshConfig({ ...route, mode: 'ssh' })

      if (!ssh) {
        return null
      }

      const host = String(ssh.host || '')
        .trim()
        .toLowerCase()
      const user = String(ssh.user || '')
        .trim()
        .toLowerCase()

      return user ? `${user}@${host}` : host
    }

    '''
)
text = text[:start] + replacement + text[end:]
path.write_text(text)

/**
 * Main-process registry route-generation authority.
 *
 * Renderer route receipts may name a stable connection id, but that id can be
 * edited in place to point at a different physical backend. Consequential
 * filesystem/Git requests therefore carry the current registry generation and
 * this module verifies it before main resolves or dials the source.
 */

interface RegistryGenerationEntry {
  generation?: unknown
  id?: unknown
}

interface RouteGenerationRequest {
  connectionId?: unknown
  method?: unknown
  path?: unknown
  routeGeneration?: unknown
}

const currentGenerations = new Map<string, string>()

const GENERATION_GATED_MUTATIONS = new Set([
  'POST /api/fs/write-text',
  'POST /api/git/branch/switch',
  'POST /api/git/review/commit',
  'POST /api/git/review/create-pr',
  'POST /api/git/review/push',
  'POST /api/git/review/revert',
  'POST /api/git/review/stage',
  'POST /api/git/review/unstage',
  'POST /api/git/worktree/add',
  'POST /api/git/worktree/remove'
])

function clean(value: unknown): string {
  return String(value ?? '').trim()
}

function requestRoute(request: RouteGenerationRequest | null | undefined): string {
  const method = clean(request?.method || 'GET').toUpperCase()
  const rawPath = clean(request?.path)

  if (!rawPath) {
    return `${method} `
  }

  try {
    return `${method} ${new URL(rawPath, 'https://hermes.invalid').pathname}`
  } catch {
    return `${method} ${rawPath.split(/[?#]/, 1)[0]}`
  }
}

export function apiRequestRequiresRouteGeneration(request: RouteGenerationRequest | null | undefined): boolean {
  return GENERATION_GATED_MUTATIONS.has(requestRoute(request))
}

export function replaceRegistryRouteGenerations(entries: Iterable<RegistryGenerationEntry>): void {
  currentGenerations.clear()

  for (const entry of entries) {
    const id = clean(entry.id)
    const generation = clean(entry.generation)

    if (id && generation) {
      currentGenerations.set(id, generation)
    }
  }
}

export function rememberRegistryRouteGeneration(idValue: unknown, generationValue: unknown): void {
  const id = clean(idValue)
  const generation = clean(generationValue)

  if (id && generation) {
    currentGenerations.set(id, generation)
  }
}

export function forgetRegistryRouteGeneration(idValue: unknown): void {
  const id = clean(idValue)

  if (id) {
    currentGenerations.delete(id)
  }
}

export function assertApiRequestRouteGeneration(
  request: RouteGenerationRequest | null | undefined,
  connectionIdValue: unknown
): void {
  if (!apiRequestRequiresRouteGeneration(request)) {
    return
  }

  const connectionId = clean(connectionIdValue)

  if (!connectionId) {
    throw new Error('Remote filesystem/Git mutation requires an exact registered connection route.')
  }

  const supplied = clean(request?.routeGeneration)
  const current = currentGenerations.get(connectionId)

  if (!supplied || !current || supplied !== current) {
    throw new Error('Remote filesystem/Git mutation route is stale or has been replaced; reactivate the connection.')
  }
}

/** @internal Test-only visibility without exposing the mutable map. */
export function currentRegistryRouteGeneration(connectionIdValue: unknown): null | string {
  return currentGenerations.get(clean(connectionIdValue)) ?? null
}

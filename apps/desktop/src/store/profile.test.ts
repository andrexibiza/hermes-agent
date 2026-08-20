import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import type { ProfileInfo } from '@/types/hermes'

// Keep profile.ts's side-effecting imports inert: the gateway socket layer and
// the REST query client must not run for real in a unit test.
const ensureGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const ensureGatewayForAgent = vi.fn(async (_connectionId: null | string, _profile: string) => undefined)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()
const activationProof = vi.hoisted(() => ({
  activeRouteMatches: vi.fn(),
  activateAgent: vi.fn(),
  activateProfile: vi.fn(),
  receiptIsCurrent: vi.fn()
}))

vi.mock('@/store/gateway', () => ({ $gateway, ensureGatewayForAgent, ensureGatewayForProfile, openGatewayForProfile }))
vi.mock('@/store/gateway-activation', () => ({
  activeGatewayRouteMatches: activationProof.activeRouteMatches,
  activateGatewayAgentWithProof: activationProof.activateAgent,
  activateGatewayProfileWithProof: activationProof.activateProfile,
  routeActivationReceiptIsCurrent: activationProof.receiptIsCurrent
}))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph }))

const {
  $activeGatewayProfile,
  $profiles,
  ensureGatewayProfile,
  invalidateProfileListFetches,
  prewarmProfileBackend,
  refreshProfiles
} = await import('./profile')

const { $connection } = await import('./session')
const { invalidateProfileScopedQueries } = await import('@/lib/query-client')
const { getProfiles } = await import('@/hermes')

const profile = (name: string, isDefault = false): ProfileInfo => ({
  has_env: false,
  is_default: isDefault,
  model: null,
  name,
  path: `/tmp/hermes/${name}`,
  provider: null,
  skill_count: 0
})

const remoteConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: 'https://hermes-roy.tail.ts.net', mode: 'remote', profile: 'vps-remote', ...over }) as HermesConnection

const localConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as HermesConnection

const getConnection = vi.fn<(profile?: string | null) => Promise<HermesConnection>>()

type ConnectionResolver =
  | Promise<HermesConnection | null>
  | (() => Promise<HermesConnection | null>)

function startConnectionResolver(resolver: ConnectionResolver): Promise<HermesConnection | null> {
  return typeof resolver === 'function' ? resolver() : resolver
}

function receipt(connectionId: null | string, profile: string, connection: HermesConnection | null) {
  const route = { connectionId, profile }

  return {
    activationGeneration: 1,
    connection,
    gateway: $gateway.get(),
    physicalRoute: route,
    readiness: { descriptor: 'resolved', gateway: 'open', route: 'exact' },
    requested: route,
    route,
    status: 'ready' as const
  }
}

beforeEach(() => {
  getConnection.mockReset()
  ensureGatewayForAgent.mockReset()
  ensureGatewayForAgent.mockResolvedValue(undefined)
  ensureGatewayForProfile.mockReset()
  ensureGatewayForProfile.mockResolvedValue(undefined)
  openGatewayForProfile.mockReset()
  openGatewayForProfile.mockResolvedValue(undefined)
  activationProof.activeRouteMatches.mockReset()
  activationProof.activeRouteMatches.mockImplementation(
    ({ connectionId, profile }: { connectionId: null | string; profile: string }) =>
      connectionId === null && profile === $activeGatewayProfile.get() && Boolean($gateway.get())
  )
  activationProof.activateProfile.mockReset()
  activationProof.activateProfile.mockImplementation(
    async (profile: string, connectionResolver: ConnectionResolver) => {
      const connectionPromise = startConnectionResolver(connectionResolver)
      const gatewayPromise = ensureGatewayForProfile(profile)
      const [connection] = await Promise.all([connectionPromise, gatewayPromise])

      return receipt(null, profile, connection)
    }
  )
  activationProof.activateAgent.mockReset()
  activationProof.activateAgent.mockImplementation(
    async (connectionId: string, profile: string, connectionResolver: ConnectionResolver) => {
      const connectionPromise = startConnectionResolver(connectionResolver)
      const gatewayPromise = ensureGatewayForAgent(connectionId, profile)
      const [connection] = await Promise.all([connectionPromise, gatewayPromise])

      return receipt(connectionId, profile, connection)
    }
  )
  activationProof.receiptIsCurrent.mockReset()
  activationProof.receiptIsCurrent.mockImplementation(
    (value: { status: string }) => value.status === 'ready' || value.status === 'degraded'
  )
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  $connection.set(localConn())
  $profiles.set([])
  vi.stubGlobal('window', { hermesDesktop: { getConnection } })
  vi.mocked(invalidateProfileScopedQueries).mockClear()
  resetStarmapGraph.mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  $connection.set(null)
})

describe('ensureGatewayProfile → $connection sync (#46651)', () => {
  it('refreshes $connection to the remote descriptor when activating a remote pool profile', async () => {
    // Regression: the primary window backend is local, so $connection.mode is
    // "local". Activating the remote profile must flip it to "remote" — without
    // this, image attach uses path-based image.attach against the remote
    // gateway ("image not found: C:\\…") instead of image.attach_bytes.
    getConnection.mockResolvedValue(remoteConn())

    await ensureGatewayProfile('vps-remote')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('vps-remote')
    expect(getConnection).toHaveBeenCalledWith('vps-remote')
    expect($connection.get()?.mode).toBe('remote')
    expect($connection.get()?.profile).toBe('vps-remote')
  })

  it('resyncs $connection back to local when returning to the default profile', async () => {
    $activeGatewayProfile.set('vps-remote')
    $connection.set(remoteConn())
    getConnection.mockResolvedValue(localConn())

    await ensureGatewayProfile('default')

    expect(getConnection).toHaveBeenCalledWith('default')
    expect($connection.get()?.mode).toBe('local')
  })

  it('leaves the prior connection intact when the descriptor fetch fails', async () => {
    getConnection.mockRejectedValue(new Error('backend unreachable'))

    await ensureGatewayProfile('vps-remote')

    // Best-effort: boot/reconnect resyncs later; we must not null it out here.
    expect($connection.get()?.mode).toBe('local')
  })

  it('does not publish a descriptor from a superseded activation generation', async () => {
    getConnection.mockResolvedValue(remoteConn())
    activationProof.receiptIsCurrent.mockReturnValueOnce(false)

    await ensureGatewayProfile('vps-remote')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('vps-remote')
    expect(getConnection).toHaveBeenCalledWith('vps-remote')
    expect($activeGatewayProfile.get()).toBe('default')
    expect($connection.get()?.mode).toBe('local')
  })

  it('does not churn $connection when the exact target route is already active', async () => {
    $activeGatewayProfile.set('vps-remote')
    $connection.set(remoteConn())

    await ensureGatewayProfile('vps-remote')

    expect(activationProof.activeRouteMatches).toHaveBeenCalledWith({ connectionId: null, profile: 'vps-remote' })
    expect(getConnection).not.toHaveBeenCalled()
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($connection.get()?.mode).toBe('remote')
  })
})

describe('profile-scoped cache invalidation', () => {
  it('drops the memory graph cache when the active gateway profile changes', () => {
    $activeGatewayProfile.set('coder')

    expect(invalidateProfileScopedQueries).toHaveBeenCalled()
    expect(resetStarmapGraph).toHaveBeenCalledTimes(1)
  })
})

describe('prewarmProfileBackend (hover-intent pool spawn)', () => {
  it('opens the gateway (spawn + connect, no activation) for a non-active profile', () => {
    prewarmProfileBackend('warm-basic')

    expect(openGatewayForProfile).toHaveBeenCalledWith('warm-basic')
    // Pre-warm must never activate — that's the click's job.
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('skips the profile the gateway is already on', () => {
    $activeGatewayProfile.set('warm-active')

    prewarmProfileBackend('warm-active')

    expect(openGatewayForProfile).not.toHaveBeenCalled()
  })

  it('throttles repeat pre-warms for the same profile within the interval', () => {
    prewarmProfileBackend('warm-throttle-a')
    prewarmProfileBackend('warm-throttle-a')
    prewarmProfileBackend('warm-throttle-b')

    const calls = openGatewayForProfile.mock.calls.map(([name]) => name)
    expect(calls.filter(name => name === 'warm-throttle-a')).toHaveLength(1)
    expect(calls.filter(name => name === 'warm-throttle-b')).toHaveLength(1)
  })

  it('swallows spawn failures — error UX belongs to the real switch', () => {
    openGatewayForProfile.mockRejectedValueOnce(new Error('spawn failed'))

    expect(() => prewarmProfileBackend('warm-failing')).not.toThrow()
  })
})

describe('refreshProfiles shared rail list (#49289)', () => {
  it('removes a deleted profile from the shared $profiles cache after Manage Profiles refreshes', async () => {
    $profiles.set([profile('default', true), profile('test1')])
    vi.mocked(getProfiles).mockResolvedValueOnce({ profiles: [profile('default', true)] })

    await refreshProfiles()

    expect($profiles.get().map(profile => profile.name)).toEqual(['default'])
  })

  it('leaves the shared $profiles cache intact when the refresh fails', async () => {
    $profiles.set([profile('default', true), profile('test1')])
    vi.mocked(getProfiles).mockRejectedValueOnce(new Error('backend unavailable'))

    await expect(refreshProfiles()).rejects.toThrow('backend unavailable')

    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'test1'])
  })
})

describe('stale profile-list fetches across a backend switch (#85731)', () => {
  it('a late response from the previous backend cannot clobber the new backend list', async () => {
    // The disappearing-rail mechanism: /api/profiles is in flight against
    // backend A when the user applies a different remote/Cloud connection.
    // The soft re-home fetches backend B's list, then A's late (often empty /
    // default-only) response lands LAST and collapses the rail.
    let resolveOld: (value: { profiles: ProfileInfo[] }) => void = () => undefined
    vi.mocked(getProfiles).mockImplementationOnce(() => new Promise(resolve => (resolveOld = resolve)))

    const oldFetch = refreshProfiles() // in flight against backend A

    // Connection apply → soft re-home strands in-flight fetches...
    invalidateProfileListFetches()

    // ...and the new backend's list arrives.
    vi.mocked(getProfiles).mockResolvedValueOnce({
      profiles: [profile('default', true), profile('eric'), profile('coder')]
    })
    await refreshProfiles()
    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'eric', 'coder'])

    // Backend A's stale response finally lands: it must NOT overwrite $profiles.
    resolveOld({ profiles: [profile('default', true)] })
    await oldFetch

    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'eric', 'coder'])
  })

  it('a normal refresh still writes the cache after prior invalidations', async () => {
    invalidateProfileListFetches()
    vi.mocked(getProfiles).mockResolvedValueOnce({ profiles: [profile('default', true), profile('solo')] })

    await refreshProfiles()

    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'solo'])
  })

  it('strands in-flight fetches when the active gateway profile swaps backends', async () => {
    // Sibling site of the same class: a live profile swap moves /api/profiles
    // routing to another backend mid-fetch. The $activeGatewayProfile
    // subscriber must bump the epoch exactly like the connection-apply wipe.
    let resolveOld: (value: { profiles: ProfileInfo[] }) => void = () => undefined
    vi.mocked(getProfiles).mockImplementationOnce(() => new Promise(resolve => (resolveOld = resolve)))

    const oldFetch = refreshProfiles() // in flight against the old profile's backend

    $activeGatewayProfile.set('coder') // swap → subscriber invalidates

    vi.mocked(getProfiles).mockResolvedValueOnce({
      profiles: [profile('default', true), profile('coder')]
    })
    await refreshProfiles()

    resolveOld({ profiles: [] })
    await oldFetch

    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'coder'])
  })
})

import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

const $connection = atom<HermesConnection | null>(null)
const hermesApi = vi.fn()
const routeProof = vi.hoisted(() => ({ currentForMutation: vi.fn() }))

vi.mock('@/hermes', () => ({ hermesApi }))
vi.mock('@/store/gateway-activation', () => ({
  currentRouteActivationReceiptForMutation: routeProof.currentForMutation
}))
vi.mock('@/store/session', () => ({ $connection }))

const { desktopFsCacheKey, readDesktopFileText, writeDesktopFileText } = await import('./desktop-fs')

const localConnection = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as HermesConnection

const remoteConnection = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({
    baseUrl: 'https://homelab.invalid',
    connectionId: 'homelab',
    mode: 'remote',
    profile: 'research',
    registryScoped: true,
    routeGeneration: 'homelab-generation-1',
    ...over
  }) as HermesConnection

const api = vi.fn()
const readFileText = vi.fn()
const writeTextFile = vi.fn()

beforeEach(() => {
  api.mockReset()
  hermesApi.mockReset()
  readFileText.mockReset()
  routeProof.currentForMutation.mockReset()
  writeTextFile.mockReset()
  vi.stubGlobal('window', {
    hermesDesktop: {
      api,
      readFileText,
      writeTextFile
    }
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  $connection.set(null)
})

describe('Desktop FS proof-carrying route mutation (#90866 / #89916)', () => {
  it('keeps same-profile caches separate across registry owners', () => {
    const first = remoteConnection({ connectionId: 'homelab-a' })
    const second = remoteConnection({ connectionId: 'homelab-b' })

    expect(desktopFsCacheKey(first)).not.toBe(desktopFsCacheKey(second))
  })

  it('sends a remote write only to the exact owner, profile, and source generation carried by the receipt', async () => {
    const descriptor = remoteConnection()
    const receipt = {
      connection: descriptor,
      route: { connectionId: 'homelab', profile: 'research' },
      status: 'ready'
    }

    $connection.set(descriptor)
    routeProof.currentForMutation.mockReturnValue(receipt)
    api.mockResolvedValue({ ok: true, path: '/repo/README.md' })

    await expect(writeDesktopFileText('/repo/README.md', 'proof')).resolves.toEqual({ path: '/repo/README.md' })

    expect(routeProof.currentForMutation).toHaveBeenCalledWith(descriptor)
    expect(api).toHaveBeenCalledWith({
      body: { content: 'proof', path: '/repo/README.md' },
      connectionId: 'homelab',
      method: 'POST',
      path: '/api/fs/write-text',
      profile: 'research',
      routeGeneration: 'homelab-generation-1'
    })
    expect(hermesApi).not.toHaveBeenCalled()
  })

  it('rejects a remote write when the active route has no current exact mutation proof', async () => {
    const descriptor = remoteConnection()

    $connection.set(descriptor)
    routeProof.currentForMutation.mockReturnValue(null)

    await expect(writeDesktopFileText('/repo/README.md', 'unsafe')).rejects.toThrow(
      'Remote file mutation requires a current exact generated route activation'
    )

    expect(api).not.toHaveBeenCalled()
    expect(hermesApi).not.toHaveBeenCalled()
  })

  it('rejects a nominally ready receipt that carries no source generation', async () => {
    const descriptor = remoteConnection({ routeGeneration: undefined } as Partial<HermesConnection>)
    const receipt = {
      connection: descriptor,
      route: { connectionId: 'homelab', profile: 'research' },
      status: 'ready'
    }

    $connection.set(descriptor)
    routeProof.currentForMutation.mockReturnValue(receipt)

    await expect(writeDesktopFileText('/repo/README.md', 'unsafe')).rejects.toThrow(
      'Remote file mutation requires a current exact generated route activation'
    )
    expect(api).not.toHaveBeenCalled()
  })

  it('keeps local writes on hardened local IPC without requiring gateway route proof', async () => {
    $connection.set(localConnection())
    writeTextFile.mockResolvedValue({ path: '/tmp/local.txt' })

    await expect(writeDesktopFileText('/tmp/local.txt', 'local')).resolves.toEqual({ path: '/tmp/local.txt' })

    expect(writeTextFile).toHaveBeenCalledWith('/tmp/local.txt', 'local')
    expect(routeProof.currentForMutation).not.toHaveBeenCalled()
  })

  it('routes remote reads through the gateway-owned connection context instead of a profile-only bridge call', async () => {
    $connection.set(remoteConnection())
    hermesApi.mockResolvedValue({ path: '/repo/README.md', text: 'hello' })

    await readDesktopFileText('/repo/README.md')

    expect(hermesApi).toHaveBeenCalledWith({
      path: '/api/fs/read-text?path=%2Frepo%2FREADME.md',
      profile: 'research'
    })
    expect(api).not.toHaveBeenCalled()
  })
})

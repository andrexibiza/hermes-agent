import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import { setApiRequestConnection } from '@/hermes'
import { $connection } from '@/store/session'

import { desktopGit } from './desktop-git'

const routeProof = vi.hoisted(() => ({ currentForMutation: vi.fn() }))

vi.mock('@/store/gateway-activation', () => ({
  currentRouteActivationReceiptForMutation: routeProof.currentForMutation
}))

const repoStatus = vi.fn(async () => ({ branch: 'main' }))
const stage = vi.fn(async () => ({ ok: true }))
const worktreeList = vi.fn(async () => [{ branch: 'main', detached: false, isMain: true, locked: false, path: '/r' }])
const localGit = { repoStatus, review: { stage }, worktreeList }

const api = vi.fn(async ({ path }: { path: string }) => {
  if (path.startsWith('/api/git/status')) {
    return { branch: 'remote-main' }
  }

  if (path.startsWith('/api/git/worktrees')) {
    return { worktrees: [{ branch: 'main', detached: false, isMain: true, locked: false, path: '/srv/r' }] }
  }

  if (path.startsWith('/api/git/review/diff')) {
    return { diff: 'remote-diff' }
  }

  if (path.startsWith('/api/git/branches')) {
    return {
      branches: [{ checkedOut: false, isDefault: false, isRemote: true, name: 'origin/feature', worktreePath: null }]
    }
  }

  if (path.startsWith('/api/git/review/pr-list')) {
    return { pullRequests: [] }
  }

  return { ok: true }
})

type GeneratedHermesConnection = HermesConnection & { routeGeneration: string }

const remoteConnection = (
  connectionId: string,
  routeGeneration = `${connectionId}-generation-1`
): GeneratedHermesConnection => ({
  baseUrl: `https://${connectionId}.invalid`,
  connectionId,
  isFullscreen: false,
  logs: [],
  mode: 'remote',
  nativeOverlayWidth: 0,
  profile: 'research',
  registryScoped: true,
  routeGeneration,
  token: '',
  windowButtonPosition: null,
  wsUrl: `wss://${connectionId}.invalid/api/ws`
})

const readyReceipt = (connectionId: string) => ({
  connection: remoteConnection(connectionId),
  route: { connectionId, profile: 'research' },
  status: 'ready'
})

describe('desktop git facade', () => {
  beforeEach(() => {
    vi.stubGlobal('window', { hermesDesktop: { api, git: localGit } })
    api.mockClear()
    repoStatus.mockClear()
    routeProof.currentForMutation.mockReset()
    stage.mockClear()
    worktreeList.mockClear()
    setApiRequestConnection(null)
    $connection.set(null)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    setApiRequestConnection(null)
    $connection.set(null)
  })

  it('returns undefined after the renderer global is torn down', () => {
    vi.stubGlobal('window', undefined)

    expect(desktopGit()).toBeUndefined()
  })

  it('keeps local reads and mutations on Electron without requiring route proof', async () => {
    $connection.set({ mode: 'local' } as never)

    await expect(desktopGit()?.repoStatus('/work')).resolves.toEqual({ branch: 'main' })
    await desktopGit()?.review.stage('/work', 'a.txt')

    expect(repoStatus).toHaveBeenCalledWith('/work')
    expect(stage).toHaveBeenCalledWith('/work', 'a.txt')
    expect(api).not.toHaveBeenCalled()
    expect(routeProof.currentForMutation).not.toHaveBeenCalled()
  })

  it('keeps same-profile Git reads on their exact registry owners', async () => {
    $connection.set(remoteConnection('homelab-a'))
    setApiRequestConnection('homelab-a')

    await expect(desktopGit()?.repoStatus('/srv/work')).resolves.toEqual({ branch: 'remote-main' })
    expect(api).toHaveBeenLastCalledWith({
      connectionId: 'homelab-a',
      path: '/api/git/status?path=%2Fsrv%2Fwork',
      profile: 'research'
    })

    api.mockClear()
    $connection.set(remoteConnection('homelab-b'))
    setApiRequestConnection('homelab-b')

    await desktopGit()?.repoStatus('/srv/work')
    expect(api).toHaveBeenLastCalledWith({
      connectionId: 'homelab-b',
      path: '/api/git/status?path=%2Fsrv%2Fwork',
      profile: 'research'
    })
    expect(routeProof.currentForMutation).not.toHaveBeenCalled()
  })

  it('preserves read envelopes and read-only POST queries on the active registry owner', async () => {
    $connection.set(remoteConnection('homelab'))
    setApiRequestConnection('homelab')

    await expect(desktopGit()?.worktreeList('/srv/work')).resolves.toEqual([
      { branch: 'main', detached: false, isMain: true, locked: false, path: '/srv/r' }
    ])
    await expect(desktopGit()?.review.diff('/srv/work', 'a.txt', 'uncommitted', null, false)).resolves.toBe(
      'remote-diff'
    )
    await desktopGit()?.review.prList('/srv/work', ['feature'], [123])

    expect(api).toHaveBeenLastCalledWith({
      body: { branches: ['feature'], numbers: [123], path: '/srv/work' },
      connectionId: 'homelab',
      method: 'POST',
      path: '/api/git/review/pr-list',
      profile: 'research'
    })
    expect(routeProof.currentForMutation).not.toHaveBeenCalled()
  })

  it('routes a permitted Git mutation from the receipt rather than ambient profile state', async () => {
    const descriptor = remoteConnection('homelab-a')

    $connection.set(descriptor)
    setApiRequestConnection('homelab-b')
    routeProof.currentForMutation.mockReturnValue(readyReceipt('homelab-a'))

    await desktopGit()?.review.stage('/srv/work', 'a.txt')

    expect(routeProof.currentForMutation).toHaveBeenCalledWith(descriptor)
    expect(api).toHaveBeenCalledWith({
      body: { file: 'a.txt', path: '/srv/work' },
      connectionId: 'homelab-a',
      method: 'POST',
      path: '/api/git/review/stage',
      profile: 'research',
      routeGeneration: 'homelab-a-generation-1'
    })
  })

  it('rejects every remote Git mutation surface without current exact proof', () => {
    $connection.set(remoteConnection('homelab'))
    setApiRequestConnection('homelab')
    routeProof.currentForMutation.mockReturnValue(null)

    const git = desktopGit()

    expect(git).toBeDefined()

    if (!git) {
      throw new Error('remote Git facade unavailable')
    }

    const mutations = [
      () => git.worktreeAdd('/srv/work', { existingBranch: 'origin/feature' }),
      () => git.worktreeRemove('/srv/work', '/srv/worktree', { force: true }),
      () => git.branchSwitch('/srv/work', 'feature'),
      () => git.review.stage('/srv/work', 'a.txt'),
      () => git.review.unstage('/srv/work', 'a.txt'),
      () => git.review.revert('/srv/work', 'a.txt'),
      () => git.review.commit('/srv/work', 'message', false),
      () => git.review.push('/srv/work'),
      () => git.review.createPr('/srv/work')
    ]

    for (const mutate of mutations) {
      expect(mutate).toThrow('Remote Git mutation requires a current exact generated route activation')
    }

    expect(api).not.toHaveBeenCalled()
    expect(routeProof.currentForMutation).toHaveBeenCalledTimes(mutations.length)
  })

  it('rejects a nominally ready Git receipt without a source generation', () => {
    const descriptor = remoteConnection('homelab', '')

    $connection.set(descriptor)
    routeProof.currentForMutation.mockReturnValue({
      connection: descriptor,
      route: { connectionId: 'homelab', profile: 'research' },
      status: 'ready'
    })

    expect(() => desktopGit()?.review.push('/srv/work')).toThrow(
      'Remote Git mutation requires a current exact generated route activation'
    )
    expect(api).not.toHaveBeenCalled()
  })

  it('keeps the remote branch-to-worktree flow under one exact generated mutation owner', async () => {
    const descriptor = remoteConnection('homelab')

    $connection.set(descriptor)
    setApiRequestConnection('homelab')
    routeProof.currentForMutation.mockReturnValue(readyReceipt('homelab'))

    await expect(desktopGit()?.branchList('/srv/work')).resolves.toEqual([
      { checkedOut: false, isDefault: false, isRemote: true, name: 'origin/feature', worktreePath: null }
    ])
    await desktopGit()?.worktreeAdd('/srv/work', { existingBranch: 'origin/feature' })

    expect(api).toHaveBeenLastCalledWith({
      body: { existingBranch: 'origin/feature', path: '/srv/work' },
      connectionId: 'homelab',
      method: 'POST',
      path: '/api/git/worktree/add',
      profile: 'research',
      routeGeneration: 'homelab-generation-1'
    })
  })
})

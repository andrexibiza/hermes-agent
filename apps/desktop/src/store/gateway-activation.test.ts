import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

import { deferred } from '../test/deferred'

const gatewayState = vi.hoisted(() => ({
  connectionId: null as null | string,
  ensureAgent: vi.fn(),
  ensureProfile: vi.fn(),
  epoch: 0,
  gateway: null as null | { connectionState: string; id: string },
  primary: true,
  profile: 'default',
  registryList: vi.fn(),
  routeGenerations: new Map<string, string>()
}))
const $gateway = atom<unknown>(null)

vi.mock('@/store/gateway', () => ({
  $gateway,
  activeGatewayConnectionId: () => gatewayState.connectionId,
  activeGatewayProfileKey: () => gatewayState.profile,
  ensureGatewayForAgent: gatewayState.ensureAgent,
  ensureGatewayForProfile: gatewayState.ensureProfile,
  gatewayActivationEpoch: () => gatewayState.epoch,
  isActivePrimary: () => gatewayState.primary
}))

const {
  $routeActivationReceipt,
  activeGatewayRouteMatches,
  activateGatewayAgentWithProof,
  activateGatewayProfileWithProof,
  currentRouteActivationReceiptForMutation,
  routeActivationReceiptAllowsMutation,
  routeActivationReceiptIsCurrent
} = await import('./gateway-activation')

const connection = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as HermesConnection

function installGateway(id: string, connectionState = 'open'): void {
  gatewayState.gateway = { connectionState, id }
  $gateway.set(gatewayState.gateway)
}

function installRouteGeneration(connectionId: string, generation = `${connectionId}-generation-1`): void {
  gatewayState.routeGenerations.set(connectionId, generation)
}

beforeEach(() => {
  gatewayState.connectionId = null
  gatewayState.epoch = 0
  gatewayState.primary = true
  gatewayState.profile = 'default'
  installGateway('primary')
  gatewayState.ensureProfile.mockReset()
  gatewayState.ensureProfile.mockImplementation((profile: string) => {
    gatewayState.epoch += 1
    gatewayState.connectionId = null
    gatewayState.primary = profile === 'default'
    gatewayState.profile = profile

    return Promise.resolve()
  })
  gatewayState.ensureAgent.mockReset()
  gatewayState.ensureAgent.mockImplementation((connectionId: string, profile: string) => {
    gatewayState.epoch += 1
    gatewayState.connectionId = connectionId
    gatewayState.primary = false
    gatewayState.profile = profile

    return Promise.resolve(true)
  })
  gatewayState.routeGenerations.clear()
  for (const id of ['homelab', 'other-source', 'removed-source']) {
    installRouteGeneration(id)
  }
  gatewayState.registryList.mockReset()
  gatewayState.registryList.mockImplementation(async () => ({
    connections: [...gatewayState.routeGenerations].map(([id, generation]) => ({ generation, id }))
  }))
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      connections: {
        list: gatewayState.registryList
      }
    }
  })
  $routeActivationReceipt.set(null)
})

describe('proof-carrying gateway activation (#90866)', () => {
  it('returns ready proof for an exact local profile route and permits mutation', async () => {
    const descriptor = connection({ profile: 'worker' })
    const result = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))

    expect(result).toMatchObject({
      activationGeneration: 1,
      epistemic: 'authoritative',
      physicalRoute: { connectionId: null, profile: 'worker' },
      proof: { authority: 'gateway-activation', kind: 'local-physical-route' },
      readiness: { descriptor: 'resolved', gateway: 'open', route: 'exact' },
      requested: { connectionId: null, profile: 'worker' },
      route: { connectionId: null, profile: 'worker' },
      status: 'ready'
    })
    expect(routeActivationReceiptIsCurrent(result)).toBe(true)
    expect(routeActivationReceiptAllowsMutation(result, descriptor)).toBe(true)
    expect(currentRouteActivationReceiptForMutation(descriptor)).toBe(result)
    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'worker' })).toBe(true)
    expect($routeActivationReceipt.get()).toBe(result)
  })

  it('binds registry-agent proof to the exact connectionId, profile, and source generation', async () => {
    installGateway('homelab')
    const descriptor = connection({
      baseUrl: 'https://homelab.invalid',
      connectionId: 'homelab',
      mode: 'remote',
      profile: 'research',
      registryScoped: true
    })

    const result = await activateGatewayAgentWithProof('homelab', 'research', Promise.resolve(descriptor))

    expect(result).toMatchObject({
      connection: { routeGeneration: 'homelab-generation-1' },
      epistemic: 'authoritative',
      physicalRoute: { connectionId: 'homelab', profile: 'research' },
      proof: { kind: 'registry-scoped-descriptor' },
      requested: { connectionId: 'homelab', profile: 'research' },
      route: { connectionId: 'homelab', profile: 'research' },
      status: 'ready'
    })
    expect(result.status === 'ready' && routeActivationReceiptAllowsMutation(result, result.connection)).toBe(true)
    expect(activeGatewayRouteMatches({ connectionId: 'homelab', profile: 'research' })).toBe(true)
    expect(gatewayState.registryList).toHaveBeenCalledTimes(2)
  })

  it('rejects a route edited while descriptor activation is in flight', async () => {
    installGateway('homelab')
    let reads = 0
    gatewayState.registryList.mockImplementation(async () => {
      reads += 1

      return {
        connections: [{ generation: reads === 1 ? 'generation-1' : 'generation-2', id: 'homelab' }]
      }
    })

    const result = await activateGatewayAgentWithProof(
      'homelab',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'homelab',
          mode: 'remote',
          profile: 'research',
          registryScoped: true
        })
      )
    )

    expect(result).toMatchObject({
      reason: 'route-generation-changed',
      requested: { connectionId: 'homelab', profile: 'research' },
      status: 'failed'
    })
    expect(activeGatewayRouteMatches({ connectionId: 'homelab', profile: 'research' })).toBe(false)
    expect(currentRouteActivationReceiptForMutation(descriptorFrom(result))).toBe(null)
  })

  it('rejects a registry route whose generation cannot be proven', async () => {
    installGateway('homelab')
    gatewayState.registryList.mockResolvedValue({ connections: [{ id: 'homelab' }] })

    const result = await activateGatewayAgentWithProof(
      'homelab',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'homelab',
          mode: 'remote',
          profile: 'research',
          registryScoped: true
        })
      )
    )

    expect(result).toMatchObject({ reason: 'route-generation-unavailable', status: 'failed' })
    expect(activeGatewayRouteMatches({ connectionId: 'homelab', profile: 'research' })).toBe(false)
  })

  it('does not promote a legacy inferred remote connectionId to mutation authority', async () => {
    const descriptor = connection({
      baseUrl: 'https://remote.invalid',
      connectionId: 'first-similar-route',
      mode: 'remote',
      profile: 'worker'
    })

    const result = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))

    expect(result).toMatchObject({
      epistemic: 'inferred',
      proof: { kind: 'legacy-inferred-descriptor' },
      readiness: { descriptor: 'resolved', gateway: 'open', route: 'inferred' },
      route: { connectionId: 'first-similar-route', profile: 'worker' },
      status: 'degraded'
    })
    expect(routeActivationReceiptIsCurrent(result)).toBe(true)
    expect(routeActivationReceiptAllowsMutation(result, descriptor)).toBe(false)
    expect(currentRouteActivationReceiptForMutation(descriptor)).toBe(null)
    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'worker' })).toBe(false)
  })

  it('does not let an activation that never acquired a generation overwrite current proof', async () => {
    const currentDescriptor = connection({ profile: 'worker' })
    const current = await activateGatewayProfileWithProof('worker', Promise.resolve(currentDescriptor))

    gatewayState.ensureProfile.mockImplementationOnce((_profile: string) => Promise.resolve())

    const failed = await activateGatewayProfileWithProof(
      'coder',
      Promise.resolve(connection({ profile: 'coder' }))
    )

    expect(failed).toMatchObject({
      activationGeneration: 1,
      reason: 'target-unavailable',
      status: 'failed'
    })
    expect($routeActivationReceipt.get()).toBe(current)
    expect(currentRouteActivationReceiptForMutation(currentDescriptor)).toBe(current)
  })

  it('rejects the older receipt when another activation advances the generation', async () => {
    const gate = deferred()

    gatewayState.ensureProfile.mockImplementationOnce((_profile: string) => {
      gatewayState.epoch += 1

      return gate.promise
    })

    const older = activateGatewayProfileWithProof(
      'worker',
      Promise.resolve(connection({ profile: 'worker' }))
    )

    // A later route owner wins while the first activation is still pending.
    const newer = await activateGatewayProfileWithProof(
      'coder',
      Promise.resolve(connection({ profile: 'coder' }))
    )

    gate.resolve()

    const result = await older

    expect(result).toMatchObject({
      activationGeneration: 1,
      observedGeneration: 2,
      observedRoute: { connectionId: null, profile: 'coder' },
      reason: 'newer-activation',
      status: 'superseded'
    })
    expect(routeActivationReceiptIsCurrent(result)).toBe(false)
    expect(routeActivationReceiptAllowsMutation(result, null)).toBe(false)
    // A late stale completion cannot replace the observable proof from the
    // newer winner.
    expect($routeActivationReceipt.get()).toBe(newer)
  })

  it('reacquires ready proof after a route lands before its gateway opens', async () => {
    const descriptor = connection({ profile: 'worker' })
    installGateway('worker-connecting', 'connecting')

    const degraded = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))

    expect(degraded).toMatchObject({
      readiness: { descriptor: 'resolved', gateway: 'not-open', route: 'exact' },
      status: 'degraded'
    })
    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'worker' })).toBe(false)

    installGateway('worker-open', 'open')
    const recovered = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))

    expect(recovered).toMatchObject({
      activationGeneration: 2,
      readiness: { descriptor: 'resolved', gateway: 'open', route: 'exact' },
      status: 'ready'
    })
    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'worker' })).toBe(true)
    expect(currentRouteActivationReceiptForMutation(descriptor)).toBe(recovered)
  })

  it('reacquires ready proof after descriptor publication follows a degraded landing', async () => {
    const degraded = await activateGatewayProfileWithProof('worker', Promise.resolve(null))

    expect(degraded).toMatchObject({
      epistemic: 'unavailable',
      proof: { kind: 'unqualified-descriptor' },
      readiness: { descriptor: 'unavailable', gateway: 'open', route: 'unqualified' },
      status: 'degraded'
    })
    expect(routeActivationReceiptIsCurrent(degraded)).toBe(true)
    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'worker' })).toBe(false)

    const descriptor = connection({ profile: 'worker' })
    const recovered = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))

    expect(recovered).toMatchObject({ activationGeneration: 2, status: 'ready' })
    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'worker' })).toBe(true)
    expect(currentRouteActivationReceiptForMutation(descriptor)).toBe(recovered)
  })

  it('reacquires A after a failed B activation leaves A physically selected', async () => {
    installGateway('homelab')
    const descriptor = connection({
      baseUrl: 'https://homelab.invalid',
      connectionId: 'homelab',
      mode: 'remote',
      profile: 'research',
      registryScoped: true
    })
    const readyA = await activateGatewayAgentWithProof('homelab', 'research', Promise.resolve(descriptor))

    expect(readyA.status).toBe('ready')

    gatewayState.ensureAgent.mockImplementationOnce((_connectionId: string, _profile: string) => {
      gatewayState.epoch += 1

      return Promise.resolve(false)
    })

    const failedB = await activateGatewayAgentWithProof(
      'removed-source',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'removed-source',
          mode: 'remote',
          profile: 'research',
          registryScoped: true
        })
      )
    )

    expect(failedB).toMatchObject({ reason: 'target-unavailable', status: 'failed' })
    expect(gatewayState.connectionId).toBe('homelab')
    expect(activeGatewayRouteMatches({ connectionId: 'homelab', profile: 'research' })).toBe(false)

    const recoveredA = await activateGatewayAgentWithProof('homelab', 'research', Promise.resolve(descriptor))

    expect(recoveredA).toMatchObject({ activationGeneration: 3, status: 'ready' })
    expect(activeGatewayRouteMatches({ connectionId: 'homelab', profile: 'research' })).toBe(true)
    expect(recoveredA.status === 'ready' && currentRouteActivationReceiptForMutation(recoveredA.connection)).toBe(
      recoveredA
    )
  })

  it('retains shared-primary logical scope for display but reacquires mutation proof on reselection', async () => {
    gatewayState.ensureProfile.mockImplementationOnce((_profile: string) => {
      gatewayState.epoch += 1
      gatewayState.connectionId = null
      gatewayState.primary = true
      gatewayState.profile = 'default'

      return Promise.resolve()
    })

    const descriptor = connection({
      baseUrl: 'https://primary.invalid',
      connectionId: 'primary-remote',
      mode: 'remote',
      profile: 'worker',
      sharedPrimary: true
    })
    const result = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))

    expect(result).toMatchObject({
      epistemic: 'inferred',
      physicalRoute: { connectionId: null, profile: 'default' },
      readiness: { descriptor: 'resolved', gateway: 'open', route: 'inferred' },
      requested: { connectionId: null, profile: 'worker' },
      route: { connectionId: 'primary-remote', profile: 'worker' },
      status: 'degraded'
    })
    expect(routeActivationReceiptIsCurrent(result)).toBe(true)
    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'worker' })).toBe(false)
    expect(routeActivationReceiptAllowsMutation(result, descriptor)).toBe(false)
  })

  it('returns failed proof when an exact registry target disappears mid-activation', async () => {
    gatewayState.ensureAgent.mockImplementationOnce((_connectionId: string, _profile: string) => {
      gatewayState.epoch += 1

      return Promise.resolve(false)
    })

    const result = await activateGatewayAgentWithProof(
      'removed-source',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'removed-source',
          mode: 'remote',
          profile: 'research',
          registryScoped: true
        })
      )
    )

    expect(result).toMatchObject({
      reason: 'target-unavailable',
      requested: { connectionId: 'removed-source', profile: 'research' },
      status: 'failed'
    })
    expect(routeActivationReceiptIsCurrent(result)).toBe(false)
    expect(activeGatewayRouteMatches({ connectionId: 'removed-source', profile: 'research' })).toBe(false)
  })

  it('rejects a descriptor whose profile differs from the activated owner', async () => {
    installGateway('homelab')

    const result = await activateGatewayAgentWithProof(
      'homelab',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'homelab',
          mode: 'remote',
          profile: 'coder',
          registryScoped: true
        })
      )
    )

    expect(result).toMatchObject({
      reason: 'descriptor-owner-mismatch',
      requested: { connectionId: 'homelab', profile: 'research' },
      status: 'failed'
    })
  })

  it('rejects a registry descriptor that names a different owner than the activated socket', async () => {
    installGateway('homelab')

    const result = await activateGatewayAgentWithProof(
      'homelab',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'other-source',
          mode: 'remote',
          profile: 'research',
          registryScoped: true
        })
      )
    )

    expect(result).toMatchObject({
      reason: 'descriptor-owner-mismatch',
      requested: { connectionId: 'homelab', profile: 'research' },
      status: 'failed'
    })
  })

  it('invalidates a landed receipt when the active gateway realization changes', async () => {
    const descriptor = connection({ profile: 'worker' })
    const result = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))

    expect(routeActivationReceiptIsCurrent(result)).toBe(true)

    gatewayState.epoch += 1
    gatewayState.profile = 'coder'
    installGateway('coder')

    expect(routeActivationReceiptIsCurrent(result)).toBe(false)
    expect(routeActivationReceiptAllowsMutation(result, descriptor)).toBe(false)
  })

  it('retains mutation authority across descriptor clones that preserve the exact local route proof', async () => {
    const descriptor = connection({ profile: 'worker' })
    const result = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))
    const replacement = connection({ isFullscreen: true, profile: 'worker' })

    expect(routeActivationReceiptIsCurrent(result)).toBe(true)
    expect(routeActivationReceiptAllowsMutation(result, replacement)).toBe(true)
    expect(currentRouteActivationReceiptForMutation(replacement)).toBe(result)
  })

  it('retains registry authority across presentation clones with the same source generation', async () => {
    installGateway('homelab')
    const result = await activateGatewayAgentWithProof(
      'homelab',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'homelab',
          mode: 'remote',
          profile: 'research',
          registryScoped: true
        })
      )
    )

    expect(result.status).toBe('ready')

    if (result.status !== 'ready') {
      throw new Error('expected ready route')
    }

    const replacement = { ...result.connection, isFullscreen: true }

    expect(routeActivationReceiptAllowsMutation(result, replacement)).toBe(true)
  })

  it('withholds registry authority after source generation replacement under the same id', async () => {
    installGateway('homelab')
    const result = await activateGatewayAgentWithProof(
      'homelab',
      'research',
      Promise.resolve(
        connection({
          connectionId: 'homelab',
          mode: 'remote',
          profile: 'research',
          registryScoped: true
        })
      )
    )

    expect(result.status).toBe('ready')

    if (result.status !== 'ready') {
      throw new Error('expected ready route')
    }

    const replacement = { ...result.connection, routeGeneration: 'homelab-generation-2' }

    expect(routeActivationReceiptAllowsMutation(result, replacement)).toBe(false)
    expect(currentRouteActivationReceiptForMutation(replacement)).toBe(null)
  })

  it('withholds mutation authority when a replacement descriptor changes the proven profile', async () => {
    const descriptor = connection({ profile: 'worker' })
    const result = await activateGatewayProfileWithProof('worker', Promise.resolve(descriptor))
    const replacement = connection({ profile: 'coder' })

    expect(routeActivationReceiptIsCurrent(result)).toBe(true)
    expect(routeActivationReceiptAllowsMutation(result, replacement)).toBe(false)
    expect(currentRouteActivationReceiptForMutation(replacement)).toBe(null)
  })

  it('does not treat a same-named profile on another source as the local route', async () => {
    installGateway('homelab')
    const descriptor = connection({
      connectionId: 'homelab',
      mode: 'remote',
      profile: 'research',
      registryScoped: true
    })

    await activateGatewayAgentWithProof('homelab', 'research', Promise.resolve(descriptor))

    expect(activeGatewayRouteMatches({ connectionId: null, profile: 'research' })).toBe(false)
    expect(activeGatewayRouteMatches({ connectionId: 'homelab', profile: 'research' })).toBe(true)
  })
})

function descriptorFrom(receipt: unknown): HermesConnection | null {
  if (!receipt || typeof receipt !== 'object') {
    return null
  }

  return ((receipt as { connection?: HermesConnection | null }).connection ?? null) as HermesConnection | null
}

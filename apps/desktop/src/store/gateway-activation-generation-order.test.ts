import { atom } from 'nanostores'
import { beforeEach, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

const state = vi.hoisted(() => ({
  connectionId: null as null | string,
  ensureAgent: vi.fn(),
  epoch: 0,
  gateway: { connectionState: 'open', id: 'gateway' },
  profile: 'default'
}))
const $gateway = atom<unknown>(state.gateway)

vi.mock('@/store/gateway', () => ({
  $gateway,
  activeGatewayConnectionId: () => state.connectionId,
  activeGatewayProfileKey: () => state.profile,
  ensureGatewayForAgent: state.ensureAgent,
  ensureGatewayForProfile: vi.fn(),
  gatewayActivationEpoch: () => state.epoch,
  isActivePrimary: () => false
}))

const { $routeActivationReceipt, activateGatewayAgentWithProof } = await import('./gateway-activation')

beforeEach(() => {
  state.connectionId = null
  state.epoch = 0
  state.profile = 'default'
  state.ensureAgent.mockReset()
  state.ensureAgent.mockImplementation((connectionId: string, profile: string) => {
    state.epoch += 1
    state.connectionId = connectionId
    state.profile = profile

    return Promise.resolve(true)
  })
  $gateway.set(state.gateway)
  $routeActivationReceipt.set(null)
})

it('does not start descriptor or gateway resolution before generation N is observed', async () => {
  let releaseGeneration!: () => void
  const generationGate = new Promise<void>(resolve => {
    releaseGeneration = resolve
  })
  let listCalls = 0
  let descriptorStarted = false

  vi.stubGlobal('window', {
    hermesDesktop: {
      connections: {
        list: vi.fn(async () => {
          listCalls += 1

          if (listCalls === 1) {
            await generationGate
          }

          return { connections: [{ generation: 'generation-1', id: 'homelab' }] }
        })
      }
    }
  })

  const activation = activateGatewayAgentWithProof('homelab', 'research', async () => {
    descriptorStarted = true

    return {
      baseUrl: 'https://homelab.test',
      connectionId: 'homelab',
      mode: 'remote',
      profile: 'research',
      registryScoped: true
    } as HermesConnection
  })

  await Promise.resolve()
  expect(descriptorStarted).toBe(false)
  expect(state.ensureAgent).not.toHaveBeenCalled()

  releaseGeneration()
  const receipt = await activation

  expect(descriptorStarted).toBe(true)
  expect(state.ensureAgent).toHaveBeenCalledWith('homelab', 'research')
  expect(receipt).toMatchObject({
    connection: { routeGeneration: 'generation-1' },
    status: 'ready'
  })

  vi.unstubAllGlobals()
})

import { atom } from 'nanostores'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

const state = vi.hoisted(() => ({
  connectionId: null as null | string,
  epoch: 0,
  gateway: { connectionState: 'open', id: 'gateway' },
  profile: 'default'
}))
const $gateway = atom<unknown>(state.gateway)

vi.mock('@/store/gateway', () => ({
  $gateway,
  activeGatewayConnectionId: () => state.connectionId,
  activeGatewayProfileKey: () => state.profile,
  ensureGatewayForAgent: vi.fn(async (connectionId: string, profile: string) => {
    state.epoch += 1
    state.connectionId = connectionId
    state.profile = profile

    return true
  }),
  ensureGatewayForProfile: vi.fn(),
  gatewayActivationEpoch: () => state.epoch,
  isActivePrimary: () => false
}))

const {
  $routeActivationReceipt,
  activateGatewayAgentWithProof,
  currentRouteActivationReceiptForMutation,
  routeActivationReceiptAllowsMutation
} = await import('./gateway-activation')

const descriptor = (routeGeneration?: string): HermesConnection =>
  ({
    baseUrl: 'https://homelab.test',
    connectionId: 'homelab',
    mode: 'remote',
    profile: 'research',
    registryScoped: true,
    ...(routeGeneration ? { routeGeneration } : {})
  }) as HermesConnection

beforeEach(() => {
  state.connectionId = null
  state.epoch = 0
  state.profile = 'default'
  $gateway.set(state.gateway)
  $routeActivationReceipt.set(null)
  vi.stubGlobal('window', {
    hermesDesktop: {
      connections: {
        list: vi.fn(async () => ({
          connections: [{ generation: 'generation-1', id: 'homelab' }]
        }))
      }
    }
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

it('retains renderer proof when reconnect refreshes an equivalent descriptor without copying generation', async () => {
  const receipt = await activateGatewayAgentWithProof('homelab', 'research', async () => descriptor())

  expect(receipt.status).toBe('ready')

  if (receipt.status !== 'ready') {
    throw new Error('expected ready route')
  }

  const reconnectDescriptor = descriptor()

  expect(routeActivationReceiptAllowsMutation(receipt, reconnectDescriptor)).toBe(true)
  expect(currentRouteActivationReceiptForMutation(reconnectDescriptor)).toBe(receipt)
})

it('still rejects an explicitly different generation before the main-process gate', async () => {
  const receipt = await activateGatewayAgentWithProof('homelab', 'research', async () => descriptor())

  expect(receipt.status).toBe('ready')

  if (receipt.status !== 'ready') {
    throw new Error('expected ready route')
  }

  const replacementDescriptor = descriptor('generation-2')

  expect(routeActivationReceiptAllowsMutation(receipt, replacementDescriptor)).toBe(false)
  expect(currentRouteActivationReceiptForMutation(replacementDescriptor)).toBe(null)
})

import { atom } from 'nanostores'

import type { HermesConnection } from '@/global'
import type { HermesGateway } from '@/hermes'
import {
  $gateway,
  activeGatewayConnectionId,
  activeGatewayProfileKey,
  ensureGatewayForAgent,
  ensureGatewayForProfile,
  gatewayActivationEpoch,
  isActivePrimary
} from '@/store/gateway'

/** Exact owner of one Desktop route. A profile name is not globally unique. */
export interface RouteOwnerRef {
  connectionId: null | string
  profile: string
}

export type RouteActivationEpistemic = 'authoritative' | 'inferred' | 'unavailable'

export type RouteActivationProofKind =
  | 'legacy-inferred-descriptor'
  | 'local-physical-route'
  | 'registry-scoped-descriptor'
  | 'unqualified-descriptor'

export type RouteActivationReadiness = {
  descriptor: 'resolved' | 'unavailable'
  gateway: 'open' | 'not-open' | 'unavailable'
  route: 'exact' | 'inferred' | 'unqualified'
}

type ConnectionResolver =
  | Promise<HermesConnection | null>
  | (() => Promise<HermesConnection | null>)

type RouteGenerationBoundConnection = HermesConnection & {
  routeGeneration?: string
}

type LandedRouteActivationBase = {
  activationGeneration: number
  connection: HermesConnection | null
  epistemic: RouteActivationEpistemic
  gateway: HermesGateway | null
  physicalRoute: RouteOwnerRef
  proof: {
    authority: 'gateway-activation'
    kind: RouteActivationProofKind
  }
  requested: RouteOwnerRef
  route: RouteOwnerRef
}

export type ReadyRouteActivationReceipt = LandedRouteActivationBase & {
  connection: HermesConnection
  epistemic: 'authoritative'
  readiness: {
    descriptor: 'resolved'
    gateway: 'open'
    route: 'exact'
  }
  status: 'ready'
}

export type DegradedRouteActivationReceipt = LandedRouteActivationBase & {
  readiness: RouteActivationReadiness
  status: 'degraded'
}

export type RejectedRouteActivationReceipt = {
  activationGeneration: number
  gateway: HermesGateway | null
  observedGeneration: number
  observedRoute: RouteOwnerRef
  reason:
    | 'descriptor-owner-mismatch'
    | 'newer-activation'
    | 'route-generation-changed'
    | 'route-generation-unavailable'
    | 'route-not-activated'
    | 'target-unavailable'
  requested: RouteOwnerRef
  status: 'failed' | 'superseded'
}

/**
 * Proof produced by the gateway activation boundary and consumed before the
 * renderer publishes route-dependent state or performs a route-dependent
 * mutation. The receipt retains owner, generation, gateway realization,
 * descriptor provenance, and readiness instead of projecting them to a bare
 * profile or boolean (#90866).
 */
export type RouteActivationReceipt =
  | DegradedRouteActivationReceipt
  | ReadyRouteActivationReceipt
  | RejectedRouteActivationReceipt

/**
 * Latest activation verdict. Consumers must still revalidate it at the use
 * site: completion order is not authority order, and a reconnect may replace
 * the descriptor realization without changing the logical route.
 */
export const $routeActivationReceipt = atom<RouteActivationReceipt | null>(null)

const normalizeProfile = (profile: string | null | undefined): string => (profile ?? '').trim() || 'default'

const normalizeConnectionId = (connectionId: string | null | undefined): null | string =>
  (connectionId ?? '').trim() || null

const normalizeRouteGeneration = (generation: unknown): null | string => String(generation ?? '').trim() || null

function routeOwner(connectionId: string | null | undefined, profile: string | null | undefined): RouteOwnerRef {
  return {
    connectionId: normalizeConnectionId(connectionId),
    profile: normalizeProfile(profile)
  }
}

function sameRoute(left: RouteOwnerRef, right: RouteOwnerRef): boolean {
  return left.connectionId === right.connectionId && left.profile === right.profile
}

function currentPhysicalRoute(): RouteOwnerRef {
  return routeOwner(activeGatewayConnectionId(), activeGatewayProfileKey())
}

function currentGatewayReadiness(gateway: HermesGateway | null): RouteActivationReadiness['gateway'] {
  if (!gateway) {
    return 'unavailable'
  }

  return gateway.connectionState === 'open' ? 'open' : 'not-open'
}

function publishReceipt(receipt: RouteActivationReceipt): RouteActivationReceipt {
  const current = $routeActivationReceipt.get()

  // Completion order is not authority order. A slow generation N may finish
  // after generation N+1; never let that stale completion replace the newer
  // observable proof. The direct N receipt is still returned to its caller so
  // that operation can deterministically reject itself.
  if (!current || receipt.activationGeneration > current.activationGeneration) {
    $routeActivationReceipt.set(receipt)
  }

  return receipt
}

function startConnectionResolution(resolver: ConnectionResolver): Promise<HermesConnection | null> {
  return typeof resolver === 'function' ? resolver() : resolver
}

async function registryRouteGeneration(connectionId: string): Promise<null | string> {
  const list = window.hermesDesktop?.connections?.list

  if (!list) {
    return null
  }

  try {
    const registry = (await list()) as unknown as {
      connections?: Array<{ generation?: unknown; id?: unknown }>
    }
    const entry = Array.isArray(registry?.connections)
      ? registry.connections.find(candidate => normalizeConnectionId(String(candidate?.id ?? '')) === connectionId)
      : undefined

    return normalizeRouteGeneration(entry?.generation)
  } catch {
    return null
  }
}

function routeGeneration(connection: HermesConnection | null): null | string {
  return normalizeRouteGeneration((connection as RouteGenerationBoundConnection | null)?.routeGeneration)
}

function bindRouteGeneration(connection: HermesConnection | null, generation: string): HermesConnection | null {
  return connection ? ({ ...connection, routeGeneration: generation } as RouteGenerationBoundConnection) : null
}

interface DescriptorRouteProof {
  epistemic: RouteActivationEpistemic
  mismatch: boolean
  proofKind: RouteActivationProofKind
  route: RouteOwnerRef
  routeReadiness: RouteActivationReadiness['route']
}

function descriptorRouteProof(
  kind: 'agent' | 'profile',
  requested: RouteOwnerRef,
  connection: HermesConnection | null,
  requireGeneration = true
): DescriptorRouteProof {
  if (!connection) {
    return {
      epistemic: 'unavailable',
      mismatch: false,
      proofKind: 'unqualified-descriptor',
      route: requested,
      routeReadiness: 'unqualified'
    }
  }

  const descriptorConnectionId = normalizeConnectionId(connection.connectionId)
  const descriptorProfile = normalizeProfile(connection.profile)
  const descriptorGeneration = routeGeneration(connection)
  const profileMismatch = descriptorProfile !== requested.profile

  if (kind === 'agent') {
    const mismatch =
      profileMismatch ||
      (descriptorConnectionId !== null && descriptorConnectionId !== requested.connectionId)

    if (
      connection.registryScoped === true &&
      descriptorConnectionId === requested.connectionId &&
      (!requireGeneration || descriptorGeneration !== null) &&
      !profileMismatch
    ) {
      return {
        epistemic: 'authoritative',
        mismatch: false,
        proofKind: 'registry-scoped-descriptor',
        route: requested,
        routeReadiness: 'exact'
      }
    }

    if (connection.registryScoped === true && descriptorConnectionId === requested.connectionId && !profileMismatch) {
      return {
        epistemic: 'unavailable',
        mismatch: false,
        proofKind: 'unqualified-descriptor',
        route: requested,
        routeReadiness: 'unqualified'
      }
    }

    return {
      epistemic: descriptorConnectionId ? 'inferred' : 'unavailable',
      mismatch,
      proofKind: descriptorConnectionId ? 'legacy-inferred-descriptor' : 'unqualified-descriptor',
      route: routeOwner(descriptorConnectionId ?? requested.connectionId, descriptorProfile),
      routeReadiness: descriptorConnectionId ? 'inferred' : 'unqualified'
    }
  }

  // A local profile-keyed route is physically exact without a registry row:
  // Electron performs the mutation on this machine and the gateway generation
  // proves which profile backend owns it. A legacy REMOTE descriptor is
  // different: `hermes:connection` reconstructs connectionId with
  // resolvedConnectionId, so it is explicitly inferred (#90048) and cannot
  // authorize a connection-scoped REST mutation.
  if (connection.mode === 'local') {
    return {
      epistemic: 'authoritative',
      mismatch: profileMismatch,
      proofKind: 'local-physical-route',
      route: routeOwner(null, descriptorProfile),
      routeReadiness: 'exact'
    }
  }

  if (connection.registryScoped === true && descriptorConnectionId) {
    return {
      epistemic: 'authoritative',
      mismatch: profileMismatch,
      proofKind: 'registry-scoped-descriptor',
      route: routeOwner(descriptorConnectionId, descriptorProfile),
      routeReadiness: 'exact'
    }
  }

  if (descriptorConnectionId) {
    return {
      epistemic: 'inferred',
      mismatch: profileMismatch,
      proofKind: 'legacy-inferred-descriptor',
      route: routeOwner(descriptorConnectionId, descriptorProfile),
      routeReadiness: 'inferred'
    }
  }

  return {
    epistemic: 'unavailable',
    mismatch: profileMismatch,
    proofKind: 'unqualified-descriptor',
    route: routeOwner(null, descriptorProfile),
    routeReadiness: 'unqualified'
  }
}

function routeLanded(
  kind: 'agent' | 'profile',
  requested: RouteOwnerRef,
  observedRoute: RouteOwnerRef,
  connection: HermesConnection | null
): boolean {
  if (kind === 'agent') {
    return sameRoute(observedRoute, requested)
  }

  if (observedRoute.connectionId !== null) {
    return false
  }

  if (observedRoute.profile === requested.profile) {
    return true
  }

  // Shared-primary routing deliberately keeps the physical gateway on its
  // launch profile while the descriptor scopes requests to the selected
  // logical profile. The activation generation binds that logical scope to the
  // physical gateway realization; without the descriptor this cannot be
  // reconstructed and the receipt fails closed.
  return Boolean(connection?.sharedPrimary || connection?.sharedRemote) && isActivePrimary()
}

function finishActivation({
  activationGeneration,
  connection,
  kind,
  landed,
  requested
}: {
  activationGeneration: number
  connection: HermesConnection | null
  kind: 'agent' | 'profile'
  landed: boolean
  requested: RouteOwnerRef
}): RouteActivationReceipt {
  const observedGeneration = gatewayActivationEpoch()
  const observedRoute = currentPhysicalRoute()
  const gateway = $gateway.get()

  if (observedGeneration !== activationGeneration) {
    return publishReceipt({
      activationGeneration,
      gateway,
      observedGeneration,
      observedRoute,
      reason: 'newer-activation',
      requested,
      status: 'superseded'
    })
  }

  if (!landed) {
    return publishReceipt({
      activationGeneration,
      gateway,
      observedGeneration,
      observedRoute,
      reason: 'target-unavailable',
      requested,
      status: 'failed'
    })
  }

  if (!routeLanded(kind, requested, observedRoute, connection)) {
    return publishReceipt({
      activationGeneration,
      gateway,
      observedGeneration,
      observedRoute,
      reason: 'route-not-activated',
      requested,
      status: 'failed'
    })
  }

  const descriptorProof = descriptorRouteProof(kind, requested, connection)

  if (descriptorProof.mismatch) {
    return publishReceipt({
      activationGeneration,
      gateway,
      observedGeneration,
      observedRoute,
      reason: 'descriptor-owner-mismatch',
      requested,
      status: 'failed'
    })
  }

  const gatewayReadiness = currentGatewayReadiness(gateway)
  const base = {
    activationGeneration,
    connection,
    epistemic: descriptorProof.epistemic,
    gateway,
    physicalRoute: observedRoute,
    proof: {
      authority: 'gateway-activation' as const,
      kind: descriptorProof.proofKind
    },
    requested,
    route: descriptorProof.route
  }

  if (
    connection &&
    descriptorProof.epistemic === 'authoritative' &&
    descriptorProof.routeReadiness === 'exact' &&
    gatewayReadiness === 'open'
  ) {
    return publishReceipt({
      ...base,
      connection,
      epistemic: 'authoritative',
      readiness: {
        descriptor: 'resolved',
        gateway: 'open',
        route: 'exact'
      },
      status: 'ready'
    })
  }

  return publishReceipt({
    ...base,
    readiness: {
      descriptor: connection ? 'resolved' : 'unavailable',
      gateway: gatewayReadiness,
      route: descriptorProof.routeReadiness
    },
    status: 'degraded'
  })
}

function rejectGeneration(
  activationGeneration: number,
  requested: RouteOwnerRef,
  reason: RejectedRouteActivationReceipt['reason']
): RouteActivationReceipt {
  const observedGeneration = gatewayActivationEpoch()
  const observedRoute = currentPhysicalRoute()
  const gateway = $gateway.get()

  return publishReceipt({
    activationGeneration,
    gateway,
    observedGeneration,
    observedRoute,
    reason,
    requested,
    status: observedGeneration === activationGeneration ? 'failed' : 'superseded'
  })
}

/**
 * Activate one local/legacy profile while retaining the descriptor result and
 * the gateway generation established by the same operation. Both reads start
 * concurrently, preserving the fail-open/atomic publication shape from #89797.
 */
export async function activateGatewayProfileWithProof(
  profile: string,
  connectionResolver: ConnectionResolver
): Promise<RouteActivationReceipt> {
  const requested = routeOwner(null, profile)
  const previousGeneration = gatewayActivationEpoch()
  const connectionPromise = startConnectionResolution(connectionResolver)
  const gatewayPromise = ensureGatewayForProfile(requested.profile)
  // The async function increments its epoch synchronously before its first
  // await. If it returns before doing so (bridge/registry unavailable), this
  // operation never acquired an activation generation and cannot claim the
  // pre-existing route as its own.
  const activationGeneration = gatewayActivationEpoch()
  const activationStarted = activationGeneration > previousGeneration
  const [connection] = await Promise.all([connectionPromise, gatewayPromise])

  return finishActivation({
    activationGeneration,
    connection,
    kind: 'profile',
    landed: activationStarted,
    requested
  })
}

/**
 * Activate one exact registry source/profile route and bind its receipt to the
 * source generation that remained stable across descriptor resolution. The
 * stable connection id alone cannot authorize a replacement route edited in
 * place under the same id.
 */
export async function activateGatewayAgentWithProof(
  connectionId: string,
  profile: string,
  connectionResolver: ConnectionResolver
): Promise<RouteActivationReceipt> {
  const requested = routeOwner(connectionId, profile)

  // Generation N must be observed before descriptor or gateway resolution
  // starts. Accepting an already-started Promise remains a compatibility path
  // for focused tests, but production callers pass a factory so an edit cannot
  // land between descriptor start and the first generation observation.
  const generationBefore = await registryRouteGeneration(requested.connectionId || '')
  const previousGeneration = gatewayActivationEpoch()
  const connectionPromise = startConnectionResolution(connectionResolver)
  const gatewayPromise = ensureGatewayForAgent(requested.connectionId, requested.profile)
  const activationGeneration = gatewayActivationEpoch()
  const activationStarted = activationGeneration > previousGeneration
  const [connection, landed] = await Promise.all([connectionPromise, gatewayPromise])

  if (!activationStarted || !landed) {
    return finishActivation({
      activationGeneration,
      connection,
      kind: 'agent',
      landed: false,
      requested
    })
  }

  const generationAfter = await registryRouteGeneration(requested.connectionId || '')

  if (!generationBefore || !generationAfter) {
    return rejectGeneration(activationGeneration, requested, 'route-generation-unavailable')
  }

  if (generationBefore !== generationAfter) {
    return rejectGeneration(activationGeneration, requested, 'route-generation-changed')
  }

  return finishActivation({
    activationGeneration,
    connection: bindRouteGeneration(connection, generationBefore),
    kind: 'agent',
    landed: true,
    requested
  })
}

/**
 * Exact no-op gate for route activation. Display-route equality is not enough:
 * degraded or rejected receipts must reacquire proof, even when the requested
 * route is still physically selected. Only a current ready receipt can suppress
 * the activation operation that mints mutation authority.
 */
export function activeGatewayRouteMatches(requested: RouteOwnerRef): boolean {
  const expected = routeOwner(requested.connectionId, requested.profile)
  const receipt = $routeActivationReceipt.get()

  return Boolean(
    receipt &&
      receipt.status === 'ready' &&
      routeActivationReceiptIsCurrent(receipt) &&
      sameRoute(receipt.requested, expected) &&
      receipt.gateway?.connectionState === 'open'
  )
}

/**
 * Revalidate immediately before publication. JavaScript cannot interleave an
 * activation between this synchronous check and the following Nanostores
 * batch, so a stale receipt deterministically becomes a no-op.
 */
export function routeActivationReceiptIsCurrent(
  receipt: RouteActivationReceipt
): receipt is DegradedRouteActivationReceipt | ReadyRouteActivationReceipt {
  if (receipt.status !== 'ready' && receipt.status !== 'degraded') {
    return false
  }

  return (
    receipt.activationGeneration === gatewayActivationEpoch() &&
    receipt.gateway === $gateway.get() &&
    sameRoute(receipt.physicalRoute, currentPhysicalRoute())
  )
}

function connectionRetainsReadyRouteProof(
  receipt: ReadyRouteActivationReceipt,
  currentConnection: HermesConnection | null
): boolean {
  if (!currentConnection) {
    return false
  }

  const kind = receipt.requested.connectionId ? 'agent' : 'profile'
  const currentProof = descriptorRouteProof(kind, receipt.requested, currentConnection, false)
  const receiptGeneration = routeGeneration(receipt.connection)
  const currentGeneration = routeGeneration(currentConnection)
  const generationCompatible =
    receipt.requested.connectionId === null ||
    currentGeneration === null ||
    (receiptGeneration !== null && currentGeneration === receiptGeneration)

  // Renderer presentation updates clone HermesConnection, and reconnect paths
  // can reissue an equivalent exact descriptor without copying the renderer's
  // non-secret generation field. Accept that exact-owner refresh here while
  // preserving an explicit generation mismatch as an immediate rejection.
  // The receipt's generation is still carried to Electron, whose main-process
  // registry map validates it immediately before backend resolution; a source
  // replaced under the same stable id therefore fails before the side effect.
  return (
    generationCompatible &&
    !currentProof.mismatch &&
    currentProof.epistemic === 'authoritative' &&
    currentProof.proofKind === receipt.proof.kind &&
    currentProof.routeReadiness === 'exact' &&
    sameRoute(currentProof.route, receipt.route)
  )
}

/** Consequential side effects require exact, current, fully ready route proof. */
export function routeActivationReceiptAllowsMutation(
  receipt: RouteActivationReceipt,
  currentConnection: HermesConnection | null
): receipt is ReadyRouteActivationReceipt {
  return (
    receipt.status === 'ready' &&
    routeActivationReceiptIsCurrent(receipt) &&
    connectionRetainsReadyRouteProof(receipt, currentConnection) &&
    receipt.gateway?.connectionState === 'open'
  )
}

/**
 * Resolve the current observable receipt into mutation authority without
 * reconstructing owner/generation from ambient profile or endpoint state.
 */
export function currentRouteActivationReceiptForMutation(
  currentConnection: HermesConnection | null
): ReadyRouteActivationReceipt | null {
  const receipt = $routeActivationReceipt.get()

  return receipt && routeActivationReceiptAllowsMutation(receipt, currentConnection) ? receipt : null
}

import assert from 'node:assert/strict'

import { beforeEach, test } from 'vitest'

import { apiRequestRegistryConnectionId } from './connection-config'
import {
  apiRequestRequiresRouteGeneration,
  currentRegistryRouteGeneration,
  forgetRegistryRouteGeneration,
  rememberRegistryRouteGeneration,
  replaceRegistryRouteGenerations
} from './connection-route-generation'

beforeEach(() => {
  replaceRegistryRouteGenerations([{ generation: 'generation-1', id: 'homelab' }])
})

test('main accepts an exact current generation for every protected FS/Git mutation family', () => {
  const paths = [
    '/api/fs/write-text',
    '/api/git/branch/switch',
    '/api/git/review/commit',
    '/api/git/review/create-pr',
    '/api/git/review/push',
    '/api/git/review/revert',
    '/api/git/review/stage',
    '/api/git/review/unstage',
    '/api/git/worktree/add',
    '/api/git/worktree/remove'
  ]

  for (const path of paths) {
    const request = {
      connectionId: 'homelab',
      method: 'POST',
      path: `${path}?profile=research`,
      routeGeneration: 'generation-1'
    }

    assert.equal(apiRequestRequiresRouteGeneration(request), true)
    assert.equal(apiRequestRegistryConnectionId(request), 'homelab')
  }
})

test('main rejects a stale receipt after dial material rotates under the same id', () => {
  rememberRegistryRouteGeneration('homelab', 'generation-2')

  assert.throws(
    () =>
      apiRequestRegistryConnectionId({
        connectionId: 'homelab',
        method: 'POST',
        path: '/api/git/review/push',
        routeGeneration: 'generation-1'
      }),
    /stale or has been replaced/
  )
  assert.equal(currentRegistryRouteGeneration('homelab'), 'generation-2')
})

test('main rejects missing generation and missing exact owner on protected routes', () => {
  assert.throws(
    () =>
      apiRequestRegistryConnectionId({
        connectionId: 'homelab',
        method: 'POST',
        path: '/api/fs/write-text'
      }),
    /stale or has been replaced/
  )

  assert.throws(
    () =>
      apiRequestRegistryConnectionId({
        method: 'POST',
        path: '/api/git/review/commit',
        routeGeneration: 'generation-1'
      }),
    /requires an exact registered connection route/
  )
})

test('main rejects a receipt after source removal', () => {
  forgetRegistryRouteGeneration('homelab')

  assert.throws(
    () =>
      apiRequestRegistryConnectionId({
        connectionId: 'homelab',
        method: 'POST',
        path: '/api/git/worktree/remove',
        routeGeneration: 'generation-1'
      }),
    /stale or has been replaced/
  )
  assert.equal(currentRegistryRouteGeneration('homelab'), null)
})

test('read-only and non-authority POST routes preserve their existing routing contract', () => {
  const read = { connectionId: 'homelab', path: '/api/git/status?path=%2Frepo' }
  const queryPost = {
    body: { branches: [] },
    connectionId: 'homelab',
    method: 'POST',
    path: '/api/git/review/pr-list'
  }

  assert.equal(apiRequestRequiresRouteGeneration(read), false)
  assert.equal(apiRequestRequiresRouteGeneration(queryPost), false)
  assert.equal(apiRequestRegistryConnectionId(read), 'homelab')
  assert.equal(apiRequestRegistryConnectionId(queryPost), 'homelab')
})

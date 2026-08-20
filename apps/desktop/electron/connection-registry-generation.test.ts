import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  normalizeConnectionInput,
  normalizeRegistry,
  removeConnection,
  upsertConnection
} from './connection-registry'
import { currentRegistryRouteGeneration } from './connection-route-generation'

test('new registered sources receive an opaque generation visible to the main authority map', () => {
  const registry = normalizeRegistry(null)
  const entry = normalizeConnectionInput(
    { kind: 'remote', label: 'Homelab', url: 'https://homelab.test', authMode: 'oauth' },
    registry
  )

  assert.ok(entry.generation)
  assert.equal(currentRegistryRouteGeneration(entry.id), entry.generation)
})

test('cosmetic label edits preserve generation while dial-material edits rotate it', () => {
  let registry = normalizeRegistry(null)
  const original = normalizeConnectionInput(
    {
      kind: 'remote',
      label: 'Homelab',
      url: 'https://homelab.test',
      authMode: 'token',
      token: { encoding: 'safeStorage', value: 'ciphertext-1' }
    },
    registry
  )
  registry = upsertConnection(registry, original)

  const renamed = normalizeConnectionInput(
    {
      id: original.id,
      kind: 'remote',
      label: 'Home server',
      url: original.url,
      authMode: original.authMode,
      token: original.token
    },
    registry
  )

  assert.equal(renamed.generation, original.generation)

  const moved = normalizeConnectionInput(
    {
      id: original.id,
      kind: 'remote',
      label: renamed.label,
      url: 'https://replacement.test',
      authMode: original.authMode,
      token: original.token
    },
    upsertConnection(registry, renamed)
  )

  assert.ok(moved.generation)
  assert.notEqual(moved.generation, original.generation)
  assert.equal(currentRegistryRouteGeneration(original.id), moved.generation)
})

test('credential and header rotation also rotate source generation', () => {
  let registry = normalizeRegistry(null)
  const original = normalizeConnectionInput(
    {
      kind: 'remote',
      label: 'Access gateway',
      url: 'https://gateway.test',
      authMode: 'token',
      token: { encoding: 'safeStorage', value: 'ciphertext-1' },
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-1' } }
    },
    registry
  )
  registry = upsertConnection(registry, original)

  const tokenRotated = normalizeConnectionInput(
    {
      id: original.id,
      kind: 'remote',
      label: original.label,
      url: original.url,
      authMode: original.authMode,
      token: { encoding: 'safeStorage', value: 'ciphertext-2' },
      headers: original.headers
    },
    registry
  )

  assert.notEqual(tokenRotated.generation, original.generation)

  const headerRotated = normalizeConnectionInput(
    {
      id: original.id,
      kind: 'remote',
      label: original.label,
      url: original.url,
      authMode: original.authMode,
      token: tokenRotated.token,
      headers: { 'CF-Access-Client-Id': { encoding: 'safeStorage', value: 'header-2' } }
    },
    upsertConnection(registry, tokenRotated)
  )

  assert.notEqual(headerRotated.generation, tokenRotated.generation)
})

test('normalization preserves persisted generations and mints missing legacy generations', () => {
  const persisted = normalizeRegistry({
    version: 2,
    primary: 'homelab',
    connections: [
      { id: 'local', kind: 'local', label: 'This device' },
      {
        authMode: 'oauth',
        generation: 'persisted-generation',
        id: 'homelab',
        kind: 'remote',
        label: 'Homelab',
        url: 'https://homelab.test'
      },
      {
        authMode: 'oauth',
        id: 'legacy',
        kind: 'remote',
        label: 'Legacy',
        url: 'https://legacy.test'
      }
    ]
  })
  const homelab = persisted.connections.find(connection => connection.id === 'homelab')
  const legacy = persisted.connections.find(connection => connection.id === 'legacy')

  assert.equal(homelab?.generation, 'persisted-generation')
  assert.ok(legacy?.generation)
  assert.equal(currentRegistryRouteGeneration('homelab'), 'persisted-generation')
  assert.equal(currentRegistryRouteGeneration('legacy'), legacy?.generation)
})

test('removal revokes the generation before the stable id can be reused', () => {
  let registry = normalizeRegistry(null)
  const entry = normalizeConnectionInput(
    { kind: 'ssh', label: 'Build box', host: 'build.test', user: 'hermes' },
    registry
  )
  registry = upsertConnection(registry, entry)

  assert.equal(currentRegistryRouteGeneration(entry.id), entry.generation)

  const removed = removeConnection(registry, entry.id)

  assert.equal(removed.connections.some(connection => connection.id === entry.id), false)
  assert.equal(currentRegistryRouteGeneration(entry.id), null)
})
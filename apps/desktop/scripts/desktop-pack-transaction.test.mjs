import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { preserveRollbackBackup } from './before-pack.mjs'
import {
  BUILDER_REEXEC_GUARD_ENV,
  MIN_BUILDER_NODE_VERSION,
  desktopBuilderRuntimeProblem,
  selectNpmNodeRuntime
} from './desktop-builder-runtime.mjs'
import {
  PACK_SESSION_ENV,
  isWindowsPeExecutable,
  readRollbackSession,
  settleDesktopPack
} from './desktop-pack-transaction.mjs'

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-pack-transaction-'))
}

function writePe(filePath, marker = 0x42) {
  const payload = Buffer.alloc(256, marker)
  payload[0] = 0x4d
  payload[1] = 0x5a
  payload.writeUInt32LE(0x80, 0x3c)
  payload[0x80] = 0x50
  payload[0x81] = 0x45
  payload[0x82] = 0x00
  payload[0x83] = 0x00
  fs.mkdirSync(path.dirname(filePath), { recursive: true })
  fs.writeFileSync(filePath, payload)
}

test('PE verification rejects a filename or MZ prefix without a PE signature', () => {
  const root = tempRoot()
  try {
    const valid = path.join(root, 'valid.exe')
    const truncated = path.join(root, 'truncated.exe')
    writePe(valid)
    fs.writeFileSync(truncated, 'MZ-not-a-complete-pe', 'utf8')

    assert.equal(isWindowsPeExecutable(valid), true)
    assert.equal(isWindowsPeExecutable(truncated), false)
    assert.equal(isWindowsPeExecutable(path.join(root, 'missing.exe')), false)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('failed builder restores the last valid packaged app over partial output', () => {
  const root = tempRoot()
  try {
    const releaseDir = path.join(root, 'release')
    const appOutDir = path.join(releaseDir, 'win-unpacked')
    const backupDir = `${appOutDir}.bak`
    writePe(path.join(backupDir, 'Hermes.exe'), 0x11)
    fs.writeFileSync(`${backupDir}.session`, 'session-a\n', 'utf8')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'partial.txt'), 'partial', 'utf8')

    const result = settleDesktopPack({
      releaseDir,
      builderSucceeded: false,
      sessionId: 'session-a'
    })

    assert.equal(result.ok, true)
    assert.deepEqual(result.restored, [appOutDir])
    assert.equal(fs.existsSync(backupDir), false)
    assert.equal(fs.existsSync(`${backupDir}.session`), false)
    assert.equal(isWindowsPeExecutable(path.join(appOutDir, 'Hermes.exe')), true)
    assert.equal(fs.existsSync(path.join(appOutDir, 'partial.txt')), false)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('successful builder discards rollback only after validating replacement PE', () => {
  const root = tempRoot()
  try {
    const releaseDir = path.join(root, 'release')
    const appOutDir = path.join(releaseDir, 'win-unpacked')
    const backupDir = `${appOutDir}.bak`
    writePe(path.join(backupDir, 'Hermes.exe'), 0x11)
    writePe(path.join(appOutDir, 'Hermes.exe'), 0x22)
    fs.writeFileSync(`${backupDir}.session`, 'session-a\n', 'utf8')

    const result = settleDesktopPack({
      releaseDir,
      builderSucceeded: true,
      sessionId: 'session-a'
    })

    assert.equal(result.ok, true)
    assert.deepEqual(result.discarded, [backupDir])
    assert.equal(fs.existsSync(backupDir), false)
    assert.equal(fs.existsSync(`${backupDir}.session`), false)
    assert.equal(isWindowsPeExecutable(path.join(appOutDir, 'Hermes.exe')), true)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('false builder success restores previous app and becomes a failure', () => {
  const root = tempRoot()
  try {
    const releaseDir = path.join(root, 'release')
    const appOutDir = path.join(releaseDir, 'win-unpacked')
    const backupDir = `${appOutDir}.bak`
    writePe(path.join(backupDir, 'Hermes.exe'), 0x11)
    fs.writeFileSync(`${backupDir}.session`, 'session-a\n', 'utf8')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Hermes.exe'), 'MZ-truncated', 'utf8')

    const result = settleDesktopPack({
      releaseDir,
      builderSucceeded: true,
      sessionId: 'session-a'
    })

    assert.equal(result.ok, false)
    assert.deepEqual(result.restored, [appOutDir])
    assert.match(result.failures[0].reason, /exited successfully.*missing or invalid/)
    assert.equal(isWindowsPeExecutable(path.join(appOutDir, 'Hermes.exe')), true)
    assert.equal(fs.existsSync(backupDir), false)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('same electron-builder session cannot overwrite the original rollback generation', () => {
  const root = tempRoot()
  try {
    const appOutDir = path.join(root, 'release', 'win-unpacked')
    const backupDir = `${appOutDir}.bak`
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Hermes.exe'), 'original-generation', 'utf8')

    assert.equal(preserveRollbackBackup(appOutDir, 'Hermes.exe', 'session-a'), true)
    assert.equal(readRollbackSession(backupDir), 'session-a')

    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Hermes.exe'), 'first-target-output', 'utf8')
    assert.equal(preserveRollbackBackup(appOutDir, 'Hermes.exe', 'session-a'), true)

    assert.equal(fs.existsSync(appOutDir), false)
    assert.equal(
      fs.readFileSync(path.join(backupDir, 'Hermes.exe'), 'utf8'),
      'original-generation'
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('a new builder session replaces stale rollback material with the current good app', () => {
  const root = tempRoot()
  try {
    const appOutDir = path.join(root, 'release', 'win-unpacked')
    const backupDir = `${appOutDir}.bak`
    fs.mkdirSync(backupDir, { recursive: true })
    fs.writeFileSync(path.join(backupDir, 'Hermes.exe'), 'older-generation', 'utf8')
    fs.writeFileSync(`${backupDir}.session`, 'stale-session\n', 'utf8')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Hermes.exe'), 'current-generation', 'utf8')

    assert.equal(preserveRollbackBackup(appOutDir, 'Hermes.exe', 'new-session'), true)
    assert.equal(readRollbackSession(backupDir), 'new-session')
    assert.equal(
      fs.readFileSync(path.join(backupDir, 'Hermes.exe'), 'utf8'),
      'current-generation'
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('pack settlement ignores rollback material owned by another generation', () => {
  const root = tempRoot()
  try {
    const releaseDir = path.join(root, 'release')
    const appOutDir = path.join(releaseDir, 'win-unpacked')
    const backupDir = `${appOutDir}.bak`
    writePe(path.join(backupDir, 'Hermes.exe'), 0x11)
    fs.writeFileSync(`${backupDir}.session`, 'other-session\n', 'utf8')

    const result = settleDesktopPack({
      releaseDir,
      builderSucceeded: false,
      sessionId: 'current-session'
    })

    assert.equal(result.ok, true)
    assert.deepEqual(result.restored, [])
    assert.equal(fs.existsSync(backupDir), true)
    assert.equal(fs.existsSync(appOutDir), false)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('retry adopts a valid interrupted rollback into its current generation', () => {
  const root = tempRoot()
  try {
    const appOutDir = path.join(root, 'release', 'win-unpacked')
    const backupDir = `${appOutDir}.bak`
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'partial.txt'), 'partial', 'utf8')
    fs.mkdirSync(backupDir, { recursive: true })
    fs.writeFileSync(path.join(backupDir, 'Hermes.exe'), 'last-good', 'utf8')
    fs.writeFileSync(`${backupDir}.session`, 'interrupted-session\n', 'utf8')

    assert.equal(preserveRollbackBackup(appOutDir, 'Hermes.exe', 'retry-session'), false)
    assert.equal(readRollbackSession(backupDir), 'retry-session')
    assert.equal(fs.existsSync(appOutDir), true)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('Windows package scripts re-exec with the Node runtime that launched npm', () => {
  const selected = selectNpmNodeRuntime({
    currentExecPath: 'C:\\fnm\\node.exe',
    npmNodeExecPath: 'C:\\Hermes\\node\\node.exe',
    guardValue: undefined,
    platform: 'win32',
    exists: () => true,
    realpath: value => value
  })
  assert.equal(selected, 'C:\\Hermes\\node\\node.exe')
})

test('runtime hand-off is bounded and ignores the same executable identity', () => {
  const common = {
    currentExecPath: 'C:\\Hermes\\NODE\\node.exe',
    npmNodeExecPath: 'c:\\hermes\\node\\node.exe',
    platform: 'win32',
    exists: () => true,
    realpath: value => value
  }
  assert.equal(selectNpmNodeRuntime({ ...common, guardValue: undefined }), undefined)
  assert.equal(
    selectNpmNodeRuntime({
      ...common,
      npmNodeExecPath: 'C:\\other\\node.exe',
      guardValue: '1'
    }),
    undefined
  )
})

test('runtime hand-off never selects missing or non-Windows npm executables', () => {
  const input = {
    currentExecPath: '/usr/bin/node',
    npmNodeExecPath: '/opt/hermes/node',
    guardValue: undefined,
    exists: () => true,
    realpath: value => value
  }
  assert.equal(selectNpmNodeRuntime({ ...input, platform: 'linux' }), undefined)
  assert.equal(
    selectNpmNodeRuntime({ ...input, platform: 'win32', exists: () => false }),
    undefined
  )
})

test('builder runtime gate rejects stale Node and disabled require(esm)', () => {
  assert.equal(MIN_BUILDER_NODE_VERSION, '22.22.0')
  assert.match(
    desktopBuilderRuntimeProblem({
      version: '20.16.0',
      execPath: 'C:\\fnm\\node.exe',
      requireModuleSupported: false
    }),
    /too old/
  )
  assert.match(
    desktopBuilderRuntimeProblem({
      version: '22.22.0',
      execPath: 'C:\\Hermes\\node.exe',
      requireModuleSupported: false
    }),
    /cannot require ESM/
  )
  assert.equal(
    desktopBuilderRuntimeProblem({
      version: '22.22.0',
      execPath: 'C:\\Hermes\\node.exe',
      requireModuleSupported: true
    }),
    undefined
  )
})

test('production wrapper owns one explicit pack generation', () => {
  const source = fs.readFileSync(new URL('./run-electron-builder.mjs', import.meta.url), 'utf8')
  assert.equal(PACK_SESSION_ENV, 'HERMES_DESKTOP_PACK_SESSION')
  assert.equal(BUILDER_REEXEC_GUARD_ENV, 'HERMES_ELECTRON_BUILDER_REEXEC')
  assert.match(source, /selectedNpmNode/)
  assert.match(source, /npm_node_execpath/)
  assert.ok(source.indexOf('selectedNpmNode') < source.indexOf('const dist = electronDistDir()'))
  assert.match(source, /PACK_SESSION_ENV/)
  assert.match(source, /settleDesktopPack/)
  assert.match(source, /env: \{ \.\.\.process\.env/)
})

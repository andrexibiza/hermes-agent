import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'

import {
  GET_WINDOWS_MISSING_ROOT_MARKER,
  GET_WINDOWS_RECOVERY_INTEGRITY,
  GET_WINDOWS_RECOVERY_RESOLVED,
  GET_WINDOWS_RECOVERY_TAR_VERSION,
  GET_WINDOWS_RECOVERY_VERSION,
  canRecoverGetWindowsPackage,
  recoverGetWindowsPackage,
  stageGetWindowsWithRecovery,
  verifyRecoveryLock
} from './stage-native-deps-recovery.mjs'

function tempParent() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-get-windows-recovery-test-'))
}

function writeRecoveryLock(root, { integrity = GET_WINDOWS_RECOVERY_INTEGRITY } = {}) {
  fs.writeFileSync(
    path.join(root, 'package-lock.json'),
    JSON.stringify({
      name: 'hermes-get-windows-recovery',
      version: '0.0.0',
      lockfileVersion: 3,
      packages: {
        '': {
          dependencies: { 'get-windows': GET_WINDOWS_RECOVERY_VERSION },
          overrides: { tar: GET_WINDOWS_RECOVERY_TAR_VERSION }
        },
        'node_modules/get-windows': {
          version: GET_WINDOWS_RECOVERY_VERSION,
          resolved: GET_WINDOWS_RECOVERY_RESOLVED,
          integrity
        },
        'node_modules/tar': {
          version: GET_WINDOWS_RECOVERY_TAR_VERSION
        }
      }
    })
  )
}

test('isolated recovery attests a lock before running the exact package lifecycle', () => {
  const parent = tempParent()
  try {
    const invocations = []

    const recovery = recoverGetWindowsPackage({
      arch: 'x64',
      npmExecPath: '/fake/npm-cli.js',
      platform: 'win32',
      tempParent: parent,
      run(command, args, options) {
        invocations.push({ args, command, options })
        if (args[1] === 'install') {
          writeRecoveryLock(options.cwd)
          return { status: 0 }
        }
        assert.equal(args[1], 'ci')
        const packageRoot = path.join(options.cwd, 'node_modules', 'get-windows')
        fs.mkdirSync(path.join(packageRoot, 'lib'), { recursive: true })
        fs.writeFileSync(
          path.join(packageRoot, 'package.json'),
          JSON.stringify({ name: 'get-windows', version: GET_WINDOWS_RECOVERY_VERSION })
        )
        fs.writeFileSync(path.join(packageRoot, 'index.js'), 'export default {}\n')
        return { status: 0 }
      }
    })

    assert.equal(invocations.length, 2)
    assert.equal(invocations[0].command, process.execPath)
    assert.equal(invocations[0].args[0], '/fake/npm-cli.js')
    assert.equal(invocations[0].args[1], 'install')
    assert.ok(invocations[0].args.includes('--package-lock-only'))
    assert.ok(invocations[0].args.includes('--ignore-scripts'))
    assert.equal(invocations[1].args[1], 'ci')
    assert.ok(invocations[1].args.includes('--ignore-scripts=false'))
    assert.ok(invocations[1].args.includes('--workspaces=false'))
    assert.ok(invocations[1].args.includes('--include=optional'))
    assert.equal(invocations[1].options.cwd, recovery.recoveryRoot)
    assert.equal(invocations[1].options.env.npm_config_platform, 'win32')
    assert.equal(invocations[1].options.env.npm_config_arch, 'x64')

    const manifest = JSON.parse(
      fs.readFileSync(path.join(recovery.recoveryRoot, 'package.json'), 'utf8')
    )
    assert.deepEqual(manifest.dependencies, {
      'get-windows': GET_WINDOWS_RECOVERY_VERSION
    })
    assert.deepEqual(manifest.overrides, {
      tar: GET_WINDOWS_RECOVERY_TAR_VERSION
    })
    assert.deepEqual(manifest.allowScripts, {
      [`get-windows@${GET_WINDOWS_RECOVERY_VERSION}`]: true
    })
    assert.equal(verifyRecoveryLock(recovery.recoveryRoot), true)
    assert.equal(
      recovery.packageRoot,
      path.join(recovery.recoveryRoot, 'node_modules', 'get-windows')
    )
    assert.ok(fs.existsSync(recovery.recoveryRoot))

    assert.equal(recovery.cleanup(), true)
    assert.equal(recovery.cleanup(), true)
    assert.equal(fs.existsSync(recovery.recoveryRoot), false)
  } finally {
    fs.rmSync(parent, { force: true, recursive: true })
  }
})

test('digest or root override drift is rejected before lifecycle scripts run', () => {
  const parent = tempParent()
  try {
    let calls = 0
    assert.throws(
      () =>
        recoverGetWindowsPackage({
          npmExecPath: '/fake/npm-cli.js',
          run(_command, _args, options) {
            calls += 1
            writeRecoveryLock(options.cwd, { integrity: 'sha512-wrong' })
            return { status: 0 }
          },
          tempParent: parent
        }),
      /lock does not match the repository version, tarball, and integrity authority/
    )
    assert.equal(calls, 1)
    assert.deepEqual(fs.readdirSync(parent), [])
  } finally {
    fs.rmSync(parent, { force: true, recursive: true })
  }
})

test('failed lock resolution is removed and cannot poison a later update', () => {
  const parent = tempParent()
  try {
    assert.throws(
      () =>
        recoverGetWindowsPackage({
          npmExecPath: '/fake/npm-cli.js',
          run: () => ({ status: 1 }),
          tempParent: parent
        }),
      /isolated get-windows recovery lock resolution exited with 1/
    )
    assert.deepEqual(fs.readdirSync(parent), [])
  } finally {
    fs.rmSync(parent, { force: true, recursive: true })
  }
})

test('failed lifecycle install is removed after a verified lock', () => {
  const parent = tempParent()
  try {
    let calls = 0
    assert.throws(
      () =>
        recoverGetWindowsPackage({
          npmExecPath: '/fake/npm-cli.js',
          run(_command, _args, options) {
            calls += 1
            if (calls === 1) {
              writeRecoveryLock(options.cwd)
              return { status: 0 }
            }
            return { status: 2 }
          },
          tempParent: parent
        }),
      /isolated get-windows recovery install exited with 2/
    )
    assert.equal(calls, 2)
    assert.deepEqual(fs.readdirSync(parent), [])
  } finally {
    fs.rmSync(parent, { force: true, recursive: true })
  }
})

test('supported native Windows staging retries with the recovered package root', () => {
  const calls = []
  let cleaned = false

  const result = stageGetWindowsWithRecovery({
    arch: 'x64',
    hostArch: 'x64',
    hostPlatform: 'win32',
    platform: 'win32',
    recover: () => ({
      cleanup() {
        cleaned = true
      },
      packageRoot: 'C:\\Temp\\isolated\\node_modules\\get-windows'
    }),
    stage(options) {
      calls.push(options)
      if (calls.length === 1) {
        throw new Error(`${GET_WINDOWS_MISSING_ROOT_MARKER}win32-x64 native payload`)
      }
      assert.equal(
        options.resolveRoot(),
        'C:\\Temp\\isolated\\node_modules\\get-windows'
      )
      return 'staged'
    }
  })

  assert.equal(result, 'staged')
  assert.equal(calls.length, 2)
  assert.equal(cleaned, true)
})

test('recovered temporary dependency is removed when the second staging attempt fails', () => {
  let calls = 0
  let cleaned = false

  assert.throws(
    () =>
      stageGetWindowsWithRecovery({
        arch: 'x64',
        hostArch: 'x64',
        hostPlatform: 'win32',
        platform: 'win32',
        recover: () => ({
          cleanup() {
            cleaned = true
          },
          packageRoot: 'C:\\Temp\\isolated\\node_modules\\get-windows'
        }),
        stage() {
          calls += 1
          if (calls === 1) {
            throw new Error(`${GET_WINDOWS_MISSING_ROOT_MARKER}win32-x64 native payload`)
          }
          throw new Error('recovered package has no binding')
        }
      }),
    /recovered package has no binding/
  )
  assert.equal(cleaned, true)
})

test('unrelated staging failures are never converted into dependency recovery', () => {
  let recovered = false

  assert.throws(
    () =>
      stageGetWindowsWithRecovery({
        recover() {
          recovered = true
          throw new Error('should not run')
        },
        stage() {
          throw new Error('native binary platform mismatch')
        }
      }),
    /native binary platform mismatch/
  )
  assert.equal(recovered, false)
})

test('cross-platform and unsupported Windows architecture requests remain fail-closed', () => {
  assert.equal(
    canRecoverGetWindowsPackage({
      arch: 'x64',
      hostArch: 'x64',
      hostPlatform: 'linux',
      platform: 'win32'
    }),
    false
  )
  assert.equal(
    canRecoverGetWindowsPackage({
      arch: 'arm64',
      hostArch: 'arm64',
      hostPlatform: 'win32',
      platform: 'win32'
    }),
    false
  )
  assert.equal(
    canRecoverGetWindowsPackage({
      arch: 'x64',
      hostArch: 'x64',
      hostPlatform: 'win32',
      platform: 'win32'
    }),
    true
  )
})

test('Desktop build and electron-builder hook both consume the recovery owner', () => {
  const manifest = JSON.parse(
    fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8')
  )

  assert.match(manifest.scripts.build, /stage-native-deps-recovery\.mjs$/)
  assert.match(
    manifest.scripts['check:test:desktop:all'],
    /stage-native-deps-recovery\.test\.mjs/
  )
  assert.equal(manifest.build.beforePack, 'scripts/before-pack-recovery.mjs')
})

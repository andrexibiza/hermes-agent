import assert from 'node:assert/strict'
import fs from 'node:fs'
import { test } from 'vitest'

function source(relativePath) {
  return fs.readFileSync(new URL(relativePath, import.meta.url), 'utf8')
}

test('isolated native recovery remains inside the canonical package transaction', () => {
  const packageJson = JSON.parse(source('../package.json'))
  const recoveryHook = source('./before-pack-recovery.mjs')
  const canonicalHook = source('./before-pack.mjs')
  const builderWrapper = source('./run-electron-builder.mjs')

  assert.equal(packageJson.build.beforePack, 'scripts/before-pack-recovery.mjs')
  assert.match(packageJson.scripts.build, /stage-native-deps-recovery\.mjs/)

  // Recovery must wrap—not replace—the owner that preserves the prior
  // packaged generation and writes its rollback-session identity.
  assert.match(recoveryHook, /import beforePack from '\.\/before-pack\.mjs'/)
  assert.match(recoveryHook, /isMissingGetWindowsPackageError/)
  assert.match(recoveryHook, /stageGetWindowsWithRecovery/)
  assert.ok(
    recoveryHook.indexOf('beforePack(context)') <
      recoveryHook.indexOf('stageGetWindowsWithRecovery'),
    'canonical staging and rollback acquisition must run before bounded recovery'
  )

  assert.match(canonicalHook, /PACK_SESSION_ENV/)
  assert.match(canonicalHook, /preserveRollbackBackup/)
  assert.match(canonicalHook, /desktop-pack-transaction\.mjs/)

  // The terminal builder result—not the recovery helper—owns commit/restore.
  assert.match(builderWrapper, /settleDesktopPack/)
  assert.match(builderWrapper, /builderSucceeded/)
  assert.match(builderWrapper, /PACK_SESSION_ENV/)
  assert.ok(
    builderWrapper.indexOf('builderSucceeded') < builderWrapper.indexOf('settleDesktopPack'),
    'the actual builder result must be known before transaction settlement'
  )
})

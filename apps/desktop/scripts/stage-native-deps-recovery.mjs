#!/usr/bin/env node
// Isolated dependency-realization wrapper for stage-native-deps.mjs.
//
// The normal Desktop build stages get-windows from the repository's installed
// dependency tree. On Windows, an interrupted/in-place npm extraction can leave
// node_modules/get-windows present but unresolvable (for example, package.json
// is missing). Reusing that tree makes every later Desktop rebuild fail at the
// same stage. This wrapper preserves the fail-closed native staging contract,
// but realizes the exact package in a fresh temporary npm prefix and stages
// from that verified root instead of mutating or trusting active node_modules.

import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'

import { stageGetWindows, stageNodePty } from './stage-native-deps.mjs'
import { isMain } from './utils.mjs'

const require = createRequire(import.meta.url)

export const GET_WINDOWS_RECOVERY_VERSION = '9.3.0'
export const GET_WINDOWS_MISSING_ROOT_MARKER =
  '[stage-native-deps] get-windows is not installed; cannot stage its '

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

export function isMissingGetWindowsPackageError(error) {
  return errorMessage(error).includes(GET_WINDOWS_MISSING_ROOT_MARKER)
}

export function canRecoverGetWindowsPackage({
  platform,
  arch,
  hostPlatform = process.platform,
  hostArch = process.arch
}) {
  if (platform !== hostPlatform) {
    return false
  }

  if (platform === 'darwin') {
    // get-windows' macOS helper is universal.
    return true
  }

  if (platform === 'win32') {
    // The package's node-pre-gyp lifecycle realizes a host-architecture
    // binding. Only consume it when the requested package architecture is the
    // same one the active Node process can actually verify and execute.
    return arch === hostArch && (arch === 'x64' || arch === 'ia32')
  }

  return false
}

function cleanupRecoveryRoot(recoveryRoot) {
  try {
    rmSync(recoveryRoot, {
      force: true,
      maxRetries: 5,
      recursive: true,
      retryDelay: 100
    })
    return true
  } catch (error) {
    console.warn(
      `[stage-native-deps] could not remove isolated get-windows recovery root ${recoveryRoot}: ${errorMessage(error)}`
    )
    return false
  }
}

export function recoverGetWindowsPackage({
  platform = process.platform,
  arch = process.arch,
  npmExecPath = process.env.npm_execpath,
  run = spawnSync,
  tempParent = tmpdir()
} = {}) {
  if (!npmExecPath) {
    throw new Error(
      '[stage-native-deps] cannot recover get-windows: npm_execpath is unavailable; run the Desktop build through npm'
    )
  }

  const recoveryRoot = mkdtempSync(join(tempParent, 'hermes-get-windows-'))
  let completed = false

  try {
    const recoveryManifest = {
      name: 'hermes-get-windows-recovery',
      private: true,
      version: '0.0.0',
      dependencies: {
        'get-windows': GET_WINDOWS_RECOVERY_VERSION
      },
      allowScripts: {
        [`get-windows@${GET_WINDOWS_RECOVERY_VERSION}`]: true
      }
    }
    writeFileSync(
      join(recoveryRoot, 'package.json'),
      `${JSON.stringify(recoveryManifest, null, 2)}\n`,
      'utf8'
    )

    const result = run(
      process.execPath,
      [
        npmExecPath,
        'install',
        '--workspaces=false',
        '--include=optional',
        '--ignore-scripts=false',
        '--no-audit',
        '--no-fund',
        '--package-lock=false',
        '--prefer-online'
      ],
      {
        cwd: recoveryRoot,
        env: {
          ...process.env,
          npm_config_arch: arch,
          npm_config_platform: platform,
          npm_config_target_arch: arch
        },
        stdio: 'inherit'
      }
    )

    if (result.error) {
      throw new Error(
        `[stage-native-deps] isolated get-windows recovery could not start npm: ${result.error.message}`
      )
    }
    if (result.status !== 0) {
      throw new Error(
        `[stage-native-deps] isolated get-windows recovery install exited with ${result.status}`
      )
    }

    let packageRoot
    try {
      // get-windows does not export package.json; resolve its root entry and
      // validate the manifest beside it.
      packageRoot = dirname(
        require.resolve('get-windows', {
          paths: [recoveryRoot]
        })
      )
    } catch (error) {
      throw new Error(
        `[stage-native-deps] isolated get-windows recovery completed without an importable package: ${errorMessage(error)}`
      )
    }

    const installedVersion = JSON.parse(
      readFileSync(join(packageRoot, 'package.json'), 'utf8')
    ).version
    if (installedVersion !== GET_WINDOWS_RECOVERY_VERSION) {
      throw new Error(
        `[stage-native-deps] isolated get-windows recovery resolved ${installedVersion}; expected ${GET_WINDOWS_RECOVERY_VERSION}`
      )
    }

    completed = true
    let cleaned = false
    return {
      packageRoot,
      recoveryRoot,
      cleanup() {
        if (cleaned) {
          return true
        }
        cleaned = cleanupRecoveryRoot(recoveryRoot)
        return cleaned
      }
    }
  } finally {
    if (!completed) {
      cleanupRecoveryRoot(recoveryRoot)
    }
  }
}

export function stageGetWindowsWithRecovery({
  platform = process.platform,
  arch = process.arch,
  hostPlatform = process.platform,
  hostArch = process.arch,
  recover = recoverGetWindowsPackage,
  stage = stageGetWindows
} = {}) {
  try {
    return stage({ platform, arch })
  } catch (error) {
    if (
      !isMissingGetWindowsPackageError(error) ||
      !canRecoverGetWindowsPackage({ platform, arch, hostPlatform, hostArch })
    ) {
      throw error
    }

    console.warn(
      `[stage-native-deps] get-windows package root is missing or corrupt for ${platform}-${arch}; ` +
        `recovering exact ${GET_WINDOWS_RECOVERY_VERSION} in an isolated npm prefix`
    )
    const recovery = recover({ platform, arch })

    try {
      return stage({
        platform,
        arch,
        resolveRoot: () => recovery.packageRoot
      })
    } finally {
      recovery.cleanup()
    }
  }
}

export function stageNativeDeps({ platform = process.platform, arch = process.arch } = {}) {
  const nodePty = stageNodePty({ platform, arch })
  const getWindows = stageGetWindowsWithRecovery({ platform, arch })
  return { getWindows, nodePty }
}

if (isMain(import.meta.url)) {
  const [platform, arch] = process.argv.slice(2)
  stageNativeDeps({ platform, arch })
}

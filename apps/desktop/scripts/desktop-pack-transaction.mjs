import {
  closeSync,
  existsSync,
  openSync,
  readFileSync,
  readSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync
} from 'node:fs'
import path from 'node:path'

export const PACK_SESSION_ENV = 'HERMES_DESKTOP_PACK_SESSION'

export function rollbackSessionMarkerPath(backupDir) {
  return `${backupDir}.session`
}

export function readRollbackSession(backupDir) {
  try {
    return readFileSync(rollbackSessionMarkerPath(backupDir), 'utf8').trim() || undefined
  } catch {
    return undefined
  }
}

export function writeRollbackSession(backupDir, sessionId) {
  if (!sessionId || typeof sessionId !== 'string') {
    return false
  }
  writeFileSync(rollbackSessionMarkerPath(backupDir), `${sessionId}\n`, 'utf8')
  return true
}

export function clearRollbackSession(backupDir) {
  try {
    unlinkSync(rollbackSessionMarkerPath(backupDir))
    return true
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      return false
    }
    throw error
  }
}

export function isWindowsPeExecutable(filePath) {
  let fd
  try {
    if (!filePath || !existsSync(filePath) || !statSync(filePath).isFile()) {
      return false
    }
    fd = openSync(filePath, 'r')
    const dosHeader = Buffer.alloc(64)
    if (readSync(fd, dosHeader, 0, dosHeader.length, 0) !== dosHeader.length) {
      return false
    }
    if (dosHeader[0] !== 0x4d || dosHeader[1] !== 0x5a) {
      return false
    }
    const peOffset = dosHeader.readUInt32LE(0x3c)
    // Keep malformed or absurd offsets from turning a verification read into
    // unbounded filesystem work. Real PE headers are close to the DOS stub.
    if (peOffset < 64 || peOffset > 16 * 1024 * 1024) {
      return false
    }
    const signature = Buffer.alloc(4)
    if (readSync(fd, signature, 0, signature.length, peOffset) !== signature.length) {
      return false
    }
    return signature.equals(Buffer.from([0x50, 0x45, 0x00, 0x00]))
  } catch {
    return false
  } finally {
    if (fd !== undefined) {
      try {
        closeSync(fd)
      } catch {}
    }
  }
}

function rollbackDirectories(releaseDir) {
  if (!releaseDir || !existsSync(releaseDir)) {
    return []
  }
  try {
    return readdirSync(releaseDir, { withFileTypes: true })
      .filter(entry => entry.isDirectory() && entry.name.endsWith('.bak'))
      .map(entry => path.join(releaseDir, entry.name))
      .sort()
  } catch {
    return []
  }
}

function removeTree(target) {
  rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
}

function clearRollbackSessionBestEffort(backupDir) {
  try {
    clearRollbackSession(backupDir)
  } catch {}
}

function restoreBackup(backupDir, originalDir) {
  removeTree(originalDir)
  renameSync(backupDir, originalDir)
  clearRollbackSessionBestEffort(backupDir)
}

/**
 * Close the transaction opened by before-pack.mjs.
 *
 * A failed builder restores the last verified unpacked app. A successful
 * builder may delete rollback material only after the replacement contains a
 * structurally valid Windows PE executable. If electron-builder reports zero
 * but leaves a missing/truncated executable, the old build is restored and
 * the wrapper converts that false success into a failure.
 */
export function settleDesktopPack({
  releaseDir,
  builderSucceeded,
  productExeName = 'Hermes.exe',
  sessionId
}) {
  const restored = []
  const discarded = []
  const failures = []

  for (const backupDir of rollbackDirectories(releaseDir)) {
    if (sessionId && readRollbackSession(backupDir) !== sessionId) {
      continue
    }

    const originalDir = backupDir.slice(0, -'.bak'.length)
    const backupExe = path.join(backupDir, productExeName)
    const originalExe = path.join(originalDir, productExeName)
    const backupValid = isWindowsPeExecutable(backupExe)
    const replacementValid = isWindowsPeExecutable(originalExe)

    try {
      if (builderSucceeded && replacementValid) {
        removeTree(backupDir)
        clearRollbackSessionBestEffort(backupDir)
        discarded.push(backupDir)
        continue
      }

      if (!backupValid) {
        failures.push({
          backupDir,
          reason: builderSucceeded
            ? `replacement ${originalExe} is invalid and rollback ${backupExe} is not a valid PE`
            : `rollback ${backupExe} is not a valid PE`
        })
        continue
      }

      restoreBackup(backupDir, originalDir)
      restored.push(originalDir)
      if (builderSucceeded) {
        failures.push({
          backupDir,
          reason: `electron-builder exited successfully but replacement ${originalExe} was missing or invalid; restored previous build`
        })
      }
    } catch (error) {
      failures.push({
        backupDir,
        reason: `could not settle rollback transaction: ${error.message}`
      })
    }
  }

  return {
    ok: failures.length === 0,
    restored,
    discarded,
    failures
  }
}

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

/**
 * Preliminary PE structure check for the builder boundary.
 *
 * This deliberately does not claim Windows launchability. The canonical
 * `_ensure_desktop_exe_launchable` gate in hermes_cli/main.py owns host-machine
 * compatibility and final rollback retirement. This check only rejects output
 * that is already provably incomplete before control returns to that gate.
 */
export function isWindowsPeExecutable(filePath) {
  let fd
  try {
    if (!filePath || !existsSync(filePath)) {
      return false
    }
    const stat = statSync(filePath)
    if (!stat.isFile() || stat.size < 64) {
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
    // unbounded filesystem work. A complete COFF header must also fit.
    if (peOffset < 64 || peOffset > 16 * 1024 * 1024 || peOffset + 24 > stat.size) {
      return false
    }

    const coff = Buffer.alloc(24)
    if (readSync(fd, coff, 0, coff.length, peOffset) !== coff.length) {
      return false
    }
    if (!coff.subarray(0, 4).equals(Buffer.from([0x50, 0x45, 0x00, 0x00]))) {
      return false
    }

    const sectionCount = coff.readUInt16LE(6)
    const optionalHeaderSize = coff.readUInt16LE(20)
    if (sectionCount < 1 || sectionCount > 96) {
      return false
    }

    const sectionTableOffset = peOffset + 24 + optionalHeaderSize
    const sectionTableSize = sectionCount * 40
    if (sectionTableOffset + sectionTableSize > stat.size) {
      return false
    }

    const section = Buffer.alloc(40)
    for (let index = 0; index < sectionCount; index += 1) {
      const offset = sectionTableOffset + index * section.length
      if (readSync(fd, section, 0, section.length, offset) !== section.length) {
        return false
      }
      const rawSize = section.readUInt32LE(16)
      const rawOffset = section.readUInt32LE(20)
      if (rawSize > 0 && (rawOffset === 0 || rawOffset + rawSize > stat.size)) {
        return false
      }
    }

    return true
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
 * Close the builder-owned part of the transaction opened by before-pack.mjs.
 *
 * A failed builder restores the last packaged app. A successful builder may
 * reject and roll back output that is already structurally incomplete, but it
 * must retain the rollback generation for the canonical Python launchability
 * gate. Builder exit zero plus a plausible PE is not authority to delete the
 * last known-good app: host architecture and full launchability are decided
 * later by `_ensure_desktop_exe_launchable`.
 */
export function settleDesktopPack({
  releaseDir,
  builderSucceeded,
  productExeName = 'Hermes.exe',
  sessionId
}) {
  const restored = []
  const retained = []
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
        // Preserve both the rollback tree and its generation marker. The
        // Python launchability gate owns wrong-architecture detection, final
        // commit, and rollback retirement.
        retained.push(backupDir)
        continue
      }

      if (!backupValid) {
        failures.push({
          backupDir,
          reason: builderSucceeded
            ? `replacement ${originalExe} is invalid and rollback ${backupExe} is not a structurally complete PE`
            : `rollback ${backupExe} is not a structurally complete PE`
        })
        continue
      }

      restoreBackup(backupDir, originalDir)
      restored.push(originalDir)
      if (builderSucceeded) {
        failures.push({
          backupDir,
          reason: `electron-builder exited successfully but replacement ${originalExe} was missing or structurally incomplete; restored previous build`
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
    retained,
    discarded,
    failures
  }
}

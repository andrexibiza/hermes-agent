import type {
  HermesConnection,
  HermesReadDirResult,
  HermesReadFileTextResult,
  HermesSelectPathsOptions
} from '@/global'
import { hermesApi } from '@/hermes'
import { currentRouteActivationReceiptForMutation } from '@/store/gateway-activation'
import { $connection } from '@/store/session'

export interface DesktopFsRemotePicker {
  selectPaths: (options?: HermesSelectPathsOptions) => Promise<string[]>
}

type RouteGenerationBoundConnection = HermesConnection & {
  routeGeneration?: string
}

let remotePicker: DesktopFsRemotePicker | null = null

export function setDesktopFsRemotePicker(next: DesktopFsRemotePicker | null) {
  remotePicker = next
}

function connectionCacheKey(connection: HermesConnection | null) {
  if (!connection) {
    return 'local:'
  }

  const target =
    connection.remoteKind === 'ssh'
      ? connection.remoteIdentity || connection.remoteHost || ''
      : connection.baseUrl || ''

  const owner = connection.connectionId ? `${connection.connectionId}:` : ''

  return `${owner}${connection.mode || 'local'}:${connection.remoteKind || ''}:${connection.profile || ''}:${target}`
}

export function desktopFsCacheKey(connection: HermesConnection | null = $connection.get()) {
  return connectionCacheKey(connection)
}

export function isDesktopFsRemoteMode() {
  return $connection.get()?.mode === 'remote'
}

// Active logical profile for FS/git REST calls. hermesApi carries the active
// registry source separately; profile names are not globally unique.
export function desktopFsProfile(): string | undefined {
  return $connection.get()?.profile || undefined
}

function fsPath(endpoint: string, filePath: string) {
  return `/api/fs/${endpoint}?path=${encodeURIComponent(filePath)}`
}

function bridge() {
  const desktop = window.hermesDesktop

  if (!desktop) {
    throw new Error('Hermes Desktop bridge is unavailable')
  }

  return desktop
}

function remoteFsApi<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  // Route read-only requests through the same active connection context the
  // gateway registry maintains. The old direct bridge call carried only a
  // profile, so a registry agent silently fell back to the primary source
  // (#89916, the owner-loss form of #90866).
  return hermesApi<T>(
    body ? { body, method: 'POST', path, profile: desktopFsProfile() } : { path, profile: desktopFsProfile() }
  )
}

function remoteFsMutationApi<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const connection = $connection.get()
  const receipt = currentRouteActivationReceiptForMutation(connection)
  const connectionId = receipt?.route.connectionId
  const routeGeneration = String(
    (receipt?.connection as RouteGenerationBoundConnection | undefined)?.routeGeneration || ''
  ).trim()

  // A remote write must name an exact registry owner AND the main-owned source
  // generation that the activation proved. A stable connectionId is UI
  // identity only: Settings can edit it in place to target a different host.
  if (!receipt || !connectionId || !routeGeneration) {
    throw new Error('Remote file mutation requires a current exact generated route activation')
  }

  // Consume the receipt directly at the side-effect boundary. Main revalidates
  // routeGeneration before resolving/dialling the registry source, so a stale
  // renderer receipt cannot authorize a replacement source under the same id.
  const request = {
    body,
    connectionId,
    method: 'POST' as const,
    path,
    profile: receipt.route.profile,
    routeGeneration
  }

  return bridge().api<T>(request)
}

export async function readDesktopDir(path: string): Promise<HermesReadDirResult> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readDir(path)
  }

  return remoteFsApi<HermesReadDirResult>(fsPath('list', path))
}

export async function readDesktopFileText(path: string): Promise<HermesReadFileTextResult> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileText(path)
  }

  return remoteFsApi<HermesReadFileTextResult>(fsPath('read-text', path))
}

// Save UTF-8 text back to a file. Local writes go through the hardened Electron
// IPC; remote writes hit the dashboard's POST /api/fs/write-text only after
// the active route proves its exact registry owner, descriptor realization, and
// generation. Stale-on-disk detection is the caller's job (re-read before save).
export async function writeDesktopFileText(path: string, content: string): Promise<{ path: string }> {
  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    if (!desktop.writeTextFile) {
      throw new Error('Saving is not available')
    }

    return desktop.writeTextFile(path, content)
  }

  const result = await remoteFsMutationApi<{ ok?: boolean; path?: string }>('/api/fs/write-text', { content, path })

  return { path: result.path || path }
}

export async function readDesktopFileDataUrl(path: string): Promise<string> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileDataUrl(path)
  }

  const result = await remoteFsApi<string | { dataUrl?: string }>(fsPath('read-data-url', path))

  return typeof result === 'string' ? result : result.dataUrl || ''
}

/**
 * Read a composer image local-shell first, even when the active agent is
 * remote. Picker, clipboard, and OS-drop paths belong to this machine; in-app
 * project-tree paths may belong only to the gateway and fall back there.
 */
export async function readDesktopFileDataUrlLocalFirst(path: string): Promise<string> {
  try {
    const local = await window.hermesDesktop?.readFileDataUrl?.(path)

    if (local) {
      return local
    }
  } catch (error) {
    if (!isDesktopFsRemoteMode()) {
      throw error
    }

    // Not on this machine (or unreadable locally) — try the active gateway.
  }

  return readDesktopFileDataUrl(path)
}

export async function desktopGitRoot(path: string): Promise<string | null> {
  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    return desktop.gitRoot ? desktop.gitRoot(path) : null
  }

  return (await remoteFsApi<{ root: string | null }>(fsPath('git-root', path))).root
}

export async function desktopDefaultCwd(): Promise<{ branch: string; cwd: string } | null> {
  if (!isDesktopFsRemoteMode()) {
    return null
  }

  return remoteFsApi<{ branch: string; cwd: string }>('/api/fs/default-cwd')
}

// Reveal a path in the OS file manager (Finder / Explorer / Files). Local only.
export async function revealDesktopPath(path: string): Promise<void> {
  await bridge().revealPath?.(path)
}

// Rename a file/folder in place; returns the new absolute path. Local only.
export async function renameDesktopPath(path: string, newName: string): Promise<string> {
  const desktop = bridge()

  if (!desktop.renamePath) {
    throw new Error('Rename is not available')
  }

  const result = await desktop.renamePath(path, newName)

  return result.path
}

// Move a file/folder to the OS trash (recoverable). Local only.
export async function trashDesktopPath(path: string): Promise<void> {
  const desktop = bridge()

  if (!desktop.trashPath) {
    throw new Error('Delete is not available')
  }

  await desktop.trashPath(path)
}

export async function copyTextToClipboard(text: string): Promise<void> {
  await bridge().writeClipboard(text)
}

// Working-tree-vs-HEAD diff for one file. Empty when unchanged / not a repo.
// Remote gateway → backend git (/api/git/file-diff); local → Electron git.
export async function desktopFileDiff(repoRoot: string, filePath: string): Promise<string> {
  if (isDesktopFsRemoteMode()) {
    const result = await remoteFsApi<{ diff: string }>(
      `/api/git/file-diff?path=${encodeURIComponent(repoRoot)}&file=${encodeURIComponent(filePath)}`
    )

    return result.diff || ''
  }

  const git = bridge().git

  return git?.fileDiff ? git.fileDiff(repoRoot, filePath) : ''
}

export async function selectDesktopPaths(options?: HermesSelectPathsOptions): Promise<string[]> {
  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    return desktop.selectPaths(options)
  }

  if (!options?.directories) {
    return desktop.selectPaths(options)
  }

  return remotePicker ? remotePicker.selectPaths({ ...options, multiple: false }) : []
}

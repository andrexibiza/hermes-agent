#!/usr/bin/env bash
# Prove a user on some earlier commit can reach this one.
#
# Installs a real, earlier Hermes the way a user does, applies ONE update route,
# and requires the checkout to land on this commit with a working `hermes`.
#
# Nothing here is mocked. scripts/dev-sandbox.sh provides the fake Internet --
# a bubblewrap sandbox with no writable host mounts, a MITM proxy serving the
# canonical install.sh URL, and a git-upload-pack shim standing in for
# github.com -- so `install.sh` really installs uv, a managed Python, Node and
# the venv, cloning "github.com" over the ssh-first path a user hits.
#
# One route per run, on a sandbox built from scratch, because the routes are only
# meaningful from a pristine install. Sharing one install across routes -- or
# rewinding the checkout with `git reset --hard` between them -- leaves the
# second route running against a tree the first already updated (same venv, same
# installed console script, same __pycache__), which is not the state any real
# user is in: a route can then pass only because its predecessor did the work,
# and a failure in the first leaves the second exercising something undefined.
# If you add a route, give it its own run.
#
# Usage:
#   tests/install/install-update-e2e.sh --route update|installer
#                                       [--install-ref REF] [--keep]
#
#   --route         which update path to exercise (required):
#                     update     `hermes update`
#                     installer  re-running the curl one-liner over the checkout
#   --install-ref   what to install first; anything git resolves (a branch, a
#                   tag like v2026.7.7, or a SHA reachable from main).
#                   Default: refs/heads/main.
#
# Requires a CLEAN worktree: every dev-sandbox invocation re-derives fake main
# from the working copy, so uncommitted changes move the update target between
# the call that installs and the call that verifies.

set -euo pipefail

ROUTE=""
INSTALL_REF="refs/heads/main"
KEEP=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --route)
      [ "$#" -ge 2 ] || { echo 'error: --route needs a value' >&2; exit 1; }
      ROUTE="$2"; shift 2 ;;
    --install-ref)
      [ "$#" -ge 2 ] || { echo 'error: --install-ref needs a value' >&2; exit 1; }
      INSTALL_REF="$2"; shift 2 ;;
    --keep) KEEP=true; shift ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 1 ;;
  esac
done
case "$ROUTE" in
  update|installer) ;;
  '') echo 'error: --route is required (update or installer)' >&2; exit 1 ;;
  *) echo "error: unknown route: $ROUTE (want update or installer)" >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# Keep sandbox state out of the default .hermes-sandbox so a run never clobbers
# a developer's own sandbox, and scope it per route so two routes can run
# concurrently (CI runs them as parallel matrix legs). dev-sandbox.sh joins this
# onto the worktree root and feeds it to `tar --exclude`, so it MUST be a
# relative directory name.
SANDBOX_DIR_NAME=".hermes-sandbox-e2e-$ROUTE"
export HERMES_DEV_SANDBOX_DIR="$SANDBOX_DIR_NAME"

SANDBOX_ROOT="$REPO_ROOT/$SANDBOX_DIR_NAME"
INSTALL_DIR="/home/hermes/.hermes/hermes-agent"   # user-level layout (sandbox default)
FAKE_REMOTE="/work/repos/hermes-agent.git"
# Used to fetch the old installer and immutable submodule objects before the
# network-isolated sandbox is built. Same override dev-sandbox.sh honours, so a
# fork can retarget both together.
UPSTREAM_URL="${HERMES_DEV_SANDBOX_UPSTREAM:-https://github.com/NousResearch/hermes-agent.git}"

# Installer transcripts live outside the sandbox root: the sandbox is recreated
# and (unless --keep) deleted, and these logs are the most useful artifact when
# a real install breaks. Created after the dirty check below, so that a log dir
# pointed inside the repo cannot be the thing that makes the tree dirty.
LOG_DIR="${HERMES_E2E_LOG_DIR:-$(mktemp -d -t hermes-install-e2e-logs.XXXXXX)}"

step() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# The sandbox's internal logs (fake-internet proxy, slirp) explain failures that
# happen BEFORE install.sh gets to say anything -- a TLS handshake the proxy
# rejected looks like a bare `curl: (35)` from outside. Copy them out where a CI
# artifact upload can find them, and echo the proxy log since it is the usual
# culprit.
collect_sandbox_logs() {
  # Separate `local` statements on purpose: a single `local a=$1 b="$a"` does
  # NOT see the earlier assignment, so under `set -u` the second expansion dies
  # with "a: unbound variable".
  local tag="$1"
  local src="$SANDBOX_ROOT/root/logs"
  local dest="$LOG_DIR/sandbox-$tag"
  [ -d "$src" ] || return 0
  mkdir -p "$dest"
  cp -a "$src/." "$dest/" 2>/dev/null || true
  # Print it, not just archive it: a rejected TLS handshake here is the whole
  # explanation for a failure that otherwise reads as a bare `curl: (35)`, and
  # whoever is reading the job log should not have to download an artifact to
  # see it. In full, not tailed -- the file is short, and the useful line is not
  # reliably at the end.
  if [ -s "$dest/proxy.log" ]; then
    echo "--- sandbox proxy.log ---" >&2
    cat "$dest/proxy.log" >&2
    echo "--- end proxy.log ---" >&2
  fi
}

# ── preflight ──────────────────────────────────────────────────────────────
# Prefer the `sandbox` wrapper from the Nix devShell: it supplies both the PATH
# (bwrap, slirp4netns, openssl, ...) and the DEV_SANDBOX_* variables the script
# needs -- notably DEV_SANDBOX_DYNAMIC_LINKER, without which it cannot find a
# glibc loader on NixOS. Off Nix, the script is the entry point and finds its
# dependencies on the system PATH.
if command -v sandbox >/dev/null 2>&1; then
  SANDBOX=(sandbox)
elif command -v bwrap >/dev/null 2>&1; then
  SANDBOX=("$REPO_ROOT/scripts/dev-sandbox.sh")
else
  fail 'no usable sandbox: enter the Nix devShell (for `sandbox`) or install bubblewrap'
fi

if [ -n "$(git status --porcelain)" ]; then
  printf '\033[1;31m✗ working tree is dirty:\033[0m\n' >&2
  git status --porcelain | sed 's/^/    /' >&2
  fail 'Every sandbox invocation re-snapshots the working copy into a new
  fake-main commit, so the update target would move mid-run. Commit or stash
  first. (If a path above is build or log output, it needs gitignoring or to
  live outside the repo.)'
fi

mkdir -p "$LOG_DIR"

if [ "$KEEP" = false ]; then
  trap 'rm -rf -- "$SANDBOX_ROOT"' EXIT INT TERM
fi
rm -rf -- "$SANDBOX_ROOT"

# ── helpers ────────────────────────────────────────────────────────────────
# Resolve REF locally for fixture inspection. Fetching the exact ref is bounded
# and happens outside the sandbox; dev-sandbox.sh independently resolves it
# again before constructing the fake remote.
ensure_ref_available() {
  local ref="$1"
  if git rev-parse --verify -q "$ref^{commit}" >/dev/null; then
    printf '%s\n' "$ref"
    return 0
  fi
  git fetch -q --depth 1 "$UPSTREAM_URL" "$ref" 2>/dev/null || return 1
  printf '%s\n' FETCH_HEAD
}

# Does the INSTALLED hermes accept FLAG on `hermes update`?
# Capture first, then match in-process: `producer | grep -q` is not safe under
# pipefail because grep exits on the match and the producer can die on SIGPIPE.
update_supports() {
  local flag="$1"
  local help=""
  help="$(in_sandbox "hermes update --help 2>&1")" || return 1
  [[ "$help" == *"$flag"* ]]
}

# Does the installer at REF accept FLAG? Read it out of that ref's own
# install.sh rather than assuming this checkout's flag set: the point of the
# matrix is to install releases from months back, whose installers predate
# options we take for granted. Avoid the same pipefail/SIGPIPE false negative
# here: the v2026.8.19 installer is large enough to trigger it reliably.
installer_supports() {
  local ref="$1"
  local flag="$2"
  local script=""
  script="$(git show "$ref:scripts/install.sh" 2>/dev/null)" || {
    git fetch -q --depth 1 "$UPSTREAM_URL" "$ref" 2>/dev/null || return 1
    script="$(git show FETCH_HEAD:scripts/install.sh" 2>/dev/null)" || return 1
  }
  [[ "$script" == *"$flag"* ]]
}

# Old releases cloned GitHub submodules recursively. Sending those HTTPS URLs
# through the very proxy under test couples repository composition to transient
# public-network behavior and hides the SSH-first path's stderr. Seed immutable
# bare mirrors at the exact gitlink SHAs and rewrite GitHub HTTPS URLs to the
# sandbox SSH shim. The parent repository still comes from /work/repos and is
# promoted normally; only declared submodule objects are prepositioned.
prepare_submodule_seed() {
  local ref="$1"
  local resolved=""
  resolved="$(ensure_ref_available "$ref")" \
    || fail "could not resolve $ref while preparing submodule fixtures"

  SANDBOX_SEED_DIR="$LOG_DIR/sandbox-seed"
  mkdir -p "$SANDBOX_SEED_DIR/.hermes-sandbox-git/github.com"
  cat > "$SANDBOX_SEED_DIR/.gitconfig" <<'GITCONFIG'
[url "git@github.com:"]
    insteadOf = https://github.com/
GITCONFIG

  local modules="$LOG_DIR/install-ref.gitmodules"
  if ! git show "$resolved:.gitmodules" > "$modules" 2>/dev/null; then
    return 0
  fi

  local key path name url repo sub_sha mirror fetched attempt
  while read -r key path; do
    [ -n "$key" ] && [ -n "$path" ] || continue
    name="${key#submodule.}"
    name="${name%.path}"
    url="$(git config -f "$modules" --get "submodule.$name.url" 2>/dev/null || true)"
    case "$url" in
      https://github.com/*) repo="${url#https://github.com/}" ;;
      git@github.com:*) repo="${url#git@github.com:}" ;;
      *)
        fail "unsupported submodule URL in $ref: $url"
        ;;
    esac
    repo="${repo%/}"
    repo="${repo%.git}"
    if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
      fail "unsafe GitHub submodule path in $ref: $repo"
    fi

    sub_sha="$(git ls-tree "$resolved" -- "$path" | awk '$2 == "commit" { print $3; exit }')"
    [ -n "$sub_sha" ] || fail "could not resolve gitlink $path in $ref"

    mirror="$SANDBOX_SEED_DIR/.hermes-sandbox-git/github.com/$repo.git"
    rm -rf -- "$mirror"
    mkdir -p "$(dirname "$mirror")"
    git init --bare -q "$mirror"
    fetched=false
    for attempt in 1 2 3; do
      if git --git-dir="$mirror" fetch -q --force "$url" \
          "$sub_sha:refs/hermes-sandbox/pinned" 2>/dev/null; then
        fetched=true
        break
      fi
      sleep 2
    done
    if [ "$fetched" = false ]; then
      # Some servers disable direct reachable-SHA wants. Fetch advertised refs
      # as a compatibility fallback, then require the declared gitlink object.
      git --git-dir="$mirror" fetch -q --force "$url" \
        '+refs/heads/*:refs/remotes/origin/*' \
        '+refs/tags/*:refs/tags/*' 2>/dev/null \
        || fail "could not prefetch submodule $repo for $ref"
      git --git-dir="$mirror" cat-file -e "$sub_sha^{commit}" 2>/dev/null \
        || fail "submodule $repo did not contain declared commit $sub_sha"
    fi
    git --git-dir="$mirror" update-ref refs/heads/main "$sub_sha"
    git --git-dir="$mirror" symbolic-ref HEAD refs/heads/main
    ok "prefetched submodule $repo at ${sub_sha:0:12}"
  done < <(git config -f "$modules" --get-regexp '^submodule\..*\.path$' || true)
}

# Run the real install one-liner inside the sandbox. `ref` non-empty installs
# that upstream commit and promotes THIS checkout to fake main afterwards,
# leaving the state a user is in when an update is waiting; empty serves this
# worktree's own installer and points fake main here.
install_in_sandbox() {
  local what="$1"
  local ref="$2"
  local tag="$3"
  local log="$LOG_DIR/$tag.log"
  local args=(install --persistent --from "$SANDBOX_SEED_DIR")
  [ -n "$ref" ] && args+=(--install-ref "$ref")
  # Serve prefetched transport fixtures from the sandbox's static HTTP root.
  # Missing paths still forward upstream, so partial prefetch success degrades
  # to the real network rather than manufacturing a response.
  if [ "$HTTP_FIXTURE_READY" = true ]; then
    args+=(--http-root "$HTTP_FIXTURE_ROOT")
  fi

  # Installer flags have to match the installer being run, not this checkout's.
  # Older releases reject options added later ("Unknown option: --skip-browser"),
  # and this test deliberately installs releases from months back. --skip-setup
  # goes back further than any tag we sample; anything newer is probed for.
  local installer_flags=(--skip-setup)
  if [ -z "$ref" ] || installer_supports "$ref" --skip-browser; then
    installer_flags+=(--skip-browser)
  fi
  # Sandbox flags must precede `--`; the rest goes to install.sh.
  args+=(-- "${installer_flags[@]}")

  # Stream the installer's output to stdout AND keep a copy on disk. It is the
  # substance of this test -- a real install of uv, a managed Python, Node and
  # the venv -- so it belongs in the job log where anyone reading the run can
  # see it, not only in an artifact they have to download. The file copy is what
  # the artifact upload keeps and what the failure paths grep.
  #
  # `set -o pipefail` is load-bearing here: without it the pipeline reports
  # tee's status and a failed install looks like a pass.
  local status=0
  "${SANDBOX[@]}" "${args[@]}" 2>&1 | tee "$log" || status=$?

  if [ "$status" -ne 0 ]; then
    collect_sandbox_logs "$tag"
    fail "$what failed (exit $status)"
  fi
  grep -q 'Installation Complete' "$log" \
    || { collect_sandbox_logs "$tag"; \
         fail "$what did not report a completed install"; }
  ok "$what completed (log: $log)"
}

in_sandbox() { "${SANDBOX[@]}" --persistent bash -lc "$1"; }

# fake main's SHA is read fresh whenever it is needed, never cached across a
# sandbox invocation: each invocation re-derives it from the worktree.
sandbox_target() { in_sandbox "git --git-dir=$FAKE_REMOTE rev-parse main" | tr -d '[:space:]'; }
sandbox_head()   { in_sandbox "cd $INSTALL_DIR && git rev-parse HEAD" | tr -d '[:space:]'; }

require_landed_on_target() {
  local what="$1" head target
  head="$(sandbox_head)"
  target="$(sandbox_target)"
  [ "$head" = "$target" ] || fail "$what left HEAD at $head, wanted $target"
  ok "$what landed on ${head:0:12}"
}

# The real smoke test: goes through the venv launcher and imports the app, so it
# fails if the venv, dependencies, or entry point are broken.
require_hermes_works() {
  local when="$1" out
  out="$(in_sandbox "hermes --version" 2>&1)" \
    || { printf '%s\n' "$out" >&2; fail "hermes --version failed $when"; }
  printf '%s\n' "$out" | sed 's/^/    /'
  ok "hermes runs $when"
}

# Prefetch the astral.sh uv installer and binary so the sandbox serves them as
# fixtures. Those CDNs intermittently return empty replies to runner egress IPs.
# Also cache the Node index and exact archive used by old installers; otherwise
# the oldest compatibility leg can silently proceed without Node after two
# empty metadata responses, weakening the witness before it even reaches Git.
prepare_http_fixtures() {
  HTTP_FIXTURE_ROOT="$LOG_DIR/http-fixture"
  HTTP_FIXTURE_READY=false
  mkdir -p "$HTTP_FIXTURE_ROOT"

  if command -v curl >/dev/null 2>&1; then
    local uv_script="$HTTP_FIXTURE_ROOT/astral.sh/uv/install.sh"
    mkdir -p "$(dirname "$uv_script")"
    local attempt
    for attempt in 1 2 3 4 5; do
      if curl -fsSL --retry 3 --retry-delay 2 https://astral.sh/uv/install.sh \
          -o "$uv_script" 2>/dev/null && [ -s "$uv_script" ]; then
        HTTP_FIXTURE_READY=true
        break
      fi
      sleep 3
    done
    if [ -s "$uv_script" ]; then
      local uv_ver=""
      uv_ver="$(grep -om1 'releases/download/[0-9][0-9.]*' "$uv_script" | cut -d/ -f3)"
      if [ -n "$uv_ver" ]; then
        local uv_rel="github/uv/releases/download/$uv_ver/uv-x86_64-unknown-linux-gnu.tar.gz"
        mkdir -p "$HTTP_FIXTURE_ROOT/releases.astral.sh/$(dirname "$uv_rel")" \
                 "$HTTP_FIXTURE_ROOT/github.com/astral-sh/$(dirname "${uv_rel#github/}")"
        curl -fsSL --retry 3 --retry-delay 2 \
          "https://releases.astral.sh/$uv_rel" \
          -o "$HTTP_FIXTURE_ROOT/releases.astral.sh/$uv_rel" 2>/dev/null \
          && cp "$HTTP_FIXTURE_ROOT/releases.astral.sh/$uv_rel" \
             "$HTTP_FIXTURE_ROOT/github.com/astral-sh/${uv_rel#github/}" \
          || echo "⚠ could not prefetch uv $uv_ver tarball; binary will fetch upstream" >&2
      fi
      ok "prefetched uv installer for sandbox fixture"
    else
      echo "⚠ could not prefetch uv installer; install will fetch astral.sh directly" >&2
    fi
  fi

  local resolved="" installer_script="" node_version=""
  resolved="$(ensure_ref_available "$INSTALL_REF")" || return 0
  installer_script="$(git show "$resolved:scripts/install.sh" 2>/dev/null || true)"
  if [[ "$installer_script" =~ NODE_VERSION=\"([0-9]+)\" ]]; then
    node_version="${BASH_REMATCH[1]}"
  else
    return 0
  fi

  local node_os node_arch
  case "$(uname -s)" in
    Linux*) node_os=linux ;;
    Darwin*) node_os=darwin ;;
    *) return 0 ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) node_arch=x64 ;;
    aarch64|arm64) node_arch=arm64 ;;
    armv7l) node_arch=armv7l ;;
    *) return 0 ;;
  esac

  local node_dir="$HTTP_FIXTURE_ROOT/nodejs.org/dist/latest-v${node_version}.x"
  local node_index="$node_dir/index.html"
  mkdir -p "$node_dir"
  local attempt
  for attempt in 1 2 3 4 5; do
    if curl -fsSL --retry 3 --retry-delay 2 \
        "https://nodejs.org/dist/latest-v${node_version}.x/" \
        -o "$node_index" 2>/dev/null && [ -s "$node_index" ]; then
      break
    fi
    sleep 3
  done
  [ -s "$node_index" ] || {
    echo "⚠ could not prefetch Node v$node_version index; install will fetch upstream" >&2
    return 0
  }

  local index_text tarball=""
  index_text="$(cat "$node_index")"
  if [[ "$index_text" =~ (node-v${node_version}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.xz) ]]; then
    tarball="${BASH_REMATCH[1]}"
  elif [[ "$index_text" =~ (node-v${node_version}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.gz) ]]; then
    tarball="${BASH_REMATCH[1]}"
  fi
  [ -n "$tarball" ] || fail "Node v$node_version index had no $node_os-$node_arch archive"

  for attempt in 1 2 3 4 5; do
    if curl -fsSL --retry 3 --retry-delay 2 \
        "https://nodejs.org/dist/latest-v${node_version}.x/$tarball" \
        -o "$node_dir/$tarball" 2>/dev/null && [ -s "$node_dir/$tarball" ]; then
      HTTP_FIXTURE_READY=true
      ok "prefetched Node $tarball for sandbox fixture"
      return 0
    fi
    sleep 3
  done
  rm -f -- "$node_dir/$tarball"
  echo "⚠ could not prefetch $tarball; install will fetch it upstream" >&2
}

# ── install the earlier Hermes ─────────────────────────────────────────────
prepare_submodule_seed "$INSTALL_REF"
prepare_http_fixtures

step "installing upstream $INSTALL_REF (real curl | install.sh: uv, Python, Node, venv)"
install_in_sandbox "install of upstream $INSTALL_REF" "$INSTALL_REF" install

BASE="$(sandbox_head)"
TARGET="$(sandbox_target)"
[ -n "$BASE" ] || fail "could not read the installed commit"
[ "$BASE" != "$TARGET" ] \
  || fail "install landed on the update target ($BASE); base and target must differ"
ok "installed ${BASE:0:12}; update target is ${TARGET:0:12}"
require_hermes_works 'after install'

# ── apply exactly one update route ─────────────────────────────────────────
case "$ROUTE" in
  update)
    step 'ROUTE: hermes update'
    # `--yes` reaches the update subcommand only in later releases, and argparse
    # rejects the whole invocation when it does not exist. Ask the installed
    # hermes which it accepts; older ones read the prompt from stdin, so close it.
    if update_supports --yes; then
      update_cmd="hermes update --yes"
    else
      update_cmd="hermes update </dev/null"
    fi
    if ! in_sandbox "cd $INSTALL_DIR && $update_cmd"; then
      collect_sandbox_logs update
      fail "hermes update failed ($update_cmd)"
    fi
    require_landed_on_target 'hermes update'
    require_hermes_works 'after hermes update'
    ;;
  installer)
    step 'ROUTE: installer re-run over the existing checkout'
    # No ref: serves this worktree's installer and points fake main at this
    # checkout, which is what the re-run must land on.
    install_in_sandbox 'installer re-run' '' reinstall
    require_landed_on_target 'installer re-run'
    require_hermes_works 'after installer re-run'
    ;;
esac

printf '\n\033[1;32m✓ install/update E2E passed (route: %s, from: %s)\033[0m\n' \
  "$ROUTE" "$INSTALL_REF"
[ "$KEEP" = true ] && echo "  sandbox kept at $SANDBOX_ROOT"
exit 0

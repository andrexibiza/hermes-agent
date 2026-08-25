#!/usr/bin/env bash
# Stand-in for ssh inside the dev sandbox.
#
# The parent Hermes repository is served from the mutable fake remote under
# /work/repos. Exact submodule objects prepared by the E2E harness live in the
# seeded home directory. Route only GitHub-shaped upload-pack requests; reject
# unknown commands and paths instead of turning arbitrary ssh arguments into a
# filesystem traversal.

set -euo pipefail

[ "$#" -gt 0 ] || {
  echo 'sandbox ssh: missing remote command' >&2
  exit 128
}

# Git normally passes the remote command as one final argument, but tolerate an
# ssh implementation that splits `git-upload-pack` from its quoted repository.
remote_command="${!#}"
if [[ "$remote_command" != git-upload-pack\ * ]] && [ "$#" -ge 2 ]; then
  previous_index=$(( $# - 1 ))
  previous="${!previous_index}"
  if [ "$previous" = git-upload-pack ]; then
    remote_command="git-upload-pack $remote_command"
  fi
fi

case "$remote_command" in
  git-upload-pack\ *) repo="${remote_command#git-upload-pack }" ;;
  *)
    echo "sandbox ssh: unsupported remote command: $remote_command" >&2
    exit 128
    ;;
esac

# OpenSSH preserves the shell quotes Git places around the repository path.
repo="${repo#\'}"
repo="${repo%\'}"
repo="${repo#\"}"
repo="${repo%\"}"
repo="${repo#/}"

if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?$ ]]; then
  echo "sandbox ssh: unsafe repository path: $repo" >&2
  exit 128
fi

fixture_root="$HOME/.hermes-sandbox-git/github.com"

# prepare_submodule_seed starts with one broad HTTPS->SSH rewrite so even old
# installers whose .gitmodules use HTTPS enter this shim. Narrow that rewrite
# as soon as the parent clone starts: each seeded repository gets one exact
# prefix, and every unrelated GitHub dependency continues over real HTTPS via
# the MITM proxy. With no declared submodules the generated config is empty.
narrow_fixture_rewrites() {
  local config="$HOME/.gitconfig"
  local tmp="$config.tmp.$$"
  local fixture rel fixture_repo
  : > "$tmp"
  for fixture in "$fixture_root"/*/*.git; do
    [ -d "$fixture" ] || continue
    rel="${fixture#"$fixture_root"/}"
    fixture_repo="${rel%.git}"
    if [[ ! "$fixture_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
      rm -f -- "$tmp"
      echo "sandbox ssh: unsafe fixture repository path: $fixture_repo" >&2
      exit 128
    fi
    {
      printf '[url "git@github.com:%s"]\n' "$fixture_repo"
      printf '    insteadOf = https://github.com/%s\n' "$fixture_repo"
    } >> "$tmp"
  done
  mv -f -- "$tmp" "$config"
}

case "$repo" in
  NousResearch/hermes-agent|NousResearch/hermes-agent.git)
    narrow_fixture_rewrites
    repo_path=/work/repos/hermes-agent.git
    ;;
  *)
    repo_path="$fixture_root/${repo%.git}.git"
    ;;
esac

[ -d "$repo_path" ] || {
  echo "sandbox ssh: repository fixture not found: $repo" >&2
  exit 128
}

exec @GIT_UPLOAD_PACK@ "$repo_path"

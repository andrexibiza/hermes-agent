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

case "$repo" in
  NousResearch/hermes-agent|NousResearch/hermes-agent.git)
    repo_path=/work/repos/hermes-agent.git
    ;;
  *)
    repo_path="$HOME/.hermes-sandbox-git/github.com/${repo%.git}.git"
    ;;
esac

[ -d "$repo_path" ] || {
  echo "sandbox ssh: repository fixture not found: $repo" >&2
  exit 128
}

exec @GIT_UPLOAD_PACK@ "$repo_path"

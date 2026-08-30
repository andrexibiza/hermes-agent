#!/usr/bin/env bash
set -euo pipefail

readonly TARGET_BRANCH="security/webhook-freshness"
readonly SOURCE_HEAD="359bd692ee887fa0562e6d99fd3c0cedccc2ac02"
readonly SOURCE_PARENT="4209d371aa1bb8840ce8447555bdd863a1a96c38"
readonly AUTHOR_NAME="Axl Ibiza, MBA"
readonly AUTHOR_EMAIL="andrexibiza@gmail.com"
readonly UPSTREAM_URL="https://github.com/NousResearch/hermes-agent.git"
readonly -a OWNED_PATHS=(
  "gateway/platforms/webhook.py"
  "tests/gateway/test_webhook_adapter.py"
  "tests/gateway/test_webhook_legacy_freshness_contract.py"
)

# The checkout action has installed a write-scoped credential on origin.
git remote remove upstream 2>/dev/null || true
git remote add upstream "${UPSTREAM_URL}"
git fetch --no-tags upstream refs/heads/main:refs/remotes/upstream/main
git fetch --no-tags origin \
  "refs/heads/${TARGET_BRANCH}:refs/remotes/origin/webhook-freshness"

readonly base="$(git rev-parse refs/remotes/upstream/main)"
readonly observed_source="$(git rev-parse refs/remotes/origin/webhook-freshness)"
test "${observed_source}" = "${SOURCE_HEAD}"
test "$(git rev-parse "${SOURCE_HEAD}^")" = "${SOURCE_PARENT}"

readonly expected_paths="$(printf '%s\n' "${OWNED_PATHS[@]}" | sort)"
readonly source_paths="$(git diff --name-only "${SOURCE_PARENT}" "${SOURCE_HEAD}" | sort)"
test "${source_paths}" = "${expected_paths}"

git diff --binary "${SOURCE_PARENT}" "${SOURCE_HEAD}" -- "${OWNED_PATHS[@]}" \
  > "${RUNNER_TEMP}/webhook-replay-security.patch"
test -s "${RUNNER_TEMP}/webhook-replay-security.patch"

git reset --hard "${base}"
git clean -fdx
git apply --3way --index "${RUNNER_TEMP}/webhook-replay-security.patch"

readonly staged_paths="$(git diff --cached --name-only | sort)"
test "${staged_paths}" = "${expected_paths}"
git diff --cached --check

# Preserve merged #97204's off-loop GitHub-comment liveness while composing
# the replay owner. These tests pin both the production seam and its contract.
grep -q "_deliver_github_comment" gateway/platforms/webhook.py
grep -q "asyncio.to_thread" gateway/platforms/webhook.py

uv run --frozen pytest \
  tests/gateway/test_webhook_adapter.py \
  tests/gateway/test_webhook_legacy_freshness_contract.py \
  tests/gateway/test_webhook_offloop_delivery.py \
  tests/gateway/test_webhook_deliver_only.py \
  -q
uv run --frozen ruff check \
  gateway/platforms/webhook.py \
  tests/gateway/test_webhook_adapter.py \
  tests/gateway/test_webhook_legacy_freshness_contract.py

readonly tree="$(git write-tree)"
cat > "${RUNNER_TEMP}/webhook-replay-security-message" <<EOF
fix(webhook): bind replay identity to authenticated content

Rematerialize the reviewed replay-security owner on exact current upstream main as one publication commit. The authenticated request identity binds replay state, legacy body-only signatures remain refused, and merged #97204 continues to own off-loop GitHub-comment delivery liveness.

Current-main: ${base}
Source-head: ${SOURCE_HEAD}
Source-parent: ${SOURCE_PARENT}
Composition-tree: ${tree}
Replay-owner: #97218
Liveness-owner: #97204
Downstream-consolidation: #97083 must subtract this replay surface

Signed-off-by: Axl Ibiza, MBA <andrexibiza@gmail.com>
EOF

GIT_AUTHOR_NAME="${AUTHOR_NAME}" \
GIT_AUTHOR_EMAIL="${AUTHOR_EMAIL}" \
GIT_COMMITTER_NAME="${AUTHOR_NAME}" \
GIT_COMMITTER_EMAIL="${AUTHOR_EMAIL}" \
  git commit --cleanup=verbatim -F "${RUNNER_TEMP}/webhook-replay-security-message"

readonly new_head="$(git rev-parse HEAD)"
test "$(git rev-parse HEAD^)" = "${base}"
test "$(git rev-parse HEAD^{tree})" = "${tree}"
test "$(git rev-list --count "${base}"..HEAD)" = "1"
test "$(git diff --name-only "${base}"..HEAD | sort)" = "${expected_paths}"
test "$(git show -s --format=%an HEAD)" = "${AUTHOR_NAME}"
test "$(git show -s --format=%ae HEAD)" = "${AUTHOR_EMAIL}"
test "$(git show -s --format=%cn HEAD)" = "${AUTHOR_NAME}"
test "$(git show -s --format=%ce HEAD)" = "${AUTHOR_EMAIL}"
git cat-file -p HEAD | grep -Eq '^author Axl Ibiza, MBA <andrexibiza@gmail.com> [0-9]+ [+-][0-9]{4}$'
git cat-file -p HEAD | grep -Eq '^committer Axl Ibiza, MBA <andrexibiza@gmail.com> [0-9]+ [+-][0-9]{4}$'
git show -s --format=%B HEAD | grep -Fx 'Signed-off-by: Axl Ibiza, MBA <andrexibiza@gmail.com>'
git diff --check "${base}"..HEAD

# Exact-main proof is invalid if upstream moved during composition/testing.
git fetch --no-tags upstream refs/heads/main:refs/remotes/upstream/main
readonly final_main="$(git rev-parse refs/remotes/upstream/main)"
test "${final_main}" = "${base}"

git push \
  --force-with-lease="refs/heads/${TARGET_BRANCH}:${SOURCE_HEAD}" \
  origin "HEAD:refs/heads/${TARGET_BRANCH}"

printf 'published_head=%s\nbase=%s\ntree=%s\n' "${new_head}" "${base}" "${tree}"

#!/usr/bin/env bash
# Deploy the live box.
#   sudo bash /opt/edgerunner/app/deploy/gcp/deploy.sh          # whatever origin/live points at
#   sudo bash /opt/edgerunner/app/deploy/gcp/deploy.sh <sha>    # pin/rollback to a commit
#
# ONLY_IF_CHANGED=1 turns the restart into a no-op when the box already holds
# the target commit. The nightly timer in /etc/cron.d/edgerunner-deploy uses it.
#
# Live tracks the `live` branch, never `main`. `live` carries no commits of its
# own — it is a pointer you fast-forward from `main` once a change has proven
# itself on paper. Nothing is ever merged back from `live` into `main`.
#
#   git checkout live && git merge --ff-only main && git push origin live
#
# Then run this. Nothing on this box moves until you do.
set -euo pipefail

APP_DIR=/opt/edgerunner
LIVE_BRANCH="${LIVE_BRANCH:-live}"
# The tree is owned by the edgerunner user; this script runs as root, and git
# refuses another user's repo as "dubious ownership" without this.
git config --global --add safe.directory "$APP_DIR/app" 2>/dev/null || true
cd "$APP_DIR/app"

git fetch --force --prune origin "+refs/heads/*:refs/remotes/origin/*"

REF="${1:-origin/${LIVE_BRANCH}}"
git rev-parse -q --verify "${REF}^{commit}" >/dev/null || {
	echo "!! '$REF' is not a known commit or branch."
	exit 1
}

CURRENT=$(git rev-parse --short HEAD)
TARGET=$(git rev-parse --short "$REF")
if [[ "$CURRENT" == "$TARGET" ]]; then
	# The nightly timer sets this. Without it an unchanged `live` would still
	# bounce the process every night, which costs a warm-up and an open-order
	# reconciliation for no reason.
	if [[ -n "${ONLY_IF_CHANGED:-}" ]]; then
		echo "==> already on $TARGET, nothing to do"
		exit 0
	fi
	echo "==> already on $TARGET, restarting anyway"
else
	echo "==> $CURRENT -> $TARGET ($REF)"
	git log --oneline "${CURRENT}..${TARGET}" 2>/dev/null | head -20 || true
fi

# Detached HEAD: no local branch to drift, and the deployed commit is explicit.
git checkout -q --detach "$REF"
"$APP_DIR/venv/bin/pip" install -q -r requirements.txt
chown -R edgerunner:edgerunner "$APP_DIR/app"

systemctl restart edgerunner
sleep 5
if systemctl is-active --quiet edgerunner; then
	echo "==> running $TARGET"
else
	echo "!! FAILED to start on $TARGET. Roll back with:"
	echo "     sudo bash $0 $CURRENT"
	systemctl --no-pager --lines=30 status edgerunner
	exit 1
fi

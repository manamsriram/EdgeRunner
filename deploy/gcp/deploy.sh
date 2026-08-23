#!/usr/bin/env bash
# Deploy a tagged release to the live box.
#   sudo bash /opt/edgerunner/app/deploy/gcp/deploy.sh              # latest live-* tag
#   sudo bash /opt/edgerunner/app/deploy/gcp/deploy.sh live-2026-08-23   # a specific one
#   sudo bash /opt/edgerunner/app/deploy/gcp/deploy.sh live-2026-08-10   # rollback
#
# Live tracks TAGS, not main. Paper (Render) auto-deploys main and is where
# changes are proven; this box moves only when you tag and run this. Rollback is
# a checkout of the previous tag, not a revert commit written under pressure
# while positions are open.
set -euo pipefail

APP_DIR=/opt/edgerunner
cd "$APP_DIR/app"

git fetch --tags --force --prune origin

REF="${1:-}"
if [[ -z "$REF" ]]; then
	REF=$(git tag -l 'live-*' --sort=-creatordate | head -1)
	[[ -n "$REF" ]] || {
		echo "!! No live-* tag exists. Create one first:"
		echo "     git tag live-\$(date +%F) && git push origin live-\$(date +%F)"
		exit 1
	}
fi
git rev-parse -q --verify "refs/tags/${REF}" >/dev/null || {
	echo "!! Tag '$REF' not found. Available:"
	git tag -l 'live-*' --sort=-creatordate | head -10
	exit 1
}

CURRENT=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)
echo "==> $CURRENT  ->  $REF"

# Detached HEAD at the tag: there is no branch to accidentally fast-forward.
git checkout -q --detach "refs/tags/${REF}"
"$APP_DIR/venv/bin/pip" install -q -r requirements.txt
chown -R edgerunner:edgerunner "$APP_DIR/app"

systemctl restart edgerunner
sleep 5
if systemctl is-active --quiet edgerunner; then
	echo "==> running $REF"
else
	echo "!! FAILED to start on $REF. Roll back with:"
	echo "     sudo bash $0 $CURRENT"
	systemctl --no-pager --lines=30 status edgerunner
	exit 1
fi

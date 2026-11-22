#!/usr/bin/env bash
#
# Cut a SIGNED node release (node-26#17, server-26#318). Owner-only: run on the
# machine that holds the release key. Replaces the CI tag/promote workflows,
# which could only make unsigned tags — nodes that enforce signatures ignore
# those, so CI must never hold this key (that would put it back on the host
# the signature exists to distrust).
#
#   scripts/release.sh             # tag HEAD of main as the next vX.Y.Z (REF=latest nodes take it)
#   scripts/release.sh --promote   # same, then move vMAJOR to it   (REF=v1 / stable nodes take it)
#
# Push main first. The images for the commit must exist before nodes can
# install it; the build workflows publish them on the main push.
set -euo pipefail

KEY="${DRB_RELEASE_KEY:-$HOME/.ssh/drb-release}"
PROMOTE=0
case "${1:-}" in
  "") ;;
  --promote) PROMOTE=1 ;;
  *) echo "usage: $0 [--promote]" >&2; exit 2 ;;
esac
die() { echo "release: $*" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)"
[ -r "$KEY" ] || die "release key $KEY not found"
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "not on main"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "uncommitted changes"
git fetch -q --force --tags origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "HEAD is not origin/main — push (or pull) first"

sign=(-c gpg.format=ssh -c "user.signingkey=$KEY")
signers="$(mktemp)"; trap 'rm -f "$signers"' EXIT
printf 'drb-release namespaces="git" %s\n' "$(cat "$KEY.pub")" > "$signers"
check() { git -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$signers" verify-tag "refs/tags/$1" >/dev/null 2>&1 \
            || die "tag $1 did not verify after signing — NOT pushed"; }

TAG="$(bash scripts/next-version.sh)"
if [ -z "$TAG" ]; then
  TAG="$(git tag --points-at HEAD | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
  echo "HEAD is already $TAG"
  check "$TAG"
else
  git "${sign[@]}" tag -s -m "DRB node release $TAG" "$TAG"
  check "$TAG"
  git push origin "refs/tags/$TAG"
  echo "released $TAG (signed)"
fi

if [ "$PROMOTE" = 1 ]; then
  MAJOR="${TAG%%.*}"
  git "${sign[@]}" tag -f -s -m "DRB promote $MAJOR -> $TAG" "$MAJOR" "$TAG^{commit}"
  check "$MAJOR"
  git push --force origin "refs/tags/$MAJOR"
  echo "promoted $MAJOR -> $TAG (signed)"
fi

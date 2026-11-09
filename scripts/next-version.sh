#!/usr/bin/env bash
# node-26#49 — the release tag this commit should get, or nothing if it has one.
#
# VERSION holds MAJOR.MINOR and is edited by hand. PATCH is automatic: one
# more than the highest existing vMAJOR.MINOR.PATCH tag, compared as an
# integer (so .10 follows .9), starting at 0 for a new MAJOR.MINOR.
#
# Usage: scripts/next-version.sh   (from the repo root, tags fetched)
set -euo pipefail

base="$(tr -d ' \r\n' < VERSION)"
[[ "$base" =~ ^[0-9]+\.[0-9]+$ ]] || { echo "VERSION must be MAJOR.MINOR, got '$base'" >&2; exit 1; }
pattern="^v${base//./\\.}\\.[0-9]+$"

if git tag --points-at HEAD | grep -qE "$pattern"; then
  exit 0   # this commit is already released
fi

last="$(git tag -l "v${base}.*" | grep -E "$pattern" | sed "s/^v${base}\.//" | sort -n | tail -1 || true)"
echo "v${base}.$(( ${last:--1} + 1 ))"

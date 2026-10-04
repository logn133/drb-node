# shellcheck shell=bash
#
# Pin each node image to the exact source it was built from (node-26#19).
# Sourced by install.sh and scripts/drb-update.sh; not run directly.
#
# The tag is the git TREE hash of the image's build context, `src-<tree>`.
# CI computes the same value (`git rev-parse HEAD:<dir>` in the build
# workflows), so a node asks for the image built from the very directory it
# has checked out. That is exact even though each image is only rebuilt when
# its own directory changes: an unchanged directory keeps the same tree hash,
# and so the same image, across commits. :latest / :stable still exist for
# local dev; a provisioned node never uses them.
#
# docker-compose.yml reads these as ${DRB_TAG_*:-latest}. The values live in
# .env so every manual `docker compose ...` on the node sees the same pins.

# VAR:build-context-dir, one per compose service with an image.
DRB_IMAGE_SOURCES="DRB_TAG_ICECAST:icecast DRB_TAG_OP25:op25-container \
DRB_TAG_SECONDARY_SDR:secondary-sdr-container DRB_TAG_EDGE_NODE:drb-edge-node"

# drb_image_tags REPO_DIR COMMIT -> "VAR=src-<tree>" lines. A directory that
# does not exist at COMMIT is skipped (its compose file won't reference it).
drb_image_tags() {
  local _p _var _dir _tree
  for _p in $DRB_IMAGE_SOURCES; do
    _var="${_p%%:*}"; _dir="${_p#*:}"
    _tree="$(git -C "$1" rev-parse --verify -q "$2:$_dir" 2>/dev/null)" || continue
    printf '%s=src-%s\n' "$_var" "$_tree"
  done
}

# drb_env_set ENV_FILE KEY VALUE — replace KEY's line in place, or append it.
# Rewrites through the same inode so .env keeps its 0600 mode and owner.
drb_env_set() {
  local _tmp; _tmp="$(mktemp)"
  awk -v k="$2" -v v="$3" '
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$1" > "$_tmp" && cat "$_tmp" > "$1"
  local _rc=$?; rm -f "$_tmp"; return $_rc
}

# drb_pin_image_tags ENV_FILE REPO_DIR COMMIT — write the pins into .env and
# export them, so compose sees them whether or not it re-reads .env.
drb_pin_image_tags() {
  local _line
  while IFS= read -r _line; do
    drb_env_set "$1" "${_line%%=*}" "${_line#*=}" || return 1
    export "$_line"
  done < <(drb_image_tags "$2" "$3")
}

# drb_images_present COMPOSE_ARGS... — 0 if every image the compose project
# names is in the local image store. `compose pull` only WARNS when a service
# that also has a build: section fails to pull, so its exit code alone does
# not prove the pinned images exist.
drb_images_present() {
  local _imgs _img _missing=0
  _imgs="$(docker compose "$@" config --images)" || return 1
  while IFS= read -r _img; do
    [ -n "$_img" ] || continue
    docker image inspect "$_img" >/dev/null 2>&1 || { printf 'missing image: %s\n' "$_img" >&2; _missing=1; }
  done <<< "$_imgs"
  return $_missing
}

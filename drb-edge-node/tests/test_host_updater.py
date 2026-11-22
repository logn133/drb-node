"""
node-26#15 — Client/scripts/drb-update.sh, the host-level updater.

Runs the real script with stub git/docker/curl on PATH, so what is checked is
the script's decision logic and its security guards: it only updates when
asked (MQTT sentinel or a new C2 request), never takes anything but "update
now" from C2, refuses a mismatched git origin or registry, and keeps node
credentials off curl's argv.
"""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "drb-update.sh"

pytestmark = pytest.mark.skipif(
    not (SCRIPT.exists() and shutil.which("bash") and shutil.which("jq") and shutil.which("flock")),
    reason="needs the repo's scripts/ (absent inside the image) plus bash, jq and flock",
)

REPO = "https://git.example.net/logan/node-26.git"
OLD, NEW, TREE = "a" * 40, "b" * 40, "c" * 40
REPO_ROOT = SCRIPT.parents[1]

STUB_GIT = r'''#!/usr/bin/env bash
echo "git $*" >> "$STUB/calls"
while [ "$1" = -c ] || [ "$1" = -C ]; do shift 2; done
case "$1" in
  --version) echo "git version ${STUB_GIT_VERSION:-2.39.5}" ;;
  rev-parse)
    if [ "$2" = HEAD ]; then cat "$STUB/head"
    elif [ "$2" = --verify ]; then echo "$STUB_TREE"   # tree hash of <commit>:<dir>
    else echo "$STUB_NEW"; fi ;;
  remote) echo "$STUB_ORIGIN" ;;
  fetch) exit 0 ;;
  show-ref) [ "$4" = "refs/tags/v1" ] ;;
  cat-file)
    if [ "$2" = tag ]; then   # signed `tag` header; STUB_TAG_HEADER forges a relabel
      echo "object $STUB_NEW"; echo "tag ${STUB_TAG_HEADER:-${3#refs/tags/}}"
    else [ -z "${STUB_NO_PIN:-}" ]; fi ;;   # target has scripts/image-tags.sh
  merge-base) [ -z "${STUB_NOT_AHEAD:-}" ] ;;
  show) case "$2" in
          *:scripts/image-tags.sh) cat "$STUB_REPO/scripts/image-tags.sh" ;;
          *:docker-compose.yml) cat "$STUB_REPO/docker-compose.yml" ;;
        esac ;;
  checkout) echo "${@: -1}" > "$STUB/head" ;;
  verify-tag) grep -qxF "${2#refs/tags/}" "$STUB/signed" 2>/dev/null ;;
  tag)
    if [ "$2" = --points-at ]; then
      if [ "$3" = HEAD ]; then cat "$STUB/tags_head" 2>/dev/null; else cat "$STUB/tags_new" 2>/dev/null; fi
    else cat "$STUB/tags_all" 2>/dev/null; fi
    exit 0 ;;
  *) exit 0 ;;
esac
'''
STUB_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$STUB/calls"
case "$*" in
  *"config --images"*) echo "reg.example/edge-node:$STUB_PULLED_TAG" ;;
  "image inspect"*) [ -z "${STUB_MISSING_IMAGE:-}" ] || exit 1 ;;
esac
exit 0
'''
STUB_CURL = r'''#!/usr/bin/env bash
echo "curl $*" >> "$STUB/calls"
for a in "$@"; do case "$a" in @*) cat "${a#@}" >> "$STUB/headers" ;; esac; done
cat "$STUB/response"
'''


def _exe(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path):
    stub = tmp_path / "stub"
    bindir = stub / "bin"
    bindir.mkdir(parents=True)
    _exe(bindir / "git", STUB_GIT)
    _exe(bindir / "docker", STUB_DOCKER)
    _exe(bindir / "curl", STUB_CURL)
    (stub / "head").write_text(OLD + "\n")
    (stub / "calls").write_text("")
    (stub / "response").write_text(json.dumps({"update_requested_at": None}))

    inst = tmp_path / "node"
    (inst / ".git").mkdir(parents=True)
    (inst / "configs").mkdir()
    (inst / ".env").write_text(
        "NODE_ID=node-7\nC2_URL=https://api.example.net\nIMAGE_REGISTRY=git.example.net\n"
    )
    (inst / "configs" / "credentials.json").write_text(json.dumps({"api_key": "k" * 64}))
    (inst / "configs" / "pickup_secret").write_text("p" * 48)

    conf = tmp_path / "updater.conf"
    conf.write_text(f"INSTALL_DIR={inst}\nREPO_URL={REPO}\nREF=v1\nREGISTRY=git.example.net\nBUILD=0\n")

    e = dict(os.environ)
    e.update({
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB": str(stub), "STUB_NEW": NEW, "STUB_ORIGIN": REPO,
        "STUB_TREE": TREE, "STUB_REPO": str(REPO_ROOT), "STUB_PULLED_TAG": f"src-{TREE}",
        "DRB_UPDATER_CONF": str(conf),
        "DRB_UPDATER_STATE": str(tmp_path / "state"),
        "DRB_UPDATER_LOCK": str(tmp_path / "lock"),
        "DRB_UPDATER_SIGNERS": str(tmp_path / "release-signers"),
        "DRB_UPDATER_SIGNING_REQUIRED": str(tmp_path / "signing-required"),
    })
    return {"env": e, "stub": stub, "inst": inst, "conf": conf, "state": tmp_path / "state",
            "signers": tmp_path / "release-signers", "required": tmp_path / "signing-required"}


def _run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env["env"], capture_output=True, text=True, timeout=30)


def _calls(env):
    return (env["stub"] / "calls").read_text()


def _pulled(calls: str) -> bool:
    return any(line.startswith("docker compose ") and line.endswith(" pull") for line in calls.splitlines())


def test_no_request_means_checkin_only(env):
    r = _run(env)
    assert r.returncode == 0, r.stderr
    calls = _calls(env)
    assert "update-check" in calls
    assert "docker" not in calls
    assert "checkout" not in calls


def test_c2_request_updates_once_and_reports(env):
    (env["stub"] / "response").write_text(json.dumps({"update_requested_at": "2026-10-04T12:00:00+00:00"}))
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    calls = _calls(env)
    assert _pulled(calls)
    assert "docker compose up -d --no-build --force-recreate --remove-orphans" in calls
    assert (env["stub"] / "head").read_text().strip() == NEW
    assert '"applied_request_at":"2026-10-04T12:00:00+00:00"' in calls
    assert '"result":"ok"' in calls
    # Same request again: nothing to do.
    (env["stub"] / "calls").write_text("")
    assert _run(env).returncode == 0
    assert "docker" not in _calls(env)


def test_mqtt_sentinel_triggers_update_and_is_consumed(env):
    sentinel = env["inst"] / "configs" / "update-requested"
    sentinel.write_text('{"ref": "evil", "cmd": "rm -rf /"}')
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert not sentinel.exists()
    calls = _calls(env)
    assert _pulled(calls)
    assert "evil" not in calls and "rm -rf" not in calls


def test_update_prunes_build_cache_and_stale_images_after_up(env):
    # node-26#41: the build cache was never pruned and filled node-002's disk.
    (env["stub"] / "response").write_text(json.dumps({"update_requested_at": "2026-10-04T12:00:00+00:00"}))
    assert _run(env).returncode == 0
    lines = _calls(env).splitlines()
    up = next(i for i, ln in enumerate(lines) if ln.startswith("docker compose up -d"))
    builder = next(i for i, ln in enumerate(lines) if ln.startswith("docker builder prune -af --filter until="))
    images = next(i for i, ln in enumerate(lines) if ln.startswith("docker image prune -af --filter until="))
    assert up < builder and up < images  # only after the new containers are running


def test_c2_response_is_never_used_as_a_ref_or_command(env):
    (env["stub"] / "response").write_text(json.dumps({"update_requested_at": "$(touch /tmp/pwned); main"}))
    r = _run(env)
    assert r.returncode == 0
    assert "docker" not in _calls(env)  # malformed request is ignored, not executed


def test_refuses_when_git_origin_differs(env):
    env["env"]["STUB_ORIGIN"] = "https://attacker.example/node-26.git"
    (env["inst"] / "configs" / "update-requested").write_text("")
    r = _run(env)
    assert r.returncode != 0
    assert "refusing" in r.stderr
    assert "docker" not in _calls(env)


def test_refuses_when_env_registry_differs(env):
    (env["inst"] / ".env").write_text(
        "NODE_ID=node-7\nC2_URL=https://api.example.net\nIMAGE_REGISTRY=evil.example\n"
    )
    (env["inst"] / "configs" / "update-requested").write_text("")
    r = _run(env)
    assert r.returncode != 0
    assert not _pulled(_calls(env))


def test_non_https_repo_in_conf_is_rejected(env):
    env["conf"].write_text(env["conf"].read_text().replace("https://", "http://"))
    r = _run(env)
    assert r.returncode != 0
    assert "git" not in _calls(env)


def test_credentials_go_in_a_header_file_never_argv(env):
    _run(env)
    calls = _calls(env)
    assert "k" * 64 not in calls and "p" * 48 not in calls
    headers = (env["stub"] / "headers").read_text()
    assert f"Authorization: Bearer {'k' * 64}" in headers
    assert f"X-Pickup-Secret: {'p' * 48}" in headers


def test_plain_http_c2_skips_checkin(env):
    (env["inst"] / ".env").write_text("NODE_ID=node-7\nC2_URL=http://api.example.net\nIMAGE_REGISTRY=git.example.net\n")
    r = _run(env)
    assert r.returncode == 0
    assert "curl" not in _calls(env)


# ── node-26#19: images pinned to the target's source tree ─────────────────

def test_pins_images_and_pulls_before_checkout(env):
    (env["inst"] / "configs" / "update-requested").write_text("")
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    lines = _calls(env).splitlines()
    pull = next(i for i, ln in enumerate(lines) if ln.startswith("docker compose ") and ln.endswith(" pull"))
    checkout = next(i for i, ln in enumerate(lines) if ln.startswith("git") and " checkout " in ln)
    assert pull < checkout, "images must be pulled before the new code is checked out"
    dotenv = (env["inst"] / ".env").read_text()
    for var in ("DRB_TAG_EDGE_NODE", "DRB_TAG_OP25", "DRB_TAG_ICECAST", "DRB_TAG_SECONDARY_SDR"):
        assert f"{var}=src-{TREE}\n" in dotenv
    assert dotenv.startswith("NODE_ID=node-7\n")  # existing lines kept
    assert not (env["inst"] / ".drb-update-compose.yml").exists()


def test_missing_pinned_image_changes_nothing(env):
    env["env"]["STUB_MISSING_IMAGE"] = "1"
    (env["inst"] / "configs" / "update-requested").write_text("")
    r = _run(env)
    assert r.returncode != 0
    assert (env["stub"] / "head").read_text().strip() == OLD
    calls = _calls(env)
    assert " checkout " not in calls
    assert "up -d" not in calls
    assert "DRB_TAG_" not in (env["inst"] / ".env").read_text()


def test_target_without_pinning_still_updates(env):
    env["env"]["STUB_NO_PIN"] = "1"
    (env["inst"] / "configs" / "update-requested").write_text("")
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert (env["stub"] / "head").read_text().strip() == NEW
    assert _pulled(_calls(env))
    assert "DRB_TAG_" not in (env["inst"] / ".env").read_text()


# ---------------------------------------------------------------------------
# Releases (node-26#49): a newer version on the node's channel is a reason to
# update by itself, and versions compare per field as integers.
# ---------------------------------------------------------------------------

def _channel(env, ref, all_tags, target_tags, head_tags):
    env["conf"].write_text(env["conf"].read_text().replace("REF=v1", f"REF={ref}"))
    (env["stub"] / "tags_all").write_text("\n".join(all_tags) + "\n")
    (env["stub"] / "tags_new").write_text("\n".join(target_tags) + "\n")
    (env["stub"] / "tags_head").write_text("\n".join(head_tags) + "\n")


def test_latest_channel_installs_a_newer_release_unasked(env):
    # 1.1.10 is newer than 1.1.9: compared as integers, not strings or floats.
    _channel(env, "latest", ["v1", "v1.1.9", "v1.1.10", "v1.1.2"], ["v1.1.10"], ["v1.1.9"])
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "release 1.1.10 on latest (installed 1.1.9)" in r.stdout
    assert (env["stub"] / "head").read_text().strip() == NEW
    assert "DRB_VERSION=1.1.10" in (env["inst"] / ".env").read_text()


def test_never_downgrades(env):
    _channel(env, "latest", ["v1.1.9", "v1.2.0"], ["v1.1.9"], ["v1.2.0"])
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert "docker" not in _calls(env)


def test_same_release_is_not_reinstalled(env):
    _channel(env, "v1", ["v1", "v1.1.3"], ["v1", "v1.1.3"], ["v1", "v1.1.3"])
    assert _run(env).returncode == 0
    assert "docker" not in _calls(env)


def test_promoted_major_tag_installs_on_stable_nodes(env):
    _channel(env, "v1", ["v1", "v1.1.0", "v1.1.4"], ["v1", "v1.1.4"], ["v1.1.0"])
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "release 1.1.4 on v1" in r.stdout
    assert (env["stub"] / "head").read_text().strip() == NEW


def test_untagged_target_still_needs_a_request(env):
    """v1 before its first promotion points at a commit with no release tag."""
    _channel(env, "v1", ["v1"], ["v1"], [])
    assert _run(env).returncode == 0
    assert "docker" not in _calls(env)


def _portal(env, **answer):
    (env["stub"] / "response").write_text(json.dumps({"update_requested_at": None, **answer}))


def test_portal_nightly_channel_overrides_the_installed_ref(env):
    _channel(env, "v1", ["v1", "v1.1.0", "v1.1.7"], ["v1.1.7"], ["v1.1.0"])
    _portal(env, channel="nightly", auto_update=True)
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "release 1.1.7 on latest (installed 1.1.0)" in r.stdout
    assert (env["state"] / "channel").read_text() == "nightly"


def test_portal_stable_follows_the_highest_major_tag(env):
    _channel(env, "latest", ["v1", "v2", "v1.1.0", "v2.0.0", "v2.0.3"], ["v2", "v2.0.0"], ["v1.1.0"])
    _portal(env, channel="stable")
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "release 2.0.0 on stable (installed 1.1.0)" in r.stdout
    assert "refs/tags/v2" in _calls(env)


def test_auto_update_off_skips_new_releases_but_honours_a_request(env):
    _channel(env, "latest", ["v1.1.0", "v1.1.5"], ["v1.1.5"], ["v1.1.0"])
    _portal(env, auto_update=False)
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert "auto-update is off" in r.stdout
    assert "docker" not in _calls(env)

    _portal(env, update_requested_at="2026-11-13T12:00:00+00:00")
    r = _run(env)   # C2 unreachable or silent on auto_update: the stored "off" still holds
    assert r.returncode == 0, r.stderr + r.stdout
    assert (env["stub"] / "head").read_text().strip() == NEW


def test_unknown_channel_word_is_ignored(env):
    _channel(env, "v1", ["v1", "v1.1.0"], ["v1", "v1.1.0"], ["v1", "v1.1.0"])
    _portal(env, channel="refs/heads/evil")
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert not (env["state"] / "channel").exists()
    assert "evil" not in _calls(env)


def test_next_version_counts_patches_as_integers(tmp_path):
    script = REPO_ROOT / "scripts" / "next-version.sh"
    g = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(tmp_path)]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "VERSION").write_text("1.1\n")
    subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "a"], check=True)

    def nxt():
        return subprocess.run(["bash", str(script)], cwd=tmp_path, capture_output=True, text=True,
                              check=True).stdout.strip()

    assert nxt() == "v1.1.0"
    for t in ("v1.1.0", "v1.1.9", "v1.0.30"):
        subprocess.run(g + ["tag", t], check=True)
    assert nxt() == "", "a commit that already has a release gets no second one"
    subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "b"], check=True)
    assert nxt() == "v1.1.10"
    (tmp_path / "VERSION").write_text("1.2\n")
    assert nxt() == "v1.2.0"


# ---------------------------------------------------------------------------
# Release signatures (node-26#17, server-26#318)
# ---------------------------------------------------------------------------

def _enforce(env, signed):
    env["signers"].write_text('drb-release namespaces="git" ssh-ed25519 AAAA\n')
    (env["stub"] / "signed").write_text("".join(t + "\n" for t in signed))


def test_without_a_signers_file_it_warns_and_still_updates(env):
    _channel(env, "v1", ["v1", "v1.1.0", "v1.1.4"], ["v1", "v1.1.4"], ["v1.1.0"])
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "release signatures NOT checked" in r.stdout
    assert (env["stub"] / "head").read_text().strip() == NEW


def test_signed_release_tag_installs(env):
    _channel(env, "v1", ["v1", "v1.1.0", "v1.1.4"], ["v1", "v1.1.4"], ["v1.1.0"])
    _enforce(env, ["v1"])
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "NOT checked" not in r.stdout
    assert (env["stub"] / "head").read_text().strip() == NEW


def test_unsigned_release_tag_is_never_checked_out(env):
    """A compromised git host can move v1, but it cannot sign it."""
    (env["stub"] / "response").write_text(json.dumps({"update_requested_at": "2026-11-22T00:00:00Z"}))
    _channel(env, "v1", ["v1", "v1.1.0", "v1.1.4"], ["v1", "v1.1.4"], ["v1.1.0"])
    _enforce(env, [])
    r = _run(env)
    assert r.returncode != 0
    assert "not signed" in r.stderr
    assert (env["stub"] / "head").read_text().strip() == OLD
    assert not _pulled(_calls(env))


def test_branch_ref_is_refused_once_signing_is_on(env):
    (env["stub"] / "response").write_text(json.dumps({"update_requested_at": "2026-11-22T00:00:00Z"}))
    env["conf"].write_text(env["conf"].read_text().replace("REF=v1", "REF=main"))
    _enforce(env, ["main"])   # even a name in the signed list: a branch is not a tag
    r = _run(env)
    assert r.returncode != 0
    assert (env["stub"] / "head").read_text().strip() == OLD


def test_latest_skips_an_unsigned_newer_tag_for_the_newest_signed_one(env):
    # An attacker-pushed v9.9.9 must neither install nor block real releases.
    _channel(env, "latest", ["v1.1.9", "v1.1.10", "v9.9.9"], ["v1.1.10"], ["v1.1.9"])
    _enforce(env, ["v1.1.9", "v1.1.10"])
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "skipping unsigned or untrusted tag v9.9.9" in r.stderr
    assert "release 1.1.10 on latest" in r.stdout


def _requested(env):
    (env["stub"] / "response").write_text(json.dumps({"update_requested_at": "2026-11-22T00:00:00Z"}))


def test_old_signed_tag_relabelled_as_newer_is_refused(env):
    """The signature covers the tag object, not the ref name it is served under."""
    _channel(env, "latest", ["v1.1.9", "v9.9.9"], ["v9.9.9"], ["v1.1.9"])
    _enforce(env, ["v1.1.9", "v9.9.9"])
    env["env"]["STUB_TAG_HEADER"] = "v1.0.0"
    r = _run(env)
    assert "release 9.9.9" not in r.stdout
    assert (env["stub"] / "head").read_text().strip() == OLD


def test_signed_rollback_is_refused(env):
    _requested(env)
    _channel(env, "v1", ["v1"], ["v1"], [])
    _enforce(env, ["v1"])
    env["env"]["STUB_NOT_AHEAD"] = "1"
    r = _run(env)
    assert r.returncode != 0
    assert "refusing a rollback" in r.stderr
    assert not _pulled(_calls(env))


def test_old_git_never_enforces_so_the_node_is_not_stranded(env):
    """git < 2.34 cannot verify SSH signatures: enforcing would block every update forever."""
    _requested(env)
    _channel(env, "v1", ["v1"], ["v1"], [])
    _enforce(env, [])          # nothing verifies, as on a real old git
    env["required"].write_text("")
    env["env"]["STUB_GIT_VERSION"] = "2.30.2"
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "too old to verify release signatures" in r.stdout
    assert (env["stub"] / "head").read_text().strip() == NEW


def test_deleted_signers_file_fails_closed_once_required(env):
    _requested(env)
    _channel(env, "v1", ["v1"], ["v1"], [])
    (env["stub"] / "signed").write_text("v1\n")
    env["required"].write_text("")   # marker present, signers file gone
    r = _run(env)
    assert r.returncode != 0
    assert (env["stub"] / "head").read_text().strip() == OLD


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")
def test_tag_is_signed_against_real_git(tmp_path):
    """The stubs above trust verify-tag; this checks the real git invocation."""
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    other = tmp_path / "o"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    signers = tmp_path / "signers"
    signers.write_text(f'drb-release namespaces="git" {(key.with_suffix(".pub")).read_text()}')
    repo = tmp_path / "r"
    g = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "gpg.format=ssh"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "a"], check=True)
    subprocess.run(g + ["-c", f"user.signingkey={key}", "tag", "-s", "-m", "ok", "good"], check=True)
    subprocess.run(g + ["-c", f"user.signingkey={other}", "tag", "-s", "-m", "x", "forged"], check=True)
    subprocess.run(g + ["tag", "-a", "-m", "u", "plain"], check=True)
    subprocess.run(g + ["tag", "light"], check=True)
    good_obj = subprocess.run(g + ["rev-parse", "refs/tags/good"], capture_output=True, text=True).stdout.strip()
    subprocess.run(g + ["update-ref", "refs/tags/relabelled", good_obj], check=True)

    funcs = "".join(SCRIPT.read_text().splitlines(keepends=True)[:-1])   # drop `main "$@"`

    def signed(tag):
        r = subprocess.run(["bash", "-c", funcs + f'\ncd "{repo}"; tag_is_signed {tag}'],
                           env={**os.environ, "DRB_UPDATER_SIGNERS": str(signers)})
        return r.returncode == 0
    assert signed("good")
    assert not signed("forged")
    assert not signed("plain")
    assert not signed("light")
    assert not signed("relabelled")   # a real signature, served under the wrong name


def test_checkin_reports_host_facts_for_the_signing_rollout(env):
    env["env"]["STUB_GIT_VERSION"] = "2.30.2"
    assert _run(env).returncode == 0
    calls = _calls(env)
    assert '"signing":"unsigned_old_git"' in calls
    assert '"git_version":"2.30.2"' in calls

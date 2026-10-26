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
  rev-parse)
    if [ "$2" = HEAD ]; then cat "$STUB/head"
    elif [ "$2" = --verify ]; then echo "$STUB_TREE"   # tree hash of <commit>:<dir>
    else echo "$STUB_NEW"; fi ;;
  remote) echo "$STUB_ORIGIN" ;;
  fetch) exit 0 ;;
  show-ref) [ "$4" = "refs/tags/v1" ] ;;
  cat-file) [ -z "${STUB_NO_PIN:-}" ] ;;   # target has scripts/image-tags.sh
  show) case "$2" in
          *:scripts/image-tags.sh) cat "$STUB_REPO/scripts/image-tags.sh" ;;
          *:docker-compose.yml) cat "$STUB_REPO/docker-compose.yml" ;;
        esac ;;
  checkout) echo "${@: -1}" > "$STUB/head" ;;
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
    })
    return {"env": e, "stub": stub, "inst": inst, "conf": conf, "state": tmp_path / "state"}


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
    assert "fetch" not in calls


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

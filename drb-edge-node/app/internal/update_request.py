"""
Hand an MQTT `node_update` command to the host-level updater (node-26#15).

The edge-node container has no business updating its own host: it writes a
sentinel file into /configs (bind-mounted from <install dir>/configs), and the
host's systemd drb-update.path unit — installed by install.sh — triggers
drb-update.service, which runs Client/scripts/drb-update.sh. The host treats
the file as a bare "please update" flag and never reads its content as input,
so nothing C2 sends can pick the ref, image or command that runs.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.internal.logger import logger

SENTINEL_NAME = "update-requested"


def sentinel_path() -> Path:
    return Path(settings.config_path) / SENTINEL_NAME


def request_host_update() -> bool:
    """Drop the sentinel. Returns False if it could not be written."""
    path = sentinel_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(SENTINEL_NAME + ".tmp")
        tmp.write_text(json.dumps({"requested_at": datetime.now(timezone.utc).isoformat()}))
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"Node update requested but the sentinel could not be written ({e}).")
        return False
    logger.info(
        f"Node update requested — handed to the host updater via {path}. If nothing happens within a minute, "
        "this node predates the updater: re-run install.sh on it once."
    )
    return True

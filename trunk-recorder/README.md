# Trunk Recorder on a DRB node (node-26#45)

Opt-in alternative to the OP25 recorder. On a node, set both in `.env`, then
`docker compose up -d`:

    RECORDER_BACKEND=trunk-recorder
    COMPOSE_PROFILES=trunk-recorder

Set both back (`RECORDER_BACKEND=op25`, remove `COMPOSE_PROFILES`) to return
to OP25.

- `run.sh`: supervisor that replaces the image's CMD. It restarts TR when the
  edge-node rewrites `configs/trunk-recorder/config.json`, and writes
  `drb-status.json` for the dashboard health check.
- `drb-upload.sh`: TR's `uploadScript`. It copies each finished call (WAV +
  JSON) into `recordings/tr-inbox`, where the edge-node uploads it through the
  normal spool.

## License (GPL-3.0)

Trunk Recorder is GPL-3.0. DRB runs the unmodified upstream image
`robotastic/trunk-recorder:5.2.1` (digest pinned in `docker-compose.yml`) as a
separate program. It talks to DRB only through files, a script argument list
and a local UDP socket. That makes it mere aggregation, the same position we
already hold for OP25.

Anyone we distribute a node image or device to must still be able to get the
corresponding source. Upstream source for this exact version is at
<https://github.com/TrunkRecorder/trunk-recorder/tree/v5.2.1>. If we ever ship
the TR image ourselves (mirrored to our registry, preinstalled on a sold
device), we must also offer that source directly, in writing, for three years,
or ship it alongside. Pointing to upstream alone is not enough at that point.
Any patch to TR itself makes DRB's copy a modified version, which must then be
published under GPL-3.0.

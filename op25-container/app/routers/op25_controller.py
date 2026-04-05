from fastapi import HTTPException, APIRouter
import subprocess
import os
import signal
import json
from models import ConfigGenerator, DecodeMode, ChannelConfig, DeviceConfig, TrunkingConfig, TrunkingChannelConfig, TerminalConfig, MetadataConfig, MetadataStreamConfig
from internal.logger import create_logger
from internal.op25_config_utls import save_talkgroup_tags, save_whitelist, del_none_in_dict, get_current_system_from_config
from internal.liquidsoap_config_utils import generate_liquid_script

LOGGER = create_logger(__name__)

op25_process = None
OP25_PATH = "/op25/op25/gr-op25_repeater/apps/"
OP25_SCRIPT = "run_multi-rx_service.sh"

def create_op25_router():
    router = APIRouter()

    @router.post("/start")
    async def start_op25():
        global op25_process
        if op25_process is None:
            try:
                op25_process = subprocess.Popen(os.path.join(OP25_PATH, OP25_SCRIPT), shell=True, preexec_fn=os.setsid, cwd=OP25_PATH)
                LOGGER.debug(op25_process)
                return {"status": "OP25 started"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        else:
            return {"status": "OP25 already running"}

    @router.post("/stop")
    async def stop_op25():
        global op25_process
        if op25_process is not None:
            try:
                os.killpg(os.getpgid(op25_process.pid), signal.SIGTERM)
                op25_process = None
                return {"status": "OP25 stopped"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        else:
            return {"status": "OP25 is not running"}

    @router.get("/status")
    async def get_status():
        return {"status": "running" if op25_process else "stopped"}

    @router.post("/generate-config")
    async def generate_config(generator: ConfigGenerator):
        try:
            if generator.type == DecodeMode.P25:
                channels = [ChannelConfig(
                    name=generator.systemName,
                    trunking_sysname=generator.systemName,
                    enable_analog="off",
                    demod_type="cqpsk",
                    cqpsk_tracking=True,
                    filter_type="rc",
                    meta_stream_name="stream_0"
                )]
                devices = [DeviceConfig()]
                save_talkgroup_tags(generator.tags)
                save_whitelist(generator.whitelist)
                trunking = TrunkingConfig(
                    module="tk_p25.py",
                    chans=[TrunkingChannelConfig(
                        sysname=generator.systemName,
                        control_channel_list=','.join(generator.channels),
                        tagsFile="/configs/active.cfg.tags.tsv",
                        whitelist="/configs/active.cfg.whitelist.tsv"
                    )]
                )

                metadata = MetadataConfig(
                    streams=[
                        MetadataStreamConfig(
                            stream_name="stream_0",
                            icecastServerAddress = f"{generator.icecastConfig.icecast_host}:{generator.icecastConfig.icecast_port}",
                            icecastMountpoint = generator.icecastConfig.icecast_mountpoint,
                            icecastPass = generator.icecastConfig.icecast_password
                        )
                    ]
                )

                # Generate the op25.liq file
                generate_liquid_script(generator.icecastConfig)


                terminal = TerminalConfig()

                config_dict = {
                    "channels": [channel.dict() for channel in channels],
                    "devices": [device.dict() for device in devices],
                    "trunking": trunking.dict(),
                    "metadata": metadata.dict(),
                    "terminal": terminal.dict()
                }

            elif generator.type == DecodeMode.ANALOG:
                generator = generator.config
                channels = [ChannelConfig(
                    channelName=generator.systemName,
                    enableAnalog="on",
                    demodType="fsk4",
                    frequency=generator.frequency,
                    filterType="widepulse",
                    nbfmSquelch=generator.nbfmSquelch
                )]
                devices = [DeviceConfig(gain="LNA:32")]

                config_dict = {
                    "channels": [channel.dict() for channel in channels],
                    "devices": [device.dict() for device in devices]
                }

            else:
                raise HTTPException(status_code=400, detail="Invalid configuration type. Must be 'p25' or 'nbfm'.")

            with open('/configs/active.cfg.json', 'w') as f:
                json.dump(del_none_in_dict(config_dict), f, indent=2)

            return {"message": "Config exported to '/configs/active.cfg.json'"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return router

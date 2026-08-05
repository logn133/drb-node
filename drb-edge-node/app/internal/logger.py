import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# The metadata watcher polls the OP25 terminal twice a second and httpx logs
# every one of those requests at INFO ("HTTP Request: POST http://... 200 OK").
# That is ~170k lines/day of pure noise which buries real events and makes field
# log-reading useless. WARNING keeps genuine transport failures visible.
# httpcore is the transport layer underneath httpx and is just as chatty.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("drb-edge-node")

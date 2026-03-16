import argparse

from loguru import logger
import sounddevice as sd

from ..common.constants import AUDIO_UDP_PORT
from ..common.logging import setup_logging
from .server import IntercomServer
from ..common.devices import format_devices, list_devices

def main() -> int:
    parser = argparse.ArgumentParser(prog="py-intercom-server")
    parser.add_argument("--bind-ip", default="0.0.0.0")
    parser.add_argument("--output-device", type=int, default=None)
    parser.add_argument("--return-enabled", action="store_true")
    parser.add_argument("--return-input-device", type=int, default=None)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--all-devices", action="store_true")
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    setup_logging(bool(args.debug))

    if args.list_devices:
        if args.all_devices:
            print(sd.query_devices())
        else:
            print(format_devices(list_devices(hostapi_substring="WASAPI")))
        return 0

    if args.gui:
        from .gui import run_gui

        return run_gui(port=int(AUDIO_UDP_PORT), minimized=bool(args.minimized))

    if args.debug:
        logger.info("starting server")
    srv = IntercomServer(
        bind_ip=args.bind_ip,
        port=int(AUDIO_UDP_PORT),
        output_device=args.output_device,
        return_input_device=args.return_input_device,
        return_enabled=bool(args.return_enabled),
    )
    srv.run_forever()
    return 0

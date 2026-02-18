import argparse
import random
import uuid
import zlib

import sounddevice as sd
from loguru import logger

from .client import ClientConfig, IntercomClient
from ..common.logging import setup_logging
from ..common.devices import format_devices, list_devices

def main() -> int:
    parser = argparse.ArgumentParser(prog="py-intercom-client")
    parser.add_argument("--server-ip", default=None)
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--client-uuid", default=None)
    parser.add_argument("--name", default="")
    parser.add_argument("--input-device", type=int, default=None)
    parser.add_argument("--output-device", type=int, default=None)
    parser.add_argument("--input-gain-db", type=float, default=0.0)
    parser.add_argument("--output-gain-db", type=float, default=0.0)
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

        server_ip = args.server_ip or ""
        input_device = args.input_device if args.input_device is not None else -1
        output_device = args.output_device if args.output_device is not None else -1

        return run_gui(
            server_ip=server_ip,
            server_port=5000,
            input_device=input_device,
            output_device=output_device,
            minimized=bool(args.minimized),
        )

    if args.server_ip is None:
        parser.error("--server-ip is required unless --list-devices is set")

    client_uuid = str(args.client_uuid or "").strip()
    if not client_uuid:
        if args.client_id is not None:
            client_uuid = f"cid:{int(args.client_id) & 0xFFFFFFFF}"
        else:
            client_uuid = str(uuid.uuid4())

    client_id = args.client_id
    if client_id is None:
        try:
            client_id = int(zlib.crc32(client_uuid.encode("utf-8")) & 0xFFFFFFFF)
        except Exception:
            client_id = random.getrandbits(32)

    cfg = ClientConfig(
        server_ip=args.server_ip,
        server_port=5000,
        client_uuid=client_uuid,
        name=str(args.name or ""),
        input_device=args.input_device,
        output_device=args.output_device,
        input_gain_db=args.input_gain_db,
        output_gain_db=args.output_gain_db,
    )

    if args.debug:
        logger.info("starting client {} -> {}:{}", client_id, cfg.server_ip, cfg.server_port)
    cli = IntercomClient(client_id=client_id, config=cfg)
    cli.run_forever()
    return 0

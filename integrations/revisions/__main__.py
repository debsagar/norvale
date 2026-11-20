"""Command-line entry point. Deployment settings live in JSON and secrets in env."""

import argparse
import json
import os
from pathlib import Path

from . import Bridge
from .receivers import ERPNext, Odoo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("provision-erp")
    publish = commands.add_parser("publish")
    publish.add_argument("native_id", type=int)
    release = commands.add_parser("release")
    release.add_argument("base_id", type=int)
    release.add_argument("work_id")
    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("work_id")
    dispatch.add_argument("quantity")
    dispatch.add_argument("hold_location_id", type=int)
    for command in (publish, release, dispatch):
        command.add_argument("--on", required=True)
    args = vars(parser.parse_args())
    config = json.loads(args.pop("config").read_text())
    command = args.pop("command")

    from norvale.loader.erp_client import ErpClient

    erp = ERPNext(ErpClient(**config["erp"], token=os.environ["REVISIONS_ERP_TOKEN"]))
    if command == "provision-erp":
        erp.provision(scoped=config["bridge"].get("scoped_receipts", False))
        return

    from integrations.mes.client import Mes
    from integrations.plm import PLM

    plm = PLM(**config["plm"], token=os.environ["REVISIONS_PLM_TOKEN"])
    mes = Mes(**config["mes"], password=os.environ["REVISIONS_MES_PASSWORD"])
    bridge = Bridge(plm, erp, Odoo(mes), **config["bridge"])
    print(json.dumps(getattr(bridge, command)(**args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

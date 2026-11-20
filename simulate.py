"""Run one versioned native company scenario from a clean checkout."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def load_scenario(path):
    cfg = json.loads(path.read_text())
    if not isinstance(cfg, dict) or set(cfg) != {"version", "days", "seed", "opening_orders"}:
        raise ValueError("Expected version, days, seed and opening_orders")
    if type(cfg["version"]) is not int or cfg["version"] != 1:
        raise ValueError("Unsupported scenario version")
    if type(cfg["days"]) is not int or not 1 <= cfg["days"] <= 365 or type(cfg["seed"]) is not int:
        raise ValueError("Expected 1..365 days and an integer seed")
    from walkthrough.engine import EnterpriseSimV4
    catalog = EnterpriseSimV4(seed=cfg["seed"], days=30, mode="dry-run")
    items = {sku["sku_id"] for sku in catalog.skus}
    if not isinstance(cfg["opening_orders"], list):
        raise ValueError("opening_orders must be a list")
    for order in cfg["opening_orders"]:
        if not isinstance(order, dict) or set(order) != {"customer", "item", "quantity", "due_day"}:
            raise ValueError("Invalid opening order fields")
        if order["customer"] not in catalog.accounts or order["item"] not in items:
            raise ValueError("Unknown customer or item in opening order")
        if type(order["quantity"]) is not int or order["quantity"] < 1:
            raise ValueError("Opening quantity must be a positive integer")
        if type(order["due_day"]) is not int or not 0 <= order["due_day"] <= 365:
            raise ValueError("Opening due_day must be an integer from 0 to 365")
    return cfg


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", type=Path, default=ROOT / "scenarios/norvale-q1.json")
    p.add_argument("--run", type=Path, required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--services", choices=("status", "stop", "start"), help="Operate existing containers for this run")
    mode.add_argument("--plan", action="store_true", help="Validate inputs and print the setup without starting services")
    a = p.parse_args()
    runtime = a.run.resolve()
    try:
        if runtime.is_relative_to(ROOT) or ROOT.is_relative_to(runtime):
            raise ValueError("Choose a private run directory outside the checkout")
        if a.services:
            from integrations.company_run.services import manage
            launcher_run = (runtime / "scenario.json").is_file() and (runtime / "revision").is_file()
            commissioned = (runtime / "services.json").is_file() and (runtime / "business/config.json").is_file()
            if not (launcher_run or commissioned):
                raise ValueError("Choose an existing simulation run")
            with (runtime / "simulate.lock").open("a") as lock:
                if a.services != "status":
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                rows = manage(runtime, a.services)
                if a.services == "status":
                    print(json.dumps(rows, indent=2))
            return
        cfg = load_scenario(a.scenario)
        if a.plan:
            print(json.dumps(dict(scenario=cfg, run=str(runtime), mode="live", archive=str(runtime / "lake")), indent=2))
            return
        if os.environ.get("NORVALE_OFFLINE") == "1" or not os.environ.get("TYPESAFE_API_KEY") or not any(
            os.environ.get(k) for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY")
        ):
            raise ValueError("Live mode requires JEV and role-model credentials and NORVALE_OFFLINE unset")
        __import__("deltalake")
        __import__("pyarrow")
        os.umask(0o077)
        release = runtime / "release"
        fresh = not runtime.exists()
        if fresh:
            if not (Path(os.environ.get("NORVALE_CLOCK_LIB", "")) / "libfaketime.so.1").is_file():
                raise ValueError("Set NORVALE_CLOCK_LIB to the directory containing libfaketime.so.1")
            subprocess.run(["docker", "compose", "version"], check=True)
            runtime.mkdir(mode=0o700)
        elif not (runtime / "scenario.json").is_file():
            raise ValueError("Existing directory is not a commissioned simulation")
        lock = (runtime / "simulate.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not fresh:
            if not (runtime / "scenario.json").is_file() or load_scenario(runtime / "scenario.json") != cfg:
                raise ValueError("Run already exists with different or missing scenario")
            if not (runtime / "commissioned").is_file():
                raise ValueError("Commissioning was interrupted; inspect provision.log before resuming the same services")
        else:
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            release.mkdir()
            (runtime / "revision").write_text(revision + "\n")
            (runtime / "scenario.json").write_text(json.dumps(cfg, indent=2) + "\n")
            subprocess.run(["git", "archive", revision, "-o", str(runtime / "source.tar")], cwd=ROOT, check=True)
            subprocess.run(["tar", "-xf", str(runtime / "source.tar"), "-C", str(release)], check=True)
            shutil.copytree(release / "integrations/revisions/odoo_addons", release / "integrations/mes/addons", dirs_exist_ok=True)
            with (runtime / "provision.log").open("w") as log:
                subprocess.run(["bash", "integrations/company_run/provision.sh", str(runtime)],
                               cwd=release, env={**os.environ, "PATH":str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                                             "NORVALE_COMPANY_OPERATING":"1"},
                               check=True, stdout=log, stderr=subprocess.STDOUT)
            (runtime / "commissioned").touch()
        subprocess.run([sys.executable, "generate.py", "--run", str(runtime / "business")], cwd=release, check=True)
    except (OSError, ImportError, ValueError, subprocess.CalledProcessError) as exc:
        p.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

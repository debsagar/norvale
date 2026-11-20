"""Bring one run's software stack up from nothing, and down again. No manual steps.

up(): compose file -> accounts -> clock placed at load time -> db -> fresh ERPNext site -> all services
-> chat accounts. Endpoints (container IPs, loopback only) go to env/endpoints.json.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

from . import accounts, compose as compose_mod, docker
from .clock import Clock

TASK = Path(__file__).resolve().parents[2]
LIB = Path(os.environ.get("NORVALE_CLOCK_LIB", str(TASK / "env-cache/lib")))
BENCH_SETUP = (
    "ls -1 apps > sites/apps.txt; bench set-config -g db_host db; bench set-config -gp db_port 3306; "
    "bench set-config -g redis_cache redis://redis-cache:6379; bench set-config -g redis_queue redis://redis-queue:6379; "
    "bench set-config -g redis_socketio redis://redis-queue:6379; bench set-config -gp socketio_port 9000; "
    "bench new-site --mariadb-user-host-login-scope='%' --admin-password=admin --db-root-username=root "
    f"--db-root-password=admin --install-app erpnext --set-default {compose_mod.SITE}")


class Stack:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir).resolve()
        self.env = self.run_dir / "env"
        self.project = "nv11-" + self.run_dir.name.lower().replace("_", "-")
        self.file = self.env / "compose.json"
        self.clock = Clock(self.env)

    def compose(self, *args, **kw):
        return docker.compose(self.project, self.file, *args, **kw)

    def log(self, msg):
        with (self.env / "stack.log").open("a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")

    def up(self, identities, load_time, frontend_port=None):
        self.env.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(compose_mod.build(self.env, LIB, frontend_port), indent=1))
        sec = accounts.make_secrets(self.env, identities)
        accounts.write_mail_config(self.env, identities, sec)
        accounts.write_calendar_config(self.env, identities, sec)
        accounts.write_matrix_config(self.env, docker.IMAGES["matrix"])
        if not self.clock.file.exists():
            self.clock.start_at(load_time)
        self.log(f"clock placed; simulated now {self.clock.now().isoformat()}")
        self.compose("up", "-d", "db", "redis-cache", "redis-queue", capture_output=True)
        self._wait(lambda: docker.exec_(self.project, self.file, "db", "sh", "-ec",
                                        'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb --protocol=tcp -hdb -uroot -e "SELECT 1"',
                                        check=False, capture_output=True).returncode == 0, "database bootstrap login")
        if not (self.env / "site-created").exists():
            self.log("creating fresh ERPNext site (several minutes)")
            r = self.compose("run", "--rm", "--no-deps", "backend", "bash", "-c", BENCH_SETUP,
                             capture_output=True, text=True, check=False)
            (self.env / "new-site.log").write_text(r.stdout[-20000:] + r.stderr[-20000:])
            if r.returncode:
                raise RuntimeError("bench new-site failed; see env/new-site.log")
            (self.env / "site-created").touch()
        self.compose("up", "-d", capture_output=True)
        eps = self.endpoints()
        self._wait(lambda: self._http_ok(eps["erp"] + "/api/method/ping"), "ERPNext", 300)
        accounts.register_chat(self.env, eps["chat"], identities, sec)
        self.log("stack ready")
        return eps

    def endpoints(self):
        ip = lambda s: docker.container_ip(self.project, s)
        ports = json.loads(self.file.read_text())["services"]["frontend"].get("ports", [])
        erp = ("http://" + ports[0].rsplit(":", 1)[0] if ports
               else f"http://{ip('backend')}:8000")
        eps = {"erp": erp, "site": compose_mod.SITE,
               "mail": {"host": ip("mail"), "smtp": 25, "imap": 143},
               "calendar": f"http://{ip('calendar')}:5232", "chat": f"http://{ip('matrix')}:8008",
               "db": {"container_service": "db", "database": None}}
        (self.env / "endpoints.json").write_text(json.dumps(eps, indent=1))
        return eps

    def settle(self, timeout=120):
        """Wait until ERP workers and the mail queue are idle (codex T9.1 rule: settle before moving the clock)."""
        def idle():
            q = docker.exec_(self.project, self.file, "redis-queue", "sh", "-c",
                             "for k in $(redis-cli --scan --pattern 'rq:queue:*'); do redis-cli llen $k; done;"
                             "for k in $(redis-cli --scan --pattern 'rq:wip:*'); do redis-cli zcard $k; done",
                             capture_output=True, text=True, check=False).stdout.split()
            mailq = docker.exec_(self.project, self.file, "mail", "postqueue", "-j",
                                 capture_output=True, text=True, check=False).stdout.strip()
            return not mailq and all(x == "0" for x in q)
        self._wait(idle, "workers and mail queue", timeout, pause=0.5)

    def sql(self, query):
        """Read-only controller query (never exposed to agents). Returns rows as lists of strings."""
        db = self.db_name()
        r = docker.exec_(self.project, self.file, "db", "mariadb", "-uroot", "-padmin", "-N", "-B", db, "-e", query,
                         capture_output=True, text=True)
        return [line.split("\t") for line in r.stdout.splitlines()]

    def db_name(self):
        if not hasattr(self, "_db"):
            cfg = docker.exec_(self.project, self.file, "backend", "cat", f"sites/{compose_mod.SITE}/site_config.json",
                               capture_output=True, text=True).stdout
            self._db = json.loads(cfg)["db_name"]
        return self._db

    def bench(self, *args):
        return docker.exec_(self.project, self.file, "backend", "bench", "--site", compose_mod.SITE, *args,
                            capture_output=True, text=True, check=False)

    def down(self, keep_volumes=True):
        self.compose("down", *([] if keep_volumes else ["-v"]), capture_output=True)

    @staticmethod
    def _http_ok(url):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"X-Frappe-Site-Name": compose_mod.SITE}),
                                        timeout=5) as r:
                return r.status == 200
        except OSError:
            return False

    @staticmethod
    def _wait(cond, what, timeout=120, pause=1.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(pause)
        raise TimeoutError(f"{what} not ready after {timeout}s")

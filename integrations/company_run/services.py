"""Operate existing run containers using their Compose ownership labels."""
import json
import subprocess

from native.company import project


def manage(runtime, action):
    files = {
        "erp": runtime / "business/env/compose.json",
        "mes": runtime / "release/integrations/mes/compose.yaml",
        "plm": runtime / "release/integrations/plm/compose.yaml",
        "qms": runtime / "release/integrations/qms/compose.yml",
    }
    sources = runtime / "services.json"
    names = {"erp": project(runtime / "business")}
    if sources.is_file():
        cfg = json.loads(sources.read_text())
        names.update(mes=cfg["mes_project"], plm=cfg["plm_project"])
    env = runtime / "qms/docker.env"
    if env.is_file():
        qms = [line.partition("=")[2] for line in env.read_text().splitlines()
               if line.startswith("COMPOSE_PROJECT_NAME=")]
        if len(qms) != 1 or not qms[0]:
            raise ValueError("Missing or ambiguous commissioned QMS project")
        names["qms"] = qms[0]
    rows = []
    for system, file in files.items():
        if not file.is_file():
            continue
        command = ["docker", "ps", "-a", "--no-trunc", "--filter", "label=com.docker.compose.oneoff=False",
                   "--filter", f"label=com.docker.compose.project.config_files={file}"]
        if system in names:
            command += ["--filter", f"label=com.docker.compose.project={names[system]}"]
        # During initial MES/PLM installation the run-local Compose file may be
        # the only ownership evidence. Refuse competing projects on that file.
        fmt = '{{.ID}} {{.State}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}'
        records = [line.split() for line in subprocess.check_output(
            [*command, "--format", fmt], text=True).splitlines()]
        if len({row[2] for row in records}) > 1:
            raise ValueError(f"Ambiguous {system} projects for {file}")
        rows.extend(dict(id=i, state=state, project=p, service=s) for i, state, p, s in records)
    if action == "status":
        return rows
    infrastructure = {"db", "redis", "redis-cache", "redis-queue"}
    if not rows:
        raise ValueError("No existing containers found for this run on the current Docker daemon")
    tiers = (True, False) if action == "start" else (False, True)
    eligible = {"exited", "created"} if action == "start" else {"running", "restarting"}
    for dependencies in tiers:
        ids = [row["id"] for row in rows if row["state"] in eligible
               and (row["service"] in infrastructure) == dependencies]
        if ids:
            subprocess.run(["docker", action, *ids], check=True)
    return rows

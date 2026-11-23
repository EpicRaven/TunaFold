"""TunaFold - local PostgreSQL instance dashboard.  Run: python app.py  ->  http://127.0.0.1:8800"""
import itertools, json, os, re, shutil, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

BASE = Path(__file__).parent
CONFIG = BASE / "instances.json"
BACKUPS = BASE / "backups"
HOST, PORT = "127.0.0.1", 8800
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # never pop up a console
HISTORY, _ids = [], itertools.count(1)
LOCAL = {"localhost", "127.0.0.1", "::1"}

app = FastAPI(title="TunaFold")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ---------- security: localhost only, no cross-site requests ----------
@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
    origin = request.headers.get("origin")
    if host not in LOCAL or (origin and urlparse(origin).hostname not in LOCAL):
        return JSONResponse({"detail": "Local requests only."}, status_code=403)
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "pgdash":
        return JSONResponse({"detail": "Missing header."}, status_code=403)
    return await call_next(request)


@app.exception_handler(psycopg2.Error)
async def pg_error(_, exc):
    return JSONResponse({"detail": (exc.pgerror or str(exc)).strip()}, status_code=502)


# ---------- config ----------
def load():
    return json.loads(CONFIG.read_text("utf-8")) if CONFIG.exists() else {}


def save(d):
    CONFIG.write_text(json.dumps(d, indent=2), "utf-8")


def get(name):
    inst = load().get(name)
    if not inst:
        raise HTTPException(404, f"Unknown instance '{name}'.")
    return inst


class InstanceIn(BaseModel):
    name: str
    data_dir: str
    port: int = 5432
    host: str = "127.0.0.1"
    user: str = "postgres"
    password: str = ""
    bin_dir: str = ""      # folder holding pg_ctl, psql, initdb (blank = use PATH)
    log_file: str = ""     # blank = <data_dir>/server.log
    service: str = ""      # systemd unit; if set, start/stop/restart use systemctl


class CreateIn(InstanceIn):
    start: bool = True


def clean(d):
    d = {k: (v.strip() if isinstance(v, str) else v) for k, v in d.items()}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", d["name"]):
        raise HTTPException(400, "Name may only contain letters, digits, _ . -")
    if not d["data_dir"]:
        raise HTTPException(400, "Data directory is required.")
    if not 0 < d["port"] < 65536:
        raise HTTPException(400, "Port must be 1-65535.")
    d["data_dir"] = str(Path(d["data_dir"]).expanduser())
    return d


# ---------- command runner (subprocess, captured, silent) ----------
def run(args, title="", env=None, shell=False, timeout=120, record=True):
    """Run a command with no visible window. Output goes via temp files, so a
    daemonised server (pg_ctl start) can't keep our pipes open and hang us."""
    started, rc = time.time(), 0
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        extra = ""
        try:
            p = subprocess.Popen(args, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, shell=shell,
                                 env={**os.environ, **(env or {})}, creationflags=NOWIN)
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                rc, extra = -9, f"\n[killed after {timeout}s timeout]"
        except OSError as e:
            rc, extra = 127, f"{e}\n"
        fo.seek(0), fe.seek(0)
        out, err = fo.read().decode(errors="replace"), fe.read().decode(errors="replace") + extra
    cmd = args if isinstance(args, str) else subprocess.list2cmdline(args)
    return log_result(title or cmd, cmd, rc, out, err, time.time() - started, record)


def log_result(title, cmd, rc, out, err, dur=0.0, record=True):
    r = {"id": next(_ids), "title": title, "cmd": cmd, "rc": rc, "stdout": out, "stderr": err,
         "seconds": round(dur, 2), "time": datetime.now().strftime("%H:%M:%S")}
    if record:
        HISTORY.insert(0, r)
        del HISTORY[40:]
    return r


def exe(inst, tool):
    return shutil.which(tool, path=inst.get("bin_dir") or None) or tool


def qi(x):
    return '"' + x.replace('"', '""') + '"'


def psql(inst, sql, title, db="postgres"):
    env = {"PGPASSWORD": inst["password"]} if inst.get("password") else {}
    return run([exe(inst, "psql"), "-X", "-h", inst["host"], "-p", str(inst["port"]), "-U", inst["user"],
                "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql], title=title, env=env)


def is_running(inst):
    if inst.get("service"):
        return run(["systemctl", "is-active", "--quiet", inst["service"]], record=False)["rc"] == 0
    return run([exe(inst, "pg_ctl"), "status", "-D", inst["data_dir"]], record=False)["rc"] == 0


def ctl(inst, action):
    n = inst["name"]
    if inst.get("service"):
        return run(["systemctl", action, inst["service"]], title=f"{action} {n} (systemctl)")
    cmd = [exe(inst, "pg_ctl"), action, "-D", inst["data_dir"]]
    if action in ("start", "restart"):
        cmd += ["-o", f"-p {inst['port']}", "-l", inst.get("log_file") or str(Path(inst["data_dir"]) / "server.log")]
    if action in ("stop", "restart"):
        cmd += ["-m", "fast"]
    return run(cmd, title=f"{action} {n} (pg_ctl)")


def conn(inst, db="postgres"):
    return psycopg2.connect(host=inst["host"], port=inst["port"], user=inst["user"],
                            password=inst.get("password") or None, dbname=db, connect_timeout=3)


def query(inst, sql, params=None, db="postgres"):
    c = conn(inst, db)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        c.close()


# ---------- pages & instances ----------
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"app_name": "TunaFold"})


@app.get("/api/instances")
def list_instances():
    items = list(load().values())
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, up in zip(items, ex.map(is_running, items)):
            i["running"] = up
    return items


@app.post("/api/instances")
def register(body: InstanceIn):
    inst, data = clean(body.model_dump()), load()
    if inst["name"] in data:
        raise HTTPException(409, "An instance with that name already exists.")
    data[inst["name"]] = inst
    save(data)
    return {"ok": True}


@app.put("/api/instances/{name}")
def update(name: str, body: InstanceIn):
    data = load()
    get(name)
    inst = clean(body.model_dump())
    inst["name"] = name
    data[name] = inst
    save(data)
    return {"ok": True}


@app.post("/api/instances/create")
def create(body: CreateIn):
    d = body.model_dump()
    start = d.pop("start")
    inst = clean(d)
    if inst["name"] in load():
        raise HTTPException(409, "An instance with that name already exists.")
    if not inst["password"]:
        raise HTTPException(400, "Set a superuser password for the new instance.")
    dd = Path(inst["data_dir"])
    if dd.exists() and any(dd.iterdir()):
        raise HTTPException(400, "Data directory already exists and is not empty.")
    dd.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".pw") as f:
        f.write(inst["password"])
    try:
        r = run([exe(inst, "initdb"), "-D", str(dd), "-U", inst["user"], "--pwfile", f.name,
                 "-A", "scram-sha-256", "-E", "UTF8"], title=f"initdb {inst['name']}")
    finally:
        os.unlink(f.name)
    results = [r]
    if r["rc"] == 0:
        with open(dd / "postgresql.conf", "a", encoding="utf-8") as f:
            f.write(f"\nport = {inst['port']}\n")
        data = load()
        data[inst["name"]] = inst
        save(data)
        if start:
            results.append(ctl(inst, "start"))
    return {"ok": r["rc"] == 0, "results": results}


@app.post("/api/instances/{name}/delete")
def delete(name: str, body: dict):
    inst = get(name)
    if body.get("confirm") != name:
        raise HTTPException(400, "Confirmation text does not match the instance name.")
    results = []
    if is_running(inst):
        results.append(ctl(inst, "stop"))
        if is_running(inst):
            return {"ok": False, "results": results}
    if body.get("delete_data"):
        dd = Path(inst["data_dir"])
        if not (dd / "PG_VERSION").exists():
            raise HTTPException(400, "Refusing to delete: no PG_VERSION file, so this doesn't look like a data directory.")
        try:
            shutil.rmtree(dd)
            results.append(log_result(f"delete data of {name}", f"rmtree {dd}", 0, f"Removed {dd}\n", ""))
        except OSError as e:
            results.append(log_result(f"delete data of {name}", f"rmtree {dd}", 1, "", str(e)))
            return {"ok": False, "results": results}
    data = load()
    data.pop(name, None)
    save(data)
    return {"ok": True, "results": results}


@app.post("/api/instances/{name}/action")
def action(name: str, body: dict):
    if body.get("action") not in ("start", "stop", "restart"):
        raise HTTPException(400, "Unknown action.")
    return {"result": ctl(get(name), body["action"])}


@app.get("/api/instances/{name}/info")
def info(name: str):
    inst = get(name)
    out = {"running": is_running(inst)}
    if out["running"]:
        try:
            q = """SELECT version() AS version, to_char(pg_postmaster_start_time(),'YYYY-MM-DD HH24:MI') AS started,
              extract(epoch FROM now()-pg_postmaster_start_time())::bigint AS uptime,
              (SELECT count(*) FROM pg_database WHERE NOT datistemplate) AS databases,
              current_setting('max_connections') AS max_conn,
              count(*) AS total,
              count(*) FILTER (WHERE state='active') AS active,
              count(*) FILTER (WHERE state='idle') AS idle,
              count(*) FILTER (WHERE state LIKE 'idle in%') AS idle_tx
              FROM pg_stat_activity WHERE backend_type='client backend'"""
            out.update(query(inst, q)[0])
            out["version"] = out["version"].split(" on ")[0]
        except psycopg2.Error as e:
            out["db_error"] = (e.pgerror or str(e)).strip()
    return out


# ---------- databases & connections ----------
@app.get("/api/instances/{name}/databases")
def databases(name: str):
    return query(get(name), """SELECT d.datname AS name, pg_get_userbyid(d.datdba) AS owner,
        pg_database_size(d.datname) AS size, pg_size_pretty(pg_database_size(d.datname)) AS size_pretty,
        (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) AS conns
        FROM pg_database d WHERE NOT d.datistemplate ORDER BY 1""")


@app.get("/api/instances/{name}/roles")
def roles(name: str):
    return [r["rolname"] for r in query(get(name), "SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1")]


@app.post("/api/instances/{name}/owner")
def owner(name: str, body: dict):
    inst = get(name)
    sql = f"ALTER DATABASE {qi(body['db'])} OWNER TO {qi(body['owner'])};"
    return {"result": psql(inst, sql, f"change owner of {body['db']} to {body['owner']}")}


@app.get("/api/instances/{name}/connections")
def connections(name: str):
    return query(get(name), r"""SELECT pid, usename, datname, state, application_name,
        COALESCE(client_addr::text,'local') AS client, to_char(backend_start,'HH24:MI:SS') AS since,
        left(regexp_replace(query,'\s+',' ','g'),90) AS query, pid = pg_backend_pid() AS me
        FROM pg_stat_activity WHERE backend_type='client backend' ORDER BY backend_start""")


@app.post("/api/instances/{name}/backend")
def backend(name: str, body: dict):
    fn = {"cancel": "pg_cancel_backend", "terminate": "pg_terminate_backend"}.get(body.get("mode"))
    if not fn:
        raise HTTPException(400, "Unknown mode.")
    pid = int(body["pid"])
    return {"result": psql(get(name), f"SELECT {fn}({pid});", f"{body['mode']} backend {pid}")}


# ---------- logs, backup, custom command ----------
@app.get("/api/instances/{name}/logs")
def logs(name: str, lines: int = 300):
    inst = get(name)
    dd = Path(inst["data_dir"])
    cands = [Path(inst["log_file"] or dd / "server.log")]
    if (dd / "log").is_dir():
        cands += sorted((dd / "log").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    path = next((p for p in cands if p.is_file()), None)
    if not path:
        return {"path": str(cands[0]), "text": "(no log file found yet)"}
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 262144))
        data = f.read().decode(errors="replace")
    return {"path": str(path), "text": "\n".join(data.splitlines()[-lines:])}


@app.post("/api/instances/{name}/backup")
def backup(name: str, body: dict):
    inst, db = get(name), body.get("db") or "postgres"
    BACKUPS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    env = {"PGPASSWORD": inst["password"]} if inst.get("password") else {}
    common = ["-h", inst["host"], "-p", str(inst["port"]), "-U", inst["user"]]
    if db == "__all__":
        out = BACKUPS / f"{name}_all_{stamp}.sql"
        r = run([exe(inst, "pg_dumpall"), *common, "-f", str(out)], title=f"pg_dumpall {name}", env=env, timeout=1800)
    else:
        out = BACKUPS / f"{name}_{db}_{stamp}.dump"
        r = run([exe(inst, "pg_dump"), *common, "-F", "c", "-f", str(out), db], title=f"pg_dump {db}", env=env, timeout=1800)
    if r["rc"] == 0 and out.exists():
        r["stdout"] += f"Backup written to {out} ({out.stat().st_size:,} bytes)\n"
    return {"result": r}


@app.post("/api/run")
def run_custom(body: dict):
    cmd = (body.get("command") or "").strip()
    if not cmd:
        raise HTTPException(400, "Enter a command.")
    return {"result": run(cmd, title="custom command", shell=True, timeout=300)}


@app.get("/api/history")
def history():
    return HISTORY


@app.delete("/api/history")
def clear_history():
    HISTORY.clear()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)

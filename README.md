# TunaFold

A small local dashboard for managing PostgreSQL instances on your own machine. Create, start, stop and back up multiple Postgres clusters from one browser tab, without remembering `pg_ctl` flags.

Built with FastAPI and a single-page UI. It binds to `127.0.0.1` only.

## Features

- Create a new instance (runs `initdb`) or register an existing cluster
- Start, stop and restart instances via `pg_ctl` (or `systemctl` for system services)
- Browse databases, roles and live connections per instance
- Cancel or terminate backend connections
- Change a database owner
- Backups with `pg_dump` (custom format) or `pg_dumpall`
- View server logs
- Run one-off shell commands and review command history

## Requirements

- Python 3.10+ (`python3-venv` on Debian/Ubuntu)
- PostgreSQL server binaries (`initdb`, `pg_ctl`, `psql`, `pg_dump`)
    - Debian/Ubuntu: `sudo apt install postgresql` (binaries land in `/usr/lib/postgresql/<version>/bin`)
    - Fedora: `sudo dnf install postgresql-server`
    - Arch: `sudo pacman -S postgresql`

## Project layout

```
TunaFold/
├── app.py              # FastAPI backend
├── requirements.txt
├── templates/
│   └── index.html      # UI
├── t-fold              # launcher script
├── install-t-fold.sh   # one-time setup for the t-fold command
├── instances.json      # created at runtime (contains passwords, git-ignored)
└── backups/            # created at runtime (git-ignored)
```

`app.py` expects the UI at `templates/index.html`. If your `index.html` sits at the top level, the `t-fold` launcher symlinks it into `templates/` automatically.

## Quick start

```
git clone <your-repo-url> TunaFold
cd TunaFold
bash install-t-fold.sh
```

Open a new terminal, then from any directory:

```
t-fold
```

This opens [http://127.0.0.1:8800](http://127.0.0.1:8800) and starts the server. Press Ctrl+C to stop it.

### What the launcher does

- Resolves the project folder from its own location, so it works from any directory
- Creates a private virtualenv (`.venv-run`) and installs `requirements.txt` on first run
- Opens the dashboard in your browser
- Runs `app.py`

`install-t-fold.sh` symlinks `t-fold` into `~/.local/bin` and adds that folder to your `PATH` in `~/.bashrc` if needed. If you use zsh, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` instead.

### Run without the launcher

```
python3 -m venv .venv-run
.venv-run/bin/pip install -r requirements.txt
.venv-run/bin/python app.py
```

## Creating an instance

In the dashboard, choose **Create** and fill in:

| Field | What to enter |
| --- | --- |
| Name | Letters, digits, `_`, `.`, `-` |
| Data directory | An empty or non-existent folder you can write to, e.g. `/home/you/pgdata/dev`. Avoid `/var/lib/postgresql/...` (system-owned) and keep it outside the repo |
| Port | `5433` or another free port if the system Postgres already uses 5432 |
| Password | Required. Becomes the superuser password |
| Bin dir | Folder containing `initdb` and `pg_ctl`, e.g. `/usr/lib/postgresql/18/bin`. Leave blank if they are on your `PATH` (`which initdb`) |

To register an existing cluster, use **Add** and point it at a folder that already contains `PG_VERSION` and `postgresql.conf`.

Then connect from any client at `127.0.0.1`, your chosen port, user `postgres`, and the password you set.

## Troubleshooting

**`No such file or directory: 'initdb'`** The server binaries are not on your `PATH`. Install PostgreSQL (see Requirements) and set **Bin dir** to the folder containing them.

**`could not create lock file "/var/run/postgresql/.s.PGSQL.5432.lock": Permission denied`** Debian's default socket directory is root-owned. TunaFold avoids this by starting every instance with `-k <tmpdir>` (normally `/tmp`), so you should not see it. If you start an instance manually, add `-k /tmp` to the `-o` options.

**`Address already in use`** Another Postgres owns that port. Pick a different port for the instance, or stop the system service: `sudo systemctl disable --now postgresql`.

**`500 Internal Server Error`** **with** **`TemplateNotFound: 'index.html'`** Put `index.html` in `templates/` (`mkdir templates && mv index.html templates/`), or start via `t-fold`, which links it for you.

**`permission denied: t-fold`** Run `chmod +x t-fold`.

**VS Code or another client shows** **`Connection refused`** **on 5432** No server is listening there. Start the instance in TunaFold and make sure the client uses the same port.

**Server won't start and the reason is unclear** Check the log: `tail -20 <data_dir>/server.log`.

## Security notes

- The server only accepts requests addressed to `localhost`, `127.0.0.1` or `::1`, and rejects cross-origin requests.
- `instances.json` stores instance passwords in plain text. It is listed in `.gitignore`. Never commit it.
- The dashboard can run arbitrary shell commands. Do not expose port 8800 beyond localhost (for example, through a reverse proxy or `--host 0.0.0.0`).

## Platform notes

Developed and tested on Debian-based Linux. The Python code also handles Windows (no console popups, no `-k` socket flag), but the `t-fold` launcher is a bash script, so on Windows run `python app.py` directly.

## License

MIT

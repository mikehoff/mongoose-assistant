# Mongoose

A household assistant that runs entirely on your own Mac. It reads only what you let it read, talks to
you from your phone, controls your home through Home Assistant, and does real work on your say-so,
without sending your life to anyone else's servers.

Codename `sillygoose`. Python 3.12, FastAPI, SQLite, Ollama. Built for an Apple Silicon Mac mini.

> **Status: beta.** Mongoose runs a real household every day and has over 3,000 automated tests.
> It is still one person's project: expect rough edges, and read
> [What it will not do](#what-it-will-not-do) before connecting anything that matters.

---

## Get started

**Double-click `Install Mongoose.command`.**

A window shows each step as it happens. Your browser opens straight away with a live progress page
(it never sits on a blank spinner: every wait has a timeout and a visible failure state). When it is
done you get a QR code: scan it with your phone's camera and Mongoose is on your phone too, as an app
you can save to your home screen.

- First run downloads the local model (a few GB). After that, starting is instant.
- No Terminal needed. If macOS warns the file is from an unidentified developer, double-click
  `Trust Mongoose.command` once (the builds are not notarised), or right-click and choose **Open**.
- Already installed? `Start Mongoose.command`, `Stop Mongoose.command` and `Mongoose Status.command`
  do what they say.

Prefer the command line:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[test,devices]'
cp .env.example .env
python -m mongoose.cli init-db
./scripts/pull_models.sh
mongoose serve          # https://localhost:8765 (self-signed certificate on first run)
mongoose doctor         # readiness check: models, Home Assistant, connectors
```

---

## Two packages

Mongoose ships as two repositories that install into the same `mongoose` import namespace:

| Package | Holds |
|---|---|
| **mongoose-core** | RTM management, banking and accounting providers, unified search, document generation, the WordPress portal plugin and its API, the site profile, the database and settings layer. Runs on its own. |
| **mongoose-assistant** (this repo) | The household assistant: chat and the tool loop, Cockpit, devices and guests, home control, voice, notifications, the brief, knowledge import, installer and updater. Depends on mongoose-core at the same version. |

Install both (`pip install mongoose-core mongoose-assistant`, or from local checkouts:
`pip install ./mongoose-core ./mongoose-assistant`). For development, install both editable with
`pip install -e ../mongoose-core -e . --config-settings editable_mode=compat`.

## Your installation's identity: the site profile

Everything that makes an install *yours* (owner name, site domain, RTM company, block and flat scheme,
WordPress namespace, voicemail provider, personal word lists) comes from one private JSON file, never
from code: `<data dir>/profile.json` next to the brain (the packaged default is entirely fictional).
Copy `src/mongoose/assets/profile.default.json` from mongoose-core, edit it, `chmod 600` it. A
profile with a missing or unknown key fails loud; no `.env` can repoint it. See `mongoose.profile`.

## What it does

### Talk to it, anywhere
- **Chat** in the browser, on your phone (installable PWA), by voice, or over WhatsApp.
- **Local models first.** A resident Ollama model handles conversation and tool use with an agentic
  loop: it proposes tool calls, Mongoose validates and runs them, up to a capped number of steps.
  Nothing leaves the Mac unless you opt in.
- **Optional cloud escalation**, off by default, with a per-turn cost cap.
- **Model tuning from evidence.** Mongoose benchmarks candidate models on your real task mix in the
  background and recommends (or, if the win is large and safe, makes) a reversible switch:
  `mongoose tune`.

### Run the house
- **Home Assistant** is the control plane: lights, plugs, scenes, climate, announcements. Explicit
  safe light groups only, never area-wide sweeps.
- **Alexa fallback** for low-sensitivity actions through a bridge you configure.
- **Weather**, **calendar and tasks** (Google, Microsoft), **reminders and time alerts**, a
  **daily brief**, and **notifications** with deduplication, privacy routing and optional ntfy push.

### Find things
- **One search across your world:** email (IMAP, Gmail, Microsoft), files, attachments, voicemails,
  notifications and imported knowledge, indexed locally with SQLite FTS5 and embeddings.
- **Document reading:** PDFs, Word files and images (OCR) are extracted, summarised and searchable.
- **Voicemail:** transcribed locally, matched to the caller, never invented.

### Phones, guests and remote access
- **Device pairing** by QR code with per-device trust and revocation.
- **Guest invites:** a guest profile on a default-deny allowlist, revocable at any time.
- **Remote access** through Cloudflare Tunnel, set up from the Cockpit, with an end-to-end proof
  check (works behind carrier-grade NAT where port forwarding cannot).

### The Cockpit
An owner-only control surface: setup, connectors, guest access, updates, backups, model tuning, bug
reports, build status and an honest readiness view. Owner journeys go through the Cockpit, not the
terminal.

### Look after itself
- **Watchdog and liveness:** a supervisor with heartbeats and recovery.
- **Updates** are staged, checked, applied with a backup first, and rolled back if the new version
  does not prove itself healthy.
- **Backups and brain preservation:** the SQLite brain is guarded against accidental reset, with
  disk-space checks and optional off-machine copies.
- **Self-diagnosis:** bug reports with evidence, proposed fixes gated on verification, and a
  transparency log of what it did and why.

### For developers
- **MCP server** so other assistants can use Mongoose's tools, with OAuth.
- **Claude Code integration** for code changes, with billing exposure made visible.
- **Connectors:** Home Assistant, Google, Microsoft, WordPress, WhatsApp providers, web search,
  crawler.

### Optional: running a residents' management company (RTM)
Mongoose includes an optional module for a UK Right to Manage company: leaseholder register, service
charge demands and receipts, apportionment, payment plans, overdue notices, a trust-account cash book
with bank reconciliation, seller packs, member votes, a WordPress leaseholder portal plugin and an
accountant export. It is inert unless you configure it.

All packaged RTM data is **fictional** (Example Court, Anytown, Brian Bennett, Sample Bank). A real
company's identity, register and letterhead live outside the code in the data directory and are
loaded at runtime. See [docs/RTM_CONFIG.md](docs/RTM_CONFIG.md).

---

## What it will not do

Mongoose is safe by default. It will not:

- send email, message anyone, buy anything, delete anything or change access without you seeing and
  confirming the action;
- use Alexa for private data;
- spend money silently (cloud and Claude Code usage are opt-in and shown);
- report success it has not verified (sends, payments and installs are checked at the layer of use).

---

## Your data stays out of the code

| What | Where |
|---|---|
| The brain (memories, conversations, indexes) | `~/.mongoose/data/mongoose.sqlite3` |
| Secrets and settings | `~/.mongoose/.env` |
| Optional private config (e.g. RTM identity) | `~/.mongoose/data/rtm/` |

The repo ships a [leak gate](tools/leak_gate.py) that runs before every commit. It checks three things:
a private term list kept outside the repo, real-looking identifiers (emails, phone numbers, bank
details, title numbers, keys), and any line that describes a person's body, health, mind, family or
routine, which must be reviewed once by a human. Install the hook:

```bash
ln -s ../../scripts/pre-commit-leak-gate.sh .git/hooks/pre-commit
```

---

## Configuration

Copy `.env.example` to `~/.mongoose/.env` and fill in only what you use. Every connector is optional:
models (Ollama), Home Assistant, email (IMAP, Google, Microsoft), notifications (ntfy, web push),
remote access (Cloudflare), WhatsApp, cloud escalation. See
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) and [docs/CONNECTORS.md](docs/CONNECTORS.md).

HTTPS is on by default with a self-signed certificate. Plain HTTP only with `--no-tls`.

---

## Architecture

```text
phone / browser / voice / WhatsApp
            |
        FastAPI app (src/mongoose/app.py) ---- Cockpit (owner) / guest surface
            |
   intent routing -> local model (Ollama) -> tool loop (tools.py, allowlisted, step-capped)
            |
  services/  home, calendar, email, files, voicemail, brief, memory, scheduler, RTM, ...
            |
   SQLite brain (FTS5 + embeddings)        connectors/  HA, Google, Microsoft, WordPress, ...
```

More: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md),
[docs/MODEL_ROLES.md](docs/MODEL_ROLES.md), and the [docs index](docs/README.md).

### Repository map

```text
src/mongoose/             the package: app, CLI, tools, persona, cockpit
src/mongoose/services/    feature services, one module per capability
src/mongoose/connectors/  external systems
wordpress-plugin/         leaseholder portal plugin (RTM module)
scripts/                  install, update, probes, model pulls, hooks
configs/                  example configs (Home Assistant, launchd, LM Studio)
tests/                    pytest suite
tools/                    release tooling, including the leak gate
docs/                     guides and design notes
```

---

## Development

```bash
pip install -e '.[test,devices]'
PYTHONPATH=src python -m pytest -p no:cacheprovider -q tests/test_<area>.py   # targeted
```

Some integration tests start a real server on port 8765. Do not run the suite on a machine that is
serving a live household. Before a release: `python tools/leak_gate.py`, the release tests
(`tests/test_release_*.py`, `tests/test_version_consistency.py`) and a clean-unzip install.

What changed: [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).

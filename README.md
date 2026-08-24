# MasterPilot-Setup

Tooling for setting up master-pilot environments. Each tool lives in its own
folder with its own README. Dependencies and the Python environment are managed
with `uv`.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — install with `brew install uv` (or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **git**

uv manages Python itself (it will fetch CPython 3.12 per `.python-version`), so
you don't need a separate Python install.

## Getting started

```bash
# 1. Clone the repo
git clone <repo-url>
cd MasterPilot-Setup

# 2. Create the virtualenv and install dependencies from the lockfile
uv sync
```

## Tools

| Tool | What it does |
|------|--------------|
| [`rate_structure_migration/`](rate_structure_migration/README.md) | Migrate utility rate plans from one or more source environments into a single target environment. |
| [`cdg_user_setup/`](cdg_user_setup/README.md) | Create QA users by running the full CDG (Customer Data Generator) porting flow, driven by `config.json` + `sources.csv`. |
| [`Persona_setup/`](Persona_setup/README.md) | Set up user personas for pilots: [`HER/`](Persona_setup/HER/README.md) (pilot config sync, DB SQL, S3 payload upload, NBI string resources, and the SHC 2.0 mock pipeline), [`BestRateEmail/`](Persona_setup/BestRateEmail/README.md) (user-scoped Best Rate email: rate selection, NBI assets, manual interaction, and the notification trigger), and [`TOUOnboarding/`](Persona_setup/TOUOnboarding/README.md) (user-scoped TOU Rate Onboarding email: rate-plan selection, enrolment file, transition wait, and the notification trigger), driven by `config.json`. |

Then pick a tool, copy its config templates, and fill them in:

```bash
cd cdg_user_setup                # or rate_structure_migration
cp config.json.example config.json
cp sources.csv.example sources.csv
```

Run the tool's script with `uv run` (it auto-syncs the env first):

```bash
uv run python cdg_user_setup/create_users.py --limit 1
uv run python rate_structure_migration/migrate_rate_structure.py
```

**The real `config.json` / `sources.csv` hold live tokens and are git-ignored** —
only the `*.example` templates are committed. Each folder's README documents
every field and the run commands.

## Linting

```bash
uv run ruff check .     # lint
uv run ruff format .    # auto-format
```

## Troubleshooting

- `uv: command not found` → install uv (see Prerequisites), then re-open your shell.
- `... not found. Copy <file>.example to <file> ...` → you haven't created the
  real `config.json` / `sources.csv` yet (see Getting started).
- `Missing required config key(s) ...` / `missing column(s) ...` → a required
  field is blank or a CSV header is missing; check the tool's README table.

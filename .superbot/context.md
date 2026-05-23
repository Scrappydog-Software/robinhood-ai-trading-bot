# Project Context

> Auto-loaded by SuperBot's `invoke-agent.js` and injected into every dev-agent prompt.
> Updated alongside any change to tech stack, infra, or deploy process.

## Tech stack
- **Language:** Python 3 only. No Node/Go/Rust here.
- **Runtime:** long-lived script, executed via `python main.py`. Not a service, not a Lambda, no Docker.
- **Entry point:** `main.py` at repo root.
- **Dependencies** (`requirements.txt`):
  - `robin_stocks` — Robinhood API client
  - `anthropic` — Anthropic SDK (this project uses **Anthropic's API** / Claude as the LLM provider; migrated from OpenAI in issue #2)
  - `onepassword` — 1Password service-account client (for Robinhood MFA secret)
  - `pandas`, `pytz`, `pyotp`
- **Package mgmt:** plain `pip install -r requirements.txt`. No poetry / pipenv / pdm / uv.

## Module layout
```
main.py                   # orchestration loop
src/
  api/
    robinhood.py          # all Robinhood interactions (auth, portfolio, orders)
    claude.py             # LLM prompt construction + completion (Anthropic Claude)
    onepassword.py        # MFA secret fetch
  utils/
    auth.py
    logger.py
```
Keep new code aligned to that split: API integrations under `src/api/`, helpers under `src/utils/`.

## Configuration
- `config.py` is **gitignored** and instantiated locally from `config.py.example`.
- Every new tunable goes in `config.py.example` with a comment, then reads from `config` in code via `from config import *`.
- Secrets (Robinhood + Anthropic credentials) live in `config.py` or 1Password. **Never** commit a populated `config.py`.

## Trading modes
- `MODE = "demo"` — simulate, do not place orders
- `MODE = "manual"` — prompt for confirmation
- `MODE = "auto"` — place orders unattended

Any change that affects order placement (`src/api/robinhood.py`, the AI decision JSON schema in `main.py`) must be tested in `demo` first.

## Patterns to follow
- Top-level functions in `main.py`; classes only when state genuinely justifies it.
- f-string formatting throughout.
- `logger.py` for output — don't introduce a second logging shim.
- Async usage is currently scoped to the Robinhood login path; don't propagate `async` into other paths without a reason.

## What is NOT used
- No web framework (Flask / FastAPI / Django).
- No database (state is implicit in Robinhood + the AI prompt).
- No test framework currently configured. If you add one, choose `pytest` and put tests under `tests/`.
- No build system / bundler.
- No type checker (`mypy` / `pyright`). Adding one is fine but requires an ADR.

## High-blast-radius areas (always recommend Code Review + QA)
- `main.py` order-placement and decision-parsing logic
- `src/api/robinhood.py` — anything touching `order_buy_*` / `order_sell_*`
- `src/api/claude.py` prompt template or response schema changes
- `requirements.txt` version bumps for `robin_stocks` or `anthropic`

This bot moves real money. Treat behavior changes the way you would treat a Lambda hotfix: don't ship without verification, and prefer reversible changes.

## CI / deploy
- No deploy automation. The bot runs wherever you start it (typically a local laptop or a long-running VM).
- The only workflow currently installed is `.github/workflows/superbot-relay.yml` (forwards events to SuperBot).
- Four-environment SDLC does not apply here — this is not a web project. Branch flow is still `feature → PR → develop → main`, but there is no QA/Staging/Production deploy.

## SuperBot-specific notes
- Repo onboarded 2026-05-23. Issues were enabled during onboarding (PM/Triage require them).
- `develop` was created off `main` on the same day.
- `main` and `develop` are guarded against deletion via `scripts/prevent-branch-deletion.js`.

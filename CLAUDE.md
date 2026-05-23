> **Global context:** See master CLAUDE.md at https://github.com/scrappydog-software/.github/blob/main/CLAUDE.md

## Parent Organization
This repo is owned by **ScrappyDog Software LLC**. High-level business goals, branding, and marketing direction are maintained in the [Scrappydog_Business](https://github.com/scrappydog-software/Scrappydog_Business) repository. Always consult the `.md` files at the root of that repo for business context, branding guidelines, and strategic priorities.

---

# robinhood-ai-trading-bot

> This repo follows the global conventions defined in the master CLAUDE.md above.

## SuperBot Management

This repo is onboarded to SuperBot (the multi-agent SDLC platform at [`scrappydog-software/SuperBot`](https://github.com/scrappydog-software/SuperBot)).

- The `.github/workflows/superbot-relay.yml` workflow forwards issue/PR/CI events to SuperBot, which runs the appropriate agent against this repo.
- Entry in SuperBot's registry: `config/repositories.yaml` → `Scrappydog-Software/robinhood-ai-trading-bot`.
- Required secret: `SUPERBOT_PAT` (inherited from the org-level secret on `scrappydog-software`).
- To trigger a CLI workflow from the command line: `cd` into this repo and run `/superbot <request>` in a Claude Code session (see [`skills/superbot/SKILL.md`](https://github.com/scrappydog-software/SuperBot/blob/main/skills/superbot/SKILL.md)).

## Branching & Workflow

- **PRs are policy, not enforcement (2026-04-22).** Branch protection is not applied by default across Scrappydog-Software repos. PRs remain the expected path for every substantive change, but direct pushes to `main`/`develop` will succeed. If this repo ever warrants hard enforcement, run `node scripts/apply-branch-protection.js --repo Scrappydog-Software/robinhood-ai-trading-bot` from SuperBot.
- If this repo adopts the four-environment web SDLC (QA → Develop → Staging → Production), create a `develop` branch.
- Feature branches: `feature/issue-{N}-{slug}` or `fix/issue-{N}-{slug}`.
- Agents default to opening PRs; direct-to-main is reserved for backlog cleanup and trivial housekeeping.

## Inherited org-wide rules

This repo inherits every rule documented in the master CLAUDE.md, including:

- **PRs are policy, not enforcement** — open a PR for substantive changes; direct push is the escape hatch.
- **AWS Lambda Source Control (hard rule)** — if this repo has (or adds) a Lambda, its source, package manifests, IAM policy, and deploy mechanism must be committed to git before any `aws lambda update-function-code` runs. The Lambda must be registered in `scrappydog-software/SuperBot` `config/lambdas.yaml`. Out-of-band edits via the AWS console are prohibited. See [`scrappydog-software/SuperBot` CLAUDE.md "AWS Lambda Source Control (Hard Rule)"](https://github.com/scrappydog-software/SuperBot/blob/main/CLAUDE.md#aws-lambda-source-control-hard-rule).

## Project context for agents

Drop a `.superbot/context.md` at the root (even sparse) so SuperBot agents pick up project-specific context automatically on dispatch. Document:

- Tech stack (languages, frameworks, build tools)
- Key files and their purposes
- Patterns to follow (module system, styling, testing)
- What is NOT used (prevents agents from introducing unwanted deps)
- Infrastructure and deploy process

---

*This CLAUDE.md was scaffolded by SuperBot onboarding. Customize freely — only the top-of-file global context pointer and the SuperBot Management / Inherited rules sections should stay in sync with the org-wide template.*

# AGENTS.md

## Project

This is a Python project managed with `uv`.

## Commands

- Create or update the environment: `uv sync`
- Run the app entry point: `uv run rawww`
- Run a Python module/script: `uv run python -m <module>`
- Add dependencies: `uv add <package>`
- Add development dependencies: `uv add --dev <package>`

## Conventions

- Keep source code under `src/rawww`.
- Prefer small, focused changes that match the existing project structure.
- Do not commit virtual environments, caches, local `.env` files, or generated build artifacts.
- Update `README.md` when adding user-facing commands or behavior.

## Git

- Check `git status --short` before making broad edits.
- Do not revert unrelated local changes.
- Keep commits focused on one logical change.

# Contributing

Thanks for helping improve Fivelanes.

## Development setup

Fivelanes separates **code** (this repo) from **private data** (your own directory). See [README.md](README.md#setup) for the full layout.

1. Copy [`.env.example`](.env.example) to `.env` in the repo root and set `FIVELANES_DATA_ROOT` to your data directory.
2. Create that data directory and copy [`data.env.example`](data.env.example) to `$FIVELANES_DATA_ROOT/.env`; set `SOURCE_ACCOUNT`, `SOURCE_OAUTH_ACCOUNT_ID`, `OWNER_NAME`, and your LLM backend.
3. Copy [`services/prompts.example.json`](services/prompts.example.json) to `$FIVELANES_DATA_ROOT/prompts.json` (optional: set `FIVELANES_PROMPTS_PATH` for a non-default location).
4. Copy [`credentials/credentials.example.json`](credentials/credentials.example.json) to `$FIVELANES_DATA_ROOT/credentials/credentials.json` (Google Cloud Console OAuth desktop client).
5. Run `python utils/add_account.py you@example.com` to create `$FIVELANES_DATA_ROOT/credentials/tokens.json`.
6. Install Python deps: `pip install -r requirements-linux.txt`
7. Install frontend deps and build: `cd frontend && npm install && npm run build`

## Prompts

Prompt text lives in your data directory (`prompts.json` by convention). The public repo ships only [`services/prompts.example.json`](services/prompts.example.json) as a schema/template. When adding new prompt keys, update the example file and document required `{placeholders}`.

## Frontend

TypeScript sources are under `frontend/src/`. After editing, run `npm run build` in `frontend/` to refresh `frontend/dist/`.

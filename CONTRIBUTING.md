# Contributing

Thanks for helping improve Fivelanes.

## Development setup

1. Copy [`.env.example`](.env.example) to `.env` and set `SOURCE_ACCOUNT`, `SOURCE_OAUTH_ACCOUNT_ID`, and your LLM backend.
2. Copy [`services/prompts.example.json`](services/prompts.example.json) to `services/prompts.json` and author your prompts.
3. Copy [`credentials/credentials.example.json`](credentials/credentials.example.json) to `credentials/credentials.json` (from Google Cloud Console OAuth desktop client).
4. Install Python deps: `pip install -r requirements-linux.txt`
5. Install frontend deps and build: `cd frontend && npm install && npm run build`

## Prompts

Prompt text lives in `services/prompts.json` (gitignored). The public repo ships only [`services/prompts.example.json`](services/prompts.example.json) as a schema/template. When adding new prompt keys, update the example file and document required `{placeholders}`.

## Frontend

TypeScript sources are under `frontend/src/`. After editing, run `npm run build` in `frontend/` to refresh `frontend/dist/`.

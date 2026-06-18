"""Paths and environment variables."""
import os
from pathlib import Path

from utils.runtime_paths import credentials_dir, database_path, env_file, infra_root, load_env

load_env()

PROJECT_ROOT = infra_root()
CREDENTIALS_DIR = credentials_dir()
CREDENTIALS_PATH = str(CREDENTIALS_DIR / "credentials.json")
TOKENS_PATH = str(CREDENTIALS_DIR / "tokens.json")

SOURCE_ACCOUNT = (os.getenv("SOURCE_ACCOUNT") or "").strip().lower()
SOURCE_OAUTH_ACCOUNT_ID = (os.getenv("SOURCE_OAUTH_ACCOUNT_ID") or "").strip()
DATABASE_NAME = database_path()
QUERY_BATCH_SIZE = 8

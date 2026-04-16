import os
from pathlib import Path

# Keep secrets out of source control. Configure via environment variables.
# Optional: if you install `python-dotenv` and create a `.env`, we'll auto-load it.
try:
    from dotenv import load_dotenv, find_dotenv

    # Prefer a `.env` next to this file (VirtualLibrary/.env). If not present, fall back
    # to the standard search (cwd and parents), which covers repo-root `.env`.
    local_env = Path(__file__).with_name(".env")
    if local_env.exists():
        load_dotenv(dotenv_path=local_env, override=False)
    else:
        load_dotenv(dotenv_path=find_dotenv(usecwd=True), override=False)
except ImportError:
    pass

OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_MODEL_ENV = "OPENAI_MODEL"

# Safe default model. Override via OPENAI_MODEL if desired.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def get_openai_api_key() -> str | None:
    return os.getenv(OPENAI_API_KEY_ENV)


def get_openai_model() -> str:
    return os.getenv(OPENAI_MODEL_ENV) or DEFAULT_OPENAI_MODEL

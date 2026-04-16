## SystemDesign

This repo contains several small system-design projects. The OpenAI integration lives in `VirtualLibrary/FuzzySearch.py` and is controlled via the `OPENAI_API_KEY` environment variable.

## Prerequisites

- Python 3
- Internet access (for OpenAI calls)

Optional:

- Docker (only if you want to run the OpenSearch-backed search flow)

## Setup (Virtual Environment)

From the repo root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
```

## Install Dependencies

Install the OpenAI SDK (required for query refinement):

```bash
python -m pip install openai
```

If you want to use `.env` files (recommended), install `python-dotenv`:

```bash
python -m pip install python-dotenv
```

If you plan to run the OpenSearch integration:

```bash
python -m pip install opensearch-py
```

## Make The OpenAI Key Available

You must set `OPENAI_API_KEY` in the environment seen by the Python process.

### Option A: Export in Your Shell

macOS/Linux (zsh/bash):

```bash
export OPENAI_API_KEY="your_key_here"
```

Windows PowerShell:

```powershell
$env:OPENAI_API_KEY="your_key_here"
```

### Option B: Use a `.env` File

1. Copy `.env.example` to `.env`
2. Put your key in `.env`:

```bash
OPENAI_API_KEY=your_key_here
```

`VirtualLibrary/settings.py` will auto-load `.env` if `python-dotenv` is installed.

### Option C: Set It In PyCharm

In your Run/Debug configuration, add an environment variable:

- `OPENAI_API_KEY=...`

## Verify OpenAI Works (No OpenSearch Needed)

Run the query refinement test as a module from the repo root (this avoids import-path issues):

```bash
.venv/bin/python -m VirtualLibrary.test_query_refinement
```

If `OPENAI_API_KEY` is set, the output should show a refined query; otherwise it will fall back to the original query.

## Run FuzzySearch With OpenSearch (Optional)

1. Start OpenSearch:

```bash
docker compose -f VirtualLibrary/docker-compose.yaml up -d
```

2. Run the end-to-end search test:

```bash
.venv/bin/python -m VirtualLibrary.test_llm_search
```

## Notes / Safety

- Never hardcode API keys in source files.
- Do not commit `.env` files; this repo’s `.gitignore` ignores `.env` by default.

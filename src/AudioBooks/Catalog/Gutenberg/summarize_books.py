"""Stream a Gutenberg text from GCS and summarize it with Ollama.

This is the lightweight local-Ollama alternative to the more complete runners
in :mod:`AudioBooks.BookSummary`. Importing this module never contacts GCS or
Ollama; external clients are created only after CLI arguments are validated.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import storage


load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DEFAULT_MODEL = "gemma2:27b"
DEFAULT_CHUNK_SIZE = 25_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize one GCS Gutenberg text with a local Ollama model.",
    )
    parser.add_argument("blob", help="GCS object name containing UTF-8 book text.")
    parser.add_argument("--bucket", default=os.environ.get("GCS_BUCKET"))
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSONL output path. Defaults to stdout only.",
    )
    return parser.parse_args()


def summarize_chunk(text: str, model: str) -> str:
    import ollama

    response = ollama.chat(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a literary assistant. Summarize this text chunk concisely.",
            },
            {"role": "user", "content": f"Text:\n{text}"},
        ],
    )
    return str(response["message"]["content"]).strip()


def stream_and_summarize(
    bucket,
    blob_name: str,
    *,
    model: str = DEFAULT_MODEL,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    output_path: Path | None = None,
) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    summaries: list[str] = []
    output_file = None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_file = output_path.open("a", encoding="utf-8")

    try:
        with bucket.blob(blob_name).open("r", encoding="utf-8") as source:
            for chunk_index in range(1, 1_000_000_000):
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                print(f"Processing {blob_name} - chunk {chunk_index}...", flush=True)
                summary = summarize_chunk(chunk, model)
                summaries.append(summary)
                if output_file is not None:
                    output_file.write(
                        json.dumps(
                            {
                                "blob": blob_name,
                                "chunk": chunk_index,
                                "summary": summary,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    output_file.flush()
    finally:
        if output_file is not None:
            output_file.close()

    return summaries


def main() -> None:
    args = parse_args()
    if not args.bucket:
        raise ValueError("Set GCS_BUCKET or pass --bucket.")

    client = storage.Client(project=args.project)
    summaries = stream_and_summarize(
        client.bucket(args.bucket),
        args.blob,
        model=args.model,
        chunk_size=args.chunk_size,
        output_path=args.output,
    )
    print(json.dumps({"blob": args.blob, "chunks": len(summaries)}), flush=True)


if __name__ == "__main__":
    main()

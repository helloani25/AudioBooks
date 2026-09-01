"""RunPod vLLM book summarization runner.

This is the RunPod-only companion to Local_HF_Endpoint.ipynb. It assumes you
already deployed a RunPod vLLM endpoint for the Hugging Face model and exposes
an OpenAI-compatible API.

RunPod serverless vLLM endpoint:
    base URL = https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1

RunPod Pod with a public/proxy vLLM server:
    pass the pod's OpenAI-compatible /v1 URL with --base-url.

Environment (.env or shell):
    RUNPOD_API_KEY                 RunPod API key.
    RUNPOD_ENDPOINT_ID             Serverless endpoint id. Used to build base URL.
    RUNPOD_VLLM_BASE_URL           Optional explicit OpenAI-compatible base URL.
    HF_TOKEN or HF_API_TOKEN       Hugging Face token for tokenizer / gated model access.
    GOOGLE_APPLICATION_CREDENTIALS Path to GCS service-account JSON key.
    GCS_BUCKET                     Bucket holding uploaded book content.

Examples:
    python AudioBooks/BookSummary/runpod_summarize.py \
        --endpoint-id "$RUNPOD_ENDPOINT_ID" \
        --book-id 4037 \
        --validate

    python AudioBooks/BookSummary/runpod_summarize.py \
        --base-url "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/openai/v1" \
        --max-books 20
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from openai import OpenAI
from transformers import AutoTokenizer

import AudioBooks.BookSummary.summarizer as summarizer
from AudioBooks.BookSummary.summarizer import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_EMBEDDING_MODEL_ID,
    DEFAULT_LEXICAL_FLOOR,
    DEFAULT_MAX_NEW_TOKENS,
    DEFAULT_NLI_CONTRADICTION_THRESHOLD,
    DEFAULT_NLI_MODEL_ID,
    DEFAULT_REDUCE_MAX_NEW_TOKENS,
    DEFAULT_SEMANTIC_THRESHOLD,
    _lexical_similarity,
    _load_book_record,
    _load_embedding_model,
    _load_nli_model,
    _make_gcs_client,
    _max_nli_contradiction,
    _resolve_book_id,
    _semantic_similarity,
    _strip_prompt,
    summarize_book,
)
from AudioBooks.BookSummary.summary_utils import truncate_at_prompt_scaffold

DEFAULT_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
# Results live next to this script (AudioBooks/BookSummary/Artifacts) regardless
# of the working directory the command is launched from.
ARTIFACTS_DIR = Path(__file__).resolve().parent / "Artifacts"
DEFAULT_OUTPUT_PATH = str(ARTIFACTS_DIR / "summary_results_runpod.jsonl")
DEFAULT_CHECKPOINT_DIR = str(ARTIFACTS_DIR / "checkpoints" / "runpod")
MIN_CHUNK_TOKENS = 20

# Generation budgets aligned with Local_HF_Endpoint.ipynb so all backends match.
# These intentionally override summarizer.py's terser module defaults (6000 / 4096),
# which compressed story_so_far and chapter summaries.
NB_CHUNK_TOKENS = 4096
NB_REDUCE_INPUT_TOKENS = 8192


def _hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN")


def _default_base_url(endpoint_id: str | None) -> str | None:
    if not endpoint_id:
        return None
    return f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model-id", default=os.environ.get("RUNPOD_MODEL_ID", DEFAULT_MODEL_ID))
    parser.add_argument("--api-key", default=os.environ.get("RUNPOD_API_KEY"))
    parser.add_argument("--endpoint-id", default=os.environ.get("RUNPOD_ENDPOINT_ID"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("RUNPOD_VLLM_BASE_URL"),
        help="OpenAI-compatible base URL. If omitted, built from --endpoint-id.",
    )
    parser.add_argument("--timeout", type=float, default=900.0, help="OpenAI client timeout in seconds.")
    parser.add_argument("--request-retries", type=int, default=3)

    parser.add_argument("--book-id", type=int, help="Single internal books.id to summarize.")
    parser.add_argument("--gutenberg-id", type=int, help="Single Gutenberg id, resolved via the GCS id map.")
    parser.add_argument(
        "--max-books",
        type=int,
        default=None,
        help="Cap unprocessed books to run. Ignored with --book-id/--gutenberg-id.",
    )

    parser.add_argument("--bucket", default=os.environ.get("GCS_BUCKET", "gutenberg-books"))
    parser.add_argument(
        "--gcs-credentials",
        default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        metavar="KEY_FILE",
    )

    parser.add_argument("--chunk-tokens", type=int, default=NB_CHUNK_TOKENS)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--reduce-input-tokens", type=int, default=NB_REDUCE_INPUT_TOKENS)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--reduce-max-new-tokens", type=int, default=DEFAULT_REDUCE_MAX_NEW_TOKENS)
    parser.add_argument("--profile-max-new-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.0, help="Keep 0.0 to match notebook greedy decoding.")
    parser.add_argument("--repetition-penalty", type=float, default=1.05, help="Keep 1.05 to match the notebook.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        help="Concurrent RunPod vLLM requests.",
    )
    parser.add_argument("--max-chapters", type=int, default=None)
    parser.add_argument("--max-chunks-per-chapter", type=int, default=None)

    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--semantic-threshold", type=float, default=DEFAULT_SEMANTIC_THRESHOLD)
    parser.add_argument("--lexical-floor", type=float, default=DEFAULT_LEXICAL_FLOOR)
    parser.add_argument("--nli-contradiction-threshold", type=float, default=DEFAULT_NLI_CONTRADICTION_THRESHOLD)
    parser.add_argument("--embedding-model-id", default=DEFAULT_EMBEDDING_MODEL_ID)
    parser.add_argument("--nli-model-id", default=DEFAULT_NLI_MODEL_ID)

    parser.add_argument("--output-path", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--checkpoint-dir", default=DEFAULT_CHECKPOINT_DIR)
    return parser.parse_args()


def _truncate_at_prompt_marker(text: str) -> str:
    # Keyword-bound: cut only at leaked prompt scaffolding, never at a legitimate
    # markdown heading such as a character-name '### Oliver Twist' in a profile.
    return truncate_at_prompt_scaffold(text)


def _setup_runpod_backend(args: argparse.Namespace, hf_token: str | None):
    base_url = args.base_url or _default_base_url(args.endpoint_id)
    if not base_url:
        raise RuntimeError("Set RUNPOD_VLLM_BASE_URL or RUNPOD_ENDPOINT_ID, or pass --base-url/--endpoint-id.")
    if not args.api_key:
        raise RuntimeError("Set RUNPOD_API_KEY or pass --api-key.")

    client = OpenAI(api_key=args.api_key, base_url=base_url, timeout=args.timeout)

    tokenizer = AutoTokenizer.from_pretrained(args.model_id, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token or "[PAD]"

    def _call_runpod(prompt: str, *, max_new_tokens: int) -> str:
        print(
            f"Calling RunPod vLLM {args.model_id} for {len(prompt):,} prompt chars, "
            f"max_tokens={max_new_tokens}",
            flush=True,
        )
        for attempt in range(args.request_retries):
            try:
                response = client.completions.create(
                    model=args.model_id,
                    prompt=prompt,
                    max_tokens=max_new_tokens,
                    temperature=args.temperature,
                    echo=False,
                    extra_body={"repetition_penalty": args.repetition_penalty},
                )
                text = response.choices[0].text or ""
                text = _truncate_at_prompt_marker(text)
                print(f"RAW MODEL OUTPUT: {text!r}", flush=True)
                return text
            except Exception as exc:
                if attempt == args.request_retries - 1:
                    raise
                wait_seconds = 5 * (attempt + 1)
                print(f"RunPod request failed ({exc}); retrying in {wait_seconds}s", flush=True)
                time.sleep(wait_seconds)

    def _remote_generate_text(model, tokenizer, prompt: str, *, device, max_input_tokens: int, max_new_tokens: int) -> str:
        input_ids = tokenizer(
            prompt,
            add_special_tokens=False,
            truncation=True,
            max_length=max_input_tokens,
        ).input_ids
        truncated_prompt = tokenizer.decode(input_ids, skip_special_tokens=True)
        generated = _call_runpod(truncated_prompt, max_new_tokens=max_new_tokens)
        return _strip_prompt(generated)

    def _remote_generate_batch(model, tokenizer, prompts: list[str], *, device, max_input_tokens: int, max_new_tokens: int) -> list[str]:
        results = [""] * len(prompts)
        work = [
            (i, prompt)
            for i, prompt in enumerate(prompts)
            if len(tokenizer(prompt, add_special_tokens=False).input_ids) >= MIN_CHUNK_TOKENS
        ]
        skipped = len(prompts) - len(work)
        if skipped:
            print(f"  skipping {skipped} trivial chunk(s) with < {MIN_CHUNK_TOKENS} tokens", flush=True)
        if not work:
            return results

        with ThreadPoolExecutor(max_workers=max(1, args.batch_size)) as executor:
            futures = {
                executor.submit(
                    _remote_generate_text,
                    model,
                    tokenizer,
                    prompt,
                    device=None,
                    max_input_tokens=max_input_tokens,
                    max_new_tokens=max_new_tokens,
                ): idx
                for idx, prompt in work
            }
            for future, idx in futures.items():
                results[idx] = future.result()
        return results

    summarizer._generate_text = _remote_generate_text
    summarizer._generate_batch = _remote_generate_batch
    print(f"RunPod vLLM generation configured: {base_url}", flush=True)
    return client, tokenizer, base_url


def _processed_ids(output_path: Path) -> set[int]:
    processed: set[int] = set()
    if not output_path.exists():
        return processed
    for line in output_path.read_text(encoding="utf-8").splitlines():
        try:
            processed.add(json.loads(line)["book_id"])
        except Exception:
            pass
    return processed


def _resolve_book_ids(gcs_client, args: argparse.Namespace, processed: set[int]) -> list[int]:
    if args.book_id is not None or args.gutenberg_id is not None:
        return [_resolve_book_id(gcs_client, args.bucket, args.book_id, args.gutenberg_id)]

    id_map_blob = gcs_client.bucket(args.bucket).blob("book-desc/gutenberg-id-map.json")
    id_map = json.loads(id_map_blob.download_as_text(encoding="utf-8"))
    all_book_ids = sorted(set(id_map.values()))
    todo = [book_id for book_id in all_book_ids if book_id not in processed]
    if args.max_books is not None:
        todo = todo[: args.max_books]
    print(f"Found {len(all_book_ids)} books in GCS bucket {args.bucket!r}", flush=True)
    return todo


def main() -> None:
    args = parse_args()
    hf_token = _hf_token()

    if not args.bucket:
        raise RuntimeError("Set GCS_BUCKET or pass --bucket.")

    output_path = Path(args.output_path)
    checkpoint_dir = Path(args.checkpoint_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    print(f"stage: connecting to GCS bucket {args.bucket!r}", flush=True)
    gcs_client = _make_gcs_client(args.gcs_credentials)

    processed = set() if (args.book_id is not None or args.gutenberg_id is not None) else _processed_ids(output_path)
    print(f"Already processed: {len(processed)} books", flush=True)

    model, tokenizer, generation_endpoint = _setup_runpod_backend(args, hf_token)

    embedding_tokenizer = embedding_model = None
    nli_tokenizer = nli_model = None
    if args.validate:
        print("stage: loading validation models (embedding + NLI)", flush=True)
        embedding_tokenizer, embedding_model = _load_embedding_model(args.embedding_model_id, hf_token)
        nli_tokenizer, nli_model = _load_nli_model(args.nli_model_id, hf_token)

    book_ids = _resolve_book_ids(gcs_client, args, processed)
    print(f"Running: {len(book_ids)} books via RunPod vLLM", flush=True)

    for index, book_id in enumerate(book_ids, start=1):
        print(f"\n[{index}/{len(book_ids)}] book_id={book_id}", flush=True)
        try:
            book, book_desc_summary = _load_book_record(gcs_client, args.bucket, book_id)
        except FileNotFoundError as exc:
            print(f"  SKIP: {exc}", flush=True)
            continue

        mode = "verify" if (book_desc_summary and args.validate) else "generate"
        print(f"  mode={mode} title={book.title!r} category={book.category or '(unknown)'!r}", flush=True)
        print(f"  generation_endpoint={generation_endpoint}", flush=True)

        try:
            result = summarize_book(
                model,
                tokenizer,
                book,
                device=None,
                chunk_tokens=args.chunk_tokens,
                chunk_overlap=args.chunk_overlap,
                reduce_input_tokens=args.reduce_input_tokens,
                max_new_tokens=args.max_new_tokens,
                reduce_max_new_tokens=args.reduce_max_new_tokens,
                batch_size=args.batch_size,
                max_chapters=args.max_chapters,
                max_chunks_per_chapter=args.max_chunks_per_chapter,
                story_so_far_tokens=args.reduce_max_new_tokens,
                checkpoint_path=str(checkpoint_dir / f"checkpoint_{book_id}.jsonl"),
                profile_max_new_tokens=args.profile_max_new_tokens,
            )
        except Exception as exc:
            print(f"  ERROR summarizing: {exc}", flush=True)
            continue

        result["mode"] = mode
        result["generation_backend"] = "runpod-vllm"
        result["generation_endpoint"] = generation_endpoint
        result["book_desc_summary"] = book_desc_summary
        result["semantic_score"] = None
        result["lexical_score"] = None
        result["nli_contradiction_score"] = None
        result["similarity_pass"] = None

        print("\nFINAL SUMMARY", flush=True)
        print(result["final_summary"], flush=True)
        profiles = result.get("character_profiles")
        if profiles:
            profile_label = "NARRATOR PROFILE" if result.get("category") == "practical" else "CHARACTER PROFILES"
            print(f"\n{profile_label}  [category={result.get('category', '?')!r}]", flush=True)
            print(profiles, flush=True)

        truncated_run = args.max_chapters is not None or args.max_chunks_per_chapter is not None
        if args.validate and book_desc_summary and not truncated_run:
            semantic = _semantic_similarity(embedding_tokenizer, embedding_model, result["final_summary"], book_desc_summary)
            lexical = _lexical_similarity(result["final_summary"], book_desc_summary)
            contradiction = _max_nli_contradiction(nli_tokenizer, nli_model, result["final_summary"], book_desc_summary)
            sem_pass = semantic >= args.semantic_threshold
            lex_pass = lexical >= args.lexical_floor
            nli_pass = contradiction < args.nli_contradiction_threshold
            sim_pass = sem_pass and (lex_pass or nli_pass)
            result.update({
                "semantic_score": semantic,
                "lexical_score": lexical,
                "nli_contradiction_score": contradiction,
                "similarity_pass": sim_pass,
            })
            print(
                f"  semantic={semantic:.3f} lexical={lexical:.3f} "
                f"nli_contradiction={contradiction:.3f} pass={sim_pass}",
                flush=True,
            )

        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(f"  saved -> {output_path}", flush=True)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()

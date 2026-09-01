"""Spot-VM book summarization runner with two interchangeable backends.

This consolidates the logic that previously lived in the Colab.ipynb and
Local_HF_Endpoint.ipynb notebooks into one resumable CLI you can run on a GCP
Spot VM (or any machine).

Two generation backends, selected with --mode:

  local        Load the model with transformers and run model.generate() on this
               machine. Use a GPU spot VM (e.g. g2-standard-* with an L4, or
               a2-* with an A100); CPU-only inference of a 7B model is far too
               slow for a full run. Add --load-in-4bit to fit a 7B model on a
               smaller GPU.

  hf-endpoint  Send every generation to a Hugging Face Inference Endpoint, exactly
               like the notebooks did. This machine only runs the tokenizer,
               chunking, GCS I/O, and (optionally) the validation models.

Both backends drive the same pipeline in summarizer.py and produce identical
output records, so you can A/B the two for quality and cost.

Environment (.env or shell):
    HF_TOKEN (or HF_API_TOKEN)        Hugging Face token; needed for gated models
                                      and for managing Inference Endpoints.
    GOOGLE_APPLICATION_CREDENTIALS    Path to the GCS service-account JSON key.
    GCS_BUCKET                        Bucket holding the uploaded book content.

Examples:
    # Local GPU run over the first 20 unprocessed books, 4-bit Qwen 7B.
    python AudioBooks/BookSummary/spot_vm_summarize.py --mode local \
        --load-in-4bit --max-books 20 --validate

    # Same pipeline, but generation runs on an HF Inference Endpoint.
    python AudioBooks/BookSummary/spot_vm_summarize.py --mode hf-endpoint \
        --max-books 20 --validate

    # Single book by Gutenberg id, no validation.
    python AudioBooks/BookSummary/spot_vm_summarize.py --mode local --gutenberg-id 1342
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import torch

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
    _semantic_similarity,
    _strip_prompt,
    load_model,
    summarize_book,
)
from AudioBooks.BookSummary.summary_utils import truncate_at_prompt_scaffold

# Both notebooks standardized on this instruct model; keep local and endpoint in
# sync so the two backends produce comparable output.
DEFAULT_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
GCS_BUCKET = os.environ.get("GCS_BUCKET", "gutenberg-books")

# Results live next to this script (AudioBooks/BookSummary/Artifacts) regardless
# of the working directory the command is launched from. The default output
# filename depends on the backend so a local run and an HF-endpoint run don't
# clobber each other; local matches the notebook's summary_results_local.jsonl.
ARTIFACTS_DIR = Path(__file__).resolve().parent / "Artifacts"
DEFAULT_OUTPUT_BY_MODE = {
    "local": str(ARTIFACTS_DIR / "summary_results_local.jsonl"),
    "hf-endpoint": str(ARTIFACTS_DIR / "summary_results.jsonl"),
}
DEFAULT_CHECKPOINT_DIR = str(ARTIFACTS_DIR / "checkpoints")

# Generation budgets aligned with Local_HF_Endpoint.ipynb so all backends match.
# These intentionally override summarizer.py's terser module defaults (6000 / 4096),
# which compressed story_so_far and chapter summaries.
NB_CHUNK_TOKENS = 4096
NB_REDUCE_INPUT_TOKENS = 8192

# Chunks shorter than this token count are heading-only and skipped before being
# sent to a remote endpoint (the local batch path tokenizes them cheaply anyway).
MIN_CHUNK_TOKENS = 20


def _hf_token() -> str | None:
    """Return the HF token, accepting either env var name used by the notebooks."""
    return os.environ.get("HF_TOKEN") or os.environ.get("HF_API_TOKEN")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("local", "hf-endpoint"),
        default="local",
        help="Generation backend: run the model on this machine or call an HF Inference Endpoint.",
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Hugging Face model id.")

    # Book selection.
    parser.add_argument("--book-id", type=int, help="Single internal books.id to summarize.")
    parser.add_argument("--gutenberg-id", type=int, help="Single Gutenberg id (resolved via the GCS id map).")
    parser.add_argument(
        "--max-books",
        type=int,
        default=None,
        help="Cap the number of unprocessed books to run (default: all). Ignored with --book-id/--gutenberg-id.",
    )

    # Book-level sharding for multi-GPU replicas (e.g. 2x L4 on g2-standard-24).
    # Each shard runs a disjoint set of WHOLE books, so the per-book rolling
    # story_so_far is never split across GPUs. See run_sharded_gpus.sh.
    parser.add_argument(
        "--num-shards",
        type=int,
        default=1,
        help="Total number of parallel replicas. Books are split round-robin across shards.",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="This replica's shard index in [0, num-shards). Output/checkpoint paths are auto-suffixed.",
    )

    # GCS.
    parser.add_argument("--bucket", default=GCS_BUCKET, help="GCS bucket with uploaded book content.")
    parser.add_argument(
        "--gcs-credentials",
        default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        metavar="KEY_FILE",
        help="Service-account JSON key (default: GOOGLE_APPLICATION_CREDENTIALS).",
    )

    # Chunking / generation budget (defaults mirror the notebooks).
    parser.add_argument("--chunk-tokens", type=int, default=NB_CHUNK_TOKENS)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--reduce-input-tokens", type=int, default=NB_REDUCE_INPUT_TOKENS)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--reduce-max-new-tokens", type=int, default=DEFAULT_REDUCE_MAX_NEW_TOKENS)
    parser.add_argument("--profile-max-new-tokens", type=int, default=1024)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        help="Local: GPU generation batch size. hf-endpoint: concurrent requests.",
    )
    parser.add_argument("--max-chapters", type=int, default=None, help="Optional chapter cap for smoke tests.")
    parser.add_argument("--max-chunks-per-chapter", type=int, default=None, help="Optional per-chapter chunk cap.")

    # Local backend only.
    parser.add_argument("--load-in-4bit", action="store_true", help="(local) Load the model in 4-bit (needs bitsandbytes).")

    # HF endpoint backend only.
    parser.add_argument("--endpoint-name", default="audiobook-summary-qwen25-7b")
    # Matches Local_HF_Endpoint.ipynb (Qwen2.5-7B on an A100). Use nvidia-a10g for a cheaper smaller GPU.
    parser.add_argument("--endpoint-instance-type", default="nvidia-a100")
    parser.add_argument("--endpoint-instance-size", default="x1")
    parser.add_argument("--endpoint-vendor", default="aws")
    parser.add_argument("--endpoint-region", default="us-east-1")
    parser.add_argument(
        "--no-create-endpoint",
        action="store_true",
        help="(hf-endpoint) Fail instead of creating the endpoint when it does not exist.",
    )

    # Validation (semantic / lexical / NLI vs. the book_desc reference summary).
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Score each summary against the book_desc reference (loads embedding + NLI models).",
    )
    parser.add_argument("--semantic-threshold", type=float, default=DEFAULT_SEMANTIC_THRESHOLD)
    parser.add_argument("--lexical-floor", type=float, default=DEFAULT_LEXICAL_FLOOR)
    parser.add_argument("--nli-contradiction-threshold", type=float, default=DEFAULT_NLI_CONTRADICTION_THRESHOLD)
    parser.add_argument("--embedding-model-id", default=DEFAULT_EMBEDDING_MODEL_ID)
    parser.add_argument("--nli-model-id", default=DEFAULT_NLI_MODEL_ID)

    # Output.
    parser.add_argument(
        "--output-path",
        default=None,
        help="JSONL file; each completed book is appended. Already-processed ids are skipped on resume. "
             "Defaults to summary_results_local.jsonl for --mode local and summary_results.jsonl for --mode hf-endpoint.",
    )
    parser.add_argument(
        "--checkpoint-dir",
        default=DEFAULT_CHECKPOINT_DIR,
        help="Per-book chapter checkpoints so an interrupted (preempted) run resumes mid-book.",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# HF Inference Endpoint backend                                               #
# --------------------------------------------------------------------------- #

def _truncate_at_prompt_marker(text: str) -> str:
    """Cut off any hallucinated continuation at a leaked prompt-scaffolding header.

    The local model.generate() path stops on EOS/no_repeat_ngram; TGI does not,
    so we replicate the notebooks' trim. It is keyword-bound (only known section
    labels), so a legitimate markdown heading like a character-name '### Oliver
    Twist' in a profile is preserved rather than truncated.
    """
    return truncate_at_prompt_scaffold(text)


def _setup_hf_endpoint_backend(args: argparse.Namespace, hf_token: str):
    """Create/attach an HF Inference Endpoint and monkeypatch summarizer to use it.

    Returns (model_handle, tokenizer, endpoint). summarizer._generate_text and
    summarizer._generate_batch are replaced with remote callers, so the rest of
    the pipeline is backend-agnostic.
    """
    from huggingface_hub import (
        InferenceClient,
        create_inference_endpoint,
        get_inference_endpoint,
    )
    from transformers import AutoTokenizer

    try:
        endpoint = get_inference_endpoint(args.endpoint_name, token=hf_token)
        print(f"Using existing endpoint: {args.endpoint_name}", flush=True)
    except Exception as exc:
        if args.no_create_endpoint:
            raise
        print(f"Creating endpoint {args.endpoint_name}: {exc}", flush=True)
        endpoint = create_inference_endpoint(
            args.endpoint_name,
            repository=args.model_id,
            framework="pytorch",
            task="text-generation",
            accelerator="gpu",
            vendor=args.endpoint_vendor,
            region=args.endpoint_region,
            instance_size=args.endpoint_instance_size,
            instance_type=args.endpoint_instance_type,
            type="authenticated",
            token=hf_token,
        )
    endpoint.wait()
    print(f"Endpoint URL: {endpoint.url}", flush=True)

    hf_client = InferenceClient(model=endpoint.url, token=hf_token)

    # Tokenizer runs locally only for prompt/chunk sizing.
    tokenizer = AutoTokenizer.from_pretrained(args.model_id, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token or "[PAD]"

    def _call_endpoint(prompt: str, *, max_new_tokens: int) -> str:
        for attempt in range(3):
            try:
                result = hf_client.text_generation(
                    prompt,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,  # greedy — matches the local backend for faithfulness
                    repetition_penalty=1.05,
                    return_full_text=False,
                )
                return _truncate_at_prompt_marker(result)
            except Exception as exc:
                if attempt == 2:
                    raise
                wait_seconds = 5 * (attempt + 1)
                print(f"Endpoint generation failed ({exc}); retrying in {wait_seconds}s", flush=True)
                time.sleep(wait_seconds)

    def _remote_generate_text(model, tokenizer, prompt, *, device, max_input_tokens, max_new_tokens):
        input_ids = tokenizer(
            prompt, add_special_tokens=False, truncation=True, max_length=max_input_tokens
        ).input_ids
        truncated_prompt = tokenizer.decode(input_ids, skip_special_tokens=True)
        return _strip_prompt(_call_endpoint(truncated_prompt, max_new_tokens=max_new_tokens))

    def _remote_generate_batch(model, tokenizer, prompts, *, device, max_input_tokens, max_new_tokens):
        results = [""] * len(prompts)
        work = [
            (i, p)
            for i, p in enumerate(prompts)
            if len(tokenizer(p, add_special_tokens=False).input_ids) >= MIN_CHUNK_TOKENS
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
                    model, tokenizer, prompt,
                    device=device,
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
    print("HF cloud generation configured. No local generation model was loaded.", flush=True)
    return hf_client, tokenizer, endpoint


# --------------------------------------------------------------------------- #
# Local backend                                                               #
# --------------------------------------------------------------------------- #

def _setup_local_backend(args: argparse.Namespace, hf_token: str):
    """Load the generation model on this machine.

    Returns (model, tokenizer, device). summarizer._generate_text/_generate_batch
    are left as their native model.generate() implementations.
    """
    if not torch.cuda.is_available():
        print(
            "WARNING: no CUDA GPU detected. Local generation of a multi-billion-parameter "
            "model on CPU is impractically slow — use a GPU spot VM or switch to --mode hf-endpoint.",
            flush=True,
        )
    print(f"stage: loading model {args.model_id} (4bit={args.load_in_4bit})", flush=True)
    tokenizer, model = load_model(args.model_id, args.load_in_4bit, hf_token)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if tokenizer.pad_token_id is not None:
        model.config.pad_token_id = tokenizer.pad_token_id
    return model, tokenizer, device


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def _resolve_book_ids(gcs_client, args: argparse.Namespace, processed_ids: set[int]) -> list[int]:
    """Resolve the explicit single book, or the list of unprocessed books to run."""
    if args.book_id is not None or args.gutenberg_id is not None:
        from AudioBooks.BookSummary.summarizer import _resolve_book_id

        return [_resolve_book_id(gcs_client, args.bucket, args.book_id, args.gutenberg_id)]

    id_map_blob = gcs_client.bucket(args.bucket).blob("book-desc/gutenberg-id-map.json")
    id_map = json.loads(id_map_blob.download_as_text(encoding="utf-8"))
    all_book_ids = sorted(set(id_map.values()))
    print(f"Found {len(all_book_ids)} books in GCS bucket {args.bucket!r}", flush=True)

    todo = [bid for bid in all_book_ids if bid not in processed_ids]
    # Round-robin book-level sharding across GPU replicas. The modulo is stable,
    # so on resume each shard still owns exactly the same books.
    if args.num_shards > 1:
        todo = [bid for i, bid in enumerate(todo) if i % args.num_shards == args.shard_index]
    if args.max_books is not None:
        todo = todo[: args.max_books]
    return todo


def main() -> None:
    args = parse_args()
    hf_token = _hf_token()

    if not args.bucket:
        raise ValueError("GCS bucket is not set. Pass --bucket or set GCS_BUCKET.")
    if not 0 <= args.shard_index < args.num_shards:
        raise ValueError(f"--shard-index must be in [0, {args.num_shards}); got {args.shard_index}.")

    # Mode-aware default output file (an explicit --output-path always wins).
    if args.output_path is None:
        args.output_path = DEFAULT_OUTPUT_BY_MODE[args.mode]

    output_path = Path(args.output_path)
    checkpoint_dir = Path(args.checkpoint_dir)
    # Give each replica its own output file and checkpoint dir so the shards never
    # race on writes or share resume state.
    if args.num_shards > 1:
        output_path = output_path.with_name(f"{output_path.stem}_shard{args.shard_index}{output_path.suffix}")
        checkpoint_dir = checkpoint_dir / f"shard{args.shard_index}"
        print(f"stage: shard {args.shard_index}/{args.num_shards} -> {output_path}", flush=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    print(f"stage: connecting to GCS bucket {args.bucket!r}", flush=True)
    gcs_client = _make_gcs_client(args.gcs_credentials)

    # Resume: skip ids already written to the output JSONL (single-book runs ignore this).
    processed_ids: set[int] = set()
    single_book = args.book_id is not None or args.gutenberg_id is not None
    if output_path.exists() and not single_book:
        for line in output_path.read_text(encoding="utf-8").splitlines():
            try:
                processed_ids.add(json.loads(line)["book_id"])
            except Exception:
                pass
    print(f"Already processed: {len(processed_ids)} books", flush=True)

    # Generation backend.
    if args.mode == "hf-endpoint":
        if not hf_token:
            raise RuntimeError("Set HF_TOKEN (or HF_API_TOKEN) — required to manage the Inference Endpoint.")
        model, tokenizer, endpoint = _setup_hf_endpoint_backend(args, hf_token)
        generation_endpoint = endpoint.url
        device = None
    else:
        model, tokenizer, device = _setup_local_backend(args, hf_token)
        generation_endpoint = str(device)

    # Validation models (optional — they load locally regardless of backend).
    embedding_tokenizer = embedding_model = None
    nli_tokenizer = nli_model = None
    if args.validate:
        print("stage: loading validation models (embedding + NLI)", flush=True)
        embedding_tokenizer, embedding_model = _load_embedding_model(args.embedding_model_id, hf_token)
        nli_tokenizer, nli_model = _load_nli_model(args.nli_model_id, hf_token)

    book_ids = _resolve_book_ids(gcs_client, args, processed_ids)
    print(f"Running: {len(book_ids)} books via {args.mode}", flush=True)

    for idx, book_id in enumerate(book_ids, start=1):
        print(f"\n[{idx}/{len(book_ids)}] book_id={book_id}", flush=True)
        try:
            book, book_desc_summary = _load_book_record(gcs_client, args.bucket, book_id)
        except FileNotFoundError as exc:
            print(f"  SKIP: {exc}", flush=True)
            continue

        mode_label = "verify" if (book_desc_summary and args.validate) else "generate"
        print(f"  mode={mode_label} title={book.title!r} category={book.category or '(unknown)'!r}", flush=True)
        print(f"  generation_endpoint={generation_endpoint}", flush=True)

        try:
            result = summarize_book(
                model,
                tokenizer,
                book,
                device=device,
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

        result["mode"] = mode_label
        result["book_desc_summary"] = book_desc_summary
        result["semantic_score"] = None
        result["lexical_score"] = None
        result["nli_contradiction_score"] = None
        result["similarity_pass"] = None

        print("\nFINAL SUMMARY", flush=True)
        print(result["final_summary"], flush=True)
        if result.get("character_profiles"):
            print(f"\nCHARACTER PROFILES  [category={result.get('category', '?')!r}]", flush=True)
            print(result["character_profiles"], flush=True)

        # Validation. A short blurb-style reference makes lexical overlap low even
        # for a faithful summary, so the pass rule is: semantic AND (lexical OR no-contradiction).
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

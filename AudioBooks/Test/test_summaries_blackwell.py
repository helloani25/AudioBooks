from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoModelForCausalLM, AutoModelForSequenceClassification, AutoTokenizer

from AudioBooks.Test.summary_utils import normalize_summary_text

try:
    from transformers import BitsAndBytesConfig
except ImportError:  # pragma: no cover
    BitsAndBytesConfig = None


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "Catalog" / "DB" / "gutenbergindex.db"
DEFAULT_MODEL_ID = "deepseek-ai/DeepSeek-R1-Distill-Llama-70B"

DEFAULT_CHUNK_TOKENS = 6000
DEFAULT_CHUNK_OVERLAP = 400
DEFAULT_REDUCE_INPUT_TOKENS = 4096
DEFAULT_MAX_NEW_TOKENS = 512
DEFAULT_REDUCE_MAX_NEW_TOKENS = 768
DEFAULT_SEMANTIC_THRESHOLD = 0.60
DEFAULT_LEXICAL_FLOOR = 0.35
DEFAULT_EMBEDDING_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_NLI_MODEL_ID = "tasksource/deberta-small-long-nli"
DEFAULT_NLI_CONTRADICTION_THRESHOLD = 0.50

CHAPTER_HEADING_RE = re.compile(
    r"^\s*(chapter|book|part|section)\s+([ivxlcdm0-9]+|\w+)?(?:\b.*)?$",
    re.IGNORECASE,
)


@dataclass
class BookRecord:
    book_id: int
    title: str
    authors: str
    text: str


@dataclass
class TextBlock:
    title: str
    text: str


def parse_args() -> argparse.Namespace:
    """Parse CLI options for book selection, model loading, chunking, and scoring."""
    parser = argparse.ArgumentParser(
        description="Chapter-by-chapter summarization harness for long books.",
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="Path to gutenbergindex.db.")
    parser.add_argument("--book-id", type=int, help="Internal books.id to summarize.")
    parser.add_argument("--gutenberg-id", type=int, help="Resolve the book by Gutenberg id.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Hugging Face model id.")
    parser.add_argument("--chunk-tokens", type=int, default=DEFAULT_CHUNK_TOKENS)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--reduce-input-tokens", type=int, default=DEFAULT_REDUCE_INPUT_TOKENS)
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS)
    parser.add_argument("--reduce-max-new-tokens", type=int, default=DEFAULT_REDUCE_MAX_NEW_TOKENS)
    parser.add_argument("--batch-size", type=int, default=2, help="Chunk summary batch size.")
    parser.add_argument("--max-chapters", type=int, default=None, help="Optional chapter cap for smoke tests.")
    parser.add_argument(
        "--max-chunks-per-chapter",
        type=int,
        default=None,
        help="Optional chunk cap per chapter for smoke tests.",
    )
    parser.add_argument("--load-in-4bit", action="store_true", help="Load the model in 4-bit mode.")
    parser.add_argument(
        "--semantic-threshold",
        "--similarity-threshold",
        dest="semantic_threshold",
        type=float,
        default=DEFAULT_SEMANTIC_THRESHOLD,
        help="Minimum semantic cosine score required to pass.",
    )
    parser.add_argument(
        "--lexical-floor",
        type=float,
        default=DEFAULT_LEXICAL_FLOOR,
        help="Minimum lexical similarity required unless there is no obvious contradiction.",
    )
    parser.add_argument(
        "--embedding-model-id",
        default=DEFAULT_EMBEDDING_MODEL_ID,
        help="Model id used to compute semantic embeddings.",
    )
    parser.add_argument(
        "--nli-model-id",
        default=DEFAULT_NLI_MODEL_ID,
        help="Model id used to detect contradiction with NLI.",
    )
    parser.add_argument(
        "--nli-contradiction-threshold",
        type=float,
        default=DEFAULT_NLI_CONTRADICTION_THRESHOLD,
        help="Maximum contradiction probability allowed before treating the pair as contradictory.",
    )
    parser.add_argument("--output-path", default=None, help="Optional JSON output path.")
    return parser.parse_args()


def _connect_db(db_path: str) -> sqlite3.Connection:
    """Open the SQLite catalog database with a busy timeout and row dict access."""
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _resolve_book_id(conn: sqlite3.Connection, book_id: int | None, gutenberg_id: int | None) -> int:
    """Resolve the internal books.id from either a direct id or a Gutenberg id."""
    if book_id is not None and gutenberg_id is not None:
        cur = conn.cursor()
        cur.execute("SELECT id FROM books WHERE id = ? AND gutenbergbookid = ?", (book_id, gutenberg_id))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"book-id {book_id} does not match gutenberg-id {gutenberg_id}")
        return int(row[0])

    if book_id is not None:
        return book_id

    if gutenberg_id is not None:
        cur = conn.cursor()
        cur.execute("SELECT id FROM books WHERE gutenbergbookid = ?", (gutenberg_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Could not resolve Gutenberg id {gutenberg_id}")
        return int(row[0])

    raise ValueError("Provide either --book-id or --gutenberg-id")


def _load_book_record(conn: sqlite3.Connection, book_id: int) -> BookRecord:
    """Load title, authors, and full text for one catalog book."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            b.id AS book_id,
            COALESCE((SELECT t.name FROM titles t WHERE t.bookid = b.id LIMIT 1), 'Untitled') AS title,
            COALESCE((
                SELECT GROUP_CONCAT(name, ', ')
                FROM (
                    SELECT DISTINCT a.name AS name
                    FROM authors a
                    JOIN book_authors ba ON ba.authorid = a.id
                    WHERE ba.bookid = b.id
                )
            ), '') AS authors,
            COALESCE(book_contents.clean_content, book_contents.raw_content, '') AS text
        FROM books b
        LEFT JOIN book_contents ON book_contents.bookid = b.id
        WHERE b.id = ?
        """,
        (book_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No book found for book id {book_id}")

    text = (row["text"] or "").strip()
    if not text:
        raise ValueError(f"No book text found for book id {book_id}")

    return BookRecord(
        book_id=int(row["book_id"]),
        title=str(row["title"] or "Untitled"),
        authors=str(row["authors"] or ""),
        text=text,
    )


def _load_book_desc_summary(conn: sqlite3.Connection, book_id: int) -> str | None:
    """Fetch the stored summary text for the same book, if it exists."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT summary
        FROM book_desc
        WHERE bookid = ?
        """,
        (book_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    summary = str(row["summary"] or "").strip()
    return summary or None


def _load_embedding_model(model_id: str, hf_token: str | None = None):
    """Load the embedding model used for semantic similarity scoring."""
    tokenizer = AutoTokenizer.from_pretrained(model_id, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token or "[PAD]"
    model = AutoModel.from_pretrained(model_id, token=hf_token, torch_dtype=torch.float32)
    model.eval()
    return tokenizer, model


def _load_nli_model(model_id: str, hf_token: str | None = None):
    """Load the NLI classifier used to detect contradiction."""
    tokenizer = AutoTokenizer.from_pretrained(model_id, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token or "[PAD]"
    model = AutoModelForSequenceClassification.from_pretrained(model_id, token=hf_token, torch_dtype=torch.float32)
    model.eval()
    return tokenizer, model


def _has_heading(line: str) -> bool:
    """Return True when a line looks like a chapter or section heading."""
    stripped = line.strip()
    return bool(stripped and CHAPTER_HEADING_RE.match(stripped))


def split_into_chapters(text: str) -> list[TextBlock]:
    """Split raw book text into heading-based chapter blocks, or one block if none exist."""
    lines = text.splitlines()
    if not any(_has_heading(line) for line in lines):
        return [TextBlock(title="Full Text", text=text.strip())]

    blocks: list[TextBlock] = []
    current_title = "Front Matter"
    current_lines: list[str] = []
    saw_heading = False

    for line in lines:
        if _has_heading(line):
            saw_heading = True
            if current_lines:
                blocks.append(TextBlock(title=current_title, text="\n".join(current_lines).strip()))
            current_title = line.strip()
            current_lines = [line]
            continue

        current_lines.append(line)

    if current_lines:
        blocks.append(TextBlock(title=current_title, text="\n".join(current_lines).strip()))

    cleaned = [block for block in blocks if block.text]
    if not saw_heading or not cleaned:
        return [TextBlock(title="Full Text", text=text.strip())]
    return cleaned


def chunk_by_tokens(tokenizer, text: str, chunk_tokens: int, overlap_tokens: int) -> list[str]:
    """Split text into token windows with optional overlap, preserving decoding back to text."""
    token_ids = tokenizer(text, add_special_tokens=False).input_ids
    if not token_ids:
        return []
    if len(token_ids) <= chunk_tokens:
        return [text]

    step = max(1, chunk_tokens - overlap_tokens)
    chunks: list[str] = []
    for start in range(0, len(token_ids), step):
        window = token_ids[start : start + chunk_tokens]
        if not window:
            break
        chunks.append(tokenizer.decode(window, skip_special_tokens=True))
    return chunks


def load_model(model_id: str, load_in_4bit: bool, hf_token: str | None = None):
    """Load the generation model and optionally quantize it to 4-bit."""
    tokenizer = AutoTokenizer.from_pretrained(model_id, token=hf_token)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token or tokenizer.unk_token or "[PAD]"

    model_kwargs: dict[str, object] = {"token": hf_token}
    if torch.cuda.is_available():
        model_kwargs["device_map"] = "auto"
        model_kwargs["torch_dtype"] = torch.bfloat16
        model_kwargs["attn_implementation"] = "flash_attention_2"
    else:
        model_kwargs["torch_dtype"] = torch.float32

    if load_in_4bit:
        if BitsAndBytesConfig is None:
            raise ImportError("bitsandbytes support is not available in this environment")
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)
    model.eval()
    return tokenizer, model


def _strip_prompt(decoded: str) -> str:
    """Remove the prompt prefix from model output and normalize the summary text."""
    if "### Summary:" in decoded:
        decoded = decoded.split("### Summary:", 1)[1]
    return normalize_summary_text(decoded)


def _lexical_similarity(left: str, right: str) -> float:
    """Compute lexical overlap with a normalized SequenceMatcher ratio."""
    left_norm = normalize_summary_text(left)
    right_norm = normalize_summary_text(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _mean_pooling(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    """Average token embeddings while masking out padding tokens."""
    mask = attention_mask.unsqueeze(-1).type_as(last_hidden_state)
    summed = (last_hidden_state * mask).sum(dim=1)
    counts = mask.sum(dim=1).clamp(min=1e-9)
    return summed / counts


def _semantic_similarity(
    embedding_tokenizer,
    embedding_model,
    left: str,
    right: str,
) -> float:
    """Compute cosine similarity between sentence embeddings for two texts."""
    left_norm = normalize_summary_text(left)
    right_norm = normalize_summary_text(right)
    if not left_norm or not right_norm:
        return 0.0

    inputs = embedding_tokenizer(
        [left_norm, right_norm],
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512,
    )
    device = next(embedding_model.parameters()).device
    inputs = inputs.to(device)

    with torch.inference_mode():
        outputs = embedding_model(**inputs)
        embeddings = _mean_pooling(outputs.last_hidden_state, inputs["attention_mask"])
        embeddings = F.normalize(embeddings, p=2, dim=1)

    return float(torch.sum(embeddings[0] * embeddings[1]).item())


def _label_index(model, label_name: str) -> int:
    """Find the classifier label index for a label name such as contradiction."""
    target = label_name.lower()
    for idx, label in getattr(model.config, "id2label", {}).items():
        if target in str(label).lower():
            return int(idx)
    raise ValueError(f"Could not find label {label_name!r} in model labels: {getattr(model.config, 'id2label', {})}")


def _nli_contradiction_probability(
    nli_tokenizer,
    nli_model,
    premise: str,
    hypothesis: str,
) -> float:
    """Score one directional NLI pair and return the contradiction probability."""
    premise_norm = normalize_summary_text(premise)
    hypothesis_norm = normalize_summary_text(hypothesis)
    if not premise_norm or not hypothesis_norm:
        return 0.0

    inputs = nli_tokenizer(
        premise_norm,
        hypothesis_norm,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=min(getattr(nli_tokenizer, "model_max_length", 512) or 512, 1024),
    )
    device = next(nli_model.parameters()).device
    inputs = inputs.to(device)

    with torch.inference_mode():
        logits = nli_model(**inputs).logits[0]
        probabilities = torch.softmax(logits, dim=-1)

    contradiction_index = _label_index(nli_model, "contradiction")
    return float(probabilities[contradiction_index].item())


def _max_nli_contradiction(
    nli_tokenizer,
    nli_model,
    left: str,
    right: str,
) -> float:
    """Check contradiction in both directions and keep the stronger signal."""
    forward = _nli_contradiction_probability(nli_tokenizer, nli_model, left, right)
    reverse = _nli_contradiction_probability(nli_tokenizer, nli_model, right, left)
    return max(forward, reverse)


def _generate_text(
    model,
    tokenizer,
    prompt: str,
    *,
    device: torch.device,
    max_input_tokens: int,
    max_new_tokens: int,
) -> str:
    """Generate one normalized completion for a prompt."""
    inputs = tokenizer(
        prompt,
        return_tensors="pt",
        padding=False,
        truncation=True,
        max_length=max_input_tokens,
    )
    inputs = inputs.to(device)

    with torch.inference_mode():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            num_beams=1,
            repetition_penalty=1.05,
            no_repeat_ngram_size=3,
        )

    decoded = tokenizer.decode(output_ids[0], skip_special_tokens=True)
    return _strip_prompt(decoded)


def _generate_batch(
    model,
    tokenizer,
    prompts: list[str],
    *,
    device: torch.device,
    max_input_tokens: int,
    max_new_tokens: int,
) -> list[str]:
    """Generate a normalized completion for each prompt in a batch."""
    if not prompts:
        return []

    inputs = tokenizer(
        prompts,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=max_input_tokens,
    )
    inputs = inputs.to(device)

    with torch.inference_mode():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            num_beams=1,
            repetition_penalty=1.05,
            no_repeat_ngram_size=3,
        )

    return [_strip_prompt(text) for text in tokenizer.batch_decode(output_ids, skip_special_tokens=True)]


def build_chunk_prompt(book_title: str, section_title: str, chunk_index: int, chunk_total: int, chunk_text: str) -> str:
    """Build the chapter-chunk prompt used for local chunk summaries."""
    return (
        "### Instruction: Summarize this chapter excerpt faithfully.\n"
        f"### Book: {book_title}\n"
        f"### Section: {section_title}\n"
        f"### Chunk: {chunk_index}/{chunk_total}\n"
        "### Constraints: Keep names, events, and chronology accurate. Do not invent details.\n"
        "### Excerpt:\n"
        f"{chunk_text}\n"
        "### Summary:"
    )


def build_reduction_prompt(book_title: str, scope_label: str, combined_text: str) -> str:
    """Build the reduction prompt used to merge chunk summaries into a larger summary."""
    return (
        "### Instruction: Combine the following summaries into one coherent summary.\n"
        f"### Book: {book_title}\n"
        f"### Scope: {scope_label}\n"
        "### Constraints: Stay factual, keep the major plot points, and do not add new details.\n"
        "### Input Summaries:\n"
        f"{combined_text}\n"
        "### Summary:"
    )


def reduce_texts(
    model,
    tokenizer,
    texts: list[str],
    *,
    book_title: str,
    scope_label: str,
    device: torch.device,
    reduce_input_tokens: int,
    reduce_max_new_tokens: int,
) -> str:
    """Recursively reduce a list of summaries until the text fits the reduce window."""
    current = [normalize_summary_text(text) for text in texts if text and text.strip()]
    if not current:
        return ""
    if len(current) == 1:
        return current[0]

    while True:
        combined = "\n\n".join(f"{index + 1}. {text}" for index, text in enumerate(current))
        token_count = len(tokenizer(combined, add_special_tokens=False).input_ids)
        if token_count <= reduce_input_tokens or len(current) == 1:
            prompt = build_reduction_prompt(book_title, scope_label, combined)
            return _generate_text(
                model,
                tokenizer,
                prompt,
                device=device,
                max_input_tokens=reduce_input_tokens,
                max_new_tokens=reduce_max_new_tokens,
            )

        next_level: list[str] = []
        blocks = chunk_by_tokens(tokenizer, combined, reduce_input_tokens, 0)
        for block_index, block in enumerate(blocks, start=1):
            prompt = build_reduction_prompt(
                book_title,
                f"{scope_label} block {block_index}/{len(blocks)}",
                block,
            )
            next_level.append(
                _generate_text(
                    model,
                    tokenizer,
                    prompt,
                    device=device,
                    max_input_tokens=reduce_input_tokens,
                    max_new_tokens=reduce_max_new_tokens,
                )
            )
        current = next_level


def summarize_book(
    model,
    tokenizer,
    book: BookRecord,
    *,
    device: torch.device,
    chunk_tokens: int,
    chunk_overlap: int,
    reduce_input_tokens: int,
    max_new_tokens: int,
    reduce_max_new_tokens: int,
    batch_size: int,
    max_chapters: int | None,
    max_chunks_per_chapter: int | None,
) -> dict:
    """Summarize a book chapter-by-chapter, then reduce the chapter summaries to one final summary."""
    print(f"stage: splitting book_id={book.book_id} into chapters", flush=True)
    chapters = split_into_chapters(book.text)
    if max_chapters is not None:
        chapters = chapters[:max_chapters]
    print(f"stage: found {len(chapters)} chapter blocks", flush=True)

    chapter_results: list[dict] = []
    for chapter_index, chapter in enumerate(chapters, start=1):
        print(
            f"stage: chapter {chapter_index}/{len(chapters)} title={chapter.title!r} length={len(chapter.text)}",
            flush=True,
        )
        chunks = chunk_by_tokens(tokenizer, chapter.text, chunk_tokens, chunk_overlap)
        if max_chunks_per_chapter is not None:
            chunks = chunks[:max_chunks_per_chapter]
        print(f"stage: chapter {chapter_index} chunk_count={len(chunks)}", flush=True)

        chunk_prompts = [
            build_chunk_prompt(book.title, chapter.title, chunk_pos, len(chunks), chunk_text)
            for chunk_pos, chunk_text in enumerate(chunks, start=1)
        ]

        chunk_summaries: list[str] = []
        for start in range(0, len(chunk_prompts), max(1, batch_size)):
            batch = chunk_prompts[start : start + max(1, batch_size)]
            batch_summaries = _generate_batch(
                model,
                tokenizer,
                batch,
                device=device,
                max_input_tokens=chunk_tokens + 512,
                max_new_tokens=max_new_tokens,
            )
            chunk_summaries.extend(batch_summaries)
            print(
                f"stage: chapter {chapter_index} summarized_chunks={len(chunk_summaries)}/{len(chunk_prompts)}",
                flush=True,
            )

        chapter_summary = reduce_texts(
            model,
            tokenizer,
            chunk_summaries,
            book_title=book.title,
            scope_label=f"chapter {chapter_index}: {chapter.title}",
            device=device,
            reduce_input_tokens=reduce_input_tokens,
            reduce_max_new_tokens=reduce_max_new_tokens,
        )
        chapter_results.append(
            {
                "chapter_index": chapter_index,
                "chapter_title": chapter.title,
                "chunk_count": len(chunks),
                "summary": chapter_summary,
            }
        )

    final_summary = reduce_texts(
        model,
        tokenizer,
        [chapter["summary"] for chapter in chapter_results],
        book_title=book.title,
        scope_label="full book",
        device=device,
        reduce_input_tokens=reduce_input_tokens,
        reduce_max_new_tokens=reduce_max_new_tokens,
    )

    return {
        "book_id": book.book_id,
        "title": book.title,
        "authors": book.authors,
        "chapter_count": len(chapter_results),
        "chapters": chapter_results,
        "final_summary": final_summary,
    }


def main() -> None:
    """Run the full summarization pipeline and optionally persist the output JSON."""
    args = parse_args()
    hf_token = os.getenv("HF_TOKEN")

    conn = _connect_db(args.db_path)
    book_id = _resolve_book_id(conn, args.book_id, args.gutenberg_id)
    book = _load_book_record(conn, book_id)
    book_desc_summary = _load_book_desc_summary(conn, book_id)
    conn.close()

    print(f"stage: loading model {args.model_id}", flush=True)
    tokenizer, model = load_model(args.model_id, args.load_in_4bit, hf_token)
    embedding_tokenizer, embedding_model = _load_embedding_model(args.embedding_model_id, hf_token)
    nli_tokenizer, nli_model = _load_nli_model(args.nli_model_id, hf_token)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if tokenizer.pad_token_id is not None:
        model.config.pad_token_id = tokenizer.pad_token_id

    print(
        f"stage: loaded book_id={book.book_id} title={book.title!r} authors={book.authors!r} "
        f"text_chars={len(book.text)}",
        flush=True,
    )

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
    )

    print("stage: final summary", flush=True)
    print(result["final_summary"], flush=True)

    semantic_score = None
    lexical_score = None
    similarity_pass = None
    contradiction_score = None
    nli_no_contradiction = None
    if book_desc_summary:
        semantic_score = _semantic_similarity(embedding_tokenizer, embedding_model, result["final_summary"], book_desc_summary)
        lexical_score = _lexical_similarity(result["final_summary"], book_desc_summary)
        contradiction_score = _max_nli_contradiction(nli_tokenizer, nli_model, result["final_summary"], book_desc_summary)
        semantic_pass = semantic_score >= args.semantic_threshold
        lexical_pass = lexical_score >= args.lexical_floor
        nli_no_contradiction = contradiction_score < args.nli_contradiction_threshold
        similarity_pass = semantic_pass and (lexical_pass or nli_no_contradiction)
        print(
            "stage: similarity "
            f"semantic={semantic_score:.3f} threshold={args.semantic_threshold:.2f} "
            f"lexical={lexical_score:.3f} floor={args.lexical_floor:.2f} "
            f"nli_contradiction={contradiction_score:.3f} "
            f"nli_threshold={args.nli_contradiction_threshold:.2f} "
            f"pass={similarity_pass}",
            flush=True,
        )
    else:
        print("stage: no book_desc summary found; skipping similarity check", flush=True)

    result["book_desc_summary"] = book_desc_summary
    result["semantic_score"] = semantic_score
    result["lexical_score"] = lexical_score
    result["semantic_threshold"] = args.semantic_threshold
    result["lexical_floor"] = args.lexical_floor
    result["nli_contradiction_score"] = contradiction_score
    result["nli_contradiction_threshold"] = args.nli_contradiction_threshold
    result["nli_no_contradiction"] = nli_no_contradiction
    result["similarity_pass"] = similarity_pass
    result["similarity_score"] = semantic_score
    result["similarity_threshold"] = args.semantic_threshold

    if args.output_path:
        output_path = Path(args.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"stage: wrote {output_path}", flush=True)


if __name__ == "__main__":
    main()

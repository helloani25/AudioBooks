# Audiobook Summarization — How It Works

This document explains what the Colab notebook (`Colab.ipynb`) does and why each technical decision was made. It is a conceptual companion to the notebook, not a code reference.

---

## Overview

The notebook runs a hierarchical summarization pipeline over every book in a GCS bucket. For each book it generates a final summary, and — when a reference summary exists — scores the output for semantic similarity, lexical overlap, and factual consistency. Results stream to a JSONL file on Google Drive so runs can be interrupted and resumed.

The pipeline never touches a local database at runtime. All book content and metadata are served from Google Cloud Storage, uploaded in advance by two local scripts.

---

## Before Opening Colab: Data Upload

The local SQLite database holds both the cleaned book text (`book_contents`) and the reference metadata and summaries (`book_desc`). Two upload scripts push this data to GCS so the Colab environment has everything it needs without cloning hundreds of gigabytes.

`book_contents_upload.py` writes each book's `clean_content` as a `.txt` or `.html` blob under `gs://<bucket>/book-contents/<bookid>/`. `book_desc_upload.py` writes per-book JSON (title, author, reference summary) under `gs://<bucket>/book-desc/<bookid>.json`, and also writes a single `gutenberg-id-map.json` index that maps every Gutenberg ID to its internal book ID. The notebook uses that index to enumerate all available books at startup.

Both scripts track run state in the local SQLite database so they are safe to interrupt and resume.

---

## Cell 1: Install Dependencies

The core stack is **Transformers** (model loading and inference), **Accelerate** (multi-device dispatch and memory management), **BitsAndBytes** (4-bit quantization), and **Flash Attention 2** (efficient attention kernels).

BitsAndBytes is uninstalled and reinstalled to guarantee version ≥ 0.46.1 because earlier versions had bugs with 4-bit inference on newer CUDA drivers. Flash Attention 2 is optional — the notebook falls back to standard attention if it is not available — but it is strongly recommended on A100 or H100 instances because it can halve memory usage and roughly double throughput for long sequences.

---

## Cell 2: Verify Installs

Both packages are checked explicitly because their CUDA extensions are compiled at install time, and a partial install (wrong CUDA version, missing compiler) silently fails. The verify cells surface these failures before any model weights are downloaded.

---

## Cell 3: Verify GPU

`nvidia-smi` confirms that Colab has assigned a GPU before the multi-gigabyte model download begins. Free Colab T4 instances have 15 GB of VRAM. The quantized 7B model and two smaller auxiliary models (embedding and NLI) fit in that budget, but only with 4-bit quantization enabled.

---

## Cell 4: Mount Drive and Clone Repo

The repo is cloned at runtime using a GitHub personal access token from Colab Secrets. Storing the token as a secret rather than in the notebook prevents it from appearing in commit history or shared notebook URLs.

Google Drive is mounted so that summarization results can be written to `MyDrive/summary_results.jsonl`. If the Colab runtime is recycled mid-run, the Drive file persists and the next run resumes from the last saved book.

---

## Cell 5: Config

This cell sets two values that flow through every subsequent cell: `MODEL_ID` and `GCS_BUCKET`. Changing the model here changes what is loaded in Cell 6. The Google credentials path is set as an environment variable so the GCS client picks it up automatically without being passed through every function call.

---

## Cell 6: Setup — Why 4-bit and Why NF4

This cell downloads the generation model, the embedding model, and the NLI model. It runs once per Colab session. Understanding the quantization options is important because they directly control what can run on a free GPU.

### Why 4-bit quantization

A 7-billion-parameter model stores weights as 16-bit floats at full precision: 7 × 10⁹ × 2 bytes ≈ 14 GB. A T4 GPU has 15 GB of VRAM, leaving almost no room for activations or the KV cache. 4-bit quantization represents each weight in 4 bits instead of 16, reducing the memory footprint to roughly 3.5 GB. This leaves 11 GB free for the forward pass, the two auxiliary models, and the attention cache during inference.

The accuracy cost is small for summarization. The model still reads weights at full precision during computation — they are dequantized on the fly to `bfloat16` for each matrix multiply — so the forward pass quality is close to native bf16. The quantization only affects stored weight size, not compute precision.

### Why NF4

Standard 4-bit integers divide a range into 16 evenly spaced values. Neural network weights, however, are not uniformly distributed — they cluster near zero and thin out toward the extremes, roughly following a normal (bell-curve) distribution. Uniform spacing wastes resolution at the tails and under-represents the dense central region.

NormalFloat4 (NF4), introduced in the QLoRA paper, solves this by placing quantization levels at equal-probability quantiles of the normal distribution rather than at equal intervals. More levels are clustered near zero where most weights live, and fewer at the edges. For the same 4-bit budget, NF4 introduces less quantization error than INT4 on normally-distributed weights.

### Why `bnb_4bit_use_double_quant`

When blocks of weights are quantized, a scaling constant is stored per block to allow dequantization. With double quantization, those constants themselves are quantized (from float32 to 8-bit). This saves approximately 0.4 bits per parameter on top of the 4-bit compression, at essentially no accuracy cost.

### Why `llm_int8_enable_fp32_cpu_offload`

Certain layers — typically the embedding table and the language model head — are structurally unsuitable for 4-bit quantization. With a 7B model, these layers alone can occupy 500 MB – 1 GB of VRAM. On a memory-constrained instance this can trigger an out-of-memory error even though the attention and MLP blocks fit.

This flag allows those specific layers to live in CPU RAM and be computed in float32. The GPU handles all the quantized transformer blocks; the CPU handles only the lightweight embedding lookups. The extra host–device transfer adds a small amount of latency per forward pass, but prevents crashes on instances where VRAM is tight.

### Flash Attention 2

Standard scaled-dot-product attention materializes the full N×N attention matrix in VRAM, where N is the sequence length. For a 2048-token context this is manageable, but it still consumes several gigabytes. Flash Attention 2 computes attention in tiles, keeping only one tile in SRAM at a time, and never materializing the full matrix. Peak VRAM usage drops by roughly 10× for long sequences, and throughput roughly doubles because memory bandwidth — not arithmetic — is the bottleneck. The notebook checks for `flash_attn` at import time and falls back to standard attention if it is not installed.

### Loading the auxiliary models

Two smaller models are loaded alongside the generation model:

- **Embedding model** (`all-MiniLM-L6-v2`, 22M parameters): encodes text into dense 384-dimensional vectors. Used to measure semantic similarity between the generated and reference summaries.
- **NLI model** (`deberta-small-long-nli`, ~70M parameters): a natural language inference classifier that predicts whether two texts are in entailment, neutral, or contradiction. Used to detect factual inconsistency between the generated and reference summaries.

Both are small enough to coexist with the 7B model in VRAM.

---

## Cell 7: Batch Summarization Loop

### How summarization works

A full book can be hundreds of thousands of tokens — far beyond the context window of any LLM. The pipeline uses a hierarchical map-reduce strategy to summarize books of arbitrary length.

**Step 1 — Chapter splitting.** The book text is scanned line-by-line for headings matching patterns like "Chapter 1", "Book II", "Part Three", or "Section IV". Each heading starts a new block. If no headings are found the entire text becomes one block. This gives a natural decomposition that mirrors how a human would approach the book.

**Step 2 — Chunk-level summarization (map).** Each chapter block is tokenized and split into overlapping windows of 2048 tokens with 400 tokens of overlap between adjacent windows. The overlap carries context from the end of one chunk into the beginning of the next, reducing boundary artifacts in summaries. The model receives a structured prompt containing the book title, section name, chunk position (e.g. "chunk 2/5"), and the excerpt, and generates a short summary for that chunk. Chunks within a chapter are processed in batches to amortize the overhead of GPU kernel launches.

**Step 3 — Chapter-level reduction (reduce).** The chunk summaries for a chapter are concatenated and counted. If they fit within the 2048-token reduce window, a single reduction call merges them into one chapter summary. If they are still too long, they are chunked again and reduced recursively until the result fits. This guarantees that chapter summaries are always short enough to feed into the next level.

**Step 4 — Book-level reduction (reduce again).** All chapter summaries are gathered and reduced with the same recursive approach, producing a single final summary for the entire book.

### Verify mode vs generate mode

Every book is processed in one of two modes, chosen automatically based on whether a trusted reference summary already exists.

**Generate mode** is the baseline. The pipeline runs the full map-reduce summarization and writes the result to `summary_results.jsonl`. There is nothing to compare the output against, so all scoring fields (`semantic_score`, `lexical_score`, `nli_contradiction_score`, `similarity_pass`) are written as `null`. Generate mode is how the system builds coverage: books that have never been summarized before, obscure titles not in the CMU dataset, foreign-language works, and anything the description backfill did not reach all go through this path. The output is a generated summary with no quality signal attached.

**Verify mode** activates when the `book-desc` JSON for the book contains a non-empty `summary` field — sourced from either the CMU Book Summary Dataset (which covers ~16,000 English-language novels) or the Gutenberg description backfill (which extracts summaries from the book's RDF metadata or ebook page). In this mode the pipeline still generates a fresh summary independently, then scores the generated text against the reference. The reference is treated as ground truth about what the book is actually about.

The distinction matters because verify mode gives a concrete signal about model quality. If the generated summary scores well against the CMU reference for, say, *Pride and Prejudice*, that is evidence the pipeline is working correctly for that class of book. If it scores poorly, that flags a problem worth investigating: the book content may still have license text contaminating early chapters, the chapter split may have gone wrong, or the model may have fixated on front matter rather than the narrative. Generate-mode books produce output but give no such signal — they extend coverage without telling you whether the coverage is good.

Over time, as more reference summaries are populated via the description backfill scripts, more books migrate from generate mode to verify mode automatically on the next run.

### Scoring

Three independent scores are computed and combined:

**Semantic similarity** encodes both the generated and reference summaries using the embedding model and computes the cosine similarity between the resulting vectors. A high score (≥ 0.60) means the two texts convey similar overall meaning even if the wording differs. This catches synonymy and paraphrase but can miss subtle factual errors.

**Lexical similarity** uses Python's `SequenceMatcher` to compute the overlap ratio between normalized versions of both texts. A floor of 0.35 catches cases where the semantic score is artificially high but the generated text shares almost no actual words with the reference — often a sign that the model has drifted to a different topic.

**NLI contradiction** runs the two texts through the DeBERTa NLI model in both directions (generated → reference and reference → generated) and takes the higher contradiction probability. If either direction exceeds 0.50, the pair is flagged as contradictory. This is the most precise of the three measures for catching hallucinations: a summary that sounds fluent and covers the right themes but states a fact opposite to the reference will score high on semantic similarity but high on NLI contradiction.

A book passes if: semantic ≥ 0.60 **and** (lexical ≥ 0.35 **or** NLI contradiction < 0.50). The lexical floor can be bypassed when the NLI model finds no contradiction, accommodating paraphrase-heavy summaries that are factually sound.

### Resume mechanism

Results are appended line-by-line to `MyDrive/summary_results.jsonl`. On every run, the cell reads the existing file and collects all `book_id` values that have already been processed. Those books are skipped. This means the cell is safe to interrupt mid-run and safe to re-run after a Colab session expires: it always picks up from where it left off without re-summarizing completed books.

`MAX_BOOKS` controls how many books to process per run. The default of 20 is a smoke test (explained below). Setting it to `None` processes all remaining books and is intended for paid runtimes with longer session lifetimes.

### Smoke tests

A smoke test is a deliberately small run designed to detect obvious failures before committing to a long job. The name comes from hardware testing: if you power on a circuit and it smokes, you know immediately that something is badly wrong — you do not need to run the full test suite to discover it.

In this pipeline, `MAX_BOOKS = 20` is the smoke test setting. It is small enough to complete in under an hour on a free T4 and large enough to exercise every part of the system: GCS credential resolution, model loading, chapter splitting, chunking, multi-level reduction, the embedding and NLI scoring pass, and the JSONL write. If any of those components are broken — wrong bucket name, expired credentials, a code change that broke the summarizer import, a model that won't load in 4-bit — the failure surfaces in minutes rather than after hours of GPU time.

A useful smoke test also targets a mix of book types: short books that fit in a single chunk, long books that need recursive reduction, books with chapters, books without chapters, and books in both verify and generate mode. The 20 books drawn from the front of the sorted id list will naturally cover some of this variety. If all 20 complete without error and at least the verify-mode books score plausibly, the full run is safe to launch.

The opposite of a smoke test is a **full run** (`MAX_BOOKS = None`). A full run is not something to iterate on — it is the production job. You run it once the smoke test has passed, on a paid runtime where you can afford hours of compute, and you leave it running. The resume mechanism means a full run can span multiple Colab sessions: start it, let it process as many books as the session allows, and rerun the cell the next day to continue from where it stopped.

---

## Data Flow Summary

```
Local SQLite
    │
    ├─ book_contents_upload.py ──► GCS book-contents/<bookid>/clean_content.{txt,html}
    └─ book_desc_upload.py     ──► GCS book-desc/<bookid>.json
                                   GCS book-desc/gutenberg-id-map.json
                                        │
                                   Colab notebook
                                        │
                                   load id map → enumerate all book IDs
                                   for each book:
                                     load content + reference summary from GCS
                                     split into chapters
                                     chunk → summarize → reduce → final summary
                                     score against reference (verify mode)
                                     append to MyDrive/summary_results.jsonl
```

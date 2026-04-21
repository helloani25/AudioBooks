# Project Gutenberg Book Downloader

This script downloads and queries books from Project Gutenberg using the `gutenbergpy` library.

## Usage

Run the script directly:

```bash
python metadata.py
```

## What It Does

1. Downloads and caches Project Gutenberg's metadata (first run may take several minutes)
2. Queries for Science Fiction books in plain text format
3. Retrieves and displays the first 10 matching books with their metadata

## Note

SSL certificate verification is disabled by default to avoid certificate validation issues. This is safe for downloading public domain books from Project Gutenberg.
 python Guttenberg.py --no-verify 
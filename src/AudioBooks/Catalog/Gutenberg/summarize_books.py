"""
Summarize books from Project Gutenberg. Split by chapter and for each chapter split
for every 10,000 words using gemma

"""

import ollama
from google.cloud import storage

# Configuration
PROJECT_ID = "gen-lang-client-0910392250"
BUCKET_NAME = "gutenberg-books"
MODEL = "gemma2:27b"  # Your M4 Max will handle this beautifully
CHUNK_SIZE = 25000    # Roughly 5,000 - 6,000 words per chunk

client = storage.Client(project=PROJECT_ID)
bucket = client.bucket(BUCKET_NAME)

def stream_and_summarize(blob_name):
    blob = bucket.blob(blob_name)

    # 1. Open a streaming reader
    # This reads the file directly from GCS without downloading the whole thing
    with blob.open("r") as f:
        chunk_count = 0

        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break  # End of book

            chunk_count += 1
            print(f"Processing {blob_name} - Chunk {chunk_count}...")

            # 2. Send chunk to your M4 Max GPU via Ollama
            response = ollama.chat(model=MODEL, messages=[
                {
                    'role': 'system',
                    'content': 'You are a literary assistant. Summarize this text chunk concisely.'
                },
                {
                    'role': 'user',
                    'content': f"Text: {chunk}"
                },
            ])

            summary_part = response['message']['content']

            # 3. Handle the output (Save to a local file or upload back to GCS)
            save_summary_locally(blob_name, chunk_count, summary_part)


import ollama
from google.cloud import storage

# 1. Setup GCS
client = storage.Client(project="gen-lang-client-0910392250")
bucket = client.bucket("gutenberg-books")

def summarize_book(blob_name):
    # Download book text
    blob = bucket.blob(blob_name)
    text = blob.download_as_text()

    # Since you are chunking, let's take a 10,000 character chunk as an example
    # You can loop through the whole text in chunks
    chunk = text[:10000]

    # 2. Call your local M4 Max GPU
    response = ollama.chat(model='gemma2:27b', messages=[
      {
        'role': 'user',
        'content': f'Summarize the following book chapter clearly: {chunk}',
      },
    ])

    return response['message']['content']

# Example usage
summary = summarize_book("example_book.txt")
print(f"Summary: {summary}")
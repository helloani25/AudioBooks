from __future__ import annotations

import io
import json

from AudioBooks.Catalog.Gutenberg import summarize_books


class _FakeBlob:
    def __init__(self, text: str):
        self.text = text

    def open(self, *_args, **_kwargs):
        return io.StringIO(self.text)


class _FakeBucket:
    def __init__(self, text: str):
        self.text = text

    def blob(self, _name: str):
        return _FakeBlob(self.text)


def test_stream_and_summarize_chunks_and_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(
        summarize_books,
        "summarize_chunk",
        lambda text, model: f"{model}:{text}",
    )
    output_path = tmp_path / "summaries.jsonl"

    summaries = summarize_books.stream_and_summarize(
        _FakeBucket("abcdefghij"),
        "book.txt",
        model="test-model",
        chunk_size=4,
        output_path=output_path,
    )

    assert summaries == [
        "test-model:abcd",
        "test-model:efgh",
        "test-model:ij",
    ]
    records = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [record["chunk"] for record in records] == [1, 2, 3]
    assert all(record["blob"] == "book.txt" for record in records)

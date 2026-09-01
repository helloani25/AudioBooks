from dataclasses import dataclass

@dataclass
class AudioBook:
    title: str
    author: list[str]
    genre: str
    year: int
    manifest_location: str


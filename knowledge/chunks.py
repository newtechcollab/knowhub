import hashlib


def text_hash(text: str) -> str:
    return hashlib.sha256(
        (text or "").encode("utf-8")
    ).hexdigest()


def split_into_chunks(
    text: str,
    chunk_size: int = 800,
    overlap: int = 100,
):
    text = (text or "").strip()

    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = min(
            start + chunk_size,
            len(text),
        )

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = max(
            0,
            end - overlap,
        )

    return chunks

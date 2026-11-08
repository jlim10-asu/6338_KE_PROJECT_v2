"""Local sentence-transformers embeddings (normalized), loaded on first use."""
from config import EMBED_MODEL

_embedder = None


def embed(texts):
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder.encode(list(texts), normalize_embeddings=True, batch_size=64, show_progress_bar=False).tolist()

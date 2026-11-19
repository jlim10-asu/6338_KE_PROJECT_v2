"""Local sentence-transformers embeddings (normalized), loaded on first use; one cached model per name.

embed():    EMBED_MODEL (all-MiniLM-L6-v2) for stage 5a statement -> claim matching; its thresholds were tuned on it.
qa_embed(): QA_EMBED_MODEL (MedEmbed-base-v0.1, Apache-2.0) for the stage 8a vector indexes, the GraphRAG engine and
            eval_qa. Questions and nodes must use the same model, so change it only together with a rerun of 8a.
"""
import config
from config import EMBED_MODEL

QA_EMBED_MODEL = getattr(config, "QA_EMBED_MODEL", "abhinand/MedEmbed-base-v0.1")
_models = {}


def embed(texts, model=EMBED_MODEL):
    if model not in _models:
        from sentence_transformers import SentenceTransformer
        _models[model] = SentenceTransformer(model)
    return _models[model].encode(list(texts), normalize_embeddings=True, batch_size=64, show_progress_bar=False).tolist()


def qa_embed(texts):
    return embed(texts, QA_EMBED_MODEL)

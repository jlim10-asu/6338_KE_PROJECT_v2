"""Small text helpers shared across stages."""
import re


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def lucene_escape(text):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)

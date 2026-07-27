"""Retrieval backend for brand knowledge, prior posts, and research evidence.

Two interchangeable implementations behind one interface:

* :class:`VertexAiSearchKnowledgeBase` - queries a Vertex AI Search datastore.
  This is the production path: the datastore is provisioned by
  ``deployment/terraform/search.tf`` and indexes the brand style guide, every
  published post, and the approved research corpus.
* :class:`LocalCorpusKnowledgeBase` - a dependency-free TF-IDF/cosine index over
  a bundled JSON corpus. This is what makes the repository *runnable by a
  reviewer with no GCP project*, and what the evaluation suite runs against, so
  golden-dataset results are deterministic rather than dependent on a live index.

Selection is automatic: if ``CONTENTFORGE_VERTEX_SEARCH_DATASTORE`` is set (and
the client library is importable) the Vertex backend is used; otherwise the local
one. Nothing above this module knows or cares which answered.
"""

from __future__ import annotations

import functools
import json
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path
from typing import Any

from content_forge.config import get_settings
from content_forge.observability.logging_config import get_logger
from content_forge.schemas import (
    BrandStyleGuide,
    ContentTone,
    EvidenceSnippet,
    PriorPostMatch,
    SourceCredibility,
)

logger = get_logger(__name__)

_CORPUS_DIR = Path(__file__).parent / "corpus"


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens of length >= 2."""
    return [t for t in re.findall(r"[a-z0-9']+", text.lower()) if len(t) >= 2]


class BaseKnowledgeBase(ABC):
    """Interface every retrieval backend implements."""

    @abstractmethod
    def fetch_style_guide(self, *, topic: str, content_type: str) -> BrandStyleGuide:
        """Return the brand rules applying to ``topic`` under ``content_type``."""

    @abstractmethod
    def search_prior_posts(self, *, query: str, limit: int) -> list[PriorPostMatch]:
        """Return published posts most similar to ``query``."""

    @abstractmethod
    def gather_evidence(self, *, subtopic: str, limit: int) -> list[EvidenceSnippet]:
        """Return citable evidence for ``subtopic``."""


class LocalCorpusKnowledgeBase(BaseKnowledgeBase):
    """TF-IDF cosine retrieval over the bundled JSON corpus.

    Deliberately dependency-free (no numpy, no embedding service): the point is
    that ``git clone && pip install -e . && pytest`` works offline, and that the
    golden-dataset evaluation is reproducible bit-for-bit on any machine.
    """

    def __init__(self, corpus_dir: Path | None = None) -> None:
        self._dir = corpus_dir or _CORPUS_DIR
        self._posts: list[dict[str, Any]] = self._load("published_posts.json")
        self._evidence: list[dict[str, Any]] = self._load("research_corpus.json")
        self._style: dict[str, Any] = self._load("brand_style_guide.json", as_list=False) or {}
        self._post_idf = self._build_idf([p["summary"] + " " + p["title"] for p in self._posts])
        self._evidence_idf = self._build_idf([e["claim"] for e in self._evidence])

    def _load(self, filename: str, *, as_list: bool = True) -> Any:
        path = self._dir / filename
        if not path.exists():
            logger.warning("corpus_file_missing", path=str(path))
            return [] if as_list else {}
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _build_idf(documents: list[str]) -> dict[str, float]:
        """Inverse document frequency over the corpus, for cosine weighting."""
        n = max(len(documents), 1)
        df: Counter[str] = Counter()
        for doc in documents:
            df.update(set(_tokenize(doc)))
        return {term: math.log(n / (1 + count)) + 1.0 for term, count in df.items()}

    @staticmethod
    def _cosine(query: str, document: str, idf: dict[str, float]) -> float:
        """TF-IDF weighted cosine similarity in [0, 1]."""
        q_terms, d_terms = Counter(_tokenize(query)), Counter(_tokenize(document))
        if not q_terms or not d_terms:
            return 0.0
        shared = set(q_terms) & set(d_terms)
        numerator = sum(q_terms[t] * d_terms[t] * (idf.get(t, 1.0) ** 2) for t in shared)
        q_norm = math.sqrt(sum((c * idf.get(t, 1.0)) ** 2 for t, c in q_terms.items()))
        d_norm = math.sqrt(sum((c * idf.get(t, 1.0)) ** 2 for t, c in d_terms.items()))
        if not q_norm or not d_norm:
            return 0.0
        return min(1.0, numerator / (q_norm * d_norm))

    def fetch_style_guide(self, *, topic: str, content_type: str) -> BrandStyleGuide:
        base = self._style.get("default", {})
        overrides = self._style.get("content_types", {}).get(content_type, {})
        merged: dict[str, Any] = {**base, **overrides}

        # Topic-specific overlays, e.g. security topics require stricter sourcing.
        for overlay in self._style.get("topic_overlays", []):
            if any(keyword in topic.lower() for keyword in overlay.get("match_keywords", [])):
                merged["banned_phrases"] = list(
                    {*merged.get("banned_phrases", []), *overlay.get("banned_phrases", [])}
                )
                if overlay.get("citation_policy"):
                    merged["citation_policy"] = overlay["citation_policy"]

        return BrandStyleGuide(
            tone=ContentTone(merged.get("tone", "authoritative")),
            reading_level=merged.get("reading_level", "grade 9-11"),
            banned_phrases=merged.get("banned_phrases", []),
            required_sections=merged.get("required_sections", []),
            max_words=int(merged.get("max_words", 1600)),
            citation_policy=merged.get("citation_policy", "One inline link per factual claim."),
        )

    def search_prior_posts(self, *, query: str, limit: int) -> list[PriorPostMatch]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for post in self._posts:
            document = f"{post['title']} {post['summary']} {post['primary_keyword']}"
            scored.append((self._cosine(query, document, self._post_idf), post))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            PriorPostMatch(
                post_id=post["post_id"],
                title=post["title"],
                url=post["url"],
                published_on=post["published_on"],
                similarity=round(score, 3),
                summary=post["summary"],
                primary_keyword=post["primary_keyword"],
            )
            for score, post in scored[:limit]
            if score > 0.05
        ]

    def gather_evidence(self, *, subtopic: str, limit: int) -> list[EvidenceSnippet]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in self._evidence:
            document = f"{item['claim']} {item.get('source_title', '')}"
            scored.append((self._cosine(subtopic, document, self._evidence_idf), item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            EvidenceSnippet(
                claim=item["claim"],
                source_url=item["source_url"],
                source_title=item["source_title"],
                credibility=SourceCredibility(item.get("credibility", "unknown")),
                published_on=item.get("published_on"),
            )
            for score, item in scored[:limit]
            if score > 0.08
        ]


class VertexAiSearchKnowledgeBase(BaseKnowledgeBase):
    """Retrieval backed by a Vertex AI Search datastore.

    The datastore, its schema and the service-account bindings are declared in
    ``deployment/terraform/search.tf``. Any retrieval failure is allowed to
    propagate: the calling tool converts it into a guided error envelope telling
    the model to proceed without evidence rather than to invent it.
    """

    def __init__(self, datastore: str, fallback: LocalCorpusKnowledgeBase) -> None:
        from google.cloud import discoveryengine_v1 as discoveryengine

        self._datastore = datastore
        self._fallback = fallback
        self._client = discoveryengine.SearchServiceClient()
        self._request_cls = discoveryengine.SearchRequest

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        response = self._client.search(
            self._request_cls(
                serving_config=f"{self._datastore}/servingConfigs/default_search",
                query=query,
                page_size=limit,
            )
        )
        documents: list[dict[str, Any]] = []
        for result in response:
            data = getattr(result.document, "struct_data", None)
            if data:
                documents.append(dict(data))
        return documents

    def fetch_style_guide(self, *, topic: str, content_type: str) -> BrandStyleGuide:
        # The style guide is small, versioned and authoritative; it is served from
        # the bundled copy so a search-ranking change can never silently alter
        # binding brand rules.
        return self._fallback.fetch_style_guide(topic=topic, content_type=content_type)

    def search_prior_posts(self, *, query: str, limit: int) -> list[PriorPostMatch]:
        matches: list[PriorPostMatch] = []
        for doc in self._search(f"blog post about {query}", limit):
            matches.append(
                PriorPostMatch(
                    post_id=str(doc.get("post_id", "")),
                    title=str(doc.get("title", "")),
                    url=str(doc.get("url", "")),
                    published_on=str(doc.get("published_on", "")),
                    similarity=float(doc.get("relevance_score", 0.5)),
                    summary=str(doc.get("summary", "")),
                    primary_keyword=str(doc.get("primary_keyword", "")),
                )
            )
        return matches

    def gather_evidence(self, *, subtopic: str, limit: int) -> list[EvidenceSnippet]:
        evidence: list[EvidenceSnippet] = []
        for doc in self._search(subtopic, limit):
            evidence.append(
                EvidenceSnippet(
                    claim=str(doc.get("claim", "")),
                    source_url=str(doc.get("source_url", "")),
                    source_title=str(doc.get("source_title", "")),
                    credibility=SourceCredibility(str(doc.get("credibility", "unknown"))),
                    published_on=doc.get("published_on"),
                )
            )
        return evidence


@functools.lru_cache(maxsize=1)
def get_knowledge_base() -> BaseKnowledgeBase:
    """Return the process-wide knowledge base, selecting the backend automatically.

    Returns:
        :class:`VertexAiSearchKnowledgeBase` when a datastore is configured and
        the client library is importable, otherwise
        :class:`LocalCorpusKnowledgeBase`.
    """
    local = LocalCorpusKnowledgeBase()
    datastore = get_settings().vertex_search_datastore
    if not datastore:
        logger.info("knowledge_base_backend", backend="local_corpus", reason="no_datastore")
        return local
    try:
        backend = VertexAiSearchKnowledgeBase(datastore, fallback=local)
        logger.info("knowledge_base_backend", backend="vertex_ai_search", datastore=datastore)
        return backend
    except ImportError:
        logger.warning(
            "knowledge_base_backend",
            backend="local_corpus",
            reason="google-cloud-discoveryengine not installed; install '.[gcp]'",
        )
        return local

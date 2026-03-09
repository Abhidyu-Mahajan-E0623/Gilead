from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from .azure_client import AzureOpenAIClient
from .utils import normalize_text

try:
    from rapidfuzz import fuzz
except Exception:  # noqa: BLE001
    fuzz = None


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class InquiryRecord:
    inquiry_id: str
    category: str
    title: str
    field_rep_says: str
    what_happened: str
    steward_steps: list[str]
    datasets_used: list[str]
    resolution_and_response_to_rep: str


@dataclass(slots=True)
class SearchResult:
    inquiry: InquiryRecord
    score: float
    lexical_score: float
    semantic_score: float | None


class PlaybookIndex:
    def __init__(
        self,
        playbook_path: Path,
        azure_client: AzureOpenAIClient,
        embedding_cache_path: Path,
    ) -> None:
        self.playbook_path = playbook_path
        self.azure_client = azure_client
        self.embedding_cache_path = embedding_cache_path
        self.embedding_cache_path.parent.mkdir(parents=True, exist_ok=True)

        self.inquiries = self._load_inquiries()
        self._search_text = {
            inquiry.inquiry_id: self._build_search_text(inquiry) for inquiry in self.inquiries
        }
        self._token_sets = {
            inquiry.inquiry_id: set(normalize_text(self._search_text[inquiry.inquiry_id]).split())
            for inquiry in self.inquiries
        }
        self._embeddings: dict[str, list[float]] = {}
        self._semantic_enabled = False

        self._init_embeddings()

    def _load_inquiries(self) -> list[InquiryRecord]:
        if not self.playbook_path.exists():
            raise FileNotFoundError(f"Playbook file not found: {self.playbook_path}")

        data = json.loads(self.playbook_path.read_text(encoding="utf-8"))
        inquiries = []

        for item in data.get("inquiries", []):
            inquiries.append(
                InquiryRecord(
                    inquiry_id=str(item.get("inquiry_id", "")),
                    category=item.get("category", ""),
                    title=item.get("title", ""),
                    field_rep_says=item.get("field_rep_says", ""),
                    what_happened=item.get("what_happened", ""),
                    steward_steps=item.get("steward_steps", []),
                    datasets_used=item.get("datasets_used", []),
                    resolution_and_response_to_rep=item.get(
                        "resolution_and_response_to_rep", ""
                    ),
                )
            )

        if not inquiries:
            raise ValueError("No inquiries found in playbook file")

        return inquiries

    @staticmethod
    def _build_search_text(inquiry: InquiryRecord) -> str:
        return (
            f"Category: {inquiry.category}. "
            f"Title: {inquiry.title}. "
            f"Field rep says: {inquiry.field_rep_says}. "
            f"What happened: {inquiry.what_happened}."
        )

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0

        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    @staticmethod
    def _keyword_overlap_score(query_tokens: set[str], target_tokens: set[str]) -> float:
        if not query_tokens or not target_tokens:
            return 0.0
        intersection = len(query_tokens.intersection(target_tokens))
        union = len(query_tokens.union(target_tokens))
        return intersection / union if union else 0.0

    @staticmethod
    def _fuzzy_score(a: str, b: str) -> float:
        if not a or not b:
            return 0.0

        if fuzz is not None:
            return fuzz.token_set_ratio(a, b) / 100.0

        a_tokens = set(normalize_text(a).split())
        b_tokens = set(normalize_text(b).split())
        return PlaybookIndex._keyword_overlap_score(a_tokens, b_tokens)

    def _init_embeddings(self) -> None:
        if not self.azure_client.is_ready:
            return

        if self._load_embeddings_from_cache():
            self._semantic_enabled = True
            LOGGER.info("Loaded inquiry embeddings from cache")
            return

        vectors_by_id: dict[str, list[float]] = {}
        batch_size = 6
        inquiry_ids = [inquiry.inquiry_id for inquiry in self.inquiries]

        for start in range(0, len(inquiry_ids), batch_size):
            batch_ids = inquiry_ids[start : start + batch_size]
            batch_text = [self._search_text[item_id] for item_id in batch_ids]
            vectors = self.azure_client.embed_texts(batch_text)
            if len(vectors) != len(batch_ids):
                LOGGER.warning("Embedding batch size mismatch; disabling semantic search")
                self._embeddings = {}
                self._semantic_enabled = False
                return

            for item_id, vector in zip(batch_ids, vectors, strict=False):
                vectors_by_id[item_id] = vector

        if len(vectors_by_id) != len(inquiry_ids):
            LOGGER.warning("Could not build all embeddings; semantic search disabled")
            return

        self._embeddings = vectors_by_id
        self._semantic_enabled = True
        self._save_embeddings_to_cache()
        LOGGER.info("Built inquiry embeddings using Azure deployment")

    def _load_embeddings_from_cache(self) -> bool:
        if not self.embedding_cache_path.exists():
            return False

        try:
            payload = json.loads(self.embedding_cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False

        if payload.get("embedding_deployment") != self.azure_client.settings.embedding_deployment:
            return False

        vectors = payload.get("vectors", {})
        inquiry_ids = {inquiry.inquiry_id for inquiry in self.inquiries}

        if set(vectors.keys()) != inquiry_ids:
            return False

        if not vectors:
            return False

        sample = next(iter(vectors.values()))
        if not isinstance(sample, list) or not sample:
            return False

        self._embeddings = {item_id: vector for item_id, vector in vectors.items()}
        return True

    def _save_embeddings_to_cache(self) -> None:
        payload = {
            "embedding_deployment": self.azure_client.settings.embedding_deployment,
            "vectors": self._embeddings,
        }
        self.embedding_cache_path.write_text(json.dumps(payload), encoding="utf-8")

    def search(self, query: str, top_k: int = 3) -> list[SearchResult]:
        normalized_query = normalize_text(query)
        query_tokens = set(normalized_query.split())

        semantic_by_id: dict[str, float] = {}
        if self._semantic_enabled:
            query_vector = self.azure_client.embed_texts([query])
            if query_vector and query_vector[0]:
                for inquiry in self.inquiries:
                    vector = self._embeddings.get(inquiry.inquiry_id, [])
                    cosine = self._cosine_similarity(query_vector[0], vector)
                    semantic_by_id[inquiry.inquiry_id] = max(0.0, (cosine + 1.0) / 2.0)
            else:
                self._semantic_enabled = False

        scored: list[SearchResult] = []
        for inquiry in self.inquiries:
            fuzzy_field = self._fuzzy_score(query, inquiry.field_rep_says)
            fuzzy_title = self._fuzzy_score(query, inquiry.title)
            fuzzy_context = self._fuzzy_score(query, inquiry.what_happened)
            overlap = self._keyword_overlap_score(query_tokens, self._token_sets[inquiry.inquiry_id])

            lexical = (
                (0.44 * fuzzy_field)
                + (0.24 * fuzzy_title)
                + (0.20 * fuzzy_context)
                + (0.12 * overlap)
            )

            semantic = semantic_by_id.get(inquiry.inquiry_id)
            if semantic is None:
                combined = lexical
            else:
                combined = (0.65 * semantic) + (0.35 * lexical)

            scored.append(
                SearchResult(
                    inquiry=inquiry,
                    score=combined,
                    lexical_score=lexical,
                    semantic_score=semantic,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[: max(1, top_k)]

    @staticmethod
    def confidence(results: list[SearchResult]) -> float:
        if not results:
            return 0.0

        top_score = results[0].score
        second_score = results[1].score if len(results) > 1 else 0.0
        margin = max(0.0, top_score - second_score)
        confidence = min(1.0, max(0.0, (top_score * 0.83) + (margin * 0.42)))
        return round(confidence, 4)

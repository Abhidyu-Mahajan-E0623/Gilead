from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .azure_client import AzureOpenAIClient
from .playbook import PlaybookIndex
from .utils import STOPWORDS, TOKEN_PATTERN, normalize_text


IDENTIFIER_PATTERNS = {
    "NPI": re.compile(r"\bNPI\b\s*[:#-]?\s*(\d{10})", re.IGNORECASE),
    "HCO ID": re.compile(r"\bHCO\s+ID\b\s*[:#-]?\s*([A-Z0-9-]+)", re.IGNORECASE),
}
TERRITORY_ID_PATTERN = re.compile(r"\b(?:[A-Z]{2,5}-\d{2,4}|[A-Z]{1,3}\d{4,8})\b", re.IGNORECASE)
TERRITORY_CONTEXT_PATTERN = re.compile(
    r"\b(?:territory(?:\s+id)?(?:\s+code)?)\b[^A-Z0-9]{0,6}([A-Z]{1,5}-?\d{2,8})\b",
    re.IGNORECASE,
)
NON_TERRITORY_PREFIXES = {"DCR", "HCO", "HCP", "NPI", "SP", "ZIP", "IC"}
PROVIDER_ID_VALUE_PATTERN = re.compile(r"\b(\d{10})\b")
PROVIDER_REF_PATTERN = re.compile(r"\bdr\.?\s+[A-Za-z'`-]+\b", re.IGNORECASE)
PROVIDER_NAME_PATTERN = re.compile(
    r"\bDr\.?\s+([A-Z][A-Za-z'`-]+(?:\s+[A-Z][A-Za-z'`-]+)+)"
)
PROVIDER_NAME_WITH_NPI_PATTERN = re.compile(
    r"\bDr\.?\s+([A-Z][A-Za-z'`-]+(?:\s+[A-Z][A-Za-z'`-]+)*)\s*\(NPI\b[^0-9]{0,8}(\d{10})",
    re.IGNORECASE,
)
ACCOUNT_NAME_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z0-9&'.-]+(?:\s+[A-Z][A-Za-z0-9&'.-]+){1,5})\s+\(HCO\s+ID\b"
)
VAGUE_QUERY_PREFIXES = (
    "what happened",
    "whats happening",
    "what is happening",
    "can you explain",
    "explain this",
    "why is this happening",
    "why did this happen",
)
GENERAL_REPLACEMENTS = (
    (re.compile(r"\bDr\.?\s+[A-Z][A-Za-z'`-]+(?:\s+[A-Z][A-Za-z'`-]+)+"), "the provider"),
    (
        re.compile(
            r"\b[A-Z][A-Za-z0-9&'.-]+(?:\s+[A-Z][A-Za-z0-9&'.-]+){1,5}\s+\(HCO\s+ID\s*:\s*[A-Z0-9-]+\)",
            re.IGNORECASE,
        ),
        "the account",
    ),
    (re.compile(r"\bNPI\b\s*[:#-]?\s*\d{10}", re.IGNORECASE), "the provider's NPI"),
    (re.compile(r"\bHCO\s+ID\b\s*[:#-]?\s*[A-Z0-9-]+", re.IGNORECASE), "the account's HCO ID"),
    (re.compile(r"\bZIP\s+code\s+\d{5}\b", re.IGNORECASE), "the relevant ZIP code"),
    (re.compile(r"\b\d{5}\b"), "the relevant ZIP code"),
    (re.compile(r"\b[A-Z]{2,5}-\d{2,4}\b"), "the affected territory"),
    (re.compile(r"\bDCR\s*#?[A-Z0-9-]+\b", re.IGNORECASE), "a correction request"),
    (re.compile(r"\bSP-\d+\b", re.IGNORECASE), "the specialty pharmacy channel"),
    (re.compile(r"\bspecialty code\s+\d+\b", re.IGNORECASE), "the relevant specialty code"),
    (re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"), "the relevant date"),
    (re.compile(r"\b\d+\s*-\s*\d+\b"), "multiple"),
    (re.compile(r"\b\d+%"), "a significant share"),
    (re.compile(r"\b\d+\b"), "multiple"),
)


@dataclass(slots=True)
class AssistantResult:
    content: str
    matched_inquiry_id: str | None
    matched_title: str | None
    confidence: float | None


@dataclass(slots=True)
class ResponseDecision:
    mode: str
    follow_up: str | None = None


@dataclass(slots=True)
class FollowUpState:
    base_query: str
    locked_context: dict[str, Any]
    requested_identifiers: list[str]
    confidence: float


class ChatResponder:
    def __init__(self, index: PlaybookIndex, azure_client: AzureOpenAIClient) -> None:
        self.index = index
        self.azure_client = azure_client
        self.inquiries_by_id = {inquiry.inquiry_id: inquiry for inquiry in self.index.inquiries}

    def _build_context_block(self, query: str) -> tuple[list[dict[str, Any]], float]:
        results = self.index.search(query, top_k=3)
        confidence = self.index.confidence(results)

        context = []
        for rank, result in enumerate(results, start=1):
            inquiry = result.inquiry
            context.append(
                {
                    "rank": rank,
                    "inquiry_id": inquiry.inquiry_id,
                    "title": inquiry.title,
                    "category": inquiry.category,
                    "field_rep_says": inquiry.field_rep_says,
                    "what_happened": inquiry.what_happened,
                    "datasets_used": inquiry.datasets_used,
                    "resolution_and_response_to_rep": inquiry.resolution_and_response_to_rep,
                    "score": round(result.score, 4),
                }
            )

        return context, confidence

    def _context_from_inquiry_id(self, inquiry_id: str, score: float = 1.0) -> dict[str, Any] | None:
        inquiry = self.inquiries_by_id.get(str(inquiry_id))
        if inquiry is None:
            return None

        return {
            "rank": 1,
            "inquiry_id": inquiry.inquiry_id,
            "title": inquiry.title,
            "category": inquiry.category,
            "field_rep_says": inquiry.field_rep_says,
            "what_happened": inquiry.what_happened,
            "datasets_used": inquiry.datasets_used,
            "resolution_and_response_to_rep": inquiry.resolution_and_response_to_rep,
            "score": round(score, 4),
        }

    @staticmethod
    def _content_tokens(value: str) -> list[str]:
        return [token for token in TOKEN_PATTERN.findall(value.lower()) if token not in STOPWORDS]

    def _issue_tokens(self, context: list[dict[str, Any]] | dict[str, Any]) -> set[str]:
        source = (
            f"{context.get('field_rep_says', '')} {context.get('what_happened', '')}"
            if isinstance(context, dict)
            else ""
        )
        return set(self._content_tokens(source))

    @staticmethod
    def _extract_identifiers(text: str) -> list[str]:
        identifiers: list[str] = []
        for label, pattern in IDENTIFIER_PATTERNS.items():
            if pattern.search(text):
                identifiers.append(label)
        return identifiers

    @staticmethod
    def _identifier_source_text(context: dict[str, Any]) -> str:
        parts = [
            str(context.get("field_rep_says", "")).strip(),
            str(context.get("what_happened", "")).strip(),
            str(context.get("resolution_and_response_to_rep", "")).strip(),
        ]
        return " ".join(part for part in parts if part)

    @staticmethod
    def _requested_identifiers_from_content(content: str) -> list[str]:
        lowered = content.lower()
        requested: list[str] = []
        if "npi ids" in lowered or "both npi" in lowered or "all npi" in lowered:
            requested.append("NPI IDs")
        elif "npi" in lowered or "ncp id" in lowered:
            requested.append("NPI ID")
        if "hco id" in lowered:
            requested.append("HCO ID")
        if "territory id" in lowered:
            requested.append("territory ID")
        return requested

    @staticmethod
    def _extract_all_npi_values(text: str) -> list[str]:
        values: list[str] = []
        for match in PROVIDER_ID_VALUE_PATTERN.findall(text):
            if match not in values:
                values.append(match)
        return values

    @staticmethod
    def _is_valid_territory_id(candidate: str) -> bool:
        value = re.sub(r"[^A-Z0-9-]", "", candidate.upper())
        if not value or not re.search(r"\d", value):
            return False

        prefix_match = re.match(r"^([A-Z]+)", value)
        if not prefix_match:
            return False

        return prefix_match.group(1) not in NON_TERRITORY_PREFIXES

    @classmethod
    def _extract_territory_id(cls, text: str) -> str | None:
        for match in TERRITORY_CONTEXT_PATTERN.finditer(text):
            candidate = match.group(1).upper()
            if cls._is_valid_territory_id(candidate):
                return candidate

        for match in TERRITORY_ID_PATTERN.finditer(text):
            candidate = match.group(0).upper()
            if cls._is_valid_territory_id(candidate):
                return candidate

        return None

    @classmethod
    def _extract_all_territory_ids(cls, text: str) -> list[str]:
        territory_ids: list[str] = []
        for pattern, group in ((TERRITORY_CONTEXT_PATTERN, 1), (TERRITORY_ID_PATTERN, 0)):
            for match in pattern.finditer(text):
                candidate = match.group(group).upper()
                if not cls._is_valid_territory_id(candidate):
                    continue
                if candidate not in territory_ids:
                    territory_ids.append(candidate)
        return territory_ids

    @staticmethod
    def _message_looks_like_identifier_reply(message: str) -> bool:
        lowered = message.lower()
        return (
            "npi" in lowered
            or "ncp" in lowered
            or "hco" in lowered
            or "territory" in lowered
            or bool(re.search(r"\d", message))
            or bool(ChatResponder._extract_territory_id(message))
        )

    @staticmethod
    def _looks_like_new_question(message: str) -> bool:
        tokens = TOKEN_PATTERN.findall(message.lower())
        if len(tokens) >= 9:
            return True
        if "?" in message and len(tokens) >= 5:
            return True
        return False

    @staticmethod
    def _extract_expected_identifier_values(context: dict[str, Any]) -> dict[str, list[str]]:
        source = ChatResponder._identifier_source_text(context)
        provider_ids = ChatResponder._extract_all_npi_values(source)
        return {
            "NPI ID": provider_ids,
            "NPI IDs": provider_ids,
            "HCO ID": [match.upper() for match in re.findall(r"\bHCO\s+ID\b\s*[:#-]?\s*([A-Z0-9-]+)", source, re.IGNORECASE)],
            "territory ID": ChatResponder._extract_all_territory_ids(source),
        }

    @classmethod
    def _expected_provider_npis_for_query(cls, query: str, context: dict[str, Any]) -> list[str]:
        source = cls._identifier_source_text(context)
        query_normalized = normalize_text(query)
        query_tokens = set(TOKEN_PATTERN.findall(query_normalized))
        expected: list[str] = []

        for raw_name, npi in PROVIDER_NAME_WITH_NPI_PATTERN.findall(source):
            normalized_name = normalize_text(raw_name)
            name_tokens = [token for token in TOKEN_PATTERN.findall(normalized_name) if token]
            if not name_tokens:
                continue

            full_name = " ".join(name_tokens)
            last_name = name_tokens[-1]
            mentioned = (
                full_name in query_normalized
                or last_name in query_tokens
                or bool(re.search(rf"\bdr\.?\s+{re.escape(last_name)}\b", query, re.IGNORECASE))
            )
            if mentioned and npi not in expected:
                expected.append(npi)

        return expected

    @staticmethod
    def _extract_query_identifier_value(identifier_type: str, text: str) -> tuple[str | None, bool]:
        upper_text = text.upper()
        lower_text = text.lower()

        if identifier_type == "NPI ID":
            match = re.search(r"\b(?:NPI|NCP(?:\s*ID)?)\s*[:#-]?\s*(\d{10})\b", text, re.IGNORECASE)
            if match is None:
                value_match = PROVIDER_ID_VALUE_PATTERN.search(text)
                match_value = value_match.group(1) if value_match else None
            else:
                match_value = match.group(1)
            attempted = "npi" in lower_text or "ncp" in lower_text or bool(re.search(r"\d", text))
            if match_value is None:
                return None, attempted
            if len(match_value) != 10:
                return None, attempted
            return match_value, attempted

        if identifier_type == "NPI":
            match = re.search(r"\b(?:NPI\s*[:#-]?\s*)?(\d{10})\b", text, re.IGNORECASE)
            attempted = "npi" in lower_text or bool(re.search(r"\d", text))
            return (match.group(1) if match else None, attempted)

        if identifier_type == "HCO ID":
            match = re.search(r"\b(?:HCO\s*ID\s*[:#-]?\s*)?(HCO-\d{2,6})\b", upper_text)
            attempted = "hco" in lower_text or bool(re.search(r"\bHCO-\d{2,6}\b", upper_text))
            return (match.group(1).upper() if match else None, attempted)

        if identifier_type == "territory ID":
            territory_id = ChatResponder._extract_territory_id(upper_text)
            attempted = "territory" in lower_text or bool(territory_id)
            return (territory_id, attempted)

        return None, False

    @staticmethod
    def _query_has_identifier(query: str) -> bool:
        return any(pattern.search(query) for pattern in IDENTIFIER_PATTERNS.values()) or bool(
            ChatResponder._extract_territory_id(query)
        ) or bool(re.search(r"\b(?:NCP(?:\s*ID)?|NPI)\b\s*[:#-]?\s*\d{10}\b", query, re.IGNORECASE)) or bool(
            PROVIDER_ID_VALUE_PATTERN.search(query)
        )

    @staticmethod
    def _extract_entities(context: dict[str, Any]) -> list[str]:
        entities: list[str] = []
        source = f"{context.get('what_happened', '')} {context.get('field_rep_says', '')}"

        for match in PROVIDER_NAME_PATTERN.findall(source):
            entity = match.strip()
            if entity not in entities:
                entities.append(entity)

        for match in ACCOUNT_NAME_PATTERN.findall(source):
            entity = match.strip()
            if entity not in entities:
                entities.append(entity)

        return entities

    def _entity_tokens(self, context: dict[str, Any]) -> set[str]:
        tokens: set[str] = set()
        for entity in self._extract_entities(context):
            tokens.update(token for token in normalize_text(entity).split() if token not in STOPWORDS)
        return tokens

    @staticmethod
    def _query_mentions_territory(query: str) -> bool:
        normalized = normalize_text(query)
        return any(
            phrase in normalized
            for phrase in (
                "my territory",
                "our territory",
                "this territory",
                "territory list",
                "territory ownership",
                "territory assignment",
                "territory realignment",
                "my geography",
                "my accounts",
            )
        ) or "territory" in normalized or bool(ChatResponder._extract_territory_id(query))

    @staticmethod
    def _query_has_provider_id(query: str) -> bool:
        return bool(
            re.search(r"\b(?:NPI|NCP(?:\s*ID)?)\b\s*[:#-]?\s*\d{10}\b", query, re.IGNORECASE)
            or PROVIDER_ID_VALUE_PATTERN.search(query)
        )

    @staticmethod
    def _query_mentions_provider(query: str, context: dict[str, Any]) -> bool:
        if PROVIDER_REF_PATTERN.search(query):
            return True

        normalized_query = normalize_text(query)
        if any(token in normalized_query for token in ("doctor", "physician", "specialist", "hcp")):
            return True

        provider_last_names: set[str] = set()
        for match in PROVIDER_NAME_PATTERN.findall(ChatResponder._identifier_source_text(context)):
            tokens = [token for token in normalize_text(match).split() if token]
            if tokens:
                provider_last_names.add(tokens[-1])

        if not provider_last_names:
            return False

        query_tokens = set(TOKEN_PATTERN.findall(normalized_query))
        return bool(query_tokens.intersection(provider_last_names))

    def _required_identifiers(self, query: str, context: dict[str, Any]) -> list[str]:
        required: list[str] = []
        identifier_source = self._identifier_source_text(context)
        available_identifiers = self._extract_identifiers(identifier_source)
        expected_provider_npis = self._expected_provider_npis_for_query(query, context)
        query_npis = set(self._extract_all_npi_values(query))

        if expected_provider_npis:
            missing_npis = [npi for npi in expected_provider_npis if npi not in query_npis]
            if missing_npis:
                required.append("NPI IDs" if len(expected_provider_npis) > 1 else "NPI ID")
        elif "NPI" in available_identifiers and self._query_mentions_provider(query, context) and not self._query_has_provider_id(query):
            required.append("NPI ID")

        if "HCO ID" in available_identifiers and not IDENTIFIER_PATTERNS["HCO ID"].search(query):
            required.append("HCO ID")

        if (
            self._query_mentions_territory(query)
            and not self._extract_territory_id(query)
        ):
            required.append("territory ID")

        return required

    @staticmethod
    def _format_identifier_follow_up(required_identifiers: list[str], *, correct: bool = False) -> str:
        labels = [
            "NPI IDs for all mentioned providers" if identifier == "NPI IDs" else identifier
            for identifier in required_identifiers
        ]
        qualifier = "correct " if correct else ""
        if len(labels) == 1:
            noun = "records" if labels[0].endswith("s") else "record"
            return (
                f"Please provide the {qualifier}{labels[0]} so the correct {noun} can be confirmed before proceeding."
            )

        if len(labels) == 2:
            joined = f"{labels[0]} and {labels[1]}"
        else:
            joined = ", ".join(labels[:-1]) + f", and {labels[-1]}"

        return f"Please provide the {qualifier}{joined} so the correct records can be confirmed before proceeding."

    def _validate_identifier_reply(
        self,
        reply: str,
        context: dict[str, Any],
        requested_identifiers: list[str],
        base_query: str | None = None,
    ) -> tuple[dict[str, str], str | None]:
        expected_values = self._extract_expected_identifier_values(context)
        validated: dict[str, str] = {}
        missing: list[str] = []
        invalid: list[str] = []

        for identifier_type in requested_identifiers:
            if identifier_type == "NPI IDs":
                provided_npis = self._extract_all_npi_values(reply)
                attempted = "npi" in reply.lower() or "ncp" in reply.lower() or bool(re.search(r"\d", reply))
                if not provided_npis:
                    if attempted:
                        invalid.append(identifier_type)
                    else:
                        missing.append(identifier_type)
                    continue

                expected_npis = self._expected_provider_npis_for_query(base_query or "", context)
                if not expected_npis:
                    expected_npis = expected_values.get("NPI IDs", [])

                expected_set = {value.upper() for value in expected_npis}
                provided_set = {value.upper() for value in provided_npis}

                if expected_set:
                    if any(value.upper() not in expected_set for value in provided_npis):
                        invalid.append(identifier_type)
                        continue
                    if any(value.upper() not in provided_set for value in expected_npis):
                        missing.append(identifier_type)
                        continue
                elif len(provided_npis) < 2:
                    missing.append(identifier_type)
                    continue

                validated[identifier_type] = ", ".join(provided_npis)
                continue

            value, attempted = self._extract_query_identifier_value(identifier_type, reply)
            if value is None:
                if attempted:
                    invalid.append(identifier_type)
                else:
                    missing.append(identifier_type)
                continue

            expected = expected_values.get(identifier_type, [])
            if expected and value.upper() not in {item.upper() for item in expected}:
                invalid.append(identifier_type)
                continue

            validated[identifier_type] = value if identifier_type == "NPI ID" else value.upper()

        if invalid:
            return {}, self._format_identifier_follow_up(invalid, correct=True)

        if missing:
            return {}, self._format_identifier_follow_up(missing)

        return validated, None

    def _resolve_follow_up_state(
        self,
        user_message: str,
        conversation_history: list[dict[str, Any]] | None,
    ) -> FollowUpState | None:
        if not conversation_history:
            return None

        history = (
            conversation_history[:-1]
            if conversation_history and conversation_history[-1].get("role") == "user"
            else conversation_history
        )
        assistant_index = next(
            (index for index in range(len(history) - 1, -1, -1) if history[index].get("role") == "assistant"),
            None,
        )
        if assistant_index is None:
            return None

        assistant_message = history[assistant_index]
        requested_identifiers = self._requested_identifiers_from_content(str(assistant_message.get("content", "")))
        if not requested_identifiers:
            return None

        if not self._message_looks_like_identifier_reply(user_message) and self._looks_like_new_question(user_message):
            return None

        metadata = assistant_message.get("metadata") or {}
        locked_context = self._context_from_inquiry_id(
            str(metadata.get("matched_inquiry_id", "")),
            score=float(metadata.get("confidence") or 1.0),
        )
        if locked_context is None:
            return None

        base_query = ""
        for index in range(assistant_index - 1, -1, -1):
            message = history[index]
            if message.get("role") != "user":
                continue
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            if self._message_looks_like_identifier_reply(content) and not self._looks_like_new_question(content):
                continue
            base_query = content
            break

        if not base_query:
            return None

        return FollowUpState(
            base_query=base_query,
            locked_context=locked_context,
            requested_identifiers=requested_identifiers,
            confidence=float(metadata.get("confidence") or locked_context.get("score") or 1.0),
        )

    def _needs_identifier_follow_up(self, query: str, context: dict[str, Any]) -> str | None:
        required_identifiers = self._required_identifiers(query, context)
        if required_identifiers:
            return self._format_identifier_follow_up(required_identifiers)
        return None

    def _classify_question_mode(self, query: str, context: dict[str, Any]) -> str:
        normalized_query = normalize_text(query)
        entity_tokens = self._entity_tokens(context)
        query_tokens = set(self._content_tokens(query))
        meaningful_query_tokens = query_tokens.difference(entity_tokens)
        query_length = len(TOKEN_PATTERN.findall(query))
        issue_overlap = len(meaningful_query_tokens.intersection(self._issue_tokens(context).difference(entity_tokens)))
        field_score = self.index._fuzzy_score(query, str(context.get("field_rep_says", "")))

        if query_length <= 5 and issue_overlap == 0 and not self._query_has_identifier(query):
            return "vague"

        if any(normalized_query.startswith(prefix) for prefix in VAGUE_QUERY_PREFIXES) and issue_overlap <= 1:
            return "vague"

        if query_length <= 8 and issue_overlap <= 1 and field_score < 0.6:
            return "vague"

        if field_score >= 0.78 or (field_score >= 0.7 and query_length >= 10) or issue_overlap >= 4:
            return "full"

        return "partial"

    def _build_decision(self, query: str, context: dict[str, Any]) -> ResponseDecision:
        follow_up = self._needs_identifier_follow_up(query, context)
        if follow_up:
            return ResponseDecision(mode="follow_up", follow_up=follow_up)
        return ResponseDecision(mode=self._classify_question_mode(query, context))

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        protected = text.strip()
        for token in ("Dr.", "Mr.", "Ms.", "Mrs.", "St.", "Sr.", "Jr."):
            protected = protected.replace(token, token.replace(".", "<dot>"))
        chunks = re.split(r"(?<=[.!?])\s+|\n+", protected)
        return [chunk.replace("<dot>", ".").strip().strip('"') for chunk in chunks if chunk.strip()]

    def _select_partial_sentences(self, query: str, text: str, limit: int = 3) -> list[str]:
        sentences = self._split_sentences(text)
        if not sentences:
            return []

        query_tokens = set(self._content_tokens(query))
        entity_tokens = set()
        for token in query_tokens:
            if any(token in normalize_text(entity).split() for entity in self._extract_entities({"what_happened": text})):
                entity_tokens.add(token)
        meaningful_query_tokens = query_tokens.difference(entity_tokens) or query_tokens
        scored: list[tuple[float, int, str]] = []

        for index, sentence in enumerate(sentences):
            sentence_tokens = set(self._content_tokens(sentence))
            overlap = len(meaningful_query_tokens.intersection(sentence_tokens))
            fuzzy = self.index._fuzzy_score(query, sentence)
            score = overlap + (0.2 * fuzzy)
            if score > 0:
                scored.append((score, index, sentence))

        if not scored:
            return sentences[: min(2, len(sentences))]

        scored.sort(key=lambda item: item[0], reverse=True)
        chosen = sorted(scored[:limit], key=lambda item: item[1])
        return [item[2] for item in chosen]

    @staticmethod
    def _generalize_text(text: str) -> str:
        generalized = text
        for pattern, replacement in GENERAL_REPLACEMENTS:
            generalized = pattern.sub(replacement, generalized)
        generalized = generalized.replace("  ", " ")
        generalized = generalized.replace(" ,", ",")
        generalized = generalized.replace(" .", ".")
        return generalized.strip()

    @staticmethod
    def _answer_source_text(context: dict[str, Any]) -> str:
        return str(context.get("what_happened", "")).strip()

    def _fallback_bullets(self, query: str, context: dict[str, Any], mode: str) -> str:
        source = self._answer_source_text(context)
        if not source:
            return "• No matched playbook explanation is available for this question."

        if mode == "full":
            bullet_items = self._split_sentences(source)
        elif mode == "vague":
            generalized = self._generalize_text(source)
            bullet_items = self._split_sentences(generalized)[:3]
        else:
            bullet_items = self._select_partial_sentences(query, source)

        return self._format_bullets(bullet_items)

    @staticmethod
    def _format_bullets(items: list[str]) -> str:
        bullets: list[str] = []
        for item in items:
            cleaned = " ".join(item.replace("•", " ").split())
            cleaned = re.sub(r"^[-*]\s*", "", cleaned)
            cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
            if not cleaned:
                continue
            bullets.append(f"• {cleaned}")

        if not bullets:
            return "• No usable answer was produced from the matched playbook context."

        return "\n".join(bullets)

    _SECTION_HEADERS = {
        "summary", "key findings", "root cause", "root cause / issue analysis",
        "issue analysis", "data sources", "next steps",
    }

    _SKIP_SECTIONS = {
        "business impact", "recommended action", "recommended actions",
    }

    def _normalize_structured_output(self, answer: str) -> str:
        raw_lines = [line.strip() for line in answer.splitlines() if line.strip()]
        if not raw_lines:
            return ""

        output_lines: list[str] = []
        skip_mode = False
        for line in raw_lines:
            stripped = re.sub(r"^#+\s*", "", line).rstrip(":")
            stripped_lower = stripped.lower()

            # Check if this header should be skipped entirely
            if stripped_lower in self._SKIP_SECTIONS:
                skip_mode = True
                continue
            if line.startswith("**") and line.endswith("**"):
                inner = line.strip("*").strip().rstrip(":")
                if inner.lower() in self._SKIP_SECTIONS:
                    skip_mode = True
                    continue

            # Recognise kept section headers — stop skipping
            if stripped_lower in self._SECTION_HEADERS:
                skip_mode = False
                output_lines.append(f"**{stripped}**")
                continue
            if line.startswith("**") and line.endswith("**"):
                inner = line.strip("*").strip().rstrip(":")
                if inner.lower() in self._SECTION_HEADERS:
                    skip_mode = False
                    output_lines.append(f"**{inner}**")
                    continue

            if skip_mode:
                continue

            if line.startswith("•"):
                output_lines.append(line)
                continue
            if line.startswith("-") or re.match(r"^\d+\.", line):
                cleaned = re.sub(r"^(-|\d+\.)\s*", "", line)
                output_lines.append(f"• {cleaned}")
                continue
            output_lines.append(line)

        return "\n".join(output_lines)

    def _normalize_bullet_output(self, answer: str) -> str:
        raw_lines = [line.strip() for line in answer.splitlines() if line.strip()]
        if not raw_lines:
            return ""

        bullet_items: list[str] = []
        for line in raw_lines:
            if line.startswith("•"):
                bullet_items.append(line.lstrip("•").strip())
                continue
            if line.startswith("-") or re.match(r"^\d+\.", line):
                bullet_items.append(re.sub(r"^(-|\d+\.)\s*", "", line))
                continue
            bullet_items.extend(self._split_sentences(line))

        return self._format_bullets(bullet_items)

    @staticmethod
    def _dataset_reference_line(context: dict[str, Any]) -> str:
        datasets_raw = context.get("datasets_used", [])
        if not isinstance(datasets_raw, list):
            return ""

        datasets: list[str] = []
        for item in datasets_raw:
            dataset_name = str(item).strip()
            if dataset_name and dataset_name not in datasets:
                datasets.append(dataset_name)

        if not datasets:
            return ""

        return f"Reference from {', '.join(datasets)}."

    def _append_dataset_reference(self, answer: str, context: dict[str, Any]) -> str:
        reference_line = self._dataset_reference_line(context)
        cleaned_answer = answer.strip()
        if not reference_line:
            return cleaned_answer
        if not cleaned_answer:
            return reference_line
        return f"{cleaned_answer}\n\n{reference_line}"

    _DCR_PATTERNS = re.compile(
        r"(?:DCR|correction request|exception request|onboarding request|mapping DCR|merge DCR|flag|submitted)"
        r".*?(?:has been submitted|was submitted|submitted to|has been logged|will be)",
        re.IGNORECASE | re.DOTALL,
    )

    _DCR_PROMPT_PATTERN = re.compile(
        r"Would you like me to (?:submit|raise|file|create|log)",
        re.IGNORECASE,
    )

    _AFFIRMATIVE_PATTERN = re.compile(
        r"^\s*(?:yes|yeah|yep|yup|sure|go ahead|please|do it|submit|raise|generate|create|file|ok|okay|absolutely|definitely|please do|yes please|ya|y)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _is_dcr_confirmation(cls, user_message: str, last_assistant_content: str) -> bool:
        if not cls._DCR_PROMPT_PATTERN.search(last_assistant_content):
            return False
        return bool(cls._AFFIRMATIVE_PATTERN.match(user_message.strip()))

    @classmethod
    def _generate_dcr_number(cls, chat_context: str) -> str:
        digest = hashlib.md5(chat_context.encode()).hexdigest()
        numeric = int(digest[:4], 16) % 9000 + 1000
        return f"DCR-2025-{numeric}"

    @classmethod
    def _build_dcr_confirmation(cls, last_assistant_content: str, inquiry_context: dict[str, Any] | None, chat_context: str) -> str:
        dcr_number = cls._generate_dcr_number(chat_context)

        lower = last_assistant_content.lower()
        if "territory" in lower and ("exception" in lower or "alignment" in lower):
            dcr_type = "Territory Alignment Exception Request"
        elif "merge" in lower or "consolidat" in lower:
            dcr_type = "Record Merge Request"
        elif "retroactive" in lower and "credit" in lower:
            dcr_type = "Retroactive Credit Correction Request"
        elif "onboarding" in lower:
            dcr_type = "HCP Onboarding Request"
        elif "deactivat" in lower:
            dcr_type = "Deactivation and Reallocation Request"
        elif "340b" in lower:
            dcr_type = "340B Exclusion Flag Activation Request"
        elif "co-promot" in lower or "co-promote" in lower:
            dcr_type = "Co-Promotion Flag Activation Request"
        elif "mapping" in lower and ("ship" in lower or "867" in lower or "pharmacy" in lower):
            dcr_type = "Distributor Feed Mapping Correction Request"
        elif "dnc" in lower or "do not contact" in lower or "contact restriction" in lower:
            dcr_type = "Contact Restriction Scoping Request"
        else:
            dcr_type = "Data Correction Request (DCR)"

        title = ""
        if inquiry_context:
            title = str(inquiry_context.get("title", ""))

        lines = [
            f"A {dcr_type} #{dcr_number} has been submitted successfully.",
            "",
            "**Submission Details**",
        ]
        if title:
            lines.append(f"• Issue: {title}")
        lines.append(f"• Request Type: {dcr_type}")
        lines.append(f"• Reference ID: {dcr_number}")
        lines.append("• Status: Submitted - Pending Review")
        lines.append("• Assigned To: Data Governance Team")
        lines.append("")
        lines.append("The Data Governance team will review and process this request. You will receive a notification once a resolution has been applied. Typical turnaround time is 3-5 business days.")

        return "\n".join(lines)

    @classmethod
    def _detect_dcr_action(cls, resolution_text: str) -> str | None:
        if not resolution_text:
            return None
        if cls._DCR_PATTERNS.search(resolution_text):
            lower = resolution_text.lower()
            if "territory exception" in lower or "alignment exception" in lower:
                return "Would you like me to submit a territory alignment exception request for this case?"
            if "merge dcr" in lower or "consolidat" in lower:
                return "Would you like me to submit a record merge request to resolve this?"
            if "retroactive" in lower and "credit" in lower:
                return "Would you like me to submit a retroactive credit correction request?"
            if "onboarding" in lower:
                return "Would you like me to submit an HCP onboarding request for this provider?"
            if "deactivat" in lower:
                return "Would you like me to submit a deactivation and reallocation request?"
            if "340b" in lower:
                return "Would you like me to submit a 340B exclusion flag activation request?"
            if "co-promot" in lower or "co-promote" in lower:
                return "Would you like me to submit a co-promotion flag activation request?"
            if "mapping" in lower and ("ship" in lower or "867" in lower or "pharmacy" in lower):
                return "Would you like me to submit a distributor feed mapping correction request?"
            if "dnc" in lower or "do not contact" in lower or "do-not-contact" in lower:
                return "Would you like me to submit a request to scope the contact restriction?"
            return "Would you like me to submit a data correction request (DCR) for this issue?"
        return None

    def _fallback_answer(
        self,
        query: str,
        context: list[dict[str, Any]],
        confidence: float,
        decision: ResponseDecision,
    ) -> AssistantResult:
        if not context:
            return AssistantResult(
                content=(
                    "Please provide the provider NPI ID, HCO ID, or territory ID, plus the exact issue, so the "
                    "correct playbook scenario can be matched."
                ),
                matched_inquiry_id=None,
                matched_title=None,
                confidence=0.0,
            )

        best = context[0]
        if decision.follow_up:
            response = decision.follow_up
        else:
            response = self._append_dataset_reference(
                self._fallback_bullets(query, best, decision.mode),
                best,
            )

        return AssistantResult(
            content=response,
            matched_inquiry_id=str(best["inquiry_id"]),
            matched_title=str(best["title"]),
            confidence=confidence,
        )

    def generate_answer(
        self,
        user_message: str,
        conversation_history: list[dict[str, Any]] | None = None,
        chat_id: str | None = None,
    ) -> AssistantResult:
        # --- DCR confirmation flow ---
        if conversation_history:
            last_assistant = None
            last_assistant_metadata = None
            for msg in reversed(conversation_history):
                if msg.get("role") == "assistant":
                    last_assistant = msg
                    last_assistant_metadata = msg.get("metadata") or {}
                    break

            if last_assistant and self._is_dcr_confirmation(
                user_message, str(last_assistant.get("content", ""))
            ):
                inquiry_id = str(last_assistant_metadata.get("matched_inquiry_id", "")) if last_assistant_metadata else ""
                inquiry_context = self._context_from_inquiry_id(inquiry_id) if inquiry_id else None
                dcr_seed = chat_id or inquiry_id or "default"
                dcr_response = self._build_dcr_confirmation(
                    str(last_assistant.get("content", "")),
                    inquiry_context,
                    dcr_seed,
                )
                return AssistantResult(
                    content=dcr_response,
                    matched_inquiry_id=inquiry_id or None,
                    matched_title=str(inquiry_context.get("title", "")) if inquiry_context else None,
                    confidence=float(last_assistant_metadata.get("confidence") or 1.0) if last_assistant_metadata else 1.0,
                )

        follow_up_state = self._resolve_follow_up_state(user_message, conversation_history)

        if follow_up_state is not None:
            validated_identifiers, validation_follow_up = self._validate_identifier_reply(
                user_message,
                follow_up_state.locked_context,
                follow_up_state.requested_identifiers,
                follow_up_state.base_query,
            )
            if validation_follow_up:
                return AssistantResult(
                    content=validation_follow_up,
                    matched_inquiry_id=str(follow_up_state.locked_context["inquiry_id"]),
                    matched_title=str(follow_up_state.locked_context["title"]),
                    confidence=follow_up_state.confidence,
                )

            clarification_lines = [f"{label}: {value}" for label, value in validated_identifiers.items()]
            effective_query = follow_up_state.base_query
            if clarification_lines:
                effective_query += "\nClarification:\n" + "\n".join(clarification_lines)

            context = [follow_up_state.locked_context]
            confidence = follow_up_state.confidence
        else:
            effective_query = user_message
            context, confidence = self._build_context_block(effective_query)
            if not context:
                return self._fallback_answer(
                    query=effective_query,
                    context=context,
                    confidence=confidence,
                    decision=ResponseDecision(mode="vague"),
                )

        query_tokens = set(self._content_tokens(effective_query))
        matched_entity_overlap = bool(query_tokens.intersection(self._entity_tokens(context[0])))

        if confidence < 0.12 and not matched_entity_overlap and not self._query_mentions_territory(effective_query):
            decision = ResponseDecision(
                mode="follow_up",
                follow_up=(
                    "Provide the provider name, account name, or the exact symptom so the correct playbook "
                    "scenario can be matched before proceeding."
                ),
            )
        else:
            decision = self._build_decision(effective_query, context[0])

        if decision.mode == "vague" and confidence < 0.12 and not decision.follow_up:
            decision = ResponseDecision(
                mode="follow_up",
                follow_up=(
                    "Provide the provider name, account name, or the exact symptom so the correct playbook "
                    "scenario can be matched before proceeding."
                ),
            )

        fallback = self._fallback_answer(effective_query, context, confidence, decision)

        if decision.follow_up or not self.azure_client.is_ready:
            return fallback

        system_prompt = (
            "You are an enterprise CRM support analyst. "
            "Use only the provided playbook context. "
            "Treat `what_happened` as primary evidence and `resolution_and_response_to_rep` as supporting context only. "
            "Preserve the same answer structure, tone, and level of detail. "
            "Do not mention internal field names. "
            "Do not invent facts, timelines, IDs, actions, or outcomes. "
            "Do not apologize or use conversational filler. State the reason directly. "
            "Maintain a professional tone suitable for an enterprise CRM support tool."
        )

        user_prompt = (
            f"Question mode: {decision.mode}\n\n"
            "Rep question:\n"
            f"{effective_query}\n\n"
            "Allowed playbook context:\n"
            "Primary (`what_happened`):\n"
            f"{context[0]['what_happened']}\n\n"
            "Supporting (`resolution_and_response_to_rep`):\n"
            f"{context[0].get('resolution_and_response_to_rep', '')}\n\n"
            "OUTPUT FORMAT (strictly follow this structure):\n"
            "1. Start with a 1-2 sentence Summary (plain text, no header, no bullet).\n"
            "2. Then output the following sections as headers (use the exact header text on its own line, no markdown hashes):\n"
            "   Key Findings\n"
            "   Root Cause / Issue Analysis\n"
            "3. Under each section header, use concise bullet points starting with `• `. One insight per bullet.\n"
            "4. Highlight important counts, entities, and identifiers (e.g., ZIP codes, HCP counts, NPI numbers).\n"
            "5. Italicize all references to data sources, systems, and datasets by wrapping them in underscores: _dataset name_.\n"
            "6. Omit any section that has no relevant content for this scenario.\n"
            "7. Do NOT include Business Impact or Recommended Action sections.\n\n"
            "CONTENT RULES:\n"
            "1. Use only the allowed context above.\n"
            "2. Prioritize `what_happened`; use `resolution_and_response_to_rep` only to clarify or complete details when consistent.\n"
            "3. If mode is `full`, cover the full scenario across all applicable sections.\n"
            "4. If mode is `partial`, answer only the specific part asked and include only the relevant sections.\n"
            "5. If mode is `vague`, give a general answer without names, NPIs, HCO IDs, exact dates, exact ZIP codes, exact addresses, territory codes, ship-to IDs, or exact counts.\n"
            "6. If specific identifiers in the rep question align with the context, you may refer to the matched provider or account by name.\n"
            "7. Do not apologize, hedge, or add conversational filler.\n"
            "8. Do NOT add a Data Sources or Recommended Action section - those will be appended separately."
        )

        answer = self.azure_client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=900,
        )

        if not answer:
            return fallback

        answer_text = self._normalize_structured_output(answer.strip())
        if not answer_text:
            return fallback

        # Detect if a DCR or action request should be suggested
        resolution_text = str(context[0].get("resolution_and_response_to_rep", ""))
        dcr_prompt = self._detect_dcr_action(resolution_text)

        # Build final output: answer → DCR prompt → references (references always last)
        final_parts = [answer_text]
        if dcr_prompt:
            final_parts.append(f"**Recommended Action**\n{dcr_prompt}")
        final_text = "\n\n".join(final_parts)
        final_with_reference = self._append_dataset_reference(final_text, context[0])

        return AssistantResult(
            content=final_with_reference,
            matched_inquiry_id=fallback.matched_inquiry_id,
            matched_title=fallback.matched_title,
            confidence=confidence,
        )

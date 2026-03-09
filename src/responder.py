from __future__ import annotations

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
    def _requested_identifiers_from_content(content: str) -> list[str]:
        lowered = content.lower()
        requested: list[str] = []
        if "npi" in lowered or "ncp id" in lowered:
            requested.append("NCP ID")
        if "hco id" in lowered:
            requested.append("HCO ID")
        if "territory id" in lowered:
            requested.append("territory ID")
        return requested

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
        source = f"{context.get('field_rep_says', '')} {context.get('what_happened', '')}"
        provider_ids = re.findall(r"\bNPI\b\s*[:#-]?\s*(\d{10})", source, re.IGNORECASE)
        return {
            "NCP ID": provider_ids,
            "HCO ID": [match.upper() for match in re.findall(r"\bHCO\s+ID\b\s*[:#-]?\s*([A-Z0-9-]+)", source, re.IGNORECASE)],
            "territory ID": ChatResponder._extract_all_territory_ids(source),
        }

    @staticmethod
    def _extract_query_identifier_value(identifier_type: str, text: str) -> tuple[str | None, bool]:
        upper_text = text.upper()
        lower_text = text.lower()

        if identifier_type == "NCP ID":
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
        for match in PROVIDER_NAME_PATTERN.findall(
            f"{context.get('field_rep_says', '')} {context.get('what_happened', '')}"
        ):
            tokens = [token for token in normalize_text(match).split() if token]
            if tokens:
                provider_last_names.add(tokens[-1])

        if not provider_last_names:
            return False

        query_tokens = set(TOKEN_PATTERN.findall(normalized_query))
        return bool(query_tokens.intersection(provider_last_names))

    def _required_identifiers(self, query: str, context: dict[str, Any]) -> list[str]:
        required: list[str] = []
        what_happened = str(context.get("what_happened", ""))
        available_identifiers = self._extract_identifiers(what_happened)

        if "NPI" in available_identifiers and self._query_mentions_provider(query, context) and not self._query_has_provider_id(query):
            required.append("NCP ID")

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
        qualifier = "correct " if correct else ""
        if len(required_identifiers) == 1:
            return (
                f"Please provide the {qualifier}{required_identifiers[0]} so the correct record can be confirmed before proceeding."
            )

        if len(required_identifiers) == 2:
            joined = f"{required_identifiers[0]} and {required_identifiers[1]}"
        else:
            joined = ", ".join(required_identifiers[:-1]) + f", and {required_identifiers[-1]}"

        return f"Please provide the {qualifier}{joined} so the correct records can be confirmed before proceeding."

    def _validate_identifier_reply(
        self,
        reply: str,
        context: dict[str, Any],
        requested_identifiers: list[str],
    ) -> tuple[dict[str, str], str | None]:
        expected_values = self._extract_expected_identifier_values(context)
        validated: dict[str, str] = {}
        missing: list[str] = []
        invalid: list[str] = []

        for identifier_type in requested_identifiers:
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

            validated[identifier_type] = value if identifier_type == "NCP ID" else value.upper()

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
                    "Please provide the provider NCP ID, HCO ID, or territory ID, plus the exact issue, so the "
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
    ) -> AssistantResult:
        follow_up_state = self._resolve_follow_up_state(user_message, conversation_history)

        if follow_up_state is not None:
            validated_identifiers, validation_follow_up = self._validate_identifier_reply(
                user_message,
                follow_up_state.locked_context,
                follow_up_state.requested_identifiers,
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
            "You are a strict sales-operations explanation engine. "
            "Use only the provided `what_happened` context. "
            "Do not mention or use any resolution field. "
            "Do not invent facts, timelines, IDs, actions, or outcomes. "
            "Do not apologize or use conversational filler. State the reason directly. "
            "Output only bullet points, and every non-empty line must start with `• `."
        )

        user_prompt = (
            f"Question mode: {decision.mode}\n\n"
            "Rep question:\n"
            f"{effective_query}\n\n"
            "Allowed playbook context (`what_happened` only):\n"
            f"{context[0]['what_happened']}\n\n"
            "OUTPUT RULES:\n"
            "1. Use only the allowed context above.\n"
            "2. If mode is `full`, answer the full scenario in as many sensible concise bullet points as possible.\n"
            "3. If mode is `partial`, answer only the specific part asked in the rep question and exclude unrelated facts.\n"
            "4. If mode is `vague`, give a general answer without names, NPIs, HCO IDs, exact dates, exact ZIP codes, exact addresses, territory codes, ship-to IDs, or exact counts.\n"
            "5. If specific identifiers in the rep question align with the context, you may refer to the matched provider or account by name.\n"
            "6. Split complex facts into separate bullets where useful.\n"
            "7. Do not apologize, hedge, or add conversational filler.\n"
            "8. Do not add any preamble or closing sentence outside the bullets.\n"
            "9. Every line must begin with `• `."
        )

        answer = self.azure_client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=700,
        )

        if not answer:
            return fallback

        answer_text = self._normalize_bullet_output(answer.strip())
        if not answer_text:
            return fallback

        answer_with_reference = self._append_dataset_reference(answer_text, context[0])
        return AssistantResult(
            content=answer_with_reference,
            matched_inquiry_id=fallback.matched_inquiry_id,
            matched_title=fallback.matched_title,
            confidence=confidence,
        )

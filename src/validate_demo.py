from __future__ import annotations

import tempfile
from pathlib import Path

from src.azure_client import AzureOpenAIClient
from src.database import ChatStore
from src.playbook import PlaybookIndex
from src.responder import ChatResponder
from src.settings import EMBED_CACHE_PATH, PLAYBOOK_PATH, load_settings


def validate_retrieval(index: PlaybookIndex) -> tuple[int, int]:
    correct = 0
    total = 0

    for inquiry in index.inquiries:
        queries = [
            inquiry.field_rep_says,
            inquiry.title,
            inquiry.what_happened[:220],
        ]

        for query in queries:
            total += 1
            result = index.search(query, top_k=1)[0]
            if result.inquiry.inquiry_id == inquiry.inquiry_id:
                correct += 1

    return correct, total


def validate_chat_store() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_file:
        db_path = Path(temp_file.name)

    store = ChatStore(db_path)

    chat = store.create_chat("Validation Chat")
    assert chat["title"] == "Validation Chat"

    user_message = store.add_message(chat["id"], "user", "Test question")
    assistant_message = store.add_message(chat["id"], "assistant", "Test answer")

    assert user_message["role"] == "user"
    assert assistant_message["role"] == "assistant"

    messages = store.list_messages(chat["id"])
    assert len(messages) == 2

    updated = store.update_chat(chat["id"], pinned=True, archived=False)
    assert updated and updated["pinned"] is True



def main() -> None:
    settings = load_settings()
    azure_client = AzureOpenAIClient(settings)
    index = PlaybookIndex(
        playbook_path=PLAYBOOK_PATH,
        azure_client=azure_client,
        embedding_cache_path=EMBED_CACHE_PATH,
    )
    responder = ChatResponder(index=index, azure_client=azure_client)

    correct, total = validate_retrieval(index)
    retrieval_accuracy = (correct / total) * 100 if total else 0.0

    sample = index.inquiries[0].title
    answer = responder.generate_answer(sample)

    validate_chat_store()

    print(f"Playbook records loaded: {len(index.inquiries)}")
    print(f"Azure configured: {settings.can_use_azure}")
    print(f"Retrieval top-1 accuracy on internal checks: {correct}/{total} ({retrieval_accuracy:.2f}%)")
    print(f"Sample response matched inquiry: {answer.matched_inquiry_id}")
    print("Validation completed successfully.")


if __name__ == "__main__":
    main()

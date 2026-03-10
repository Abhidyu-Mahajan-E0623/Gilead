from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .azure_client import AzureOpenAIClient
from .database import ChatStore
from .playbook import PlaybookIndex
from .responder import ChatResponder
from .schemas import (
    ChatSummary,
    CreateChatRequest,
    MessageRecord,
    SendMessageRequest,
    SendMessageResponse,
    UpdateChatRequest,
)
from .settings import DB_PATH, EMBED_CACHE_PATH, PLAYBOOK_PATH, STATIC_DIR, load_settings
from .utils import auto_title_from_message


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


settings = load_settings()
azure_client = AzureOpenAIClient(settings)
chat_store = ChatStore(DB_PATH)
playbook_index = PlaybookIndex(
    playbook_path=Path(PLAYBOOK_PATH),
    azure_client=azure_client,
    embedding_cache_path=Path(EMBED_CACHE_PATH),
)
responder = ChatResponder(index=playbook_index, azure_client=azure_client)


app = FastAPI(title="Gilead Field Inquiry Demo Assistant", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "azure_configured": settings.can_use_azure,
        "playbook_records": len(playbook_index.inquiries),
    }


@app.get("/api/chats", response_model=list[ChatSummary])
def list_chats(
    include_archived: bool = Query(True, description="Include archived chats in response"),
) -> list[dict]:
    return chat_store.list_chats(include_archived=include_archived)


@app.post("/api/chats", response_model=ChatSummary)
def create_chat(payload: CreateChatRequest | None = None) -> dict:
    title = (payload.title.strip() if payload and payload.title else "") or "New Chat"
    return chat_store.create_chat(title=title)


@app.get("/api/chats/{chat_id}", response_model=ChatSummary)
def get_chat(chat_id: str) -> dict:
    chat = chat_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@app.patch("/api/chats/{chat_id}", response_model=ChatSummary)
def update_chat(chat_id: str, payload: UpdateChatRequest) -> dict:
    updated = chat_store.update_chat(
        chat_id,
        title=payload.title.strip() if payload.title is not None else None,
        pinned=payload.pinned,
        archived=payload.archived,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Chat not found")
    return updated


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str) -> dict[str, bool]:
    deleted = chat_store.delete_chat(chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"deleted": True}


@app.get("/api/chats/{chat_id}/messages", response_model=list[MessageRecord])
def list_messages(chat_id: str) -> list[dict]:
    chat = chat_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat_store.list_messages(chat_id)


@app.post("/api/chats/{chat_id}/messages", response_model=SendMessageResponse)
def send_message(chat_id: str, payload: SendMessageRequest) -> dict:
    chat = chat_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    user_message = chat_store.add_message(chat_id=chat_id, role="user", content=content)

    if chat["title"].strip().lower() in {"new chat", "untitled chat"}:
        auto_title = auto_title_from_message(content)
        updated = chat_store.update_chat(chat_id=chat_id, title=auto_title)
        if updated:
            chat = updated

    history = chat_store.list_messages(chat_id)
    assistant_result = responder.generate_answer(
        user_message=content,
        conversation_history=history,
        chat_id=chat_id,
    )

    metadata = {
        "matched_inquiry_id": assistant_result.matched_inquiry_id,
        "matched_title": assistant_result.matched_title,
        "confidence": assistant_result.confidence,
    }

    assistant_message = chat_store.add_message(
        chat_id=chat_id,
        role="assistant",
        content=assistant_result.content,
        metadata=metadata,
    )

    updated_chat = chat_store.get_chat(chat_id)
    if updated_chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    return {
        "chat": updated_chat,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "matched_inquiry_id": assistant_result.matched_inquiry_id,
        "matched_title": assistant_result.matched_title,
        "confidence": assistant_result.confidence,
    }


@app.get("/api/playbook/summary")
def playbook_summary() -> dict[str, object]:
    return {
        "count": len(playbook_index.inquiries),
        "categories": sorted({item.category for item in playbook_index.inquiries}),
    }

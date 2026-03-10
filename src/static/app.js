const state = {
  chats: [],
  activeChatId: null,
  sending: false,
  menuChatId: null,
  archivedExpanded: false,
  renameChatId: null,
};
const LOADING_PHASES = [
  { label: "Analyzing your query", durationMs: 2000 },
  { label: "Formulating a Response", durationMs: 2000 },
];

const dom = {
  appShell: document.getElementById("app-shell"),
  sidebar: document.getElementById("sidebar"),
  pinnedList: document.getElementById("pinned-list"),
  recentList: document.getElementById("recent-list"),
  archivedList: document.getElementById("archived-list"),
  archivedToggle: document.getElementById("archived-toggle"),
  newChatBtn: document.getElementById("new-chat-btn"),
  collapseSidebarBtn: document.getElementById("collapse-sidebar"),
  openSidebarBtn: document.getElementById("open-sidebar"),
  activeTitle: document.getElementById("active-title"),
  stream: document.getElementById("message-stream"),
  input: document.getElementById("chat-input"),
  sendBtn: document.getElementById("send-btn"),
  menu: document.getElementById("context-menu"),
  renameModal: document.getElementById("rename-modal"),
  renameInput: document.getElementById("rename-input"),
  renameCancel: document.getElementById("rename-cancel"),
  renameSave: document.getElementById("rename-save"),
  typingTemplate: document.getElementById("typing-template"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });

  if (!response.ok) {
    let detail = "Request failed";
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      detail = `Request failed (${response.status})`;
    }
    throw new Error(detail);
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function formatMessageContent(message) {
  const escaped = escapeHtml(String(message.content || ""));

  if (message.role !== "assistant") {
    return escaped.replace(/\n/g, "<br>");
  }

  let formatted = escaped;

  // Bold section headers: **Header Text**
  formatted = formatted.replace(
    /\*\*([^*]+)\*\*/g,
    '<strong style="display:block;margin-top:10px;margin-bottom:2px;font-size:0.92rem;">$1</strong>',
  );

  // Italic dataset/system references: _text_ (but not __double__)
  formatted = formatted.replace(
    /(?<!\w)_([^_]+?)_(?!\w)/g,
    "<em>$1</em>",
  );

  // Reference line styling (at the end of the message)
  formatted = formatted.replace(
    /(^|\n)(Reference(?:s)?\s+from[^\n]*)/gim,
    (_, prefix, referenceLine) =>
      `${prefix}<span class="meta-line"><em>${referenceLine}</em></span>`,
  );

  // "Would you like me to..." action prompt
  formatted = formatted.replace(
    /(^|\n)(Would you like me to[^\n]*)/gim,
    (_, prefix, actionLine) =>
      `${prefix}<span style="display:block;margin-top:4px;font-style:italic;color:#3B1018;">${actionLine}</span>`,
  );

  let html = formatted.replace(/\n/g, "<br>");

  // Collapse extra <br> around block-level elements (headings & action prompts)
  html = html.replace(/(<br>)+\s*(<strong\b)/gi, "$2");
  html = html.replace(/(<\/strong>)\s*(<br>)+/gi, "$1");
  html = html.replace(/(<br>)+\s*(<span\b[^>]*display:\s*block)/gi, "$2");
  html = html.replace(/(<\/span>)\s*(<br>)+/gi, "$1");
  // Clean leading <br>
  html = html.replace(/^(<br>)+/, "");

  return html;
}

function relativeTime(isoValue) {
  const then = new Date(isoValue).getTime();
  const now = Date.now();
  const diffSec = Math.max(1, Math.round((now - then) / 1000));

  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}d ago`;
}

function updateSendButtonState() {
  const hasText = dom.input.value.trim().length > 0;
  dom.sendBtn.disabled = !hasText || state.sending;
}

function autoresizeInput() {
  dom.input.style.height = "auto";
  dom.input.style.height = `${Math.min(dom.input.scrollHeight, 200)}px`;
}

function groupedChats() {
  const pinned = state.chats.filter((chat) => chat.pinned && !chat.archived);
  const recent = state.chats.filter((chat) => !chat.pinned && !chat.archived);
  const archived = state.chats.filter((chat) => chat.archived);
  return { pinned, recent, archived };
}

function chatCardMarkup(chat) {
  const activeClass = chat.id === state.activeChatId ? "active" : "";
  const preview = chat.last_message_preview
    ? escapeHtml(chat.last_message_preview)
    : "No messages yet";

  return `
    <div class="chat-item ${activeClass}" data-chat-id="${chat.id}">
      <span class="chat-title">${escapeHtml(chat.title)}</span>
      <span class="chat-preview">${preview}</span>
      <span class="meta-dot">${relativeTime(chat.updated_at)}</span>
      <button class="menu-trigger" data-action="menu" data-chat-id="${chat.id}" aria-label="Chat options">...</button>
    </div>
  `;
}

function renderChatGroup(container, chats, emptyText) {
  if (!chats.length) {
    container.innerHTML = `<div class="chat-item" style="opacity:.75;cursor:default;">${emptyText}</div>`;
    return;
  }

  container.innerHTML = chats.map(chatCardMarkup).join("");
}

function renderChatLists() {
  const { pinned, recent, archived } = groupedChats();
  renderChatGroup(dom.pinnedList, pinned, "No pinned chats");
  renderChatGroup(dom.recentList, recent, "No recent chats");
  renderChatGroup(dom.archivedList, archived, "No archived chats");

  const active = state.chats.find((chat) => chat.id === state.activeChatId);
  dom.activeTitle.textContent = active ? active.title : "New Chat";
}

function closeContextMenu() {
  dom.menu.hidden = true;
  state.menuChatId = null;
}

function openContextMenu(chatId, trigger) {
  const chat = state.chats.find((item) => item.id === chatId);
  if (!chat) return;

  state.menuChatId = chatId;
  dom.menu.hidden = false;

  const pinBtn = dom.menu.querySelector('[data-action="pin"]');
  const archiveBtn = dom.menu.querySelector('[data-action="archive"]');

  pinBtn.textContent = chat.pinned ? "Unpin Chat" : "Pin Chat";
  archiveBtn.textContent = chat.archived ? "Unarchive" : "Archive";

  const rect = trigger.getBoundingClientRect();
  const menuWidth = 168;
  const menuHeight = 154;
  const left = Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 12);
  const top = Math.min(rect.bottom + 6, window.innerHeight - menuHeight - 12);
  dom.menu.style.left = `${Math.max(12, left)}px`;
  dom.menu.style.top = `${Math.max(12, top)}px`;
}

function renderEmptyState() {
  dom.stream.innerHTML = `
    <div class="empty-state">
      <h3>Hello, I am your Gilead Assistant</h3>
      <p>How can I help you today? Ask me any field inquiry questions to get started.</p>
    </div>
  `;
}

function messageMarkup(message) {
  const bubbleClass = message.role === "user" ? "user-bubble" : "assistant-bubble";
  const rowClass = message.role === "user" ? "user" : "assistant";

  return `
    <div class="message-row ${rowClass}">
      <div class="bubble ${bubbleClass}">${formatMessageContent(message)}</div>
    </div>
  `;
}

function renderMessages(messages) {
  if (!messages.length) {
    renderEmptyState();
    return;
  }

  dom.stream.innerHTML = messages.map(messageMarkup).join("");
  dom.stream.scrollTop = dom.stream.scrollHeight;
}

function appendUserMessage(text) {
  const html = messageMarkup({ role: "user", content: text });
  dom.stream.insertAdjacentHTML("beforeend", html);
  dom.stream.scrollTop = dom.stream.scrollHeight;
}

function appendErrorMessage(text) {
  const html = messageMarkup({ role: "assistant", content: text, metadata: null });
  dom.stream.insertAdjacentHTML("beforeend", html);
  dom.stream.scrollTop = dom.stream.scrollHeight;
}

function appendTypingBubble() {
  const node = dom.typingTemplate.content.firstElementChild.cloneNode(true);
  if (dom.stream.querySelector(".empty-state")) {
    dom.stream.innerHTML = "";
  }
  dom.stream.appendChild(node);
  dom.stream.scrollTop = dom.stream.scrollHeight;
  return node;
}

function startLoadingSequence(typingNode) {
  const inlineTextElement = typingNode.querySelector("[data-loading-inline-text]");
  const loadingBubble = typingNode.querySelector(".loading-bubble");
  let stopped = false;

  const setStage = (label, index) => {
    if (stopped) return;

    const applyChange = () => {
      if (inlineTextElement) {
        inlineTextElement.textContent = label;
      }
      if (loadingBubble) {
        loadingBubble.dataset.phase = String(index);
      }
    };

    /* First phase: just fade in */
    if (index === 0) {
      applyChange();
      if (inlineTextElement) {
        inlineTextElement.style.transition = "none";
        inlineTextElement.style.opacity = "0";
        inlineTextElement.style.transform = "translateY(4px)";
        window.requestAnimationFrame(() => {
          inlineTextElement.style.transition = "opacity 350ms ease, transform 350ms ease";
          inlineTextElement.style.opacity = "1";
          inlineTextElement.style.transform = "translateY(0)";
        });
      }
      return;
    }

    /* Subsequent phases: fade out → swap → fade in */
    if (inlineTextElement) {
      inlineTextElement.style.transition = "opacity 250ms ease, transform 250ms ease";
      inlineTextElement.style.opacity = "0";
      inlineTextElement.style.transform = "translateY(-4px)";
    }

    window.setTimeout(() => {
      if (stopped) return;
      applyChange();
      if (inlineTextElement) {
        inlineTextElement.style.transition = "none";
        inlineTextElement.style.transform = "translateY(4px)";
        window.requestAnimationFrame(() => {
          inlineTextElement.style.transition = "opacity 350ms ease, transform 350ms ease";
          inlineTextElement.style.opacity = "1";
          inlineTextElement.style.transform = "translateY(0)";
        });
      }
    }, 260);
  };

  const minimumDelayPromise = (async () => {
    for (let index = 0; index < LOADING_PHASES.length; index += 1) {
      const phase = LOADING_PHASES[index];
      setStage(phase.label, index);
      await delay(phase.durationMs);
    }
  })();

  return {
    minimumDelayPromise,
    stop() {
      stopped = true;
    },
  };
}

async function loadChats() {
  state.chats = await api("/api/chats?include_archived=true");
  renderChatLists();
}

async function createChat(autoSelect = true) {
  const chat = await api("/api/chats", {
    method: "POST",
    body: JSON.stringify({}),
  });

  await loadChats();

  if (autoSelect) {
    await selectChat(chat.id);
  }

  return chat;
}

async function selectChat(chatId) {
  state.activeChatId = chatId;
  renderChatLists();

  const messages = await api(`/api/chats/${chatId}/messages`);
  renderMessages(messages);

  if (window.innerWidth <= 980) {
    dom.appShell.classList.remove("mobile-open");
  }
}

async function patchChat(chatId, payload) {
  await api(`/api/chats/${chatId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });

  await loadChats();

  if (chatId === state.activeChatId) {
    const active = state.chats.find((chat) => chat.id === chatId);
    dom.activeTitle.textContent = active ? active.title : "New Chat";
  }
}

async function deleteChat(chatId) {
  await api(`/api/chats/${chatId}`, { method: "DELETE" });
  const wasActive = chatId === state.activeChatId;

  await loadChats();

  if (!state.chats.length) {
    const chat = await createChat(false);
    await selectChat(chat.id);
    return;
  }

  if (wasActive) {
    const fallback = state.chats.find((chat) => !chat.archived) || state.chats[0];
    await selectChat(fallback.id);
  }
}

async function sendMessage() {
  const content = dom.input.value.trim();
  if (!content || state.sending) return;

  if (!state.activeChatId) {
    const chat = await createChat(false);
    state.activeChatId = chat.id;
  }

  state.sending = true;
  updateSendButtonState();

  dom.input.value = "";
  autoresizeInput();

  if (dom.stream.querySelector(".empty-state")) {
    dom.stream.innerHTML = "";
  }

  appendUserMessage(content);
  const typingNode = appendTypingBubble();
  const loadingSequence = startLoadingSequence(typingNode);

  try {
    const [payload] = await Promise.all([
      api(`/api/chats/${state.activeChatId}/messages`, {
        method: "POST",
        body: JSON.stringify({ content }),
      }),
      loadingSequence.minimumDelayPromise,
    ]);

    typingNode.remove();
    await loadChats();

    if (payload.chat && payload.chat.id !== state.activeChatId) {
      state.activeChatId = payload.chat.id;
    }

    const messages = await api(`/api/chats/${state.activeChatId}/messages`);
    renderMessages(messages);
  } catch (error) {
    loadingSequence.stop();
    typingNode.remove();
    appendErrorMessage(`Error: ${error.message}`);
  } finally {
    state.sending = false;
    updateSendButtonState();
  }
}

function openRenameModal(chatId) {
  const chat = state.chats.find((item) => item.id === chatId);
  if (!chat) return;

  state.renameChatId = chatId;
  dom.renameInput.value = chat.title;
  dom.renameModal.hidden = false;
  dom.renameInput.focus();
  dom.renameInput.select();
}

function closeRenameModal() {
  state.renameChatId = null;
  dom.renameModal.hidden = true;
}

async function handleSidebarClick(event) {
  const menuTrigger = event.target.closest('[data-action="menu"]');
  if (menuTrigger) {
    event.stopPropagation();
    openContextMenu(menuTrigger.dataset.chatId, menuTrigger);
    return;
  }

  const chatItem = event.target.closest(".chat-item[data-chat-id]");
  if (chatItem) {
    closeContextMenu();
    await selectChat(chatItem.dataset.chatId);
  }
}

async function handleMenuAction(action, chatId) {
  if (!chatId) return;

  const chat = state.chats.find((item) => item.id === chatId);
  if (!chat) return;

  if (action === "pin") {
    await patchChat(chatId, { pinned: !chat.pinned });
  }

  if (action === "rename") {
    openRenameModal(chatId);
  }

  if (action === "archive") {
    await patchChat(chatId, { archived: !chat.archived });
    if (chatId === state.activeChatId) {
      const fallback = state.chats.find((item) => !item.archived) || state.chats[0];
      if (fallback) {
        await selectChat(fallback.id);
      }
    }
  }

  if (action === "delete") {
    const confirmed = window.confirm("Delete this chat permanently?");
    if (confirmed) {
      await deleteChat(chatId);
    }
  }
}

function bindEvents() {
  dom.newChatBtn.addEventListener("click", async () => {
    closeContextMenu();
    const chat = await createChat(false);
    await selectChat(chat.id);
  });

  dom.pinnedList.addEventListener("click", handleSidebarClick);
  dom.recentList.addEventListener("click", handleSidebarClick);
  dom.archivedList.addEventListener("click", handleSidebarClick);

  dom.archivedToggle.addEventListener("click", () => {
    state.archivedExpanded = !state.archivedExpanded;
    dom.archivedList.hidden = !state.archivedExpanded;
    dom.archivedToggle.setAttribute("aria-expanded", String(state.archivedExpanded));
  });

  dom.collapseSidebarBtn.addEventListener("click", () => {
    dom.appShell.classList.toggle("collapsed");
  });

  dom.openSidebarBtn.addEventListener("click", () => {
    dom.appShell.classList.toggle("mobile-open");
  });

  dom.input.addEventListener("input", () => {
    autoresizeInput();
    updateSendButtonState();
  });

  dom.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });

  dom.sendBtn.addEventListener("click", sendMessage);

  document.addEventListener("click", (event) => {
    if (dom.menu.hidden) return;
    if (event.target.closest("#context-menu") || event.target.closest('[data-action="menu"]')) {
      return;
    }
    closeContextMenu();
  });

  dom.menu.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;

    const action = button.dataset.action;
    const chatId = state.menuChatId;
    closeContextMenu();
    await handleMenuAction(action, chatId);
  });

  dom.renameCancel.addEventListener("click", closeRenameModal);

  dom.renameSave.addEventListener("click", async () => {
    const title = dom.renameInput.value.trim();
    if (!state.renameChatId || !title) {
      closeRenameModal();
      return;
    }

    await patchChat(state.renameChatId, { title });
    closeRenameModal();
  });

  dom.renameInput.addEventListener("keydown", async (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      dom.renameSave.click();
    }
  });

  dom.renameModal.addEventListener("click", (event) => {
    if (event.target === dom.renameModal) {
      closeRenameModal();
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 980) {
      dom.appShell.classList.remove("mobile-open");
    }
  });
}

async function init() {
  bindEvents();
  autoresizeInput();
  updateSendButtonState();

  await loadChats();

  if (!state.chats.length) {
    const chat = await createChat(false);
    state.activeChatId = chat.id;
    renderChatLists();
    renderEmptyState();
    return;
  }

  const preferred = state.chats.find((chat) => !chat.archived) || state.chats[0];
  if (preferred) {
    await selectChat(preferred.id);
  } else {
    renderEmptyState();
  }
}

init().catch((error) => {
  renderEmptyState();
  appendErrorMessage(`Startup error: ${error.message}`);
});


"""Fase 2b - Modelo: do registro bruto do suplemento para uma conversa "limpa".

Formato real do registro (confirmado em um armazenamento de nov/2026)
---------------------------------------------------------------------
Banco ``claude-chat-history``, store ``chats``, chave = id da conversa::

    id, userId, orgId, fileId, surface, hostOrigin, title, createdAt, updatedAt
    snapshot:
        version: 1
        ui:    messages[]   <- o que aparece na tela (é o que exportamos)
        api:   transcript[] <- o que foi enviado ao modelo (role + blocos)
        agent: ...          <- estado interno (resumos de contexto etc.)

Cada item de ``snapshot.ui.messages`` tem ``uuid``, ``timestamp`` e ``type``:

* ``user`` / ``assistant`` -> ``content`` (texto); o assistente traz ``model``;
* ``thinking``             -> ``content`` (raciocínio) e ``endTimestamp``;
* ``tool``                 -> ``name``, ``status`` (complete/error), ``input``,
  ``result`` ou ``error``, e ``images`` (lista de ``{data: base64, mediaType}``).

Store ``blobs`` (chave = [chatId, nome]): dados auxiliares da conversa, em
``{chatId, name, bytes}``.

Como esse formato é interno e pode mudar, todo acesso aqui é DEFENSIVO: campo
ausente ou de tipo inesperado vira None/lista vazia em vez de erro. Se
``ui.messages`` não existir, caímos para ``api.transcript``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .reader import UNDEFINED, SnapshotReader, TypedArray

CHAT_DATABASE = "claude-chat-history"
CHATS_STORE = "chats"
BLOBS_STORE = "blobs"
KNOWN_SNAPSHOT_VERSION = 1

# "surface" é o campo que diz em qual app a conversa aconteceu. "sheet" (Excel)
# e "slide" (PowerPoint) foram confirmados em dados reais; os demais são os
# nomes mais prováveis e,
# se não baterem, o valor original é mostrado como está.
_SURFACE_APPS = {
    "sheet": "Excel",
    "doc": "Word", "document": "Word",
    "slide": "PowerPoint", "slides": "PowerPoint", "presentation": "PowerPoint",
    "mail": "Outlook", "email": "Outlook",
}


@dataclass
class Image:
    media_type: str
    data: bytes
    sha256: str


@dataclass
class Attachment:
    name: str
    data: bytes


@dataclass
class Message:
    kind: str  # user | assistant | thinking | tool | (outro valor futuro)
    uuid: str | None = None
    timestamp: datetime | None = None
    end_timestamp: datetime | None = None
    text: str | None = None
    model: str | None = None
    effort: str | None = None
    api_id: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    tool_input: Any = None
    tool_result: Any = None
    tool_error: str | None = None
    result_chars: int | None = None
    images: list[Image] = field(default_factory=list)
    files: Any = None
    extra: dict = field(default_factory=dict)  # campos que ainda não conhecemos


@dataclass
class Conversation:
    id: str
    title: str | None = None
    surface: str | None = None
    file_id: str | None = None
    user_id: str | None = None
    org_id: str | None = None
    host_origin: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    messages: list[Message] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # Preenchidos só quando a conversa vem do arquivo permanente (Fase 3):
    # mensagens que existiram em versões anteriores e sumiram da versão atual,
    # e desde quando a conversa inteira não aparece mais na origem.
    absent_messages: list[Message] = field(default_factory=list)
    missing_since: datetime | None = None
    message_source: str = "none"  # "ui" | "transcript" | "none"
    snapshot_version: int | None = None
    seq: int = 0

    @property
    def app(self) -> str:
        if not self.surface:
            return "desconhecido"
        return _SURFACE_APPS.get(self.surface.lower(), self.surface)


# ----------------------------------------------------------------- utilidades
def _clean(value: Any) -> Any:
    """``undefined`` do JavaScript vira None."""
    return None if value is UNDEFINED else value


def _text(value: Any) -> str | None:
    value = _clean(value)
    return value if isinstance(value, str) else None


def _to_datetime(value: Any) -> datetime | None:
    """Aceita Date do JavaScript (já convertido) ou milissegundos desde 1970."""
    value = _clean(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _decode_image(raw: Any, media_type: Any) -> Image | None:
    """base64 (ou bytes) -> Image. Devolve None se não for decodificável."""
    media = _text(media_type) or "application/octet-stream"
    if isinstance(raw, bytes):
        data = raw
    elif isinstance(raw, str):
        if raw.startswith("data:") and "," in raw:  # "data:image/png;base64,...."
            header, raw = raw.split(",", 1)
            media = header[5:].split(";")[0] or media
        try:
            data = base64.b64decode(raw, validate=False)
        except (binascii.Error, ValueError):
            return None
    else:
        return None
    if not data:
        return None
    return Image(media, data, hashlib.sha256(data).hexdigest())


def _images(items: Any) -> list[Image]:
    result: list[Image] = []
    for item in _clean(items) or []:
        if isinstance(item, dict):
            image = _decode_image(item.get("data"), item.get("mediaType") or item.get("media_type"))
            if image:
                result.append(image)
    return result


# ------------------------------------------------- mensagens: formato da tela
_KNOWN_UI_KEYS = {
    "uuid", "timestamp", "endTimestamp", "type", "content", "apiId", "model", "effort",
    "name", "status", "input", "result", "error", "resultChars", "images", "files",
}


def _message_from_ui(item: dict) -> Message:
    result_chars = _clean(item.get("resultChars"))
    extra = {k: _clean(v) for k, v in item.items()
             if k not in _KNOWN_UI_KEYS and _clean(v) is not None}
    return Message(
        kind=_text(item.get("type")) or "desconhecido",
        uuid=_text(item.get("uuid")),
        timestamp=_to_datetime(item.get("timestamp")),
        end_timestamp=_to_datetime(item.get("endTimestamp")),
        text=_text(item.get("content")),
        model=_text(item.get("model")),
        effort=_text(item.get("effort")),
        api_id=_text(item.get("apiId")),
        tool_name=_text(item.get("name")),
        tool_status=_text(item.get("status")),
        tool_input=_clean(item.get("input")),
        tool_result=_clean(item.get("result")),
        tool_error=_text(item.get("error")),
        result_chars=result_chars if isinstance(result_chars, int) else None,
        images=_images(item.get("images")),
        files=_clean(item.get("files")),
        extra=extra,
    )


# ----------------------------------------- mensagens: formato da API (reserva)
def _messages_from_transcript(transcript: list) -> list[Message]:
    """Converte ``api.transcript`` (role + blocos) quando não há ``ui.messages``.

    Um bloco ``tool_use`` (o modelo pede uma ferramenta) e o ``tool_result``
    correspondente (a resposta) são unidos em uma única mensagem "tool".
    """
    messages: list[Message] = []
    tools_by_id: dict[str, Message] = {}
    for entry in transcript:
        if not isinstance(entry, dict):
            continue
        role = _text(entry.get("role")) or "desconhecido"
        content = _clean(entry.get("content"))
        if isinstance(content, str):
            messages.append(Message(kind=role, text=content))
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = _text(block.get("type")) or ""
            if btype == "text":
                messages.append(Message(kind=role, text=_text(block.get("text"))))
            elif btype == "thinking":
                messages.append(Message(kind="thinking", text=_text(block.get("thinking"))))
            elif btype in ("tool_use", "server_tool_use"):
                tool = Message(kind="tool", tool_name=_text(block.get("name")),
                               tool_input=_clean(block.get("input")))
                messages.append(tool)
                if _text(block.get("id")):
                    tools_by_id[block["id"]] = tool
            elif btype.endswith("tool_result"):
                tool = tools_by_id.get(_text(block.get("tool_use_id")) or "")
                if tool is None:
                    tool = Message(kind="tool")
                    messages.append(tool)
                inner = _clean(block.get("content"))
                if isinstance(inner, list):
                    texts = []
                    for part in inner:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text" and _text(part.get("text")):
                            texts.append(part["text"])
                        source = part.get("source")
                        if part.get("type") == "image" and isinstance(source, dict):
                            image = _decode_image(source.get("data"), source.get("media_type"))
                            if image:
                                tool.images.append(image)
                    tool.tool_result = "\n".join(texts) or None
                else:
                    tool.tool_result = inner
                is_error = _clean(block.get("is_error")) is True
                tool.tool_status = "error" if is_error else "complete"
                if is_error and isinstance(tool.tool_result, str):
                    tool.tool_error, tool.tool_result = tool.tool_result, None
    return messages


# ------------------------------------------------------------------ conversa
def conversation_from_record(value: dict, attachments: list[Attachment] | None = None,
                             *, key: Any = None, seq: int = 0) -> Conversation:
    """Monta uma Conversation a partir do valor bruto do store ``chats``."""
    conv_id = _text(value.get("id")) or (key if isinstance(key, str) else str(key))
    conv = Conversation(
        id=conv_id,
        title=_text(value.get("title")),
        surface=_text(value.get("surface")),
        file_id=_text(value.get("fileId")),
        user_id=_text(value.get("userId")),
        org_id=_text(value.get("orgId")),
        host_origin=_text(value.get("hostOrigin")),
        created_at=_to_datetime(value.get("createdAt")),
        updated_at=_to_datetime(value.get("updatedAt")),
        attachments=list(attachments or []),
        seq=seq,
    )
    snapshot = _clean(value.get("snapshot"))
    if not isinstance(snapshot, dict):
        return conv
    version = _clean(snapshot.get("version"))
    conv.snapshot_version = version if isinstance(version, int) else None

    ui = _clean(snapshot.get("ui"))
    ui_messages = _clean(ui.get("messages")) if isinstance(ui, dict) else None
    if isinstance(ui_messages, list):
        # Lista presente, mesmo vazia, é a palavra final: uma conversa sem
        # mensagens na tela não deve ser "preenchida" com o transcript da API.
        conv.messages = [_message_from_ui(m) for m in ui_messages if isinstance(m, dict)]
        conv.message_source = "ui"
        return conv

    api = _clean(snapshot.get("api"))
    transcript = _clean(api.get("transcript")) if isinstance(api, dict) else None
    if isinstance(transcript, list) and transcript:
        conv.messages = _messages_from_transcript(transcript)
        conv.message_source = "transcript"
    return conv


def check_schema(reader: SnapshotReader) -> list[str]:
    """Confere se o snapshot tem o esquema que conhecemos. Devolve avisos."""
    warnings: list[str] = []
    schema = reader.schema()
    if CHAT_DATABASE not in schema:
        warnings.append(f"banco '{CHAT_DATABASE}' não existe neste snapshot")
        return warnings
    for store in (CHATS_STORE, BLOBS_STORE):
        if store not in schema[CHAT_DATABASE]:
            warnings.append(f"store '{store}' não existe em '{CHAT_DATABASE}'")
    return warnings


def load_conversations(reader: SnapshotReader) -> tuple[list[Conversation], list[str]]:
    """Lê todas as conversas (versão mais nova de cada uma) e seus anexos."""
    warnings = check_schema(reader)
    schema = reader.schema()
    if CHATS_STORE not in schema.get(CHAT_DATABASE, []):
        return [], warnings

    attachments: dict[str, list[Attachment]] = {}
    if BLOBS_STORE in schema[CHAT_DATABASE]:
        for record in reader.records(CHAT_DATABASE, BLOBS_STORE):
            if record.deleted or not isinstance(record.value, dict):
                continue
            chat_id = _text(record.value.get("chatId"))
            name = _text(record.value.get("name"))
            data = _clean(record.value.get("bytes"))
            if isinstance(record.key, list) and len(record.key) == 2:
                chat_id = chat_id or (record.key[0] if isinstance(record.key[0], str) else None)
                name = name or (record.key[1] if isinstance(record.key[1], str) else None)
            if isinstance(data, TypedArray):
                data = data.data
            if isinstance(data, str):
                try:
                    data = data.encode("latin-1")  # texto de um byte: volta aos bytes originais
                except UnicodeEncodeError:
                    data = data.encode("utf-8")
            if chat_id and name and isinstance(data, bytes):
                attachments.setdefault(chat_id, []).append(Attachment(name, data))

    conversations: list[Conversation] = []
    for record in reader.records(CHAT_DATABASE, CHATS_STORE):
        if record.deleted or not isinstance(record.value, dict):
            continue
        conv = conversation_from_record(record.value, key=record.key, seq=record.seq)
        conv.attachments = sorted(attachments.get(conv.id, []), key=lambda a: a.name)
        if conv.snapshot_version not in (None, KNOWN_SNAPSHOT_VERSION):
            warnings.append(f"conversa {conv.id}: snapshot.version={conv.snapshot_version} "
                            f"(esta ferramenta foi testada com a versão {KNOWN_SNAPSHOT_VERSION})")
        if conv.message_source != "ui":
            warnings.append(f"conversa {conv.id}: sem ui.messages; origem das mensagens = "
                            f"{conv.message_source}")
        conversations.append(conv)
    conversations.sort(key=lambda c: (c.created_at or datetime.min.replace(tzinfo=timezone.utc), c.id))
    return conversations, warnings

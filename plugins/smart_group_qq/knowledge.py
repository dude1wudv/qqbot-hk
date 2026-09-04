# -*- coding: utf-8 -*-
"""Group-isolated knowledge-base helpers for the QQ plugin.

This module deliberately keeps the persistence boundary small.  The running
plugin can provide a SQLite-backed object implementing :class:`KnowledgeStore`
(``Store`` is expected to grow these methods), while ``InMemoryKnowledgeStore``
is useful for tests and local development.  Indexing and search are
deterministic and lexical; a later integration may replace the implementation
behind the same interface without changing command or extraction callers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol
from xml.etree import ElementTree


DEFAULT_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DOCUMENT_CHARS = 200_000
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 120
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 50

SUPPORTED_EXTENSIONS = frozenset(
    {
        ".txt",
        ".text",
        ".md",
        ".markdown",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".toml",
        ".pdf",
        ".docx",
    }
)
TEXT_EXTENSIONS = frozenset(SUPPORTED_EXTENSIONS - {".pdf", ".docx"})


class KnowledgeError(ValueError):
    """Base error for invalid knowledge-base input."""


class UnsupportedDocumentError(KnowledgeError):
    """Raised when a document type cannot be extracted safely."""


class DocumentTooLargeError(KnowledgeError):
    """Raised when a document exceeds a byte or character budget."""


class UnsafePathError(KnowledgeError):
    """Raised when an uploaded/cache path escapes its configured root."""


class KnowledgeStore(Protocol):
    """Persistence contract used by :class:`KnowledgeBase`.

    Implementations must scope every operation by ``group_id``.  The methods
    intentionally match the API planned for the plugin's SQLite ``Store``.
    """

    def add_knowledge_document(
        self,
        group_id: str,
        title: str,
        text: str,
        source: str,
        created_by: str,
        message_id: str,
        chunk_size: int,
        overlap: int,
    ) -> dict[str, Any]: ...

    def list_knowledge_documents(self, group_id: str) -> list[dict[str, Any]]: ...

    def remove_knowledge_document(self, group_id: str, doc_id: str) -> bool: ...

    def clear_knowledge(self, group_id: str) -> int: ...

    def search_knowledge(
        self, group_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]: ...


def _as_nonempty(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise KnowledgeError(f"{field} cannot be empty")
    return result


def normalize_document_text(value: Any) -> str:
    """Normalize line endings and surrounding whitespace for stored text."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise KnowledgeError("document text cannot be empty")
    return text


def validate_document_limits(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> str:
    """Validate a decoded document and return its normalized text."""

    normalized = normalize_document_text(text)
    max_chars = int(max_chars)
    if max_chars < 1:
        raise KnowledgeError("max_chars must be positive")
    if len(normalized) > max_chars:
        raise DocumentTooLargeError(
            f"document has {len(normalized)} characters; limit is {max_chars}"
        )
    return normalized


def validate_cache_path(path: str | os.PathLike[str], cache_dir: str | os.PathLike[str]) -> Path:
    """Return a regular file below ``cache_dir`` after path safety checks.

    ``resolve`` plus ``relative_to`` prevents ``..`` traversal and symlinks
    pointing outside the cache.  Symlinks are rejected even when they point
    back inside the cache so an upload cannot change its target after the
    validation step.
    """

    root = Path(cache_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise UnsafePathError("cache_dir must be a directory")
    candidate_input = Path(path).expanduser()
    if candidate_input.is_symlink():
        raise UnsafePathError("symlinked document paths are not allowed")
    try:
        candidate = candidate_input.resolve(strict=True)
        candidate.relative_to(root)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise UnsafePathError("document path is outside the cache directory") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise UnsafePathError("document path must be a regular file")
    return candidate


def _validate_extension(filename: str | os.PathLike[str]) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise UnsupportedDocumentError(
            f"unsupported document extension {extension or '<none>'}; supported: {supported}"
        )
    return extension


def _decode_text(data: bytes) -> str:
    # UTF-8 is the common path.  gb18030 keeps Chinese plain-text uploads
    # useful without introducing a third-party charset detector.
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnsupportedDocumentError("document is not valid UTF-8 or GB18030 text")


def _extract_xml_text(data: bytes) -> str:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise UnsupportedDocumentError("invalid XML document") from exc
    pieces = [piece.strip() for piece in root.itertext() if piece.strip()]
    return "\n".join(pieces)


def _extract_docx(data: bytes, *, max_bytes: int) -> str:
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            total_uncompressed = 0
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or any(part == ".." for part in name.split("/")):
                    raise UnsafePathError("DOCX archive contains an unsafe member path")
                total_uncompressed += max(0, int(info.file_size))
                if total_uncompressed > max_bytes:
                    raise DocumentTooLargeError("DOCX uncompressed content exceeds byte limit")
            try:
                document_xml = archive.read("word/document.xml")
            except KeyError as exc:
                raise UnsupportedDocumentError("DOCX has no word/document.xml") from exc
    except zipfile.BadZipFile as exc:
        raise UnsupportedDocumentError("invalid DOCX archive") from exc

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise UnsupportedDocumentError("invalid DOCX document XML") from exc
    # Word paragraphs are represented by <w:p>; line breaks inside a run are
    # represented by <w:br>.  ElementTree's namespace-agnostic local-name
    # check keeps extraction compatible with normal DOCX namespace variants.
    paragraphs: list[str] = []
    for paragraph in root.iter():
        if str(paragraph.tag).rsplit("}", 1)[-1] != "p":
            continue
        pieces: list[str] = []
        for node in paragraph.iter():
            local = str(node.tag).rsplit("}", 1)[-1]
            if local == "t" and node.text:
                pieces.append(node.text)
            elif local == "br":
                pieces.append("\n")
        value = "".join(pieces).strip()
        if value:
            paragraphs.append(value)
    return "\n".join(paragraphs)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UnsupportedDocumentError(
            "PDF extraction requires the optional pypdf package"
        ) from exc
    try:
        reader = PdfReader(__import__("io").BytesIO(data))
        pages = [str(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:  # pypdf exposes several parser-specific errors
        raise UnsupportedDocumentError("invalid or unreadable PDF document") from exc
    return "\n\n".join(page for page in pages if page)


def extract_document(
    source: str | bytes | os.PathLike[str],
    *,
    filename: str | os.PathLike[str] | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
    max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
) -> str:
    """Extract text from a supported path or byte payload.

    ``bytes`` requires ``filename`` so the extension can be checked.  A string
    is treated as a file path only when it points to an existing file; callers
    with raw text should pass ``bytes`` plus a filename or use
    :func:`validate_document_limits` directly.  When ``cache_dir`` is given,
    every path is required to resolve below that directory.
    """

    max_bytes = int(max_bytes)
    if max_bytes < 1:
        raise KnowledgeError("max_bytes must be positive")
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        if filename is None:
            raise KnowledgeError("filename is required for byte document input")
        display_name = filename
    else:
        candidate = Path(source)
        if not candidate.exists():
            # A non-existing string is a useful error, not an attempt to read
            # an arbitrary path supplied as document text.
            raise FileNotFoundError(str(candidate))
        if cache_dir is not None:
            candidate = validate_cache_path(candidate, cache_dir)
        elif candidate.is_symlink() or not candidate.is_file():
            raise UnsafePathError("document path must be a regular file")
        display_name = filename or candidate.name
        size = candidate.stat().st_size
        if size > max_bytes:
            raise DocumentTooLargeError(
                f"document has {size} bytes; limit is {max_bytes}"
            )
        data = candidate.read_bytes()
    if len(data) > max_bytes:
        raise DocumentTooLargeError(
            f"document has {len(data)} bytes; limit is {max_bytes}"
        )

    extension = _validate_extension(display_name)
    if extension in TEXT_EXTENSIONS:
        text = _decode_text(data)
        if extension == ".xml":
            text = _extract_xml_text(data)
        elif extension == ".json":
            # Pretty-print valid JSON to make retrieval snippets readable;
            # preserve malformed-but-readable input as plain text.
            try:
                value = json.loads(text)
                text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
            except (TypeError, ValueError):
                pass
    elif extension == ".docx":
        text = _extract_docx(data, max_bytes=max_bytes)
    elif extension == ".pdf":
        text = _extract_pdf(data)
    else:  # pragma: no cover - extension validation makes this unreachable
        raise UnsupportedDocumentError(f"unsupported document extension {extension}")
    return validate_document_limits(text, max_chars=max_chars)


def _validate_chunking(chunk_size: int, overlap: int) -> tuple[int, int]:
    chunk_size, overlap = int(chunk_size), int(overlap)
    if chunk_size < 1:
        raise KnowledgeError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise KnowledgeError("overlap must be >= 0 and smaller than chunk_size")
    return chunk_size, overlap


def chunk_text(text: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Split text into bounded overlapping chunks at readable boundaries."""

    value = normalize_document_text(text)
    chunk_size, overlap = _validate_chunking(chunk_size, overlap)
    if len(value) <= chunk_size:
        return [value]

    chunks: list[str] = []
    start = 0
    length = len(value)
    while start < length:
        end = min(length, start + chunk_size)
        if end < length:
            # Prefer a paragraph/newline/space boundary in the latter half of
            # the candidate window.  Chinese text often has no spaces, so a
            # hard character boundary remains the deterministic fallback.
            boundary_floor = start + max(1, chunk_size // 2)
            candidates = [value.rfind(marker, boundary_floor, end) for marker in ("\n\n", "\n", " ", "，", "。", "！", "？")]
            boundary = max(candidates)
            if boundary > start:
                # Keep the hard upper bound strict.  The omitted separator is
                # harmless because the next chunk starts with overlap and
                # ``strip`` removes boundary whitespace.
                end = boundary
        piece = value[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        next_start = max(start + 1, end - overlap)
        # Avoid repeating only whitespace forever after a boundary trim.
        while next_start < length and value[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks


_LATIN_WORD = re.compile(r"[a-z0-9]+(?:['_-][a-z0-9]+)*", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+"),
)


def redact_document_secrets(value: Any) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[已隐藏敏感信息]", text)
    return text


def _lexical_features(value: str) -> dict[str, float]:
    text = str(value or "").casefold()
    features: dict[str, float] = {}
    for match in _LATIN_WORD.finditer(text):
        features[match.group(0)] = features.get(match.group(0), 0.0) + 1.0
    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        # Include unigrams for short/single-character queries and bi/tri-grams
        # for useful Chinese phrase matching.
        for size in (1, 2, 3):
            if len(run) < size:
                continue
            weight = float(size)
            for index in range(len(run) - size + 1):
                gram = run[index:index + size]
                features[gram] = features.get(gram, 0.0) + weight
    return features


def lexical_score(query: str, text: str) -> float:
    """Return a deterministic [roughly 0..1] lexical relevance score."""

    normalized_query = " ".join(str(query or "").casefold().split())
    if not normalized_query:
        return 0.0
    query_features = _lexical_features(normalized_query)
    text_features = _lexical_features(text)
    if not query_features:
        return 0.0
    matched = sum(min(weight, text_features.get(feature, 0.0)) for feature, weight in query_features.items())
    total = sum(query_features.values())
    score = matched / total if total else 0.0
    haystack = " ".join(str(text or "").casefold().split())
    if normalized_query in haystack:
        score += 0.25
    return round(score, 6)


def _content_hash(title: str, text: str) -> str:
    canonical = "\n".join((title.casefold().strip(), normalize_document_text(text)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _doc_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in document.items() if key not in {"text", "chunks"}}
    result.setdefault("doc_id", result.get("id"))
    result.setdefault("id", result.get("doc_id"))
    return result


class InMemoryKnowledgeStore:
    """Reference Store implementation used by tests and local dry runs."""

    def __init__(self, *, max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS):
        self.max_chars = int(max_chars)
        self._groups: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def add_knowledge_document(
        self,
        group_id: str,
        title: str,
        text: str,
        source: str = "",
        created_by: str = "",
        message_id: str = "",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> dict[str, Any]:
        group_id = _as_nonempty(group_id, "group_id")
        title = _as_nonempty(title, "title")
        text = redact_document_secrets(validate_document_limits(text, max_chars=self.max_chars))
        chunk_size, overlap = _validate_chunking(chunk_size, overlap)
        digest = _content_hash(title, text)
        with self._lock:
            group = self._groups.setdefault(group_id, {})
            for document in group.values():
                if document["content_hash"] == digest:
                    result = _doc_summary(document)
                    result["deduplicated"] = True
                    return result
            doc_id = "doc-" + digest[:16]
            chunks = [
                {"chunk_id": f"{doc_id}-chunk-{index + 1}", "text": piece}
                for index, piece in enumerate(chunk_text(text, chunk_size=chunk_size, overlap=overlap))
            ]
            document = {
                "id": doc_id,
                "doc_id": doc_id,
                "group_id": group_id,
                "title": title,
                "source": str(source or ""),
                "created_by": str(created_by or ""),
                "message_id": str(message_id or ""),
                "created_at": time.time(),
                "content_hash": digest,
                "chunk_count": len(chunks),
                "chunks": chunks,
                "text": text,
                "chunk_size": chunk_size,
                "overlap": overlap,
            }
            group[doc_id] = document
            result = _doc_summary(document)
            result["deduplicated"] = False
            return result

    def list_knowledge_documents(self, group_id: str) -> list[dict[str, Any]]:
        group_id = str(group_id)
        with self._lock:
            group = self._groups.get(group_id, {})
            return [_doc_summary(document) for document in group.values()]

    def remove_knowledge_document(self, group_id: str, doc_id: str) -> bool:
        with self._lock:
            group = self._groups.get(str(group_id), {})
            return group.pop(str(doc_id), None) is not None

    def clear_knowledge(self, group_id: str) -> int:
        with self._lock:
            group = self._groups.pop(str(group_id), {})
            return len(group)

    def search_knowledge(self, group_id: str, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
        query = _as_nonempty(query, "query")
        limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        with self._lock:
            documents = list(self._groups.get(str(group_id), {}).values())
        results: list[dict[str, Any]] = []
        for document in documents:
            for chunk in document["chunks"]:
                score = lexical_score(query, chunk["text"])
                if score <= 0:
                    continue
                results.append(
                    {
                        "doc_id": document["doc_id"],
                        "title": document["title"],
                        "chunk_id": chunk["chunk_id"],
                        "score": score,
                        "text": chunk["text"],
                        "source": document["source"],
                    }
                )
        results.sort(key=lambda item: (-float(item["score"]), str(item["title"]).casefold(), str(item["doc_id"]), str(item["chunk_id"])))
        return results[:limit]


class KnowledgeBase:
    """Validated facade over a Store-compatible persistence object."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
        max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
        max_chars: int = DEFAULT_MAX_DOCUMENT_CHARS,
        cache_dir: str | os.PathLike[str] | None = None,
    ):
        self.store = store
        self.chunk_size, self.overlap = _validate_chunking(chunk_size, overlap)
        self.max_bytes = int(max_bytes)
        self.max_chars = int(max_chars)
        if self.max_bytes < 1 or self.max_chars < 1:
            raise KnowledgeError("document limits must be positive")
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir is not None else None

    def add_knowledge_document(
        self,
        group_id: str,
        title: str,
        text: str,
        source: str = "",
        created_by: str = "",
        message_id: str = "",
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> dict[str, Any]:
        group_id = _as_nonempty(group_id, "group_id")
        title = _as_nonempty(title, "title")
        text = redact_document_secrets(validate_document_limits(text, max_chars=self.max_chars))
        actual_chunk_size, actual_overlap = _validate_chunking(
            self.chunk_size if chunk_size is None else chunk_size,
            self.overlap if overlap is None else overlap,
        )
        return self.store.add_knowledge_document(
            group_id,
            title,
            text,
            str(source or ""),
            str(created_by or ""),
            str(message_id or ""),
            actual_chunk_size,
            actual_overlap,
        )

    add_document = add_knowledge_document

    def add_file(
        self,
        group_id: str,
        title: str,
        path: str | os.PathLike[str],
        *,
        source: str = "",
        created_by: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        if self.cache_dir is not None:
            safe_path = validate_cache_path(path, self.cache_dir)
        else:
            safe_path = Path(path)
            if safe_path.is_symlink() or not safe_path.is_file():
                raise UnsafePathError("document path must be a regular file")
        text = extract_document(
            safe_path,
            filename=safe_path.name,
            max_bytes=self.max_bytes,
            max_chars=self.max_chars,
        )
        return self.add_knowledge_document(
            group_id,
            title,
            text,
            source=source or safe_path.name,
            created_by=created_by,
            message_id=message_id,
        )

    def list_knowledge_documents(self, group_id: str) -> list[dict[str, Any]]:
        return self.store.list_knowledge_documents(str(group_id))

    list_documents = list_knowledge_documents

    def remove_knowledge_document(self, group_id: str, doc_id: str) -> bool:
        return bool(self.store.remove_knowledge_document(str(group_id), _as_nonempty(doc_id, "doc_id")))

    remove_document = remove_knowledge_document

    def clear_knowledge(self, group_id: str) -> int:
        return int(self.store.clear_knowledge(str(group_id)))

    def search_knowledge(self, group_id: str, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
        query = _as_nonempty(query, "query")
        return list(self.store.search_knowledge(str(group_id), query, max(1, min(int(limit), MAX_SEARCH_LIMIT))))

    search = search_knowledge


@dataclass(frozen=True)
class KnowledgeCommand:
    """Parsed ``/kb`` command.

    ``argument`` contains the query/document ID for single-argument actions;
    ``title`` and ``text`` are populated for ``add``.
    """

    action: str
    argument: str = ""
    title: str = ""
    text: str = ""

    @property
    def name(self) -> str:
        return self.action


_KB_PREFIX = re.compile(r"^[／/]\s*kb(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_MENTION = re.compile(r"(?:<@!?[^>]+>|^@\S+)\s*")


def _clean_kb_input(value: Any) -> str:
    text = str(value or "").strip().replace("／", "/", 1)
    while True:
        cleaned = _MENTION.sub("", text, count=1).strip()
        if cleaned == text:
            return text
        text = cleaned


def parse_kb_command(value: Any) -> KnowledgeCommand | None:
    """Parse ``/kb add|list|search|remove|clear|help`` without side effects."""

    match = _KB_PREFIX.fullmatch(_clean_kb_input(value))
    if not match:
        return None
    rest = str(match.group(1) or "").strip()
    if not rest or rest.lower() == "help":
        return KnowledgeCommand("help")
    action, separator, argument = rest.partition(" ")
    action = action.casefold()
    argument = argument.strip() if separator else ""
    if action == "add":
        title, pipe, text = argument.partition("|")
        if not pipe or not title.strip() or not text.strip():
            return None
        return KnowledgeCommand("add", title=title.strip(), text=text.strip())
    if action == "list" and not argument:
        return KnowledgeCommand("list")
    if action == "search" and argument:
        return KnowledgeCommand("search", argument=argument)
    if action == "remove" and argument and " " not in argument:
        return KnowledgeCommand("remove", argument=argument)
    if action == "clear" and argument.casefold() == "confirm":
        return KnowledgeCommand("clear")
    if action == "help" and not argument:
        return KnowledgeCommand("help")
    return None


parse_knowledge_command = parse_kb_command


def kb_help_text() -> str:
    return (
        "【群知识库】\n"
        "/kb add 标题 | 正文  添加群文档\n"
        "/kb list  查看本群文档\n"
        "/kb search 关键词  搜索本群知识\n"
        "/kb remove 文档ID  删除本群文档\n"
        "/kb clear confirm  确认清空本群知识\n"
        "/kb help  查看帮助"
    )


__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_DOCUMENT_CHARS",
    "DocumentTooLargeError",
    "InMemoryKnowledgeStore",
    "KnowledgeBase",
    "KnowledgeCommand",
    "KnowledgeError",
    "KnowledgeStore",
    "SUPPORTED_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "UnsafePathError",
    "UnsupportedDocumentError",
    "chunk_text",
    "extract_document",
    "kb_help_text",
    "lexical_score",
    "normalize_document_text",
    "parse_kb_command",
    "parse_knowledge_command",
    "redact_document_secrets",
    "validate_cache_path",
    "validate_document_limits",
]

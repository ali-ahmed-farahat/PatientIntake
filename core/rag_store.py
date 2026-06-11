import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from urllib.error import HTTPError, URLError

from core.agent_utils import load_secret as read_secret, request_json


# Purpose: configure local PDF sources, Chroma vector storage, and Gemini embeddings.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAG_SOURCE_DIR = os.environ.get("RAG_SOURCE_DIR", os.path.join(BASE_DIR, "data", "rag_files"))
RAG_CHROMA_DIR = os.environ.get("RAG_CHROMA_DIR", os.path.join(BASE_DIR, "chroma_db"))
RAG_COLLECTION_NAME = os.environ.get("RAG_COLLECTION_NAME", "clinical_guidelines")
GEMINI_EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
GEMINI_EMBEDDING_DIM = int(os.environ.get("GEMINI_EMBEDDING_DIM", "768"))
CHUNK_MAX_CHARS = int(os.environ.get("RAG_CHUNK_MAX_CHARS", "2400"))
CHUNK_OVERLAP_CHARS = int(os.environ.get("RAG_CHUNK_OVERLAP_CHARS", "300"))
EMBEDDING_BATCH_SIZE = int(os.environ.get("RAG_EMBEDDING_BATCH_SIZE", "16"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("RAG_REQUEST_TIMEOUT_SECONDS", "45"))


def utc_now():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def load_secret(name):
    """Read Gemini credentials from env vars or APIkey."""
    return read_secret(
        name,
        base_dir=BASE_DIR,
        aliases={"GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI", "GOOGLE"},
        bare_value=lambda line: name == "GEMINI_API_KEY" and line.startswith("AIza"),
    )


def _load_chromadb():
    """Import Chroma lazily so startup can explain missing dependencies clearly."""
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError(
            "chromadb is not installed. Add it to requirements and reinstall dependencies."
        ) from exc
    return chromadb


def model_resource_name():
    if GEMINI_EMBEDDING_MODEL.startswith("models/"):
        return GEMINI_EMBEDDING_MODEL
    return f"models/{GEMINI_EMBEDDING_MODEL}"


def normalize_text(text):
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_source_path(path):
    return os.path.relpath(path, BASE_DIR).replace(os.sep, "/")


def list_pdf_files(source_dir=RAG_SOURCE_DIR):
    if not os.path.isdir(source_dir):
        return []

    paths = []
    for root, _, filenames in os.walk(source_dir):
        for filename in filenames:
            if filename.lower().endswith(".pdf"):
                paths.append(os.path.join(root, filename))
    return sorted(paths, key=lambda item: item.lower())


def extract_pdf_pages(path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install pypdf first: pip install -r requirements.txt") from exc

    reader = PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass

    pages = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = normalize_text(page.extract_text() or "")
        except Exception:
            text = ""
        if text:
            pages.append({"page_number": index, "text": text})

    return pages, len(reader.pages)


def split_text_into_chunks(text, max_chars=CHUNK_MAX_CHARS, overlap_chars=CHUNK_OVERLAP_CHARS):
    words = text.split()
    chunks = []
    current = []
    current_len = 0

    for word in words:
        addition = len(word) + (1 if current else 0)
        if current and current_len + addition > max_chars:
            chunks.append(" ".join(current))
            overlap = []
            overlap_len = 0
            for previous in reversed(current):
                previous_addition = len(previous) + (1 if overlap else 0)
                if overlap and overlap_len + previous_addition > overlap_chars:
                    break
                overlap.insert(0, previous)
                overlap_len += previous_addition
            current = overlap
            current_len = overlap_len

        current.append(word)
        current_len += len(word) + (1 if current_len else 0)

    if current:
        chunks.append(" ".join(current))

    return [chunk for chunk in chunks if chunk.strip()]


def build_document_chunks(path):
    pages, page_count = extract_pdf_pages(path)
    chunks = []

    for page in pages:
        for chunk_text in split_text_into_chunks(page["text"]):
            chunks.append({
                "page_number": page["page_number"],
                "text": chunk_text,
            })

    return chunks, page_count


def extract_embedding_values(item):
    if isinstance(item.get("values"), list):
        return item["values"]
    embedding = item.get("embedding")
    if isinstance(embedding, dict) and isinstance(embedding.get("values"), list):
        return embedding["values"]
    raise RuntimeError("Gemini embedding response did not include embedding values.")


def gemini_embed_texts(texts, *, task_type, title=None, api_key=None, output_dim=None):
    if not texts:
        return []

    api_key = api_key or load_secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured in the environment or APIkey file.")

    model_name = model_resource_name()
    embedding_dim = int(output_dim or GEMINI_EMBEDDING_DIM)
    url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:batchEmbedContents"
    config = {
        "taskType": task_type,
        "autoTruncate": True,
        "outputDimensionality": embedding_dim,
    }
    if title and task_type == "RETRIEVAL_DOCUMENT":
        config["title"] = title

    body = {
        "requests": [
            {
                "model": model_name,
                "content": {"parts": [{"text": text}]},
                "embedContentConfig": config,
            }
            for text in texts
        ]
    }

    last_error = None
    for attempt in range(3):
        try:
            payload = request_json(
                url,
                method="POST",
                headers={"x-goog-api-key": api_key},
                body=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            embeddings = payload.get("embeddings") or []
            if len(embeddings) != len(texts):
                raise RuntimeError(
                    f"Gemini returned {len(embeddings)} embeddings for {len(texts)} texts."
                )
            return [extract_embedding_values(item) for item in embeddings]
        except HTTPError as exc:
            last_error = f"Gemini embedding HTTP {exc.code}"
            if exc.code not in {429, 500, 502, 503, 504}:
                try:
                    detail = exc.read().decode("utf-8")
                except Exception:
                    detail = ""
                raise RuntimeError(f"{last_error}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = str(exc)

        time.sleep(2 ** attempt)

    raise RuntimeError(f"Gemini embedding request failed after retries: {last_error}")


def get_chroma_client():
    chromadb = _load_chromadb()
    os.makedirs(RAG_CHROMA_DIR, exist_ok=True)
    return chromadb.PersistentClient(path=RAG_CHROMA_DIR)


def get_rag_collection():
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=RAG_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def batch_items(items, batch_size):
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


def collect_pdf_paths(source_dir, source_paths=None, limit=None):
    """Resolve all requested PDFs while preserving order and uniqueness."""
    pdf_paths = []
    seen_paths = set()

    def add_path(path):
        normalized = os.path.normcase(os.path.normpath(path))
        if normalized not in seen_paths:
            pdf_paths.append(path)
            seen_paths.add(normalized)

    if source_paths is not None:
        for source_path in source_paths:
            candidate_path = (
                os.path.abspath(source_path)
                if os.path.isabs(source_path)
                else os.path.normpath(os.path.join(source_dir, source_path))
            )
            if os.path.isdir(candidate_path):
                for path in list_pdf_files(candidate_path):
                    add_path(path)
            elif os.path.isfile(candidate_path):
                add_path(candidate_path)
            else:
                raise RuntimeError(f"Specified path does not exist: {source_path}")
    else:
        pdf_paths = list_pdf_files(source_dir)
    return pdf_paths[:int(limit)] if limit is not None else pdf_paths


def _collection_records(collection):
    try:
        return collection.get(include=["metadatas"])
    except TypeError:
        return collection.get()


def _group_index_records(records):
    grouped = defaultdict(int)
    metadatas = records.get("metadatas") or []
    for metadata in metadatas:
        if not metadata:
            continue
        key = (
            str(metadata.get("embedding_model") or ""),
            int(metadata.get("embedding_dim") or 0),
        )
        grouped[key] += 1
    return grouped


def _available_embedding_indexes(collection):
    records = _collection_records(collection)
    grouped = _group_index_records(records)
    return [
        {
            "embedding_model": embedding_model,
            "embedding_dim": embedding_dim,
            "chunks": count,
        }
        for (embedding_model, embedding_dim), count in sorted(grouped.items())
        if embedding_model and embedding_dim
    ]


def _resolve_search_index(collection):
    grouped = _group_index_records(_collection_records(collection))
    active_count = grouped.get((GEMINI_EMBEDDING_MODEL, GEMINI_EMBEDDING_DIM), 0)
    if active_count:
        return GEMINI_EMBEDDING_MODEL, GEMINI_EMBEDDING_DIM

    if len(grouped) == 1:
        (embedding_model, embedding_dim), _ = next(iter(grouped.items()))
        return embedding_model, embedding_dim

    if grouped:
        available = ", ".join(
            f"{embedding_model} / {embedding_dim} dims ({count} chunks)"
            for (embedding_model, embedding_dim), count in sorted(grouped.items())
            if embedding_model and embedding_dim
        )
        raise RuntimeError(
            f"No indexed chunks found for {GEMINI_EMBEDDING_MODEL} at {GEMINI_EMBEDDING_DIM} dimensions. "
            f"Available indexes: {available}. Re-index the source PDFs."
        )

    raise RuntimeError("No indexed RAG chunks found. Run python rag_store.py index first.")


def index_rag_files(*, source_dir=RAG_SOURCE_DIR, force=False, limit=None, source_paths=None):
    """Extract, embed, and store PDF chunks in Chroma."""
    pdf_paths = collect_pdf_paths(source_dir, source_paths, limit)
    if pdf_paths and not load_secret("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is not configured in the environment or APIkey file.")

    summary = {
        "source_dir": source_dir,
        "database": RAG_CHROMA_DIR,
        "storage_backend": "chromadb",
        "collection_name": RAG_COLLECTION_NAME,
        "embedding_model": GEMINI_EMBEDDING_MODEL,
        "embedding_dim": GEMINI_EMBEDDING_DIM,
        "files_found": len(pdf_paths),
        "files_indexed": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "chunks_indexed": 0,
        "documents": [],
    }

    collection = get_rag_collection()

    for path in pdf_paths:
        source_path = relative_source_path(path)
        filename = os.path.basename(path)
        file_hash = sha256_file(path)
        file_size = os.path.getsize(path)

        try:
            existing = collection.get(
                where={"source_path": source_path},
                include=["metadatas"],
            )
            existing_hashes = [
                str(metadata.get("sha256") or "")
                for metadata in (existing.get("metadatas") or [])
                if metadata
            ]
            if existing_hashes and file_hash in existing_hashes and not force:
                chunk_count = len(existing.get("ids") or [])
                summary["files_skipped"] += 1
                summary["documents"].append({
                    "file": source_path,
                    "status": "skipped",
                    "chunks": chunk_count,
                })
                continue

            collection.delete(where={"source_path": source_path})

            chunks, page_count = build_document_chunks(path)
            embedded_rows = []
            for batch in batch_items(chunks, EMBEDDING_BATCH_SIZE):
                texts = [chunk["text"] for chunk in batch]
                embedded_rows.extend(
                    gemini_embed_texts(texts, task_type="RETRIEVAL_DOCUMENT", title=filename)
                )

            if len(embedded_rows) != len(chunks):
                raise RuntimeError(
                    f"Gemini returned {len(embedded_rows)} embeddings for {len(chunks)} chunks."
                )

            now = utc_now()
            ids = []
            metadatas = []
            documents = []
            embeddings = []
            for index, (chunk, embedding) in enumerate(zip(chunks, embedded_rows), start=1):
                ids.append(f"{source_path}:{index}:{GEMINI_EMBEDDING_MODEL}:{GEMINI_EMBEDDING_DIM}")
                documents.append(chunk["text"])
                embeddings.append([float(value) for value in embedding])
                metadatas.append({
                    "source_path": source_path,
                    "filename": filename,
                    "sha256": file_hash,
                    "file_size": file_size,
                    "page_count": page_count,
                    "page_number": chunk["page_number"],
                    "chunk_index": index,
                    "embedding_model": GEMINI_EMBEDDING_MODEL,
                    "embedding_dim": GEMINI_EMBEDDING_DIM,
                    "indexed_at": now,
                })

            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )

            summary["files_indexed"] += 1
            summary["chunks_indexed"] += len(ids)
            summary["documents"].append({
                "file": source_path,
                "status": "indexed",
                "pages": page_count,
                "chunks": len(ids),
            })
        except Exception as exc:
            summary["files_failed"] += 1
            summary["documents"].append({
                "file": source_path,
                "status": "failed",
                "error": str(exc),
            })

    return summary


def rag_status():
    pdf_count = len(list_pdf_files())
    try:
        collection = get_rag_collection()
        records = _collection_records(collection)
        metadatas = records.get("metadatas") or []
        documents = len({metadata.get("source_path") for metadata in metadatas if metadata and metadata.get("source_path")})
        chunks = int(collection.count())
        latest = None
        if metadatas:
            indexed_times = [
                metadata.get("indexed_at")
                for metadata in metadatas
                if metadata and metadata.get("indexed_at")
            ]
            latest = max(indexed_times) if indexed_times else None
        indexed_embeddings = _available_embedding_indexes(collection)
        active_chunks = sum(
            1
            for metadata in metadatas
            if metadata
            and metadata.get("embedding_model") == GEMINI_EMBEDDING_MODEL
            and int(metadata.get("embedding_dim") or 0) == GEMINI_EMBEDDING_DIM
        )
        try:
            search_embedding_model, search_embedding_dim = _resolve_search_index(collection)
        except RuntimeError:
            search_embedding_model = GEMINI_EMBEDDING_MODEL
            search_embedding_dim = GEMINI_EMBEDDING_DIM
    except Exception:
        documents = 0
        chunks = 0
        latest = None
        indexed_embeddings = []
        active_chunks = 0
        search_embedding_model = GEMINI_EMBEDDING_MODEL
        search_embedding_dim = GEMINI_EMBEDDING_DIM

    return {
        "source_dir": RAG_SOURCE_DIR,
        "database": RAG_CHROMA_DIR,
        "storage_backend": "chromadb",
        "collection_name": RAG_COLLECTION_NAME,
        "pdf_files": pdf_count,
        "indexed_documents": documents,
        "indexed_chunks": chunks,
        "active_indexed_chunks": active_chunks,
        "indexed_embeddings": indexed_embeddings,
        "latest_indexed_at": latest,
        "embedding_model": GEMINI_EMBEDDING_MODEL,
        "embedding_dim": GEMINI_EMBEDDING_DIM,
        "search_embedding_model": search_embedding_model,
        "search_embedding_dim": search_embedding_dim,
        "gemini_configured": bool(load_secret("GEMINI_API_KEY")),
    }


def search_rag(query, *, top_k=6, min_score=None):
    query = normalize_text(query)
    if not query:
        raise RuntimeError("Search query is empty.")

    collection = get_rag_collection()
    embedding_model, embedding_dim = _resolve_search_index(collection)

    query_embedding = gemini_embed_texts(
        [query],
        task_type="RETRIEVAL_QUERY",
        output_dim=embedding_dim,
    )[0]

    query_results = collection.query(
        query_embeddings=[query_embedding],
        n_results=int(top_k),
        where={
            "$and": [
                {"embedding_model": embedding_model},
                {"embedding_dim": int(embedding_dim)},
            ]
        },
        include=["documents", "metadatas", "distances"],
    )

    documents = (query_results.get("documents") or [[]])[0]
    metadatas = (query_results.get("metadatas") or [[]])[0]
    distances = (query_results.get("distances") or [[]])[0]

    results = []
    for rank, (document, metadata, distance) in enumerate(zip(documents, metadatas, distances), start=1):
        score = 1.0 - float(distance or 0.0)
        if min_score is not None and score < min_score:
            continue
        filename = metadata.get("filename") or os.path.basename(str(metadata.get("source_path") or ""))
        page_number = int(metadata.get("page_number") or 0)
        chunk_index = int(metadata.get("chunk_index") or rank)
        source_path = str(metadata.get("source_path") or "")
        results.append({
            "rank": rank,
            "score": round(score, 4),
            "filename": filename,
            "source_path": source_path,
            "page": page_number,
            "chunk": chunk_index,
            "citation": f"{filename}, p. {page_number}",
            "passage": document,
            "snippet": document[:900].rstrip() + ("..." if len(document) > 900 else ""),
        })

    return {
        "query": query,
        "top_k": int(top_k),
        "results": results,
        "indexed_chunks_searched": len(documents),
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
    }


def build_clinical_context(query, *, top_k=6, max_passage_chars=1400):
    result = search_rag(query, top_k=top_k)
    lines = []
    for item in result["results"]:
        passage = item["passage"][:max_passage_chars].strip()
        if len(item["passage"]) > max_passage_chars:
            passage += "..."
        lines.append(
            f"[{item['rank']}] {item['citation']} | score {item['score']}\n{passage}"
        )
    return {
        "query": result["query"],
        "context": "\n\n".join(lines),
        "sources": [
            {
                "rank": item["rank"],
                "citation": item["citation"],
                "source_path": item["source_path"],
                "score": item["score"],
            }
            for item in result["results"]
        ],
    }


def print_json(data):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(data, ensure_ascii=True, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index and search local clinical RAG PDFs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Extract, chunk, embed, and store PDF content.")
    index_parser.add_argument("--force", action="store_true", help="Re-index unchanged PDFs.")
    index_parser.add_argument("--limit", type=int, help="Index only the first N PDFs.")
    index_parser.add_argument(
        "--file",
        action="append",
        help="Index one or more specific PDF files or directories relative to the RAG source directory.",
    )

    search_parser = subparsers.add_parser("search", help="Search the indexed books.")
    search_parser.add_argument("query", help="Clinical search query.")
    search_parser.add_argument("--top-k", type=int, default=6)

    subparsers.add_parser("status", help="Show local RAG index status.")

    args = parser.parse_args(argv)
    if args.command == "index":
        print_json(index_rag_files(force=args.force, limit=args.limit, source_paths=args.file))
    elif args.command == "search":
        print_json(search_rag(args.query, top_k=args.top_k))
    elif args.command == "status":
        print_json(rag_status())


if __name__ == "__main__":
    main(sys.argv[1:])

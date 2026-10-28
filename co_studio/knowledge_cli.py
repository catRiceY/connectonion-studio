"""Explicit knowledge setup/ingestion; safe defaults never call a hosted model."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from .knowledge import Document, KnowledgeError, LocalEmbedder, configured_store, fingerprint, tokenize


def write_report(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def bootstrap(directory: Path):
    """Create separate local knowledge credentials once, without printing them."""
    directory.mkdir(parents=True, exist_ok=True)
    env_path, config_path = directory / "knowledge.env", directory / "knowledge-db.json"
    if env_path.exists() or config_path.exists():
        raise KnowledgeError("Knowledge bootstrap files already exist; reuse them without overwriting")
    password = secrets.token_urlsafe(32)
    for path, text in (
        (env_path, f"POSTGRES_PASSWORD={password}\n"),
        (config_path, json.dumps({"database_url": f"postgresql://studio_knowledge:{password}@127.0.0.1:55433/studio_knowledge"})),
    ):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text + "\n")
    print(json.dumps({"env_file": str(env_path), "config_file": str(config_path)}))


def ingest_package(report: Path, limit: int | None = None):
    distribution = importlib.metadata.distribution("connectonion")
    root = Path(distribution.locate_file("connectonion/docs"))
    store = configured_store()
    manifest, seen = [], set()
    paths = sorted(root.rglob("*.md"))
    for index, path in enumerate(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            continue
        relative = path.relative_to(root).as_posix()
        text = path.read_text(errors="strict")
        digest = fingerprint(text.strip())
        reason = None
        if len(tokenize(text)) < 40:
            reason = "too little substantive text"
        elif digest in seen:
            reason = "duplicate source content"
        elif len(text.encode()) > 512000:
            reason = "source exceeds per-document limit"
        seen.add(digest)
        entry = {"source": f"studio://packages/connectonion/{distribution.version}/docs/{relative}",
                 "upstream": "https://github.com/openonion/connectonion", "package_version": distribution.version,
                 "license": "Apache-2.0", "original_sha256": digest, "relative_path": relative}
        if reason:
            manifest.append({**entry, "included": False, "reason": reason})
            continue
        title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
        try:
            result = store.import_document(Document(entry["source"], title[:240], text,
                                                   distribution.version, "Apache-2.0"))
            manifest.append({**entry, "included": not result.get("duplicate", False), **result})
        except KnowledgeError as exc:
            manifest.append({**entry, "included": False, "reason": str(exc)})
        if index % 25 == 0:
            print(f"Source scan {index + 1}/{len(paths)}", flush=True)
            write_report(report, {"complete": False, "sources": manifest})
        if limit is not None and sum(item.get("included", False) for item in manifest) >= limit:
            break
    value = {"complete": True, "dataset": "connectonion-packaged-docs-v1", "generated_at": datetime.now(timezone.utc).isoformat(),
             "source_type": "public package documentation", "package_version": distribution.version,
             "independent_documents": len({item["doc_id"] for item in manifest if item.get("included")}),
             "sources": manifest, "storage": store.status(),
             "note": "Public upstream package documents, not enterprise/private traffic; chunks are not counted as documents."}
    write_report(report, value)
    print(json.dumps({key: value[key] for key in ("complete", "independent_documents", "storage")}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="Explicitly download the public local ONNX embedding model")
    boot = sub.add_parser("bootstrap", help="Create separate local DB credentials, without starting a service")
    boot.add_argument("--directory", type=Path, default=Path(".co-studio"))
    sub.add_parser("initialize", help="Explicitly initialize the configured pgvector schema")
    ingest = sub.add_parser("ingest-package", help="Index licensed docs from the installed ConnectOnion package")
    ingest.add_argument("--report", type=Path, default=Path(".co-studio/reports/corpus-manifest.json"))
    ingest.add_argument("--limit", type=int)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", choices=["bm25", "vector", "hybrid"], default="hybrid")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            model = LocalEmbedder(download=True)
            print(json.dumps({"model": model.model_id, "dimensions": len(model.encode(["Agent diagnostics"])[0])}))
        elif args.command == "bootstrap":
            bootstrap(args.directory)
        elif args.command == "initialize":
            configured_store().initialize()
            print(json.dumps(configured_store().status()))
        elif args.command == "ingest-package":
            ingest_package(args.report, args.limit)
        else:
            print(json.dumps(configured_store().search(args.query, mode=args.mode), ensure_ascii=False, indent=2))
    except KnowledgeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

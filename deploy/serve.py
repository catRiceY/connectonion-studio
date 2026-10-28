"""Local container supervisor; no credential discovery or model calls."""
import os
import signal
import subprocess
import sys
import time
from urllib.parse import quote

from co_studio.history_archive import PostgresArchive
from co_studio.knowledge import KnowledgeStore


def main():
    password = os.environ.get("STUDIO_DATABASE_PASSWORD", "")
    if len(password) < 16:
        sys.exit("Set a random STUDIO_DATABASE_PASSWORD with at least 16 characters")
    dsn = f"postgresql://studio:{quote(password, safe='')}@db:5432/studio"
    os.environ["STUDIO_HISTORY_DATABASE_URL"] = dsn
    os.environ["STUDIO_KNOWLEDGE_DATABASE_URL"] = dsn
    # Only the dedicated Compose database. Existing desktop databases are not mounted.
    try:
        PostgresArchive(dsn).initialize()
        KnowledgeStore(dsn).initialize()
    except Exception:
        sys.exit("Container database initialization failed; credentials are not logged")
    children = []
    stopping = False

    def shutdown(*_):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "co_studio.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "9900", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--timeout-graceful-shutdown", "5"]))
        children.append(subprocess.Popen(["nginx", "-c", "/app/deploy/nginx.conf", "-g", "daemon off;"]))
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(.2)
    finally:
        shutdown()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return 0 if all(child.returncode in (0, -signal.SIGTERM) for child in children) else 1


if __name__ == "__main__":
    sys.exit(main())

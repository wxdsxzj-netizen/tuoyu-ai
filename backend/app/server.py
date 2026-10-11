"""Start all HTTP workers with one fresh multiprocess metrics directory."""
import os
from tempfile import TemporaryDirectory


def main():
    # Set before importing Uvicorn/application/Prometheus in any child process.
    # A fresh directory per master lifetime prevents counters from an old server
    # being reattached. Worker restarts retain the current master's counters.
    with TemporaryDirectory(prefix="tuoyu-http-metrics-") as directory:
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = directory
        import uvicorn
        workers = int(os.environ.get("TL_HTTP_WORKERS", "2"))
        if not 1 <= workers <= 64:
            raise ValueError("TL_HTTP_WORKERS must be between 1 and 64")
        uvicorn.run("app.main:app", host=os.environ.get("TL_HTTP_HOST", "0.0.0.0"),
                    port=int(os.environ.get("TL_HTTP_PORT", "8000")), workers=workers,
                    proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()

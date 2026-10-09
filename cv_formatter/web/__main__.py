"""Service web : python -m cv_formatter.web  (port : variable PORT, 8080 par défaut, comme sur Cloud Run)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "cv_formatter.web.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 — conteneur : écoute sur toutes les interfaces
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()

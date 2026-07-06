from fastapi import Header, HTTPException, status

from .config import settings


async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """Gate for every public route. Checked in-app, not by Cloud Run IAM --
    see the security section in the README for why."""
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")


async def require_internal_key(x_internal_key: str | None = Header(default=None, alias="X-Internal-Key")) -> None:
    """Gate for /internal/process/{job_id}. Only the Cloud Tasks queue knows
    this value -- it is set as a header on the enqueued task, never handed to
    any public client."""
    if x_internal_key != settings.internal_task_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

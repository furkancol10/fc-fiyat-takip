from __future__ import annotations

import dataclasses
import hmac
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.checker import run_check
from app.db import get_connection
from app.fetcher import fetch_html
from app.limits import CheckCooldown, check_cooldown
from app.reset import reset_demo
from app.security import demo_policy
from app.telegram import notifier_from_env

router = APIRouter(prefix="/jobs", tags=["jobs"])


def get_fetch(request: Request) -> Callable[..., str]:
    override = getattr(request.app.state, "fetch_override", None)
    return override if override is not None else fetch_html


def get_notifier(request: Request) -> Any:
    override = getattr(request.app.state, "notifier_override", None)
    return override if override is not None else notifier_from_env()


def get_policy(request: Request) -> Any:
    override = getattr(request.app.state, "policy_override", None)
    return override if override is not None else demo_policy(os.environ.get("MOCK_BASE_URL"))


def get_now(request: Request) -> datetime | None:
    return getattr(request.app.state, "now_override", None)


def get_check_cooldown(request: Request) -> CheckCooldown:
    override = getattr(request.app.state, "check_cooldown_override", None)
    return override if override is not None else check_cooldown


def verify_job_token(
    x_job_token: str | None = Header(default=None, alias="X-Job-Token"),
) -> None:
    expected = (os.environ.get("JOB_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="JOB_TOKEN yapılandırılmamış")
    if not x_job_token or not hmac.compare_digest(x_job_token, expected):
        raise HTTPException(status_code=401, detail="Geçersiz veya eksik görev anahtarı")


@router.post("/check")
def jobs_check(
    _: None = Depends(verify_job_token),
    fetch: Callable[..., str] = Depends(get_fetch),
    notifier: Any = Depends(get_notifier),
    policy: Any = Depends(get_policy),
    now: datetime | None = Depends(get_now),
    cooldown: CheckCooldown = Depends(get_check_cooldown),
) -> JSONResponse:
    allowed, retry_after = cooldown.try_acquire()
    if not allowed:
        return JSONResponse(
            content={"detail": "Kontrol turu soğuma süresinde"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    with get_connection() as conn:
        summary = run_check(
            conn,
            fetch=fetch,
            notifier=notifier,
            policy=policy,
            now=now,
        )

    return JSONResponse(content=dataclasses.asdict(summary), status_code=200)


@router.post("/reset-demo")
def jobs_reset_demo(
    _: None = Depends(verify_job_token),
) -> dict[str, str]:
    reset_demo()
    return {"status": "ok"}

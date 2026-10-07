from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

# -----------------------------------------------------------------------------
# Basic configuration
# -----------------------------------------------------------------------------
PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8080"))
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://127.0.0.1:8081").rstrip("/")
LOG_DIR = Path(os.getenv("PROXY_LOG_DIR", "./logs")).resolve()

# Full prompts may contain source code, secrets, paths, and private text.
# Keep this OFF by default. Enable only while debugging.
LOG_REQUEST_BODIES = os.getenv("LOG_REQUEST_BODIES", "0") not in {"0", "false", "False"}
MAX_LOG_BODY_BYTES = int(os.getenv("MAX_LOG_BODY_BYTES", str(1024 * 1024)))  # 1 MiB
LOG_RETENTION_DAYS = float(os.getenv("LOG_RETENTION_DAYS", "7"))
LOG_MAX_FILES = int(os.getenv("LOG_MAX_FILES", "100"))

CANCEL_ON_DISCONNECT = os.getenv("CANCEL_ON_DISCONNECT", "1") not in {"0", "false", "False"}
CANCEL_TIMEOUT = float(os.getenv("CANCEL_TIMEOUT", "5"))
CANCEL_MAX_ATTEMPTS = int(os.getenv("CANCEL_MAX_ATTEMPTS", "3"))
CANCEL_MAX_TOTAL_ATTEMPTS = int(os.getenv("CANCEL_MAX_TOTAL_ATTEMPTS", "6"))
CANCEL_RETRY_BASE = float(os.getenv("CANCEL_RETRY_BASE", "0.25"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = os.getenv("READ_TIMEOUT", "none").lower()
DISCONNECT_POLL = float(os.getenv("DISCONNECT_POLL", "0.20"))

# -----------------------------------------------------------------------------
# VS Code Agent Stop side-channel
# -----------------------------------------------------------------------------
VSCODE_ABORT_LOG_WATCH = os.getenv("VSCODE_ABORT_LOG_WATCH", "1") not in {"0", "false", "False"}
VSCODE_LOG_DISCOVERY_POLL = float(os.getenv("VSCODE_LOG_DISCOVERY_POLL", "5.0"))
VSCODE_LOG_TAIL_POLL = float(os.getenv("VSCODE_LOG_TAIL_POLL", "0.25"))
VSCODE_NEW_FILE_TAIL_BYTES = int(os.getenv("VSCODE_NEW_FILE_TAIL_BYTES", str(16 * 1024)))
VSCODE_LOG_RECENT_SECONDS = float(os.getenv("VSCODE_LOG_RECENT_SECONDS", "21600"))  # 6h
_default_vscode_logs = Path(os.getenv("APPDATA", "")) / "Code" / "logs"
VSCODE_LOG_ROOT = Path(os.getenv("VSCODE_LOG_ROOT", str(_default_vscode_logs))).resolve()

_patterns_env = os.getenv(
    "VSCODE_ABORT_PATTERNS",
    "Aborting session,Cancelling session,Canceling session,Session aborted,Abort session",
)
VSCODE_ABORT_PATTERNS = tuple(p.strip().lower() for p in _patterns_env.split(",") if p.strip())

# Admin endpoints are localhost-only AND token-protected.
# If no token is supplied, generate a random one for this process and print it once.
PROXY_ADMIN_TOKEN = os.getenv("PROXY_ADMIN_TOKEN") or secrets.token_urlsafe(24)

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("llama-proxy")
app = FastAPI(title="VS Code -> llama.cpp cancellation proxy")


@dataclass
class ActiveConversation:
    conv_id: str
    started_at: float = field(default_factory=time.time)
    path: str = "/v1/chat/completions"
    phase: str = "pending"  # pending -> streaming/non-stream -> done
    agent_task_id: str | None = None
    cancel_reason: str | None = None
    cancel_requested: bool = False
    cancel_requested_at: float | None = None
    cancel_requested_while_pending: bool = False
    cancel_in_flight: bool = False
    cancel_attempts: int = 0
    cancel_confirmed_at: float | None = None
    last_cancel_status: int | None = None
    last_cancel_error: str | None = None


@dataclass
class WatcherState:
    watched_file: str | None = None
    offset: int = 0
    last_discovery_at: float | None = None
    last_read_at: float | None = None
    last_detected_at: float | None = None
    last_detected_pattern: str | None = None
    last_detected_line: str | None = None


ACTIVE: dict[str, ActiveConversation] = {}
ACTIVE_LOCK = asyncio.Lock()
WATCHER = WatcherState()
_shutdown_event = asyncio.Event()
_proxy_started_at = time.time()


def _read_timeout_value() -> float | None:
    if READ_TIMEOUT in {"none", "null", "inf", "infinite", "0"}:
        return None
    return float(READ_TIMEOUT)


def _safe_headers(headers: Any) -> dict[str, str]:
    hidden = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-proxy-admin-token"}
    return {k: ("<redacted>" if k.lower() in hidden else v) for k, v in headers.items()}


def _request_summary(body: bytes) -> dict[str, Any]:
    summary: dict[str, Any] = {"body_bytes": len(body)}
    try:
        obj = json.loads(body.decode("utf-8")) if body else None
        if isinstance(obj, dict):
            summary["model"] = obj.get("model")
            messages = obj.get("messages")
            tools = obj.get("tools")
            if isinstance(messages, list):
                summary["message_count"] = len(messages)
                summary["message_roles"] = [m.get("role") for m in messages if isinstance(m, dict)]
            if isinstance(tools, list):
                summary["tool_count"] = len(tools)
            summary["stream"] = obj.get("stream")
    except Exception:
        summary["json_parse_error"] = True
    return summary


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _apply_reasoning_policy(body: bytes) -> tuple[bytes, dict[str, Any] | None]:
    """Add per-request reasoning controls without overriding explicit client choices.

    The token budget is a ceiling: the model may naturally finish thinking before
    reaching it. llama-server must be started with --reasoning-budget -1 for the
    request-level thinking_budget_tokens value to take effect.
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return body, None
    if not isinstance(obj, dict):
        return body, None

    nested_reasoning = obj.get("reasoning")
    has_explicit_effort = "reasoning_effort" in obj or (
        isinstance(nested_reasoning, dict) and "effort" in nested_reasoning
    )
    has_explicit_budget = "thinking_budget_tokens" in obj
    if has_explicit_effort or has_explicit_budget:
        return body, {
            "profile": "client-explicit",
            "effort": obj.get("reasoning_effort"),
            "budget": obj.get("thinking_budget_tokens"),
        }

    messages = obj.get("messages")
    combined = "\n".join(_message_text(m) for m in messages) if isinstance(messages, list) else ""
    normalized = combined.lower()

    if "local deep coding" in normalized or "perform deep analysis" in normalized:
        profile, effort, budget = "deep-coding", "xhigh", 4096
    elif "work directly on the user's requested coding task" in normalized:
        profile, effort, budget = "coding", "medium", 1536
    elif "answer the user's question directly and concisely" in normalized:
        profile, effort, budget = "quick-chat", "low", 512
    else:
        # Safe fallback for unknown agents. This is still only a ceiling; the
        # model remains free to finish its reasoning earlier.
        profile, effort, budget = "default", "medium", 1536

    obj["reasoning_effort"] = effort
    obj["thinking_budget_tokens"] = budget
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, {"profile": profile, "effort": effort, "budget": budget}


def _cleanup_request_logs() -> None:
    try:
        files = sorted((p for p in LOG_DIR.glob("*.json") if p.is_file()), key=lambda p: p.stat().st_mtime)
        cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
        for p in list(files):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    files.remove(p)
            except OSError:
                pass
        excess = max(0, len(files) - LOG_MAX_FILES)
        for p in files[:excess]:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception as e:
        log.debug("request-log cleanup failed: %r", e)


def _log_request(conv_id: str, request: Request, body: bytes) -> None:
    summary = _request_summary(body)
    log.info(
        "REQUEST conv_id=%s bytes=%s messages=%s tools=%s model=%s",
        conv_id,
        summary.get("body_bytes"),
        summary.get("message_count"),
        summary.get("tool_count"),
        summary.get("model"),
    )
    if not LOG_REQUEST_BODIES:
        return

    _cleanup_request_logs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = LOG_DIR / f"{stamp}-{conv_id}.json"
    record: dict[str, Any] = {
        "conversation_id": conv_id,
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
        "headers": _safe_headers(request.headers),
        **summary,
    }
    clipped = body[:MAX_LOG_BODY_BYTES]
    record["body_truncated"] = len(body) > len(clipped)
    try:
        record["body"] = json.loads(clipped.decode("utf-8")) if clipped else None
    except Exception:
        record["body_text"] = clipped.decode("utf-8", errors="replace")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _forward_headers(request: Request, conv_id: str | None = None) -> dict[str, str]:
    hop_by_hop = {
        "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
        "x-proxy-admin-token",
    }
    headers = {k: v for k, v in request.headers.items() if k.lower() not in hop_by_hop}
    if conv_id:
        headers["X-Conversation-Id"] = conv_id
    return headers


async def _mark_active(conv_id: str, path: str, agent_task_id: str | None = None) -> None:
    async with ACTIVE_LOCK:
        ACTIVE[conv_id] = ActiveConversation(
            conv_id=conv_id,
            path=path,
            phase="pending",
            agent_task_id=agent_task_id,
        )
        count = len(ACTIVE)
    log.info("ACTIVE ADD conv_id=%s path=%s agent_task_id=%s active=%d", conv_id, path, agent_task_id, count)


async def _set_phase(conv_id: str, phase: str) -> None:
    async with ACTIVE_LOCK:
        state = ACTIVE.get(conv_id)
        if state:
            state.phase = phase


async def _mark_inactive(conv_id: str) -> None:
    async with ACTIVE_LOCK:
        existed = ACTIVE.pop(conv_id, None)
        count = len(ACTIVE)
    if existed:
        log.info("ACTIVE REMOVE conv_id=%s active=%d", conv_id, count)


async def _newest_active_id() -> str | None:
    async with ACTIVE_LOCK:
        if not ACTIVE:
            return None
        return max(ACTIVE.values(), key=lambda s: s.started_at).conv_id


async def cancel_upstream(conv_id: str, reason: str = "unspecified", *, force: bool = False) -> int | None:
    """Explicitly cancel one llama.cpp stream, with bounded retries.

    Crucially, cancellation is only marked complete after a 2xx response. A
    network failure or non-2xx response leaves the conversation eligible for a
    later cancellation signal.
    """
    if not CANCEL_ON_DISCONNECT:
        return None

    async with ACTIVE_LOCK:
        state = ACTIVE.get(conv_id)
        if state:
            state.cancel_requested = True
            state.cancel_requested_at = state.cancel_requested_at or time.time()
            if state.phase == "pending":
                state.cancel_requested_while_pending = True
            if state.cancel_confirmed_at is not None and not force:
                log.info("CANCEL SKIP confirmed conv_id=%s new_reason=%s", conv_id, reason)
                return state.last_cancel_status
            if state.cancel_in_flight:
                log.info("CANCEL SKIP in-flight conv_id=%s new_reason=%s", conv_id, reason)
                return None
            if state.cancel_attempts >= CANCEL_MAX_TOTAL_ATTEMPTS:
                log.error("CANCEL GIVE-UP total-attempt-limit conv_id=%s attempts=%d", conv_id, state.cancel_attempts)
                return state.last_cancel_status
            state.cancel_in_flight = True
            state.cancel_reason = reason

    url = f"{UPSTREAM_BASE}/v1/stream"
    last_status: int | None = None
    last_error: str | None = None

    try:
        remaining_total = CANCEL_MAX_TOTAL_ATTEMPTS
        async with ACTIVE_LOCK:
            state = ACTIVE.get(conv_id)
            if state:
                remaining_total = max(0, CANCEL_MAX_TOTAL_ATTEMPTS - state.cancel_attempts)
        attempts_this_call = min(CANCEL_MAX_ATTEMPTS, remaining_total)

        for attempt in range(1, attempts_this_call + 1):
            async with ACTIVE_LOCK:
                state = ACTIVE.get(conv_id)
                if state:
                    state.cancel_attempts += 1
                    total_attempt = state.cancel_attempts
                else:
                    total_attempt = attempt
            try:
                async with httpx.AsyncClient(timeout=CANCEL_TIMEOUT) as client:
                    r = await client.delete(url, params={"conv_id": conv_id})
                last_status = r.status_code
                last_error = None
                log.warning(
                    "CANCEL conv_id=%s reason=%s attempt=%d/%d total=%d -> upstream HTTP %s",
                    conv_id, reason, attempt, attempts_this_call, total_attempt, r.status_code,
                )
                if 200 <= r.status_code < 300:
                    async with ACTIVE_LOCK:
                        state = ACTIVE.get(conv_id)
                        if state:
                            state.cancel_confirmed_at = time.time()
                            state.last_cancel_status = r.status_code
                            state.last_cancel_error = None
                    return r.status_code
            except Exception as e:
                last_error = repr(e)
                log.error(
                    "CANCEL FAILED conv_id=%s reason=%s attempt=%d/%d: %r",
                    conv_id, reason, attempt, attempts_this_call, e,
                )

            if attempt < attempts_this_call:
                await asyncio.sleep(CANCEL_RETRY_BASE * (2 ** (attempt - 1)))

        return last_status
    finally:
        async with ACTIVE_LOCK:
            state = ACTIVE.get(conv_id)
            if state:
                state.cancel_in_flight = False
                state.last_cancel_status = last_status
                state.last_cancel_error = last_error


async def _repeat_pending_cancel_after_upstream_ready(conv_id: str) -> None:
    """Replay an early cancellation after llama.cpp has accepted the request."""
    deadline = time.monotonic() + CANCEL_TIMEOUT + 1.0
    reason = "pending-cancel-replay"
    while True:
        async with ACTIVE_LOCK:
            state = ACTIVE.get(conv_id)
            if not state or not state.cancel_requested_while_pending:
                return
            in_flight = state.cancel_in_flight
            reason = f"{state.cancel_reason or 'early-cancel'}-after-upstream-ready"
        if not in_flight or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)

    # llama.cpp can return 204 for an unknown id, so replay once the request
    # is known to have reached the upstream server.
    log.warning("CANCEL REPLAY after upstream ready conv_id=%s", conv_id)
    await cancel_upstream(conv_id, reason, force=True)


async def cancel_upstream_detached(conv_id: str, reason: str) -> None:
    task = asyncio.create_task(cancel_upstream(conv_id, reason), name=f"cancel-{conv_id}")
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        def _done(t: asyncio.Task) -> None:
            try:
                t.result()
            except Exception as e:
                log.error("DETACHED CANCEL FAILED conv_id=%s: %r", conv_id, e)
        task.add_done_callback(_done)


async def cancel_newest_active(reason: str) -> dict[str, int | None]:
    conv_id = await _newest_active_id()
    if not conv_id:
        log.warning("CANCEL-NEWEST reason=%s but no active conversations", reason)
        return {}
    log.warning("CANCEL-NEWEST reason=%s conv_id=%s", reason, conv_id)
    return {conv_id: await cancel_upstream(conv_id, reason)}


async def cancel_all_active(reason: str) -> dict[str, int | None]:
    async with ACTIVE_LOCK:
        conv_ids = list(ACTIVE.keys())
    if not conv_ids:
        log.warning("CANCEL-ALL reason=%s but no active conversations", reason)
        return {}
    log.warning("CANCEL-ALL reason=%s count=%d ids=%s", reason, len(conv_ids), ",".join(conv_ids))
    results = await asyncio.gather(*(cancel_upstream(cid, reason) for cid in conv_ids))
    return dict(zip(conv_ids, results))


async def _watch_downstream_disconnect(request: Request, conv_id: str, stop_event: asyncio.Event) -> None:
    try:
        while not stop_event.is_set():
            if await request.is_disconnected():
                log.warning("DOWNSTREAM WATCHER detected disconnect conv_id=%s", conv_id)
                await cancel_upstream_detached(conv_id, "downstream-disconnect")
                stop_event.set()
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=DISCONNECT_POLL)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("DOWNSTREAM WATCHER ERROR conv_id=%s: %r", conv_id, e)


async def _stream_upstream(
    request: Request,
    upstream_response: httpx.Response,
    client: httpx.AsyncClient,
    conv_id: str,
) -> AsyncIterator[bytes]:
    stop_event = asyncio.Event()
    watcher = asyncio.create_task(
        _watch_downstream_disconnect(request, conv_id, stop_event),
        name=f"disconnect-watch-{conv_id}",
    )
    disconnected = False
    try:
        async for chunk in upstream_response.aiter_raw():
            if stop_event.is_set():
                disconnected = True
                break
            if chunk:
                yield chunk
    except asyncio.CancelledError:
        disconnected = True
        log.warning("STREAM CANCELLED BY ASGI conv_id=%s", conv_id)
        await cancel_upstream_detached(conv_id, "asgi-stream-cancelled")
        raise
    except (httpx.ReadError, httpx.RemoteProtocolError) as e:
        if await request.is_disconnected():
            disconnected = True
            log.warning("CLIENT DISCONNECTED DURING READ conv_id=%s: %r", conv_id, e)
            await cancel_upstream_detached(conv_id, "disconnect-during-read")
        else:
            log.error("UPSTREAM STREAM ERROR conv_id=%s: %r", conv_id, e)
            raise
    finally:
        stop_event.set()
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        await upstream_response.aclose()
        await client.aclose()
        await _mark_inactive(conv_id)
        log.info("STREAM %s conv_id=%s", "CLOSED after cancellation" if disconnected else "COMPLETE", conv_id)


def _is_log_candidate(p: Path) -> bool:
    name = p.name.lower()
    parent = str(p.parent).lower()
    if p.suffix.lower() not in {"", ".log", ".txt"}:
        return False
    return (
        "agenthost" in name
        or "agent" in name
        or "copilot" in name
        or "github.copilot" in parent
        or "copilot" in parent
    )


def _discover_latest_vscode_log() -> Path | None:
    if not VSCODE_LOG_ROOT.exists():
        return None
    cutoff = time.time() - VSCODE_LOG_RECENT_SECONDS
    best: tuple[float, Path] | None = None
    try:
        for p in VSCODE_LOG_ROOT.rglob("*"):
            if not p.is_file() or not _is_log_candidate(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff or st.st_size > 64 * 1024 * 1024:
                continue
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, p)
    except OSError:
        return None
    return best[1] if best else None


def _match_abort_pattern(line: str) -> str | None:
    low = line.lower()
    for pattern in VSCODE_ABORT_PATTERNS:
        if pattern in low:
            return pattern
    return None


async def _watch_vscode_abort_logs() -> None:
    """Watch one current VS Code agent log instead of rescanning the tree every 350 ms.

    Existing file at proxy startup is baselined at EOF. A file discovered later
    is treated as a new/rotated log and the last few KiB are replayed to avoid
    missing an abort written before discovery.
    """
    if not VSCODE_ABORT_LOG_WATCH:
        log.info("VS Code abort-log watcher disabled")
        return

    log.info("VS Code abort-log watcher enabled root=%s patterns=%s", VSCODE_LOG_ROOT, VSCODE_ABORT_PATTERNS)
    watched: Path | None = None
    offset = 0
    partial = ""
    next_discovery = 0.0
    initial_discovery = True

    while not _shutdown_event.is_set():
        now = time.time()
        if watched is None or now >= next_discovery:
            candidate = _discover_latest_vscode_log()
            WATCHER.last_discovery_at = now
            next_discovery = now + VSCODE_LOG_DISCOVERY_POLL

            if candidate != watched:
                watched = candidate
                partial = ""
                if watched is None:
                    WATCHER.watched_file = None
                    WATCHER.offset = 0
                else:
                    try:
                        size = watched.stat().st_size
                        # At process startup, baseline existing logs at EOF to avoid replaying old aborts.
                        # Later new/rotated logs get a small tail replay to close the creation race.
                        if initial_discovery:
                            offset = size
                        else:
                            offset = max(0, size - VSCODE_NEW_FILE_TAIL_BYTES)
                        WATCHER.watched_file = str(watched)
                        WATCHER.offset = offset
                        log.info("VS Code watcher now tailing %s from offset=%d", watched, offset)
                    except OSError:
                        watched = None
                        offset = 0
            initial_discovery = False

        if watched is not None:
            try:
                size = watched.stat().st_size
                if size < offset:  # truncation/rotation of same path
                    offset = max(0, size - VSCODE_NEW_FILE_TAIL_BYTES)
                    partial = ""
                if size > offset:
                    with watched.open("rb") as f:
                        f.seek(offset)
                        data = f.read()
                    offset += len(data)
                    WATCHER.offset = offset
                    WATCHER.last_read_at = time.time()
                    text = partial + data.decode("utf-8", errors="replace")
                    lines = text.splitlines(keepends=True)
                    if lines and not lines[-1].endswith(("\n", "\r")):
                        partial = lines.pop()
                    else:
                        partial = ""
                    for line in lines:
                        matched = _match_abort_pattern(line)
                        if matched:
                            WATCHER.last_detected_at = time.time()
                            WATCHER.last_detected_pattern = matched
                            WATCHER.last_detected_line = line.strip()[:500]
                            log.warning("VS CODE ABORT DETECTED pattern=%r file=%s line=%s", matched, watched, line.strip()[:500])
                            # No precise session->conversation mapping is available here.
                            # Safest fallback for multi-slot/multi-window is only the newest request.
                            await cancel_newest_active("vscode-agent-abort-log")
            except (OSError, PermissionError):
                watched = None
            except Exception as e:
                log.error("VS Code log watcher error for %s: %r", watched, e)

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=VSCODE_LOG_TAIL_POLL)
        except asyncio.TimeoutError:
            pass


def _require_admin(request: Request) -> None:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="admin endpoint is loopback-only")
    token = request.headers.get("X-Proxy-Admin-Token")
    if not token or not secrets.compare_digest(token, PROXY_ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid proxy admin token")


@app.on_event("startup")
async def _startup() -> None:
    _shutdown_event.clear()
    _cleanup_request_logs()
    app.state.vscode_log_task = asyncio.create_task(_watch_vscode_abort_logs(), name="vscode-abort-log-watch")


@app.on_event("shutdown")
async def _shutdown() -> None:
    _shutdown_event.set()
    task = getattr(app.state, "vscode_log_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.get("/health")
async def health() -> dict[str, Any]:
    async with ACTIVE_LOCK:
        active = [
            {
                "conversation_id": s.conv_id,
                "age_seconds": round(time.time() - s.started_at, 3),
                "path": s.path,
                "phase": s.phase,
                "agent_task_id": s.agent_task_id,
                "cancel_reason": s.cancel_reason,
                "cancel_requested": s.cancel_requested,
                "cancel_requested_at": s.cancel_requested_at,
                "cancel_requested_while_pending": s.cancel_requested_while_pending,
                "cancel_in_flight": s.cancel_in_flight,
                "cancel_attempts": s.cancel_attempts,
                "cancel_confirmed_at": s.cancel_confirmed_at,
                "last_cancel_status": s.last_cancel_status,
                "last_cancel_error": s.last_cancel_error,
            }
            for s in sorted(ACTIVE.values(), key=lambda x: x.started_at)
        ]
    return {
        "ok": True,
        "proxy": f"http://{PROXY_HOST}:{PROXY_PORT}",
        "upstream": UPSTREAM_BASE,
        "cancel_on_disconnect": CANCEL_ON_DISCONNECT,
        "disconnect_poll_seconds": DISCONNECT_POLL,
        "cancel_retry": {
            "per_signal": CANCEL_MAX_ATTEMPTS,
            "total_limit": CANCEL_MAX_TOTAL_ATTEMPTS,
            "base_delay_seconds": CANCEL_RETRY_BASE,
        },
        "vscode_abort_log_watch": VSCODE_ABORT_LOG_WATCH,
        "vscode_log_root": str(VSCODE_LOG_ROOT),
        "vscode_abort_patterns": VSCODE_ABORT_PATTERNS,
        "watcher": {
            "watched_file": WATCHER.watched_file,
            "offset": WATCHER.offset,
            "last_discovery_at": WATCHER.last_discovery_at,
            "last_read_at": WATCHER.last_read_at,
            "last_detected_at": WATCHER.last_detected_at,
            "last_detected_pattern": WATCHER.last_detected_pattern,
            "last_detected_line": WATCHER.last_detected_line,
        },
        "active": active,
        "request_body_logging": LOG_REQUEST_BODIES,
        "log_dir": str(LOG_DIR),
        "admin_auth_required": True,
    }


@app.get("/__proxy/active")
async def proxy_active(request: Request) -> dict[str, Any]:
    _require_admin(request)
    async with ACTIVE_LOCK:
        active = [
            {
                "conversation_id": s.conv_id,
                "age_seconds": round(time.time() - s.started_at, 3),
                "phase": s.phase,
                "agent_task_id": s.agent_task_id,
                "cancel_reason": s.cancel_reason,
                "cancel_requested": s.cancel_requested,
                "cancel_requested_at": s.cancel_requested_at,
                "cancel_requested_while_pending": s.cancel_requested_while_pending,
                "cancel_attempts": s.cancel_attempts,
                "cancel_confirmed_at": s.cancel_confirmed_at,
            }
            for s in sorted(ACTIVE.values(), key=lambda x: x.started_at)
        ]
    return {"count": len(active), "active": active}


@app.post("/__proxy/cancel-newest")
@app.delete("/__proxy/cancel-newest")
async def proxy_cancel_newest(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {"cancelled": await cancel_newest_active("proxy-admin-newest")}


@app.post("/__proxy/cancel-all")
@app.delete("/__proxy/cancel-all")
async def proxy_cancel_all(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {"cancelled": await cancel_all_active("proxy-admin-all")}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    body = await request.body()
    is_chat = request.method == "POST" and request.url.path == "/v1/chat/completions"
    conv_id = request.headers.get("X-Conversation-Id") or (str(uuid.uuid4()) if is_chat else None)

    if is_chat:
        body, reasoning_policy = _apply_reasoning_policy(body)
        if reasoning_policy:
            log.info(
                "REASONING profile=%s effort=%s budget=%s",
                reasoning_policy.get("profile"),
                reasoning_policy.get("effort"),
                reasoning_policy.get("budget"),
            )

    if conv_id:
        _log_request(conv_id, request, body)

    # Register BEFORE waiting for upstream response headers. This closes the blind
    # window where Stop could happen during long prompt processing / delayed headers.
    if is_chat and conv_id:
        await _mark_active(conv_id, request.url.path, request.headers.get("X-Agent-Task-Id"))

    upstream_url = f"{UPSTREAM_BASE}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    headers = _forward_headers(request, conv_id)
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=_read_timeout_value(), write=None, pool=None)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    try:
        upstream_req = client.build_request(request.method, upstream_url, headers=headers, content=body)
        upstream = await client.send(upstream_req, stream=True)
    except Exception as e:
        await client.aclose()
        if is_chat and conv_id:
            await _mark_inactive(conv_id)
        log.error("UPSTREAM CONNECT ERROR %s %s: %r", request.method, upstream_url, e)
        return JSONResponse(status_code=502, content={"error": f"proxy upstream error: {e}"})

    if is_chat and conv_id:
        await _repeat_pending_cancel_after_upstream_ready(conv_id)

    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in {"content-length", "transfer-encoding", "connection", "content-encoding"}
    }
    if conv_id:
        response_headers["X-Conversation-Id"] = conv_id

    content_type = upstream.headers.get("content-type", "")
    is_stream = "text/event-stream" in content_type.lower()

    if is_stream and conv_id:
        await _set_phase(conv_id, "streaming")
        log.info("STREAM START conv_id=%s %s -> %s", conv_id, request.url.path, upstream.status_code)
        return StreamingResponse(
            _stream_upstream(request, upstream, client, conv_id),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type="text/event-stream",
        )

    if is_chat and conv_id:
        await _set_phase(conv_id, "non-stream")

    try:
        data = await upstream.aread()
        status_code = upstream.status_code
    finally:
        await upstream.aclose()
        await client.aclose()
        if is_chat and conv_id:
            await _mark_inactive(conv_id)

    return Response(content=data, status_code=status_code, headers=response_headers, media_type=content_type or None)


if __name__ == "__main__":
    log.info("Proxy listening on http://%s:%d", PROXY_HOST, PROXY_PORT)
    log.info("Forwarding to %s", UPSTREAM_BASE)
    log.info("Request logs: %s (full body logging=%s)", LOG_DIR, LOG_REQUEST_BODIES)
    log.info("VS Code abort-log watcher: %s (%s)", VSCODE_ABORT_LOG_WATCH, VSCODE_LOG_ROOT)
    log.warning("Proxy admin token for this run: %s", PROXY_ADMIN_TOKEN)
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="info", access_log=True)

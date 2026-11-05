"""Prompt -> video with BytePlus Seedance through the Comfy partner-nodes API.

Run the web UI:   .venv/bin/python app.py            (http://127.0.0.1:8000)
Or one-off CLI:   .venv/bin/python app.py --prompt "A red fox in the snow"

The API key is read from COMFY_API_KEY by comfy_sdk; it is never logged here.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from comfy_sdk import AsyncComfy, BinaryResult, ComfyError
from pydantic import BaseModel, Field

MODEL = "byteplus/dreamina-seedance-2-5-260628"
OUTPUT_DIR = Path(__file__).parent / "outputs"
STATIC_DIR = Path(__file__).parent / "static"

DEFAULTS = {"duration": 5, "ratio": "16:9", "resolution": "720p"}
RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
RESOLUTIONS = ["480p", "720p", "1080p"]


@dataclass
class JobState:
    id: str
    prompt: str
    status: str = "SUBMITTING"
    queue_position: int | None = None
    request_id: str | None = None
    message: str = "Submitting request..."
    video_file: str | None = None
    error: str | None = None
    done: bool = False
    started: float = field(default_factory=time.time)


# ---------------------------------------------------------------- result → mp4

def _find_video_url(obj: Any, path: str = "") -> str | None:
    """Find the video URL in the provider's JSON result.

    Prefers a string under a key containing "video" (BytePlus uses
    content.video_url), then any http(s)/data URL that looks like a video.
    """
    candidates: list[tuple[int, str]] = []

    def walk(node: Any, key: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{key}.{k}" if key else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{key}[{i}]")
        elif isinstance(node, str) and re.match(r"^(https?://|data:video/)", node):
            lowered_key = key.lower()
            bare = node.split("?", 1)[0].lower()
            score = 0
            if "video" in lowered_key:
                score += 2
            if bare.endswith((".mp4", ".mov", ".webm")) or node.startswith("data:video/"):
                score += 1
            if "thumb" in lowered_key or "cover" in lowered_key or "last_frame" in lowered_key:
                score -= 3
            candidates.append((score, node))

    walk(obj, path)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _looks_like_mp4(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] == b"ftyp"


async def _download(url: str, dest: Path) -> None:
    """Download the signed URL exactly as given (no re-quoting, no trimming)."""
    if url.startswith("data:"):
        dest.write_bytes(base64.b64decode(url.split(",", 1)[1]))
        return
    tmp = dest.with_suffix(".part")
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=300)) as http:
        async with http.stream("GET", url) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:300].decode(errors="replace")
                raise RuntimeError(f"download failed: HTTP {resp.status_code}: {body}")
            with tmp.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    tmp.replace(dest)


async def save_result(result: Any, stem: str) -> Path:
    """Turn whatever the SDK returned into outputs/<stem>.mp4."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    dest = OUTPUT_DIR / f"{stem}.mp4"

    if isinstance(result, BinaryResult):
        (OUTPUT_DIR / f"{stem}.result.json").write_text(
            json.dumps({"binary": True, "content_type": result.content_type,
                        "bytes": len(result.content)}, indent=2)
        )
        dest.write_bytes(result.content)
    else:
        # Keep the raw provider payload next to the video for debugging.
        (OUTPUT_DIR / f"{stem}.result.json").write_text(json.dumps(result, indent=2, default=str))
        url = _find_video_url(result)
        if not url:
            raise RuntimeError(f"no video URL found in result: {json.dumps(result, default=str)[:500]}")
        await _download(url, dest)

    with dest.open("rb") as f:
        if not _looks_like_mp4(f.read(12)):
            bad = dest.with_suffix(".bin")
            dest.replace(bad)
            raise RuntimeError(f"downloaded file is not an MP4 (saved as {bad.name})")
    return dest


# ---------------------------------------------------------------- generation

async def generate(prompt: str, options: dict[str, Any], state: JobState) -> Path:
    arguments = {"content": [{"text": prompt, "type": "text"}], **options}
    async with AsyncComfy() as client:
        handle = await client.models.submit(MODEL, arguments)
        state.request_id = handle.request_id
        state.status = "SUBMITTED"
        state.message = "Submitted, waiting for queue..."
        async for update in handle.iter_events():
            state.status = update.status or state.status
            state.queue_position = update.queue_position
            pos = f" (queue position {update.queue_position})" if update.queue_position is not None else ""
            state.message = f"{state.status}{pos}"
        state.message = "Fetching result..."
        result = await handle.get()
    state.status = "DOWNLOADING"
    state.message = "Downloading video..."
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{state.id[:8]}"
    return await save_result(result, stem)


async def run_job(prompt: str, options: dict[str, Any], state: JobState) -> None:
    try:
        path = await generate(prompt, options, state)
        state.video_file = path.name
        state.status = "DONE"
        state.message = f"Saved {path.name} ({path.stat().st_size / 1e6:.1f} MB)"
    except ComfyError as exc:  # typed SDK/router errors
        state.status = "FAILED"
        state.error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        state.status = "FAILED"
        state.error = f"{type(exc).__name__}: {exc}"
    finally:
        state.done = True
        if state.error:
            state.message = state.error


# ---------------------------------------------------------------- web app

class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    duration: int = Field(DEFAULTS["duration"], ge=1, le=15)
    ratio: str = DEFAULTS["ratio"]
    resolution: str = DEFAULTS["resolution"]


def create_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="Prompt to Video")
    jobs: dict[str, JobState] = {}
    tasks: set[asyncio.Task] = set()
    OUTPUT_DIR.mkdir(exist_ok=True)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/generate")
    async def start(body: GenerateIn):
        if body.ratio not in RATIOS or body.resolution not in RESOLUTIONS:
            raise HTTPException(400, "unsupported ratio or resolution")
        state = JobState(id=uuid.uuid4().hex, prompt=body.prompt.strip())
        jobs[state.id] = state
        options = {"duration": body.duration, "ratio": body.ratio, "resolution": body.resolution}
        task = asyncio.create_task(run_job(state.prompt, options, state))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"job_id": state.id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        state = jobs.get(job_id)
        if not state:
            raise HTTPException(404, "unknown job")
        data = asdict(state)
        data["elapsed"] = round(time.time() - state.started)
        data["video_url"] = f"/videos/{state.video_file}" if state.video_file else None
        return data

    @app.get("/api/videos")
    def videos():
        files = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "url": f"/videos/{p.name}"} for p in files[:20]]

    app.mount("/videos", StaticFiles(directory=OUTPUT_DIR), name="videos")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt", help="generate once from the command line instead of starting the web UI")
    parser.add_argument("--duration", type=int, default=DEFAULTS["duration"])
    parser.add_argument("--ratio", default=DEFAULTS["ratio"], choices=RATIOS)
    parser.add_argument("--resolution", default=DEFAULTS["resolution"], choices=RESOLUTIONS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.prompt:
        state = JobState(id=uuid.uuid4().hex, prompt=args.prompt)
        options = {"duration": args.duration, "ratio": args.ratio, "resolution": args.resolution}

        async def cli() -> None:
            task = asyncio.create_task(run_job(args.prompt, options, state))
            last = None
            while not task.done():
                if state.message != last:
                    print(f"[{time.time() - state.started:5.0f}s] {state.message}", flush=True)
                    last = state.message
                await asyncio.sleep(0.5)
            await task

        asyncio.run(cli())
        if state.error:
            raise SystemExit(f"FAILED: {state.error}")
        print(f"Saved: {OUTPUT_DIR / state.video_file}")
        return

    import uvicorn

    print(f"Open http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

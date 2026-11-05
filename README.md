# Prompt to Video

Type a prompt and get an `.mp4` back from `byteplus/dreamina-seedance-2-5-260628`, run through the Comfy partner-nodes API with `comfy_sdk.AsyncComfy`.

The web page shows the queue status and position while the job runs. When it finishes, the page plays the video and gives you a download link. Videos are saved in `outputs/`.

## Setup (once)

You need Python 3.10+ and `comfy_sdk` installed for that Python. It is already installed for Homebrew's `python3` on this Mac.

```bash
cd ~/ComfyProjects/prompt-to-video
```

```bash
python3 -m venv --system-site-packages .venv
```

```bash
.venv/bin/pip install fastapi "uvicorn[standard]"
```

`--system-site-packages` lets the venv use the `comfy_sdk` that is already installed globally. If you don't have it globally, run `.venv/bin/pip install comfy-sdk` too.

## Run

Set your key in the shell. The app reads it only from `COMFY_API_KEY` and never prints it.

```bash
export COMFY_API_KEY=comfyui-...
```

**Web UI:** start the server, then open http://127.0.0.1:8000

```bash
.venv/bin/python app.py
```

**Command line (no browser):**

```bash
.venv/bin/python app.py --prompt "A red fox trotting through a snowy pine forest"
```

Options for both modes: `--duration 5`, `--ratio 16:9`, `--resolution 720p` (these are the defaults). The web UI also has `--host` and `--port`.

## What gets saved

For each run, `outputs/` gets:

- `YYYYMMDD-HHMMSS-<id>.mp4`: the video.
- `YYYYMMDD-HHMMSS-<id>.result.json`: the raw result from the API, kept so you can see its exact shape.

## How the result becomes an mp4

`handle.get()` returns one of two things:

- **A dict.** The app looks for the video URL in it, preferring a key with "video" in its name (BytePlus uses `content.video_url`). It downloads that URL **exactly as given**. The URL is a signed GCS link, so changing any character (decoding `%2B`, `%2F` or `%3D`, or adding a stray `'` on the end) breaks the signature. The link also expires, so it is downloaded right away.
- **A `BinaryResult`.** The app writes the bytes straight to disk.

Then the app checks the file is a real MP4. If it isn't, the file is kept as `.bin` and the job is reported as failed.

## Troubleshooting

- `MissingApiKey`: `COMFY_API_KEY` isn't set in the shell that started the app.
- `Unauthorized` / `InsufficientCredits`: there's a problem with the key or the account's credits.
- `download failed: HTTP 400/403`: the signed URL expired or was changed. The `request_id` is shown on the page. To fetch the result again, rehydrate the handle with `handle = await client.models.handle(MODEL, request_id)` and then `await handle.get()`.

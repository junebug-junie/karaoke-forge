from __future__ import annotations

import html
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    DB_PATH,
    JOBS_DIR,
    LIBRARY_DIR,
    PUBLIC_BASE_PATH,
    RENDERS_DIR,
    ROOT_DIR,
    SONGS_DIR,
    ensure_library_dirs,
)
from .review_proxy import router as review_router
from .runner import run_job
from .store import Job, create_job, get_job, init_db, list_jobs

app = FastAPI(title="Karaoke Forge", version="0.1.0")
app.include_router(review_router)


def public_url(path: str = "/") -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{PUBLIC_BASE_PATH}{path}" if PUBLIC_BASE_PATH else path


def esc(value: object) -> str:
    return html.escape(str(value))


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._- " else "-" for ch in value).strip()
    return cleaned or "untitled"


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def page(title: str, body: str) -> HTMLResponse:
    css_url = esc(public_url("/static/style.css"))
    home_url = esc(public_url("/"))
    review_url = esc(public_url("/review"))

    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{esc(title)} · Karaoke Forge</title>
  <link rel="stylesheet" href="{css_url}" />
</head>
<body>
  <main>
    <header>
      <a class="brand" href="{home_url}">Karaoke Forge</a>
      <nav class="tabs">
        <a href="{home_url}">Jobs</a>
        <a href="{review_url}">Review</a>
      </nav>
    </header>
    {body}
  </main>
</body>
</html>"""
    )


@app.on_event("startup")
def startup() -> None:
    ensure_library_dirs()
    init_db()

    static_dir = Path(__file__).resolve().parents[2] / "apps" / "web" / "static"
    if static_dir.exists() and "static" not in [route.path.strip("/") for route in app.routes]:
        app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    jobs = list_jobs(limit=50)
    latest = jobs[0] if jobs else None
    latest_block = latest_job_block(latest) if latest else "<p>No jobs yet. Upload one cursed indie banger.</p>"
    rows = "".join(job_row(job) for job in jobs) or "<p>No jobs yet.</p>"

    body = f"""
<section class="panel status-panel">
  <h1>Karaoke Forge</h1>
  <p class="muted">Private indie karaoke factory running from Atlas.</p>
  <div class="path-grid">
    <div><strong>Public base</strong><code>{esc(PUBLIC_BASE_PATH or '/')}</code></div>
    <div><strong>Repo root</strong><code>{esc(ROOT_DIR)}</code></div>
    <div><strong>Library</strong><code>{esc(LIBRARY_DIR)}</code></div>
    <div><strong>Songs</strong><code>{esc(SONGS_DIR)}</code></div>
    <div><strong>Jobs</strong><code>{esc(JOBS_DIR)}</code></div>
    <div><strong>Renders</strong><code>{esc(RENDERS_DIR)}</code></div>
    <div><strong>SQLite DB</strong><code>{esc(DB_PATH)}</code></div>
  </div>
</section>

<section class="panel latest-panel">
  <h2>Latest job</h2>
  {latest_block}
</section>

<section class="panel">
  <h2>Make a karaoke video</h2>
  <form method="post" action="{esc(public_url('/jobs'))}" enctype="multipart/form-data">
    <label>Artist <input name="artist" required placeholder="The Wrens" /></label>
    <label>Title <input name="title" required placeholder="Happy" /></label>
    <label>Audio file <input type="file" name="audio" accept="audio/*" required /></label>
    <label>Lyrics, optional but recommended
      <textarea name="lyrics" rows="10" placeholder="Paste known lyrics here for obscure songs"></textarea>
    </label>
    <button type="submit">Queue generation</button>
  </form>
</section>

<section class="panel">
  <h2>All jobs</h2>
  <div class="jobs">{rows}</div>
</section>
"""
    return page("Home", body)


def latest_job_block(job: Job) -> str:
    job_url = esc(public_url("/jobs/" + job.id))
    log_url = esc(public_url("/jobs/" + job.id + "/log"))
    review_url = esc(public_url("/review"))
    renders = job.metadata.get("render_outputs", []) if job.metadata else []
    render_count = len(renders)
    return f"""
<div class="latest-job-card">
  <h3><a href="{job_url}">{esc(job.artist)} — {esc(job.title)}</a></h3>
  <p><span class="status {esc(job.status)}">{esc(job.status)}</span> updated {esc(job.updated_at)}</p>
  <div class="button-row">
    <a class="button-link" href="{job_url}">Details</a>
    <a class="button-link" href="{log_url}">Live log</a>
    <a class="button-link" href="{review_url}">Review</a>
  </div>
  <dl>
    <dt>Job directory</dt><dd><code>{esc(job.job_dir)}</code></dd>
    <dt>Source audio</dt><dd><code>{esc(job.source_audio_path)}</code></dd>
    <dt>Output directory</dt><dd><code>{esc(job.output_dir)}</code></dd>
    <dt>Current run log</dt><dd><code>{esc(job.log_path)}</code></dd>
    <dt>Copied renders</dt><dd>{render_count}</dd>
  </dl>
</div>
"""


def job_row(job: Job) -> str:
    renders = job.metadata.get("render_outputs", []) if job.metadata else []
    render_links = "".join(
        f"<a href='{esc(public_url('/jobs/' + job.id + '/render/' + str(idx)))}'>render {idx + 1}</a> "
        for idx, _ in enumerate(renders)
    )

    error = f"<p class='error'>{esc(job.error)}</p>" if job.error else ""
    job_url = esc(public_url("/jobs/" + job.id))
    log_url = esc(public_url("/jobs/" + job.id + "/log"))

    return f"""
<article class="job">
  <div>
    <h3><a href="{job_url}">{esc(job.artist)} — {esc(job.title)}</a></h3>
    <p><span class="status {esc(job.status)}">{esc(job.status)}</span> created {esc(job.created_at)}</p>
    <p class="muted"><code>{esc(job.log_path)}</code></p>
    {error}
  </div>
  <div class="actions">
    <a href="{job_url}">details</a>
    <a href="{log_url}">live log</a>
    {render_links}
  </div>
</article>
"""


@app.post("/jobs")
def submit_job(
    background_tasks: BackgroundTasks,
    artist: str = Form(...),
    title: str = Form(...),
    lyrics: str = Form(""),
    audio: UploadFile = File(...),
) -> RedirectResponse:
    ensure_library_dirs()

    stem = safe_name(f"{artist} - {title}")
    run_id = new_run_id()
    job_dir = JOBS_DIR / stem
    run_dir = job_dir / "runs" / run_id
    song_dir = SONGS_DIR / stem
    output_dir = RENDERS_DIR / stem
    log_path = run_dir / "karaoke-gen.log"

    for path in (job_dir, run_dir, song_dir, output_dir):
        path.mkdir(parents=True, exist_ok=True)

    suffix = Path(audio.filename or "input.audio").suffix or ".audio"
    audio_path = song_dir / f"input{suffix}"
    with audio_path.open("wb") as target:
        shutil.copyfileobj(audio.file, target)

    lyrics_path: Path | None = None
    if lyrics.strip():
        lyrics_path = song_dir / "lyrics.txt"
        lyrics_path.write_text(lyrics.strip() + "\n", encoding="utf-8")

    job = create_job(
        artist=artist.strip(),
        title=title.strip(),
        source_audio_path=audio_path,
        lyrics_path=lyrics_path,
        job_dir=job_dir,
        output_dir=output_dir,
        log_path=log_path,
        metadata={"upload_filename": audio.filename, "run_id": run_id, "run_dir": str(run_dir)},
    )

    background_tasks.add_task(run_job, job.id)
    return RedirectResponse(public_url("/jobs/" + job.id), status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str) -> HTMLResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    renders = job.metadata.get("render_outputs", []) if job.metadata else []
    render_links = "".join(
        f"<li><a href='{esc(public_url('/jobs/' + job.id + '/render/' + str(idx)))}'>{esc(Path(path).name)}</a></li>"
        for idx, path in enumerate(renders)
    ) or "<li>No copied renders yet.</li>"

    raw_log_url = esc(public_url('/jobs/' + job.id + '/log/raw'))
    live_log_url = esc(public_url('/jobs/' + job.id + '/log/text'))
    body = f"""
<section class="panel">
  <h1>{esc(job.artist)} — {esc(job.title)}</h1>
  <p><span class="status {esc(job.status)}">{esc(job.status)}</span></p>
  <p>
    <a class="button-link" href="{esc(public_url('/review'))}">Open Review tab</a>
    <a class="button-link" href="{raw_log_url}">Raw run log</a>
  </p>

  <dl>
    <dt>Created</dt><dd>{esc(job.created_at)}</dd>
    <dt>Updated</dt><dd>{esc(job.updated_at)}</dd>
    <dt>Started</dt><dd>{esc(job.started_at)}</dd>
    <dt>Finished</dt><dd>{esc(job.finished_at)}</dd>
    <dt>Error</dt><dd>{esc(job.error)}</dd>
    <dt>Source audio</dt><dd><code>{esc(job.source_audio_path)}</code></dd>
    <dt>Lyrics file</dt><dd><code>{esc(job.lyrics_path)}</code></dd>
    <dt>Job directory</dt><dd><code>{esc(job.job_dir)}</code></dd>
    <dt>Run directory</dt><dd><code>{esc(job.metadata.get('run_dir') if job.metadata else '')}</code></dd>
    <dt>Output directory</dt><dd><code>{esc(job.output_dir)}</code></dd>
    <dt>Current run log</dt><dd><code>{esc(job.log_path)}</code></dd>
  </dl>

  <h2>Live run log</h2>
  <pre id="live-log" class="live-log">Loading log...</pre>
  <script>
    const logUrl = "{live_log_url}";
    async function refreshLog() {{
      try {{
        const response = await fetch(logUrl, {{cache: "no-store"}});
        const text = await response.text();
        const el = document.getElementById("live-log");
        const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 80;
        el.textContent = text || "No log yet.";
        if (nearBottom) el.scrollTop = el.scrollHeight;
      }} catch (err) {{
        document.getElementById("live-log").textContent = "Failed to load log: " + err;
      }}
    }}
    refreshLog();
    setInterval(refreshLog, 2000);
  </script>

  <h2>Renders</h2>
  <ul>{render_links}</ul>
</section>
"""
    return page("Job", body)


@app.get("/jobs/{job_id}/log", response_class=HTMLResponse)
def job_log(job_id: str) -> HTMLResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return RedirectResponse(public_url("/jobs/" + job.id), status_code=303)


@app.get("/jobs/{job_id}/log/text", response_class=PlainTextResponse)
def job_log_text(job_id: str) -> PlainTextResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    log_path = Path(job.log_path)
    if not log_path.exists():
        return PlainTextResponse("No log yet. Refresh after the worker starts.\n")

    return PlainTextResponse(log_path.read_text(encoding="utf-8", errors="replace"))


@app.get("/jobs/{job_id}/log/raw", response_class=PlainTextResponse)
def job_log_raw(job_id: str) -> PlainTextResponse:
    return job_log_text(job_id)


@app.get("/jobs/{job_id}/render/{idx}")
def job_render(job_id: str, idx: int) -> FileResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    renders = job.metadata.get("render_outputs", []) if job.metadata else []
    if idx < 0 or idx >= len(renders):
        raise HTTPException(status_code=404, detail="Render not found")

    path = Path(renders[idx])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Render file missing on disk")

    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "true"}

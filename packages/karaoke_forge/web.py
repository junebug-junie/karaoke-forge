from __future__ import annotations

import html
import shutil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import JOBS_DIR, RENDERS_DIR, SONGS_DIR, ensure_library_dirs
from .runner import run_job
from .store import Job, create_job, get_job, init_db, list_jobs

app = FastAPI(title="Karaoke Forge", version="0.1.0")


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._- " else "-" for ch in value).strip()
    return cleaned or "untitled"


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>{html.escape(title)} · Karaoke Forge</title>
          <link rel="stylesheet" href="/static/style.css" />
        </head>
        <body>
          <main>
            <header>
              <a class="brand" href="/">Karaoke Forge</a>
              <span class="tagline">private indie karaoke factory</span>
            </header>
            {body}
          </main>
        </body>
        </html>
        """
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
    rows = "".join(_job_row(job) for job in jobs) or "<p>No jobs yet. Upload one cursed indie banger.</p>"
    body = f"""
    <section class="panel">
      <h1>Make a karaoke video</h1>
      <form method="post" action="/jobs" enctype="multipart/form-data">
        <label>Artist <input name="artist" required placeholder="The Wrens" /></label>
        <label>Title <input name="title" required placeholder="Happy" /></label>
        <label>Audio file <input type="file" name="audio" accept="audio/*" required /></label>
        <label>Lyrics, optional but recommended <textarea name="lyrics" rows="10" placeholder="Paste known lyrics here for obscure songs"></textarea></label>
        <button type="submit">Queue generation</button>
      </form>
    </section>
    <section class="panel">
      <h2>Jobs</h2>
      <div class="jobs">{rows}</div>
    </section>
    """
    return _page("Home", body)


def _job_row(job: Job) -> str:
    status_class = html.escape(job.status)
    error = f"<p class='error'>{html.escape(job.error)}</p>" if job.error else ""
    renders = job.metadata.get("render_outputs", []) if job.metadata else []
    render_links = "".join(
        f"<a href='/jobs/{html.escape(job.id)}/render/{idx}'>render {idx + 1}</a> "
        for idx, _ in enumerate(renders)
    )
    return f"""
    <article class="job">
      <div>
        <h3><a href="/jobs/{html.escape(job.id)}">{html.escape(job.artist)} — {html.escape(job.title)}</a></h3>
        <p><span class="status {status_class}">{html.escape(job.status)}</span> created {html.escape(job.created_at)}</p>
        {error}
      </div>
      <div class="actions">
        <a href="/jobs/{html.escape(job.id)}/log">log</a>
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
    artist_clean = _safe_name(artist)
    title_clean = _safe_name(title)
    stem = _safe_name(f"{artist_clean} - {title_clean}")
    job_dir = JOBS_DIR / stem
    suffix = Path(audio.filename or "input.audio").suffix or ".audio"
    song_dir = SONGS_DIR / stem
    output_dir = RENDERS_DIR / stem
    log_path = job_dir / "karaoke-gen.log"

    for path in (job_dir, song_dir, output_dir):
        path.mkdir(parents=True, exist_ok=True)

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
        metadata={"upload_filename": audio.filename},
    )
    background_tasks.add_task(run_job, job.id)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: str) -> HTMLResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    renders = job.metadata.get("render_outputs", []) if job.metadata else []
    render_links = "".join(
        f"<li><a href='/jobs/{html.escape(job.id)}/render/{idx}'>{html.escape(Path(path).name)}</a></li>"
        for idx, path in enumerate(renders)
    ) or "<li>No copied renders yet.</li>"
    body = f"""
    <section class="panel">
      <h1>{html.escape(job.artist)} — {html.escape(job.title)}</h1>
      <p><span class="status {html.escape(job.status)}">{html.escape(job.status)}</span></p>
      <dl>
        <dt>Created</dt><dd>{html.escape(job.created_at)}</dd>
        <dt>Updated</dt><dd>{html.escape(job.updated_at)}</dd>
        <dt>Started</dt><dd>{html.escape(str(job.started_at))}</dd>
        <dt>Finished</dt><dd>{html.escape(str(job.finished_at))}</dd>
        <dt>Error</dt><dd>{html.escape(str(job.error))}</dd>
      </dl>
      <p><a href="/jobs/{html.escape(job.id)}/log">View log</a></p>
      <h2>Renders</h2>
      <ul>{render_links}</ul>
    </section>
    """
    return _page("Job", body)


@app.get("/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: str) -> PlainTextResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    log_path = Path(job.log_path)
    if not log_path.exists():
        return PlainTextResponse("No log yet. Refresh after the worker starts.\n")
    return PlainTextResponse(log_path.read_text(encoding="utf-8", errors="replace"))


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

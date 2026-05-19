from __future__ import annotations

import html
import shutil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import JOBS_DIR, PUBLIC_BASE_PATH, RENDERS_DIR, SONGS_DIR, ensure_library_dirs
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
    rows = "".join(job_row(job) for job in jobs) or "<p>No jobs yet. Upload one cursed indie banger.</p>"

    body = f"""
<section class="panel">
  <h1>Make a karaoke video</h1>
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
  <h2>Jobs</h2>
  <div class="jobs">{rows}</div>
</section>
"""
    return page("Home", body)


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
    {error}
  </div>
  <div class="actions">
    <a href="{log_url}">log</a>
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
    job_dir = JOBS_DIR / stem
    song_dir = SONGS_DIR / stem
    output_dir = RENDERS_DIR / stem
    log_path = job_dir / "karaoke-gen.log"

    for path in (job_dir, song_dir, output_dir):
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
        metadata={"upload_filename": audio.filename},
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

    body = f"""
<section class="panel">
  <h1>{esc(job.artist)} — {esc(job.title)}</h1>
  <p><span class="status {esc(job.status)}">{esc(job.status)}</span></p>

  <dl>
    <dt>Created</dt><dd>{esc(job.created_at)}</dd>
    <dt>Updated</dt><dd>{esc(job.updated_at)}</dd>
    <dt>Started</dt><dd>{esc(job.started_at)}</dd>
    <dt>Finished</dt><dd>{esc(job.finished_at)}</dd>
    <dt>Error</dt><dd>{esc(job.error)}</dd>
  </dl>

  <p><a href="{esc(public_url('/jobs/' + job.id + '/log'))}">View log</a></p>

  <h2>Renders</h2>
  <ul>{render_links}</ul>
</section>
"""
    return page("Job", body)


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

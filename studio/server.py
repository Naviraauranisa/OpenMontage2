"""Storyboard Studio server — FastAPI.

GET  /                     UI (satu halaman)
GET  /api/health           status Claude CLI / Piper / Remotion
POST /api/generate         {task, profile, idea, script, settings, part?} → Claude Code headless
POST /api/render           storyboard → Piper TTS + Remotion Explainer (background job)
GET  /api/render/{id}      status job
GET  /api/render/{id}/video  hasil MP4
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from studio import prompts

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent / "ui"
PROJECTS_DIR = REPO_ROOT / "projects"
COMPOSER_DIR = REPO_ROOT / "remotion-composer"
PIPER_MODEL = Path(os.environ.get("STUDIO_PIPER_MODEL", REPO_ROOT / "id_ID-news_tts-medium.onnx"))
CLAUDE_MODEL = os.environ.get("STUDIO_CLAUDE_MODEL", "sonnet")
CLAUDE_TIMEOUT = int(os.environ.get("STUDIO_CLAUDE_TIMEOUT", "240"))

SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}

app = FastAPI(title="Storyboard Studio")


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def _version_key(path: str) -> tuple:
    m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", path)
    return tuple(int(x) for x in m.groups()) if m else (0,)


def find_claude() -> str | None:
    env = os.environ.get("STUDIO_CLAUDE_BIN")
    if env and Path(env).is_file():
        return env
    found = shutil.which("claude")
    if found:
        return found
    # Claude Code ekstensi VS Code membawa binary sendiri.
    home = Path.home()
    pattern = str(home / ".vscode" / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary" / "claude*")
    hits = [p for p in glob.glob(pattern) if Path(p).is_file()]
    return max(hits, key=_version_key) if hits else None


def find_piper() -> str | None:
    venv = REPO_ROOT / ".venv" / "Scripts" / "piper.exe"
    return str(venv) if venv.is_file() else shutil.which("piper")


def tools_status() -> dict[str, Any]:
    return {
        "claude": find_claude(),
        "piper": find_piper(),
        "piper_model": str(PIPER_MODEL) if PIPER_MODEL.is_file() else None,
        "npx": shutil.which("npx"),
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "composer": (COMPOSER_DIR / "node_modules").is_dir(),
    }


# ---------------------------------------------------------------------------
# Claude Code headless
# ---------------------------------------------------------------------------

class AIError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


def _extract_json(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise AIError("invalid_json")


def ask_claude(user_prompt: str, want_json: bool) -> Any:
    claude = find_claude()
    if not claude:
        raise AIError("not_configured")
    cmd = [
        claude, "-p",
        "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--system-prompt", prompts.SYSTEM,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
    ]
    # cwd kosong supaya CLAUDE.md / setting repo tidak ikut termuat.
    with tempfile.TemporaryDirectory(prefix="studio-claude-") as cwd:
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=CLAUDE_TIMEOUT, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            raise AIError("timeout")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise AIError("upstream_error", msg[-1] if msg else "Claude CLI gagal.")
    result = str(envelope.get("result") or "")
    if envelope.get("is_error") or proc.returncode != 0:
        raise AIError("upstream_error", result or "Claude CLI mengembalikan error.")
    if not result.strip():
        raise AIError("empty_completion")
    return _extract_json(result) if want_json else {"text": result.strip().strip('"')}


@app.post("/api/generate")
def generate(body: dict) -> JSONResponse:
    task = body.get("task")
    try:
        user_prompt, want_json = prompts.build(task, body)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"error": "validation", "message": str(exc)}, status_code=422)
    try:
        result = ask_claude(user_prompt, want_json)
    except AIError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=502)
    return JSONResponse({"result": result})


# ---------------------------------------------------------------------------
# Render: Piper TTS → narasi → props → Remotion Explainer
# ---------------------------------------------------------------------------

JOBS: dict[str, dict[str, Any]] = {}
RENDER_LOCK = threading.Lock()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "video")[:40]


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def _hex(color: str, fallback: str) -> str:
    return color if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) else fallback


def _legible(accent: str, dark_bg: bool) -> str:
    """Geser warna aksen ke putih/hitam sampai cukup kontras dengan latar."""
    r, g, b = (int(accent[i : i + 2], 16) for i in (1, 3, 5))
    for _ in range(10):
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        if (dark_bg and lum >= 0.55) or (not dark_bg and lum <= 0.45):
            break
        target = 255 if dark_bg else 0
        r, g, b = (round(c + (target - c) * 0.2) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def _theme(mode: str, accent: str) -> dict[str, Any]:
    accent = _legible(accent, dark_bg=mode != "terang")
    if mode == "terang":
        base = {"backgroundColor": "#F8FAFC", "surfaceColor": "#FFFFFF", "textColor": "#0F172A",
                "mutedTextColor": "#475569", "captionBackgroundColor": "rgba(255,255,255,0.85)"}
    else:
        base = {"backgroundColor": "#0B1120", "surfaceColor": "#111827", "textColor": "#FFFFFF",
                "mutedTextColor": "#E2E8F0", "captionBackgroundColor": "rgba(10,10,26,0.8)"}
    return {
        **base,
        "primaryColor": accent,
        "accentColor": accent,
        "captionHighlightColor": accent,
        "chartColors": [accent, base["mutedTextColor"], accent, base["mutedTextColor"], accent],
        "headingFont": "Inter",
        "bodyFont": "Inter",
    }


def _set(job: dict, **kw: Any) -> None:
    job.update(kw)
    job["updated"] = time.time()


def _run_render(job: dict, req: dict) -> None:
    try:
        with RENDER_LOCK:
            _render(job, req)
    except Exception as exc:  # noqa: BLE001 — semua error dilaporkan ke UI
        _set(job, status="error", error=str(exc)[-600:])


def _render(job: dict, req: dict) -> None:
    scenes = req["scenes"]
    ratio = req.get("ratio") if req.get("ratio") in SIZES else "9:16"
    width, height = SIZES[ratio]
    speed = float(req.get("speed") or 1.0)
    length_scale = round(1.0 / max(0.7, min(1.4, speed)), 3)

    project_dir = Path(job["dir"])
    public = project_dir / "public"
    public.mkdir(parents=True, exist_ok=True)
    (project_dir / "artifacts").mkdir(exist_ok=True)
    (project_dir / "renders").mkdir(exist_ok=True)
    (project_dir / "artifacts" / "storyboard.json").write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1) Narasi per scene
    piper = find_piper()
    timeline: list[dict[str, Any]] = []
    for idx, sc in enumerate(scenes, 1):
        _set(job, status="running", stage=f"Membuat suara scene {idx}/{len(scenes)}…", progress=round(0.25 * (idx - 1) / len(scenes), 3))
        vo = str(sc.get("voiceover") or "").strip()
        planned = max(2.0, float(sc.get("end", 0)) - float(sc.get("start", 0)))
        wav = public / f"scene-{idx:02d}.wav"
        voice_dur = 0.0
        if vo:
            proc = subprocess.run(
                [piper, "--model", str(PIPER_MODEL), "--length-scale", str(length_scale),
                 "--sentence-silence", "0.25", "--output_file", str(wav)],
                input=vo, capture_output=True, text=True, encoding="utf-8", timeout=300,
            )
            if proc.returncode != 0 or not wav.is_file():
                raise RuntimeError(f"Piper gagal di scene {idx}: {proc.stderr[-300:]}")
            voice_dur = _probe_duration(wav)
        dur = round(max(voice_dur + 0.45, 2.0) if vo else planned, 3)
        timeline.append({"idx": idx, "vo": vo, "wav": wav if vo else None, "voice": voice_dur, "dur": dur, "scene": sc})

    # 2) Gabung narasi jadi satu track, tiap scene dipad sesuai durasinya
    _set(job, stage="Menggabungkan narasi…", progress=0.27)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    filters, labels = [], []
    for n, t in enumerate(timeline):
        if t["wav"]:
            cmd += ["-i", str(t["wav"])]
        else:
            cmd += ["-f", "lavfi", "-t", str(t["dur"]), "-i", "anullsrc=r=22050:cl=mono"]
        filters.append(f"[{n}:a]aformat=sample_rates=22050:channel_layouts=mono,apad=whole_dur={t['dur']}[a{n}]")
        labels.append(f"[a{n}]")
    filters.append(f"{''.join(labels)}concat=n={len(timeline)}:v=0:a=1[out]")
    narration = public / "narration.wav"
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", str(narration)]
    subprocess.run(cmd, check=True, capture_output=True)

    # 3) Props Remotion
    accent = _hex(req.get("accent"), "#34D399")
    theme = _theme(req.get("theme", "gelap"), accent)
    cuts, captions, cursor = [], [], 0.0
    for t in timeline:
        sc = t["scene"]
        text = str(sc.get("on_screen_text") or "").strip() or " ".join(t["vo"].split()[:6]) or "…"
        cuts.append({
            "id": f"scene-{t['idx']:02d}", "source": "", "type": "hero_title", "text": text,
            "in_seconds": round(cursor, 3), "out_seconds": round(cursor + t["dur"], 3),
            "transition_in": "fade", "transition_out": "fade", "transition_duration": 0.3,
        })
        if req.get("captions", True) and t["vo"]:
            words = t["vo"].split()
            # Piper tidak memberi timestamp kata: bagi rata sepanjang durasi suara.
            step = t["voice"] / len(words)
            for w_i, w in enumerate(words):
                captions.append({
                    "word": w,
                    "startMs": int((cursor + w_i * step) * 1000),
                    "endMs": int((cursor + (w_i + 1) * step) * 1000),
                    "pageBreakAfter": w_i == len(words) - 1 or w.endswith((".", "!", "?")),
                })
        cursor += t["dur"]
    props = {
        "themeConfig": theme,
        "cuts": cuts,
        "overlays": [],
        "captions": captions,
        "audio": {"narration": {"src": "narration.wav", "volume": 1}},
    }
    props_path = project_dir / "artifacts" / "props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) Render Remotion
    out = project_dir / "renders" / f"{_slug(req.get('title') or 'video')}.mp4"
    _set(job, stage="Merender video…", progress=0.3)
    npx = shutil.which("npx") or "npx"
    proc = subprocess.Popen(
        [npx, "remotion", "render", "src/index.tsx", "Explainer", str(out),
         f"--props={props_path}", f"--public-dir={public}", f"--width={width}", f"--height={height}"],
        cwd=COMPOSER_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    tail: list[str] = []
    buf = ""
    assert proc.stdout
    while True:
        ch = proc.stdout.read(256)
        if not ch:
            break
        buf += ch
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for line in parts:
            if not line.strip():
                continue
            tail = (tail + [line])[-20:]
            m = re.search(r"Rendered\s+(\d+)/(\d+)|(\d+)/(\d+)", line)
            if m:
                a, b = [int(x) for x in m.groups() if x is not None][:2]
                if b:
                    _set(job, progress=round(0.3 + 0.68 * min(1, a / b), 3), stage=f"Merender video… {a}/{b} frame")
    if proc.wait() != 0 or not out.is_file():
        raise RuntimeError("Remotion gagal:\n" + "\n".join(tail[-8:]))

    total = round(cursor, 1)
    _set(job, status="done", stage="Selesai", progress=1.0, video=str(out), duration=total)
    (project_dir / "artifacts" / "render.json").write_text(
        json.dumps({"video": str(out), "duration": total, "size": [width, height]}, indent=2), encoding="utf-8"
    )


@app.post("/api/render")
def start_render(body: dict) -> JSONResponse:
    scenes = body.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return JSONResponse({"error": "validation", "message": "Storyboard masih kosong."}, status_code=422)
    status = tools_status()
    missing = [k for k in ("piper", "piper_model", "npx", "ffmpeg", "ffprobe", "composer") if not status[k]]
    if missing:
        return JSONResponse({"error": "render_unavailable", "message": "Belum tersedia: " + ", ".join(missing)}, status_code=503)
    if any(j["status"] in ("queued", "running") for j in JOBS.values()):
        return JSONResponse({"error": "busy", "message": "Masih ada video yang sedang dirender."}, status_code=409)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_id = f"studio-{_slug(body.get('title') or 'video')}-{stamp}"
    job = {"id": job_id, "dir": str(PROJECTS_DIR / job_id), "status": "queued", "stage": "Menyiapkan…",
           "progress": 0.0, "started": time.time(), "updated": time.time()}
    JOBS[job_id] = job
    threading.Thread(target=_run_render, args=(job, body), daemon=True).start()
    return JSONResponse(_public_job(job))


def _public_job(job: dict) -> dict:
    out = {k: job.get(k) for k in ("id", "status", "stage", "progress", "error", "duration")}
    if job.get("status") == "done":
        out["url"] = f"/api/render/{job['id']}/video"
    return out


def _job_or_disk(job_id: str) -> dict:
    if not re.fullmatch(r"studio-[a-z0-9-]+", job_id):
        raise HTTPException(404)
    if job_id in JOBS:
        return JOBS[job_id]
    # Server sempat restart: pulihkan dari disk.
    meta = PROJECTS_DIR / job_id / "artifacts" / "render.json"
    if meta.is_file():
        info = json.loads(meta.read_text(encoding="utf-8"))
        return {"id": job_id, "status": "done", "progress": 1.0, "video": info["video"], "duration": info.get("duration")}
    return {"id": job_id, "status": "error", "error": "Job tidak ditemukan (server di-restart saat render)."}


@app.get("/api/render/{job_id}")
def render_status(job_id: str) -> JSONResponse:
    return JSONResponse(_public_job(_job_or_disk(job_id)))


@app.get("/api/render/{job_id}/video")
def render_video(job_id: str, download: int = 0) -> FileResponse:
    job = _job_or_disk(job_id)
    path = Path(job.get("video") or "")
    if job.get("status") != "done" or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", filename=path.name if download else None)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    s = tools_status()
    return {
        "ai": bool(s["claude"]),
        "video": all(s[k] for k in ("piper", "piper_model", "npx", "ffmpeg", "ffprobe", "composer")),
        "model": CLAUDE_MODEL,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((UI_DIR / "index.html").read_text(encoding="utf-8"))

"""
MoM Generator — Web App (FastAPI)

Alur:
  1. User mengisi kolom header (Klien, Penugasan, Tanggal, Jam, dll) + kehadiran.
  2. User upload recording (audio/video) dan/atau catatan kasar (teks / PDF).
  3. Backend: Whisper transkrip -> LLM susun MoM -> render .docx.
  4. User download .docx.

Transkripsi berjalan LOKAL (Whisper + GPU). Penyusunan MoM memakai OpenAI
gpt-5-nano secara default, atau LLM lokal via Ollama bila LLM_BACKEND=ollama.
"""
import os
import uuid
import json
import shutil
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config as cfg
from app.core import transcribe, summarize, docx_builder, pdf_extract
from app.core.datetime_id import JamTidakValid, format_hari_tanggal, format_jam

BASE = Path(__file__).parent
DATA_DIR = cfg.DATA_DIR
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
for d in (UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

METODE_VALID = {"Offline", "Online", "Hybrid"}

app = FastAPI(title="MoM Generator")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# job store sederhana in-memory
JOBS = {}  # job_id -> {status, step, progress, error, output, meta}


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    """Cek konfigurasi & dependency — berguna saat setup pertama kali."""
    return cfg.health()


@app.post("/api/generate")
async def generate(
    klien: str = Form(""),
    penugasan: str = Form(""),
    topik: str = Form(""),
    tanggal: str = Form(""),          # YYYY-MM-DD dari <input type="date">
    jam_mulai: str = Form(""),        # HH:MM dari <input type="time">
    jam_selesai: str = Form(""),      # HH:MM
    zona: str = Form("WIB"),
    metode: str = Form(""),           # Offline | Online | Hybrid
    lokasi: str = Form(""),
    kehadiran: str = Form("[]"),      # JSON: [{"grup":..,"orang":[..]}]
    catatan: str = Form(""),
    next_meetings: str = Form("[]"),  # JSON
    recording: UploadFile = File(None),
    catatan_pdf: UploadFile = File(None),
):
    # --- validasi & normalisasi input header ---
    hari_tanggal = format_hari_tanggal(tanggal)
    if tanggal.strip() and not hari_tanggal:
        raise HTTPException(400, "Format tanggal tidak valid (harus YYYY-MM-DD).")

    try:
        jam = format_jam(jam_mulai, jam_selesai, zona)
    except JamTidakValid as e:
        raise HTTPException(400, str(e))

    metode = metode.strip()
    if metode and metode not in METODE_VALID:
        raise HTTPException(
            400, f"Metode rapat harus salah satu dari: {', '.join(sorted(METODE_VALID))}."
        )

    try:
        kehadiran_data = json.loads(kehadiran or "[]")
        next_data = json.loads(next_meetings or "[]")
    except json.JSONDecodeError:
        raise HTTPException(400, "Format kehadiran/next_meetings tidak valid.")

    job_id = uuid.uuid4().hex[:12]

    # --- catatan kasar: teks manual + hasil ekstraksi PDF ---
    catatan_gabungan = catatan.strip()
    pdf_info = None
    if catatan_pdf is not None and catatan_pdf.filename:
        if Path(catatan_pdf.filename).suffix.lower() != ".pdf":
            raise HTTPException(400, "Catatan kasar hanya menerima file berformat PDF.")
        pdf_path = UPLOAD_DIR / f"{job_id}_catatan.pdf"
        _simpan_terbatas(catatan_pdf, pdf_path, cfg.MAX_PDF_MB)
        try:
            teks_pdf = pdf_extract.extract_pdf_text(str(pdf_path))
        except pdf_extract.PDFError as e:
            raise HTTPException(400, str(e))
        pdf_info = {"nama": catatan_pdf.filename, "karakter": len(teks_pdf)}
        judul = f"[Catatan dari {catatan_pdf.filename}]"
        catatan_gabungan = f"{catatan_gabungan}\n\n{judul}\n{teks_pdf}".strip()

    if not catatan_gabungan and not (recording is not None and recording.filename):
        raise HTTPException(400, "Unggah rekaman atau isi catatan kasar terlebih dahulu.")

    # --- rekaman ---
    audio_path = None
    if recording is not None and recording.filename:
        ext = Path(recording.filename).suffix or ".dat"
        audio_path = UPLOAD_DIR / f"{job_id}{ext}"
        with open(audio_path, "wb") as f:
            shutil.copyfileobj(recording.file, f)

    meta = {
        "klien": klien, "penugasan": penugasan, "topik": topik,
        "hari_tanggal": hari_tanggal, "jam": jam, "metode": metode,
        "lokasi": lokasi, "kehadiran": kehadiran_data,
        "next_meetings": next_data,
    }

    JOBS[job_id] = {
        "status": "queued", "step": "Menunggu", "progress": 0,
        "error": None, "output": None, "meta": meta, "pdf": pdf_info,
    }

    t = threading.Thread(
        target=_process_job,
        args=(job_id, str(audio_path) if audio_path else None, catatan_gabungan, meta),
        daemon=True,
    )
    t.start()
    return {"job_id": job_id, "hari_tanggal": hari_tanggal, "jam": jam, "pdf": pdf_info}


def _simpan_terbatas(upload: UploadFile, dest: Path, max_mb: int):
    """Salin UploadFile ke disk sambil membatasi ukuran maksimum."""
    limit = max_mb * 1024 * 1024
    total = 0
    with open(dest, "wb") as f:
        while chunk := upload.file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"Ukuran PDF melebihi batas {max_mb} MB.")
            f.write(chunk)


def _process_job(job_id, audio_path, catatan, meta):
    job = JOBS[job_id]
    try:
        transcript = ""
        if audio_path:
            job.update(status="running", step="Transkripsi audio (Whisper)", progress=10)

            def prog(cur, total):
                if total:
                    pct = 10 + int(50 * min(cur / total, 1.0))
                    job["progress"] = pct

            transcript = transcribe.transcribe(audio_path, progress_cb=prog)

        job.update(status="running", step="Menyusun MoM (LLM)", progress=65)
        structured = summarize.summarize(transcript, catatan)

        job.update(step="Membuat dokumen Word", progress=85)
        data = dict(meta)
        data["diskusi"] = structured.get("diskusi", [])
        data["tindak_lanjut"] = structured.get("tindak_lanjut", [])

        out_name = f"MoM_{job_id}.docx"
        out_path = OUTPUT_DIR / out_name
        docx_builder.build_mom(data, str(out_path))

        job.update(status="done", step="Selesai", progress=100,
                   output=out_name, transcript=transcript)
    except Exception as e:
        traceback.print_exc()
        job.update(status="error", step="Gagal", error=str(e))


@app.get("/api/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    return JSONResponse({
        "status": job["status"], "step": job["step"],
        "progress": job["progress"], "error": job["error"],
        "output": job["output"],
    })


@app.get("/api/download/{job_id}")
def download(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.get("output"):
        raise HTTPException(404, "File belum siap")
    path = OUTPUT_DIR / job["output"]
    if not path.exists():
        raise HTTPException(404, "File tidak ditemukan")
    fname = f"MoM_{job['meta'].get('topik','') or 'Rapat'}.docx".replace("/", "-")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=fname,
    )


@app.get("/api/transcript/{job_id}")
def get_transcript(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    return {"transcript": job.get("transcript", "")}

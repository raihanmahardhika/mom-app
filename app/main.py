"""
MoM Generator — Web App (FastAPI)

Alur:
  1. User mengisi kolom header (Klien, Penugasan, Tanggal, Jam, dll) + kehadiran.
  2. User upload recording (audio/video, opsional, bisa beberapa file) dan/atau
     catatan kasar (teks / PDF / DOCX / TXT / MD, bisa beberapa file).
  3. Backend: transkripsi Whisper tiap rekaman (berurutan, lalu digabung)
     -> LLM susun MoM -> render .docx.
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
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config as cfg
from app.core import transcribe, summarize, docx_builder, note_extract
from app.core.datetime_id import JamTidakValid, format_hari_tanggal, format_jam

BASE = Path(__file__).parent
DATA_DIR = cfg.DATA_DIR
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
USAGE_HISTORY_PATH = DATA_DIR / "token_usage_history.json"
for d in (UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

METODE_VALID = {"Offline", "Online", "Hybrid"}
CATATAN_EXTENSIONS = note_extract.SUPPORTED_EXTENSIONS
USAGE_FIELDS = (
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "total_tokens",
)
USAGE_HISTORY_LOCK = threading.Lock()

app = FastAPI(title="MoM Generator")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

# job store sederhana in-memory
JOBS = {}  # job_id -> {status, step, progress, error, output, meta}


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/usage", response_class=HTMLResponse)
def usage_page():
    """Halaman ringkasan pemakaian token yang tersimpan secara lokal."""
    return (BASE / "templates" / "usage.html").read_text(encoding="utf-8")


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
    recordings: list[UploadFile] = File(default=[]),
    # Kompatibilitas untuk klien versi lama yang masih mengirim satu rekaman.
    recording: UploadFile = File(None),
    catatan_files: list[UploadFile] = File(default=[]),
    # Kompatibilitas untuk klien versi lama yang masih mengirim satu PDF.
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
    if not isinstance(kehadiran_data, list) or not isinstance(next_data, list):
        raise HTTPException(400, "Format kehadiran/next_meetings harus berupa daftar.")
    next_data = _normalisasi_next_meetings(next_data)

    job_id = uuid.uuid4().hex[:12]

    # --- catatan kasar: teks manual + hasil ekstraksi banyak dokumen ---
    catatan_gabungan = catatan.strip()
    catatan_info = []
    unggahan_catatan = [f for f in (catatan_files or []) if f is not None and f.filename]
    if catatan_pdf is not None and catatan_pdf.filename:
        unggahan_catatan.append(catatan_pdf)
    if len(unggahan_catatan) > cfg.MAX_NOTE_FILES:
        raise HTTPException(
            400,
            f"Maksimum {cfg.MAX_NOTE_FILES} dokumen catatan dapat diunggah sekaligus.",
        )

    for index, unggahan in enumerate(unggahan_catatan, start=1):
        suffix = Path(unggahan.filename).suffix.lower()
        if suffix not in CATATAN_EXTENSIONS:
            daftar = ", ".join(ext.removeprefix(".").upper() for ext in sorted(CATATAN_EXTENSIONS))
            raise HTTPException(400, f"Format catatan tidak didukung. Gunakan: {daftar}.")

        catatan_path = UPLOAD_DIR / f"{job_id}_catatan_{index}{suffix}"
        _simpan_terbatas(
            unggahan,
            catatan_path,
            cfg.MAX_NOTE_FILE_MB,
            jenis="dokumen catatan",
        )
        try:
            teks_catatan = note_extract.extract_note_text(str(catatan_path))
        except note_extract.NoteExtractError as e:
            raise HTTPException(400, str(e))
        catatan_info.append({"nama": unggahan.filename, "karakter": len(teks_catatan)})
        judul = f"[Catatan dari {unggahan.filename}]"
        catatan_gabungan = f"{catatan_gabungan}\n\n{judul}\n{teks_catatan}".strip()

    # --- rekaman (bisa beberapa; ditranskripsi sesuai urutan unggah) ---
    unggahan_rekaman = [f for f in (recordings or []) if f is not None and f.filename]
    if recording is not None and recording.filename:
        unggahan_rekaman.append(recording)
    if len(unggahan_rekaman) > cfg.MAX_RECORDINGS:
        raise HTTPException(
            400,
            f"Maksimum {cfg.MAX_RECORDINGS} rekaman dapat diunggah sekaligus.",
        )

    if not catatan_gabungan and not unggahan_rekaman:
        raise HTTPException(400, "Unggah rekaman atau isi catatan kasar terlebih dahulu.")

    rekaman = []  # [(path, nama asli)]
    for index, unggahan in enumerate(unggahan_rekaman, start=1):
        ext = Path(unggahan.filename).suffix or ".dat"
        audio_path = UPLOAD_DIR / f"{job_id}_rekaman_{index}{ext}"
        with open(audio_path, "wb") as f:
            shutil.copyfileobj(unggahan.file, f)
        rekaman.append((str(audio_path), unggahan.filename))

    meta = {
        "klien": klien, "penugasan": penugasan, "topik": topik,
        "tanggal": tanggal.strip(),
        "hari_tanggal": hari_tanggal, "jam": jam, "metode": metode,
        "lokasi": lokasi, "kehadiran": kehadiran_data,
        "next_meetings": next_data,
    }

    JOBS[job_id] = {
        "status": "queued", "step": "Menunggu", "progress": 0,
        "error": None, "output": None, "meta": meta, "catatan_files": catatan_info,
        "recordings": [nama for _, nama in rekaman],
        "usage": None, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    t = threading.Thread(
        target=_process_job,
        args=(job_id, rekaman, catatan_gabungan, meta),
        daemon=True,
    )
    t.start()
    return {
        "job_id": job_id,
        "hari_tanggal": hari_tanggal,
        "jam": jam,
        "catatan_files": catatan_info,
        "recordings": [nama for _, nama in rekaman],
    }


def _simpan_terbatas(upload: UploadFile, dest: Path, max_mb: int, jenis: str = "file"):
    """Salin UploadFile ke disk sambil membatasi ukuran maksimum."""
    limit = max_mb * 1024 * 1024
    total = 0
    with open(dest, "wb") as f:
        while chunk := upload.file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"Ukuran {jenis} melebihi batas {max_mb} MB.")
            f.write(chunk)


def _normalisasi_next_meetings(raw: list) -> list[dict]:
    """Buang baris rencana rapat yang kosong agar tidak muncul di MoM."""
    hasil = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        meeting = {
            "nama": str(item.get("nama", "") or "").strip(),
            "tanggal": str(item.get("tanggal", "") or "").strip(),
            "catatan": str(item.get("catatan", "") or "").strip(),
        }
        if any(meeting.values()):
            hasil.append(meeting)
    return hasil


def _process_job(job_id, rekaman, catatan, meta):
    job = JOBS[job_id]
    try:
        transcript = ""
        if rekaman:
            # Rentang progres 10-60% dibagi rata ke setiap rekaman.
            jumlah = len(rekaman)
            bagian = []
            for urutan, (audio_path, nama) in enumerate(rekaman):
                awal = 10 + 50 * urutan / jumlah
                step = "Transkripsi audio (Whisper)"
                if jumlah > 1:
                    step = f"Transkripsi audio (Whisper) {urutan + 1}/{jumlah}: {nama}"
                job.update(status="running", step=step, progress=int(awal))

                def prog(cur, total, awal=awal):
                    if total:
                        job["progress"] = int(awal + (50 / jumlah) * min(cur / total, 1.0))

                teks = transcribe.transcribe(audio_path, progress_cb=prog)
                if jumlah > 1:
                    teks = f"[Rekaman {urutan + 1} dari {jumlah}: {nama}]\n{teks}"
                bagian.append(teks)
            transcript = "\n\n".join(bagian)
        else:
            # Catatan saja adalah sumber yang sah; tidak ada tahap transkripsi.
            job.update(status="running", step="Membaca catatan kasar", progress=20)

        job.update(status="running", step="Menyusun MoM (LLM)", progress=65)
        structured, usage = summarize.summarize_with_usage(transcript, catatan)
        job["usage"] = usage

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
    finally:
        if job.get("usage") is not None:
            _simpan_riwayat_usage(job_id, job)


@app.get("/api/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    return JSONResponse({
        "status": job["status"], "step": job["step"],
        "progress": job["progress"], "error": job["error"],
        "output": job["output"], "usage": job.get("usage"),
        "peringatan": (job.get("usage") or {}).get("peringatan"),
    })


@app.get("/api/usage")
def usage():
    """Usage token historis dan job yang masih berjalan pada sesi saat ini."""
    totals = {field: 0 for field in USAGE_FIELDS}
    jobs = _baca_riwayat_usage()
    job_ids_tercatat = {item.get("job_id") for item in jobs}

    for job_id, job in JOBS.items():
        if job_id in job_ids_tercatat:
            continue
        jobs.append(_rekam_usage(job_id, job))

    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    for job in jobs:
        token_usage = job.get("usage") or _usage_menunggu()
        for field in USAGE_FIELDS:
            value = token_usage.get(field)
            if isinstance(value, int):
                totals[field] += value

    return {
        "default_model": cfg.OPENAI_MODEL,
        "total": totals,
        "jobs": jobs,
    }


def _rekam_usage(job_id: str, job: dict) -> dict:
    """Bentuk data minimum yang aman untuk riwayat tanpa menyimpan catatan rapat."""
    usage = job.get("usage") or _usage_menunggu()
    return {
        "job_id": job_id,
        "created_at": job.get("created_at", ""),
        "status": job.get("status", ""),
        "topik": job.get("meta", {}).get("topik", "") or "(Tanpa topik)",
        "usage": {
            "available": bool(usage.get("available")),
            "backend": str(usage.get("backend") or "tidak diketahui"),
            "model": str(usage.get("model") or "tidak diketahui"),
            **{
                field: usage.get(field) if isinstance(usage.get(field), int) else None
                for field in USAGE_FIELDS
            },
        },
    }


def _baca_riwayat_usage() -> list[dict]:
    """Baca riwayat; berkas yang rusak tidak pernah ditimpa secara otomatis."""
    if not USAGE_HISTORY_PATH.exists():
        return []
    try:
        raw = json.loads(USAGE_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[usage] Riwayat token tidak dapat dibaca: {exc}")
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("job_id")]


def _simpan_riwayat_usage(job_id: str, job: dict) -> None:
    """Simpan satu riwayat atomik agar tetap tersedia setelah server direstart."""
    with USAGE_HISTORY_LOCK:
        if USAGE_HISTORY_PATH.exists():
            try:
                riwayat = json.loads(USAGE_HISTORY_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[usage] Riwayat token tidak disimpan agar berkas rusak tidak tertimpa: {exc}")
                return
            if not isinstance(riwayat, list):
                print("[usage] Riwayat token tidak disimpan karena format berkas tidak valid.")
                return
        else:
            riwayat = []

        riwayat = [
            item for item in riwayat
            if isinstance(item, dict) and item.get("job_id") != job_id
        ]
        riwayat.append(_rekam_usage(job_id, job))
        temporary = USAGE_HISTORY_PATH.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(riwayat, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(USAGE_HISTORY_PATH)
        except OSError as exc:
            print(f"[usage] Riwayat token gagal disimpan: {exc}")
            temporary.unlink(missing_ok=True)


def _usage_menunggu() -> dict:
    return {
        "available": False,
        "backend": "menunggu",
        "model": "Belum selesai",
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "cached_tokens": None,
        "total_tokens": None,
    }


@app.get("/api/download/{job_id}")
def download(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.get("output"):
        raise HTTPException(404, "File belum siap")
    path = OUTPUT_DIR / job["output"]
    if not path.exists():
        raise HTTPException(404, "File tidak ditemukan")
    tanggal = _nama_file_aman(job["meta"].get("tanggal", ""), "tanpa-tanggal")
    topik = _nama_file_aman(job["meta"].get("topik", ""), "Rapat")
    fname = f"[MoM] {tanggal} - {topik}.docx"
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=fname,
    )


def _nama_file_aman(value: str, fallback: str) -> str:
    """Bersihkan komponen nama file agar valid dan aman pada Windows."""
    bersih = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(value or ""))
    bersih = re.sub(r"\s+", " ", bersih).strip(" .")
    return (bersih or fallback)[:120]


@app.get("/api/transcript/{job_id}")
def get_transcript(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    return {"transcript": job.get("transcript", "")}

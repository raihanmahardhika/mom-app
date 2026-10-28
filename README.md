# Notula — Generator MoM (Whisper GPU + GPT-5 nano)

Aplikasi web untuk mengubah **catatan kasar, rekaman rapat, atau keduanya** menjadi
**Minutes of Meeting (MoM)** berformat Word, dengan format yang meniru template MoM
RSM Indonesia.

- **Transkripsi**: OpenAI Whisper (`faster-whisper`) — berjalan **lokal di GPU Anda**.
- **Penyusunan MoM**: **OpenAI gpt-5-nano** (default), atau LLM lokal via **Ollama**,
  atau ekstraktor berbasis-aturan bila keduanya tidak tersedia.

Kolom header (Klien, Penugasan, Topik, dll.) diisi manual di UI. Isi **Diskusi**
dan **Tindak Lanjut** digenerate otomatis dari transkrip dan/atau catatan kasar.
Catatan dapat diketik langsung atau diunggah dalam beberapa file PDF, DOCX, TXT,
atau Markdown.

> **Catatan privasi:** transkrip dikirim ke API OpenAI saat penyusunan MoM.
> Untuk rapat yang datanya tidak boleh keluar (mis. proyek pemerintah), set
> `LLM_BACKEND=ollama` di `.env` — seluruh proses jadi 100% lokal.

---

## 1. Prasyarat

- Python 3.10+ (teruji di 3.12)
- **ffmpeg** — wajib, dipakai Whisper untuk membaca audio/video
  - Windows: `winget install Gyan.FFmpeg` lalu **buka terminal baru**
  - macOS: `brew install ffmpeg` · Ubuntu: `sudo apt install ffmpeg`
- GPU NVIDIA (opsional tapi disarankan) — driver CUDA 12/13

## 2. Instalasi

```bash
cd mom-app
python -m venv venv
venv\Scripts\activate          # Linux/macOS: source venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-gpu.txt   # hanya bila memakai GPU NVIDIA
```

`requirements-gpu.txt` memasang cuBLAS + cuDNN lewat pip, sehingga **tidak perlu**
memasang CUDA Toolkit secara manual.

## 3. Konfigurasi

Semua setting ada di file **`.env`** (sudah dibuat dari `.env.example`).
Yang wajib diisi hanya API key:

```ini
OPENAI_API_KEY=sk-...            # <<< ganti dengan API key Anda
```

Setting penting lainnya (nilai default sudah sesuai untuk GPU 6 GB):

| Variabel | Default | Keterangan |
|---|---|---|
| `LLM_BACKEND` | `openai` | `openai` / `ollama` / `rule` |
| `OPENAI_MODEL` | `gpt-5-nano` | model penyusun MoM |
| `OPENAI_REASONING_EFFORT` | `minimal` | `minimal`/`low`/`medium`/`high` |
| `WHISPER_MODEL` | `small` | `tiny`/`base`/`small`/`medium`/`large-v3` |
| `WHISPER_DEVICE` | `cuda` | `cuda` (GPU) / `cpu` / `auto` |
| `WHISPER_COMPUTE` | `float16` | `float16` untuk GPU, `int8` untuk CPU |
| `MAX_NOTE_FILE_MB` | `25` | batas ukuran setiap dokumen catatan |
| `MAX_NOTE_FILES` | `10` | jumlah maksimum dokumen catatan per MoM |
| `MAX_RECORDINGS` | `5` | jumlah maksimum rekaman per MoM |

Bila GPU gagal dipakai (driver/VRAM/cuDNN), aplikasi **otomatis turun ke CPU**
dan tetap berjalan — pesannya muncul di konsol server.

## 4. Menjalankan aplikasi

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Buka browser: **http://localhost:8000**

Cek kesiapan setup kapan saja lewat **http://localhost:8000/api/health** —
endpoint ini melaporkan status API key, paket yang terpasang, dan ffmpeg.

Pantau token yang dipakai setiap proses MoM lewat **http://localhost:8000/usage**.
Halaman tersebut menampilkan token input, output, reasoning (bila dilaporkan
model), total historis, serta backend/model yang benar-benar dipakai. Riwayat
disimpan secara lokal di `data/token_usage_history.json`, sehingga tetap ada
setelah server direstart.

## 5. Cara pakai

1. Isi **Identitas Rapat**:
   - **Hari, Tanggal** → pilih lewat kalender; nama hari (mis. "Senin") terisi otomatis.
   - **Jam Mulai** & **Jam Selesai** → jam selesai wajib lebih besar dari jam mulai;
     durasi rapat ditampilkan otomatis.
   - **Metode Rapat** → pilih **Offline / Online / Hybrid**.
2. Isi **Kehadiran** — kelompokkan per instansi, satu nama per baris.
3. Pilih minimal satu sumber notula:
   - isi **catatan kasar** langsung di aplikasi;
   - unggah satu atau beberapa file catatan **PDF, DOCX, TXT, atau MD**; dan/atau
   - unggah satu atau beberapa **rekaman** rapat (mis. rapat yang terpotong
     menjadi beberapa file). Rekaman bersifat opsional, ditranskripsi berurutan
     sesuai daftar, lalu transkripnya digabung menjadi satu MoM.
4. (opsional) Isi **rencana rapat berikutnya**.
5. Klik **Buat MoM** → tunggu proses → **Unduh MoM (.docx)**.

> PDF hasil **scan/foto** tidak punya layer teks sehingga tidak bisa dibaca —
> aplikasi akan memberi tahu dan Anda dapat memakai catatan manual atau dokumen lain.

Bagian **Expected Next Meeting(s)** tidak akan disertakan dalam MoM jika seluruh
baris rencana rapat dibiarkan kosong.

---

## Pemilihan model Whisper (akurasi vs kecepatan)

| Model     | VRAM (float16) | Kecepatan | Akurasi Bhs Indonesia |
|-----------|----------------|-----------|-----------------------|
| tiny      | ~1 GB          | sangat cepat | rendah             |
| base      | ~1 GB          | cepat     | sedang                |
| **small** | **~2 GB**      | **cepat** | **baik** (dipakai saat ini) |
| medium    | ~5 GB          | sedang    | sangat baik           |
| large-v3  | ~10 GB         | lambat    | terbaik               |

Naikkan ke `medium` di `.env` bila akurasi kurang dan VRAM mencukupi.

## Struktur proyek

```
mom-app/
├─ app/
│  ├─ main.py                 # FastAPI: endpoint generate, status, download, health
│  ├─ config.py               # semua setting + pemuat .env
│  ├─ core/
│  │  ├─ transcribe.py        # Whisper (GPU, model di-cache)
│  │  ├─ summarize.py         # gpt-5-nano -> Ollama -> rule-based
│  │  ├─ note_extract.py      # ekstraksi teks PDF, DOCX, TXT, dan MD
│  │  ├─ pdf_extract.py       # ekstraksi teks PDF
│  │  ├─ datetime_id.py       # format tanggal & jam Bahasa Indonesia
│  │  └─ docx_builder.py      # render .docx sesuai format template
│  └─ templates/index.html    # UI
├─ .env                       # konfigurasi Anda (jangan di-commit)
├─ .env.example               # contoh konfigurasi
├─ requirements.txt
└─ requirements-gpu.txt
```

## Pemecahan masalah

**`ModuleNotFoundError: No module named 'faster_whisper'`**
Anda menjalankan uvicorn dari Python sistem, bukan dari venv. Aktifkan dulu:
`venv\Scripts\activate`, pastikan prompt berawalan `(venv)`.

**`RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`**
DLL CUDA hasil pip tidak ditemukan CTranslate2. Sudah ditangani otomatis oleh
`_daftarkan_dll_cuda()` di `app/core/transcribe.py`. Bila masih muncul,
pastikan `pip install -r requirements-gpu.txt` sudah dijalankan **di dalam venv**.
Aplikasi tetap berjalan (otomatis pindah ke CPU), hanya lebih lambat.

**ffmpeg tidak terdeteksi padahal sudah diinstall**
Buka terminal baru — PATH hanya dibaca saat terminal dimulai.

**Perubahan kode tidak terlihat**
Restart uvicorn. File `.py` hanya dibaca saat startup (kecuali dijalankan dengan
`--reload`). Perubahan pada `index.html` cukup refresh browser.

## Catatan keamanan data

- Transkripsi (Whisper) berjalan lokal; audio tidak pernah dikirim keluar.
- Penyusunan MoM memakai API OpenAI — set `LLM_BACKEND=ollama` bila seluruh
  proses harus offline.
- File unggahan & output disimpan di `data/uploads` dan `data/outputs`.
  Hapus berkala sesuai kebijakan retensi data Anda.
- `.env` berisi API key dan sudah masuk `.gitignore` — jangan di-commit.

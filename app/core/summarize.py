"""
Mengubah transkrip mentah (+ catatan kasar) menjadi struktur MoM.

Backend penyusun MoM (pilih lewat env LLM_BACKEND):
  - "openai"  (default) -> OpenAI API, model gpt-5-nano
  - "ollama"            -> LLM lokal via http://localhost:11434
  - "rule"              -> ekstraktor berbasis aturan, tanpa LLM

Urutan fallback: backend pilihan -> backend cadangan -> rule-based.
Aplikasi tidak pernah gagal total; paling buruk hasilnya sederhana.

Output selalu berupa dict:
  {"diskusi": [...], "tindak_lanjut": [...]}
yang langsung dipakai docx_builder.build_mom().
"""
import json
import re
import requests

from app import config as cfg

SYSTEM_PROMPT = """Anda adalah asisten notulen rapat profesional untuk perusahaan konsultan.
Tugas Anda: mengubah transkrip rapat (dan catatan kasar bila ada) menjadi
Minutes of Meeting (MoM) yang ringkas, formal, dan akurat dalam Bahasa Indonesia.

ATURAN:
- Jangan mengarang informasi yang tidak ada di transkrip.
- Kelompokkan hasil diskusi menjadi beberapa TOPIK bernomor dengan judul singkat.
- Tiap topik berisi poin-poin ringkas (kalimat lengkap, gaya formal notulen).
- Ekstrak semua ACTION ITEM / tindak lanjut beserta PIC (penanggung jawab) dan
  target waktu bila disebutkan.
- Gunakan istilah yang muncul di transkrip; rapikan bahasanya.
- Catatan kasar lebih tepercaya daripada transkrip bila keduanya berbeda,
  karena transkrip bisa salah dengar.

Balas HANYA dalam format JSON valid berikut, tanpa teks lain, tanpa markdown:
{
  "diskusi": [
    {"judul": "Judul Topik", "poin": ["poin 1", "poin 2"]}
  ],
  "tindak_lanjut": [
    {"tindak_lanjut": "deskripsi", "ref": "", "pic": "nama/tim", "target": "tanggal/kosong"}
  ]
}"""


def summarize(transcript: str, catatan: str = "") -> dict:
    """Kembalikan struktur MoM sesuai backend terpilih, dengan fallback berjenjang."""
    if not (transcript or "").strip() and not (catatan or "").strip():
        return {
            "diskusi": [{"judul": "Ringkasan", "poin": ["(Tidak ada transkrip maupun catatan)"]}],
            "tindak_lanjut": [],
        }

    urutan = {
        "openai": ["openai", "ollama"],
        "ollama": ["ollama", "openai"],
        "rule": [],
    }.get(cfg.LLM_BACKEND, ["openai", "ollama"])

    for nama in urutan:
        if not _tersedia(nama):
            continue
        try:
            return _RUNNER[nama](transcript, catatan)
        except Exception as e:
            print(f"[summarize] Backend '{nama}' gagal ({e}) - coba berikutnya.")

    return _summarize_rulebased(transcript, catatan)


def _tersedia(nama: str) -> bool:
    if nama == "openai":
        return bool(cfg.OPENAI_API_KEY)
    if nama == "ollama":
        try:
            return requests.get(f"{cfg.OLLAMA_URL}/api/tags", timeout=3).status_code == 200
        except Exception:
            return False
    return False


def _user_prompt(transcript: str, catatan: str) -> str:
    bagian = []
    if (catatan or "").strip():
        bagian.append(f"CATATAN KASAR (ditulis peserta rapat):\n{catatan.strip()}")
    if (transcript or "").strip():
        bagian.append(
            f"TRANSKRIP RAPAT (hasil Whisper, mungkin ada salah dengar):\n{transcript.strip()}"
        )
    return "\n\n".join(bagian)


# ---------- Backend OpenAI (gpt-5-nano) ----------

def _summarize_openai(transcript: str, catatan: str) -> dict:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("Paket 'openai' belum terinstall (pip install openai)") from e

    client = OpenAI(
        api_key=cfg.OPENAI_API_KEY,
        base_url=cfg.OPENAI_BASE_URL,
        timeout=cfg.OPENAI_TIMEOUT,
        max_retries=2,
    )

    kwargs = {
        "model": cfg.OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(transcript, catatan)},
        ],
        "response_format": {"type": "json_object"},
        # gpt-5-nano adalah reasoning model -> pakai max_completion_tokens,
        # dan temperature tidak didukung (nilainya selalu 1).
        "max_completion_tokens": cfg.OPENAI_MAX_OUTPUT_TOKENS,
    }
    if cfg.OPENAI_REASONING_EFFORT:
        kwargs["reasoning_effort"] = cfg.OPENAI_REASONING_EFFORT

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:
        # Model non-reasoning / endpoint kompatibel yang menolak parameter baru.
        if not _param_tak_dikenal(e):
            raise
        kwargs.pop("reasoning_effort", None)
        kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
        resp = client.chat.completions.create(**kwargs)

    choice = resp.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        alasan = getattr(choice, "finish_reason", "")
        if alasan == "length":
            raise RuntimeError(
                "Jawaban kosong: anggaran token habis untuk reasoning. "
                "Naikkan OPENAI_MAX_OUTPUT_TOKENS atau turunkan OPENAI_REASONING_EFFORT."
            )
        raise RuntimeError(f"Model mengembalikan jawaban kosong (finish_reason={alasan}).")

    return _normalisasi(json.loads(_strip_fence(content)))


def _param_tak_dikenal(err: Exception) -> bool:
    pesan = str(err).lower()
    return any(
        k in pesan
        for k in (
            "unsupported", "unrecognized", "unknown parameter",
            "max_completion_tokens", "reasoning_effort", "not supported",
        )
    )


# ---------- Backend Ollama (LLM lokal) ----------

def _summarize_ollama(transcript: str, catatan: str) -> dict:
    payload = {
        "model": cfg.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(transcript, catatan)},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }
    r = requests.post(f"{cfg.OLLAMA_URL}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    return _normalisasi(json.loads(_strip_fence(r.json()["message"]["content"])))


def _strip_fence(teks: str) -> str:
    """Buang pagar markdown (```json ... ```) bila model tetap menambahkannya."""
    teks = teks.strip()
    if teks.startswith("```"):
        teks = re.sub(r"^```[a-zA-Z]*\s*", "", teks)
        teks = re.sub(r"\s*```$", "", teks)
    return teks.strip()


def _normalisasi(data: dict) -> dict:
    """Pastikan struktur & tipe field sesuai kontrak docx_builder."""
    if not isinstance(data, dict):
        raise ValueError("Respons LLM bukan objek JSON.")

    diskusi = []
    for sec in data.get("diskusi") or []:
        if not isinstance(sec, dict):
            continue
        poin = sec.get("poin") or []
        if isinstance(poin, str):
            poin = [poin]
        diskusi.append({
            "judul": str(sec.get("judul", "")).strip(),
            "poin": [str(p).strip() for p in poin if str(p).strip()],
        })

    tindak = []
    for t in data.get("tindak_lanjut") or []:
        if not isinstance(t, dict):
            continue
        tindak.append({
            "tindak_lanjut": str(t.get("tindak_lanjut", "")).strip(),
            "ref": str(t.get("ref", "") or "").strip(),
            "pic": str(t.get("pic", "") or "").strip(),
            "target": str(t.get("target", "") or "").strip(),
        })

    return {"diskusi": diskusi, "tindak_lanjut": [t for t in tindak if t["tindak_lanjut"]]}


# ---------- Fallback rule-based (tanpa LLM) ----------

_ACTION_KW = re.compile(
    r"\b(akan|perlu|harus|segera|menyusun|mengirim|konfirmasi|assessment|"
    r"pengembangan|penyediaan|klarifikasi|penambahan|follow[- ]?up|tindak lanjut|"
    r"ditugaskan|bertanggung jawab|PIC|target)\b",
    re.IGNORECASE,
)


def _split_sentences(text: str):
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) > 3]


def _summarize_rulebased(transcript: str, catatan: str) -> dict:
    """
    Ekstraktor sederhana tanpa LLM:
    - Menggabungkan catatan + transkrip.
    - Kalimat yang mengandung kata kunci aksi -> tindak lanjut.
    - Sisanya dikelompokkan sebagai poin diskusi (satu topik umum + chunking).
    """
    combined = (catatan + "\n" + transcript).strip()
    sentences = _split_sentences(combined)

    tindak = []
    poin = []
    for s in sentences:
        if _ACTION_KW.search(s):
            pic = ""
            m = re.search(r"\((\w[\w &./]+)\)", s)
            if m:
                pic = m.group(1).strip()
            tindak.append({"tindak_lanjut": s, "ref": "", "pic": pic, "target": ""})
        else:
            poin.append(s)

    # bagi poin diskusi jadi beberapa "topik" per ~5 kalimat agar terbaca
    diskusi = []
    chunk_size = 5
    for i in range(0, len(poin), chunk_size):
        chunk = poin[i:i + chunk_size]
        diskusi.append({
            "judul": f"Pembahasan {i // chunk_size + 1}",
            "poin": chunk,
        })
    if not diskusi:
        diskusi = [{"judul": "Ringkasan", "poin": poin or ["(Transkrip kosong)"]}]

    return {"diskusi": diskusi, "tindak_lanjut": tindak}


_RUNNER = {"openai": _summarize_openai, "ollama": _summarize_ollama}

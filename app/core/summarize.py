"""
Mengubah transkrip mentah (+ catatan kasar) menjadi struktur MoM.

Backend penyusun MoM (pilih lewat env LLM_BACKEND):
  - "openai"  (default) -> OpenAI API, model gpt-5-nano
  - "ollama"            -> LLM lokal via http://localhost:11434
  - "rule"              -> ekstraktor berbasis aturan, tanpa LLM

Urutan fallback: backend pilihan -> backend cadangan -> rule-based.
Aplikasi tidak pernah gagal total; paling buruk hasilnya sederhana. Bila sampai
jatuh ke rule-based, alasan kegagalan LLM dicatat di usage["peringatan"].

Transkrip yang melebihi context window model diproses bertahap (map-reduce):
tiap bagian dirangkum terpisah, lalu draf per bagian digabung menjadi satu MoM.

Output selalu berupa dict:
  {"diskusi": [{"judul", "narasi", "poin"}], "tindak_lanjut": [...]}
yang langsung dipakai docx_builder.build_mom().
"""
import json
import re
import unicodedata
import requests

from app import config as cfg

SYSTEM_PROMPT = """Anda adalah asisten notulen rapat profesional untuk perusahaan konsultan.
Tugas Anda: mengubah transkrip rapat (dan catatan kasar bila ada) menjadi
Minutes of Meeting (MoM) yang ringkas, formal, dan akurat dalam Bahasa Indonesia.

ATURAN:
- Jangan mengarang informasi yang tidak ada di transkrip.
- Susun hasil diskusi seperti MoM profesional: kelompokkan hanya topik yang
  benar-benar berbeda. Umumnya cukup 4-7 topik untuk satu rapat; jangan memecah
  satu tema menjadi beberapa topik kecil yang saling tumpang tindih.
- Untuk tiap topik, gunakan "narasi" berisi satu paragraf padat 2-4 kalimat.
  Gunakan paragraf kedua hanya bila topiknya memang luas. Narasi harus menjelaskan
  inti pembahasan, pertimbangan penting, dan hasilnya secara mengalir.
- Gunakan "poin" secara selektif, hanya bila keputusan, kebutuhan, risiko, daftar
  opsi, atau rincian diskret akan lebih jelas dibaca sebagai bullet. Setiap topik
  boleh tidak memiliki bullet sama sekali; jangan membuat kuota atau pola tetap,
  atau menambahkan bullet hanya untuk memenuhi jumlah tertentu. Jangan mengulang
  isi narasi dalam bullet. Tindak lanjut operasional harus dicatat hanya di
  "tindak_lanjut", bukan diulang dalam diskusi.
- Nilai di dalam "poin" WAJIB berupa kalimat teks biasa. Jangan mengirim objek
  JSON, pasangan key-value, atau struktur seperti {"konteks": "...",
  "keputusan": "..."} sebagai isi bullet.
- Gunakan Bahasa Indonesia baku dan formal. Jangan gunakan karakter atau kata dari
  bahasa lain kecuali istilah, nama produk, akronim, atau kutipan yang benar-benar
  muncul pada sumber rapat. Jangan membuat placeholder, catatan dalam tanda siku
  ([...]), atau label mekanis seperti "Pembahasan:", "Hasil:", dan "Solusi:".
- Jangan mengulang informasi yang sama antara kalimat, antar-topik, atau antara
  narasi dan tindak lanjut.
- Ekstrak semua ACTION ITEM / tindak lanjut beserta PIC (penanggung jawab) dan
  target waktu bila disebutkan.
- Gunakan istilah yang muncul di transkrip; rapikan bahasanya.
- Catatan kasar lebih tepercaya daripada transkrip bila keduanya berbeda,
  karena transkrip bisa salah dengar.

Balas HANYA dalam format JSON valid berikut, tanpa teks lain, tanpa markdown:
{
  "diskusi": [
    {
      "judul": "Judul Topik",
      "narasi": ["Paragraf notulen yang merangkum pembahasan dan hasilnya."],
      "poin": []
    }
  ],
  "tindak_lanjut": [
    {"tindak_lanjut": "deskripsi", "ref": "", "pic": "nama/tim", "target": "tanggal/kosong"}
  ]
}"""

MERGE_PROMPT = SYSTEM_PROMPT + """

CATATAN KHUSUS: Transkrip rapat terlalu panjang sehingga sudah dirangkum per
bagian. Input berisi draf MoM (JSON) untuk tiap bagian secara berurutan, plus
catatan kasar bila ada. Gabungkan semua draf menjadi SATU MoM utuh: satukan topik
yang sama dari bagian berbeda, hilangkan duplikasi, dan gabungkan tindak lanjut
yang sama. Jangan menambah informasi yang tidak ada di draf atau catatan."""


def summarize(transcript: str, catatan: str = "") -> dict:
    """Kembalikan struktur MoM; kompatibel dengan pemanggil lama tanpa data usage."""
    structured, _ = summarize_with_usage(transcript, catatan)
    return structured


def summarize_with_usage(transcript: str, catatan: str = "") -> tuple[dict, dict]:
    """Kembalikan struktur MoM dan penggunaan token dari backend yang benar-benar dipakai."""
    if not (transcript or "").strip() and not (catatan or "").strip():
        return {
            "diskusi": [{
                "judul": "Ringkasan",
                "narasi": ["(Tidak ada transkrip maupun catatan)"],
                "poin": [],
            }],
            "tindak_lanjut": [],
        }, _usage_tidak_tersedia("rule", "Rule-based")

    urutan = {
        "openai": ["openai", "ollama"],
        "ollama": ["ollama", "openai"],
        "rule": [],
    }.get(cfg.LLM_BACKEND, ["openai", "ollama"])

    kegagalan = []
    for nama in urutan:
        if not _tersedia(nama):
            kegagalan.append(f"{nama}: tidak tersedia")
            continue
        try:
            return _dengan_pemecahan(_RUNNER[nama], transcript, catatan)
        except Exception as e:
            print(f"[summarize] Backend '{nama}' gagal ({e}) - coba berikutnya.")
            kegagalan.append(f"{nama}: {e}")

    usage = _usage_tidak_tersedia("rule", "Rule-based")
    if cfg.LLM_BACKEND != "rule":
        usage["peringatan"] = (
            "LLM gagal menyusun MoM, sehingga hasil diskusi hanya berupa potongan "
            "transkrip (rule-based). Detail: " + " | ".join(kegagalan)
        )
    return _summarize_rulebased(transcript, catatan), usage


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


# ---------- Pemrosesan bertahap untuk transkrip panjang ----------

def _dengan_pemecahan(call, transcript: str, catatan: str) -> tuple[dict, dict]:
    """Coba sekali jalan; bila context window terlampaui, pakai map-reduce."""
    try:
        data, usage = call(SYSTEM_PROMPT, _user_prompt(transcript, catatan))
        return _normalisasi(data), usage
    except Exception as e:
        if not _context_terlampaui(e) or not (transcript or "").strip():
            raise
    print("[summarize] Transkrip melebihi context window model - diproses per bagian.")

    draf, usages = [], []
    for teks in _pecah_teks(transcript, cfg.LLM_CHUNK_CHARS):
        draf_bagian, usage_bagian = _rangkum_bagian(call, teks)
        draf.extend(draf_bagian)
        usages.extend(usage_bagian)

    bagian = []
    if (catatan or "").strip():
        bagian.append(f"CATATAN KASAR (ditulis peserta rapat):\n{catatan.strip()}")
    for i, d in enumerate(draf, start=1):
        bagian.append(
            f"DRAF MoM BAGIAN {i} dari {len(draf)}:\n{json.dumps(d, ensure_ascii=False)}"
        )
    data, usage_akhir = call(MERGE_PROMPT, "\n\n".join(bagian))
    return _normalisasi(data), _gabung_usage(usages + [usage_akhir])


def _rangkum_bagian(call, teks: str, kedalaman: int = 0) -> tuple[list, list]:
    """Rangkum satu potongan transkrip; belah dua lagi bila masih terlalu panjang."""
    try:
        data, usage = call(
            SYSTEM_PROMPT,
            f"TRANSKRIP RAPAT - SEBAGIAN (hasil Whisper, mungkin ada salah dengar):\n{teks}",
        )
        return [_normalisasi(data)], [usage]
    except Exception as e:
        if not _context_terlampaui(e) or kedalaman >= 4 or len(teks) < 2000:
            raise
    draf, usages = [], []
    for sub in _pecah_teks(teks, len(teks) // 2 + 1):
        d, u = _rangkum_bagian(call, sub, kedalaman + 1)
        draf.extend(d)
        usages.extend(u)
    return draf, usages


def _pecah_teks(teks: str, maks: int) -> list[str]:
    """Potong teks maksimal `maks` karakter, sebisa mungkin di batas paragraf/kalimat."""
    teks = teks.strip()
    hasil = []
    while len(teks) > maks:
        jendela = teks[:maks]
        cut = jendela.rfind("\n")
        if cut < maks // 2:
            cut = max(jendela.rfind(". "), jendela.rfind("? "), jendela.rfind("! "))
            cut = cut + 1 if cut >= 0 else -1
        if cut < maks // 2:
            cut = jendela.rfind(" ")
        if cut <= 0:
            cut = maks
        hasil.append(teks[:cut].strip())
        teks = teks[cut:].strip()
    if teks:
        hasil.append(teks)
    return hasil


class OutputTerpotong(RuntimeError):
    """Jawaban model terpotong di tengah karena batas token output."""


def _context_terlampaui(err: Exception) -> bool:
    """Input terlalu panjang atau output terpotong -> perlu potongan lebih kecil."""
    if isinstance(err, OutputTerpotong):
        return True
    pesan = str(err).lower()
    return any(
        k in pesan
        for k in (
            "context length", "context window", "contextwindowexceeded",
            "maximum context", "context_length_exceeded", "too many tokens",
            "reduce the length",
        )
    )


def _gabung_usage(usages: list[dict]) -> dict:
    total = {}
    for f in (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "cached_tokens", "reasoning_tokens",
    ):
        nilai = [u.get(f) for u in usages if isinstance(u.get(f), int)]
        total[f] = sum(nilai) if nilai else None
    return _usage_record(backend=usages[-1]["backend"], model=usages[-1]["model"], **total)


# ---------- Backend OpenAI (gpt-5-nano) ----------

def _call_openai(system: str, user: str) -> tuple[dict, dict]:
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
            {"role": "system", "content": system},
            {"role": "user", "content": user},
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
    if getattr(choice, "finish_reason", "") == "length":
        # JSON pasti terpotong; bagian transkrip perlu diperkecil.
        raise OutputTerpotong("Output model terpotong karena OPENAI_MAX_OUTPUT_TOKENS habis.")

    return json.loads(_strip_fence(content)), _usage_openai(resp)


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

def _call_ollama(system: str, user: str) -> tuple[dict, dict]:
    payload = {
        "model": cfg.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }
    r = requests.post(f"{cfg.OLLAMA_URL}/api/chat", json=payload, timeout=600)
    r.raise_for_status()
    response = r.json()
    return json.loads(_strip_fence(response["message"]["content"])), _usage_ollama(response)


def _usage_openai(response) -> dict:
    """Ambil usage dari respons Chat Completions tanpa bergantung pada versi SDK."""
    usage = _field(response, "usage")
    prompt_details = _field(usage, "prompt_tokens_details")
    completion_details = _field(usage, "completion_tokens_details")
    return _usage_record(
        backend="openai",
        model=_field(response, "model") or cfg.OPENAI_MODEL,
        prompt_tokens=_field(usage, "prompt_tokens"),
        completion_tokens=_field(usage, "completion_tokens"),
        total_tokens=_field(usage, "total_tokens"),
        cached_tokens=_field(prompt_details, "cached_tokens"),
        reasoning_tokens=_field(completion_details, "reasoning_tokens"),
    )


def _usage_ollama(response: dict) -> dict:
    """Ollama melaporkan token input/output dengan nama field yang berbeda."""
    prompt_tokens = _field(response, "prompt_eval_count")
    completion_tokens = _field(response, "eval_count")
    total_tokens = None
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        total_tokens = prompt_tokens + completion_tokens
    return _usage_record(
        backend="ollama",
        model=_field(response, "model") or cfg.OLLAMA_MODEL,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _usage_tidak_tersedia(backend: str, model: str) -> dict:
    return _usage_record(backend=backend, model=model)


def _usage_record(
    *, backend: str, model: str, prompt_tokens=None, completion_tokens=None,
    total_tokens=None, cached_tokens=None, reasoning_tokens=None,
) -> dict:
    """Normalisasi format usage agar bisa langsung dipakai API dan halaman web."""
    fields = {
        "prompt_tokens": _angka(prompt_tokens),
        "completion_tokens": _angka(completion_tokens),
        "total_tokens": _angka(total_tokens),
        "cached_tokens": _angka(cached_tokens),
        "reasoning_tokens": _angka(reasoning_tokens),
    }
    return {
        "available": any(value is not None for value in fields.values()),
        "backend": backend,
        "model": model,
        **fields,
    }


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _angka(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
    raw_diskusi = data.get("diskusi") or []
    if isinstance(raw_diskusi, dict):
        raw_diskusi = [raw_diskusi]
    if not isinstance(raw_diskusi, list):
        raw_diskusi = []

    for sec in raw_diskusi:
        if not isinstance(sec, dict):
            continue
        narasi = sec.get("narasi") or []
        if not isinstance(narasi, list):
            narasi = [narasi]
        poin = sec.get("poin") or []
        if not isinstance(poin, list):
            poin = [poin]
        judul = _bersihkan_teks_diskusi(sec.get("judul", ""))
        judul = judul or f"Pembahasan {len(diskusi) + 1}"
        narasi_bersih = [_bersihkan_teks_diskusi(n) for n in narasi]
        poin_bersih = [_bersihkan_poin_diskusi(p) for p in poin]
        narasi_bersih = [n for n in narasi_bersih if n]
        poin_bersih = [p for p in poin_bersih if p]
        # Pertahankan bullet yang memang relevan. Jika respons lama hanya berisi
        # poin, ubah menjadi paragraf agar topik tetap memiliki konteks naratif.
        if not narasi_bersih and poin_bersih:
            narasi_bersih = _gabung_narasi(poin_bersih)
            poin_bersih = []
        if not narasi_bersih and not poin_bersih:
            continue
        diskusi.append({
            "judul": judul,
            "narasi": narasi_bersih,
            "poin": poin_bersih,
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


def _bersihkan_teks_diskusi(value) -> str:
    """Tahan placeholder dan karakter lintas-skrip yang tidak layak masuk ke MoM."""
    text = str(value or "").strip()
    if not text or _mengandung_skrip_tak_didukung(text):
        return ""
    if text.startswith("[") and text.endswith("]"):
        return ""
    # Label seperti ini membuat tiap topik terasa seperti template yang kaku.
    text = re.sub(r"^(?:pembahasan|hasil|solusi|keputusan)\s*:\s*", "", text, flags=re.I)
    return text.strip()


def _bersihkan_poin_diskusi(value) -> str:
    """Ubah poin terstruktur dari model menjadi satu kalimat bullet yang natural."""
    if isinstance(value, dict):
        konteks = _bersihkan_teks_diskusi(
            value.get("konteks") or value.get("kategori") or value.get("topik") or ""
        )
        isi = ""
        for key in ("keputusan", "isi", "ringkasan", "deskripsi", "detail", "hasil", "catatan"):
            if key in value:
                isi = _bersihkan_poin_diskusi(value[key])
                if isi:
                    break

        if konteks and isi:
            return isi if konteks.casefold() in isi.casefold() else f"{konteks}: {isi}"
        if isi:
            return isi

        # Cadangan untuk bentuk objek tak terduga: ambil nilainya tanpa pernah
        # menampilkan representasi dictionary Python/JSON di dokumen Word.
        values = [_bersihkan_poin_diskusi(item) for item in value.values()]
        return "; ".join(item for item in values if item)

    if isinstance(value, list):
        values = [_bersihkan_poin_diskusi(item) for item in value]
        return "; ".join(item for item in values if item)

    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return _bersihkan_poin_diskusi(parsed)
    return _bersihkan_teks_diskusi(text)


def _mengandung_skrip_tak_didukung(text: str) -> bool:
    """MoM berbahasa Indonesia hanya menerima huruf Latin pada teks generatif."""
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "LATIN" not in name:
            return True
    return "\ufffd" in text


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
    - Sisa pembahasan dibuat menjadi paragraf naratif, bukan dipaksa menjadi
      kumpulan lima bullet per topik.
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

    paragraphs = _gabung_narasi(poin)
    diskusi = [{
        "judul": "Ringkasan Diskusi",
        "narasi": paragraphs or ["(Transkrip dan catatan tidak memuat pembahasan)"],
        "poin": [],
    }]

    return {"diskusi": diskusi, "tindak_lanjut": tindak}


def _gabung_narasi(sentences: list[str], max_per_paragraph: int = 3) -> list[str]:
    """Gabungkan kalimat menjadi paragraf pendek yang nyaman dibaca di MoM."""
    return [
        " ".join(sentences[i:i + max_per_paragraph])
        for i in range(0, len(sentences), max_per_paragraph)
    ]


_RUNNER = {"openai": _call_openai, "ollama": _call_ollama}

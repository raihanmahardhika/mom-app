"""
Transkripsi audio/video menggunakan OpenAI Whisper (lokal).

Mendukung dua backend:
  - faster-whisper (direkomendasikan: jauh lebih cepat, hemat memori)
  - openai-whisper (implementasi referensi asli dari github.com/openai/whisper)

Default konfigurasi (lihat .env):
  WHISPER_MODEL=small  WHISPER_DEVICE=cuda  WHISPER_COMPUTE=float16
yang pas untuk GPU NVIDIA kelas 6 GB.

Model di-cache di memori, jadi rapat kedua dan seterusnya tidak perlu
memuat ulang bobot model.
"""
import os
import site
import threading
from pathlib import Path

from app import config as cfg

_MODEL = None
_MODEL_KEY = None
_LOCK = threading.Lock()


def _daftarkan_dll_cuda():
    """
    Windows: daftarkan folder DLL CUDA hasil `pip install nvidia-*`.

    CTranslate2 (mesin di balik faster-whisper) tidak mencari DLL di
    site-packages/nvidia/*/bin secara otomatis — berbeda dengan PyTorch.
    Tanpa ini, model berhasil dimuat tetapi gagal saat inferensi dengan:
        RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

    Dipanggil saat modul di-import, yaitu SEBELUM faster_whisper di-import.
    """
    if os.name != "nt":
        return

    basis = set(site.getsitepackages())
    try:
        basis.add(site.getusersitepackages())
    except Exception:
        pass

    folder = []
    for base in basis:
        try:
            folder += [str(p) for p in Path(base).glob("nvidia/*/bin") if p.is_dir()]
        except Exception:
            continue

    for d in folder:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    if folder:
        # Cadangan: sebagian versi CTranslate2 memuat DLL lewat PATH proses.
        os.environ["PATH"] = os.pathsep.join(folder) + os.pathsep + os.environ.get("PATH", "")


_daftarkan_dll_cuda()


def transcribe(audio_path: str, progress_cb=None) -> str:
    """Kembalikan transkrip teks penuh dari file audio/video."""
    if cfg.WHISPER_BACKEND == "openai":
        return _transcribe_openai(audio_path, progress_cb)
    return _transcribe_faster(audio_path, progress_cb)


def _load_faster_model():
    """Muat (atau ambil dari cache) WhisperModel, dengan fallback GPU -> CPU."""
    global _MODEL, _MODEL_KEY
    from faster_whisper import WhisperModel

    key = (cfg.WHISPER_MODEL, cfg.WHISPER_DEVICE, cfg.WHISPER_COMPUTE)
    with _LOCK:
        if _MODEL is not None and _MODEL_KEY == key:
            return _MODEL
        try:
            model = WhisperModel(
                cfg.WHISPER_MODEL,
                device=cfg.WHISPER_DEVICE,
                compute_type=cfg.WHISPER_COMPUTE,
            )
        except Exception as e:
            if cfg.WHISPER_DEVICE == "cpu":
                raise
            # GPU gagal (driver/cuDNN/VRAM) -> jangan gagalkan job, turun ke CPU.
            print(f"[transcribe] GPU tidak bisa dipakai ({e}). Fallback ke CPU int8.")
            model = WhisperModel(cfg.WHISPER_MODEL, device="cpu", compute_type="int8")
        _MODEL, _MODEL_KEY = model, key
        return model


def _transcribe_faster(audio_path: str, progress_cb=None) -> str:
    try:
        return _jalankan(_load_faster_model(), audio_path, progress_cb)
    except Exception as e:
        # Sebagian kegagalan GPU (DLL CUDA hilang, VRAM habis) baru muncul saat
        # inferensi, bukan saat model dimuat — jadi fallback harus ada di sini juga.
        if cfg.WHISPER_DEVICE == "cpu":
            raise
        print(f"[transcribe] Inferensi GPU gagal ({e}). Mengulang di CPU.")
        return _jalankan(_muat_cpu(), audio_path, progress_cb)


def _jalankan(model, audio_path: str, progress_cb=None) -> str:
    segments, info = model.transcribe(
        audio_path,
        language=cfg.WHISPER_LANGUAGE if cfg.WHISPER_LANGUAGE != "auto" else None,
        vad_filter=True,
        beam_size=cfg.WHISPER_BEAM_SIZE,
    )
    parts = []
    for seg in segments:
        parts.append(seg.text.strip())
        if progress_cb:
            progress_cb(seg.end, info.duration)
    return " ".join(parts).strip()


def _muat_cpu():
    """Muat ulang model di CPU dan jadikan model aktif (agar job berikutnya tidak retry GPU)."""
    global _MODEL, _MODEL_KEY
    from faster_whisper import WhisperModel
    with _LOCK:
        model = WhisperModel(cfg.WHISPER_MODEL, device="cpu", compute_type="int8")
        _MODEL, _MODEL_KEY = model, (cfg.WHISPER_MODEL, "cpu", "int8")
        return model


def _transcribe_openai(audio_path: str, progress_cb=None) -> str:
    import whisper  # openai-whisper

    model = whisper.load_model(cfg.WHISPER_MODEL)
    result = model.transcribe(
        audio_path,
        language=None if cfg.WHISPER_LANGUAGE == "auto" else cfg.WHISPER_LANGUAGE,
        fp16=cfg.WHISPER_DEVICE != "cpu",
    )
    return result["text"].strip()

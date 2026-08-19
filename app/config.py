"""
Konfigurasi terpusat aplikasi MoM.

Semua setting dibaca dari environment variable, dengan file `.env` di root
proyek sebagai sumber utama (dimuat otomatis saat modul ini di-import).

Ubah nilai di `.env` — tidak perlu menyentuh kode.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

# Muat .env sedini mungkin (sebelum modul lain membaca os.environ).
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=False)
    _DOTENV_OK = True
except ImportError:          # python-dotenv belum terinstall -> pakai env OS saja
    _DOTENV_OK = False


def _s(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(_s(key) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- LLM
# Backend penyusun MoM: "openai" | "ollama" | "rule"
LLM_BACKEND = _s("LLM_BACKEND", "openai").lower()

OPENAI_API_KEY = _s("OPENAI_API_KEY")
OPENAI_MODEL = _s("OPENAI_MODEL", "gpt-5-nano")
OPENAI_BASE_URL = _s("OPENAI_BASE_URL") or None      # None = api.openai.com
# gpt-5-nano adalah reasoning model: "minimal" paling cepat & murah untuk
# tugas ekstraksi terstruktur seperti ini.
OPENAI_REASONING_EFFORT = _s("OPENAI_REASONING_EFFORT", "minimal").lower()
OPENAI_MAX_OUTPUT_TOKENS = _i("OPENAI_MAX_OUTPUT_TOKENS", 16000)
OPENAI_TIMEOUT = _i("OPENAI_TIMEOUT", 600)

OLLAMA_URL = _s("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = _s("OLLAMA_MODEL", "qwen2.5:14b-instruct")

# ---------------------------------------------------------------- Whisper
WHISPER_BACKEND = _s("WHISPER_BACKEND", "faster").lower()   # faster | openai
WHISPER_MODEL = _s("WHISPER_MODEL", "small")
WHISPER_LANGUAGE = _s("WHISPER_LANGUAGE", "id")
WHISPER_DEVICE = _s("WHISPER_DEVICE", "cuda")               # cuda | cpu | auto
WHISPER_COMPUTE = _s("WHISPER_COMPUTE", "float16")          # float16 (GPU) | int8 (CPU)
WHISPER_BEAM_SIZE = _i("WHISPER_BEAM_SIZE", 5)

# ---------------------------------------------------------------- Upload
DATA_DIR = Path(_s("MOM_DATA_DIR") or (BASE_DIR / "data"))
MAX_PDF_MB = _i("MAX_PDF_MB", 25)


def _key_state() -> str:
    if not OPENAI_API_KEY:
        return "kosong"
    if not OPENAI_API_KEY.startswith("sk-") or len(OPENAI_API_KEY) < 25:
        return "placeholder"       # masih nilai contoh, belum diganti
    return "terisi"


def health() -> dict:
    """Ringkasan konfigurasi + dependency — dipakai endpoint /api/health."""
    def _has(mod: str) -> bool:
        import importlib.util
        return importlib.util.find_spec(mod) is not None

    import shutil
    return {
        "env_file": str(ENV_FILE) if ENV_FILE.exists() else None,
        "dotenv_terpasang": _DOTENV_OK,
        "llm": {
            "backend": LLM_BACKEND,
            "openai_model": OPENAI_MODEL,
            "openai_api_key": _key_state(),
            "paket_openai": _has("openai"),
            "ollama_model": OLLAMA_MODEL,
        },
        "whisper": {
            "backend": WHISPER_BACKEND,
            "model": WHISPER_MODEL,
            "device": WHISPER_DEVICE,
            "compute_type": WHISPER_COMPUTE,
            "bahasa": WHISPER_LANGUAGE,
            "paket_faster_whisper": _has("faster_whisper"),
        },
        "pdf": {"paket_pypdf": _has("pypdf")},
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }

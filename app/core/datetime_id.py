"""
Format tanggal & jam Bahasa Indonesia untuk header MoM.

Input dari UI memakai format standar HTML:
  - tanggal   : "YYYY-MM-DD"  (<input type="date">)
  - jam       : "HH:MM"       (<input type="time">)

Output mengikuti gaya template MoM RSM:
  - "Senin, 27 Juli 2026"
  - "10.00 - 12.30 WIB"
"""
from datetime import date, time

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]


class JamTidakValid(ValueError):
    """Jam selesai tidak lebih besar dari jam mulai."""


def parse_tanggal(iso: str):
    """'2026-07-27' -> date, atau None bila kosong/tidak valid."""
    iso = (iso or "").strip()
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def parse_jam(hhmm: str):
    """'10:30' atau '10:30:00' -> time, atau None bila kosong/tidak valid."""
    hhmm = (hhmm or "").strip()
    if not hhmm:
        return None
    try:
        return time.fromisoformat(hhmm)
    except ValueError:
        return None


def format_hari_tanggal(iso: str) -> str:
    """'2026-07-27' -> 'Senin, 27 Juli 2026'. String kosong bila input kosong."""
    d = parse_tanggal(iso)
    if d is None:
        return ""
    return f"{HARI[d.weekday()]}, {d.day} {BULAN[d.month - 1]} {d.year}"


def format_jam(mulai: str, selesai: str, zona: str = "WIB") -> str:
    """
    Gabungkan jam mulai & selesai jadi '10.00 - 12.30 WIB'.

    Raise JamTidakValid bila jam selesai <= jam mulai.
    Bila hanya salah satu terisi, kembalikan yang ada saja
    (mis. '10.00 - selesai' bila jam selesai kosong).
    """
    t1, t2 = parse_jam(mulai), parse_jam(selesai)
    zona = (zona or "").strip()
    suffix = f" {zona}" if zona else ""

    if t1 is None and t2 is None:
        return ""
    if t1 is not None and t2 is None:
        return f"{_hm(t1)} - selesai{suffix}"
    if t1 is None:
        return f"s.d. {_hm(t2)}{suffix}"

    if t2 <= t1:
        raise JamTidakValid(
            f"Jam selesai ({_hm(t2)}) harus lebih besar dari jam mulai ({_hm(t1)})."
        )
    return f"{_hm(t1)} - {_hm(t2)}{suffix}"


def durasi_menit(mulai: str, selesai: str):
    """Selisih menit antara jam mulai & selesai, atau None bila tak lengkap."""
    t1, t2 = parse_jam(mulai), parse_jam(selesai)
    if t1 is None or t2 is None:
        return None
    return (t2.hour * 60 + t2.minute) - (t1.hour * 60 + t1.minute)


def _hm(t: time) -> str:
    """time -> '10.00' (gaya Indonesia, titik sebagai pemisah)."""
    return f"{t.hour:02d}.{t.minute:02d}"

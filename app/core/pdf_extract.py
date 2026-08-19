"""
Ekstraksi teks dari PDF catatan kasar.

Dipakai bila user mengunggah catatan rapat dalam bentuk PDF (hasil export
Word/Notes/OneNote). Teks hasil ekstraksi digabung dengan catatan yang diketik
manual, lalu dikirim ke LLM bersama transkrip.

Catatan: PDF hasil SCAN (gambar) tidak berisi layer teks sehingga hasilnya
kosong — modul ini akan memberi pesan yang jelas, bukan gagal diam-diam.
"""
import re


class PDFError(RuntimeError):
    pass


def extract_pdf_text(path: str) -> str:
    """Kembalikan teks seluruh halaman PDF, dirapikan."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise PDFError(
            "Paket 'pypdf' belum terinstall. Jalankan: pip install pypdf"
        ) from e

    try:
        reader = PdfReader(path)
    except Exception as e:
        raise PDFError(f"File PDF tidak dapat dibaca: {e}") from e

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")          # PDF terproteksi tanpa password
        except Exception as e:
            raise PDFError("PDF terkunci password — buka proteksinya dulu.") from e

    parts = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            teks = page.extract_text() or ""
        except Exception:
            teks = ""
        teks = _bersihkan(teks)
        if teks:
            parts.append(teks)

    hasil = "\n\n".join(parts).strip()
    if not hasil:
        raise PDFError(
            "Tidak ada teks yang bisa diambil dari PDF ini. "
            "Kemungkinan berupa hasil scan/gambar — silakan ketik catatan manual."
        )
    return hasil


def _bersihkan(teks: str) -> str:
    """Rapikan artefak umum ekstraksi PDF: pemenggalan baris & spasi ganda."""
    teks = teks.replace("\u00ad", "")                    # soft hyphen
    teks = re.sub(r"-\n(?=\w)", "", teks)                # kata terpenggal antar baris
    teks = re.sub(r"[ \t]+", " ", teks)
    teks = re.sub(r"\n{3,}", "\n\n", teks)
    return "\n".join(b.strip() for b in teks.split("\n")).strip()

"""Local OCR — reading, never clicking.

OCR is for extracting TEXT from a frame. It is deliberately not used to
locate controls: 11C resolves click targets through UIA/Playwright, which
return real control objects instead of pixel guesses.

Tesseract is an optional local install (like Piper). Without it Anna says so
honestly rather than pretending she read the screen.
"""

import shutil

from app.agent.devlog import devlog

INSTALL_HINT = ("Install Tesseract OCR to let me read screen text locally: "
                "winget install UB-Mannheim.TesseractOCR   (then "
                "pip install pytesseract). Nothing leaves this PC.")


def _tesseract_exe(config) -> str:
    configured = (getattr(config, "tesseract_exe", "") or "").strip()
    if configured:
        return configured
    return shutil.which("tesseract") or ""


def ocr_status(config) -> tuple[bool, str]:
    """(available, reason-if-not). Never raises."""
    backend = (getattr(config, "ocr_backend", "auto") or "auto").lower()
    if backend == "off":
        return False, "OCR is turned off in settings."
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False, INSTALL_HINT
    if not _tesseract_exe(config):
        return False, INSTALL_HINT
    return True, ""


def extract_text(frame, config) -> str:
    """OCR the frame locally. Returns "" when OCR isn't available — the
    caller degrades to window-title + heuristics rather than failing."""
    available, reason = ocr_status(config)
    if not available:
        devlog.log(f"Vision: OCR unavailable ({reason.split(':')[0]})")
        return ""
    try:
        import pytesseract
        exe = _tesseract_exe(config)
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        text = pytesseract.image_to_string(frame.image) or ""
    except Exception as e:
        devlog.warn(f"Vision: OCR failed ({' '.join(str(e).split())[:120]})")
        return ""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())

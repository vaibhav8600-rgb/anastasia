"""Local OCR — reading, never clicking.

OCR is for extracting TEXT from a frame. It is deliberately not used to
locate controls: 11C resolves click targets through UIA/Playwright, which
return real control objects instead of pixel guesses.

Tesseract is an optional local install (like Piper). Without it Anna says so
honestly rather than pretending she read the screen.
"""

import os
import shutil

from app.agent.devlog import devlog

INSTALL_HINT = ("Install Tesseract OCR to let me read screen text locally: "
                "winget install UB-Mannheim.TesseractOCR   (then "
                "python -m pip install pytesseract). Nothing leaves this PC.")

# winget's installer does NOT put tesseract on PATH, so shutil.which() misses a
# perfectly good install. Check where it actually lands before giving up.
_KNOWN_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 r"Programs\Tesseract-OCR\tesseract.exe"),
)


def _tesseract_exe(config) -> str:
    configured = (getattr(config, "tesseract_exe", "") or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    for path in _KNOWN_PATHS:
        if path and os.path.isfile(path):
            return path
    return ""


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


OCR_TIMEOUT_S = 12    # a whole 4K desktop must never hang the pipeline


def extract_text(frame, config) -> str:
    """OCR the frame locally. Returns "" when OCR isn't available — the
    caller degrades to window-title + heuristics rather than failing.

    Measured on a 2576x1408 window: colour 132 real words in 5.0s, grayscale
    142 in 5.0s, 2x upscale only 136 for 11.7s. So: grayscale (free accuracy),
    no upscale, and a hard timeout.
    """
    available, reason = ocr_status(config)
    if not available:
        devlog.log(f"Vision: OCR unavailable ({reason.split(':')[0]})")
        return ""
    import time
    started = time.perf_counter()
    try:
        import pytesseract
        exe = _tesseract_exe(config)
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        text = pytesseract.image_to_string(frame.image.convert("L"),
                                           timeout=OCR_TIMEOUT_S) or ""
    except Exception as e:
        devlog.warn(f"Vision: OCR failed ({' '.join(str(e).split())[:120]})")
        return ""
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    devlog.log(f"Vision: OCR read {len(text)} chars in "
               f"{time.perf_counter() - started:.1f}s (local)")
    return text

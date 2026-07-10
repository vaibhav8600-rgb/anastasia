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


# Tesseract is intrinsically slow on a dense desktop: measured 15-20s on a
# full 2560x1440 code editor at ANY page-segmentation mode — the tiny fonts,
# not the mode, are the cost. The only lever is resolution, so OCR downscales
# HARD before running. A fast pass (small, short cap) drives the sensitive
# pre-scan; a full pass (larger, still capped) is used only when cloud vision
# is off, since cloud describes a screen far faster and better.
FAST_OCR_PIXELS = 500_000
FAST_OCR_TIMEOUT_S = 4
FULL_OCR_PIXELS = 1_100_000
FULL_OCR_TIMEOUT_S = 8
# psm 6 (assume a uniform block) + oem 1 (LSTM) was the quickest that still
# read real text in benchmarking; --psm 3 (full layout analysis) is slower.
_TESS_CONFIG = "--oem 1 --psm 6"


def _downscale_for_ocr(image, max_pixels: int):
    width, height = image.size
    if max_pixels and width * height > max_pixels:
        scale = (max_pixels / float(width * height)) ** 0.5
        image = image.resize((max(1, round(width * scale)),
                              max(1, round(height * scale))))
    return image.convert("L")


def extract_text(frame, config, *, fast: bool = False) -> str:
    """OCR the frame locally. Returns "" when OCR isn't available or times out
    — the caller degrades gracefully rather than hanging.

    fast=True: a small, quickly-capped pass for the sensitive-content scan.
    fast=False: a larger (still bounded) pass for a local-only description.
    """
    available, reason = ocr_status(config)
    if not available:
        devlog.log(f"Vision: OCR unavailable ({reason.split(':')[0]})")
        return ""
    import time
    max_px = (int(getattr(config, "ocr_fast_pixels", FAST_OCR_PIXELS)) if fast
              else int(getattr(config, "ocr_max_pixels", FULL_OCR_PIXELS)))
    timeout = (FAST_OCR_TIMEOUT_S if fast
               else int(getattr(config, "ocr_timeout_s", FULL_OCR_TIMEOUT_S)))
    started = time.perf_counter()
    try:
        import pytesseract
        exe = _tesseract_exe(config)
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        image = _downscale_for_ocr(frame.image, max_px)
        text = pytesseract.image_to_string(
            image, config=_TESS_CONFIG, timeout=timeout) or ""
    except Exception as e:
        # pytesseract raises RuntimeError on timeout. Best-effort, never fatal.
        devlog.warn(f"Vision: OCR {'fast ' if fast else ''}gave up "
                    f"({' '.join(str(e).split())[:80]})")
        return ""
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    devlog.log(f"Vision: OCR read {len(text)} chars in "
               f"{time.perf_counter() - started:.1f}s "
               f"({'fast/local' if fast else 'local'})")
    return text

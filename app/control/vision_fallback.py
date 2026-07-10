"""Vision fallback — the LAST resort (11C, principle 3).

Used only when UIA and Playwright both fail to find the control: custom-drawn
UI, games, Electron apps with a sparse accessibility tree. It reads OCR word
boxes and guesses where the label is.

**Everything this returns is a guess**, so it is stamped `confidence < 1.0`
and carries a cropped screenshot of exactly the region that would be clicked.
The safety validator turns both facts into a mandatory confirmation. There is
no way to make this backend produce a certain target.
"""

import base64
import io

from app.agent.devlog import devlog
from app.control import ActionResult, ResolvedTarget, Scope

MAX_CONFIDENCE = 0.9        # a guess is never certain, by construction
MIN_MATCH = 70              # fuzzy score below this = not found
CROP_PAD = 12               # px of context around the target in the crop


def _crop_data_url(image, bbox) -> str:
    """A small picture of exactly what would be clicked, for the card."""
    try:
        left, top, right, bottom = bbox
        box = (max(0, left - CROP_PAD), max(0, top - CROP_PAD),
               min(image.width, right + CROP_PAD),
               min(image.height, bottom + CROP_PAD))
        crop = image.crop(box)
        if crop.width > 420:
            scale = 420 / crop.width
            crop = crop.resize((420, max(1, round(crop.height * scale))))
        buffer = io.BytesIO()
        crop.convert("RGB").save(buffer, "JPEG", quality=75)
        return "data:image/jpeg;base64," + \
            base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return ""


class VisionFallbackBackend:
    name = "vision"

    def __init__(self, config=None, capture=None, ocr_data=None):
        self.config = config
        self._capture = capture
        self._ocr_data = ocr_data

    def available(self) -> bool:
        from app.vision.ocr import ocr_status
        return ocr_status(self.config)[0]

    def _grab(self):
        if self._capture is not None:
            return self._capture()
        from app.vision import screen
        return screen.capture(self.config, scope="full")

    def _words(self, image):
        """[(text, left, top, right, bottom, ocr_conf)] from Tesseract."""
        if self._ocr_data is not None:
            return self._ocr_data(image)
        import pytesseract
        from app.vision.ocr import _tesseract_exe
        exe = _tesseract_exe(self.config)
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        data = pytesseract.image_to_data(image.convert("L"),
                                         output_type=pytesseract.Output.DICT)
        words = []
        for i, text in enumerate(data["text"]):
            text = (text or "").strip()
            if not text:
                continue
            left, top = data["left"][i], data["top"][i]
            words.append((text, left, top, left + data["width"][i],
                          top + data["height"][i], float(data["conf"][i])))
        return words

    def find_control(self, hint: str, scope: Scope = None):
        if not hint:
            return None
        frame = None
        try:
            frame = self._grab()
            words = self._words(frame.image)
            if not words:
                return None
            from rapidfuzz import fuzz
            want = hint.strip().lower()
            best, best_score = None, 0.0
            for word in words:
                score = fuzz.partial_ratio(want, word[0].lower())
                if score > best_score:
                    best, best_score = word, score
            if best is None or best_score < MIN_MATCH:
                return None

            bbox = (best[1], best[2], best[3], best[4])
            # OCR's own per-word confidence caps ours; never reaches 1.0.
            ocr_conf = max(0.0, min(best[5], 100.0)) / 100.0
            confidence = round(min(MAX_CONFIDENCE,
                                   (best_score / 100.0) * (0.5 + ocr_conf / 2)), 3)
            target = ResolvedTarget(
                name=best[0], control_type="VisionGuess", bbox=bbox,
                backend="vision", confidence=confidence,
                app=(scope.app if scope else ""),
                crop_data_url=_crop_data_url(frame.image, bbox))
            devlog.warn(f"Vision fallback GUESSED a target for {hint!r}: "
                        f"{target.describe()} — needs confirmation.")
            return target
        except Exception as e:
            devlog.warn(f"Vision fallback failed "
                        f"({' '.join(str(e).split())[:110]})")
            return None
        finally:
            if frame is not None:
                frame.release()

    def click(self, target: ResolvedTarget) -> ActionResult:
        try:
            import pyautogui
            pyautogui.click(*target.center())
            return ActionResult(True, f"Clicked where I saw “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't click there: "
                                       f"{' '.join(str(e).split())[:120]}")

    def type_into(self, target: ResolvedTarget, text: str) -> ActionResult:
        result = self.click(target)
        if not result.success:
            return result
        try:
            import pyautogui
            pyautogui.typewrite(text, interval=0.01)
            return ActionResult(True, f"Typed into “{target.name}”.")
        except Exception as e:
            return ActionResult(False, f"I couldn't type there: "
                                       f"{' '.join(str(e).split())[:120]}")

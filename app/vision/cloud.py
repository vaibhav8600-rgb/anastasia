"""Cloud vision — a single frame to Gemini, only with explicit consent (11B.4).

This is the ONLY code path by which a screen or camera frame may leave the
machine, and it hard-gates on `vision_cloud_allowed()` before touching the
network. No consent -> PrivacyViolation, never a silent local downgrade here
(the caller decides to fall back; this function refuses).

One still image per call. There is no streaming variant on purpose.
"""

from app.agent.devlog import devlog
from app.llm.providers import PrivacyViolation, vision_cloud_allowed
from app.voice.gemini_live import gemini_key

DEFAULT_PROMPT = ("Describe what is on this screen for someone who can't see "
                  "it. Be concise and concrete: the app, the main content, and "
                  "any error text. Two or three short sentences.")

CAMERA_PROMPT = ("Describe what you see in this photo in two or three short, "
                 "warm sentences. Do not try to identify or name any person.")

# The preview tier retires and rate-limits models without warning (verified
# live: 2.5-flash 404s for new keys; 3.5-flash and flash-latest returned 503
# while 3-flash-preview answered). One retry on a different model turns an
# outage into a blip; if both fail the caller falls back to local OCR.
FALLBACK_MODELS = ("gemini-3.1-flash-lite", "gemini-2.0-flash")


def describe_frame(frame, question: str, config) -> str:
    """Send ONE frame to Gemini and return its description."""
    allowed, why = vision_cloud_allowed(config)
    if not allowed:
        raise PrivacyViolation(f"Vision frame blocked: {why}")
    key = gemini_key(config)
    if not key:
        raise PrivacyViolation("Vision frame blocked: no Gemini API key")

    from google import genai
    prompt = question.strip() or (CAMERA_PROMPT if frame.source == "camera"
                                  else DEFAULT_PROMPT)
    client = genai.Client(api_key=key)
    configured = getattr(config, "vision_cloud_model", FALLBACK_MODELS[0])
    models = [configured] + [m for m in FALLBACK_MODELS if m != configured]

    last_error = None
    for model in models:
        try:
            response = client.models.generate_content(
                model=model, contents=[prompt, frame.image])
        except Exception as e:
            last_error = e
            devlog.warn(f"Vision: {model} unavailable "
                        f"({' '.join(str(e).split())[:90]}) — trying next.")
            continue
        text = (getattr(response, "text", "") or "").strip()
        # The description is loggable; the frame never is.
        devlog.log(f"Vision: cloud description of {frame.describe()} "
                   f"via {model} ({len(text)} chars)")
        return text
    raise RuntimeError(f"No cloud vision model answered: {last_error}")

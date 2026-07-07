"""clipboard_read / clipboard_write / summarize_clipboard."""

from app.tools import ToolContext, ToolResult, tool

_MAX_SUMMARY_INPUT = 6000


def _clip():
    import pyperclip
    return pyperclip


@tool("clipboard_read")
def clipboard_read(args: dict, ctx: ToolContext) -> ToolResult:
    text = _clip().paste() or ""
    if not text.strip():
        return ToolResult(True, "Your clipboard is empty right now.", data="")
    preview = text[:300] + ("…" if len(text) > 300 else "")
    return ToolResult(True, f"Your clipboard says: {preview}", data=text)


@tool("clipboard_write")
def clipboard_write(args: dict, ctx: ToolContext) -> ToolResult:
    text = str(args.get("text") or "")
    if not text:
        return ToolResult(False, "What should I put on the clipboard?")
    _clip().copy(text)
    return ToolResult(True, f"Copied {len(text)} characters to your clipboard.")


@tool("summarize_clipboard")
def summarize_clipboard(args: dict, ctx: ToolContext) -> ToolResult:
    text = (_clip().paste() or "").strip()
    if not text:
        return ToolResult(False, "Your clipboard is empty — copy some text first, then ask me again.")
    if ctx.llm is None and ctx.brain is None:
        return ToolResult(False, "I need the local AI model for that, and it isn't available right now.")
    if len(text) > _MAX_SUMMARY_INPUT:
        text = text[:_MAX_SUMMARY_INPUT]

    from app.llm.ollama_client import OllamaError
    from app.llm.prompt_builder import build_summarize_messages
    messages = build_summarize_messages(text, ctx.config, ctx.memory)
    try:
        if ctx.brain is not None:
            # Privacy-aware routing (8C): CLIPBOARD reaches the cloud only
            # with the explicit opt-in; otherwise the router keeps it local.
            from app.llm.providers import DataClass
            summary = ctx.brain.complete(
                "chat", messages,
                payload_classes={DataClass.TRANSCRIPT, DataClass.CLIPBOARD},
            ).text.strip()
        else:
            summary = ctx.llm.chat(messages, json_format=False,
                                   temperature=0.6).strip()
    except OllamaError as e:
        return ToolResult(False, str(e))
    from app.llm.intent_parser import strip_thinking
    summary = strip_thinking(summary).strip()
    if not summary:
        return ToolResult(False, "The model didn't give me a summary — try again?")
    return ToolResult(True, summary, data=summary)

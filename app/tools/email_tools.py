"""Email (Phase 11E) — draft, preview, then a CONFIRMED send.

No API/OAuth required (the user hasn't set one up):

  * Gmail  -> a pre-filled compose URL opened in the browser
              (mail.google.com/mail/?view=cm&fs=1&to=...&su=...&body=...)
  * Outlook / any desktop client -> the `mailto:` protocol, which opens the
              user's DEFAULT mail app (Outlook) with the draft pre-filled.

Composing only OPENS a draft the user can see — nothing is ever sent by
opening it. Sending is a separate `send_email`, which the safety validator
forces to high risk + the strong approval phrase, blocks when the recipient
is missing/ambiguous, and (on execute) clicks the real Send button through
the 11C control path — never a hidden programmatic send.
"""

import urllib.parse

from app.tools import Tier, ToolContext, ToolResult, tool

_EMAIL_RE = None


def _email_re():
    global _EMAIL_RE
    if _EMAIL_RE is None:
        import re
        _EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
    return _EMAIL_RE


def parse_recipients(to) -> list:
    """Extract e-mail addresses from a To string/list. Names without an
    address are NOT returned — sending needs a real address (a name alone is
    'ambiguous', which the validator refuses to send to)."""
    if isinstance(to, (list, tuple)):
        text = ", ".join(str(t) for t in to)
    else:
        text = str(to or "")
    seen, out = set(), []
    for match in _email_re().findall(text):
        low = match.lower()
        if low not in seen:
            seen.add(low)
            out.append(match)
    return out


def recipient_status(to) -> tuple:
    """(emails, status). status: 'ok' | 'missing' | 'ambiguous' | 'multiple'."""
    raw = ("" if to is None else
           (", ".join(str(t) for t in to) if isinstance(to, (list, tuple))
            else str(to))).strip()
    emails = parse_recipients(to)
    if not raw:
        return emails, "missing"
    if not emails:
        return emails, "ambiguous"        # a name with no resolvable address
    if len(emails) > 1:
        return emails, "multiple"
    return emails, "ok"


def email_preview(to, subject: str, body: str) -> dict:
    emails, status = recipient_status(to)
    return {"to": emails, "to_raw": to if isinstance(to, str) else list(to or []),
            "subject": str(subject or ""), "body": str(body or ""),
            "recipient_status": status}


def _gmail_url(emails, subject, body) -> str:
    params = {"view": "cm", "fs": "1", "to": ",".join(emails),
              "su": subject or "", "body": body or ""}
    return "https://mail.google.com/mail/?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote)


def _mailto_url(emails, subject, body) -> str:
    query = urllib.parse.urlencode({"subject": subject or "", "body": body or ""},
                                   quote_via=urllib.parse.quote)
    return f"mailto:{','.join(emails)}?{query}"


def _provider(ctx: ToolContext, args: dict) -> str:
    choice = str(args.get("provider") or getattr(ctx.config, "email_provider",
                                                 "auto") or "auto").lower()
    if choice in ("gmail", "outlook", "mailto"):
        return choice
    # auto: Gmail if a browser is the sensible default, else the desktop client
    default = (getattr(ctx.config, "default_browser", "") or "").lower()
    return "gmail" if "chrome" in default or "edge" in default or not default \
        else "outlook"


@tool("compose_email", tier=Tier.SAFE, offline_ok=True,
      description="Open a pre-filled email DRAFT (Gmail in the browser or Outlook). "
                  "Shows a preview and sends nothing.",
      schema={"to": ("string", "recipient(s)"),
              "subject": ("string", "the subject line"),
              "body": ("string", "the message body")},
      required=("to",))
def compose_email(args: dict, ctx: ToolContext) -> ToolResult:
    """Open a pre-filled draft (Gmail in the browser, or Outlook via mailto).
    Shows a preview; sends nothing."""
    to = args.get("to") or args.get("recipient") or args.get("recipients")
    subject = str(args.get("subject") or "")
    body = str(args.get("body") or args.get("text") or "")
    emails, status = recipient_status(to)
    if status == "missing":
        return ToolResult(False, "Who should I write to? I need a recipient.")

    provider = _provider(ctx, args)
    # For drafting we can open with a bare name too (the mail app resolves it
    # from contacts); sending later still requires a real address.
    addrs = emails or ([str(to).strip()] if to else [])
    preview = email_preview(to, subject, body)

    try:
        if provider == "gmail":
            url = _gmail_url(addrs, subject, body)
            import webbrowser
            webbrowser.open(url)
            where = "Gmail"
        else:
            import subprocess
            subprocess.Popen(f'start "" "{_mailto_url(addrs, subject, body)}"',
                             shell=True)
            where = "your mail app (Outlook)"
    except Exception as e:
        return ToolResult(False, f"I couldn't open a draft: "
                                 f"{' '.join(str(e).split())[:120]}")

    who = ", ".join(addrs) or "your recipient"
    note = ("" if status != "ambiguous" else
            " (I couldn't see a full email address, so double-check the To field)")
    return ToolResult(
        True,
        f"I've opened a draft to {who} in {where}{note}. Subject: "
        f"“{subject or '(none)'}”. Review it, and say “send it” when you're ready.",
        data={"preview": preview, "provider": provider})


@tool("send_email", tier=Tier.CONFIRM, offline_ok=False,
      description="Send the open draft by clicking the real Send button. Needs a "
                  "clear recipient and confirmation; the Send click is itself a "
                  "destructive target, so the strong phrase is demanded.",
      schema={"to": ("string", "recipient(s) — must resolve to a real address")},
      required=("to",))
def send_email(args: dict, ctx: ToolContext) -> ToolResult:
    """Send the open draft. The validator has already forced confirmation and
    checked the recipient; here we click the real Send button via the 11C
    control path (browser DOM for Gmail, UIA for a desktop client)."""
    emails, status = recipient_status(
        args.get("to") or args.get("recipient") or args.get("recipients"))
    if status in ("missing", "ambiguous"):
        return ToolResult(False, "I won't send without a clear recipient "
                                 "address — nothing was sent.")

    from app.tools.control_tools import _resolver
    from app.control import Scope
    resolver = _resolver(ctx)
    scope = resolver.current_scope()
    target = resolver.resolve("Send", scope, allow_vision=False)
    if target is None:
        return ToolResult(False, "I couldn't find the Send button in the open "
                                 "draft, so I didn't send anything. Is the "
                                 "compose window in front?")
    backend = resolver.backend_for(target)
    result = backend.click(target)
    if result.success:
        return ToolResult(True, f"Sent to {', '.join(emails)}.",
                          data={"to": emails})
    return ToolResult(False, result.message)

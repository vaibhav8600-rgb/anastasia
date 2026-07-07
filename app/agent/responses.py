"""Short, warm action acknowledgements with restrained affectionate wording."""

import threading


class WarmActionResponses:
    _POOL = (
        "Done — {detail}.",
        "There you go.",
        "All set.",
        "On it.",
        "Got it.",
        "All set, love.",
    )

    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()

    def next(self, plan) -> str:
        with self._lock:
            template = self._POOL[self._index % len(self._POOL)]
            self._index += 1
        return template.format(detail=self._detail(plan))

    @staticmethod
    def _detail(plan) -> str:
        args = plan.arguments or {}
        if plan.intent == "open_app":
            return f"opened {str(args.get('app_name', 'it')).title()} for you"
        if plan.intent == "open_folder":
            return f"opened your {args.get('folder', 'folder')} folder"
        if plan.intent == "browser_open":
            return "opened that for you"
        if plan.intent == "type_text":
            return "typed that for you"
        if plan.intent == "press_hotkey":
            return "handled that for you"
        if plan.intent == "window_control":
            return f"{args.get('action', 'updated')}d the window"
        return "done"


ROTATED_ACTION_INTENTS = {
    "open_app", "open_folder", "browser_open", "type_text",
    "press_hotkey", "window_control",
}

warm_action_responses = WarmActionResponses()

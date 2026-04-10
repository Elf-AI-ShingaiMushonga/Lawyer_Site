from __future__ import annotations

from flask import request
from flask_login import login_required

from ..helpers import resolve_active_matter, set_active_matter_context
from ..services.assistant_hub import (
    assistant_examples,
    assistant_matter_options,
    assistant_recent_history,
    execute_assistant_confirmation,
    process_assistant_prompt,
    record_assistant_result,
)
from ..templates import page


def register_assistant_routes(app):
    @app.route("/assistant", methods=["GET", "POST"])
    @login_required
    def assistant_home():
        active_matter = resolve_active_matter()
        selected_matter_id = request.values.get("matter_id", type=int)
        if not selected_matter_id and active_matter is not None:
            selected_matter_id = int(active_matter.id)

        prompt_value = ""
        assistant_result = None
        if request.method == "POST":
            action_mode = (request.form.get("action_mode") or "preview").strip().lower()
            prompt_value = (request.form.get("prompt") or "").strip()
            if action_mode == "confirm":
                assistant_result = execute_assistant_confirmation(
                    request.form.get("confirm_token") or "",
                    prompt=prompt_value,
                )
            else:
                assistant_result = process_assistant_prompt(prompt_value, selected_matter_id=selected_matter_id)

            if assistant_result and assistant_result.get("matter_id"):
                set_active_matter_context(assistant_result["matter_id"])
            record_assistant_result(prompt_value, assistant_result)

        return page(
            "Assistant",
            "assistant/index.html",
            assistant_result=assistant_result,
            assistant_examples=assistant_examples(),
            assistant_recent_history=assistant_recent_history(),
            assistant_matters=assistant_matter_options(),
            assistant_prompt_value=prompt_value,
            assistant_selected_matter_id=selected_matter_id,
        )

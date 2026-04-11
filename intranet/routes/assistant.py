from __future__ import annotations

from flask import abort, request, send_from_directory
from flask_login import login_required

from ..helpers import resolve_active_matter, set_active_matter_context
from ..services.assistant_agent import assistant_agent_meta
from ..services.assistant_hub import (
    assistant_examples,
    assistant_input_context_preview,
    assistant_matter_options,
    assistant_output_mode_options,
    assistant_recent_history,
    decorate_assistant_result,
    execute_assistant_confirmation,
    load_assistant_artifact,
    process_assistant_prompt,
    record_assistant_result,
)
from ..templates import page


def register_assistant_routes(app):
    @app.get("/assistant/artifacts/<token>")
    @login_required
    def assistant_artifact_download(token: str):
        artifact = load_assistant_artifact(token)
        if artifact is None:
            abort(404)
        return send_from_directory(
            app.config["UPLOAD_DIR"],
            artifact["stored_filename"],
            as_attachment=True,
            download_name=artifact["download_name"],
            mimetype=artifact["content_type"] or None,
        )

    @app.route("/assistant", methods=["GET", "POST"])
    @login_required
    def assistant_home():
        active_matter = resolve_active_matter()
        selected_matter_id = request.values.get("matter_id", type=int)
        if not selected_matter_id and active_matter is not None:
            selected_matter_id = int(active_matter.id)

        prompt_value = ""
        source_text_value = ""
        preferred_output = "interactive"
        attachment_token = ""
        assistant_result = None
        if request.method == "POST":
            action_mode = (request.form.get("action_mode") or "preview").strip().lower()
            prompt_value = (request.form.get("prompt") or "").strip()
            source_text_value = (request.form.get("source_text") or "").strip()
            preferred_output = (request.form.get("preferred_output") or "interactive").strip().lower() or "interactive"
            attachment_token = (request.form.get("attachment_token") or "").strip()
            if action_mode == "confirm":
                assistant_result = execute_assistant_confirmation(
                    request.form.get("confirm_token") or "",
                    prompt=prompt_value,
                )
            else:
                assistant_result = process_assistant_prompt(
                    prompt_value,
                    selected_matter_id=selected_matter_id,
                    pasted_text=source_text_value,
                    uploaded_file=request.files.get("source_file"),
                    attachment_token=attachment_token,
                    preferred_output=preferred_output,
                )
                attachment_token = str((assistant_result or {}).get("input_context", {}).get("attachment_token") or attachment_token)

            if assistant_result and assistant_result.get("matter_id"):
                set_active_matter_context(assistant_result["matter_id"])
            assistant_result = decorate_assistant_result(
                assistant_result,
                preferred_output=preferred_output,
                input_context=(assistant_result or {}).get("input_context")
                or assistant_input_context_preview(
                    pasted_text=source_text_value,
                    attachment_token=attachment_token,
                ),
            )
            record_assistant_result(prompt_value, assistant_result)

        return page(
            "Assistant",
            "assistant/index.html",
            assistant_result=assistant_result,
            assistant_examples=assistant_examples(),
            assistant_recent_history=assistant_recent_history(),
            assistant_matters=assistant_matter_options(),
            assistant_prompt_value=prompt_value,
            assistant_source_text_value=source_text_value,
            assistant_selected_matter_id=selected_matter_id,
            assistant_attachment_token=attachment_token,
            assistant_output_mode=preferred_output,
            assistant_output_modes=assistant_output_mode_options(),
            assistant_agent_meta=assistant_agent_meta(),
        )

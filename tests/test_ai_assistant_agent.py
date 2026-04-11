from __future__ import annotations

import json
import sys
import types

import intranet.services.assistant_agent as assistant_agent


def test_assistant_agent_defaults_to_reasoning_capable_model(app_ctx):
    app = app_ctx

    assert app.config["AI_ASSISTANT_MODEL"] == "gpt-5.2"
    assert assistant_agent.assistant_agent_meta()["model"] == "gpt-5.2"


def test_assistant_agent_uses_responses_api_for_tool_planning(app_ctx, monkeypatch):
    app = app_ctx
    app.config.update(
        AI_ENABLED=True,
        AI_PROVIDER="openai",
        AI_OPENAI_API_KEY="test-key",
        AI_ASSISTANT_AGENT_ENABLED=True,
        AI_ASSISTANT_MODEL="gpt-5.2",
        AI_ASSISTANT_REASONING_EFFORT="medium",
    )
    captured: dict[str, object] = {}

    class _FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                model="gpt-5.2",
                output=[
                    types.SimpleNamespace(
                        type="function_call",
                        name="prepare_task",
                        arguments=json.dumps({"title": "File notice of motion"}, ensure_ascii=True),
                    )
                ],
            )

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.responses = _FakeResponses()

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    result = assistant_agent.plan_assistant_request(
        prompt="Create a task to file the notice of motion.",
        matter_context={"matter_no": "2026-AST-9001", "title": "Motion Matter"},
        recent_history=[],
    )

    assert result == {
        "tool_name": "prepare_task",
        "arguments": {"title": "File notice of motion"},
        "model": "gpt-5.2",
        "reasoning_effort": "medium",
    }
    assert captured["model"] == "gpt-5.2"
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["tool_choice"] == "required"
    assert "reasoning_effort" not in captured
    assert captured["tools"]
    first_tool = captured["tools"][0]
    assert first_tool["type"] == "function"
    assert "name" in first_tool
    assert "function" not in first_tool
    assert set(first_tool["parameters"]["required"]) == set(first_tool["parameters"]["properties"].keys())


def test_assistant_response_tool_schemas_are_strict_openai_compatible(app_ctx):
    def assert_strict(schema: dict[str, object]) -> None:
        properties = schema.get("properties")
        if isinstance(properties, dict):
            assert set(schema.get("required") or []) == set(properties.keys())
            assert schema.get("additionalProperties") is False
            for property_schema in properties.values():
                if isinstance(property_schema, dict):
                    assert_strict(property_schema)
        items = schema.get("items")
        if isinstance(items, dict):
            assert_strict(items)

    tools = {tool["name"]: tool for tool in assistant_agent._assistant_response_tools()}
    assert tools
    for tool in tools.values():
        assert_strict(tool["parameters"])

    source_parameters = tools["analyze_source_material"]["parameters"]
    assert source_parameters["required"] == ["analysis_goal", "preferred_output"]
    assert source_parameters["properties"]["analysis_goal"]["type"] == ["string", "null"]
    assert None in source_parameters["properties"]["preferred_output"]["enum"]

    briefing_parameters = tools["matter_briefing"]["parameters"]
    assert briefing_parameters["required"] == []

    task_item = tools["prepare_task_bundle"]["parameters"]["properties"]["tasks"]["items"]
    assert set(task_item["required"]) == {"title", "description", "due_date", "priority"}
    assert task_item["properties"]["title"]["type"] == "string"
    assert task_item["properties"]["description"]["type"] == ["string", "null"]

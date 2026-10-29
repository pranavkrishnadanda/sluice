"""Shared helpers for Pydantic AI `FunctionModel` fakes (importable: ``tests`` is on pythonpath)."""

from pydantic_ai.messages import TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo


def output_part(info: AgentInfo, payload: str) -> TextPart | ToolCallPart:
    """The model's structured answer: the output-tool call in tool mode, else the JSON text.

    `sluice.llm.client.LLM` defaults to native structured output (no output tool), so a fake
    answers with text; one built with ``output_mode="tool"`` gets the tool call it expects.
    """
    if info.output_tools:
        return ToolCallPart(info.output_tools[0].name, payload)
    return TextPart(payload)

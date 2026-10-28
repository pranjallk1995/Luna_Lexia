"""A Pydantic AI model that talks to Luna Sentia's Ollama through its native chat API.

Pydantic AI's built-in Ollama model uses Ollama's OpenAI-compatible endpoint,
which ignores per-request options, so every request would run in the server's
default 4096-token context: too small for a multi-page Form 16. The native
``/api/chat`` endpoint accepts ``num_ctx`` and grammar-constrains the reply to
the agent's output schema through ``format``.
"""

import base64
import copy
from typing import Any

import httpx
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelResponse,
    RetryFeedbackPart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


def strict_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every property required (still nullable) for constrained decoding.

    Ollama's grammar lets a model skip optional keys entirely, so requiring
    them makes the model consider each field. Titles and defaults are dropped
    to keep the prompt short, and string formats are dropped so the model can
    copy a date as printed: small models garble dates they must reformat, while
    the schema's validators parse printed dates reliably.
    """

    def visit(node: Any) -> Any:
        if isinstance(node, dict):
            # Property and definition names are data, so only annotations are dropped.
            node = {
                key: {name: visit(child) for name, child in value.items()}
                if key in {"properties", "$defs"} and isinstance(value, dict)
                else visit(value)
                for key, value in node.items()
                if key not in {"title", "default", "format"}
            }
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"])
            return node
        if isinstance(node, list):
            return [visit(item) for item in node]
        return node

    return visit(copy.deepcopy(schema))


class OllamaChat:
    """Translate Pydantic AI messages to one native Ollama chat request and back."""

    # A reply longer than this is a model stuck repeating itself; cut it off so
    # validation fails fast and the agent retries, instead of filling the context.
    MAX_REPLY_TOKENS = 8192

    def __init__(self, base_url: str, model: str, context_tokens: int, timeout: float = 600.0) -> None:
        self.base_url = base_url
        self.model = model
        self.context_tokens = context_tokens
        self.timeout = timeout

    def as_model(self) -> FunctionModel:
        return FunctionModel(self.respond, model_name=f"ollama:{self.model}")

    @staticmethod
    def messages(history: list[ModelMessage], instructions: str | None) -> list[dict[str, Any]]:
        """Flatten requests, replies, and validation-retry prompts into chat messages."""
        chat: list[dict[str, Any]] = [{"role": "system", "content": instructions}] if instructions else []
        for message in history:
            if isinstance(message, ModelResponse):
                text = "".join(part.content for part in message.parts if isinstance(part, TextPart))
                chat.append({"role": "assistant", "content": text})
                continue
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    chat.append({"role": "system", "content": part.content})
                elif isinstance(part, UserPromptPart):
                    items = [part.content] if isinstance(part.content, str) else list(part.content)
                    entry: dict[str, Any] = {
                        "role": "user",
                        "content": "\n\n".join(item for item in items if isinstance(item, str)),
                    }
                    images = [item.data for item in items if isinstance(item, BinaryContent) and item.is_image]
                    if images:
                        entry["images"] = [base64.b64encode(image).decode("ascii") for image in images]
                    chat.append(entry)
                elif isinstance(part, (RetryPromptPart, RetryFeedbackPart)):
                    chat.append({"role": "user", "content": part.model_response()})
        return chat

    def respond(self, history: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output = info.model_request_parameters.output_object
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            # Thinking models (Qwen3-VL) otherwise reason at length before every reply.
            "think": False,
            "options": {"temperature": 0, "num_ctx": self.context_tokens, "num_predict": self.MAX_REPLY_TOKENS},
            "messages": self.messages(history, info.instructions),
        }
        if output is not None:
            payload["format"] = strict_format(output.json_schema)
        with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
            response = client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
        return ModelResponse(parts=[TextPart(response.json()["message"]["content"])])

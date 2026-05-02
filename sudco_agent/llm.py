"""OpenAI-compatible client wrapper for the local llama.cpp router.

Usage:
    client = LLMClient.from_config(cfg)
    text = client.text_generate("Write a tagline for a bakery in Iowa.")
    obj = client.json_generate("Return JSON: {tagline, services[]}", schema={...})
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from openai import OpenAI

from .config import Config

log = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, text_model: str, vision_model: str):
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=180)
        self.text_model = text_model
        self.vision_model = vision_model

    @classmethod
    def from_config(cls, cfg: Config) -> "LLMClient":
        return cls(cfg.llm_base_url, cfg.llm_api_key, cfg.text_model, cfg.vision_model)

    # ---- text -----------------------------------------------------------
    def text_generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        disable_thinking: bool = False,
    ) -> str:
        """Run a text completion. If ``disable_thinking`` is True, suppress
        Qwen 3.x's ``<think>...</think>`` reasoning trace via the ``/no_think``
        soft directive AND the ``chat_template_kwargs.enable_thinking=False``
        body parameter (whichever the serving stack honors). Useful when you
        want a short answer and don't need chain-of-thought."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        user_text = f"/no_think {prompt}" if disable_thinking else prompt
        messages.append({"role": "user", "content": user_text})
        kwargs: dict = {
            "model": self.text_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if disable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def json_generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
    ) -> Any:
        """Ask the model for strict JSON. Tries response_format first, falls
        back to extracting a JSON object from the response text if the model
        wraps it in prose or markdown."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = self.client.chat.completions.create(
                model=self.text_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
            return json.loads(text)
        except Exception as exc:
            log.warning("json_generate strict mode failed (%s); falling back to extraction", exc)
            text = self.text_generate(
                prompt=prompt + "\n\nRespond with ONLY valid JSON, no prose.",
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return _extract_json(text)

    # ---- vision ---------------------------------------------------------
    def vision_judge(
        self,
        prompt: str,
        image_url_or_b64: str,
        *,
        max_tokens: int = 1024,
        disable_thinking: bool = False,
    ) -> str:
        """Ask the VL model to judge an image. image arg is either a URL or
        a `data:image/png;base64,...` string.

        If ``disable_thinking`` is True, suppress Qwen 3.x's
        ``<think>...</think>`` reasoning trace via the ``/no_think`` soft
        directive AND the ``chat_template_kwargs.enable_thinking=False`` body
        parameter. This roughly halves token cost and lets ``max_tokens`` be
        much smaller since the model goes straight to the answer."""
        text = f"/no_think {prompt}" if disable_thinking else prompt
        kwargs: dict = {
            "model": self.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": image_url_or_b64}},
                    ],
                }
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        if disable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""


def _extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a block of text. Tolerant of
    markdown code fences and leading/trailing prose. Uses json.JSONDecoder's
    raw_decode so string contents containing { or } don't confuse the parser."""
    text = text.strip()
    if text.startswith("```"):
        # Strip leading fence (with optional "json" tag) and trailing fence.
        # Don't use .strip("`") — that eats backticks anywhere in the body too.
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()
    start_idx = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start_idx < 0:
        raise ValueError(f"No JSON found in response: {text[:200]}")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start_idx:])
        return obj
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse JSON from response: {text[:200]}") from exc

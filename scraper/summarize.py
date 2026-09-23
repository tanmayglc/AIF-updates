"""Turning a document's text into a short, written summary.

The summary is generated prose, not an extract: the tracker's whole premise is
that it links to documents rather than reproducing them, so the prompt asks for
the substance in the model's own words and the text it was given is discarded.

Providers are pluggable because free inference offerings move around - GitHub
Models, for instance, was retired in July 2026. Configure with environment
variables:

    SUMMARY_PROVIDER   gemini (default) | anthropic | openai | none
    SUMMARY_MODEL      overrides the provider's default model
    GEMINI_API_KEY     for the Gemini free tier
    ANTHROPIC_API_KEY  for Claude
    OPENAI_API_KEY     plus OPENAI_BASE_URL for anything OpenAI-compatible

With no key configured the tracker simply runs without summaries.
"""

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 90
MAX_ATTEMPTS = 3

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash-lite",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

PROMPT = """You are summarising a document for Indian fund lawyers and \
compliance officers who track SEBI Alternative Investment Fund and IFSCA fund \
management regulation.

Document title: {title}
Regulator: {regulator}
Document type: {doc_type}
Date: {date}

---
{text}
---

Write a summary of 2 to 3 sentences covering, where the document says so:
- what it actually does, changes or requires
- who it applies to
- any deadline, effective date or compliance timeline

Rules:
- Use your own words. Do not copy sentences from the document.
- Start straight with the substance. No preamble, no "This document...", no \
heading, no bullet points, no markdown.
- Be specific about numbers, dates and thresholds where they matter.
- If it is an enforcement order, say who it concerns and the outcome.
- If the text is too garbled or truncated to summarise, reply with exactly: \
UNAVAILABLE"""


class SummaryError(Exception):
    """A provider call failed in a way worth retrying or reporting."""


# --- provider implementations ----------------------------------------------

def _post(url, payload, headers):
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last = repr(exc)
        else:
            if resp.status_code == 200:
                return resp.json()
            last = "HTTP %s: %s" % (resp.status_code, resp.text[:300])
            # Rate limited or server-side: worth another go.
            if resp.status_code not in (408, 429) and resp.status_code < 500:
                break
        if attempt < MAX_ATTEMPTS:
            time.sleep(5 * attempt)
    raise SummaryError(last or "unknown error")


def _gemini(prompt, model, key):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent" % model)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
    }
    data = _post(url, payload, {"x-goog-api-key": key,
                                "Content-Type": "application/json"})
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise SummaryError("unexpected Gemini response: %s"
                           % json.dumps(data)[:300])


def _anthropic(prompt, model, key):
    payload = {
        "model": model,
        "max_tokens": 400,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _post("https://api.anthropic.com/v1/messages", payload, {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    })
    try:
        return "".join(b.get("text", "") for b in data["content"])
    except (KeyError, TypeError):
        raise SummaryError("unexpected Anthropic response: %s"
                           % json.dumps(data)[:300])


def _openai(prompt, model, key):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    payload = {
        "model": model,
        "max_tokens": 400,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _post(base.rstrip("/") + "/chat/completions", payload, {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SummaryError("unexpected OpenAI response: %s"
                           % json.dumps(data)[:300])


_PROVIDERS = {"gemini": _gemini, "anthropic": _anthropic, "openai": _openai}
_KEY_VARS = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
             "openai": "OPENAI_API_KEY"}


# --- public API -------------------------------------------------------------

class Summariser:
    """Holds provider config; ``.available`` is False when no key is set."""

    def __init__(self):
        self.provider = (os.environ.get("SUMMARY_PROVIDER") or "gemini").lower()
        self.model = os.environ.get("SUMMARY_MODEL") or \
            DEFAULT_MODELS.get(self.provider, "")
        key_var = _KEY_VARS.get(self.provider)
        self.key = os.environ.get(key_var, "").strip() if key_var else ""
        self.calls = 0
        self.failures = 0
        self.last_error = None

    @property
    def available(self):
        return bool(self.provider in _PROVIDERS and self.key and self.model)

    def describe(self):
        if self.provider == "none":
            return "disabled (SUMMARY_PROVIDER=none)"
        if self.provider not in _PROVIDERS:
            return "unknown provider %r" % self.provider
        if not self.key:
            return "no %s set" % _KEY_VARS[self.provider]
        return "%s / %s" % (self.provider, self.model)

    def summarise(self, item, text):
        """Return a summary string, or "" when one could not be produced."""
        if not self.available or not text or len(text) < 200:
            return ""

        prompt = PROMPT.format(
            title=item.get("title", ""),
            regulator=item.get("regulator", ""),
            doc_type=item.get("doc_type", ""),
            date=item.get("date") or "unknown",
            text=text,
        )
        try:
            self.calls += 1
            raw = _PROVIDERS[self.provider](prompt, self.model, self.key)
        except SummaryError as exc:
            self.failures += 1
            self.last_error = str(exc)
            log.warning("summary failed for %s: %s", item.get("id"), exc)
            return ""

        return _tidy(raw)


def _tidy(raw):
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", raw).strip()
    if text.upper().startswith("UNAVAILABLE"):
        return ""
    # Strip any stray markdown the model adds despite the instruction.
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^(summary|abstract)\s*[:\-]\s*", "", text, flags=re.I)
    return text.strip()

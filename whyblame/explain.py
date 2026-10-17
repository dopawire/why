"""Turn a Story into a short, cited explanation using a language model."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .story import Story

CLAUDE_MODEL = "claude-opus-5-5"
OPENROUTER_MODEL = "anthropic/claude-opus-5.5"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

SYSTEM = """\
You explain why a piece of code is the way it is, for a developer who is about to change it.

You are given the current file around the lines in question (those lines are marked with >), \
and numbered sources: commits that touched these exact lines (with range-scoped diffs), and the \
pull requests, reviews and issues linked to them.

Rules:
- Every factual claim cites its source like [3]. Never cite a source for something it doesn't say.
- Facts read from the current file (comments, definitions, what a name holds) cite [code] with \
the line number, like [code L4].
- Lead with the answer: one or two sentences on why this code exists in its current form.
- Then the short story of how it got here, oldest to newest, only the turns that matter.
- If the sources don't record a reason, say so plainly ("No rationale was recorded; [2] only \
says 'cleanup'"). That's a useful answer. Label any guess of your own as "Inference:".
- End with "Before you change it:" and any constraint, bug or edge case the sources warn \
about, or "Nothing recorded." if there is none.
- Plain text, under 250 words. No preamble."""


class BackendError(RuntimeError):
    pass


def render_prompt(story: Story) -> str:
    shown = story.context or [(story.start + i, line) for i, line in enumerate(story.code)]
    rows, prev = [], None
    for n, line in shown:
        if prev is not None and n != prev + 1:
            rows.append("  ...")
        mark = ">" if story.start <= n <= story.end else " "
        rows.append(f"{mark}{n:>5}  {line}")
        prev = n
    code = "\n".join(rows)
    parts = [f"File: {story.path}, asking about lines {story.start}-{story.end}\n\n{code}\n\nSources:"]
    for s in story.sources:
        parts.append(
            f'<source id="{s.id}" kind="{s.kind}" author="{s.author}" date="{s.date}">\n'
            f"{s.title}\n{s.text}\n</source>"
        )
    return "\n\n".join(parts)


def _claude_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _ollama_available() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=1).close()
        return True
    except (urllib.error.URLError, OSError):
        return False


def pick_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    if _claude_available():
        return "claude"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if _ollama_available():
        return "ollama"
    return "none"


def explain_claude(story: Story, model: str | None) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise BackendError("the Claude backend needs: pip install 'why-blame[claude]'") from e

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=model or CLAUDE_MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": render_prompt(story)}],
        )
    except anthropic.AuthenticationError as e:
        raise BackendError("Claude rejected the credentials; check ANTHROPIC_API_KEY") from e
    except anthropic.APIStatusError as e:
        raise BackendError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise BackendError("could not reach the Claude API") from e

    if response.stop_reason == "refusal":
        raise BackendError("the model declined to summarize this history")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def explain_openrouter(story: Story, model: str | None) -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise BackendError("the OpenRouter backend needs OPENROUTER_API_KEY")
    body = json.dumps(
        {
            "model": model or os.environ.get("WHY_OPENROUTER_MODEL") or OPENROUTER_MODEL,
            # Without a cap OpenRouter reserves the model's full output limit against the
            # balance, so small accounts get a 402. The answer is ~250 words; this leaves
            # room for reasoning tokens on thinking models.
            "max_tokens": 8000,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": render_prompt(story)},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "X-Title": "why",  # shows up as the app name in OpenRouter's dashboard
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = e.reason
        raise BackendError(f"OpenRouter error {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise BackendError(f"could not reach OpenRouter: {e.reason}") from e
    # OpenRouter can return 200 with an error object when the upstream provider fails.
    if "error" in data:
        raise BackendError(f"OpenRouter error: {data['error'].get('message', data['error'])}")
    return (data["choices"][0]["message"]["content"] or "").strip()


def explain_ollama(story: Story, model: str | None) -> str:
    model = model or os.environ.get("WHY_OLLAMA_MODEL")
    if not model:
        raise BackendError("pick a local model with --model or WHY_OLLAMA_MODEL (e.g. qwen3)")
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": render_prompt(story)},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.load(resp)["message"]["content"].strip()
    except urllib.error.URLError as e:
        raise BackendError(f"could not reach Ollama at {OLLAMA_HOST}: {e.reason}") from e


def explain(story: Story, backend: str, model: str | None = None) -> str | None:
    if backend == "none" or not story.sources:
        return None
    if backend == "claude":
        return explain_claude(story, model)
    if backend == "openrouter":
        return explain_openrouter(story, model)
    if backend == "ollama":
        return explain_ollama(story, model)
    raise BackendError(f"unknown backend: {backend}")

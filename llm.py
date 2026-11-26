"""One call shape for every AI provider: run_structured(system, content, schema) -> (data, usage).

`content` is a list of Anthropic-style blocks ({"type": "text"} / {"type": "image", "source": {base64}}),
converted here for OpenAI and Gemini. The answer always comes back as JSON matching `schema`.
"""

import base64
import copy
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import config


def _key(provider: str) -> str:
    s = config.load_settings()
    env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}[provider]
    key = (s.get("api_keys") or {}).get(provider) or os.environ.get(env)
    if not key:
        raise RuntimeError(f"authentication_error: no {provider} API key. Add it on the Setup page.")
    return key


def current() -> tuple[str, str]:
    s = config.load_settings()
    provider = s.get("provider") or "anthropic"
    return provider, s.get("model") or config.DEFAULT_MODELS.get(provider, "")


def strict_schema(schema: dict) -> dict:
    """Structured-output rules: every object closed and every property required."""
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    return s


# ---------------------------------------------------------------- Anthropic API

def _anthropic(system, content, schema, model):
    import anthropic
    client = anthropic.Anthropic(api_key=_key("anthropic"))
    s = config.load_settings()
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": s.get("effort") or "medium",
                       "format": {"type": "json_schema", "schema": strict_schema(schema)}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    text = "".join(b.text for b in msg.content if b.type == "text")
    u = msg.usage
    usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
             "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
             "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
    return json.loads(text), usage


# ---------------------------------------------------------------- OpenAI

def _openai(system, content, schema, model):
    from openai import OpenAI
    client = OpenAI(api_key=_key("openai"))
    parts = []
    for b in content:
        if b["type"] == "text":
            parts.append({"type": "text", "text": b["text"]})
        else:
            src = b["source"]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": parts}],
        response_format={"type": "json_schema", "json_schema": {"name": "answer", "strict": True, "schema": strict_schema(schema)}},
    )
    usage = {"input_tokens": r.usage.prompt_tokens, "output_tokens": r.usage.completion_tokens} if r.usage else {}
    return json.loads(r.choices[0].message.content), usage


# ---------------------------------------------------------------- Gemini

def _gemini(system, content, schema, model):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=_key("gemini"))
    parts = []
    for b in content:
        if b["type"] == "text":
            parts.append(types.Part(text=b["text"]))
        else:
            parts.append(types.Part(inline_data=types.Blob(mime_type=b["source"]["media_type"],
                                                           data=base64.b64decode(b["source"]["data"]))))
    decl = types.FunctionDeclaration(name="submit", description="Submit the answer.", parameters=schema)
    r = client.models.generate_content(
        model=model, contents=types.Content(role="user", parts=parts),
        config=types.GenerateContentConfig(
            system_instruction=system, temperature=0.4, max_output_tokens=8000,
            tools=[types.Tool(function_declarations=[decl])],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
        ),
    )
    usage = {}
    if r.usage_metadata:
        usage = {"input_tokens": r.usage_metadata.prompt_token_count or 0,
                 "output_tokens": r.usage_metadata.candidates_token_count or 0}
    for p in (r.candidates[0].content.parts if r.candidates and r.candidates[0].content else []):
        if p.function_call:
            return json.loads(json.dumps(dict(p.function_call.args), default=list)), usage
    raise RuntimeError("Gemini returned no answer")


# ---------------------------------------------------------------- Claude Code CLI (subscription, no key)

def _claude_code(system, content, schema, model):
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("claude CLI not found. Install Claude Code or pick another provider.")
    prompt_file = Path(tempfile.gettempdir()) / f"ha_prompt_{os.getpid()}_{threading.get_ident()}.txt"
    prompt_file.write_text(system, encoding="utf-8")
    cmd = [path, "-p", "--model", model, "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--json-schema", json.dumps(schema), "--system-prompt-file", str(prompt_file),
           "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    try:
        with tempfile.TemporaryDirectory(prefix="ha_judge_") as cwd:
            proc = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True,
                                  encoding="utf-8", cwd=cwd, timeout=config.CLAUDE_CLI_TIMEOUT_S)
    finally:
        prompt_file.unlink(missing_ok=True)
    result = None
    for line in proc.stdout.splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "result":
            result = evt
    if result is None:
        raise RuntimeError(f"claude CLI returned nothing: {(proc.stderr or proc.stdout)[-400:]}")
    if result.get("is_error"):
        m = str(result.get("result", ""))
        if "login" in m.lower() or "authenticate" in m.lower():
            m = f"authentication_error: {m} (run `claude` then /login)"
        raise RuntimeError(f"claude CLI error: {m}")
    data = result.get("structured_output")
    if data is None:
        data = json.loads(result.get("result", "") or "{}")
    u = result.get("usage") or {}
    return data, {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
                  "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0) or 0,
                  "cache_read_input_tokens": u.get("cache_read_input_tokens", 0) or 0,
                  "cli_cost_usd": result.get("total_cost_usd", 0) or 0}


PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini, "claude_code": _claude_code}


def run_structured(system: str, content: list[dict], schema: dict) -> tuple[dict, dict]:
    provider, model = current()
    fn = PROVIDERS.get(provider)
    if fn is None:
        raise RuntimeError(f"Unknown provider {provider!r}")
    data, usage = fn(system, content, schema, model)
    usage["model"] = model
    return data, usage


def test_key(provider: str, key: str, model: str | None = None) -> str:
    """A tiny call to check a key works. Returns the model's answer or raises."""
    model = model or config.DEFAULT_MODELS[provider]
    if provider == "anthropic":
        import anthropic
        r = anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=200, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word: ready"}])
        return "".join(b.text for b in r.content if b.type == "text").strip()
    if provider == "openai":
        from openai import OpenAI
        r = OpenAI(api_key=key).chat.completions.create(model=model, messages=[{"role": "user", "content": "Reply with the single word: ready"}])
        return r.choices[0].message.content.strip()
    if provider == "gemini":
        from google import genai
        return genai.Client(api_key=key).models.generate_content(model=model, contents="Reply with the single word: ready").text.strip()
    if provider == "claude_code":
        if not shutil.which("claude"):
            raise RuntimeError("claude CLI not found on PATH")
        return "claude CLI found"
    raise RuntimeError("unknown provider")

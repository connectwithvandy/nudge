"""ai.py — the brain, on free tiers only.

In production that is Groq's free cloud tier (chat, vision, and Whisper for voice notes);
with no GROQ_API_KEY it falls back to a local Ollama, which is the offline development setup.

Everything here is written around one fact: a free tier refuses requests routinely, and the
difference between a refusal and a real bug has to be visible in the logs, or debugging is
guesswork.
"""
import base64
import re
import time

import httpx

from . import config

# Models Groq has deleted from the free tier — they do this without notice, and a stale env
# var pointing at one used to break a whole feature silently. Anything listed here is swapped
# for a current model at startup instead of failing on first use.
GONE_MODELS = {
    "llama-3.3-70b-versatile", "meta-llama/llama-4-scout-17b-16e-instruct",
    "mixtral-8x7b-32768", "llama-3.1-70b-versatile", "llama-3.1-8b-instant",
    "llama3-70b-8192", "llama3-8b-8192", "gemma2-9b-it",
}
CHAT_DEFAULT = "openai/gpt-oss-120b"
FALLBACK_DEFAULT = "openai/gpt-oss-20b"
# The intent router must answer in about five tokens, so it uses the model we can tell not to
# reason at all. A reasoning model spends the whole budget thinking and returns "".
ROUTER_DEFAULT = "qwen/qwen3.6-27b"
VISION_DEFAULT = "qwen/qwen3.6-27b"


def post_with_retry(url, *, attempts=2, wait_on_limit=False, **kwargs):
    """POST to Groq, retrying a transient 5xx or timeout.

    By default a rate limit raises IMMEDIATELY so the caller can fall back to a lighter model.
    With wait_on_limit=True (used for vision, which has no fallback model) it waits and
    retries instead, so a burst of images that briefly rate-limits still succeeds.

    Groq expresses a rate limit as EITHER 429 or 413. A 413 from api.groq.com almost never
    means the prompt is too big — a full coaching prompt is only ~7k tokens — it means this
    one request would exceed what is left of the per-minute token allowance. Treating 413 as
    fatal is how messages get lost in under a second while the backup model sits unused.
    """
    r = None
    for i in range(attempts):
        last = i == attempts - 1
        try:
            r = httpx.post(url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError):
            if last:
                raise
            time.sleep(1)
            continue
        if r.status_code in (429, 413) and wait_on_limit and not last:
            ra = r.headers.get("retry-after")
            try:
                wait = min(float(ra), 15) if ra else 3 * (i + 1)
            except ValueError:
                wait = 3 * (i + 1)
            time.sleep(wait)
            continue
        if r.status_code >= 500 and not last:
            time.sleep(1)
            continue
        r.raise_for_status()
        return r
    if r is not None:
        r.raise_for_status()
    return r


class GroqClient:
    """A stand-in for the Ollama client that talks to Groq's free cloud brain.

    It accepts the same .chat(model=..., messages=[...], options=...) calls the rest of the
    app makes (including Ollama-style {'images': [bytes]} messages) and returns the same
    {'message': {'content': ...}} shape, so no call site has to know which brain it got.
    """

    BASE = "https://api.groq.com/openai/v1/chat/completions"
    # Headroom for models that reason before answering. max_tokens is only a ceiling, so
    # unused tokens cost nothing — but too tight a budget returns an empty string.
    REASONING_RESERVE = config.REASONING_RESERVE

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self.chat_model = config.GROQ_CHAT_MODEL
        if self.chat_model in GONE_MODELS:
            self.chat_model = CHAT_DEFAULT
        self.chat_fallback = config.GROQ_CHAT_FALLBACK
        if self.chat_fallback in GONE_MODELS or self.chat_fallback == self.chat_model:
            self.chat_fallback = FALLBACK_DEFAULT
        self.vision_model = config.GROQ_VISION_MODEL
        if self.vision_model in GONE_MODELS:
            self.vision_model = VISION_DEFAULT
        # {model: "YYYY-MM-DD"} — a model that hit its DAILY budget is skipped for the rest of
        # that day rather than wasting a doomed call plus its latency on every message.
        self._out_today = {}
        # Models Groq answered 404 for. Kept SEPARATE from _out_today rather than marked with a
        # far-future date: the daily check is "is this marker today?", so a sentinel date is not
        # equal to today and the supposedly-retired model gets retried on every single message.
        self._gone = set()

    @staticmethod
    def reasoning_effort(model: str):
        """Steer per-model: qwen answers directly with "none"; gpt-oss takes "low". Models
        that accept neither must not be sent the parameter at all."""
        m = (model or "").lower()
        if "qwen" in m:
            return "none"
        if "gpt-oss" in m:
            return "low"
        return None

    def chat(self, model=None, messages=None, options=None, keep_alive=None):
        from datetime import date as _date
        options = options or {}
        out_messages, has_image = [], False
        for m in messages or []:
            if m.get("images"):  # Ollama passes raw bytes; Groq wants base64 data URLs
                has_image = True
                content = [{"type": "text", "text": m.get("content", "")}]
                for img in m["images"]:
                    b64 = base64.b64encode(img).decode()
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
                out_messages.append({"role": m["role"], "content": content})
            else:
                out_messages.append({"role": m["role"], "content": m.get("content", "")})
        max_tokens = max(int(options["num_predict"]), 1) if options.get("num_predict") else None

        is_vision = has_image or model == config.OLLAMA_VISION_MODEL
        if is_vision:
            models = [self.vision_model]
        else:
            models = [self.chat_model]
            if self.chat_fallback and self.chat_fallback != self.chat_model:
                models.append(self.chat_fallback)
        today = _date.today().isoformat()
        usable = [m for m in models
                  if m not in self._gone and self._out_today.get(m) != today] or models[-1:]

        last_exc = None
        for i, gm in enumerate(usable):
            payload = {"model": gm, "messages": out_messages}
            if max_tokens:
                payload["max_tokens"] = max_tokens
            eff = self.reasoning_effort(gm)
            if eff:
                payload["reasoning_effort"] = eff
                # A reasoning model spends max_tokens THINKING before writing a word, so a
                # tight budget can be consumed entirely by the reasoning and return "" — no
                # error, no clue, just nothing. Reserve headroom.
                if max_tokens:
                    payload["max_tokens"] = max_tokens + self.REASONING_RESERVE
            try:
                r = post_with_retry(
                    self.BASE, headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload, timeout=90 if has_image else 20,
                    # Vision has no fallback model, so wait out a burst rate-limit.
                    attempts=4 if is_vision else 2, wait_on_limit=is_vision)
                content = r.json()["choices"][0]["message"]["content"]
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
                if not content:
                    # Never hand an empty string back as if it were an answer — callers turn
                    # that into a meal with no items or a message with no words.
                    print(f"[brain] {gm} returned an EMPTY reply "
                          f"(budget {payload.get('max_tokens')}) — trying the next model")
                    last_exc = RuntimeError(f"{gm} returned an empty reply")
                    if i < len(usable) - 1:
                        continue
                    raise last_exc
                return {"message": {"content": content}}
            except httpx.HTTPStatusError as e:
                last_exc = e
                code = e.response.status_code if e.response is not None else 0
                body = (e.response.text or "") if e.response is not None else ""
                # 429/413 = rate or budget limited; 404 = the model was deleted. All three
                # mean "try the next model", never "give up".
                if code not in (429, 404, 413):
                    raise
                # ALWAYS log what Groq actually said. The status code alone sends you hunting
                # for an oversized prompt when the real message is a per-minute rate limit.
                print(f"[brain] {gm} returned {code}: {body[:200]}")
                if code == 404:
                    self._gone.add(gm)  # removed by Groq; never ask for it again this run
                    print(f"[brain] {gm} was removed by Groq — falling back")
                elif "per day" in body.lower():
                    self._out_today[gm] = today
                    print(f"[brain] {gm} out of daily budget — skipping it until tomorrow")
                if i < len(usable) - 1:
                    continue
                raise
        raise last_exc


if config.GROQ_API_KEY:
    client = GroqClient(config.GROQ_API_KEY)
    CHAT_MODEL = client.chat_model
    VISION_MODEL = client.vision_model
    print(f"[brain] Groq cloud: chat={client.chat_model}, vision={client.vision_model}")
else:
    from ollama import Client as _Ollama
    client = _Ollama(host=config.OLLAMA_HOST) if config.OLLAMA_HOST else _Ollama()
    CHAT_MODEL = config.OLLAMA_MODEL
    VISION_MODEL = config.OLLAMA_VISION_MODEL
    print("[brain] local Ollama (no GROQ_API_KEY set)")


def busy_brain(e: Exception) -> bool:
    """True when the failure is the free tier momentarily out of room, not a real bug.
    The user gets different words for this, because it clears on its own in seconds and
    saying so is the difference between them re-sending and assuming the app is broken."""
    r = getattr(e, "response", None)
    return getattr(r, "status_code", None) in (429, 413)


def transcribe(audio: bytes, content_type: str | None = None) -> str:
    """Voice note -> text via Groq Whisper. Auto-detects the language, so a user texting in
    any language (or mixing two) comes through fine.

    Two models are tried. Unlike chat there is no graceful degradation for audio: a single
    rate-limited second throws away everything the person said, and the fallback model has
    its own separate budget.
    """
    if not config.GROQ_API_KEY:
        raise RuntimeError("voice notes need the Groq cloud brain (GROQ_API_KEY)")
    ext = "ogg"
    if content_type and "/" in content_type:
        ext = content_type.split("/", 1)[1].split(";", 1)[0] or "ogg"
    models = [m for m in (config.GROQ_STT_MODEL, config.GROQ_STT_FALLBACK) if m]
    last = None
    for i, model in enumerate(models):
        try:
            r = post_with_retry(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                data={"model": model, "temperature": "0"},
                files={"file": (f"note.{ext}", audio, content_type or "audio/ogg")},
                timeout=90)
            return (r.json().get("text") or "").strip()
        except httpx.HTTPStatusError as e:
            last = e
            code = e.response.status_code if e.response is not None else 0
            body = (e.response.text if e.response is not None else "")[:200]
            print(f"[voice] {model} transcription failed: HTTP {code} {body}")
            if i < len(models) - 1:
                if code in (429, 413):
                    time.sleep(2)  # per-minute limits clear in seconds
                continue
            raise
        except Exception as e:
            last = e
            print(f"[voice] {model} transcription failed: {e!r}")
            if i < len(models) - 1:
                continue
            raise
    raise last


def route_intent(text: str, recent_items: str | None) -> str | None:
    """Classify a message as MEAL / FIX / ASK / CHAT, or None when the router is unavailable
    (the caller then falls back to keywords, so routing can never be worse than no router).

    The label descriptions are lowercase on purpose. With shouty words in the prompt a small
    router answers with one of THOSE instead of a label, and a clean classification is thrown
    away. Nothing but the four labels may appear in capitals anywhere in this prompt.
    """
    fix_line = (f"FIX - correcting or adjusting the meal just logged ({recent_items}) — "
                "portions, items, 'x not y', 'add oil'\n" if recent_items else "")
    prompt = (
        "Classify one chat message from someone texting their fitness coach. "
        "Answer with exactly one of these four labels and nothing else:\n"
        "MEAL - they are reporting food or drink they actually consumed (now or earlier "
        "today). Not plans ('thinking pasta for lunch'), cravings, or questions.\n"
        + fix_line +
        "ASK - a question about their day, data, body, food or nutrition advice\n"
        "CHAT - anything else: greetings, feelings, life talk, plans, jokes\n\n"
        f"Message: \"{text.strip()}\"\n\nLabel:")
    try:
        if isinstance(client, GroqClient) and config.GROQ_ROUTER_MODEL:
            model = config.GROQ_ROUTER_MODEL
            if model in GONE_MODELS:
                model = ROUTER_DEFAULT
            payload = {"model": model, "max_tokens": 5,
                       "messages": [{"role": "user", "content": prompt}]}
            eff = GroqClient.reasoning_effort(model)
            if eff:
                payload["reasoning_effort"] = eff
                if eff != "none":
                    payload["max_tokens"] = 5 + GroqClient.REASONING_RESERVE
            r = post_with_retry(GroqClient.BASE, attempts=1,  # the fallback chain is right there
                                headers={"Authorization": f"Bearer {client.api_key}"},
                                json=payload, timeout=8)
            out = r.json()["choices"][0]["message"]["content"]
        else:
            out = client.chat(model=CHAT_MODEL, messages=[{"role": "user", "content": prompt}],
                              options={"num_predict": 5})["message"]["content"]
        m = re.search(r"\b(MEAL|FIX|ASK|CHAT)\b", (out or "").upper())
        intent = m.group(1) if m else None
        if intent is None:
            print(f"[router] no label in {out!r} — using keyword fallback")
        if intent == "FIX" and not recent_items:
            intent = None  # nothing to fix — don't trust a label we never offered
        return intent
    except Exception as e:
        print(f"[router] unavailable, using keyword fallback: {e!r}")
        return None

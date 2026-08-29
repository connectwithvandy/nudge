"""The brain's free-tier behaviour, and the rule that a failed estimate never becomes a number.

Every Groq call is faked. These are the failures that cost real messages: a 413 treated as
fatal, an empty reply from a reasoning model, a model Groq deleted, and a nutrition block that
never arrived being logged as 0 kcal.
"""
import httpx

import _safe

c = _safe.Check()
db = _safe.load("db")
db.connect().close()
ai = _safe.load("ai")
nutrition = _safe.load("nutrition")


class FakeResponse:
    def __init__(self, status, body="", content=""):
        self.status_code = status
        self.text = body
        self._content = content
        self.headers = {}

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


def fake_groq(script):
    """Replace the HTTP call with a scripted sequence, recording which model was asked."""
    asked = []

    def post(url, **kw):
        asked.append(kw["json"]["model"])
        return script.pop(0)
    ai.httpx.post = post
    return asked


client = ai.GroqClient("test-key")
client.chat_model = "primary"
client.chat_fallback = "backup"

# ---- 413 means "rate limited", not "your prompt is too big" ---------------------------------
# Treating it as fatal is what loses a message in under a second while the backup model sits
# unused — and the status code alone points you at the wrong bug entirely.
asked = fake_groq([FakeResponse(413, "Request too large for model on tokens per minute"),
                   FakeResponse(200, content="hello")])
out = client.chat(messages=[{"role": "user", "content": "hi"}])
c("a 413 falls through to the backup model", out["message"]["content"], "hello")
c("both models were tried, in order", asked, ["primary", "backup"])

# ---- 429 does the same -----------------------------------------------------------------------
asked = fake_groq([FakeResponse(429, "rate limit"), FakeResponse(200, content="hi there")])
c("a 429 falls through too",
  client.chat(messages=[{"role": "user", "content": "x"}])["message"]["content"], "hi there")

# ---- an empty reply is never handed back as an answer -------------------------------------------
# A reasoning model can spend its whole budget thinking and return "". Passing that on becomes a
# meal with no items, or a message with no words.
asked = fake_groq([FakeResponse(200, content="   "), FakeResponse(200, content="real answer")])
c("an empty reply tries the next model",
  client.chat(messages=[{"role": "user", "content": "x"}])["message"]["content"], "real answer")

fake_groq([FakeResponse(200, content=""), FakeResponse(200, content="")])
try:
    client.chat(messages=[{"role": "user", "content": "x"}])
    c("two empty replies raise rather than return nothing", False, True)
except Exception:
    c("two empty replies raise rather than return nothing", True, True)

# ---- a model Groq has deleted is skipped, not retried ---------------------------------------------
fresh = ai.GroqClient("test-key")
fresh.chat_model, fresh.chat_fallback = "deleted", "backup"
asked = fake_groq([FakeResponse(404, "model not found"), FakeResponse(200, content="ok")])
fresh.chat(messages=[{"role": "user", "content": "x"}])
asked2 = fake_groq([FakeResponse(200, content="ok again")])
fresh.chat(messages=[{"role": "user", "content": "x"}])
c("a deleted model is never asked again this run", asked2, ["backup"])

# ---- a daily budget exhaustion skips that model for the rest of the day ------------------------------
day = ai.GroqClient("test-key")
day.chat_model, day.chat_fallback = "spent", "backup"
fake_groq([FakeResponse(429, "Rate limit reached ... tokens per day"),
           FakeResponse(200, content="ok")])
day.chat(messages=[{"role": "user", "content": "x"}])
asked3 = fake_groq([FakeResponse(200, content="ok")])
day.chat(messages=[{"role": "user", "content": "x"}])
c("a model out of daily budget is skipped, not re-tried", asked3, ["backup"])

# ---- a real error is a real error ---------------------------------------------------------------------
fake_groq([FakeResponse(500, "server error"), FakeResponse(500, "server error")])
try:
    client.chat(messages=[{"role": "user", "content": "x"}])
    c("a 500 eventually raises", False, True)
except Exception:
    c("a 500 eventually raises", True, True)

# ---- a stale env var pointing at a retired model is corrected at startup --------------------------------
c("a retired model is swapped for a current one",
  "llama-3.1-8b-instant" in ai.GONE_MODELS, True)

# ---- reasoning effort is steered per model, never sent to one that rejects it -----------------------------
c("qwen is told not to reason at all", ai.GroqClient.reasoning_effort("qwen/qwen3.6-27b"), "none")
c("gpt-oss reasons lightly", ai.GroqClient.reasoning_effort("openai/gpt-oss-120b"), "low")
c("anything else is sent nothing", ai.GroqClient.reasoning_effort("some-other-model"), None)

# ---- a free-tier refusal is distinguishable from a crash --------------------------------------------------
c("413 reads as busy, not broken",
  ai.busy_brain(httpx.HTTPStatusError("x", request=None, response=FakeResponse(413))), True)
c("429 reads as busy", ai.busy_brain(
    httpx.HTTPStatusError("x", request=None, response=FakeResponse(429))), True)
c("500 does not read as busy", ai.busy_brain(
    httpx.HTTPStatusError("x", request=None, response=FakeResponse(500))), False)
c("a plain exception does not read as busy", ai.busy_brain(ValueError("nope")), False)

# ---- a failed estimate must NEVER become a number ------------------------------------------------------------
# A 0-kcal meal is invisible in the moment, then corrupts that day's totals and every weekly
# average after it. No answer is always better than a fabricated zero.
c("a reply with no calories line returns nothing",
  nutrition.parse_nutrients("I can see a plate of food but I'm not sure of the portions"), None)
c("a zero-calorie reply returns nothing",
  nutrition.parse_nutrients("ITEMS: rice\nCALORIES: 0\nPROTEIN: 0"), None)
c("a truncated reply returns nothing",
  nutrition.parse_nutrients("ITEMS: dal, rice, salad, yoghurt and a"), None)
c("an empty reply returns nothing", nutrition.parse_nutrients(""), None)

good = nutrition.parse_nutrients(
    "ITEMS: 2 rotis, dal\nCALORIES: 420\nPROTEIN: 16\nCARBS: 60\nFAT: 11\nFIBER: 7\n"
    "SUGAR: 3\nSODIUM: 480\nIRON: 3\nCALCIUM: 90")
c("a good reply parses the items", good[0], "2 rotis, dal")
c("and the calories", good[1]["calories"], 420)
c("and the micros", good[1]["sodium"], 480)

# The model often thinks out loud first; the block still has to be found.
chatty = nutrition.parse_nutrients(
    "Let me look at this. I see rice and some curry. Roughly a cup of rice...\n"
    "ITEMS: rice, curry\nCALORIES: 500\nPROTEIN: 12")
c("a chatty reply still parses", chatty[1]["calories"], 500)
c("and missing nutrients default to 0 rather than breaking", chatty[1]["iron"], 0)

raise SystemExit(0 if c.done("brain: ") else 1)

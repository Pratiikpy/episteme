"""MiniMax M3 LLM node — document.extract_json (structured extraction).

Uses the OpenAI-compatible client pointed at the configured router (MiniMax M3
via 0g.ai). Key is read by name from settings (never hard-coded). Enforces
structured JSON output; degrades gracefully to ENGINE_UNAVAILABLE without a key.
"""
from __future__ import annotations

import json

from config import get_settings
from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

# Router replies that mean "this MODEL cannot serve" rather than "this REQUEST was bad". Only these
# advance the chain; a genuine 400 from a malformed request must surface, not silently downgrade the
# model on every call.
_MODEL_UNAVAILABLE = ("balance_insufficient", "insufficient", "model_not_found", "no provider",
                      "unavailable", "unsupported model", "quota", "not available on the", "api format")


def _chat(client, messages: list[dict], *, skip: tuple[str, ...] = (), **kw) -> tuple[str, str]:
    """Call the router, walking the configured model chain when a model itself is unusable.

    Returns (content, model_that_answered) so a receipt can name the model that ACTUALLY ran rather
    than the one that was configured — an LLM-backed result attributed to a model that never executed
    would be a false provenance claim on a signed artifact.

    `skip` excludes models already known to have answered badly. The chain previously restarted from
    the top on every call and advanced only when a model *threw*, so a model that returned prose
    where JSON was asked for was re-asked the identical question and answered the same way. Measured:
    retrying without this left one call in five still failing. Moving on to the next model is what
    makes the second attempt different from the first.

    Raises NodeError(ENGINE_FAILED) only when every model in the chain has been tried.
    """
    s = get_settings()
    last = ""
    for model in s.llm_model_chain:
        if model in skip:
            continue
        try:
            resp = client.chat.completions.create(model=model, messages=messages, **kw)
            return (resp.choices[0].message.content or ""), model
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            if not any(t in last.lower() for t in _MODEL_UNAVAILABLE):
                raise  # a real request error — the next model would reject it identically
    raise NodeError(ErrorCode.ENGINE_FAILED,
                    f"every model in the chain failed ({', '.join(s.llm_model_chain)}): {last[:200]}")


class DocumentExtractJsonNode(Node):
    name = "document.extract_json"
    price_usdt = 0.05
    deterministic = False  # LLM output; capped at L2 (schema-validated)
    asp_type = "A2MCP"
    engine = "minimax-m3"

    def __init__(self):
        self._settings = get_settings()
        self.engine_version = self._settings.llm_model

    def engine_available(self) -> bool:
        try:
            import openai  # noqa
        except Exception:
            return False
        return self._settings.llm_configured

    def run(self, ctx: NodeContext) -> dict:
        text = str(ctx.input.get("text", "")).strip()
        schema = ctx.input.get("schema") or ctx.input.get("fields")
        if not text:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'text'")
        if not schema:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'schema' (JSON schema or field list)")
        if len(text) > 200_000:
            raise NodeError(ErrorCode.LIMIT_EXCEEDED, "text too large (>200k chars)")

        s = self._settings
        try:
            from openai import OpenAI
        except Exception:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE, "openai client not installed")
        if not s.llm_configured:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE, "LLM API key not configured")

        client = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=60)
        sys = ("You are a strict data extractor. Return ONLY a single minified JSON object that conforms "
               "to the requested schema/fields. Do NOT include <think> tags, reasoning, explanation, "
               "prose, or markdown/code fences. Your entire response MUST start with { and end with }.")
        user = f"SCHEMA/FIELDS:\n{json.dumps(schema)}\n\nDOCUMENT:\n{text[:60000]}\n\nReturn JSON only."
        msgs = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        try:
            content, used_model = _chat(client, msgs, temperature=0,
                                        response_format={"type": "json_object"})
        except NodeError:
            raise
        except Exception:
            # Retry without response_format (some routers reject it), still across the whole chain.
            content, used_model = _chat(client, msgs, temperature=0)
        content = content or "{}"

        extracted, parsed_ok = _coerce_json(content)

        # Retry once when the response cannot be parsed.
        #
        # The retry above only fires when the *request* throws — a router rejecting response_format.
        # A model that answers with prose instead of JSON never reached it, so a single unparseable
        # reply ended the call. Measured: this endpoint returned a signed result for one request and
        # ENGINE_FAILED for the byte-identical request later, because the failure is the model's
        # formatting on the day. The caller had already paid both times.
        #
        # One retry, with the failure quoted back so the model has something to correct, and
        # `_chat` advancing through its model chain. Still fails closed if the second attempt is also
        # unparseable — an answer we could not read must never be signed as though we had.
        tried: list[str] = []
        while not parsed_ok and len(tried) < 2:
            tried.append(used_model)
            try:
                retry_msgs = msgs + [
                    {"role": "assistant", "content": content[:2000]},
                    {"role": "user", "content": "That response could not be parsed as JSON. Reply "
                                                "with the JSON object alone: no prose, no code "
                                                "fence, no reasoning. Start with { and end with }."},
                ]
                content, used_model = _chat(client, retry_msgs, temperature=0,
                                            skip=tuple(tried))
                extracted, parsed_ok = _coerce_json(content or "")
            except Exception:                                        # noqa: BLE001
                break                                                # keep the original failure

        if not parsed_ok:
            # Fail closed. Previously this returned a {"_raw":..., "_parse_error":True} sentinel —
            # which is itself a dict, so `isinstance(extracted, (dict, list))` was True and the
            # result got stamped "validated" and cryptographically signed. Signing an assertion of
            # correctness over output we know we could not parse is the worst thing this codebase
            # could do, so an unparseable model response is now an error, not a paid deliverable.
            raise NodeError(
                ErrorCode.ENGINE_FAILED,
                "the model did not return parseable JSON across up to three attempts at temperature "
                "0, each on a different model in the chain and quoting the previous failure back "
                "for correction. Nothing was "
                "signed, because an answer we could not read must not be certified as one we could.",
            )

        requested_type = None
        schema = ctx.input.get("schema")
        if isinstance(schema, dict):
            requested_type = schema.get("type")
        # If an object was asked for, a list is the wrong shape however well-formed it is.
        shape_ok = not (requested_type == "object" and not isinstance(extracted, dict))

        return {
            "extracted": extracted,
            # The model that ACTUALLY answered, which is not always the configured one once the
            # fallback chain engages. This value is signed into the receipt.
            "model": used_model,
            "schema_valid": shape_ok,
            "requested_type": requested_type,
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        extracted = result.get("extracted")
        checks = [
            ValidationCheck(name="json_parsed", passed=result.get("schema_valid") is True),
            ValidationCheck(name="non_empty", passed=bool(extracted)),
            # Explicit: the old parse-failure sentinel must never reach a caller again.
            ValidationCheck(
                name="no_parse_error",
                passed=not (isinstance(extracted, dict) and extracted.get("_parse_error")),
            ),
        ]
        if result.get("requested_type") == "object":
            checks.append(ValidationCheck(name="is_object", passed=isinstance(extracted, dict)))
        # When the caller declared required fields, actually enforce them rather than trusting shape.
        schema = ctx.input.get("schema")
        required = schema.get("required") if isinstance(schema, dict) else None
        if isinstance(required, list) and isinstance(extracted, dict):
            missing = [k for k in required if k not in extracted]
            checks.append(ValidationCheck(
                name="required_fields_present",
                passed=not missing,
                detail=None if not missing else f"missing: {missing}",
            ))
        return checks


def _coerce_json(content: str) -> tuple[object, bool]:
    """Recover JSON from a model response. Returns (value, parsed_ok).

    Returns an explicit success flag rather than a sentinel value: a sentinel dict looks like a
    successful extraction to every `isinstance(x, dict)` check downstream, which is how an
    unparseable response previously ended up signed as valid.

    Candidates are tried in order of trustworthiness, and the ORIGINAL text is always among them —
    stripping <think> blocks with a greedy regex can delete a region containing real JSON when the
    model leaves a tag unclosed, so a strip that helps one response must not destroy another.
    """
    import re

    raw = (content or "").strip()
    candidates: list[str] = []

    def add(c: str) -> None:
        c = (c or "").strip()
        if c and c not in candidates:
            candidates.append(c)

    # Prefer whatever follows the last closing </think> — that is the model's actual answer.
    if "</think>" in raw.lower():
        idx = raw.lower().rfind("</think>")
        add(raw[idx + len("</think>"):])
    add(re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE))
    add(raw)  # never lose the original

    def strip_fence(c: str) -> str:
        if c.startswith("```"):
            c = c.strip("`").strip()
            if c.lower().startswith("json"):
                c = c[4:]
        return c.strip()

    for cand in list(candidates):
        add(strip_fence(cand))

    for cand in candidates:
        try:
            return json.loads(cand), True
        except Exception:
            pass

    # Last resort: the outermost balanced {...}. Only an object — manufacturing an array from
    # "[" ... "]" when an object was requested produced confidently-wrong paid output before.
    for cand in candidates:
        start = cand.find("{")
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i, ch in enumerate(cand[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cand[start : i + 1]), True
                    except Exception:
                        break
    return None, False



class TextSummarizeNode(Node):
    name = "text.summarize"
    price_usdt = 0.03
    deterministic = False
    asp_type = "A2MCP"
    engine = "minimax-m3"

    def __init__(self):
        self._settings = get_settings()
        self.engine_version = self._settings.llm_model

    def engine_available(self) -> bool:
        try:
            import openai  # noqa
        except Exception:
            return False
        return self._settings.llm_configured

    def run(self, ctx: NodeContext) -> dict:
        text = str(ctx.input.get("text", "")).strip()
        if not text:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'text'")
        if len(text) > 200_000:
            raise NodeError(ErrorCode.LIMIT_EXCEEDED, "text too large (>200k chars)")

        # A summary must be SHORTER than what it summarizes. The default budget of 120 words was
        # applied regardless of input size, so a 68-word source came back as a 77-word "summary" —
        # the buyer paid $0.03 to receive a longer text than they sent. The target is now bounded by
        # the source itself, so compression is structural rather than a hope about model behaviour.
        source_words = len(text.split())
        if source_words < 30:
            raise NodeError(
                ErrorCode.INVALID_INPUT,
                f"text is only {source_words} words — too short to summarize meaningfully. "
                f"Send at least 30 words, or use text.stats for measurements of short text.")
        requested = ctx.input.get("max_words")
        ceiling = max(15, int(source_words * 0.5))  # never allow more than half the source
        max_words = min(int(requested), ceiling) if requested else min(120, ceiling)
        s = self._settings
        try:
            from openai import OpenAI
        except Exception:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE, "openai client not installed")
        if not s.llm_configured:
            raise NodeError(ErrorCode.ENGINE_UNAVAILABLE, "LLM API key not configured")
        client = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=60)
        sysmsg = ("You are a precise summarizer. Return only the summary text, no preamble, "
                  "no <think> tags, no markdown headers. BEGIN WITH A COMPLETE SENTENCE — never start "
                  "mid-sentence or mid-phrase, and never drop the opening words of the subject.")
        user = (f"Summarize the following in at most {max_words} words. The source is {source_words} "
                f"words, so your summary MUST be substantially shorter than it.\n\n{text[:60000]}")
        try:
            out, used_model = _chat(
                client,
                [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}],
                temperature=0,
            )
            out = (out or "").strip()
        except NodeError:
            raise
        except Exception as e:
            raise NodeError(ErrorCode.ENGINE_FAILED, f"LLM call failed: {e}")
        import re
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL | re.IGNORECASE).strip()
        out = re.sub(r"<think>.*$", "", out, flags=re.DOTALL | re.IGNORECASE).strip()
        return {"summary": out, "word_count": len(out.split()), "model": used_model,
                # Surfaced so the caller can see the budget was bounded by their own input rather
                # than silently ignored — a request for 120 words on a 68-word source cannot be met.
                "source_words": source_words, "max_words_used": max_words}

    def validate(self, result, ctx):
        """Check the summary is a summary, not a fragment.

        A measured delivery began "generation (RAG) grounds model outputs..." — the model had dropped
        the opening words "Retrieval-augmented", so the first thing the buyer read was a sentence
        fragment. `non_empty` could not see that, and the artifact was signed regardless.
        """
        summary = (result.get("summary") or "").strip()
        checks = [ValidationCheck(name="non_empty_summary", passed=bool(summary))]
        if summary:
            first = summary.lstrip("\"'([")[:1]
            checks.append(ValidationCheck(
                name="starts_complete_sentence",
                passed=bool(first) and (first.isupper() or first.isdigit()),
                detail="a summary that opens lower-case is usually a truncated first sentence"))
            checks.append(ValidationCheck(
                name="shorter_than_source",
                passed=len(summary) < len(str(ctx.input.get("text") or "")),
                detail="a summary must be shorter than what it summarizes"))
        return checks
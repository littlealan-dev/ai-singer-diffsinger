# OpenAI LLM Client LLD

## 1. Goal

Add a new backend LLM client for the OpenAI API so the application can switch between the existing Gemini client and a new OpenAI client using environment variables only.

Primary goals:

- keep the current orchestrator contract unchanged
- make provider selection runtime-configurable via env
- support OpenAI prompt caching in the design
- avoid changing prompt semantics for the current workflow
- preserve Gemini as a supported provider

Out of scope for v1:

- multi-provider fallback within a single request
- streaming responses
- model-specific prompt tuning beyond minimal provider adaptation
- replacing the current orchestrator response schema

## 2. Context

Current LLM path:

- [`src/backend/llm_factory.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_factory.py)
  - selects `static` or `gemini` based on `LLM_PROVIDER`
- [`src/backend/llm_client.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_client.py)
  - defines the `LlmClient.generate(prompt_bundle, history) -> str` protocol
- [`src/backend/llm_prompt.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py)
  - builds `PromptBundle(static_prompt_text, dynamic_prompt_text)`
- [`src/backend/llm_gemini.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_gemini.py)
  - converts `PromptBundle` + chat history into a Gemini request
- [`src/backend/gemini_cache.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/gemini_cache.py)
  - manages Gemini explicit prompt caches

Important architectural point:

- the backend already separates static prompt text from dynamic runtime context
- that split is the right foundation for OpenAI prompt caching too

## 3. External Constraints

This section is based on current official OpenAI docs as of 2026-03-13.

Primary references:

- OpenAI text generation guide: https://platform.openai.com/docs/guides/text-generation
- OpenAI Responses migration guide: https://platform.openai.com/docs/guides/responses-vs-chat-completions
- OpenAI prompt caching guide: https://platform.openai.com/docs/guides/prompt-caching
- OpenAI Responses API reference: https://platform.openai.com/docs/api-reference/responses

Relevant points from those docs:

- OpenAI recommends the Responses API for new text-generation integrations.
- Prompt caching is automatic for prompts with long identical prefixes.
- Cache hits depend on exact prefix matches, so stable instructions must appear first.
- `prompt_cache_key` is optional but improves routing/cache locality for similar requests.
- `prompt_cache_retention` is configurable per request.
- default retention is `in_memory`.
- `24h` retention is available only for supported models.
- cache visibility is indirect through `usage.prompt_tokens_details.cached_tokens`; there is no manual cache create/list/delete flow like Gemini explicit caching.

Inference from those constraints:

- we do not need an OpenAI equivalent of `GeminiPromptCacheManager`
- we do need a stable request prefix and stable `prompt_cache_key`
- we should log cached-token metrics for observability

## 4. Design Summary

Add a new provider implementation:

- `OpenAIRestClient` in a new module, likely [`src/backend/llm_openai.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_openai.py)

Extend settings and factory logic so:

- `LLM_PROVIDER=gemini` uses the current Gemini client
- `LLM_PROVIDER=openai` uses the new OpenAI client
- `LLM_PROVIDER=static` remains available for tests

OpenAI v1 transport choice:

- use direct REST over `urllib.request`, matching the current Gemini client style

Reason:

- keeps dependency surface small
- matches current backend implementation style
- avoids introducing an SDK-wide abstraction change during the hackathon

## 5. Proposed Environment Variables

Retain existing:

- `LLM_PROVIDER`

Add OpenAI-specific settings:

- `OPENAI_API_KEY`
- `OPENAI_API_KEY_SECRET`
- `OPENAI_API_KEY_SECRET_VERSION`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_TIMEOUT_SECONDS`
- `OPENAI_PROMPT_CACHE_ENABLED`
- `OPENAI_PROMPT_CACHE_KEY_PREFIX`
- `OPENAI_PROMPT_CACHE_RETENTION`
- `OPENAI_REASONING_EFFORT`

Recommended semantics:

- `LLM_PROVIDER`
  - allowed values: `gemini`, `openai`, `static`, `none`
- `OPENAI_API_KEY`
  - used directly in dev/local
- `OPENAI_API_KEY_SECRET`
  - Secret Manager name for prod-like envs
- `OPENAI_API_KEY_SECRET_VERSION`
  - Secret Manager version, default `latest`
- `OPENAI_BASE_URL`
  - default `https://api.openai.com/v1`
- `OPENAI_MODEL`
  - required when `LLM_PROVIDER=openai`
- `OPENAI_TIMEOUT_SECONDS`
  - default aligned with existing Gemini timeout strategy
- `OPENAI_PROMPT_CACHE_ENABLED`
  - default `true` in prod-like envs, `false` in local/test unless explicitly enabled
- `OPENAI_PROMPT_CACHE_KEY_PREFIX`
  - optional stable prefix, default `sightsinger`
- `OPENAI_PROMPT_CACHE_RETENTION`
  - allowed: empty, `in_memory`, `24h`
- `OPENAI_REASONING_EFFORT`
  - optional pass-through for models that support reasoning effort

## 6. Settings Changes

Add these fields to [`Settings`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/__init__.py):

- `openai_api_key: str`
- `openai_api_key_secret: str`
- `openai_api_key_secret_version: str`
- `openai_base_url: str`
- `openai_model: str`
- `openai_timeout_seconds: float`
- `openai_prompt_cache_enabled: bool`
- `openai_prompt_cache_key_prefix: str`
- `openai_prompt_cache_retention: str`
- `openai_reasoning_effort: str`

Loading rules should mirror Gemini:

- in dev/local/test:
  - read `OPENAI_API_KEY` directly from env
- in prod-like env:
  - read the real key from Secret Manager via `OPENAI_API_KEY_SECRET`

Validation rules:

- if `LLM_PROVIDER=openai` and no API key is resolved, `create_llm_client()` returns `None` or raises a clear configuration error
- if `OPENAI_PROMPT_CACHE_RETENTION` is non-empty and not one of `in_memory` or `24h`, fail fast during config loading

## 7. Request Shape

### 7.1 Chosen API

Use OpenAI Responses API.

Reason:

- officially recommended for new work
- better long-term fit than Chat Completions
- supports prompt caching fields directly
- supports developer/user role structure cleanly

### 7.2 Prompt Mapping

Current internal prompt model:

- `static_prompt_text`
- `dynamic_prompt_text`
- `history`

OpenAI request mapping:

- `input` is an ordered message array
- the static prompt becomes the first `developer` message
- the dynamic prompt becomes the next `user` message
- existing chat history follows in order

Target request shape:

```json
{
  "model": "<OPENAI_MODEL>",
  "input": [
    {
      "role": "developer",
      "content": [
        {
          "type": "input_text",
          "text": "<static_prompt_text>"
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "<dynamic_prompt_text>"
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "<history[0].content>"
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "<history[1].content>"
        }
      ]
    }
  ]
}
```

Notes:

- exact content-item types may be simplified to text-only request items in implementation
- the important part is stable ordering and stable prefix

### 7.3 History Mapping

History mapping rule:

- backend `assistant` -> OpenAI `assistant`
- everything else -> OpenAI `user`

History truncation should continue to honor the current `LLM_MAX_HISTORY_ITEMS` policy.

## 8. Prompt Caching Design

## 8.1 Why the current prompt split is already correct

OpenAI cache hits require an exact stable prefix.

That means the beginning of each request should be:

1. deployment-static instructions
2. stable tool schemas / lint rules
3. only then request-varying runtime context

The existing `PromptBundle` split already supports this:

- `static_prompt_text`
  - cache-friendly prefix
- `dynamic_prompt_text`
  - per-session / per-turn suffix

This is the correct structure for OpenAI prompt caching and should be preserved.

## 8.2 No manual cache manager

Unlike Gemini explicit caching:

- do not create `openai_cache.py`
- do not try to create/list/delete cache records
- do not persist cache IDs in application state

OpenAI caching should be implemented by:

- prompt prefix stability
- optional `prompt_cache_key`
- optional `prompt_cache_retention`
- usage logging via `cached_tokens`

## 8.3 Prompt cache key

Recommended `prompt_cache_key`:

`{prefix}:llm:{provider}:{model}:{build_id}`

Example:

`sightsinger:llm:openai:gpt-4.1:build-abc123`

Reason:

- stable across requests within one deployment
- segregates caches across model changes
- segregates caches across backend prompt revisions
- avoids accidental cache fragmentation by session-specific values

Do not include:

- session ID
- user ID
- score summary
- parsed score JSON

Those would destroy cache reuse.

## 8.4 Retention policy

Config behavior:

- empty value:
  - omit `prompt_cache_retention`
- `in_memory`:
  - send `prompt_cache_retention="in_memory"`
- `24h`:
  - send `prompt_cache_retention="24h"`

Recommended v1 default:

- use `in_memory`

Reason:

- safer baseline
- broadly supported
- does not depend on extended-cache model support

`24h` should be opt-in via env only.

## 8.5 Observability

On every OpenAI response, log:

- provider
- model
- prompt cache key
- retention policy
- prompt token count
- cached token count
- completion token count
- total token count

Source field to inspect:

- `usage.prompt_tokens_details.cached_tokens`

This gives deployment-time evidence that caching is actually working.

## 9. OpenAI Client Responsibilities

The new `OpenAIRestClient` should:

- accept `Settings`
- resolve API key
- build a Responses API request from `PromptBundle` + history
- optionally add prompt caching parameters
- optionally add reasoning settings
- extract text output
- log usage metadata, especially cached tokens
- raise clear runtime errors for HTTP and schema failures

Suggested public shape:

```python
class OpenAIRestClient:
    def __init__(self, settings: Settings, *, api_key: str | None = None) -> None: ...
    def generate(self, prompt_bundle: PromptBundle | str, history: list[dict[str, str]]) -> str: ...
```

## 10. Response Parsing

The current backend expects:

- a string containing JSON
- later parsed by [`parse_llm_response()`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py#L172)

OpenAI v1 should preserve that contract:

- request ordinary text output
- do not change orchestrator parsing logic in this migration

This keeps risk low.

Optional future enhancement:

- use OpenAI structured outputs / JSON schema enforcement

Out of scope for this LLD v1 because it would change validation behavior and require separate rollout/testing.

## 11. Factory Changes

Update [`create_llm_client()`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_factory.py#L16) to support:

- `provider == "openai"`

Decision table:

- `static`
  - return `StaticLlmClient`
- `gemini`
  - return `GeminiRestClient`
- `openai`
  - return `OpenAIRestClient`
- `none` / disabled
  - return `None`

## 12. Failure Modes

Expected OpenAI-specific failures:

- missing API key
- invalid base URL
- HTTP 401 / 403 auth failures
- HTTP 429 rate limiting
- HTTP 5xx upstream failures
- malformed or empty response payload
- model does not support requested retention policy or reasoning settings

Handling:

- wrap transport errors in clear `RuntimeError` messages, matching current Gemini behavior
- include HTTP code and response body summary where safe
- do not silently fall back from OpenAI to Gemini

## 13. Compatibility with Existing Prompt Architecture

No changes should be required to:

- system prompt files
- tool schema generation
- dynamic context assembly
- orchestrator response parsing

The provider-specific difference should remain confined to:

- request transport
- request payload mapping
- caching parameters
- response extraction

This keeps provider switching a factory/config concern instead of an orchestrator concern.

## 14. MCP Tool Calling Impact

MCP tool calling should have minimal impact in this migration.

Current behavior is not provider-native tool calling.

Current flow:

- tool schemas are rendered into the prompt in [`build_prompt_bundle()`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py#L46)
- the LLM returns JSON with:
  - `tool_calls`
  - `final_message`
  - `include_score`
- the backend parses that JSON with [`parse_llm_response()`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py#L172)
- the orchestrator then executes the requested MCP tools itself

Implications for OpenAI v1:

- do not switch to OpenAI native tool/function calling
- do not translate MCP tools into OpenAI tool definitions
- do not change orchestrator tool dispatch
- do not change the JSON response contract expected from the model

Required design check:

- the tool schema block must remain part of `static_prompt_text`

Reason:

- it is deployment-static
- it is repeated on every request
- it is large enough to benefit from prompt caching
- it keeps Gemini and OpenAI behavior aligned

Risk to watch:

- provider swap must not accidentally reorder prompt components so that tool schema moves after runtime context
- if the tool schema stops being part of the stable prefix, OpenAI prompt cache efficiency will degrade

Verification requirements:

- with `LLM_PROVIDER=openai`, the model must still produce the same `tool_calls` JSON shape the orchestrator expects
- tool names and argument shapes must remain unchanged
- action-required follow-up flows must still work because they depend on the same prompt-level tool contract, not provider-native tool invocation

Future enhancement explicitly out of scope:

- provider-native OpenAI tool calling for MCP tools

That would require a separate design because it changes:

- request schema
- response parsing
- tool execution boundary
- retry semantics
- prompt caching composition

## 15. Test Plan

Unit tests:

- config loads OpenAI env fields correctly
- invalid `OPENAI_PROMPT_CACHE_RETENTION` fails fast
- factory returns `OpenAIRestClient` when `LLM_PROVIDER=openai`
- OpenAI payload ordering is:
  - developer static prompt first
  - dynamic prompt second
  - history afterward
- `prompt_cache_key` is stable across requests with same build/model
- `prompt_cache_retention` omitted when unset
- usage logging handles missing `cached_tokens` gracefully
- text extraction works for normal OpenAI responses
- HTTP error mapping includes status and summary

Integration tests with stubbed HTTP:

- successful `responses.create` round-trip
- request contains expected auth header
- request contains `prompt_cache_key` when enabled
- request contains `prompt_cache_retention` when configured
- request omits cache fields when disabled

Non-goal for tests:

- real OpenAI network calls in standard CI

Additional MCP-specific tests:

- OpenAI provider preserves the exact JSON response shape currently parsed by `parse_llm_response()`
- prompt bundle still includes tool schema in `static_prompt_text`
- tool-calling follow-up turns using internal tool output payloads still round-trip without provider-specific branching

## 16. Rollout Plan

Phase 1:

- add config fields
- add `OpenAIRestClient`
- add factory selection
- add tests

Phase 2:

- deploy with `LLM_PROVIDER=gemini` unchanged
- run OpenAI in dev using env switch

Phase 3:

- deploy hackathon environment with:
  - `LLM_PROVIDER=openai`
  - `OPENAI_MODEL=<chosen model>`
  - `OPENAI_PROMPT_CACHE_ENABLED=true`
  - `OPENAI_PROMPT_CACHE_RETENTION=in_memory`

Phase 4:

- inspect logs for:
  - correctness
  - latency
  - `cached_tokens > 0`

Only after that:

- consider `24h` retention for supported models

## 17. Open Questions

1. Which exact OpenAI model should be the first hackathon default?

Recommendation:

- keep the model fully env-driven in v1
- do not hardcode a production default beyond a safe empty/default config requirement

2. Should v1 use OpenAI structured outputs?

Recommendation:

- no
- preserve current plain-text JSON contract first

3. Should we keep Gemini explicit cache logic if `LLM_PROVIDER=openai`?

Recommendation:

- yes, but only as dormant provider-specific code
- no shared cache abstraction is required in v1 because Gemini and OpenAI caching mechanisms are fundamentally different

4. Should MCP tools ever move to provider-native OpenAI tool calling?

Recommendation:

- not in this migration
- keep MCP tool calling prompt-driven for provider parity and low risk

## 18. Final Recommendation

Implement OpenAI as a second provider behind the current `LlmClient` interface, selected by `LLM_PROVIDER=openai`, using the Responses API and the existing `PromptBundle` static/dynamic split.

Prompt caching design for OpenAI should be:

- automatic caching only
- static prefix first
- stable `prompt_cache_key`
- optional retention policy via env
- usage logging through `cached_tokens`

This is the lowest-risk migration path because it:

- reuses the current prompt architecture
- avoids orchestrator changes
- keeps Gemini intact
- makes provider switching operational rather than architectural

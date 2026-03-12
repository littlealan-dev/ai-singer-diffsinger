# Gemini Explicit Prompt Caching LLD

## 1. Goal

Reduce Gemini prompt cost and latency by moving deployment-static prompt text into Gemini explicit cache.

Scope for v1:

- cache the static backend workflow prompt in [`src/backend/config/system_prompt.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt.txt)
- cache the planning / replanning guidance in [`src/backend/config/system_prompt_lessons.txt`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/system_prompt_lessons.txt)
- cache deployment-static MCP tool schema / tool specs
- cache rendered lint and post-flight rule text in the same cached artifact, because those are generated from code and change only when the build changes

Out of scope for v1:

- caching per-session score summaries
- caching parsed score JSON
- caching repair follow-up payloads
- caching chat history

## 2. External Constraints

Based on the Gemini docs:

- Explicit caching is appropriate when a substantial initial context is reused repeatedly.
- Cached content has a TTL or `expire_time`. If omitted, TTL defaults to 1 hour.
- Cache metadata can be listed and fetched.
- Cache TTL / `expire_time` can be updated.
- Cache contents cannot be otherwise mutated; if the cached prompt changes, a new cache should be created.

Primary references:

- Gemini long context: https://ai.google.dev/gemini-api/docs/long-context
- Gemini context caching: https://ai.google.dev/gemini-api/docs/caching

Relevant points from those docs:

- Long-context optimization guidance explicitly recommends context caching as the primary optimization for repeated large prompt prefixes.
- Gemini guidance says the query is usually best placed at the end of the prompt.
- Explicit caching supports manual create / get / list / update / delete.
- TTL has no documented min/max bounds, but it is still time-bounded. “Never expires” must therefore be implemented as an effectively non-expiring far-future expiration policy plus deployment refresh.

## 3. Current State

Current prompt construction:

- [`src/backend/llm_prompt.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py)
  - `_load_system_prompt()` loads and concatenates:
    - `system_prompt.txt`
    - `system_prompt_lessons.txt`
    - rendered lint rules
    - rendered post-flight validation rules
  - `build_system_prompt(...)` then injects dynamic runtime data:
    - score summary
    - parsed score JSON
    - voice-part signals
    - preprocess mapping context
    - last preprocess plan
    - voicebank details
    - tool schemas

Current Gemini request path:

- [`src/backend/llm_gemini.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_gemini.py)
  - sends the entire built prompt as `system_instruction`
  - sends chat history as `contents`
  - does not use `cachedContent`

Result:

- the same static prompt files are resent on every Gemini call
- every repair turn pays again for the same large instruction prefix
- this is exactly the repeated long-context prefix pattern explicit caching is meant to optimize

## 4. Design Summary

Split prompt construction into two layers:

1. Static cached prompt layer
- deployment-static prompt text
- created once per deployment / prompt fingerprint
- referenced by Gemini `cachedContent`

2. Dynamic runtime prompt layer
- per-request runtime context
- still sent on every request
- remains outside the explicit cache

The static cached layer becomes the “instruction backbone”.
The dynamic runtime layer remains the request-specific context that changes with:

- score availability
- score summary / parsed score
- preprocess mapping context
- latest preprocess plan
- voicebank details

Static/dynamic separation rule:

- Static layer contains instruction prose and deployment-static reference material only.
- Dynamic layer contains only session-specific and request-specific JSON/data blocks.
- Runtime placeholders such as `{score_summary}` and `{parsed_score_json}` must not remain embedded in the cached static prompt text.

## 5. Prompt Split

### 5.1 Static cached payload

v1 cached payload should include:

- `system_prompt.txt`
- `system_prompt_lessons.txt`
- MCP tool schema / tool specs
- rendered lint rules
- rendered post-flight validation rules

Reason:

- they are deployment-static
- they are repeated on every request
- they are large
- they are instruction-heavy and expensive to resend

### 5.2 Dynamic runtime payload

v1 dynamic prompt payload should include:

- score hint
- score summary
- parsed score JSON
- voice-part signals
- preprocess mapping context
- last preprocess plan
- voicebank details

These remain request-scoped because they vary across sessions and turns.

Implementation rule for `PromptBundle`:

- `static_prompt_text`
  - instruction text from `system_prompt.txt`
  - instruction text from `system_prompt_lessons.txt`
  - MCP tool schema / tool specs
  - rendered lint rules
  - rendered post-flight validation rules
- `dynamic_prompt_text`
  - score hint
  - score summary
  - parsed score JSON
  - voice-part signals
  - preprocess mapping context
  - last preprocess plan
  - voicebank details
  - any other request-scoped JSON blocks

## 6. Request Shape

### 6.1 Current

Current Gemini request:

- `system_instruction = full prompt`
- `contents = chat history`

### 6.2 Target

Target Gemini request for cached path:

- `cachedContent = <cache-name>`
- `contents = [dynamic runtime prompt, chat history]`

Important prompt-order rule:

- the cached long-form instructions come first via `cachedContent`
- the dynamic runtime context comes after that
- the actual latest user query remains near the end, which is consistent with Gemini long-context guidance

Implementation detail:

- the dynamic runtime prompt should be injected as a synthetic first user content entry before the chat history entries, not as system instruction
- this avoids ambiguity around mixing `cachedContent` with a second per-request `system_instruction`
- the dynamic block must be explicitly delimited so the model can distinguish it from organic user chat history

Recommended wrapper:

```text
Dynamic Context:
<runtime JSON blocks>
End Dynamic Context.
```

## 7. Cache Identity and Lifecycle

### 7.1 Cache identity

Each cache should be identified by:

- Gemini model name
- deployment build ID
- static prompt fingerprint

Recommended deterministic display name format:

`prompt-cache:{model}:{build_id}:{fingerprint_prefix}`

Where:

- `build_id` is a deployment-scoped identifier
- `fingerprint_prefix` is the first 12-16 hex chars of a SHA-256 of the cached static prompt text

### 7.2 Build ID source

Add a new backend setting:

- `BACKEND_BUILD_ID`

Priority:

1. explicit env `BACKEND_BUILD_ID`
2. Cloud Run `K_REVISION` fallback
3. final fallback: `unknown-build`

Reason:

- cache refresh should be tied to deployment identity, not process start

### 7.3 Storage of cache identity

Do not add a database dependency in v1.

Instead:

- use `display_name` as the discovery key
- on first Gemini use, call list / get cache metadata
- reuse a matching cache if one exists for the current model + build ID + fingerprint

This is acceptable because:

- the number of prompt caches is tiny
- only one or a few builds exist at a time
- prompt cache lookup is infrequent

Refinement:

- cache discovery results must be memoized in memory after the first successful lookup
- hot-path requests must not repeatedly call the cache listing endpoint once a matching cache has been resolved
- if cache creation succeeds, update the in-memory mapping immediately
- if stale caches are deleted, evict them from the in-memory mapping too

## 8. TTL / “Never Expires” Policy

Literal infinite TTL is not available. The design therefore uses an effectively non-expiring expiration policy.

### 8.1 Policy

On cache creation:

- set `expire_time` far in the future
- recommended default: current time + 3650 days (10 years)

On each new deployment:

- create or reuse the cache for the new build ID
- old deployment caches are considered stale
- stale caches should be deleted asynchronously or opportunistically

### 8.2 Why not refresh on each request

Do not PATCH the TTL on every request.

Reason:

- adds unnecessary API traffic
- adds latency to the hot path
- complicates failure behavior
- is unnecessary if the cache is already build-scoped and far-future

### 8.3 Deployment refresh semantics

“Refresh on each new build deployment” means:

- a new deployment gets a new build ID
- a new build ID produces a new cache identity
- prompt changes naturally produce a new fingerprint
- old caches are deleted during cleanup

This is simpler and safer than mutating a shared cache in place.

## 9. Cleanup Policy

On cache initialization:

- list prompt caches with the known display-name prefix for the same model
- keep the cache matching current build ID + fingerprint
- delete all other prompt caches for the same model and prefix

This keeps at most one active prompt cache per model per deployment line.

If deletion fails:

- log warning only
- do not fail request handling

## 10. Failure Behavior

If cache lookup / create / update / delete fails:

- log the failure with model, build ID, and phase
- fall back to current non-cached Gemini request path

No user-visible failure should be introduced by cache problems.

This is a performance and cost optimization, not a correctness dependency.

## 11. Proposed Code Changes

### 11.1 New prompt split API

Update [`src/backend/llm_prompt.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_prompt.py):

- add `load_static_prompt_text() -> str`
- add `build_dynamic_prompt_context(...) -> str`
- keep `build_system_prompt(...)` for backward compatibility / non-cached providers, but implement it as:
  - `static + dynamic`

Suggested shape:

```python
@dataclass(frozen=True)
class PromptBundle:
    static_prompt_text: str
    dynamic_prompt_text: str
```

Add:

- `build_prompt_bundle(...) -> PromptBundle`

Specific v1 requirement:

- move MCP tool schema rendering fully into the static prompt builder
- do not continue injecting tool schema via the dynamic prompt path

### 11.2 Gemini cache manager

Add new module:

- `src/backend/gemini_cache.py`

Responsibilities:

- compute static prompt fingerprint
- derive display name
- list caches
- find matching cache metadata
- create cache when missing
- delete stale prompt caches
- return `cached_content_name`
- maintain an in-memory mapping of resolved prompt caches

Suggested public API:

```python
class GeminiPromptCacheManager:
    def ensure_prompt_cache(
        self,
        *,
        model: str,
        build_id: str,
        static_prompt_text: str,
    ) -> str | None:
        ...
```

Return:

- cache name on success
- `None` on failure / disabled mode

Recommended internal state:

```python
self._cache_names_by_key: dict[tuple[str, str, str], str]
```

Key tuple:

- model
- build_id
- static prompt fingerprint

### 11.3 Gemini REST client changes

Update [`src/backend/llm_gemini.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_gemini.py):

- add support for a prompt bundle instead of a single string
- if prompt caching is enabled and a cache name is available:
  - send `cachedContent`
  - prepend dynamic runtime prompt to `contents`
- otherwise:
  - fall back to current `system_instruction + contents` path

This will require a small LLM client interface change.

### 11.4 LLM client interface

Update [`src/backend/llm_client.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_client.py):

- replace `generate(system_prompt, history)` with one of:

Option A:

```python
def generate(self, prompt_bundle: PromptBundle, history: List[Dict[str, str]]) -> str:
    ...
```

Option B:

```python
def generate(
    self,
    *,
    static_prompt: str,
    dynamic_prompt: str,
    history: List[Dict[str, str]],
) -> str:
    ...
```

Recommended:

- Option A, because it keeps the call site simpler

### 11.5 Factory and settings

Update:

- [`src/backend/config/__init__.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/config/__init__.py)
- [`src/backend/llm_factory.py`](/Users/alanchan/antigravity/ai-singer-diffsinger/src/backend/llm_factory.py)

Add settings:

- `BACKEND_BUILD_ID`
- `GEMINI_PROMPT_CACHE_ENABLED` default `true` in prod, `false` in dev
- `GEMINI_PROMPT_CACHE_TTL_DAYS` default `3650`
- optional `GEMINI_PROMPT_CACHE_DELETE_STALE` default `true`

## 12. Detailed Flow

### 12.1 Startup / first use

Cache initialization is lazy. It does not happen at process startup.

1. Build prompt bundle
2. Compute static prompt fingerprint
3. Build cache display name from model + build ID + fingerprint
4. List existing caches
5. If matching cache exists:
   - use it
6. Else:
   - create cache with:
     - `model`
     - `display_name`
     - `system_instruction` = static prompt text
     - `expire_time` = now + configured days
7. Optionally delete stale caches for the same prefix

### 12.2 Per Gemini request

1. Build prompt bundle
2. Ensure prompt cache exists
3. If cache available:
   - `cachedContent = cache.name`
   - `contents = [dynamic runtime prompt, chat history...]`
4. Else:
   - send existing non-cached request

## 13. Concurrency

Cloud Run may have multiple instances starting simultaneously.

v1 policy:

- tolerate duplicate cache creation races
- if two instances create equivalent caches, cleanup later removes stale duplicates
- do not add distributed locking in v1

Reason:

- this is low-frequency
- duplicate cache cost is small
- simplicity is better than adding lock infrastructure

## 14. Observability

Add logs:

- `gemini_prompt_cache_lookup`
- `gemini_prompt_cache_hit`
- `gemini_prompt_cache_memory_hit`
- `gemini_prompt_cache_create`
- `gemini_prompt_cache_create_failed`
- `gemini_prompt_cache_delete_stale`
- `gemini_prompt_cache_delete_failed`
- `gemini_prompt_cache_fallback_uncached`

Recommended fields:

- model
- build_id
- fingerprint_prefix
- display_name
- cache_name
- expire_time
- session_id when request-scoped

## 15. Security / Data Boundaries

Do not cache session data.

Only cache deployment-static prompt text.

This avoids:

- user-data retention surprises
- score-content leakage across sessions
- invalidation complexity for session-specific content

## 16. Rollout Plan

1. Add prompt split API
2. Add Gemini prompt cache manager
3. Add settings / env vars
4. Keep uncached fallback path intact
5. Enable in dev first
6. Verify:
   - cache created once
   - later requests use `cachedContent`
   - stale caches deleted on redeploy
7. Enable in prod

## 17. Test Plan

### Unit

- static prompt fingerprint is stable
- display name format is deterministic
- prompt split produces static vs dynamic outputs correctly
- cache lookup reuses exact match
- stale cache selection is ignored
- far-future `expire_time` is computed correctly

### Integration

- first request with cache enabled creates prompt cache
- second request reuses it
- Gemini request uses `cachedContent`
- cache-manager failure falls back to uncached path
- new `BACKEND_BUILD_ID` causes new cache creation
- stale cache cleanup deletes old build cache

### Non-functional

- verify request payload size drops materially on cached path
- verify latency improvement on repeated repair turns
- verify no user/session content appears in cached artifact

## 18. Open Questions

1. Should v1 cache only `system_prompt.txt` + `system_prompt_lessons.txt`, or also include rendered lint/post-flight rule text and MCP tool schema?

Recommended answer:

- include rendered lint and post-flight rules, and include MCP tool schema too, because all are build-static and large

2. Should cache initialization happen eagerly at startup or lazily on first Gemini request?

Recommended answer:

- lazy on first request
- avoids startup hard dependency on Gemini cache APIs

These decisions are accepted for v1.

Additional accepted refinements from review:

- `PromptBundle` must cleanly separate static instruction/reference text from dynamic session JSON blocks.
- cache discovery must be memoized in-memory after the first successful lookup.
- dynamic prompt context must be clearly delimited when injected into `contents`.

## 19. Recommendation

Implement deployment-scoped explicit prompt caching with:

- static prompt split
- build-ID-based cache identity
- far-future `expire_time`
- stale-cache deletion on new deployment
- uncached fallback on any cache error

That gives cost and latency improvement without making cache state part of request correctness.

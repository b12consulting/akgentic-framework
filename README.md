# akgentic-llm

[![CI](https://github.com/b12consulting/akgentic-llm/actions/workflows/ci.yml/badge.svg)](https://github.com/b12consulting/akgentic-llm/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/gpiroux/dd80a44fe9e2e27b46f7f3431e19202f/raw/coverage.json)](https://github.com/b12consulting/akgentic-llm/actions/workflows/ci.yml)

LLM integration layer for the [Akgentic](https://github.com/b12consulting/akgentic-quick-start)
multi-agent framework. Wraps pydantic-ai's REACT execution loop with persistent context
management, production HTTP retry logic, and a clean provider abstraction — letting agents
call any LLM without coupling to a specific vendor or framework primitive.

## Table of Contents

- [Overview](#overview)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
  - [ModelConfig](#modelconfig)
  - [UsageLimits](#usagelimits)
  - [RuntimeConfig](#runtimeconfig)
  - [ReactAgentConfig](#reactagentconfig)
- [Providers](#providers)
- [ReactAgent API](#reactagent-api)
- [Multimodal Prompts](#multimodal-prompts)
- [Context Management](#context-management)
  - [ContextManager](#contextmanager)
  - [Observer Pattern](#observer-pattern)
  - [Tool Event Observability](#tool-event-observability)
- [Cost Tracking and Aggregation](#cost-tracking-and-aggregation)
- [Prompts](#prompts)
- [Development](#development)
- [License](#license)

## Overview

`akgentic-llm` is the LLM execution layer between agent logic and LLM providers. It provides:

- **ReactAgent** — a thin wrapper around pydantic-ai's `Agent.iter()` that persists message
  history across calls, deduplicates messages across tool-call iterations, and translates
  pydantic-ai's `UsageLimitExceeded` into a framework-local `UsageLimitError`
- **Provider abstraction** — `create_model()` dispatches to one of six provider factories
  (OpenAI, Azure, Anthropic, Google, Mistral, NVIDIA); `get_output_type()` wraps output types
  with `NativeOutput` for providers that support structured output, falls back to prompt-based
  extraction for those that don't
- **HTTP retry** — `create_http_client()` configures `AsyncTenacityTransport` with exponential
  backoff, jitter, and `Retry-After` header support; fast-fails on 4xx (except 429)
- **Context management** — `ContextManager` tracks message history across multiple `run()` calls,
  supports checkpoint/rewind for error recovery, and applies a sliding window (system messages
  always preserved) when a message cap is configured
- **Prompt utilities** — `PromptTemplate` for config-time `{placeholder}` rendering;
  `current_datetime_prompt` and `json_output_reminder_prompt` as ready-made dynamic prompts
- **Multimodal** — `UserPrompt = str | list[str | BinaryContent]`; exported so `akgentic-agent`
  can annotate its own `act()` signature without importing pydantic-ai directly

```
ReactAgent
  │
  ├── run(user_prompt: UserPrompt)           # str | list[str | BinaryContent]
  │     │
  │     ├── pydantic_agent.iter(            # pydantic-ai REACT loop
  │     │       user_prompt,
  │     │       message_history=context.messages,
  │     │       output_type=get_output_type(model_cfg, output_type),
  │     │   )
  │     │     │
  │     │     └── for each step:
  │     │           context.add_message()   # persists + notifies observers
  │     │
  │     └── return run.result.output
  │
  ├── context: ContextManager               # persistent message history
  ├── checkpoint() / rewind()               # snapshot and restore context
  └── system_prompt(func)                   # register dynamic system prompt
```

**Module boundary:** `akgentic-llm` depends only on `pydantic-ai`, `httpx`, and `tenacity`.
It MUST NOT import from `akgentic-core`, `akgentic-tool`, or `akgentic-agent`.

## Installation

### Workspace Installation (Recommended)

```bash
git clone git@github.com:b12consulting/akgentic-quick-start.git
cd akgentic-quick-start
git submodule update --init --recursive

uv venv && source .venv/bin/activate
uv sync --all-packages --all-extras
```

### Standalone

```bash
cd packages/akgentic-llm
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Quick Start

```python
from akgentic.llm import ReactAgent, ReactAgentConfig, ModelConfig

config = ReactAgentConfig(
    model_cfg=ModelConfig(provider="openai", model="gpt-4o")
)

agent = ReactAgent(config=config)
result = agent.run_sync("Summarise the key priorities for next sprint.")
print(result)
```

With tools and a per-call output type:

```python
from pydantic import BaseModel
from akgentic.llm import ReactAgent, ReactAgentConfig, ModelConfig, UsageLimits

class Summary(BaseModel):
    title: str
    points: list[str]

def fetch_data(topic: str) -> str:
    """Retrieve data about a topic."""
    return f"Latest data on {topic}: ..."

agent = ReactAgent(
    config=ReactAgentConfig(
        model_cfg=ModelConfig(provider="anthropic", model="claude-3-5-sonnet-20241022"),
        usage_limits=UsageLimits(request_limit=10, total_tokens_limit=20_000),
    ),
    tools=[fetch_data],
)

result = agent.run_sync("Summarise AI trends", output_type=Summary)
print(result.title, result.points)
```

## Configuration

### ModelConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | `Literal[...]` | `"openai"` | LLM provider |
| `model` | `str` | `"gpt-5.2"` | Model identifier (provider-specific) |
| `temperature` | `float \| None` | `None` | 0.0–2.0; `None` = provider default |
| `seed` | `int \| None` | `None` | Reproducible outputs (not all providers) |
| `max_tokens` | `int \| None` | `None` | Max response tokens; `None` = provider max |
| `reasoning_effort` | `Literal["low","medium","high"] \| None` | `None` | For o1/o3-style models only |

```python
from akgentic.llm import ModelConfig

# Standard chat model
ModelConfig(provider="openai", model="gpt-4o", temperature=0.7)

# Deterministic with token cap
ModelConfig(provider="anthropic", model="claude-3-5-sonnet-20241022",
            temperature=0.0, seed=42, max_tokens=2000)

# Reasoning model
ModelConfig(provider="openai", model="o1", reasoning_effort="high")
```

### UsageLimits

Limits are cumulative across all requests in a single `run()` call. Breaching any limit
raises `UsageLimitError`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_limit` | `int \| None` | `50` | Max LLM API requests — acts as a safety brake |
| `tool_calls_limit` | `int \| None` | `None` | Max tool invocations |
| `input_tokens_limit` | `int \| None` | `None` | Max cumulative input tokens |
| `output_tokens_limit` | `int \| None` | `None` | Max cumulative output tokens |
| `total_tokens_limit` | `int \| None` | `None` | Max cumulative total tokens |

```python
from akgentic.llm import UsageLimits

UsageLimits(request_limit=10, total_tokens_limit=5_000)   # tight budget
UsageLimits(request_limit=None)                            # unlimited (no safety brake)
```

### RuntimeConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `retries` | `int` | `3` | Retry attempts for tool failures and output validation errors |
| `end_strategy` | `Literal["early","exhaustive"]` | `"exhaustive"` | Tool execution termination |
| `parallel_tool_calls` | `bool` | `True` | Concurrent tool execution when provider supports it |
| `http_client_config` | `HttpClientConfig` | `HttpClientConfig()` | HTTP timeout and retry tuning |

**End strategies:**
- `"early"` — stops after the first successful result (fast path)
- `"exhaustive"` — runs all tool calls even when a result is available (complete data gathering)

> **Note:** `parallel_tool_calls` is silently forced to `False` for providers without native
> structured output (google-gla, mistral, non-openai NVIDIA). See [Providers](#providers).

`HttpClientConfig` fields: `timeout=120.0`, `max_retries=5`, `backoff_multiplier=0.5`,
`backoff_max=60.0` — all configurable.

### ReactAgentConfig

Composes all three layers:

```python
from akgentic.llm import ReactAgentConfig, ModelConfig, UsageLimits, RuntimeConfig, HttpClientConfig

config = ReactAgentConfig(
    model_cfg=ModelConfig(
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        temperature=0.7,
    ),
    usage_limits=UsageLimits(
        request_limit=10,
        total_tokens_limit=50_000,
    ),
    runtime_cfg=RuntimeConfig(
        end_strategy="exhaustive",
        http_client_config=HttpClientConfig(timeout=180.0, max_retries=3),
    ),
)
```

## Providers

| Provider | `ModelConfig.provider` | Auth env var(s) | Native structured output |
|----------|------------------------|-----------------|--------------------------|
| OpenAI | `"openai"` | `OPENAI_API_KEY` | ✅ |
| Azure OpenAI | `"azure"` | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT` | ✅ |
| Anthropic | `"anthropic"` | `ANTHROPIC_API_KEY` | ✅ |
| NVIDIA NIM (openai/* models) | `"nvidia"` | `NVIDIA_API_KEY` | ✅ |
| NVIDIA NIM (other models) | `"nvidia"` | `NVIDIA_API_KEY` | ❌ |
| Google Gemini | `"google-gla"` | `GOOGLE_API_KEY` or `GOOGLE_APPLICATION_CREDENTIALS` | ❌ |
| Mistral AI | `"mistral"` | `MISTRAL_API_KEY` | ❌ |

Providers without native structured output use pydantic-ai's prompt-based extraction fallback.
`parallel_tool_calls` is automatically disabled for these providers to prevent malformed
tool-call responses.

```python
# NVIDIA NIM — openai-compatible model (native output)
ModelConfig(provider="nvidia", model="openai/gpt-oss-120b")

# NVIDIA NIM — non-OpenAI model (no native output)
ModelConfig(provider="nvidia", model="meta/llama-3.1-8b-instruct")
```

## ReactAgent API

```python
class ReactAgent:
    def __init__(
        self,
        config: ReactAgentConfig,
        deps_type: type[Any] | None = None,  # dependency injection type
        tools: list[Any] | None = None,       # tool functions
        toolsets: list[Any] | None = None,    # MCP server toolsets
        result_type: type[Any] = str,         # default output type
        observer: ContextObserver | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None: ...

    # Execution
    async def run(self, user_prompt: UserPrompt, deps=None, output_type=None) -> Any: ...
    def run_sync(self, user_prompt: UserPrompt, deps=None, output_type=None) -> Any: ...

    # Context
    @property
    def context(self) -> ContextManager: ...
    def subscribe_context(self, observer: ContextObserver) -> None: ...
    def checkpoint(self, checkpoint_id: str | None = None) -> ContextSnapshot: ...
    def rewind(self, checkpoint_id: str) -> None: ...

    # Dynamic prompts and tools (decorator API)
    def system_prompt(self, func: Any) -> Any: ...  # wraps @agent.system_prompt(dynamic=True)
    def tool(self, func: Any) -> Any: ...            # wraps @agent.tool()

    # Advanced
    @property
    def pydantic_agent(self) -> Agent[Any, Any]: ...  # access underlying pydantic-ai Agent
```

`output_type` in `run()` overrides the construction-time `result_type` for that call only.
Both are wrapped with `get_output_type()` to apply the provider-aware `NativeOutput` strategy.

## Multimodal Prompts

`UserPrompt = str | list[str | BinaryContent]` is the accepted type for `run()` and
`run_sync()`. Pass a mix of text strings and `BinaryContent` objects:

```python
from pydantic_ai import BinaryContent
from akgentic.llm import ReactAgent, ReactAgentConfig, ModelConfig

agent = ReactAgent(config=ReactAgentConfig(
    model_cfg=ModelConfig(provider="openai", model="gpt-4o")
))

with open("diagram.png", "rb") as f:
    image_bytes = f.read()

result = agent.run_sync([
    "Describe what is shown in this architecture diagram.",
    BinaryContent(data=image_bytes, media_type="image/png"),
])
```

`UserPrompt` is exported from `akgentic.llm` so consuming layers (`akgentic-agent`) can
annotate their own signatures without importing `pydantic_ai` directly.

> **Note:** Provider support for `BinaryContent` varies — passing an image to a non-vision
> model raises a provider-level error. Multimodal turns are not JSON-serializable and are
> treated as ephemeral (not persisted in history replay).

## Context Management

`ReactAgent` maintains a persistent `ContextManager` across calls. Message history is passed
as `message_history` on every `Agent.iter()` invocation, giving the LLM full conversation
continuity without manual history threading.

```python
agent = ReactAgent(config=config)

# First turn
agent.run_sync("Start the analysis.")

# Second turn — model sees the previous exchange
agent.run_sync("Now summarise your findings.")

# Checkpoint before a risky operation
snap = agent.checkpoint("before-migration")

try:
    agent.run_sync("Apply the database migration plan.")
except Exception:
    agent.rewind("before-migration")   # restore to known-good state
```

### ContextManager

```python
from akgentic.llm import ContextManager

# With optional sliding window (system messages always preserved)
ctx = ContextManager(max_messages=20)

ctx.add_message(msg)
ctx.checkpoint("id", metadata={"note": "pre-flight"})
ctx.rewind("id")
ctx.get_checkpoint("id")     # → ContextSnapshot | None
ctx.list_checkpoints()       # → list[str] in creation order
ctx.subscribe(observer)
ctx.unsubscribe(observer)
ctx.clear()
```

### Observer Pattern

```python
from akgentic.llm import (
    ContextObserver, LlmMessageEvent, LlmCheckpointCreatedEvent,
    LlmUsageEvent, ToolCallEvent, ToolReturnEvent,
)

class MyObserver:
    def notify_event(self, event: object) -> None:
        if isinstance(event, ToolCallEvent):
            print(f"Tool called: {event.tool_name} ({event.tool_call_id})")
        elif isinstance(event, ToolReturnEvent):
            status = "success" if event.success else "error"
            print(f"Tool returned: {event.tool_name} ({status})")
        elif isinstance(event, LlmUsageEvent):
            print(f"Usage: {event.model_name} — {event.input_tokens}in/{event.output_tokens}out")
        elif isinstance(event, LlmMessageEvent):
            print(f"New message: {event.message}")
        elif isinstance(event, LlmCheckpointCreatedEvent):
            print(f"Checkpoint created: {event.snapshot.checkpoint_id}")

agent = ReactAgent(config=config, observer=MyObserver())
# or: agent.subscribe_context(MyObserver())
```

Events: `LlmMessageEvent`, `LlmUsageEvent`, `LlmCheckpointCreatedEvent`,
`LlmCheckpointRestoredEvent`, `ToolCallEvent`, `ToolReturnEvent`.
Observers are notified synchronously — exceptions propagate to the caller.

### Tool Event Observability

`ToolCallEvent` and `ToolReturnEvent` are emitted by `ContextManager.add_message()` after
`LlmMessageEvent`, derived from the same message. They provide a clean observability interface
for tool activity without requiring consumers to parse pydantic-ai message internals.

**Part-kind → event mapping:**

| `part_kind` in message | Event emitted | Condition |
|---|---|---|
| `tool-call` | `ToolCallEvent` | One event per part (parallel calls → N events) |
| `tool-return` | `ToolReturnEvent(success=True)` | Always |
| `retry-prompt` | `ToolReturnEvent(success=False)` | Only when tool raised an error |

**Field semantics:**

- `tool_name` — identifies which tool was called; primary routing key in observer handlers
- `tool_call_id` — provider-assigned identifier; correlates a `ToolCallEvent` with its
  corresponding `ToolReturnEvent` within the same message stream
- `arguments` — raw JSON string from the provider. Use `json.loads(event.arguments)` for
  structured access. Stored as `str` to avoid coupling to tool-specific parameter schemas.
- `success` — `True` for clean returns; `False` when the tool raised an error (pydantic-ai
  emits a `retry-prompt` part in that case). The return content is not carried in
  `ToolReturnEvent`; it is already in the accompanying `LlmMessageEvent`.

**Emission ordering:** `LlmMessageEvent` always fires first. Tool events follow immediately.
A consumer receiving `ToolCallEvent` can safely assume the full message is already in context.

## Cost Tracking and Aggregation

`akgentic-llm` emits an `LlmUsageEvent` for every `ModelResponse` received from a provider.
These events carry per-request token counts and can be aggregated into hierarchical cost
summaries using `aggregate_usage()`.

### Pricing Table

Model pricing is externalized in `pricing.yaml` (bundled with the package), loaded once at
import time into the `PRICING` dict. It carries **26 rows**: Anthropic (Claude Sonnet 4,
Claude Opus 4), the OpenAI GPT-4o and GPT-4.1 families, the GPT-5 line through `gpt-5.5` with
its `-mini`, `-nano`, `-pro` and `-codex-mini` siblings, and the GPT-5.6 family (`luna`,
`sol`, `terra`). Every row carries per-1M-token rates for `input`, `output`, `cache_read`,
and `cache_write`.

**The input rate applies to the uncached remainder, not to `input_tokens`.** Providers report
`input_tokens` *inclusive* of the cached figures — the OpenAI chat API's `prompt_tokens`
already counts `prompt_tokens_details.cached_tokens` — so applying the input rate to the whole
of it would bill every cached token a second time, once there and once in its own term:

```
cost = ( max(0, input − cache_read − cache_write) × input_rate
       + output       × output_rate
       + cache_read   × cache_read_rate
       + cache_write  × cache_write_rate ) / 1_000_000
```

`max(0, …)` is a **guard** against a malformed usage row, not a rounding choice: it makes such
a row under-report rather than produce a negative cost that silently offsets other models in
the same summary.

`cache_write` equal to `input` in most OpenAI rows is deliberate rather than filler — those
providers bill a cache write at the ordinary input rate, and the two terms then cancel to
`(input − cache_read) × input_rate`. The three GPT-5.6 rows carry a genuine 1.25× surcharge
instead.

Each rate is a single scalar, so the table always charges the **base tier**; long-context
tiers are deliberately not modelled (ADR-024 §D3 records the condition under which that is
worth revisiting).

> **Corrected on this release line.** The previous implementation applied the input rate to
> the whole of `input_tokens`, billing each cached token twice. Figures it produced were
> overstatements — **3.18×** on a measured `gpt-5.4` turn (50 000 input tokens of which 43 000
> were cache reads, 800 output), and more at higher cache hit rates. Reported costs therefore
> **step down** wherever caching is active once this version is deployed.

Pricing resolution uses substring matching against model names (longest key first), so
versioned names like `"claude-sonnet-4-20250514"` match the `"claude-sonnet-4-20250514"`
key, `"gpt-4.1-mini-2025-12-11"` matches `"gpt-4.1-mini"` before `"gpt-4.1"`, `"gpt-5.4"`
resolves to the `gpt-5.4` row rather than to `gpt-5`, and `"gpt-5.4-mini"` to its own row
rather than to the flagship.

**Adding a row means adding the whole family.** Because the longest key wins, a flagship added
without its cheaper siblings silently bills them at the flagship's rate: `gpt-5.4-mini`
contains `gpt-5.4`, so with its own row missing it matches the flagship — and there is no
unknown-model signal, because a row *was* found. For each new key, ask which real model ids
contain it as a substring, and price every one of them.

### Aggregation

```python
from akgentic.llm import LlmUsageEvent, aggregate_usage

# Collect events from an observer
events: list[LlmUsageEvent] = my_observer.collected_events

# Aggregate totals and per-model breakdown
summary = aggregate_usage(events)
print(f"Total cost: ${summary.total_cost_usd:.4f}")
print(f"Input tokens: {summary.total_input_tokens}")
for model_name, usage in summary.by_model.items():
    print(f"  {model_name}: ${usage.estimated_cost_usd:.4f}")

# Include per-run breakdown
summary = aggregate_usage(events, by_run=True)
for run in summary.runs:
    print(f"Run {run.run_id}: ${run.total_cost_usd:.4f}")
```

### Data Models

| Model | Description |
|-------|-------------|
| `LlmUsageEvent` | Frozen dataclass emitted per `ModelResponse` — carries `run_id`, `model_name`, `provider_name`, token counts, and `requests` |
| `ModelUsage` | Aggregated tokens and estimated cost for a single model |
| `RunUsageSummary` | Per-run summary with per-model breakdown |
| `AgentUsageSummary` | Top-level summary with `by_model`, optional `runs`, and grand totals |

## Prompts

### PromptTemplate

Config-time `{placeholder}` rendering. Used by `AgentConfig.prompt` in `akgentic-agent`:

```python
from akgentic.llm import PromptTemplate

tpl = PromptTemplate(
    template="You are {role}.\n\nInstructions: {instructions}",
    params={"role": "the Librarian", "instructions": "Extract structured data."},
)
print(tpl.render())
# → "You are the Librarian.\n\nInstructions: Extract structured data."
```

### Dynamic System Prompts

Register callables that are evaluated fresh on every LLM call:

```python
from akgentic.llm import ReactAgent, ReactAgentConfig, ModelConfig
from akgentic.llm import current_datetime_prompt, json_output_reminder_prompt

agent = ReactAgent(config=ReactAgentConfig(
    model_cfg=ModelConfig(provider="openai", model="gpt-4o")
))

# Built-in utilities
agent.system_prompt(current_datetime_prompt)       # "The current date and time is …"
agent.system_prompt(json_output_reminder_prompt)   # reminder to output JSON only

# Custom prompt
@agent.system_prompt
def workspace_context(ctx: Any) -> str:
    return f"Working directory: {get_current_workspace()}"
```

## Development

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

### Setup

```bash
uv sync --all-packages --all-extras
```

### Commands

```bash
# Run tests
uv run pytest packages/akgentic-llm/tests/

# Run tests with coverage
uv run pytest packages/akgentic-llm/tests/ --cov=akgentic.llm --cov-fail-under=80

# Lint
uv run ruff check packages/akgentic-llm/src/

# Format
uv run ruff format packages/akgentic-llm/src/

# Type check
uv run mypy packages/akgentic-llm/src/
```

### CI Pipeline

Every pull request runs the full quality gate via GitHub Actions (`.github/workflows/ci.yml`):

| Step | Command | Gate |
|------|---------|------|
| Type check | `mypy packages/akgentic-llm/src/` (strict, Python 3.12) | Zero errors |
| Lint | `ruff check packages/akgentic-llm/src/` | Zero errors |
| Tests | `pytest packages/akgentic-llm/tests/ --cov=akgentic.llm --cov-fail-under=80` | All pass, ≥ 80% coverage |

The CI badge at the top of this README reflects the current state of `master`. PRs are
blocked from merging until all steps are green.

### Project Structure

```
src/akgentic/llm/
    __init__.py     # Public API exports
    agent.py        # ReactAgent, UsageLimitError, UserPrompt type alias
    config.py       # ModelConfig, UsageLimits, HttpClientConfig, RuntimeConfig, ReactAgentConfig
    context.py      # ContextManager, ContextSnapshot
    event.py        # LlmMessageEvent, LlmUsageEvent, LlmCheckpoint*Event,
                    #   ToolCallEvent, ToolReturnEvent, ContextObserver protocol
    pricing.py      # PRICING dict, ModelUsage, RunUsageSummary, AgentUsageSummary,
                    #   aggregate_usage()
    pricing.yaml    # Externalized per-1M-token pricing table — Anthropic plus the
                    #   OpenAI GPT-4o, GPT-4.1, GPT-5 and GPT-5.6 lines
    prompts.py      # PromptTemplate, current_datetime_prompt, json_output_reminder_prompt
    providers.py    # create_model(), create_http_client(), get_output_type(),
                    #   create_model_settings(), _supports_native_output()
tests/              # Tests organised by module
```

## License

See the repository root for license information.

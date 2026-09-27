# LLM providers routed per task, DeepSeek first, validated on our side

The model layer was Gemini only. V2 puts it behind a provider interface and routes
**each task** — capture review, organize, event detection, Digest prose, Priorities,
the agent — to a provider named in the config. DeepSeek is the default because it
is cheaper. Gemini is the fallback.

DeepSeek offers JSON mode but no response schema, so structured output is validated
on our side: parse with the same Pydantic models, retry once, then go to the
fallback. Every call records its tokens and cost per provider, which is what the
admin section of the Digest reports.

Speech-to-text is the exception to "remote models": Whisper runs locally on the
server, so audio never leaves the house.

## Considered Options

- **A classifier choosing the provider per message.** Rejected: it costs an extra
  call on every message, and it misroutes without anybody seeing it.
- **Routing by message length or content.** Rejected: a short message can be the
  hardest one to parse.
- **A local LLM on the server.** Rejected for now: 8 GB and a 6th-gen i5 run a 3B
  model, which handles Portuguese relative dates and the user's Priorities far worse
  than the hosted models. The interface leaves room for it.

## Consequences

- A malformed response is an expected path, not an exception. It has tests.
- Switching a task to another provider is a config change.

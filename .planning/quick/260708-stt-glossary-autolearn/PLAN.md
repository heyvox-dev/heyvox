# Feature: Auto-learn the STT glossary from session transcripts

**Created:** 2026-07-08 · **Status:** SCAFFOLDED (not built) · **Owner:** Franz

## Goal

Automatically keep the HeyVox STT glossary (`stt.local.initial_prompt` / `vocab_learner`)
current by periodically mining recent session transcripts for **domain-term
misrecognitions** and adding the correct spelling back to the glossary — so
`german-q4` recognises them on the first decode next time.

Concrete trigger (2026-07-08): the STT model wrote `"Google Doc" → "Dugel-Dog"`,
`"Threads" → "Frats"`. We added those manually. Franz wants this automated:
"regelmäßig Sessions durchlesen, merken 'das wurde falsch erkannt', wieder einbauen."

Model decision is settled: **stay on `german-q4` + glossary** (see
`[[project_stt_offline_bench_german_q4]]`); this feature is what makes staying on
the glossary sustainable without manual upkeep.

## Key finding: the "over-all-sessions" harness already exists (and is broken)

`~/Personal/Source/digital-life/mcp-servers/digital-life-sync/scripts/transcript_processor.py`
already does the exact scan-and-extract pipeline this feature needs:

- `find_transcripts()` → globs `~/.claude/projects/*/*.jsonl` (all Claude Code sessions)
- `parse_transcript()` + `extract_user_messages()` → Franz's dictated messages
- `filter_personal_projects()` → scope filter
- `FACT_EXTRACTION_PROMPT` + `extract_facts_with_llm()` → LLM pass (via Letta/Gemini agent)
- `deduplicate_facts()` + `store_fact_in_letta()` → dedup + persist
- `load_processed()`/`mark_processed()` → `.processed_transcripts.json` incremental state

**Status of the existing infra (all confirmed 2026-07-08):**
1. `transcript_processor.py` — **DORMANT since 2026-05-02** (`transcript_processor.log` last entry). The over-all-sessions run stopped ~2 months ago.
2. `launchd at.felberer.letta-safety-sync` (daily `StartCalendarInterval`, runs `run_letta_safety_sync.sh`) — **FAILING on the SSH tunnel** to the Letta host `100.85.92.66` (Tailscale): `ssh: connect ... port 22: Operation timed out`, `fail streak=3` (2026-07-05).
3. `letta_sync.py` — **works on-demand** (used successfully today by `/save-session`), has SSH-tunnel-auto-open + `~/.claude/letta_outbox.jsonl` fallback.

So Franz's "das funktioniert vielleicht alles nicht mehr" is **correct**: the batch
processor is dormant and the scheduled sync fails on the tunnel.

## Combine decision: share the HARNESS, keep DESTINATIONS independent

**Yes, combine — but only at the scan/schedule layer, not the persistence layer.**

- **Share:** transcript discovery, user-message extraction, per-file processed-state,
  scheduling (launchd), LLM-call plumbing. One scan of the sessions feeds N extractors.
- **Do NOT couple the destinations.** The Letta path needs the SSH tunnel to
  `100.85.92.66` and is flaky; the glossary path writes **locally** to
  `~/.config/heyvox/config.yaml` and needs no network. If glossary-learning is bolted
  onto the Letta pipeline it inherits the tunnel fragility and stops working exactly
  when the tunnel is down (i.e. often). Glossary extraction should use a **local/direct
  LLM call** (Anthropic API or a local model) and persist locally, independent of Letta.

Target shape: generalise `transcript_processor.py` into a **pluggable multi-extractor**:

```
scan sessions (once) ──▶ [extractor: facts]      ──▶ Letta   (needs tunnel; ok to fail)
                     └──▶ [extractor: glossary]  ──▶ HeyVox config (local; must not depend on Letta)
```

## Glossary extractor — design

1. **Input:** recent Claude Code transcripts (Franz's dictated messages + surrounding
   context — the context is what lets an LLM infer `"Dugel-Dog" = "Google Doc"`).
2. **LLM prompt:** "These are voice-dictated instructions to a coding agent. Identify
   likely STT misrecognitions of technical terms, product names, proper nouns, repos,
   or identifiers, and give the CORRECT spelling. Return `{wrong, correct, confidence}`."
3. **Merge into glossary:** add high-confidence `correct` terms to the HeyVox glossary.
   Respect the existing mechanics (see `[[project_stt_offline_bench_german_q4]]`,
   `[[project_languages_allowlist]]`): the `vocab_learner` (currently FREQUENCY-based,
   `max_terms=35`) + the **pinned-term** feature (bypasses the corpus_freq gate) + the
   hard ~220-token prompt budget. New terms should likely become **pinned** (they're
   user-confirmed identifiers, not frequency-organic). Dedup against existing terms.
4. **Safety:** only add terms that are plausibly identifiers/proper-nouns (avoid polluting
   the prompt with common German words); cap additions per run; log what was added.

Note the distinction from today's `vocab_learner`: it learns by **frequency**; this is
**misrecognition-driven** (needs the wrong→correct signal an LLM infers from context).
That signal is the new capability.

## Open questions / decisions for the build session

1. **Revive vs rebuild** `transcript_processor.py`? It's dormant 2 months — check whether
   the dormancy is just the failing tunnel (Letta host unreachable) or code rot. The
   glossary path doesn't need the tunnel, so it can run even while the Letta path is down.
2. **LLM for glossary extraction:** Letta/Gemini agent (shared, but tunnel-coupled) vs a
   direct Anthropic API call (independent, costs API budget) vs a local model. Recommend
   direct/local to keep glossary-learning independent of the flaky tunnel.
3. **Fix the launchd tunnel reliability** (`run_letta_safety_sync.sh` fail streak) —
   separate concern but it's why the automation is dead; worth fixing regardless.
4. **Write target:** pinned `vocab_learner` terms vs directly editing `initial_prompt`.
   Pinned is cleaner (survives vocab_learner's own re-ranking, has an existing store flag).
5. **Cadence:** weekly is probably enough (misrecognitions accumulate slowly).

## Tasks (for the build session)

1. Audit `transcript_processor.py` runnability today (dormancy cause; tunnel vs code).
2. Refactor its scan/state core into a reusable harness with pluggable extractors.
3. Implement the glossary extractor (prompt + local LLM + confidence filter).
4. Implement the glossary merge (pinned vocab_learner terms, dedup, token-budget guard, log).
5. Keep the glossary path fully local — no Letta/tunnel dependency.
6. (Optional, separate) fix the `letta-safety-sync` SSH-tunnel failure so the fact path revives too.
7. Schedule via launchd (reuse the existing agent pattern); weekly.
8. Tests: extractor prompt on the known cases (Google Doc/Dugel-Dog, Threads/Frats), merge dedup, token-budget cap.

## Cross-repo note

Spans two repos: `heyvox` (glossary target, config) and `digital-life` (transcript
harness). The harness generalisation lives in digital-life; the glossary extractor +
merge could live in either — leaning digital-life (co-located with the scan) with a thin
HeyVox config-writer, OR a `heyvox learn-glossary-from-sessions` CLI that imports the
shared harness. Decide in the build session.

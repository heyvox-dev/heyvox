# Handover — Auto-learn STT glossary from sessions

Paste this into a fresh session to resume. Full design: `PLAN.md` (same dir).

---

**Task:** Build a feature that periodically mines Claude Code session transcripts
for STT domain-term **misrecognitions** and adds the correct spelling to the HeyVox
glossary, so `german-q4` gets those terms right on the first decode. Trigger case:
`"Google Doc"→"Dugel-Dog"`, `"Threads"→"Frats"` (added manually 2026-07-08; automate it).

**Read first (in order):**
1. `.planning/quick/260708-stt-glossary-autolearn/PLAN.md` — the design, the combine
   decision, and the open questions. Don't re-derive; it's all there.
2. Memory `project_stt_offline_bench_german_q4` — why german-q4+glossary is the settled
   model choice, the glossary mechanics (token budget, pinned terms), and DEF-195.
3. `~/Personal/Source/digital-life/mcp-servers/digital-life-sync/scripts/transcript_processor.py`
   — the EXISTING session-scan + LLM-extract harness to reuse (it already globs all
   `~/.claude/projects/*/*.jsonl`, extracts user messages, LLM-extracts, dedups, persists).

**Three facts that shape the build:**
1. The over-all-sessions harness already exists (`transcript_processor.py`) but is
   **DORMANT since 2026-05-02**. Don't build a new scanner — generalise this one.
2. The scheduled `launchd at.felberer.letta-safety-sync` is **FAILING** on the SSH
   tunnel to the Letta host `100.85.92.66` (timeout). That's why the automation is dead.
3. **Combine at the harness level only.** Share scan/schedule/processed-state; keep the
   glossary destination **local + independent of the Letta tunnel** (write to
   `~/.config/heyvox/config.yaml`, use a direct/local LLM call). Coupling glossary-learning
   to the flaky Letta path would make it fail whenever the tunnel is down.

**First step:** audit whether `transcript_processor.py`'s dormancy is just the failing
tunnel or code rot (the glossary path won't need the tunnel), then refactor its scan/state
core into a pluggable multi-extractor and add the glossary extractor. Task list in PLAN.md §Tasks.

**Glossary write mechanics:** new terms should become **pinned** `vocab_learner` entries
(user-confirmed identifiers, not frequency-organic), deduped, under the ~220-token prompt
budget. `vocab_learner` today is frequency-based; the misrecognition (wrong→correct)
signal is the new capability.

**Scope note:** this is HeyVox (glossary) × digital-life (harness). Decide extractor
placement in the build session (PLAN.md §Cross-repo note). Scaffolding only so far — nothing built.

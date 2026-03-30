# Vox — Voice Layer for AI Coding Agents

**Author:** Franz Felberer
**Date:** 2026-03-26
**Status:** Draft / Internal

---

## One-Liner Pitch

> **Vox turns your voice into a first-class input device for AI coding agents — wake word, push-to-talk, local STT, TTS orchestration, zero cloud dependency.**

---

## Part 1: Market Research

### 1.1 Existing Competitors (Updated March 2026)

#### Voice-to-Text for Developers

| Tool | Type | Price | Strengths | Weaknesses |
|------|------|-------|-----------|------------|
| **SuperWhisper** | macOS local STT | $8.49/mo, $84.99/yr, lifetime $849 | Local Metal GPU, cross-device (Mac/Win/iOS/iPad), meeting transcription | No wake word, no agent integration. Lifetime price 3x'd from $249 to $849 |
| **Wispr Flow** | Cloud STT | $15/mo or $144/yr | $30M Series A funding, Cursor/Windsurf/Replit integration, dev jargon recognition, Command Mode references files/variables | Cloud-only (no offline), privacy concerns, no wake word |
| **Spokenly** | Free BYOK + MCP | Free (local models or BYOK API keys) | **MCP integration with Claude Code, Cursor, Codex CLI** — agent can call `ask_user_dictation` tool. Developer-focused. | New entrant, unclear maturity. No wake word, no TTS |
| **OpenWhispr** | OSS cross-platform | Free | Whisper + NVIDIA Parakeet, AI agent mode, meeting transcription, notes system, Google Calendar integration | Feature bloat risk, cross-platform = less macOS polish |
| **macOS Dictation** | Built-in | Free | On-device, zero setup | No customization, no wake word, no API |

#### Voice Coding (Code-Specific)

| Tool | Type | Price | Strengths | Weaknesses |
|------|------|-------|-----------|------------|
| **Talon Voice** | Hands-free coding | Free (core) | Full IDE control, powerful grammar, ~100K Slack members | Steep learning curve (weeks-months), not designed for AI agent chat |
| **Cursorless** | Structural voice editing | Free (OSS) | VS Code integration, precise | Requires Talon, subset of Talon users |
| **VS Code Speech** | VS Code extension | Free | Local STT, "Hey Code" wake word, integrated with Copilot Chat | VS Code only, basic features, no multi-agent |
| **Agent Voice** (new!) | VS Code extension | Free | **Full-duplex voice with Copilot Agents** via Azure OpenAI GPT-Realtime | Azure-dependent (cloud), VS Code only, early stage |
| **Cursor 2.0 Voice** | Built into Cursor | Free (w/ Cursor) | Native voice mode, commands like "refactor this to async/await", AI patch proposals | Cursor-only, no wake word, no TTS output |

#### Building Blocks (Not Products)

- **whisper.cpp / MLX Whisper** — open-source STT engines, no product layer
- **Buzz / MacWhisper** — GUI wrappers for Whisper, no agent integration
- **Cloud STT APIs** (OpenAI, Google, Deepgram) — used by apps, not directly by devs

#### Key Competitive Shifts Since 2025

1. **Spokenly's MCP approach** is the closest competitor — free, developer-focused, integrates with Claude Code/Cursor via MCP protocol. BUT: no wake word, no TTS, no orchestration.
2. **Cursor 2.0 has built-in voice** — reduces the "no voice for AI agents" gap, but only for Cursor users.
3. **Agent Voice (VS Code)** — full-duplex voice with Copilot agents is the exact vision, but Azure-dependent and VS Code-only.
4. **Wispr Flow** has $30M funding and Cursor integration — well-funded competitor moving into dev space, but cloud-only.
5. **SuperWhisper** tripled its lifetime price ($249→$849) — signals strong demand for local STT.

### 1.2 Market Size & Trends

**AI Coding Assistant Adoption (as of early 2025):**
- GitHub Copilot: ~1.8M+ paid subscribers
- Cursor: 500K-1M+ users (Anysphere valued ~$2.5B)
- Claude Code: growing rapidly in power-user segment
- Stack Overflow 2024: ~76% of developers using or planning to use AI tools
- AI-assisted coding market: $5-10B in 2025, projected $30-50B by 2030

**Voice in Developer Tools: Extremely Low Adoption**
- No mainstream AI coding assistant has a mature voice interface
- Voice coding (Talon/Cursorless): ~10-20K users globally
- BUT: shift from autocomplete to chat-based agents creates a new opening for voice
- Talking to an agent is a natural voice use case (unlike writing code by dictating syntax)

**Accessibility Market:**
- ~7-10% of developers report RSI or repetitive strain (~2-3M globally)
- Talon Voice is the only serious option, requires massive learning investment
- Massively underserved, vocal community, great for word-of-mouth

### 1.3 Key Market Gaps (Revised)

| Gap | Description | Who's Closest? | Our Edge |
|-----|-------------|----------------|----------|
| **Wake word → Agent pipeline** | Hands-free: wake word → record → STT → paste → submit | VS Code Speech has "Hey Code" but only for VS Code | Works with ANY agent, custom wake words |
| **Bidirectional Voice (TTS output)** | AI agent reads responses aloud, coordinates with mic input | Agent Voice (Azure GPT-Realtime) — cloud, VS Code only | Local TTS, audio ducking, queue management |
| **Multi-Agent Orchestration** | Voice-switch between multiple concurrent AI agents | Nobody | Unique differentiator |
| **Local STT + Agent Integration** | Privacy-first STT that targets specific agent apps | Spokenly (MCP, free, BYOK) is closest | Wake word, TTS, deeper integration |
| **Cross-Agent Voice Layer** | One voice tool for Conductor + Cursor + Claude Code + terminal | Spokenly (MCP for multiple agents) | TTS, wake word, orchestration beyond just input |

**Reality check:** The "nobody does this" claims from our initial research were partially wrong. Spokenly (MCP), Cursor 2.0 (built-in voice), and Agent Voice (full-duplex) have closed some gaps. Our remaining unique advantages are:
1. **Wake word** (hands-free, not just push-a-button)
2. **Bidirectional voice with local TTS** (not cloud-dependent)
3. **Multi-agent orchestration** (workspace switching, TTS routing)
4. **Fully local** (MLX Whisper + Kokoro, zero cloud)

### 1.4 Pricing Landscape

| Category | Range |
|----------|-------|
| Voice/STT tools | Free — $10/mo |
| Developer productivity tools | $10-20/mo |
| AI coding assistants | $10-20/mo (individual), $19-50/mo (business) |
| Sweet spot for Vox | **$10-12/mo** |

---

## Part 2: Product Concept

### 2.1 Product Name

**"Vox" is taken:** Homebrew cask (music player), PyPI (`vox-core` exists), multiple GitHub repos. Need alternatives.

| # | Name | CLI Feel | Notes |
|---|------|----------|-------|
| 1 | **Hotmic** | `hotmic start` | Fun, implies always-ready, unique. Easy to remember. |
| 2 | **Murmur** | `murmur start` | Poetic, low-profile, developer-friendly. |
| 3 | **Voxdev** | `voxdev start` | Clear positioning, likely available. |
| 4 | **Saydev** | `saydev start` | Simple, memorable, ".dev" domain possible. |
| 5 | **Talkdev** | `talkdev setup` | Self-explanatory, SEO-friendly. |
| 6 | **Speakeasy** | `speakeasy on` | Voice input made easy. May have conflicts. |
| 7 | **Voiceloop** | `vloop start` | Describes the bidirectional voice loop (unique feature). |

**Action needed:** Check availability on GitHub, PyPI, Homebrew, npm, and .dev/.sh domains before deciding.

### 2.2 Core Value Proposition

Voice input for AI coding is not a dictation problem — it's an **orchestration** problem. You need the mic, the STT, the app targeting, the TTS coordination, the visual feedback, and the command vocabulary to all work together as one system. **That system doesn't exist today.**

| Capability | macOS Dictation | SuperWhisper | Wispr Flow | Spokenly | Cursor Voice | **Ours** |
|-----------|----------------|-------------|-----------|----------|-------------|---------|
| Wake word (hands-free) | No | No | No | No | No | **Yes** |
| Push-to-talk | No | Hotkey | Hotkey | Hotkey | Built-in | **Yes (fn key)** |
| Local STT (Metal GPU) | No | Yes | No (cloud) | BYOK/local | Unknown | **Yes** |
| AI agent targeting + auto-submit | No | No | Cursor only | MCP (multi) | Cursor only | **Yes (any app)** |
| Voice commands (skip/mute TTS) | No | No | No | No | No | **Yes** |
| TTS output from agents | No | No | No | No | No | **Yes** |
| TTS coordination (don't talk over user) | No | No | No | No | No | **Yes** |
| Recording indicator (fullscreen) | No | No | No | No | No | **Yes** |
| Custom wake words (trainable) | No | No | No | No | No | **Yes** |
| Multi-agent workspace switching | No | No | No | No | No | **Yes** |
| MCP integration | No | No | Yes | **Yes** | No | Not yet |
| Cross-platform | No | Yes (new) | Mac only | Mac only | Mac/Win/Linux | Mac only |
| Price | Free | $8.49/mo | $15/mo | Free | Free w/ Cursor | Free (OSS) |

### 2.3 Target Audiences

| Audience | Size | Pain Point | Willingness to Pay |
|----------|------|-----------|-------------------|
| **Power users with multiple AI agents** | Tens of thousands, growing fast | Switching between agents, repetitive typing | High ($15-30/mo) |
| **Developers with RSI** | ~2-3M globally | Typing causes pain; Talon too complex | Very high |
| **Hands-free workflow enthusiasts** | Niche, growing | Want to code from treadmill, standing, etc. | Moderate ($10-15/mo) |
| **Enterprise AI teams** | Emerging | Managing 5-20+ agents across projects | Enterprise pricing |

### 2.4 Product Tiers

#### Free / Open Source (MIT)

Everything for single-user, single-agent voice input:
- Wake word detection (openwakeword)
- Push-to-talk (configurable modifier key)
- Local STT (MLX Whisper + sherpa-onnx fallback)
- Text injection into focused app
- Recording indicator overlay
- Audio cue feedback
- Silence timeout + cancellation
- CLI (`heyvox start|stop|restart|status|logs`)
- YAML configuration
- launchd service
- Adapter interface for AI agents

#### Pro ($12/month or $99/year)

| Feature | Why It's Worth Paying For |
|---------|--------------------------|
| **Native macOS app** (menubar, GUI prefs, guided setup) | Eliminates 15-min manual terminal setup |
| **Multi-agent workspace orchestration** | TTS routing, voice-activated workspace switching |
| **Built-in TTS engine** (Kokoro integration) | Queue management, audio ducking, pause-during-recording |
| **Voice command extensions** | Custom commands via config |
| **Whisper model management** | One-click download/update |
| **Smart transcription** | Context-aware, project vocabulary |
| **Priority support** | macOS permission troubleshooting |

**Lifetime option:** $199 (early bird $149)

### 2.5 Architecture for Decoupling

#### Current: Tightly Coupled to Conductor

Hardcoded: target_app, TTS paths, `/tmp/claude-ww-recording`, Enter count, bundle IDs.

#### Target: Adapter Architecture

```
Vox Core (wake word + STT + audio)
         |
    Dispatcher
         |
  +------+------+--------+
  |      |      |        |
Conductor Cursor Terminal  Custom
Adapter  Adapter Adapter  Adapter
```

#### Adapter Protocol

```python
class AgentAdapter(Protocol):
    name: str
    def focus(self) -> bool         # bring window to front
    def inject_text(self, text: str) -> bool  # paste text
    def submit(self) -> bool        # press Enter/submit
    def is_active(self) -> bool     # is window focused?
    # Optional TTS hooks
    def pause_tts(self) -> None
    def resume_tts(self) -> None
    def handle_voice_command(self, cmd: str) -> bool
```

#### Platform Portability

| Component | macOS-Only | Cross-Platform |
|-----------|-----------|----------------|
| openwakeword | | Yes (pure Python) |
| MLX Whisper | Yes (Metal) | No — use sherpa-onnx |
| Quartz event tap (fn key) | Yes | pynput (Linux/Win) |
| Recording indicator (AppKit) | Yes | Tkinter/Qt |
| Text injection (osascript) | Yes | xdotool / pyautogui |
| Audio cues (afplay) | Yes | sounddevice |
| launchd | Yes | systemd / Task Scheduler |

**Strategy:** macOS-first (where paying dev audience is), add Linux in v2.

### 2.6 Installation UX

#### Current: 15-Minute Manual Setup (Barrier to Adoption)

Clone, pyenv, pip install, brew install portaudio, 3 permission grants, fn key setting, install.sh, ww start.

#### Target: 3-Minute Guided Setup

**Homebrew (OSS):**
```bash
brew install vox-voice
heyvox setup    # interactive guided setup
heyvox start
```

`heyvox setup` handles: portaudio, bundled Python, model download, permission deep-links with verification, fn key config, mic test, wake word test.

**Native .app (Pro):**
`.dmg` → guided SwiftUI wizard → permissions with screenshots → model download → agent selection → menubar icon → done.

### 2.7 MVP Scope

#### v1.0 — "It Works for Anyone" (8-12 weeks)

| Feature | Status | Work Needed |
|---------|--------|-------------|
| Wake word detection | Done | Threshold tuning |
| Push-to-talk | Done | Document fn key setting |
| Local STT (MLX Whisper) | Done | Setup automation |
| Generic text injection | 80% | Remove Conductor hardcoding |
| Recording indicator | Done | Remove bundle ID check |
| Audio cues | Done | License check |
| Silence timeout | Done | — |
| CLI (rename to `vox`) | Done | Rename + add `heyvox setup` |
| YAML config | Done | Add adapter selection |
| Homebrew formula | Not started | Write formula + tap |
| `heyvox setup` installer | Not started | Interactive setup |
| 3 adapters | Not started | Conductor, Cursor, Generic |
| README + landing page | Partial | Rewrite for public |

**v1 does NOT include:** native .app, TTS, GUI, multi-workspace, cross-platform.

#### v2.0 — "The Pro Product" (6 months post-v1)

Native macOS app, TTS engine, multi-agent management, voice command extensions, model manager, auto-update.

#### v3.0 — "Platform" (12 months)

Linux support, plugin marketplace, team features, API/SDK.

### 2.8 Distribution

| Channel | Tier | Priority |
|---------|------|----------|
| **GitHub** (MIT) | OSS | Must-have |
| **Homebrew tap** | OSS | Must-have for v1 |
| **Direct .dmg + Lemon Squeezy** | Pro | Must-have |
| **Mac App Store** | Pro | Unlikely — sandboxing blocks Accessibility API |
| **Setapp** | Pro | After direct sales |

### 2.9 Revenue Projections (Conservative)

| Metric | Month 6 | Month 12 | Month 24 |
|--------|---------|----------|----------|
| GitHub stars | 500 | 2,000 | 5,000 |
| OSS monthly active | 200 | 1,000 | 3,000 |
| Pro subscribers | 20 | 100 | 500 |
| MRR | $240 | $1,200 | $6,000 |
| ARR | $2,880 | $14,400 | $72,000 |

This is a **lifestyle business**, not a VC-scale startup.

**Alternative: Sponsorware Model** — all features OSS, GitHub Sponsors ($12/mo) get 30-day early access. Simpler, builds goodwill. Works well with solo maintainer + dev audience.

### 2.10 Risks (Updated)

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Spokenly (MCP approach)** is already free + dev-focused | High | Differentiate on wake word, TTS, orchestration. Consider MCP adapter too. |
| **Cursor 2.0 built-in voice** reduces gap | High | Position as cross-agent, not Cursor-specific. Cursor users are only one segment. |
| **Wispr Flow** ($30M funding) moves into dev space | High | They're cloud-only. Our local-first + TTS is a different value prop. |
| AI agents add built-in voice (trend accelerating) | High | Value = ONE layer across ALL agents. Per-agent voice is fragmented. |
| macOS permission changes | Critical | Abstract permission checks, test on every macOS beta |
| OSS core is "good enough" | High (#1 risk) | Make native app dramatically better UX |
| Too niche market | Medium | RSI devs + multi-agent power users. Market growing fast. |
| Solo maintainer burnout | Critical | Keep scope small, charge early, don't overbuild |
| **"Vox" name unavailable** | Low | Multiple alternatives ready (Hotmic, Murmur, etc.) |

**New strategic consideration:** Spokenly's MCP approach is elegant — the agent calls a voice tool when it needs input. This is complementary, not competitive with our wake-word approach. We could **also offer MCP integration** alongside wake word + PTT. The combination would be unique.

### 2.11 What Makes This Defensible

1. **Integration depth** — each adapter is edge-case-hardened
2. **Custom wake word training pipeline** — already built, creates user investment
3. **macOS platform expertise** — months of Quartz/AppKit/launchd knowledge
4. **TTS orchestration** — nobody else has bidirectional voice for AI coding
5. **Community** — shared adapters, wake words, voice command configs

---

## Go-to-Market

1. **Weeks 1-2:** Clean up repo, decouple from Conductor, adapter protocol
2. **Weeks 3-4:** Homebrew formula, `heyvox setup`, demo video
3. **Week 5:** Soft launch — Claude Code Discord, r/ClaudeAI, r/LocalLLaMA
4. **Week 6:** Show HN with demo video
5. **Weeks 7-8:** Iterate on feedback
6. **Month 3:** Pro waiting list
7. **Month 4-6:** Ship Pro v1

**Content:**
- 2-min demo video (voice workflow side-by-side with typing)
- Blog: "I replaced typing with voice for AI coding — here's what I learned"
- Short clips on X/Twitter

---

## Decision: Should You Build This?

**Yes, if:**
- You believe voice for AI coding goes mainstream in 2-3 years
- You're comfortable with lifestyle business ($50-100K ARR ceiling)
- You enjoy macOS platform engineering
- You want to build in public with strong OSS core

**No, if:**
- You need immediate revenue (market is early)
- You want VC-scale returns (too niche without expanding beyond devs)
- You'd rather build for web/cloud (this is deeply native)

**Recommended next step:** Ship OSS core as v1 within 8 weeks. Validate with 100 GitHub stars and 20 active users before investing in Pro native app.

---

## Part 3: Codebase Audit — What Needs to Change

### 3.1 Coupling Points (All Conductor/Personal References)

| Location | Line(s) | What | Severity |
|----------|---------|------|----------|
| `wake_word_listener.py` | 284-288 | `/Users/work/.claude/hooks/tts-ctl.sh` x5 voice command lambdas | **HIGH — crashes if missing** |
| `ww` (CLI) | 10 | `$HOME/.claude/hooks/tts-ctl.sh` TTS control path | **HIGH — TTS commands fail** |
| `wake_word_listener.py` | 408,431,681,888 | `/tmp/claude-ww-recording` IPC flag file | Medium — name-only |
| `recording_indicator.py` | 22 | `com.conductor.app` bundle ID | Medium — fails silently |
| `recording_indicator.py` | 30 | `"conductor"` window owner name | Medium — fails silently |
| `wake_word_listener.py` | 60 | Default `target_app: "Conductor"` | Low — config overrides |
| `config.yaml` | 25 | `target_app: "Conductor"` | Low — user-editable |
| `config.yaml` | 77-79 | Personal mic list (Jabra, G435) | Low — cosmetic |
| `install.sh` | 68 | SuperWhisper reference in output | Low — cosmetic |

**Only 2 high-severity items** — both are the hardcoded `tts-ctl.sh` path. Everything else fails gracefully.

### 3.2 Undeclared Dependencies

Missing from `pyproject.toml`:
- `mlx-whisper` (the default STT engine!)
- `sherpa-onnx` (fallback STT)
- `pyobjc-framework-Cocoa` (recording indicator)
- `pyobjc-framework-Quartz` (fn key PTT)

### 3.3 Decoupling Priority Order

**Phase 1: Make it not crash for non-Conductor users (1-2 days)**
1. Make TTS script path configurable in config.yaml (or gracefully disable voice commands when missing)
2. Rename IPC flag to `/tmp/heyvox-recording`
3. Change `target_app` default to empty (paste into focused app)
4. Fix `recording_indicator.py` to accept `--bundle-id` arg, default to main screen

**Phase 2: Rebrand (1 day)**
5. Rename package in pyproject.toml
6. Rename launchd label to `com.heyvox.listener`
7. Rename CLI from `ww` to chosen name
8. Rename log files

**Phase 3: Productionize (1-2 weeks)**
9. Declare all runtime dependencies properly
10. Make voice commands user-configurable in config.yaml
11. Strip personal mic priority from default config
12. Remove SuperWhisper backend code
13. Module restructuring (wake_word_listener.py → vox/main.py)

### 3.4 What's Already Generic (Good News)

~90% of the codebase is already agent-agnostic:
- Wake word detection loop
- Mic management + device priority
- Silence timeout + cancellation
- PTT via Quartz event tap
- Local STT (MLX + sherpa-onnx)
- Audio cue system
- Recording indicator UI
- Clipboard paste mechanism
- Log rotation
- launchd service structure

### 3.5 Dependency Graph

```
config.yaml
    |
    v
wake_word_listener.py  (reads config, runs main loop)
    |
    |---> recording_indicator.py  (subprocess, SIGKILL to stop)
    |---> afplay                  (audio cues)
    |---> osascript               (clipboard, keystroke, app focus)
    |---> /tmp/heyvox-recording      (IPC flag to TTS process)
    |---> tts-ctl.sh              (voice command actions — needs config path)
    |
    +-- openwakeword   (wake word)
    +-- pyaudio        (mic input)
    +-- mlx_whisper    (STT — Metal GPU)
    +-- Quartz         (PTT fn key)

CLI (ww → vox)
    |---> launchctl    (service control)
    |---> tts-ctl.sh   (skip/mute/quiet/replay — needs config path)

install.sh ---> brew (portaudio) + pip (deps) + CLI install
```

---

## Part 4: OSS Launch Playbook

### 4.1 Name Availability (Action Items)

"Vox" is taken on Homebrew (music player cask), PyPI (`vox-core` exists), and likely GitHub/domains.

**Check commands:**
```bash
pip index versions <name> 2>/dev/null
brew search /^<name>$/
gh api users/<name> 2>/dev/null | jq '.login, .type'
whois <name>.dev | grep -i "registrant\|no match"
```

**Strong alternatives:**

| Name | CLI | Rationale |
|------|-----|-----------|
| **heyvox** | `heyvox start` | Matches wake word pattern, brandable, likely available |
| **voxcode** | `voxcode start` | Clear: voice + code |
| **hotmic** | `hotmic start` | Fun, implies always-ready |
| **hark** | `hark start` | "Listen" in archaic English, 4 letters |
| **murmur** | `murmur start` | Evocative, developer-friendly |

**Strategy:** Use `vox` as brand, `voxcode` or `heyvox` as package name on registries. CLI command can still be short.

Check ALL of these before deciding:
- [ ] GitHub org/repo
- [ ] PyPI
- [ ] Homebrew
- [ ] npm
- [ ] Domain: .dev, .sh, .app
- [ ] Twitter/X handle

### 4.2 README Structure (for dev tools that get stars)

Based on successful dev tool launches (Starship, Zoxide, Atuin, etc.):

1. **One-liner + badges** (what it is in one sentence)
2. **Demo GIF/video** (15-30 seconds, shows the magic moment)
3. **Quick install** (`brew install <name>` — three lines max)
4. **Feature list** (with checkmarks, short)
5. **How it works** (architecture diagram)
6. **Configuration** (show config.yaml example)
7. **Adapters** (Conductor, Cursor, Generic Terminal)
8. **Requirements** (macOS, Apple Silicon, permissions)
9. **FAQ** (privacy, "is my audio sent anywhere?", permissions)
10. **Contributing** (link to CONTRIBUTING.md)

### 4.3 Demo Video

- **Length:** 60-90 seconds
- **Format:** Terminal + screen recording side-by-side
- **Show:** Wake word trigger → recording → STT → paste into Claude Code → response → TTS reads it back
- **End with:** "Fully local. Zero cloud. Your voice never leaves your Mac."

### 4.4 Distribution Strategy

**Phase 1 (launch):** `pipx install` + `install.sh` script — avoids Homebrew formula complexity
```bash
brew install portaudio pipx
pipx install heyvox  # or whatever name
heyvox setup         # guided permission + model setup
```

**Phase 2 (100+ stars):** Create Homebrew tap (`brew tap you/heyvox && brew install heyvox`)

**Phase 3 (500+ stars):** Submit to homebrew-core

**Homebrew formula notes:**
- Use `Language::Python::Virtualenv` pattern (like httpie, ansible, yt-dlp)
- `depends_on "portaudio"` for PyAudio
- Use `poet` tool to auto-generate resource blocks: `pip install homebrew-pypi-poet && poet --formula heyvox`
- MLX Whisper: make it a runtime download on first use (models download on first run is standard)

### 4.5 Launch Sequence

**Pre-launch checklist:**
- [ ] Name finalized, all registries claimed
- [ ] README: demo GIF at top, one-line install, feature table
- [ ] 60-90s demo video on YouTube
- [ ] Blog post written (submit URL to HN)
- [ ] Install flow tested on 3-5 beta testers' machines
- [ ] GitHub Discussions enabled (Announcements, Q&A, Ideas, Show & Tell)
- [ ] Basic GitHub Actions CI (lint + test)
- [ ] No personal paths, no hardcoded configs, no secrets in repo

**Launch week:**

| Day | Action |
|-----|--------|
| Tue 9am ET | Show HN goes live. Be in comments all day. |
| Tue 11am | Tweet thread with demo video. Tag @AnthropicAI @cursor_ai |
| Tue 2pm | r/commandline (380K), r/MacOS (500K+) |
| Wed | r/LocalLLaMA (400K+), r/ClaudeAI, r/CursorAI |
| Thu | Dev.to / Hashnode blog post. Discord servers. |
| Fri | Thank contributors, address issues, tag v0.1.0 |

**Show HN title:** `Show HN: [Name] – Voice layer for AI coding agents (local STT, macOS)`

**Growth milestones:**

| Stars | Action |
|-------|--------|
| 100 | Create Homebrew tap |
| 250 | Submit to awesome-cli-apps, awesome-macos lists |
| 500 | Submit to homebrew-core, consider Discord |
| 1000 | Write "Lessons from launching" post (second HN wave) |
| 2500 | Linux port (biggest feature request to expect) |

### 4.6 Accessibility Outreach

**Position as complementary to Talon, not competitive:**
- Talon = full input replacement for coding
- This tool = voice interface specifically for AI agent interaction
- Message: "If you use Talon for coding, this adds AI agent voice control"

**Communities:**

| Community | How to Engage |
|-----------|---------------|
| **Talon Voice Slack** (~100K) | Share as complementary tool, ask for feedback |
| **r/RSI** (40K+) | Personal story: "I built this because of RSI" |
| **r/accessibility** (30K+) | Technical angle: local processing protects privacy |
| HN RSI stories | RSI stories consistently hit front page |

**Key voices:** Emily Shea (@yomilly, voice coding talks), Josh Comeau (discussed RSI openly), Ryan Hileman (Talon creator)

### 4.7 Content Strategy (Solo Dev)

**Demo video (launch):** 60-90s, show full wake word → speak → code appears workflow. No face cam needed.

**Blog post (Show HN):** Engineer-to-engineer tone. Include architecture decisions, tradeoffs, gotchas (Bluetooth A2DP has no mic, etc.). HN respects technical honesty.

**Twitter/X:** 2-3 tweets/week with visuals. Demo clips get highest engagement. Use #BuildInPublic, #DevTools, #VoiceCoding.

**Tweet template that works:**
> "Talk to Claude Code without touching the keyboard.
> Say 'Hey Jarvis' → speak your prompt → it types and sends automatically.
> 100% local. ~0.5s latency. Open source.
> [15-second video clip]"

### 4.8 License

**MIT** for OSS core (maximum adoption — same as Starship, Zoxide, Atuin). Pro features in separate closed-source repo. No CLA at launch (deters early contributors).

---

## Part 5: Strategic Summary

### Unique Position (post competitive update)

```
                    Wake Word    Local STT    TTS Output    Multi-Agent    MCP
SuperWhisper           ✗            ✓             ✗             ✗          ✗
Wispr Flow             ✗            ✗             ✗             ✗          ✗
Spokenly               ✗            ✓             ✗             ✗          ✓
Cursor Voice           ✗            ?             ✗             ✗          ✗
VS Code Speech         ✓            ✓             ✗             ✗          ✗
Agent Voice            ✗            ✗             ✓(cloud)      ✗          ✗
THIS PROJECT           ✓            ✓             ✓(local)      ✓          planned
```

### The Moat

Nobody combines wake word + local STT + local TTS + multi-agent orchestration. Individual features are being added by competitors (Spokenly=MCP, Cursor=voice, Agent Voice=duplex), but the **full loop** remains unique.

### Biggest Risk

Market timing. Voice-for-AI-agents is emerging but not mainstream yet. Ship fast, build community early, iterate based on what users actually want.

### Decision Framework

| If you want... | Do this |
|----------------|---------|
| Validate quickly | Phase 1 decoupling (2 days) → GitHub + Show HN |
| Build a product | Full rebrand + Homebrew (4 weeks) → launch |
| Maximize revenue | Add MCP adapter + Cursor adapter → capture Spokenly/Cursor audiences |
| Stay focused | Just ship wake word + PTT + local STT. TTS and orchestration as v2. |

---

## Part 6: MCP Voice Server Architecture

### 6.1 The Key Insight

Instead of writing adapters for each AI tool, build ONE MCP server. Every MCP-compatible tool (Claude Code, Cursor, VS Code Copilot, Claude Desktop) connects automatically.

### 6.2 The Hybrid Model

- **Voice IN:** OS-level (wake word → STT → osascript paste). Bypasses MCP, works with ANY app.
- **Voice OUT:** MCP tool (`voice_speak`). The LLM decides when to speak.
- **Voice HUD:** Independent process, receives state from both via Unix socket.

Why hybrid? MCP has **no "inject user message" primitive**. Voice input → user prompt requires OS-level text injection. MCP is designed for LLM→tool calls, not tool→user messages.

### 6.3 MCP Tools

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("voice")

@mcp.tool()
async def voice_listen(mode="wake_word", timeout_secs=30.0, auto_enter=False) -> str:
    """Start listening. Returns transcribed text when done."""

@mcp.tool()
async def voice_speak(text: str, voice="default", priority="normal") -> str:
    """TTS playback. Auto-ducks if recording. Returns status."""

@mcp.tool()
async def voice_status() -> dict:
    """Current state: idle, listening, recording, transcribing, speaking."""

@mcp.tool()
async def voice_queue(action="list") -> str:
    """Manage TTS queue: list, skip, stop, clear, mute, unmute."""

@mcp.tool()
async def voice_config(setting=None, value=None) -> dict:
    """Get or set voice configuration."""
```

### 6.4 MCP Resources

```python
@mcp.resource("voice://status")        # Current state
@mcp.resource("voice://transcript/latest")  # Last transcription
@mcp.resource("voice://config")        # Current configuration
```

### 6.5 Client Support Matrix

| Client | Tools | Resources | Notes |
|--------|-------|-----------|-------|
| **Claude Code** | Yes | Yes | Primary target |
| **Cursor** | Yes | No | Tools only |
| **Claude Desktop** | Yes | Yes | Full support |
| **VS Code Copilot** | Yes | Yes | Full support |

All major clients support **Tools**. This is the universal interface.

### 6.6 Integration Patterns

**Pattern A: LLM-driven TTS (works today)**
The LLM calls `voice_speak("I've finished the refactoring")` when it decides to speak. Natural — LLM sees the tool and uses it.

**Pattern B: Hook-triggered TTS (works today)**
Claude Code post-response hook calls `heyvox speak "summary"` via CLI. The hook triggers the MCP server.

**Pattern C: Voice input → user message (current approach stays)**
Wake word → STT → osascript paste → text appears in chat. Bypasses MCP entirely. Works with ANY app.

**Pattern D: Multi-client via Streamable HTTP (future)**
Vox runs as HTTP server. Claude Code, Cursor, Claude Desktop all connect simultaneously. One voice layer, many clients.

### 6.7 Implementation: Wake Word Listener = MCP Server

The wake_word_listener.py **becomes** the MCP server:
- Add `mcp` Python SDK as dependency
- Wrap existing functions as MCP tools
- Switch logging from stdout to stderr (stdio transport requires clean stdout)
- Run MCP transport alongside existing audio loop

### 6.8 Claude Code Configuration

```json
// ~/.claude/settings.json
{
  "mcpServers": {
    "voice": {
      "command": "python3.12",
      "args": ["/path/to/vox/voice_mcp_server.py"]
    }
  }
}
```

### 6.9 Key Limitations

1. **No user message injection via MCP** — voice input must use OS-level paste
2. **stdio = single client** — use Streamable HTTP for multiple simultaneous clients
3. **Tasks API not yet supported** — no elegant "start listening, poll for result" yet
4. **Tool approval friction** — users must allow tool calls (or auto-approve in settings)
5. **Audio content type** — spec supports it, no client renders it yet. Play locally instead.

---

## Part 7: Voice HUD Design

### 7.1 Layout Decision: Top-Center Notch Bar

Collapses to tiny pill when idle, expands for active states. Top-center is the peripheral vision sweet spot — mirrors macOS menu bar mental model, horizontal expansion doesn't steal vertical coding space.

### 7.2 States and Wireframes

**IDLE** (barely visible, 28px tall):
```
╭──────────────────╮
│  ◉  3 agents     │
╰──────────────────╯
```
On hover, expands to show agent list with status and Cmd+number shortcuts.

**LISTENING** (wake word or PTT active):
```
╭─────────────────────────────────────────────────────────────────╮
│  🔴 LISTENING          ▁▃▅▇▅▃▁▃▅▇▅▃▁       → manama    [ESC] │
├─────────────────────────────────────────────────────────────────┤
│  "refactor the auth module to use JWT tokens..."                │
╰─────────────────────────────────────────────────────────────────╯
```
- Pulsing red circle replaces old recording_indicator.py
- Live waveform (15 bars, maps to mic RMS amplitude)
- Live partial transcription appearing word-by-word
- Target workspace indicator
- Wake word shows `(voice)`, PTT shows `(fn)`

**PROCESSING** (sent to agent, waiting):
```
╭─────────────────────────────────────────────────────────────────╮
│  ⟳ Sending to manama...                               2s ago   │
├─────────────────────────────────────────────────────────────────┤
│  "refactor the auth module to use JWT tokens and update tests"  │
╰─────────────────────────────────────────────────────────────────╯
```
- Spinning icon, amber color
- Indeterminate progress shimmer
- Shows elapsed time

**SPEAKING** (TTS playing):
```
╭─────────────────────────────────────────────────────────────────╮
│  🔊 manama speaking                         ⏸  ⏭  ■     2/5   │
├─────────────────────────────────────────────────────────────────┤
│  "I've refactored the auth module. The JWT implementation..."   │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
╰─────────────────────────────────────────────────────────────────╯
```
- Speaker icon with animated waves
- Playback controls (voice commands also work: "pause", "skip", "stop")
- Progress bar (green = spoken, dim = remaining)
- Message counter (2/5)

**MULTI-AGENT** (queue from multiple workspaces):
```
╭─────────────────────────────────────────────────────────────────╮
│  🔊 manama speaking                         ⏸  ⏭  ■     1/3   │
├─────────────────────────────────────────────────────────────────┤
│  "The JWT tokens are now configured..."                         │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
├─────────────────────────────────────────────────────────────────┤
│  UP NEXT                                                        │
│  ● hangzhou    1 msg     "Build succeeded with 0 warn..."      │
│  ● dubai       1 msg     "Invoice template ready for..."       │
│                                          3 queued total         │
╰─────────────────────────────────────────────────────────────────╯
```
- Queue section with agent color dots
- Preview of next messages
- Max 4 visible items, collapsible

### 7.3 State Transitions

```
IDLE ──(wake word/fn)──> LISTENING ──(stop)──> PROCESSING ──(TTS)──> SPEAKING ──(done)──> IDLE
  ^                         │                                            │
  └────────(ESC/cancel)─────┘                                            │
  └────────────────────────(stop/done)───────────────────────────────────┘
```

- If user speaks while TTS is playing: TTS pauses immediately, HUD shows LISTENING
- IDLE → LISTENING: 200ms expand, SPEAKING → IDLE: 300ms collapse
- Auto-hide to pill after 5s idle (hover prevents collapse)

### 7.4 Information Priority (Glanceability)

1. **STATE** (color) — red=recording, green=speaking, amber=processing, gray=idle
2. **TRANSCRIPTION** — what did I say?
3. **TARGET** — where is this going?
4. **TTS CONTENT** — what is the agent saying?
5. **QUEUE** — what else is pending?

### 7.5 Visual Design

```
Background:    NSVisualEffectView, material: .hudWindow (frosted glass)
               rgba(28, 28, 30, 0.88) base
State colors:  IDLE #86868B, LISTENING #FF3B30, PROCESSING #FFD60A, SPEAKING #30D158
Agent dots:    #30D158 (green), #0A84FF (blue), #FF9F0A (orange), #BF5AF2 (purple)
Typography:    SF Pro Text, 13-15px
Dimensions:    IDLE 140x28px, expanded max 520x200px
Corner radius: 14px
Shadow:        0 2px 8px rgba(0, 0, 0, 0.3)
```

### 7.6 Interaction Model

**Voice (primary):** "pause", "skip", "stop", "mute", "replay", "switch to [workspace]"
**Mouse (secondary):** hover to expand, click controls, drag to reposition
**Keyboard (tertiary):** `⌘⇧Space` toggle, `ESC` cancel, `⌘⇧]` skip, `⌘⇧[` replay

### 7.7 Technical Implementation

- **Extend `recording_indicator.py`** — same NSWindow patterns, same permissions
- **NSVisualEffectView** with `.hudWindow` material (enum value 13 in PyObjC)
- **CATextLayer** for status text and transcription
- **Unix domain socket** (`/tmp/heyvox-hud.sock`) for IPC
- **Window level:** `NSStatusWindowLevel + 1` (proven in existing code)
- **Collection behavior:** `canJoinAllSpaces | fullScreenAuxiliary` (proven)
- **No new macOS permissions required**

### 7.8 IPC Protocol (JSON over Unix socket)

```json
{"type": "state", "state": "listening", "source": "wake_word", "target": "manama"}
{"type": "audio_level", "rms": 1847}
{"type": "transcript", "text": "refactor the auth...", "partial": true}
{"type": "tts_start", "workspace": "manama", "message_index": 1, "total": 5}
{"type": "tts_progress", "position": 0.45, "current_sentence": "The JWT tokens..."}
{"type": "queue_update", "queue": [{"workspace": "hangzhou", "count": 1, "preview": "Build..."}]}
{"type": "error", "message": "Microphone unavailable", "action": "open_settings"}
```

### 7.9 Architecture

```
wake_word_listener.py ──┐
TTS orchestrator ───────┼──(Unix socket)──> hud_overlay.py (persistent AppKit process)
Conductor status ───────┘                       │
                                          NSVisualEffectView + CATextLayer
                                          NSStatusWindowLevel + 1
                                          canJoinAllSpaces | fullScreenAuxiliary
```

Separate process required (AppKit needs its own run loop). Unix socket IPC is more reliable than file-based flags. Any component can update the HUD.

### 7.10 Phase 1 vs Phase 2

**Phase 1 (extend recording_indicator.py with PyObjC):**
- Fast iteration, stays in Python
- Add vibrancy, text layers, socket IPC
- Good enough for v1

**Phase 2 (native Swift/SwiftUI app, if needed):**
- Better performance, modern layout
- Communicates with Python services over same Unix socket
- Consider only if PyObjC becomes a bottleneck

---

## Part 8: Interface Strategy

### 8.1 The Question

Should Vox integrate with existing UIs (adapter approach) or become its own interface?

### 8.2 The Answer: Voice Replaces Typing, Not Screens

The visual interface shifts from "primary input surface" to "verification/review surface." Like a pilot: verbal commands to ATC, instruments for monitoring.

| Activity | Best Interface |
|----------|---------------|
| Giving instructions to AI | **Voice** |
| Reviewing code diffs | **Visual** (editor/Conductor) |
| Reading error messages | **Visual + TTS summary** |
| Quick commands | **Voice** |
| Architecture discussions | **Voice** |
| Debugging | **Visual** |
| Multi-agent management | **Voice + HUD** |

### 8.3 Evolution Path

- **v1:** MCP server + basic recording indicator. Voice input works with any app via osascript.
- **v2:** Rich HUD (multi-agent queue, TTS visualization). Becomes the "command center."
- **v3:** HUD pulls visual context (current file, last diff summary) for voice-only workflows on 80% of tasks.

### 8.4 Why HUD + MCP Is the Product

The combination is the product — not adapters, not CLI scripts.

- **The HUD** is what users see, what goes in the demo video, what differentiates visually
- **The MCP server** is what makes it integrate with every AI tool without per-app adapters
- **The voice pipeline** (wake word → STT → paste) is the universal input layer

Product story: **"One voice server, any AI tool, beautiful HUD."**

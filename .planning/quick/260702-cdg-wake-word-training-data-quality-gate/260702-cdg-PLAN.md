---
phase: quick-260702-cdg-wake-word-training-data-quality-gate
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - heyvox/audio/training_collector.py
  - heyvox/recording.py
  - heyvox/main.py
  - tests/test_training_collector.py
  - tools/collect_personal_features.py
  - tools/quality_gate.py
  - tools/fp_rate_eval.py
  - .planning/DEFECT-LOG.md
autonomous: true
requirements: [CDG-01, CDG-02, CDG-03, CDG-04, CDG-05]
must_haves:
  truths:
    - "An aborted/no-speech/empty-STT/user-cancelled trigger no longer relabels its tp_start clip as fp/ — the real trigger clip stays in tp/ or is left alone; no reclassify_tp_start_as_fp call exists anywhere in the codebase"
    - "A start-trigger that follows a recent tn/ save no longer relabels that tn/ clip as fn/ — reclassify_fn_start and the _recent_tn tracking list no longer exist"
    - "Running the batch gate over training data moves any positive-dir clip that Whisper (with the lenient _WW_RE matcher) finds contains NO wake word into quarantine/, and moves any negative-dir clip found to CONTAIN the wake word into positives/ (<=2.5s) or quarantine/ (>2.5s) — never deletes a file"
    - "collect_personal_features.py refuses to featurize (exits non-zero before touching source dirs) unless the gate has been run, UNLESS --skip-gate is passed explicitly"
    - "tools/fp_rate_eval.py --history-file appends one JSON line per run to an append-only JSONL file without truncating prior entries"
    - "DEFECT-LOG.md has a new DEF-167 entry documenting the two retroactive relabelers and the process gap that let 192 fp/ clips go unaudited by the original whisper_sanity_pass"
  artifacts:
    - path: "heyvox/audio/training_collector.py"
      provides: "TrainingCollector with retroactive relabelers removed; save_fn_stop remains the sole evidence-based fn path"
      contains: "def classify_stop_outcome"
    - path: "tools/quality_gate.py"
      provides: "Mandatory batch Whisper quality gate: RMS pre-gate, per-clip hard timeout, resumable write-ahead JSONL, lenient _WW_RE-based verdict, quarantine-only moves with a manifest"
      exports: ["run_gate", "main"]
    - path: "tools/collect_personal_features.py"
      provides: "Gate invocation before featurization, --gate-only and --skip-gate CLI flags"
      contains: "run_gate"
    - path: ".planning/DEFECT-LOG.md"
      provides: "DEF-167 entry"
      contains: "DEF-167"
  key_links:
    - from: "heyvox/main.py"
      to: "heyvox/audio/training_collector.py"
      via: "call sites for save_tp_start/save_fn_stop only — no reclassify_* calls remain"
      pattern: "reclassify_(fn_start|tp_start_as_fp)"
    - from: "tools/collect_personal_features.py"
      to: "tools/quality_gate.py"
      via: "run_gate(POSITIVE_DIRS, HARD_NEGATIVE_DIRS, positives_dir, quarantine_dir) called before gather()/featurise()"
      pattern: "run_gate\\("
    - from: "tools/quality_gate.py"
      to: "tools/whisper_sanity_pass.py"
      via: "imports _WW_RE / _HALLUCINATION_RE (or an equivalent lenient matcher) for the has-wake-word verdict on both positive and negative sides"
      pattern: "_WW_RE|_contains_wake_word"
---

<objective>
Close the wake-word training-data quality gap found this session: two retroactive relabelers in `TrainingCollector` (`reclassify_fn_start`, `reclassify_tp_start_as_fp`) manufacture mislabeled clips from real triggers — measured 54% of `fn_start` relabels contained NO wake word (real TNs mislabeled as misses) and 11.5% (22/192) of `fp`-reclassified clips contained the REAL wake word (real triggers mislabeled as hard negatives). Both classes of mislabel train the model toward FALSE NEGATIVES on the user's own voice. This plan (1) removes the two heuristic relabelers, keeping only the evidence-based `save_fn_stop` path; (2) builds a mandatory batch Whisper quality gate that audits every training-data source dir before every featurization run and quarantines (never deletes) mismatches; (3) reuses the already-proven lenient wake-word matcher from `whisper_sanity_pass.py` on both the positive and negative sides; (4) adds an append-only eval-history log to `fp_rate_eval.py`; (5) documents the defect and process gap in `DEFECT-LOG.md`.

Purpose: Stop the training pipeline from silently poisoning itself via unaudited heuristic relabeling, and make the previously one-off manual Whisper audit (this session's `REFERENCE-fp_check_step0.py` + the 26-clip `recfp_cleanup_manifest_20260702_084240.json` precursor) a permanent, mandatory, resumable step in the normal collect flow.
Output: `TrainingCollector` with the two relabelers deleted; `tools/quality_gate.py` (new, absorbing `whisper_sanity_pass.py`'s regex + transcribe logic); `tools/collect_personal_features.py` gated by default with `--gate-only`/`--skip-gate` escape hatches; `tools/fp_rate_eval.py --history-file`; `.planning/DEFECT-LOG.md` DEF-167 entry.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md

<interfaces>
Current TrainingCollector surface (heyvox/audio/training_collector.py) that Task 1 modifies. Executor should edit in place, no codebase exploration needed.

Methods to DELETE entirely (both the method body and the state they exist solely to serve):

heyvox/audio/training_collector.py lines 169-188, reclassify_tp_start_as_fp(self, reason="no-speech") -> int: pops from self._recent_tp_start LIFO, on a match does shutil.move(tp_path, fp_path) with filename prefix swap tp_start_ -> fp_{reason}_, returns 1 if reclassified else 0.

heyvox/audio/training_collector.py lines 271-298, reclassify_fn_start(self) -> int: iterates self._recent_tn, for entries within self._fn_window does shutil.move(tn_path, fn_path) with filename prefix swap tn_ -> fn_start_, returns count reclassified.

State that exists ONLY to feed the two deleted methods (delete alongside them):
- __init__ ~line 116: self._recent_tn: list[tuple[float, Path]] = []
- __init__ ~line 120: self._recent_tp_start: list[tuple[float, Path]] = []

_recent_tp_start is written in save_tp_start (lines 161-165) and read only by reclassify_tp_start_as_fp -- the write side in save_tp_start must also be removed (but save_tp_start itself, its buffer-clip extraction, and its _save(..., return_path=True) call stay -- only the append/prune-into-_recent_tp_start tracking goes).

_recent_tn is written in save_tn (lines 240-245) and read only by reclassify_fn_start -- same treatment: save_tn keeps saving TN clips, only the _recent_tn bookkeeping goes.

fn_reclassify_window_secs constructor param (self._fn_window) becomes unused once reclassify_fn_start is gone -- remove the param and its use in __init__ too (grep for fn_reclassify_window_secs at any call sites before removing the constructor signature entry -- heyvox/main.py constructs it and may or may not pass it explicitly).

Call sites to remove (delete the "if self.training_collector: ... reclassify_tp_start_as_fp(...)" block entirely at each; leave the surrounding control flow -- cue/HUD/logging calls outside the if block -- untouched):
- heyvox/recording.py:666-674 ("post-trim-short" reason)
- heyvox/recording.py:795-802 ("low-energy" reason -- note this call site ALSO calls save_fp(..., reason="low-energy") immediately after; KEEP that save_fp call, only remove the reclassify_tp_start_as_fp block above it)
- heyvox/recording.py:1059-1066 ("empty-stt" reason -- same pattern: save_fp(..., reason="empty-stt") follows and must stay)
- heyvox/recording.py:1077-1084 ("user-cancelled" reason -- same pattern: save_fp(..., reason="user-cancelled") follows and must stay)
- heyvox/main.py:1370-1372 ("no-speech" reason, inside the _NO_SPEECH_CANCEL_SECS cancel branch)
- heyvox/main.py:1905-1909 (save_tp_start(s) call stays; the reclassify_fn_start() call and its "if reclass:" logging block are removed)

Methods that MUST remain untouched (do not modify their bodies): classify_stop_outcome, save_tp_stop, save_fp, save_tn (only remove _recent_tn bookkeeping inside it -- keep the save), save_fn_stop, feed, counts.

tests/test_training_collector.py current state: no test currently exercises reclassify_fn_start/reclassify_tp_start_as_fp by name -- the file only tests classify_stop_outcome, save_fn_stop, save_tp_stop, _prune. No existing tests will break from the deletion; Task 1's test changes are ADDITIVE, proving the new negative behavior.
</interfaces>

<interfaces>
Reference regex + transcribe logic from tools/whisper_sanity_pass.py that Task 2/3 absorb into tools/quality_gate.py. Reuse verbatim -- this matcher was proven against real data this session (matches box/fox/vops/vocks/wax as "vox" garbles, rejects hallucination loops).

tools/whisper_sanity_pass.py lines 32-57 define these module-level constants: _HEY, _VOX, _SEP regex fragments combined into _WW_RE = re.compile a word-boundary "hey"-like-token + separator + "vox"-like-token pattern, case-insensitive. _HALLUCINATION_RE matches repeated "harriet" (2+), repeated "evet" (3+, Turkish loop), repeated "see" (5+), "hay subscribe" (Vietnamese spam), a Russian subtitle-spam phrase, and a Vietnamese channel-name phrase -- case-insensitive. _contains_wake_word(text): returns False if _HALLUCINATION_RE matches, else bool(_WW_RE.search(text)). _load_wav(path): opens via stdlib wave module, returns (audio: np.ndarray float32 normalized /32768.0, sr: int).

From REFERENCE-fp_check_step0.py (the proven robust/resumable driver in this quick-task directory -- reuse these EXACT patterns in tools/quality_gate.py, not a fresh reimplementation):
- _SILENCE_RMS = 150.0 / 32768.0 -- skip clips below this RMS without transcribing.
- transcribe(audio, sr) calls mlx_whisper.transcribe with path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="en", condition_on_previous_text=False (DEF-075: don't amplify loops), compression_ratio_threshold=2.2 (DEF-075: bail from degenerate decode), logprob_threshold=-0.8 (DEF-075), no_speech_threshold=0.6, word_timestamps=False. Returns the stripped text field of the result.
- Resumability: write-ahead inflight marker (inflight.txt, written before each clip starts, deleted after each clip completes) + append-only results.jsonl (one JSON record per completed clip, flushed immediately). On restart: any name in inflight.txt but not yet in results.jsonl was the clip that hung last run -> record it as timeout=true and skip it.
- A per-clip hard timeout is REQUIRED on top of the write-ahead pattern because a thread cannot kill a stuck MLX Metal GPU call -- REFERENCE-fp_check_step0.py relies on being run under an EXTERNAL timeout/gtimeout wrapper and resuming after a kill. tools/quality_gate.py must enforce the per-clip timeout ITSELF (not rely on an external wrapper) using a subprocess-per-clip (spawn a small helper invocation via subprocess.run with a timeout kwarg) or an equivalent process-level hard cap, since the gate must be a single command a human/CI can run to completion without needing external timeout wrapping. subprocess.run's timeout kills via SIGKILL on expiry, which DOES reliably terminate a stuck MLX GPU call (unlike a Python thread join).

From tools/collect_personal_features.py (current source-dir lists Task 2 gates -- reuse verbatim, do not redefine):
POSITIVE_DIRS = [tp, positives, fn under ~/.config/heyvox/training/, plus training/recordings, training/recordings_friends, training/recordings_jabra relative to the repo/tools dir].
HARD_NEGATIVE_DIRS = [tn, fp under ~/.config/heyvox/training/, plus training/negatives].

Note: training/recordings_friends and training/recordings_jabra do not currently exist on disk relative to tools/ -- gather() already handles this (missing dir is skipped with a log line, not an error) -- the gate must apply the same missing-dir tolerance, not error out.

Verified this session: ~/.config/heyvox/training/{tp,fp,tn,fn,positives,quarantine} all already exist on disk (quarantine/ already has 309 wavs and positives/ has 403 wavs from the manual recfp_cleanup_manifest_20260702_084240.json precursor -- see Task 5). The gate must create quarantine/ if absent (fresh installs) rather than assume it exists.
</interfaces>

<interfaces>
tools/fp_rate_eval.py current structure that Task 4 extends.

main() builds `results` (list of per-threshold dicts: threshold, fp_count, fp_per_hour, tp_rate, gate_pass) over THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95], with TP_GATE = 0.70 and FP_PER_HOUR_GATE = 1.0, then prints a summary dict (negative_hours, negative_files, positive_files, results) via json.dumps at the very end of main(). --history-file appends ONE more record built from that same data, after the existing print, before main() returns.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Remove retroactive relabelers from TrainingCollector and all call sites</name>
  <files>heyvox/audio/training_collector.py, heyvox/recording.py, heyvox/main.py, tests/test_training_collector.py</files>
  <behavior>
    - reclassify_tp_start_as_fp no longer exists as a method on TrainingCollector (AttributeError on access).
    - reclassify_fn_start no longer exists as a method on TrainingCollector (AttributeError on access).
    - save_tp_start(score) still saves a clip to tp/ from the rolling buffer and returns True/False exactly as before, but no longer appends to any _recent_tp_start-style tracking list (attribute removed).
    - save_tn(max_score) still saves a clip to tn/ under the same rate-limit/score-range rules as before, but no longer appends to any _recent_tn-style tracking list (attribute removed).
    - An aborted trigger (simulate: call save_tp_start, then attempt the old reclassify call) does NOT move the tp/ clip to fp/ -- because the method to do so no longer exists, and no equivalent behavior is reintroduced anywhere in recording.py/main.py.
    - A start-trigger following a recent save_tn call does NOT relabel that tn/ clip as fn/ -- same reasoning.
    - grep -rn "reclassify_fn_start\|reclassify_tp_start_as_fp" heyvox/ returns zero matches after this task.
  </behavior>
  <action>
    In heyvox/audio/training_collector.py:
    1. Delete the reclassify_tp_start_as_fp method (lines ~169-188) in full.
    2. Delete the reclassify_fn_start method (lines ~271-298) in full.
    3. In __init__, remove the self._recent_tp_start: list[tuple[float, Path]] = [] line and the self._recent_tn: list[tuple[float, Path]] = [] line.
    4. Remove the fn_reclassify_window_secs: float = 5.0 constructor parameter and the self._fn_window = fn_reclassify_window_secs assignment (grep call sites of TrainingCollector( across the repo first, heyvox/main.py constructs it, to check whether that kwarg is passed positionally/by-name and remove it there too if present).
    5. In save_tp_start, remove the self._recent_tp_start.append(...) line and the list-comprehension prune block that follows it (and the now = time.time() line if it becomes otherwise-unused in that method). save_tp_start must still call self._save("tp", audio, score, suffix="start", return_path=True) and return True/False based on whether the save succeeded, just drop the tracking-list side effect.
    6. In save_tn, remove the self._recent_tn.append((now, filepath)) line and the list-comprehension prune block that follows it. save_tn must still call self._save("tn", audio, max_score, return_path=True) and return True/False, just drop the tracking-list side effect.
    7. Update the module docstring (lines 1-24): remove the paragraph describing "The retrospective FN-start detection works by tracking recent TN saves..." (lines 21-24) since that mechanism no longer exists. Do not otherwise rewrite the docstring.
    8. Remove the now-unused "import shutil" at the top of the file ONLY if nothing else in the file uses shutil (grep for other shutil. usages before removing the import; _save/_prune use Path/unlink/glob, not shutil, so this import is very likely dead after the deletion, confirm before removing).

    In heyvox/recording.py, remove these four "if self.training_collector: ... reclassify_tp_start_as_fp(...)" blocks entirely, leaving everything else in each surrounding code path unchanged:
    - Around line 666-674 ("post-trim-short" reason, inside the post-trim-too-short cancel branch).
    - Around line 795-802 ("low-energy" reason) -- the immediately-following self.training_collector.save_fp(audio_chunks, ..., reason="low-energy") call (lines ~803-807) stays untouched.
    - Around line 1059-1066 ("empty-stt" reason) -- the immediately-following save_fp(_training_chunks, ..., reason="empty-stt") call stays untouched.
    - Around line 1077-1084 ("user-cancelled" reason) -- the immediately-following save_fp(_training_chunks, ..., reason="user-cancelled") call stays untouched.

    In heyvox/main.py:
    - Around line 1370-1372, remove the "if _training_collector is not None: if _training_collector.reclassify_tp_start_as_fp('no-speech'): log(...)" block. Leave recording.cancel() and log("Ready for next wake word.") (the surrounding lines) untouched.
    - Around line 1905-1909, keep _training_collector.save_tp_start(s) but remove the "reclass = _training_collector.reclassify_fn_start()" line and its following "if reclass: log(...)" block. Update the comment above this block ("# Training data: save TP-start and reclassify recent TN->FN") to note that evidence-based fn recovery now happens via the batch quality gate (tools/quality_gate.py), not retroactive heuristics.

    In tests/test_training_collector.py, add new tests proving the negative behavior (aborted trigger does not fp-relabel; start trigger does not fn-relabel a tn):
    - test_save_tp_start_has_no_reclassify_method: assert not hasattr(collector, "reclassify_tp_start_as_fp").
    - test_save_tn_has_no_reclassify_method: assert not hasattr(collector, "reclassify_fn_start").
    - test_aborted_trigger_leaves_tp_start_clip_alone: feed enough buffer via collector.feed(_speech(3.0)), call collector.save_tp_start(0.85), assert the resulting clip is still in tp/ (glob tp_start_*.wav) and that fp/ is empty, i.e. no reclassification path exists to move it.
    - test_tn_save_not_relabeled_by_subsequent_trigger: collector.feed(_speech(3.0)) then collector.save_tn(0.4) (within the default tn_score_range of 0.1-0.7), then collector.feed(_speech(3.0)) and collector.save_tp_start(0.9) to simulate "a trigger followed a recent TN" -- assert the tn/ clip is still present in tn/ and fn/ is empty.
  </action>
  <verify>
    <automated>python3 -m pytest tests/test_training_collector.py -v && ! grep -rn "reclassify_fn_start\|reclassify_tp_start_as_fp\|_recent_tp_start\|_recent_tn\b" heyvox/ --include="*.py"</automated>
  </verify>
  <done>python3 -m pytest tests/test_training_collector.py -v passes with all tests green (existing + 4 new ones added above); the negative grep for the two removed method names and their tracking-list attributes across heyvox/ returns zero matches; TrainingCollector.__init__ no longer accepts fn_reclassify_window_secs.</done>
</task>

<task type="auto">
  <name>Task 2: Build the mandatory batch Whisper quality gate</name>
  <files>tools/quality_gate.py</files>
  <action>
    Create tools/quality_gate.py as a new module absorbing whisper_sanity_pass.py's regex/matcher logic and REFERENCE-fp_check_step0.py's robust/resumable transcription driver. This module is imported by tools/collect_personal_features.py (Task 3) and is also independently runnable as a CLI.

    Module docstring: explain the gate's purpose (mandatory single chokepoint auditing every training-data source dir before featurization, quarantine-only moves, resumable across MLX GPU hangs) and reference that it absorbs tools/whisper_sanity_pass.py's matcher (kept in place, not deleted -- quality_gate.py imports from it rather than duplicating the regex).

    Constants (copy from tools/whisper_sanity_pass.py by importing _WW_RE, _HALLUCINATION_RE, _contains_wake_word, _load_wav directly -- add "sys.path.insert(0, str(Path(__file__).resolve().parent))" then "from whisper_sanity_pass import _WW_RE, _HALLUCINATION_RE, _contains_wake_word, _load_wav" so there is exactly one source of truth for the matcher, matching the import pattern already used in REFERENCE-fp_check_step0.py):
    - _SILENCE_RMS = 150.0 / 32768.0 (copy from REFERENCE-fp_check_step0.py)
    - _RECOVERY_MAX_SECS = 2.5 (the positives-vs-quarantine split point for negative-dir clips that DO contain the wake word -- per task_scope: <=2.5s -> positives/, >2.5s -> quarantine/)
    - _CLIP_TIMEOUT_SECS = 20 (per-clip hard timeout enforced via subprocess, generous enough for a single 2-5s clip on Metal but short enough that one stuck clip does not stall a multi-hundred-clip run for more than ~7 minutes total worst case)
    - _QUARANTINE_RATE_WARN_THRESHOLD = 0.15 (warn if more than 15% of clips in a single run get quarantined -- the task_scope's "guard against Whisper false-quarantining real positives" requirement; this is a WARN not a hard-fail, since the gate must never silently block the user's ability to see and review its own output)

    Per-clip transcription with hard timeout (the core new capability beyond both source scripts -- REFERENCE-fp_check_step0.py relies on an EXTERNAL timeout wrapper, this module must be self-contained):
    - Write a small standalone worker entry point in this same file, e.g. "def _transcribe_worker(wav_path: str) -> None", that loads the wav via _load_wav, checks the RMS silence pre-gate (if below _SILENCE_RMS, print a JSON line {"text": "", "silent": true} to stdout and return -- do not call mlx_whisper at all), otherwise calls mlx_whisper.transcribe with the exact decode params from REFERENCE-fp_check_step0.py (path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="en", condition_on_previous_text=False, compression_ratio_threshold=2.2, logprob_threshold=-0.8, no_speech_threshold=0.6, word_timestamps=False) and prints a JSON line {"text": <result>} to stdout.
    - Wire this worker to run when the module is invoked with a special internal flag, e.g. "python3 tools/quality_gate.py --_worker <wav_path>" (leading underscore signals this is not a user-facing subcommand) -- this lets the parent process spawn it via subprocess.run([sys.executable, __file__, "--_worker", str(wav_path)], capture_output=True, text=True, timeout=_CLIP_TIMEOUT_SECS) and reliably SIGKILL it on timeout (a Python thread cannot interrupt a stuck native MLX/Metal call, but subprocess.run's timeout mechanism can and does kill the child OS process).
    - The parent-side transcribe_with_timeout(wav_path: Path) -> tuple[str, bool] function: runs the subprocess as above, catches subprocess.TimeoutExpired to return ("", True) (timed_out=True), parses the last stdout line as JSON on success to extract "text", and treats any other non-zero exit or unparseable output as an error case returning ("", False) with a logged warning (not silently swallowed).

    Resumable write-ahead JSONL driver (copy the exact pattern from REFERENCE-fp_check_step0.py, generalized to run over multiple dirs in one pass rather than a single hardcoded fp/ dir):
    - State files live under a run-scoped directory: a --state-dir CLI arg defaulting to a fixed path such as ~/.config/heyvox/training/.gate_state/ (create if missing). Inside: results.jsonl (append-only, one completed-clip record per line) and inflight.txt (write-ahead marker, written before each clip starts, deleted after it completes).
    - On startup: load_done() reads results.jsonl into a set of already-processed absolute clip paths; recover any name found in inflight.txt but not in results.jsonl by appending a {"path": ..., "verdict": "timeout", "moved": false} record for it (matching the REFERENCE script's stalled-clip recovery) before continuing.
    - Each run iterates over the full ordered clip list (see gather logic below), skipping any already in the done set, and appends one JSON record to results.jsonl per completed clip (flushed immediately after each write, matching the REFERENCE script's f.flush() pattern) so a kill/resume never loses more than the one in-flight clip.

    Gather + verdict + move logic (run_gate is the public entry point Task 3 calls):
    - def run_gate(positive_dirs: list[Path], negative_dirs: list[Path], positives_dir: Path, quarantine_dir: Path, *, state_dir: Path | None = None) -> dict: the main orchestration function.
    - quarantine_dir.mkdir(parents=True, exist_ok=True) -- create if absent (fresh installs per the Verified-this-session note in context).
    - For each dir in positive_dirs: if not dir.is_dir(), skip with a log line (same missing-dir tolerance as tools/collect_personal_features.py's gather()) -- do not error. Otherwise glob *.wav, and for each clip not already in the done set: run transcribe_with_timeout, compute has_ww = _contains_wake_word(text) (or verdict already computed as "silent" from the RMS pre-gate, which counts as has_ww=False), record verdict + append to results.jsonl. If NOT has_ww (positive dir, no wake word found) -> shutil.move the clip into quarantine_dir, append a manifest entry {"from": str(clip), "to": str(new_path), "side": "positive", "text": text, "reason": "no-wake-word-in-positive-dir"}.
    - For each dir in negative_dirs: same transcribe-with-resume loop, but the move logic inverts: if has_ww is True (negative dir clip that DOES contain the wake word -- a genuine miss, matching task_scope's "evidence-based fn recovery" framing), read the clip duration via soundfile.info(clip).duration or equivalent (frames/samplerate from the already-loaded audio array divided by sample rate), and if duration <= _RECOVERY_MAX_SECS move it to positives_dir, else move it to quarantine_dir; append a manifest entry with side="negative", reason="wake-word-in-negative-dir", duration=<val>, moved_to="positives" or "quarantine".
    - Track counts: total clips processed, quarantined count, recovered-to-positives count, timeout count, error count. quarantine_rate = quarantined / total (guard against division by zero when total==0). If quarantine_rate > _QUARANTINE_RATE_WARN_THRESHOLD, print a clearly-flagged WARNING line to stderr (not silent) -- per task_scope, "guards against Whisper false-quarantining real positives ... a spike = suspicious, surface it don't silently shrink the positives set."
    - Write a run-scoped manifest file (e.g. state_dir / f"manifest_{timestamp}.json", timestamp via time.strftime like the existing recfp_cleanup_manifest_*.json precedent in this repo) containing the full list of moves made this run, in the exact same {"from", "to", ...} shape as the precedent manifest so it is reversible by the same manual process used for the Schritt-0 cleanup.
    - Print a final summary dict to stdout (positives_checked, negatives_checked, quarantined, recovered_to_positives, timeouts, errors, quarantine_rate, manifest_path) and RETURN that same dict from run_gate() so callers (Task 3) can inspect gate_pass-like signals programmatically.

    CLI entry point (def main() -> int, guarded by if __name__ == "__main__"): argparse with --state-dir, and defaults that gate over the SAME dir lists as tools/collect_personal_features.py's POSITIVE_DIRS/HARD_NEGATIVE_DIRS constants (import them from collect_personal_features to avoid a second source of truth -- watch for circular import risk: collect_personal_features.py will import run_gate FROM quality_gate.py in Task 3, so quality_gate.py must NOT import collect_personal_features.py at module level; instead accept dir lists as run_gate() parameters and have quality_gate.py's OWN __main__ block do a lazy/deferred import of collect_personal_features only inside main(), or duplicate just the two dir-list constants locally in quality_gate.py with a comment noting they must be kept in sync with collect_personal_features.py -- prefer the deferred-import approach since it has zero duplication risk).
  </action>
  <verify>
    <automated>python3 -c "import sys; sys.path.insert(0, 'tools'); import quality_gate; assert hasattr(quality_gate, 'run_gate'); assert hasattr(quality_gate, 'main'); print('quality_gate imports OK, run_gate + main present')"</automated>
  </verify>
  <done>tools/quality_gate.py exists, imports without error, exposes run_gate(positive_dirs, negative_dirs, positives_dir, quarantine_dir) and main(); running "python3 tools/quality_gate.py --_worker <path-to-a-real-short-silent-wav>" prints a JSON line with text=="" and silent==true without invoking mlx_whisper (verify by timing it -- must return in well under 1s, proving the RMS pre-gate skipped transcription).</done>
</task>

<task type="auto">
  <name>Task 3: Wire the gate into collect_personal_features.py with --gate-only / --skip-gate flags, and surface a spot-check log</name>
  <files>tools/collect_personal_features.py</files>
  <action>
    In tools/collect_personal_features.py:

    1. Add "from quality_gate import run_gate" near the existing "from featurise_clips import collect_wav_paths, featurise" line (same sys.path.insert(0, str(Path(__file__).resolve().parent)) already present handles this import too).

    2. Add two new CLI flags to the existing argparse setup in main():
       - --gate-only (action="store_true"): run the quality gate and print its summary, then return 0 WITHOUT running gather()/featurise()/packing. This lets the user audit training data without paying the featurization cost.
       - --skip-gate (action="store_true"): the explicit escape hatch. When set, main() skips run_gate() entirely and proceeds straight to the existing gather/featurise/pack flow, matching today's behavior exactly. Print a clearly visible warning line to stderr when this flag is used (e.g. "WARNING: --skip-gate set -- featurizing WITHOUT the mandatory Whisper quality audit") so accidental use is not silent.
       - These two flags are mutually exclusive in intent (--gate-only implies no featurization happens regardless of --skip-gate) -- if both are passed, --gate-only takes precedence (gate runs, featurization is skipped) and a note is printed explaining why --skip-gate had no effect.

    3. In main(), BEFORE the existing "print('=== Gathering positives ===')" line, insert the gate invocation as the new first step (this is the "mandatory single chokepoint that runs BEFORE featurizing" from task_scope):
       - If not args.skip_gate: call run_gate(POSITIVE_DIRS, HARD_NEGATIVE_DIRS, _expand("~/.config/heyvox/training/positives"), _expand("~/.config/heyvox/training/quarantine")), capture the returned summary dict, and print a one-line "=== Quality gate: N quarantined, M recovered to positives (rate=X%) ===" status line.
       - If args.gate_only: return 0 right after printing that status line (before any gather() call).
       - If args.skip_gate: print the warning from step 2 and skip straight past this block to the existing gather() call (no gate summary line, since the gate never ran).

    4. Spot-check log (task_scope Task 3's "one-line spot-check log listing negative-with-WW recoveries"): after run_gate() returns, if its summary dict includes a "recovered_to_positives" count greater than zero, read the manifest file it wrote (summary["manifest_path"]) and print one line per manifest entry where side=="negative" AND moved_to=="positives", in the format "  recovered: {clip_name} <- {duration:.1f}s, transcript: '{text[:60]}'" -- so the user can eyeball the rare genuine-ambient-confusable case (a real other-speaker "hey box" that fired) without it blocking the run. This is informational only -- do not prompt for confirmation, do not exit non-zero, just print and continue.

    5. Since POSITIVE_DIRS/HARD_NEGATIVE_DIRS are now consumed by both the existing gather() calls AND the new run_gate() call with the SAME lists, no duplication is introduced here -- just pass the existing module-level constants directly into run_gate().

    6. Update the module docstring's Usage section to document the two new flags with one line each.
  </action>
  <verify>
    <automated>python3 tools/collect_personal_features.py --help 2>&1 | grep -q -- "--gate-only" && python3 tools/collect_personal_features.py --help 2>&1 | grep -q -- "--skip-gate" && echo "OK: both flags present in --help"</automated>
  </verify>
  <done>python3 tools/collect_personal_features.py --help shows both --gate-only and --skip-gate; running with --gate-only exits 0 without creating /tmp/personal_features (verify: rm -rf /tmp/personal_features_gate_only_test; python3 tools/collect_personal_features.py --gate-only --out-dir /tmp/personal_features_gate_only_test; test ! -d /tmp/personal_features_gate_only_test/personal_positive.npy is not the right check since --gate-only returns before out_dir.mkdir -- instead assert the process printed a "Quality gate:" summary line and exited 0, and that no personal_positive.npy/personal_hard_negative.npy file was created anywhere under /tmp/personal_features_gate_only_test).</done>
</task>

<task type="auto">
  <name>Task 4: Add --history-file to fp_rate_eval.py and document the local-vs-remote retrain boundary</name>
  <files>tools/fp_rate_eval.py</files>
  <action>
    In tools/fp_rate_eval.py:

    1. Add a new CLI arg: p.add_argument("--history-file", default=os.path.expanduser("~/.config/heyvox/training/eval_history.jsonl"), help="Append-only JSONL log of every eval run (default: ~/.config/heyvox/training/eval_history.jsonl)").

    2. After the existing "print('\n' + json.dumps({...}, indent=2))" block at the end of main() (the one building the negative_hours/negative_files/positive_files/results summary), add: compute best-passing-threshold as the LOWEST threshold in THRESHOLDS whose corresponding results[i]["gate_pass"] is True (None if no threshold passes -- iterate results in the existing THRESHOLDS order, which is already ascending, so the first gate_pass==True entry found is the lowest passing threshold).

    3. Build a history record dict: {"timestamp": <ISO 8601 UTC via datetime.now(timezone.utc).isoformat()>, "model": args.model, "negative_hours": round(neg_hours, 3), "negative_files": len(neg_scores), "positive_files": pos_n, "per_threshold": results, "best_passing_threshold": <value from step 2, or null>}. Import datetime/timezone at the top of the file (stdlib, no new dependency).

    4. Append this record as a single JSON line to args.history_file: os.makedirs(os.path.dirname(args.history_file), exist_ok=True) then open(args.history_file, "a") as f: f.write(json.dumps(history_record) + "\n") -- append mode only, NEVER open with "w" (would truncate prior history). This must run unconditionally at the end of every main() invocation (no flag needed to enable it -- it is always-on since it is cheap and append-only, matching the "auto after each retrain" framing in the module docstring update below).

    5. Update the module's top-of-file docstring to add a short paragraph clarifying the local-vs-remote retrain boundary: retrain itself runs on Colab (remote GPU, tools/retrain_heyvox_v8.py or equivalent), but fp_rate_eval.py runs LOCALLY because it needs both the LibriSpeech/negative-corpus WAV files AND the downloaded .onnx model artifact on local disk -- so "run fp_rate_eval after every retrain" is a documented manual local post-download step in the retrain workflow, NOT a CI hook or something that runs automatically inside the Colab notebook. State this explicitly so the distinction is not re-litigated later.
  </action>
  <verify>
    <automated>python3 tools/fp_rate_eval.py --help 2>&1 | grep -q -- "--history-file" && echo "OK: --history-file flag present"</automated>
  </verify>
  <done>python3 tools/fp_rate_eval.py --help shows --history-file with the documented default path; the module docstring explains local-vs-remote retrain/eval split; a synthetic run (with a tiny fixture negatives dir) appends exactly one well-formed JSON line to the history file without disturbing any pre-existing lines in it (verify: write two dummy lines to a temp history file first, run the script pointed at that file, confirm the file now has three lines and the first two are byte-identical to before).</done>
</task>

<task type="auto">
  <name>Task 5: Log DEF-167 in DEFECT-LOG.md and add a Patterns entry</name>
  <files>.planning/DEFECT-LOG.md</files>
  <action>
    Append a new entry to .planning/DEFECT-LOG.md, inserted directly after the current last entry (DEF-166) and before the "## Patterns & Process Gaps" section header, following the EXACT structural format used by DEF-165/DEF-166: a level-2 markdown heading "## DEF-167 -- <one-line summary>" (note: the existing file uses an em-dash "-" between the DEF number and summary -- match that exact character, not a plain hyphen), followed by a bulleted list using **Date**:, **Category**:, **Severity**:, **Symptom**:, **Root cause**:, **Fix**:, **Found by**:, **Would have caught earlier**: as bold-prefixed list items (grep the last two entries in the file for the exact markdown syntax before writing -- do not guess the format from this description alone).

    Content requirements for each field:
    - Date: 2026-07-02.
    - Category: state-pollution / error-handling.
    - Severity: S2 (does not crash the app or lose user-facing functionality, but silently degrades the wake-word model's own training data over time -- every retrain compounds the mislabeling).
    - Symptom: describe how this was found -- during a routine training-data audit this session, a resumable Whisper pass (REFERENCE-fp_check_step0.py, absorbed into tools/quality_gate.py by this same plan) was run over fp/ (304 clips at the time) to check how many contained the actual wake word. It found 22 of 192 previously-unaudited fp-reclassified clips (11.5%) DID contain "Hey Vox" (including box/fox/vocks-style Whisper garbles of the user's real voice) -- meaning reclassify_tp_start_as_fp had moved 22 genuine wake-word triggers into the false-positive training set. A companion pass over fn_start-reclassified clips found 54% contained NO wake word at all -- meaning reclassify_fn_start had moved ordinary background-noise TN clips into the false-negative training set purely because a trigger happened to follow within a 5-second window, with zero evidence the TN clip itself contained speech.
    - Root cause: two methods on TrainingCollector (heyvox/audio/training_collector.py) performed RETROACTIVE, evidence-free relabeling based on temporal proximity/outcome heuristics rather than STT evidence: reclassify_tp_start_as_fp(reason) moved the most recent tp_start clip to fp/ any time a trigger's recording aborted (no speech, empty STT, user-cancelled, or post-trim-too-short) -- but "the recording aborted" does not prove "the wake word wasn't actually said" (the user could say "Hey Vox" correctly and then hesitate, get interrupted, or have the mic clip the recording for unrelated reasons). reclassify_fn_start() moved any tn/ clip saved within the prior 5 seconds to fn/ whenever a SUCCESSFUL trigger followed -- but "a trigger followed a hard-negative sample" does not prove the hard-negative clip itself contained a missed wake word; it just means the user spoke again shortly after, for any reason. Both mechanisms manufacture training labels from correlation/timing instead of the transcript evidence that classify_stop_outcome() (the one relabeling path that stayed, save_fn_stop) already correctly required. Net effect: every reclassify_* call polluted the training set with clips whose true content contradicted their assigned label, and both pollution modes push the model toward FALSE NEGATIVES on the user's own voice (real positives got labeled fp; real hard-negatives got labeled fn, i.e. "should have fired here" on audio where no wake word was said).
    - Fix: deleted reclassify_tp_start_as_fp() and reclassify_fn_start() outright, along with the _recent_tp_start/_recent_tn tracking lists and the fn_reclassify_window_secs constructor param that existed solely to feed them, and removed all six call sites across heyvox/recording.py and heyvox/main.py. save_fn_stop() (evidence-based via classify_stop_outcome(), unchanged) remains the only automatic fn-labeling path from the live daemon. In its place, built a MANDATORY batch Whisper quality gate (tools/quality_gate.py, wired into tools/collect_personal_features.py ahead of every featurization run) that re-derives BOTH directions of evidence-based correction from the transcript itself: any positive-dir clip without the wake word is quarantined; any negative-dir clip WITH the wake word is recovered into positives/ (short clips) or quarantine/ (long clips, likely contains more than just the wake word) -- this is the new evidence-based fn-recovery mechanism, replacing the deleted heuristic one, but gated on STT proof rather than timing correlation.
    - Found by: this session's manual investigation, starting from a Whisper sanity pass over fn/ + a backed-up fp_garbled/ subset (tools/whisper_sanity_pass.py, pre-existing) that had never been run over the full fp/ directory or the fn_start-reclassified clips specifically -- extending that audit to ALL of fp/ (not just the fp_garbled subtype) via the more robust REFERENCE-fp_check_step0.py driver surfaced the 22-clip contamination; a parallel audit of fn_start clips surfaced the 54% figure.
    - Would have caught earlier: a MANDATORY Whisper gate over ALL training-data directories (this plan's Task 2/3) run automatically before every featurization, rather than an ad-hoc manual audit script that only ever covered a subset of one category (fn/ and a backed-up fp_garbled/ sample) and had to be manually re-scoped and re-run this session to find the gap. General pattern: any heuristic/retroactive relabeling of ALREADY-COLLECTED evidence-backed data (a saved audio clip is direct evidence; a temporal correlation is not) should never ship without either (a) requiring the same evidence standard as the mechanism it's "correcting," or (b) being paired with a permanent, mandatory audit step -- not a one-off script run manually after the fact once contamination is suspected.

    Also add one line noting the manual precursor to this automated gate: "Schritt-0 cleanup already moved 26 clips out of fp/ manually before this fix landed (22 to positives/, 4 to quarantine/; manifest: recfp_cleanup_manifest_20260702_084240.json) -- that hand-run pass is what the automated gate in this plan now performs on every collect run."

    In the "## Patterns & Process Gaps" section at the bottom of the file, add one new bullet (after the existing P-log-path-split and P-unbounded-native-call bullets) named **P-retroactive-labeling** (DEF-167): summarize that any training/label-correction mechanism which infers a label from OUTCOME OR TIMING rather than re-examining the underlying evidence (audio transcript, in this case) risks manufacturing exactly the class of error it claims to fix -- and note the general action item: any future auto-labeling code path in this training pipeline must be evidence-based (derive the label from re-inspecting the actual data, e.g. via STT) or must run through the same mandatory tools/quality_gate.py chokepoint before its output is trusted.
  </action>
  <verify>
    <automated>grep -q "DEF-167" .planning/DEFECT-LOG.md && grep -q "P-retroactive-labeling" .planning/DEFECT-LOG.md && grep -c "^## DEF-" .planning/DEFECT-LOG.md</automated>
  </verify>
  <done>.planning/DEFECT-LOG.md contains a DEF-167 entry with all 8 required fields (Date, Category, Severity, Symptom, Root cause, Fix, Found by, Would have caught earlier) in the same structural format as DEF-165/166, references the 22/192 (11.5%) and 54% measured contamination rates, references the recfp_cleanup_manifest_20260702_084240.json precursor, and the Patterns section has a new P-retroactive-labeling bullet.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|--------------|
| Filesystem (training clip dirs) -> Whisper decode | Every clip in tp/fp/tn/fn/positives/recordings*/negatives is fed to mlx_whisper.transcribe. A malformed/adversarial WAV could theoretically trigger a decode hang or crash -- these clips are all locally-recorded, not remotely-sourced or user-uploaded, so this is a reliability concern (already measured: a noise clip caused a 15-minute hang without the fixes in this plan) rather than a security concern. |
| tools/quality_gate.py subprocess worker -> mlx_whisper | The --_worker subcommand is invoked via subprocess.run([sys.executable, __file__, ...]) with a hard timeout. This is process isolation for reliability (killable on hang), not for untrusted-input sandboxing -- the worker runs with the same privileges as the parent, on the same local files. |
| tools/quality_gate.py -> filesystem moves | shutil.move operations relocate files between tp/fp/tn/fn/positives/quarantine directories, all under the user's own ~/.config/heyvox/training/ tree. No path is ever deleted -- every move is reversible via the written manifest, matching the existing recfp_cleanup_manifest_*.json precedent. |
| heyvox/main.py / recording.py (live daemon) -> training_collector.py | Removing reclassify_tp_start_as_fp/reclassify_fn_start changes live-daemon behavior (Task 1). This is the only trust boundary in this plan touching the always-running process rather than an offline tools/ script. |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|------------------|
| T-quick260702-01 | Denial of Service | mlx_whisper.transcribe (per-clip) | mitigate | Per-clip subprocess with hard timeout (_CLIP_TIMEOUT_SECS=20) enforced via subprocess.run(timeout=...), which SIGKILLs the child on expiry -- proven necessary this session (a naive in-process call hung 15 minutes on a single noise clip with no way to interrupt it from a thread) |
| T-quick260702-02 | Denial of Service | Batch gate run across hundreds of clips | mitigate | Write-ahead JSONL resume (results.jsonl + inflight.txt) means a killed/crashed run (session teardown, manual interrupt, OOM) loses at most one in-flight clip on restart, not the whole run's progress |
| T-quick260702-03 | Tampering (data integrity) | Training label correctness | mitigate | This entire plan's Task 1+2+3 -- removing evidence-free retroactive relabeling and replacing it with STT-transcript-derived verdicts is itself the primary mitigation for the DEF-167 defect (mislabeled training data silently corrupting the model) |
| T-quick260702-04 | Repudiation (of automated moves) | tools/quality_gate.py file moves | mitigate | Every move is quarantine-only (never delete) and recorded in a timestamped, from/to-shaped manifest file matching the existing recfp_cleanup_manifest_*.json precedent -- fully auditable/reversible after the fact |
| T-quick260702-05 | Information Disclosure | eval_history.jsonl / gate manifest contents | accept | Both files contain only local filesystem paths, clip durations, and STT transcripts of the single owner's own recorded voice/ambient audio, written to the user's own ~/.config/heyvox/training/ tree -- no new exposure surface versus the pre-existing training clip storage |
| T-quick260702-06 | Tampering (supply chain) | No new package installs in this plan | accept | This plan adds zero new third-party dependencies (mlx_whisper, numpy, soundfile/wave are all already used elsewhere in this codebase) -- no Package Legitimacy Gate applies |
</threat_model>

<verification>
1. python3 -m pytest tests/test_training_collector.py -v -- all tests pass (existing 8 + 4 new from Task 1).
2. python3 -m pytest tests/test_defect_guards.py -v -- full regression suite stays green (no training_collector call-site removal breaks an unrelated defect-guard test).
3. grep -rn "reclassify_fn_start\|reclassify_tp_start_as_fp" heyvox/ --include="*.py" -- zero matches.
4. python3 -c "import sys; sys.path.insert(0,'tools'); import quality_gate, collect_personal_features" -- both modules import cleanly with no circular-import error.
5. python3 tools/collect_personal_features.py --help shows --gate-only and --skip-gate.
6. python3 tools/fp_rate_eval.py --help shows --history-file.
7. grep -q "DEF-167" .planning/DEFECT-LOG.md -- entry present.
8. Manual smoke test (small subset, not the full multi-hundred-clip corpus -- that is a separate, longer-running validation the user runs after this plan lands, not part of automated verify): python3 tools/quality_gate.py --_worker <path to one real tp/ clip> returns a JSON line with text containing a wake-word-like phrase within a few seconds.
9. Daemon restart required after Task 1 (training_collector.py/recording.py/main.py are live-daemon hot-path files): launchctl kickstart -k "gui/$UID/com.heyvox.listener" (per reference_launchctl_kickstart memory) after Task 1 lands, before relying on the new no-retroactive-relabeling behavior in a live session. Tasks 2-4 (tools/ scripts) have zero live-daemon impact and need no restart.
</verification>

<success_criteria>
- TrainingCollector no longer contains any evidence-free retroactive relabeling method; all six call sites across recording.py/main.py are removed; existing evidence-based save_fn_stop path (classify_stop_outcome) is unchanged.
- tools/quality_gate.py exists as a mandatory, resumable, timeout-hardened batch Whisper gate reusing the proven _WW_RE matcher and REFERENCE-fp_check_step0.py's robustness patterns; it quarantines mismatches (never deletes) and recovers evidence-based false negatives from negative dirs into positives/.
- tools/collect_personal_features.py runs the gate by default before every featurization, with --gate-only and --skip-gate as the only two ways to deviate from that default.
- tools/fp_rate_eval.py appends one JSONL record per run to an append-only history file, with the local-vs-remote retrain/eval boundary documented in its module docstring.
- .planning/DEFECT-LOG.md has a complete DEF-167 entry plus a new P-retroactive-labeling Patterns bullet.
- Every automated test referenced in the verification section passes.
- Deferred (explicitly out of scope for this plan, per task_scope): an env-diversity recording guide (music/distance/outdoor conditions) -- noted here only so it is not silently forgotten, no task in this plan addresses it.
</success_criteria>

<output>
Create `.planning/quick/260702-cdg-wake-word-training-data-quality-gate/260702-cdg-SUMMARY.md` when done
</output>

"""Whisper sanity pass over fn/ and fp_garbled/ training clips.

Checks:
  - fn/ clips (treated as positives): should contain "Hey Vox". Clips without
    a wake-word-like phrase are suspect and should be removed from positives.
  - fp_garbled/ clips (treated as negatives): should NOT contain "Hey Vox".
    Clips that do are mislabeled positives and must be removed from negatives.

Output:
  - Prints a summary table per clip.
  - Writes two files to --out-dir:
      fn_suspect.txt   — fn clips with no wake word detected (remove from positives)
      fp_real.txt      — fp_garbled clips with wake word detected (keep out of negatives)

Usage:
    python3 tools/whisper_sanity_pass.py \\
        --fn-dir ~/.config/heyvox/training/fn \\
        --fp-garbled-dir /tmp/fp_garbled_backup \\
        --out-dir /tmp/sanity_pass
"""

from __future__ import annotations

import argparse
import re
import sys
import wave
from pathlib import Path

import numpy as np

# "Hey" variants across languages Whisper may output for the same phoneme:
#   hey (EN), hej (SV), hei (NO/FI), hé (FR), he (clipped)
_HEY = r"h[eé](?:y|j|i)?"

# "Vox" variants — phonetically close misreadings Whisper commonly produces:
#   vox, voks, vops, vop, vap, vack, vocks, wax, ox (clipped),
#   box, boks, bops, bock, bok, bak, bud, bub,  (b≈v confusion)
#   docs, dox, fox, foks, locks, rocks  (distant but seen in logs)
_VOX = r"(?:v[ao][xcks]+|v[ao]p[s]?|v[ao]t|w[ao][xp][s]?|[bdfl][ao][xcks]+|[bdfl][ao]p[s]?|ox)"

# Optional punctuation/spaces between the two words, including comma+space
_SEP = r"[,\s]*"

_WW_RE = re.compile(rf"\b{_HEY}{_SEP}{_VOX}\b", re.IGNORECASE)

# Whisper hallucination patterns — clips producing these are definitely suspect
# regardless of whether a wake-word variant appears anywhere.
_HALLUCINATION_RE = re.compile(
    r"(?:harriet[\s,]*){2,}"          # repeated "Harriet"
    r"|(?:evet[\s,]*){3,}"             # Turkish "yes" loop
    r"|(?:see[\s,]*){5,}"              # English repeat loop
    r"|hãy subscribe"                  # Vietnamese YouTube spam
    r"|субтитры сделал"                # Russian subtitle spam
    r"|ghiền mì gõ",                   # Vietnamese channel name
    re.IGNORECASE,
)


def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, sr


def _transcribe(audio: np.ndarray, sr: int, model) -> str:
    import mlx_whisper
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        word_timestamps=False,
        condition_on_previous_text=False,
        compression_ratio_threshold=2.4,
        logprob_threshold=-1.0,
        no_speech_threshold=0.6,
        language=None,
    )
    return result.get("text", "").strip()


def _contains_wake_word(text: str) -> bool:
    """True if text contains a plausible wake-word transcription.

    Also returns False for known Whisper hallucination loops, since those clips
    are silent/noise and should be treated as suspect regardless.
    """
    if _HALLUCINATION_RE.search(text):
        return False
    return bool(_WW_RE.search(text))


def process_dir(
    clips_dir: Path,
    expected_label: str,
    out_list: list[str],
    *,
    verbose: bool,
) -> tuple[int, int]:
    """Transcribe all .wav files in clips_dir.

    expected_label: "positive" (fn clips) or "negative" (fp_garbled clips).
    out_list: populated with paths of clips that violate their expected label.
    Returns (total, violations).
    """
    import mlx_whisper  # noqa: F401 — pre-load check

    wavs = sorted(clips_dir.glob("*.wav"))
    if not wavs:
        print(f"  No .wav files in {clips_dir}", file=sys.stderr)
        return 0, 0

    total = len(wavs)
    violations = 0
    for i, wav_path in enumerate(wavs, 1):
        audio, sr = _load_wav(wav_path)
        text = _transcribe(audio, sr, None)
        has_ww = _contains_wake_word(text)

        if expected_label == "positive" and not has_ww:
            violations += 1
            out_list.append(str(wav_path))
            flag = "SUSPECT (no WW)"
        elif expected_label == "negative" and has_ww:
            violations += 1
            out_list.append(str(wav_path))
            flag = "MISLABELED (has WW)"
        else:
            flag = "ok"

        if verbose or flag != "ok":
            print(f"  [{i}/{total}] {wav_path.name}: {flag!r}  — {text!r}",
                  file=sys.stderr)
        elif i % 50 == 0:
            print(f"  [{i}/{total}] ...", file=sys.stderr)

    return total, violations


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fn-dir", default="~/.config/heyvox/training/fn")
    ap.add_argument("--fp-garbled-dir", default="/tmp/fp_garbled_backup")
    ap.add_argument("--out-dir", default="/tmp/sanity_pass")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    fn_dir = Path(args.fn_dir).expanduser().resolve()
    fp_dir = Path(args.fp_garbled_dir).expanduser().resolve()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Whisper sanity pass ===", file=sys.stderr)
    print(f"fn dir:         {fn_dir}  ({len(list(fn_dir.glob('*.wav')))} clips)",
          file=sys.stderr)
    print(f"fp_garbled dir: {fp_dir}  ({len(list(fp_dir.glob('*.wav')))} clips)",
          file=sys.stderr)

    fn_suspect: list[str] = []
    fp_real: list[str] = []

    print(f"\n--- fn/ clips (expected: contain Hey Vox) ---", file=sys.stderr)
    fn_total, fn_violations = process_dir(fn_dir, "positive", fn_suspect,
                                          verbose=args.verbose)

    print(f"\n--- fp_garbled/ clips (expected: no Hey Vox) ---", file=sys.stderr)
    fp_total, fp_violations = process_dir(fp_dir, "negative", fp_real,
                                          verbose=args.verbose)

    fn_out = out_dir / "fn_suspect.txt"
    fp_out = out_dir / "fp_real.txt"
    fn_out.write_text("\n".join(fn_suspect) + ("\n" if fn_suspect else ""))
    fp_out.write_text("\n".join(fp_real) + ("\n" if fp_real else ""))

    print(f"\n=== Results ===", file=sys.stderr)
    print(f"fn clips:         {fn_total} total, {fn_violations} suspect "
          f"(no wake word) → {fn_out}", file=sys.stderr)
    print(f"fp_garbled clips: {fp_total} total, {fp_violations} mislabeled "
          f"(has wake word) → {fp_out}", file=sys.stderr)
    print(f"\nNext steps:", file=sys.stderr)
    print(f"  - fn_suspect.txt  → remove these from fn/ before re-featurising",
          file=sys.stderr)
    print(f"  - fp_real.txt     → keep these OUT of fp/ (leave in backup)",
          file=sys.stderr)


if __name__ == "__main__":
    main()

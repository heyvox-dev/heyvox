"""Step 0 (robust/resumable): measure how many fp/ clips contain 'Hey Vox'.

Reuses whisper_sanity_pass's regex (_contains_wake_word) for a comparable
verdict, but transcribes with production-grade decode params (DEF-075/083
thresholds) so noise clips bail out of repeat-token loops instead of hanging.

Resumability (a single pathological clip can still stall MLX):
  - results.jsonl : one JSON line per COMPLETED clip  (the source of truth)
  - inflight.txt  : name of the clip currently being decoded (write-ahead)
On restart, any name in inflight.txt but not in results.jsonl was the clip
that hung last run -> it is recorded as {"timeout": true} and skipped.

Run under a Bash `timeout`; on kill (exit 124) just re-run — it resumes and
skips the offender. Exits 0 only when every clip has a results.jsonl entry.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

# Below this RMS (int16-equivalent) a clip is effectively silence/hum. Whisper
# with a forced language loops on silence — and silence can't contain a wake
# word — so skip transcription entirely. Matches the collector's _MIN_SPEECH_RMS
# spirit (300) but a bit lower so borderline-quiet speech is still decoded.
_SILENCE_RMS = 150.0 / 32768.0

TOOLS = Path("/Users/work/conductor/workspaces/vox-v2/seattle/tools")
sys.path.insert(0, str(TOOLS))
from whisper_sanity_pass import _load_wav, _contains_wake_word  # noqa: E402

OUT = Path("/tmp/fp_check_step0")
OUT.mkdir(exist_ok=True)
RESULTS = OUT / "results.jsonl"
INFLIGHT = OUT / "inflight.txt"

FP_DIR = Path("~/.config/heyvox/training/fp").expanduser().resolve()


def subtype(name: str) -> str:
    parts = name.split("_")
    return parts[1] if len(parts) > 1 else "?"


def transcribe(audio, sr) -> str:
    import mlx_whisper
    r = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        language="en",                       # stability > multilang for a 2s WW check
        condition_on_previous_text=False,    # DEF-075: don't amplify loops
        compression_ratio_threshold=2.2,     # DEF-075: bail from degenerate decode
        logprob_threshold=-0.8,              # DEF-075
        no_speech_threshold=0.6,
        word_timestamps=False,
    )
    return (r.get("text") or "").strip()


def load_done() -> set[str]:
    done: set[str] = set()
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    done.add(json.loads(line)["name"])
                except Exception:  # noqa: BLE001
                    pass
    return done


def append(rec: dict) -> None:
    with RESULTS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()


def main() -> int:
    done = load_done()

    # Write-ahead recovery: a clip marked inflight but never completed = last hang.
    if INFLIGHT.exists():
        stalled = INFLIGHT.read_text().strip()
        if stalled and stalled not in done:
            append({"name": stalled, "subtype": subtype(stalled),
                    "has_ww": False, "timeout": True, "text": ""})
            done.add(stalled)
            print(f"  recovered: skipped stalled clip {stalled}", file=sys.stderr)
        INFLIGHT.unlink()

    # Non-garbled first (the 192 the prior pass never checked); garbled last.
    wavs = sorted(FP_DIR.glob("*.wav"),
                  key=lambda p: (subtype(p.name) == "garbled", p.name))
    todo = [w for w in wavs if w.name not in done]
    print(f"fp dir: {FP_DIR}  ({len(wavs)} total, {len(done)} done, "
          f"{len(todo)} to go)", file=sys.stderr)

    for i, w in enumerate(todo, 1):
        INFLIGHT.write_text(w.name)
        with INFLIGHT.open("a") as f:
            f.flush()
        t0 = time.time()
        try:
            audio, sr = _load_wav(w)
            rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
            if rms < _SILENCE_RMS:
                append({"name": w.name, "subtype": subtype(w.name),
                        "has_ww": False, "timeout": False, "silent": True,
                        "rms": round(rms, 5), "text": ""})
                continue
            text = transcribe(audio, sr)
            has = _contains_wake_word(text)
            append({"name": w.name, "subtype": subtype(w.name),
                    "has_ww": has, "timeout": False, "text": text})
        except Exception as e:  # noqa: BLE001
            append({"name": w.name, "subtype": subtype(w.name),
                    "has_ww": False, "timeout": False, "error": str(e), "text": ""})
        dt = time.time() - t0
        if i % 10 == 0 or dt > 8:
            print(f"  [{i}/{len(todo)}] {w.name[:38]} {dt:.1f}s", file=sys.stderr)

    if INFLIGHT.exists():
        INFLIGHT.unlink()
    print("ALL DONE", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

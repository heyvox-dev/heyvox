#!/usr/bin/env python3
"""Interactive by-ear review of the wake-word clips the quality gate is unsure about.

The gate (tools/quality_gate.py) auto-decides the clear cases: high-score
triggers are kept, clear garbage (empty/loops) is quarantined. The uncertain
low-score positives -- clips where Whisper couldn't cleanly read "hey vox" but
they may well be your real (hard/quiet/far) wake word -- are left for you.

This tool plays each one and you decide with a single keypress:

    k / SPACE / →   KEEP     (it IS your wake word -- leave it in positives)
    n / DELETE      QUARANTINE (it is NOT -- move to quarantine/)
    r               REPLAY
    s               SKIP for now (ask again next run)
    u               UNDO the previous decision
    q               QUIT (progress is saved; resume anytime)

Decisions persist to <state-dir>/review_decisions.jsonl, so quitting and
re-running resumes where you left off. Every quarantine move is recorded in a
reversible manifest and NEVER deletes a file.

Reads the gate's dry-run verdicts, so run the gate first (a dry run is enough):
    python3 tools/quality_gate.py            # dry run, writes .gate_state/
    python3 tools/review_clips.py            # then review the uncertain ones

Usage:
    python3 tools/review_clips.py [--state-dir DIR] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import termios
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality_gate import (  # noqa: E402
    _DEFAULT_STATE_DIR, _TRUST_SCORE, _is_garbage, _results_path, _safe_dest,
)

_QUARANTINE_DIR = Path("~/.config/heyvox/training/quarantine").expanduser()


def _getch() -> str:
    """Read a single keypress (no Enter). Returns '' on EOF."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    # Map arrow keys (ESC [ C = right) to a single token; swallow other escapes.
    if ch == "\x1b":
        rest = sys.stdin.read(2) if sys.stdin.readable() else ""
        return "RIGHT" if rest.endswith("C") else "\x1b"
    return ch


def _uncertain_positives(state_dir: Path) -> list[dict]:
    """The low-score positives the gate leaves for manual review: not trusted,
    no clean wake word, and not clear garbage. Highest score first (strongest
    'probably real' candidates lead)."""
    rp = _results_path(state_dir)
    if not rp.exists():
        sys.exit(f"No gate results at {rp} -- run `python3 tools/quality_gate.py` first.")
    out = []
    for line in rp.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a partial last line if the gate is still writing
        if r.get("side") != "positive":
            continue
        score = r.get("score")
        if score is None or score >= _TRUST_SCORE:
            continue
        if r.get("has_ww") or _is_garbage(r.get("text", "")):
            continue
        out.append(r)
    out.sort(key=lambda r: -(r.get("score") or 0.0))
    return out


def _load_decisions(path: Path) -> dict[str, str]:
    d: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    d[rec["path"]] = rec["decision"]
                except Exception:  # noqa: BLE001
                    pass
    return d


def _append_decision(path: Path, clip_path: str, decision: str) -> None:
    with path.open("a") as f:
        f.write(json.dumps({"path": clip_path, "decision": decision}) + "\n")
        f.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=_DEFAULT_STATE_DIR)
    ap.add_argument("--limit", type=int, default=0, help="Review at most N this run (0 = all).")
    args = ap.parse_args()

    _QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    decisions_path = args.state_dir / "review_decisions.jsonl"
    manifest_path = args.state_dir / "review_manifest.jsonl"
    decided = _load_decisions(decisions_path)

    clips = _uncertain_positives(args.state_dir)
    todo = [r for r in clips if r["path"] not in decided]
    if args.limit:
        todo = todo[: args.limit]

    total = len(clips)
    print(f"\nUncertain positives: {total} total, {len(decided)} already decided, "
          f"{len(todo)} to review this run.")
    print("Keys:  [k/space]=keep  [n]=quarantine  [r]=replay  [s]=skip  [u]=undo  [q]=quit\n")

    kept = quar = 0
    history: list[tuple[dict, str]] = []
    i = 0
    while i < len(todo):
        r = todo[i]
        clip = Path(r["path"])
        if not clip.exists():
            print(f"  [{i+1}/{len(todo)}] gone (already moved): {clip.name}")
            _append_decision(decisions_path, r["path"], "missing")
            i += 1
            continue
        print(f"[{i+1}/{len(todo)}] score={r.get('score')}  [{clip.parent.name}]  "
              f"whisper heard: {r.get('text','')[:60]!r}")
        subprocess.run(["afplay", str(clip)], check=False)

        while True:
            key = _getch().lower()
            if key in ("k", " ", "right", "j"):
                _append_decision(decisions_path, r["path"], "keep")
                history.append((r, "keep"))
                kept += 1
                print("    -> KEEP\n")
                i += 1
                break
            if key in ("n", "d", "\x7f"):
                dst = _safe_dest(_QUARANTINE_DIR, clip.name)
                try:
                    shutil.move(str(clip), str(dst))
                except OSError as e:
                    print(f"    ! move failed: {e}\n")
                    break
                with manifest_path.open("a") as f:
                    f.write(json.dumps({"from": str(clip), "to": str(dst),
                                        "text": r.get("text", ""),
                                        "score": r.get("score")}) + "\n")
                _append_decision(decisions_path, r["path"], "quarantine")
                history.append((r, "quarantine"))
                quar += 1
                print("    -> QUARANTINE\n")
                i += 1
                break
            if key == "r":
                subprocess.run(["afplay", str(clip)], check=False)
                continue
            if key == "s":
                print("    -> skipped (will ask again)\n")
                i += 1
                break
            if key == "u":
                if not history:
                    print("    (nothing to undo)")
                    continue
                prev_r, prev_dec = history.pop()
                # rewrite decisions file without the last entry for prev_r
                lines = [l for l in decisions_path.read_text().splitlines()
                         if l.strip() and json.loads(l)["path"] != prev_r["path"]]
                decisions_path.write_text("\n".join(lines) + ("\n" if lines else ""))
                if prev_dec == "quarantine":
                    # move it back
                    src = _QUARANTINE_DIR / Path(prev_r["path"]).name
                    if src.exists():
                        shutil.move(str(src), prev_r["path"])
                    quar -= 1
                else:
                    kept -= 1
                todo.insert(i, prev_r)  # re-review it
                print(f"    -> UNDID {prev_dec} on {Path(prev_r['path']).name}\n")
                break
            if key in ("q", "\x03", ""):
                print(f"\nStopped. This run: kept {kept}, quarantined {quar}. "
                      f"Resume anytime with the same command.")
                return 0
            print("    ? keys: k=keep n=quarantine r=replay s=skip u=undo q=quit")

    print(f"\nDone. This run: kept {kept}, quarantined {quar}.")
    print(f"Decisions: {decisions_path}")
    if quar:
        print(f"Quarantine manifest (reversible): {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

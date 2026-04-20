"""Offline eval harness for the deployed hey_vox wake word model.

Scores every WAV in ~/.config/heyvox/training/{tp,fn,tn,fp,positives}/
using the same openwakeword streaming path the runtime uses, then
reports per-category and per-mic recall / false-fire rates against
the production threshold.

Run:
    python3 tools/wakeword_eval.py \
        [--model PATH] [--threshold 0.65] [--report OUT.md]

Output is printed to stdout and (optionally) written as Markdown.
Read-only — never touches the model, config, or collected clips.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

DEFAULT_MODEL = Path.home() / ".config/heyvox/models/hey_vox.onnx"
DEFAULT_BASE = Path.home() / ".config/heyvox/training"
DEFAULT_THRESHOLD = 0.65
CATEGORIES = ("tp", "fn", "tn", "fp", "positives")
FIRE_EXPECTED = {"tp": True, "fn": True, "positives": True, "tn": False, "fp": False}

# Filename: {cat}_{suffix?}_{mic-tag?}_{YYYYMMDD_HHMMSS}_score{X.XX}.wav
# We extract mic-tag when present; otherwise label as "untagged" (pre-mic-tagging era).
_FNAME_RE = re.compile(
    r"^(?P<cat>tp|fp|tn|fn)_"
    r"(?:(?P<suffix>start|stop|garbled|no-speech)_)?"
    r"(?:(?P<mic>[a-z0-9][a-z0-9\-]*?)_)?"
    r"(?P<ts>\d{8}_\d{6})_score(?P<score>[0-9.]+)\.wav$"
)


def parse_filename(path: Path) -> dict:
    m = _FNAME_RE.match(path.name)
    if not m:
        return {"cat": "?", "mic": "untagged", "orig_score": None, "ts": ""}
    g = m.groupdict()
    try:
        orig = float(g["score"])
    except (TypeError, ValueError):
        orig = None
    return {
        "cat": g["cat"],
        "suffix": g.get("suffix") or "",
        "mic": g.get("mic") or "untagged",
        "ts": g["ts"],
        "orig_score": orig,
    }


def score_clip(model, path: Path) -> float:
    """Return max streaming score for a clip, using the runtime feature chain.

    Label-agnostic: some exported models key the score by "hey_vox", others by
    "hey_vox.onnx" (the filename). We load a single model, so taking max over
    all values in each per-frame prediction dict is safe.
    """
    model.reset()
    preds = model.predict_clip(str(path), padding=1)
    if not preds:
        return 0.0
    best = 0.0
    for p in preds:
        if p:
            v = max(float(x) for x in p.values())
            if v > best:
                best = v
    return best


def classify(score: float, thr: float, should_fire: bool) -> str:
    fired = score >= thr
    if should_fire and fired:
        return "correct_fire"
    if should_fire and not fired:
        return "miss"
    if (not should_fire) and fired:
        return "false_fire"
    return "correct_silence"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def aggregate(rows: list[dict]) -> dict:
    """Group per-clip rows by category and by mic. Returns nested dict."""
    by_cat: dict[str, dict] = {}
    for cat in CATEGORIES:
        subset = [r for r in rows if r["cat"] == cat]
        scores = [r["score"] for r in subset]
        if not scores:
            by_cat[cat] = {"count": 0}
            continue
        outcomes = defaultdict(int)
        for r in subset:
            outcomes[r["outcome"]] += 1
        by_cat[cat] = {
            "count": len(subset),
            "min": min(scores),
            "median": statistics.median(scores),
            "mean": statistics.mean(scores),
            "p90": _percentile(scores, 0.90),
            "max": max(scores),
            "outcomes": dict(outcomes),
        }

    by_mic_cat: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_mic_cat[r["mic"]][r["cat"]].append(r)
    mic_summary: dict[str, dict] = {}
    for mic, cats in by_mic_cat.items():
        entry: dict = {}
        for cat in CATEGORIES:
            subset = cats.get(cat, [])
            if not subset:
                continue
            correct = sum(1 for r in subset if r["outcome"] in ("correct_fire", "correct_silence"))
            entry[cat] = {
                "count": len(subset),
                "correct": correct,
                "median_score": statistics.median([r["score"] for r in subset]),
            }
        mic_summary[mic] = entry
    return {"by_cat": by_cat, "by_mic": mic_summary}


def recall(by_cat: dict, cat: str) -> tuple[float, int, int]:
    c = by_cat.get(cat, {})
    if not c.get("count"):
        return 0.0, 0, 0
    fires = c["outcomes"].get("correct_fire", 0)
    return fires / c["count"], fires, c["count"]


def false_fire_rate(by_cat: dict, cat: str) -> tuple[float, int, int]:
    c = by_cat.get(cat, {})
    if not c.get("count"):
        return 0.0, 0, 0
    ff = c["outcomes"].get("false_fire", 0)
    return ff / c["count"], ff, c["count"]


def render_markdown(summary: dict, thr: float, model_path: Path, elapsed: float) -> str:
    lines: list[str] = []
    lines.append("# Wake-Word Eval Baseline\n")
    lines.append(f"- **Model:** `{model_path}`")
    lines.append(f"- **Threshold (fire if score >=):** `{thr}`")
    lines.append(f"- **Wall clock:** {elapsed:.1f}s")
    lines.append(f"- **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    by_cat = summary["by_cat"]
    lines.append("## Per-Category Scoreboard\n")
    lines.append("| Category | N | Fires ≥ thr | Silent < thr | Recall / FFR | Median | Mean | p90 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for cat in CATEGORIES:
        c = by_cat.get(cat, {})
        if not c.get("count"):
            lines.append(f"| {cat} | 0 | - | - | - | - | - | - |")
            continue
        fires = c["outcomes"].get("correct_fire", 0) + c["outcomes"].get("false_fire", 0)
        silent = c["count"] - fires
        if FIRE_EXPECTED[cat]:
            rec, _, _ = recall(by_cat, cat)
            metric = f"**recall={rec:.1%}**"
        else:
            ffr, _, _ = false_fire_rate(by_cat, cat)
            metric = f"**FFR={ffr:.1%}**"
        lines.append(
            f"| {cat} | {c['count']} | {fires} | {silent} | {metric} | "
            f"{c['median']:.2f} | {c['mean']:.2f} | {c['p90']:.2f} |"
        )
    lines.append("")

    # Headline numbers
    tp_rec, tp_fires, tp_n = recall(by_cat, "tp")
    fn_rec, fn_fires, fn_n = recall(by_cat, "fn")
    pos_rec, pos_fires, pos_n = recall(by_cat, "positives")
    tn_ffr, tn_ff, tn_n = false_fire_rate(by_cat, "tn")
    fp_ffr, fp_ff, fp_n = false_fire_rate(by_cat, "fp")

    lines.append("## Headline\n")
    lines.append(f"- TP recall: **{tp_rec:.1%}** ({tp_fires}/{tp_n}) — clips the model already got right should still fire.")
    lines.append(f"- FN recovery: **{fn_rec:.1%}** ({fn_fires}/{fn_n}) — historic misses the model would fire on today.")
    lines.append(f"- Positives recall: **{pos_rec:.1%}** ({pos_fires}/{pos_n}) — curated positive bank.")
    lines.append(f"- TN false-fire rate: **{tn_ffr:.1%}** ({tn_ff}/{tn_n}) — hard negatives that slip through.")
    lines.append(f"- FP repeat rate: **{fp_ffr:.1%}** ({fp_ff}/{fp_n}) — prior false alarms still firing.\n")

    mics = summary["by_mic"]
    if mics:
        lines.append("## Per-Mic Breakdown\n")
        lines.append("| Mic | TP n | TP recall | FN n | FN recovered | TN n | TN FFR |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for mic in sorted(mics.keys()):
            e = mics[mic]
            def fmt(cat, correct_key):
                d = e.get(cat)
                if not d:
                    return "-", "-"
                n = d["count"]
                if cat in ("tp", "fn", "positives"):
                    return str(n), f"{d['correct']/n:.1%}"
                else:
                    return str(n), f"{(n - d['correct'])/n:.1%}"
            tp_n, tp_r = fmt("tp", True)
            fn_n, fn_r = fmt("fn", True)
            tn_n, tn_r = fmt("tn", False)
            lines.append(f"| {mic} | {tp_n} | {tp_r} | {fn_n} | {fn_r} | {tn_n} | {tn_r} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--clips-root", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None, help="Write raw per-clip rows as JSONL")
    ap.add_argument("--limit", type=int, default=0, help="Cap clips per category for a quick smoke test")
    args = ap.parse_args()

    if not args.model.exists():
        print(f"ERROR: model not found: {args.model}", file=sys.stderr)
        return 2

    from openwakeword.model import Model

    print(f"Loading model: {args.model}", file=sys.stderr)
    model = Model(wakeword_models=[str(args.model)], inference_framework="onnx")

    rows: list[dict] = []
    start = time.time()
    for cat in CATEGORIES:
        cat_dir = args.clips_root / cat
        if not cat_dir.is_dir():
            print(f"  (skipping missing dir {cat_dir})", file=sys.stderr)
            continue
        clips = sorted(cat_dir.glob("*.wav"))
        if args.limit:
            clips = clips[: args.limit]
        print(f"  {cat}: scoring {len(clips)} clips", file=sys.stderr)
        for i, wav in enumerate(clips, 1):
            meta = parse_filename(wav)
            # Filenames in "positives/" often use fn_* prefix, but the
            # directory itself defines category for scoring purposes.
            cat_for_scoring = cat
            should_fire = FIRE_EXPECTED[cat_for_scoring]
            try:
                score = score_clip(model, wav)
            except Exception as e:
                print(f"    skip {wav.name}: {e}", file=sys.stderr)
                continue
            outcome = classify(score, args.threshold, should_fire)
            rows.append({
                "cat": cat_for_scoring,
                "mic": meta["mic"],
                "score": float(score),
                "orig_score": meta["orig_score"],
                "outcome": outcome,
                "file": wav.name,
            })
            if i % 100 == 0:
                print(f"    {i}/{len(clips)}", file=sys.stderr)

    elapsed = time.time() - start
    summary = aggregate(rows)
    md = render_markdown(summary, args.threshold, args.model, elapsed)
    print(md)

    if args.report:
        args.report.write_text(md)
        print(f"\nReport written: {args.report}", file=sys.stderr)
    if args.json:
        args.json.write_text("\n".join(json.dumps(r) for r in rows))
        print(f"Raw rows: {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

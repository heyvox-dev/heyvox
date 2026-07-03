"""Collect every personal wake-word clip, featurise it, and pack for Colab.

Produces a tarball containing two .npy files:
  - personal_positive.npy         (N, 16, 96)   label=1
  - personal_hard_negative.npy    (M, 16, 96)   label=0

Retrain pipeline on Colab ingests this tarball alongside the existing
openwakeword synthetic features, appending personal_positive into
positive_features_train.npy and adding personal_hard_negative as a new
class in feature_data_files.

Source directory roles (hard-coded — this is a single-owner personal model):

  Positives (should fire — label 1):
    ~/.config/heyvox/training/tp          runtime TPs (user's voice, real mic)
    ~/.config/heyvox/training/positives   clean auto-collected positives
    ~/.config/heyvox/training/fn          runtime false negatives (evidence-based, save_fn_stop)
    training/recordings                   user recordings
    training/recordings_friends           friends recordings (voice diversity)
    training/recordings_jabra             jabra-specific recordings (codec cover)

  Hard-negatives (should NOT fire — label 0):
    ~/.config/heyvox/training/tn          confusable ambient that ALMOST fired
    ~/.config/heyvox/training/fp          actual runtime false positives
    training/negatives                    Common Voice ambient speech

Usage:
    python3 tools/collect_personal_features.py \\
        --out-dir /tmp/personal_features \\
        --tarball /tmp/personal_features.tar.gz

Before featurising, this script runs a mandatory Whisper quality gate
(tools/quality_gate.py) over every source dir listed below, quarantining
mismatched clips (DEF-167). Two flags change that default:
    --gate-only    Run the gate, print its summary, exit 0 -- no featurisation.
    --skip-gate    Skip the gate entirely and featurise as-is (explicit escape
                   hatch; prints a WARNING since this bypasses the audit).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from pathlib import Path

import numpy as np

# Reuse the featuriser — no duplication.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from featurise_clips import collect_wav_paths, featurise  # noqa: E402
from quality_gate import run_gate  # noqa: E402


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


POSITIVE_DIRS = [
    _expand("~/.config/heyvox/training/tp"),
    _expand("~/.config/heyvox/training/positives"),
    _expand("~/.config/heyvox/training/fn"),
    Path("training/recordings").resolve(),
    Path("training/recordings_friends").resolve(),
    Path("training/recordings_jabra").resolve(),
]

HARD_NEGATIVE_DIRS = [
    _expand("~/.config/heyvox/training/tn"),
    _expand("~/.config/heyvox/training/fp"),
    Path("training/negatives").resolve(),
]


def gather(dirs: list[Path]) -> list[Path]:
    """Collect .wav files from any subset of dirs that actually exist."""
    all_paths: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            print(f"  skip (missing): {d}", file=sys.stderr)
            continue
        paths = collect_wav_paths(d, globs=None, recursive=True)
        print(f"  {d.name}: {len(paths)} clips  ({d})", file=sys.stderr)
        all_paths.extend(paths)
    return all_paths


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/personal_features"))
    ap.add_argument("--tarball", type=Path, default=Path("/tmp/personal_features.tar.gz"))
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--limit-per-side", type=int, default=0,
                    help="Cap N per side (smoke testing). 0 = no cap.")
    ap.add_argument("--gate-only", action="store_true",
                    help="Run the quality gate, print its summary, exit -- "
                         "no featurisation.")
    ap.add_argument("--gate-apply", action="store_true",
                    help="Let the gate actually MOVE clips (quarantine/recover). "
                         "Default is a DRY RUN that only reports what would move.")
    ap.add_argument("--gate-force", action="store_true",
                    help="With --gate-apply, override the hard quarantine-rate "
                         "brake (use only after reviewing the dry-run output).")
    ap.add_argument("--skip-gate", action="store_true",
                    help="Skip the quality gate entirely and featurise as-is "
                         "(explicit escape hatch; prints a warning).")
    args = ap.parse_args()

    if args.skip_gate and not args.gate_only:
        print(
            "WARNING: --skip-gate set -- featurizing WITHOUT the Whisper "
            "quality audit",
            file=sys.stderr,
        )
    elif args.skip_gate and args.gate_only:
        print(
            "NOTE: both --gate-only and --skip-gate passed -- --gate-only "
            "takes precedence (gate runs, no featurization either way)",
            file=sys.stderr,
        )

    if not args.skip_gate or args.gate_only:
        gate_summary = run_gate(
            POSITIVE_DIRS, HARD_NEGATIVE_DIRS,
            _expand("~/.config/heyvox/training/positives"),
            _expand("~/.config/heyvox/training/quarantine"),
            apply=args.gate_apply, force=args.gate_force,
        )
        mode = "APPLIED" if gate_summary["applied"] else "DRY RUN"
        brake = ", BRAKE TRIPPED" if gate_summary["brake_tripped"] else ""
        print(
            f"=== Quality gate [{mode}]: would-quarantine="
            f"{gate_summary['would_quarantine']}, would-recover="
            f"{gate_summary['would_recover']}, pos-quarantine-rate="
            f"{gate_summary['positives_quarantine_rate']:.1%}{brake}, "
            f"moves-made={gate_summary['moves_made']} ===",
            file=sys.stderr,
        )
        # Spot-check log: list negative->positives recoveries so a rare genuine
        # ambient confusable can be eyeballed. Only meaningful once moves are
        # applied (a dry run writes no manifest).
        if gate_summary.get("applied") and gate_summary.get("manifest_path"):
            try:
                manifest = json.loads(Path(gate_summary["manifest_path"]).read_text())
            except (OSError, json.JSONDecodeError) as e:
                print(f"WARNING: failed to read gate manifest for spot-check log: {e}",
                      file=sys.stderr)
                manifest = []
            for entry in manifest:
                if entry.get("side") == "negative" and entry.get("moved_to") == "positives":
                    print(
                        f"  recovered: {Path(entry['from']).name} <- "
                        f"{entry.get('dur', 0.0)}s, transcript: "
                        f"'{entry.get('text', '')[:60]}'",
                        file=sys.stderr,
                    )

        if args.gate_only:
            return 0

        # In the featurize path: if the user asked to APPLY but the brake stopped
        # the moves, stop -- featurizing now would use data they expected to be
        # cleaned. Dry-run (default) just advises and proceeds on current data.
        if args.gate_apply and gate_summary["brake_tripped"] and not gate_summary["applied"]:
            print(
                "Gate brake stopped the requested --gate-apply. Review the "
                "would-quarantine set, then re-run with --gate-apply --gate-force "
                "to override, or --skip-gate to featurize as-is.",
                file=sys.stderr,
            )
            return 3
        if not args.gate_apply and (gate_summary["would_quarantine"] or gate_summary["would_recover"]):
            print(
                "NOTE: gate ran DRY -- featurizing current data unchanged. Run "
                "`python3 tools/quality_gate.py --apply` first to clean.",
                file=sys.stderr,
            )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print("=== Gathering positives ===", file=sys.stderr)
    pos_paths = gather(POSITIVE_DIRS)
    print(f"  total positives: {len(pos_paths)}", file=sys.stderr)

    print("=== Gathering hard-negatives ===", file=sys.stderr)
    neg_paths = gather(HARD_NEGATIVE_DIRS)
    print(f"  total hard-negatives: {len(neg_paths)}", file=sys.stderr)

    if args.limit_per_side:
        pos_paths = pos_paths[: args.limit_per_side]
        neg_paths = neg_paths[: args.limit_per_side]
        print(f"  limited to {args.limit_per_side} per side", file=sys.stderr)

    if not pos_paths:
        print("ERROR: no positive clips found", file=sys.stderr)
        return 2
    if not neg_paths:
        print("ERROR: no hard-negative clips found", file=sys.stderr)
        return 2

    print("=== Featurising positives ===", file=sys.stderr)
    pos = featurise(pos_paths, batch_size=args.batch_size)
    pos_path = args.out_dir / "personal_positive.npy"
    np.save(pos_path, pos)
    print(f"  wrote {pos.shape} -> {pos_path}", file=sys.stderr)

    print("=== Featurising hard-negatives ===", file=sys.stderr)
    neg = featurise(neg_paths, batch_size=args.batch_size)
    neg_path = args.out_dir / "personal_hard_negative.npy"
    np.save(neg_path, neg)
    print(f"  wrote {neg.shape} -> {neg_path}", file=sys.stderr)

    # Manifest makes the tarball self-describing — retrain script reads this
    # to know what to expect without guessing paths.
    manifest_path = args.out_dir / "MANIFEST.txt"
    manifest_path.write_text(
        f"personal_positive.npy       shape={tuple(pos.shape)}  label=1\n"
        f"personal_hard_negative.npy  shape={tuple(neg.shape)}  label=0\n"
        f"feature_format: openwakeword melspec+embedding (16 frames x 96 dim, float32)\n"
        f"target_phrase: hey vox\n"
    )

    print(f"=== Packing tarball: {args.tarball} ===", file=sys.stderr)
    with tarfile.open(args.tarball, "w:gz") as tf:
        tf.add(pos_path, arcname=pos_path.name)
        tf.add(neg_path, arcname=neg_path.name)
        tf.add(manifest_path, arcname=manifest_path.name)
    size_mb = args.tarball.stat().st_size / (1024 * 1024)
    print(f"  {size_mb:.1f} MB: {args.tarball}", file=sys.stderr)
    print(f"Positives:      {pos.shape}", file=sys.stderr)
    print(f"Hard-negatives: {neg.shape}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

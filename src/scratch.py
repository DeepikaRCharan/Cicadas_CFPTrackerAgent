"""Make a scratch tracker for a local run or a live demo, never touching
the real tracker.json.

    python -m src.scratch            # copy tracker.json -> scratch/tracker.json, first-run snapshot pending again
    python -m src.scratch --empty    # scratch/tracker.json starts empty: a run re-classifies the papers live

The copy keeps every real classification, score and notification, and only
clears `snapshot_sent` on the live profiles, so the next digest rendered
from it opens with the FIRST LOOK snapshot -- the real model output from the
real run, shown again at no API cost. Then point any command at the copy
(scratch/ is gitignored):

    python -m src.notify --tracker scratch/tracker.json --dry-run digest        # print the digest, snapshot first
    python -m src.run --tracker scratch/tracker.json --traces-dir scratch/traces --profile immune-tolerance
    python -m src.watch --tracker scratch/tracker.json --today 2027-03-24 --dry-run

Without --dry-run, notify and watch really send to NOTIFY_TO (and mark the
scratch copy, not tracker.json).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import TRACKER_PATH
from src.profile_schema import load_live_profiles
from src.tracker import TrackerError, load_tracker, save_tracker
from src.tracker_schema import TrackerFile

SCRATCH_TRACKER = "scratch/tracker.json"


def make_scratch(source: str | Path, target: str | Path, live: set[str], empty: bool = False) -> list[str]:
    """Write the scratch tracker; returns the profile ids whose snapshot is
    pending in it."""
    source, target = Path(source), Path(target)
    if target.resolve() == source.resolve() or target.resolve() == Path(TRACKER_PATH).resolve():
        raise TrackerError(f"refusing to write the scratch copy over {target} -- that's the real tracker")
    if empty:
        tracker = TrackerFile()
    else:
        if not source.exists():
            raise TrackerError(f"{source} not found -- nothing to copy (use --empty to start from nothing)")
        tracker = load_tracker(source)
        for profile_id in live & set(tracker.profiles):
            tracker.profiles[profile_id].snapshot_sent = None
    target.parent.mkdir(parents=True, exist_ok=True)
    save_tracker(tracker, target)
    return sorted(live & set(tracker.profiles))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.scratch", description="Make a scratch tracker copy for demos.")
    parser.add_argument("--from", dest="source", default=TRACKER_PATH)
    parser.add_argument("--to", dest="target", default=SCRATCH_TRACKER)
    parser.add_argument("--empty", action="store_true", help="start the scratch tracker empty instead of copying")
    args = parser.parse_args(argv)
    try:
        pending = make_scratch(args.source, args.target, set(load_live_profiles()), args.empty)
    except (TrackerError, ValueError) as exc:
        print(f"[scratch] FAILED: {exc}", file=sys.stderr)
        return 1
    if args.empty:
        print(f"[scratch] wrote an empty tracker to {args.target}; a run against it classifies both papers live")
    else:
        print(f"[scratch] copied {args.source} -> {args.target}; first-run snapshot pending for: {', '.join(pending) or 'none'}")
    print(f"[scratch] next: python -m src.notify --tracker {args.target} --dry-run digest")
    return 0


if __name__ == "__main__":
    sys.exit(main())

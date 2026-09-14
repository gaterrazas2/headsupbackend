import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from backend import Backend


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "always"
    is_sunday = datetime.now(ZoneInfo("America/Denver")).weekday() == 6
    if (mode == "sunday" and not is_sunday) or (mode == "weekday" and is_sunday):
        print(f"NFL snapshot skipped for {mode} schedule")
        raise SystemExit(0)
    result = Backend().nfl.snapshot_announced_rosters()
    print(f"NFL snapshots saved: {len(result['saved'])}; games graded: {len(result['settled'])}; missed snapshots: {len(result['missed'])}; already processed: {len(result['alreadySaved'])}; errors: {len(result['errors'])}")

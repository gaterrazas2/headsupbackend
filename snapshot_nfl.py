from backend import Backend


if __name__ == "__main__":
    result = Backend().nfl.snapshot_announced_rosters()
    print(f"NFL snapshots saved: {len(result['saved'])}; already saved: {len(result['alreadySaved'])}; errors: {len(result['errors'])}")

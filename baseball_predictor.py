import math


class BaseballPredictor:
    def _safe_float(self, value):
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _record_pct(self, record):
        if not record or record == "N/A":
            return None

        try:
            wins_str, losses_str = record.split("-")
            wins = int(wins_str)
            losses = int(losses_str)
            total = wins + losses
            if total == 0:
                return None
            return wins / total
        except (ValueError, TypeError):
            return None

    def _clamp(self, value, low, high):
        return max(low, min(high, value))

    def _sigmoid(self, x):
        return 1 / (1 + math.exp(-x))

    def _extract_inning_number(self, inning_value):
        if inning_value is None:
            return 0

        inning_str = str(inning_value)
        digits = ""

        for char in inning_str:
            if char.isdigit():
                digits += char
            elif digits:
                break

        return int(digits) if digits else 0

    def calculate_win_probability(self, payload):
        stats = payload.get("stats", {}) or {}
        signals = payload.get("signals", {}) or {}

        home_team = stats.get("homeTeam", "Home Team")
        away_team = stats.get("awayTeam", "Away Team")

        home_record_pct = self._record_pct(stats.get("homeRecord"))
        away_record_pct = self._record_pct(stats.get("awayRecord"))

        home_starter = stats.get("probablePitchers", {}).get("home", {}) or {}
        away_starter = stats.get("probablePitchers", {}).get("away", {}) or {}

        home_era = self._safe_float(home_starter.get("era"))
        away_era = self._safe_float(away_starter.get("era"))
        home_whip = self._safe_float(home_starter.get("whip"))
        away_whip = self._safe_float(away_starter.get("whip"))

        live_state = stats.get("liveState", {}) or {}
        current_matchup = stats.get("currentMatchup", {}) or {}

        batter = current_matchup.get("batter", {}) or {}
        pitcher = current_matchup.get("pitcher", {}) or {}

        batter_avg = self._safe_float(batter.get("avg"))
        batter_ops = self._safe_float(batter.get("ops"))
        pitcher_era = self._safe_float(pitcher.get("era"))
        pitcher_whip = self._safe_float(pitcher.get("whip"))

        home_runs = self._safe_float(live_state.get("homeRuns"))
        away_runs = self._safe_float(live_state.get("awayRuns"))
        outs = self._safe_float(live_state.get("outs"))
        inning_number = self._extract_inning_number(live_state.get("inning"))

        runners = live_state.get("runnersOnBase", {}) or {}
        runner_count = sum(
            [
                1 if runners.get("first") else 0,
                1 if runners.get("second") else 0,
                1 if runners.get("third") else 0,
            ]
        )

        batter_side = batter.get("batSide")
        pitcher_hand = pitcher.get("pitchHand")

        raw_score = 0.0

        # Team strength
        if home_record_pct is not None and away_record_pct is not None:
            raw_score += (home_record_pct - away_record_pct) * 2.0

        # Starting pitcher edge
        if home_era is not None and away_era is not None:
            raw_score += ((away_era - home_era) / 2.0) * 0.9

        if home_whip is not None and away_whip is not None:
            raw_score += (away_whip - home_whip) * 0.7

        # Live score edge grows later in the game
        if home_runs is not None and away_runs is not None:
            score_diff = home_runs - away_runs
            raw_score += score_diff * (0.28 + 0.06 * inning_number)

        # Current matchup
        if batter_ops is not None:
            raw_score += (batter_ops - 0.720) * (-1.1)

        if batter_avg is not None:
            raw_score += (batter_avg - 0.250) * (-2.0)

        if pitcher_whip is not None:
            raw_score += (1.25 - pitcher_whip) * 0.75

        if pitcher_era is not None:
            raw_score += (4.00 - pitcher_era) * 0.18

        # Handedness
        if batter_side and pitcher_hand:
            batter_is_left = "Left" in batter_side
            pitcher_is_left = "Left" in pitcher_hand

            if batter_is_left != pitcher_is_left:
                raw_score -= 0.12
            else:
                raw_score += 0.08

        # Live base/out pressure
        if outs is not None and runner_count > 0:
            if outs <= 1:
                raw_score += runner_count * (-0.07)
            else:
                raw_score += runner_count * (-0.03)

        # Frontend signal adjustments
        favorite = signals.get("favorite")
        total_lean = signals.get("totalLean")
        current_at_bat_edge = signals.get("currentAtBatEdge")
        pitcher_lean = signals.get("pitcherLean")

        if favorite == home_team:
            raw_score += 0.18
        elif favorite == away_team:
            raw_score -= 0.18

        if pitcher_lean and home_team in pitcher_lean:
            raw_score += 0.14
        elif pitcher_lean and away_team in pitcher_lean:
            raw_score -= 0.14

        if current_at_bat_edge == "Current pitcher in a strong spot":
            raw_score += 0.10
        elif current_at_bat_edge == "Current batter in a strong spot":
            raw_score -= 0.10

        if total_lean == "Lean under":
            raw_score += 0.02
        elif total_lean == "Lean over":
            raw_score -= 0.02

        home_prob = self._sigmoid(raw_score)
        home_prob = self._clamp(home_prob, 0.01, 0.99)
        away_prob = 1 - home_prob

        home_pct = round(home_prob * 100)
        away_pct = 100 - home_pct

        model_favorite = home_team if home_pct >= away_pct else away_team

        return {
            "homeWinProbability": home_pct,
            "awayWinProbability": away_pct,
            "modelFavorite": model_favorite,
        }
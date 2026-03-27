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

    def _get_handedness_edge(self, batter_side, pitcher_hand):
        if not batter_side or not pitcher_hand:
            return 0.0

        batter_is_left = "Left" in batter_side
        pitcher_is_left = "Left" in pitcher_hand

        if batter_is_left != pitcher_is_left:
            return 0.18

        return -0.08

    def _build_reason(self, reasons):
        filtered = [reason for reason in reasons if reason]
        if not filtered:
            return "Model found a mild edge from the available stats"
        return ", ".join(filtered[:3])

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

        if home_record_pct is not None and away_record_pct is not None:
            raw_score += (home_record_pct - away_record_pct) * 2.0

        if home_era is not None and away_era is not None:
            raw_score += ((away_era - home_era) / 2.0) * 0.9

        if home_whip is not None and away_whip is not None:
            raw_score += (away_whip - home_whip) * 0.7

        if home_runs is not None and away_runs is not None:
            score_diff = home_runs - away_runs
            raw_score += score_diff * (0.28 + 0.06 * inning_number)

        if batter_ops is not None:
            raw_score += (batter_ops - 0.720) * (-1.1)

        if batter_avg is not None:
            raw_score += (batter_avg - 0.250) * (-2.0)

        if pitcher_whip is not None:
            raw_score += (1.25 - pitcher_whip) * 0.75

        if pitcher_era is not None:
            raw_score += (4.00 - pitcher_era) * 0.18

        raw_score -= self._get_handedness_edge(batter_side, pitcher_hand) * 0.5

        if outs is not None and runner_count > 0:
            if outs <= 1:
                raw_score += runner_count * (-0.07)
            else:
                raw_score += runner_count * (-0.03)

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

    def _calculate_batter_hit_prop(self, payload):
        stats = payload.get("stats", {}) or {}
        current_matchup = stats.get("currentMatchup", {}) or {}

        batter = current_matchup.get("batter", {}) or {}
        pitcher = current_matchup.get("pitcher", {}) or {}
        live_state = stats.get("liveState", {}) or {}

        batter_name = batter.get("fullName")
        if not batter_name or batter_name == "N/A":
            return None

        batter_avg = self._safe_float(batter.get("avg"))
        batter_obp = self._safe_float(batter.get("obp"))
        batter_ops = self._safe_float(batter.get("ops"))
        pitcher_era = self._safe_float(pitcher.get("era"))
        pitcher_whip = self._safe_float(pitcher.get("whip"))

        batter_side = batter.get("batSide")
        pitcher_hand = pitcher.get("pitchHand")

        score = 0.0
        reasons = []

        if batter_avg is not None:
            score += (batter_avg - 0.245) * 18
            if batter_avg >= 0.275:
                reasons.append("strong batting average")

        if batter_obp is not None:
            score += (batter_obp - 0.315) * 10
            if batter_obp >= 0.340:
                reasons.append("solid on base profile")

        if batter_ops is not None:
            score += (batter_ops - 0.720) * 3.0
            if batter_ops >= 0.800:
                reasons.append("good overall hitting profile")

        if pitcher_whip is not None:
            score += (pitcher_whip - 1.25) * 1.4
            if pitcher_whip >= 1.30:
                reasons.append("pitcher allows traffic")

        if pitcher_era is not None:
            score += (pitcher_era - 4.00) * 0.18
            if pitcher_era >= 4.30:
                reasons.append("pitcher has weaker run prevention")

        handedness_edge = self._get_handedness_edge(batter_side, pitcher_hand)
        score += handedness_edge
        if handedness_edge > 0:
            reasons.append("favorable handedness matchup")

        balls = self._safe_float(live_state.get("balls"))
        strikes = self._safe_float(live_state.get("strikes"))

        if balls is not None and strikes is not None:
            count_edge = (balls - strikes) * 0.05
            score += count_edge
            if count_edge > 0.04:
                reasons.append("current count favors the hitter")

        probability = self._sigmoid(score)
        probability = round(self._clamp(probability, 0.01, 0.99) * 100)

        return {
            "type": "batter_hit",
            "player": batter_name,
            "recommendation": "To record a hit",
            "probability": probability,
            "reason": self._build_reason(reasons),
        }

    def _calculate_pitcher_strikeout_prop(self, payload):
        stats = payload.get("stats", {}) or {}
        current_matchup = stats.get("currentMatchup", {}) or {}

        pitcher = current_matchup.get("pitcher", {}) or {}
        batter = current_matchup.get("batter", {}) or {}
        live_state = stats.get("liveState", {}) or {}

        pitcher_name = pitcher.get("fullName")
        if not pitcher_name or pitcher_name == "N/A":
            return None

        k9 = self._safe_float(pitcher.get("strikeoutsPer9Inn"))
        whip = self._safe_float(pitcher.get("whip"))
        era = self._safe_float(pitcher.get("era"))
        batter_avg = self._safe_float(batter.get("avg"))
        batter_ops = self._safe_float(batter.get("ops"))
        outs = self._safe_float(live_state.get("outs"))
        strikes = self._safe_float(live_state.get("strikes"))

        score = 0.0
        reasons = []

        if k9 is not None:
            score += (k9 - 8.2) * 0.32
            if k9 >= 9.5:
                reasons.append("strong strikeout rate")

        if whip is not None:
            score += (1.22 - whip) * 0.9
            if whip <= 1.15:
                reasons.append("good command profile")

        if era is not None:
            score += (4.00 - era) * 0.10

        if batter_avg is not None:
            score += (0.245 - batter_avg) * 8
            if batter_avg <= 0.240:
                reasons.append("current hitter has weaker contact profile")

        if batter_ops is not None:
            score += (0.720 - batter_ops) * 1.6
            if batter_ops <= 0.700:
                reasons.append("current hitter has limited power production")

        if strikes is not None:
            score += strikes * 0.08
            if strikes >= 2:
                reasons.append("pitcher is ahead in the count")

        if outs is not None and outs == 2:
            score += 0.04

        over_probability = round(self._clamp(self._sigmoid(score), 0.01, 0.99) * 100)
        recommendation = "Over strikeouts" if over_probability >= 50 else "Under strikeouts"
        probability = over_probability if over_probability >= 50 else 100 - over_probability

        return {
            "type": "pitcher_strikeouts",
            "player": pitcher_name,
            "recommendation": recommendation,
            "probability": probability,
            "reason": self._build_reason(reasons),
        }

    def _calculate_batter_total_bases_prop(self, payload):
        stats = payload.get("stats", {}) or {}
        current_matchup = stats.get("currentMatchup", {}) or {}

        batter = current_matchup.get("batter", {}) or {}
        pitcher = current_matchup.get("pitcher", {}) or {}
        live_state = stats.get("liveState", {}) or {}

        batter_name = batter.get("fullName")
        if not batter_name or batter_name == "N/A":
            return None

        batter_avg = self._safe_float(batter.get("avg"))
        batter_ops = self._safe_float(batter.get("ops"))
        batter_slg = self._safe_float(batter.get("slg"))
        pitcher_era = self._safe_float(pitcher.get("era"))
        pitcher_whip = self._safe_float(pitcher.get("whip"))

        batter_side = batter.get("batSide")
        pitcher_hand = pitcher.get("pitchHand")

        score = 0.0
        reasons = []

        if batter_slg is not None:
            score += (batter_slg - 0.400) * 5.5
            if batter_slg >= 0.470:
                reasons.append("strong slugging profile")

        if batter_ops is not None:
            score += (batter_ops - 0.720) * 2.2
            if batter_ops >= 0.800:
                reasons.append("quality extra base potential")

        if batter_avg is not None:
            score += (batter_avg - 0.245) * 10
            if batter_avg >= 0.270:
                reasons.append("good contact base")

        if pitcher_whip is not None:
            score += (pitcher_whip - 1.25) * 1.3
            if pitcher_whip >= 1.30:
                reasons.append("pitcher allows frequent baserunners")

        if pitcher_era is not None:
            score += (pitcher_era - 4.00) * 0.14
            if pitcher_era >= 4.30:
                reasons.append("pitcher is vulnerable to damage")

        handedness_edge = self._get_handedness_edge(batter_side, pitcher_hand)
        score += handedness_edge * 0.9
        if handedness_edge > 0:
            reasons.append("favorable split for power contact")

        runners = live_state.get("runnersOnBase", {}) or {}
        runner_count = sum(
            [
                1 if runners.get("first") else 0,
                1 if runners.get("second") else 0,
                1 if runners.get("third") else 0,
            ]
        )
        score += runner_count * 0.04

        probability = round(self._clamp(self._sigmoid(score), 0.01, 0.99) * 100)

        return {
            "type": "batter_total_bases",
            "player": batter_name,
            "recommendation": "Over total bases" if probability >= 50 else "Under total bases",
            "probability": probability if probability >= 50 else 100 - probability,
            "reason": self._build_reason(reasons),
        }

    def calculate_prop_predictions(self, payload):
        raw_props = [
            self._calculate_batter_hit_prop(payload),
            self._calculate_pitcher_strikeout_prop(payload),
            self._calculate_batter_total_bases_prop(payload),
        ]

        props = []
        for prop in raw_props:
            if not prop:
                continue

            if prop["type"] == "batter_hit" and prop["probability"] >= 57:
                props.append(prop)
            elif prop["type"] == "pitcher_strikeouts" and prop["probability"] >= 56:
                props.append(prop)
            elif prop["type"] == "batter_total_bases" and prop["probability"] >= 55:
                props.append(prop)

        props.sort(key=lambda item: item["probability"], reverse=True)
        return props
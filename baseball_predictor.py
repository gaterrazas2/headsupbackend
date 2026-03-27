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
        return ", ".join(filtered[:4])

    def _game_started(self, payload):
        status = str(payload.get("status", "") or "").lower()

        return not (
            "scheduled" in status
            or "pre-game" in status
            or "preview" in status
            or "warmup" in status
        )

    def _nearest_half_line(self, value, minimum=0.5):
        if value is None:
            return minimum
        rounded = math.floor(value) + 0.5
        return max(minimum, rounded)

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

        batter_name = batter.get("fullName")
        if not batter_name or batter_name == "N/A":
            return None

        batter_avg = self._safe_float(batter.get("avg"))
        batter_obp = self._safe_float(batter.get("obp"))
        batter_ops = self._safe_float(batter.get("ops"))
        batter_slg = self._safe_float(batter.get("slg"))
        pitcher_era = self._safe_float(pitcher.get("era"))
        pitcher_whip = self._safe_float(pitcher.get("whip"))

        batter_side = batter.get("batSide")
        pitcher_hand = pitcher.get("pitchHand")

        score = 0.0
        reasons = []

        if batter_avg is not None:
            score += (batter_avg - 0.245) * 7.2
            if batter_avg >= 0.280:
                reasons.append("strong batting average")

        if batter_obp is not None:
            score += (batter_obp - 0.315) * 3.6
            if batter_obp >= 0.340:
                reasons.append("good on base profile")

        if batter_ops is not None:
            score += (batter_ops - 0.720) * 1.0
            if batter_ops >= 0.800:
                reasons.append("strong overall hitting profile")

        if batter_slg is not None:
            score += (batter_slg - 0.400) * 0.9
            if batter_slg >= 0.470:
                reasons.append("quality contact authority")

        if pitcher_whip is not None:
            score += (pitcher_whip - 1.25) * 0.75
            if pitcher_whip >= 1.30:
                reasons.append("pitcher allows baserunners")

        if pitcher_era is not None:
            score += (pitcher_era - 4.00) * 0.07
            if pitcher_era >= 4.30:
                reasons.append("pitcher is easier to attack")

        handedness_edge = self._get_handedness_edge(batter_side, pitcher_hand)
        score += handedness_edge * 0.45
        if handedness_edge > 0:
            reasons.append("favorable handedness matchup")

        probability = self._sigmoid(score)
        probability = self._clamp(probability, 0.20, 0.80)
        probability_pct = round(probability * 100)

        baseline_hit_prob = 45
        value_score = round(probability_pct - baseline_hit_prob, 1)

        return {
            "type": "batter_hit",
            "player": batter_name,
            "recommendation": "To record a hit",
            "probability": probability_pct,
            "valueScore": value_score,
            "reason": self._build_reason(reasons),
        }

    def _calculate_pitcher_strikeout_prop(self, payload):
        stats = payload.get("stats", {}) or {}
        current_matchup = stats.get("currentMatchup", {}) or {}
        probable_pitchers = stats.get("probablePitchers", {}) or {}

        pitcher = current_matchup.get("pitcher", {}) or {}
        batter = current_matchup.get("batter", {}) or {}

        pitcher_name = pitcher.get("fullName")
        k9 = self._safe_float(pitcher.get("strikeoutsPer9Inn"))
        whip = self._safe_float(pitcher.get("whip"))
        era = self._safe_float(pitcher.get("era"))
        batter_avg = self._safe_float(batter.get("avg"))
        batter_ops = self._safe_float(batter.get("ops"))

        if not pitcher_name or pitcher_name == "N/A":
            home_pitcher = probable_pitchers.get("home", {}) or {}
            away_pitcher = probable_pitchers.get("away", {}) or {}

            fallback_pitcher = None
            if home_pitcher.get("fullName"):
                fallback_pitcher = home_pitcher
            elif away_pitcher.get("fullName"):
                fallback_pitcher = away_pitcher

            if not fallback_pitcher:
                return None

            pitcher_name = fallback_pitcher.get("fullName")
            k9 = self._safe_float(fallback_pitcher.get("strikeoutsPer9Inn"))
            whip = self._safe_float(fallback_pitcher.get("whip"))
            era = self._safe_float(fallback_pitcher.get("era"))

        if not pitcher_name or pitcher_name == "N/A":
            return None

        reasons = []

        expected_strikeouts = 4.7

        if k9 is not None:
            expected_strikeouts += (k9 - 8.5) * 0.55
            if k9 >= 9.5:
                reasons.append("strong strikeout rate")

        if whip is not None:
            expected_strikeouts += (1.22 - whip) * 0.95
            if whip <= 1.15:
                reasons.append("good command profile")

        if era is not None:
            expected_strikeouts += (4.00 - era) * 0.10

        if batter_avg is not None:
            expected_strikeouts += (0.245 - batter_avg) * 5.5
            if batter_avg <= 0.240:
                reasons.append("current hitter has weaker contact profile")

        if batter_ops is not None:
            expected_strikeouts += (0.720 - batter_ops) * 1.1
            if batter_ops <= 0.700:
                reasons.append("favorable hitting matchup")

        estimated_value = self._clamp(expected_strikeouts, 2.5, 11.5)
        display_line = self._nearest_half_line(estimated_value)
        edge = abs(estimated_value - display_line)

        base_prob = self._sigmoid((estimated_value - display_line) * 2.5)
        probability = self._clamp(max(base_prob, 1 - base_prob), 0.45, 0.80)
        probability_pct = round(probability * 100)

        recommendation = (
            f"Lean over {display_line:.1f} strikeouts"
            if estimated_value >= display_line
            else f"Lean under {display_line:.1f} strikeouts"
        )

        value_score = round((probability_pct - 50) + (edge * 8), 1)

        return {
            "type": "pitcher_strikeouts",
            "player": pitcher_name,
            "recommendation": recommendation,
            "estimatedValue": round(estimated_value, 1),
            "probability": probability_pct,
            "valueScore": value_score,
            "reason": self._build_reason(reasons),
        }

    def _calculate_batter_total_bases_prop(self, payload):
        stats = payload.get("stats", {}) or {}
        current_matchup = stats.get("currentMatchup", {}) or {}

        batter = current_matchup.get("batter", {}) or {}
        pitcher = current_matchup.get("pitcher", {}) or {}

        batter_name = batter.get("fullName")
        if not batter_name or batter_name == "N/A":
            return None

        batter_avg = self._safe_float(batter.get("avg"))
        batter_ops = self._safe_float(batter.get("ops"))
        batter_slg = self._safe_float(batter.get("slg"))
        batter_obp = self._safe_float(batter.get("obp"))
        pitcher_era = self._safe_float(pitcher.get("era"))
        pitcher_whip = self._safe_float(pitcher.get("whip"))

        batter_side = batter.get("batSide")
        pitcher_hand = pitcher.get("pitchHand")

        reasons = []

        expected_total_bases = 1.0

        if batter_avg is not None:
            expected_total_bases += (batter_avg - 0.245) * 3.5
            if batter_avg >= 0.275:
                reasons.append("strong contact profile")

        if batter_obp is not None:
            expected_total_bases += (batter_obp - 0.315) * 1.2

        if batter_ops is not None:
            expected_total_bases += (batter_ops - 0.720) * 1.7
            if batter_ops >= 0.800:
                reasons.append("good overall hitting profile")

        if batter_slg is not None:
            expected_total_bases += (batter_slg - 0.400) * 3.0
            if batter_slg >= 0.470:
                reasons.append("strong slugging profile")

        if pitcher_whip is not None:
            expected_total_bases += (pitcher_whip - 1.25) * 0.8
            if pitcher_whip >= 1.30:
                reasons.append("pitcher allows extra traffic")

        if pitcher_era is not None:
            expected_total_bases += (pitcher_era - 4.00) * 0.10
            if pitcher_era >= 4.30:
                reasons.append("pitcher is vulnerable to damage")

        handedness_edge = self._get_handedness_edge(batter_side, pitcher_hand)
        expected_total_bases += handedness_edge * 0.65
        if handedness_edge > 0:
            reasons.append("favorable split for power contact")

        estimated_value = self._clamp(expected_total_bases, 0.3, 3.5)
        display_line = 1.5
        edge = abs(estimated_value - display_line)

        base_prob = self._sigmoid((estimated_value - display_line) * 2.3)
        probability = self._clamp(max(base_prob, 1 - base_prob), 0.40, 0.80)
        probability_pct = round(probability * 100)

        recommendation = (
            "Lean over 1.5 total bases"
            if estimated_value >= display_line
            else "Lean under 1.5 total bases"
        )

        value_score = round((probability_pct - 50) + (edge * 10), 1)

        return {
            "type": "batter_total_bases",
            "player": batter_name,
            "recommendation": recommendation,
            "estimatedValue": round(estimated_value, 1),
            "probability": probability_pct,
            "valueScore": value_score,
            "reason": self._build_reason(reasons),
        }

    def calculate_player_props(self, payload):
        if self._game_started(payload):
            return []

        raw_props = [
            self._calculate_batter_hit_prop(payload),
            self._calculate_pitcher_strikeout_prop(payload),
            self._calculate_batter_total_bases_prop(payload),
        ]

        props = []
        for prop in raw_props:
            if not prop:
                continue
            if prop["probability"] >= 65:
                props.append(prop)

        props.sort(key=lambda item: item["valueScore"], reverse=True)
        return props

    def calculate_game_props(self, payload):
        if not self._game_started(payload):
            return []

        stats = payload.get("stats", {}) or {}
        probabilities = self.calculate_win_probability(payload)

        home_team = stats.get("homeTeam", "Home Team")
        away_team = stats.get("awayTeam", "Away Team")
        favorite = probabilities.get("modelFavorite")

        home_prob = probabilities.get("homeWinProbability", 50)
        away_prob = probabilities.get("awayWinProbability", 50)

        live_state = stats.get("liveState", {}) or {}
        home_runs = self._safe_float(live_state.get("homeRuns"))
        away_runs = self._safe_float(live_state.get("awayRuns"))
        inning_number = self._extract_inning_number(live_state.get("inning"))

        score_diff = 0
        if home_runs is not None and away_runs is not None:
            score_diff = home_runs - away_runs

        props = []

        if favorite == home_team:
            ml_probability = home_prob
            ml_reason = "model favors the home team based on current game state"
            if score_diff > 0:
                ml_reason = "home team leads and the model still favors them"
            props.append(
                {
                    "type": "moneyline",
                    "player": None,
                    "recommendation": f"{home_team} moneyline",
                    "probability": ml_probability,
                    "valueScore": round(ml_probability - 50, 1),
                    "reason": ml_reason,
                }
            )
        else:
            ml_probability = away_prob
            ml_reason = "model favors the away team based on current game state"
            if score_diff < 0:
                ml_reason = "away team leads and the model still favors them"
            props.append(
                {
                    "type": "moneyline",
                    "player": None,
                    "recommendation": f"{away_team} moneyline",
                    "probability": ml_probability,
                    "valueScore": round(ml_probability - 50, 1),
                    "reason": ml_reason,
                }
            )

        if favorite == home_team:
            estimated_margin = max(
                0.5,
                (home_prob - 50) / 18 + max(score_diff, 0) * 0.45 + inning_number * 0.03,
            )
            spread_recommendation = (
                f"{home_team} -1.5"
                if estimated_margin >= 1.5
                else f"{away_team} +1.5"
            )
            spread_probability = max(55, min(80, home_prob))
        else:
            estimated_margin = max(
                0.5,
                (away_prob - 50) / 18 + max(-score_diff, 0) * 0.45 + inning_number * 0.03,
            )
            spread_recommendation = (
                f"{away_team} -1.5"
                if estimated_margin >= 1.5
                else f"{home_team} +1.5"
            )
            spread_probability = max(55, min(80, away_prob))

        props.append(
            {
                "type": "spread",
                "player": None,
                "recommendation": spread_recommendation,
                "estimatedValue": round(estimated_margin, 1),
                "probability": spread_probability,
                "valueScore": round((spread_probability - 50) + (abs(estimated_margin - 1.5) * 6), 1),
                "reason": "spread lean is based on model edge, score state, and inning context",
            }
        )

        filtered_props = [prop for prop in props if prop["probability"] >= 65]
        filtered_props.sort(key=lambda item: item["valueScore"], reverse=True)
        return filtered_props

    def calculate_props(self, payload):
        if self._game_started(payload):
            return self.calculate_game_props(payload)
        return self.calculate_player_props(payload)
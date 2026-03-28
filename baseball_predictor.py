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
            return 0.12

        return -0.05

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

    def _team_offense_score(self, offense):
        if not offense:
            return 0.0

        avg = self._safe_float(offense.get("avg"))
        obp = self._safe_float(offense.get("obp"))
        ops = self._safe_float(offense.get("ops"))
        slg = self._safe_float(offense.get("slg"))

        score = 0.0
        if avg is not None:
            score += (avg - 0.245) * 10.0
        if obp is not None:
            score += (obp - 0.315) * 6.0
        if ops is not None:
            score += (ops - 0.720) * 2.8
        if slg is not None:
            score += (slg - 0.400) * 3.5

        return score

    def _get_pitcher_side(self, stats, pitcher_name):
        probable_home = stats.get("probablePitchers", {}).get("home", {}) or {}
        probable_away = stats.get("probablePitchers", {}).get("away", {}) or {}

        if pitcher_name and probable_home.get("fullName") == pitcher_name:
            return "home"
        if pitcher_name and probable_away.get("fullName") == pitcher_name:
            return "away"

        current_pitcher = stats.get("currentMatchup", {}).get("pitcher", {}) or {}
        current_pitcher_name = current_pitcher.get("fullName")
        if current_pitcher_name and current_pitcher_name == pitcher_name:
            # If current pitcher matches but probable did not, infer from live state not available.
            # Return None and let caller use a neutral fallback.
            return None

        return None

    def _get_opposing_team_offense_for_pitcher(self, stats, pitcher_name):
        team_offense = stats.get("teamOffense", {}) or {}
        pitcher_side = self._get_pitcher_side(stats, pitcher_name)

        if pitcher_side == "home":
            return team_offense.get("away", {}) or {}
        if pitcher_side == "away":
            return team_offense.get("home", {}) or {}

        home_offense = team_offense.get("home", {}) or {}
        away_offense = team_offense.get("away", {}) or {}

        home_score = self._team_offense_score(home_offense)
        away_score = self._team_offense_score(away_offense)

        return away_offense if away_score >= home_score else home_offense

    def _get_batting_team_offense_for_current_matchup(self, stats, pitcher_name):
        team_offense = stats.get("teamOffense", {}) or {}
        pitcher_side = self._get_pitcher_side(stats, pitcher_name)

        if pitcher_side == "home":
            return team_offense.get("away", {}) or {}
        if pitcher_side == "away":
            return team_offense.get("home", {}) or {}

        home_offense = team_offense.get("home", {}) or {}
        away_offense = team_offense.get("away", {}) or {}

        home_score = self._team_offense_score(home_offense)
        away_score = self._team_offense_score(away_offense)

        return away_offense if away_score >= home_score else home_offense

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

        home_offense = stats.get("teamOffense", {}).get("home", {}) or {}
        away_offense = stats.get("teamOffense", {}).get("away", {}) or {}

        home_offense_score = self._team_offense_score(home_offense)
        away_offense_score = self._team_offense_score(away_offense)

        live_state = stats.get("liveState", {}) or {}
        current_matchup = stats.get("currentMatchup", {}) or {}
        pitcher = current_matchup.get("pitcher", {}) or {}

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

        raw_score = 0.0

        if home_record_pct is not None and away_record_pct is not None:
            raw_score += (home_record_pct - away_record_pct) * 1.8

        raw_score += (home_offense_score - away_offense_score) * 0.45

        if home_era is not None and away_era is not None:
            raw_score += ((away_era - home_era) / 2.0) * 0.8

        if home_whip is not None and away_whip is not None:
            raw_score += (away_whip - home_whip) * 0.6

        # Current game score only, lightly weighted early and capped
        if home_runs is not None and away_runs is not None:
            score_diff = home_runs - away_runs
            capped_diff = self._clamp(score_diff, -4, 4)
            inning_weight = min(0.10 + (inning_number * 0.05), 0.45)
            raw_score += capped_diff * inning_weight

        # Tiny live current-pitcher context only
        if pitcher_whip is not None:
            raw_score += (1.25 - pitcher_whip) * 0.12

        if pitcher_era is not None:
            raw_score += (4.00 - pitcher_era) * 0.04

        # Light live pressure adjustment only
        if outs is not None and runner_count > 0:
            if outs <= 1:
                raw_score += runner_count * (-0.03)
            else:
                raw_score += runner_count * (-0.015)

        favorite = signals.get("favorite")
        pitcher_lean = signals.get("pitcherLean")

        if favorite == home_team:
            raw_score += 0.12
        elif favorite == away_team:
            raw_score -= 0.12

        if pitcher_lean and home_team in pitcher_lean:
            raw_score += 0.10
        elif pitcher_lean and away_team in pitcher_lean:
            raw_score -= 0.10

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
        pitcher_name = pitcher.get("fullName")

        batting_team_offense = self._get_batting_team_offense_for_current_matchup(
            stats, pitcher_name
        )

        team_avg = self._safe_float((batting_team_offense or {}).get("avg"))
        team_ops = self._safe_float((batting_team_offense or {}).get("ops"))
        team_obp = self._safe_float((batting_team_offense or {}).get("obp"))
        team_slg = self._safe_float((batting_team_offense or {}).get("slg"))

        score = 0.0
        reasons = []

        if batter_avg is not None:
            score += (batter_avg - 0.245) * 5.0
            if batter_avg >= 0.280:
                reasons.append("strong batting average")

        if batter_obp is not None:
            score += (batter_obp - 0.315) * 2.0

        if batter_ops is not None:
            score += (batter_ops - 0.720) * 0.8
            if batter_ops >= 0.800:
                reasons.append("strong overall hitting profile")

        if batter_slg is not None:
            score += (batter_slg - 0.400) * 0.7

        if team_avg is not None:
            score += (team_avg - 0.245) * 3.0
            if team_avg >= 0.255:
                reasons.append("supportive team batting average")

        if team_ops is not None:
            score += (team_ops - 0.720) * 0.9

        if team_obp is not None:
            score += (team_obp - 0.315) * 1.4

        if team_slg is not None:
            score += (team_slg - 0.400) * 1.2
            if team_slg >= 0.420:
                reasons.append("solid lineup power context")

        if pitcher_whip is not None:
            score += (pitcher_whip - 1.25) * 0.6
            if pitcher_whip >= 1.30:
                reasons.append("pitcher allows baserunners")

        if pitcher_era is not None:
            score += (pitcher_era - 4.00) * 0.06

        handedness_edge = self._get_handedness_edge(batter_side, pitcher_hand)
        score += handedness_edge * 0.35
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
        pitcher_name = pitcher.get("fullName")
        k9 = self._safe_float(pitcher.get("strikeoutsPer9Inn"))
        whip = self._safe_float(pitcher.get("whip"))
        era = self._safe_float(pitcher.get("era"))

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

        # Correct logic: use the lineup THIS pitcher is facing
        opposing_offense = self._get_opposing_team_offense_for_pitcher(stats, pitcher_name)

        team_avg = self._safe_float((opposing_offense or {}).get("avg"))
        team_ops = self._safe_float((opposing_offense or {}).get("ops"))
        team_obp = self._safe_float((opposing_offense or {}).get("obp"))
        team_strikeouts = self._safe_float((opposing_offense or {}).get("strikeOuts"))

        reasons = []
        expected_strikeouts = 4.7

        if k9 is not None:
            expected_strikeouts += (k9 - 8.5) * 0.55
            if k9 >= 9.5:
                reasons.append("strong strikeout rate")

        if whip is not None:
            expected_strikeouts += (1.22 - whip) * 0.9
            if whip <= 1.15:
                reasons.append("good command profile")

        if era is not None:
            expected_strikeouts += (4.00 - era) * 0.10

        # Opposing lineup contact quality
        if team_avg is not None:
            expected_strikeouts += (0.245 - team_avg) * 5.0
            if team_avg <= 0.240:
                reasons.append("opposing lineup has weaker contact profile")

        if team_ops is not None:
            expected_strikeouts += (0.720 - team_ops) * 1.2
            if team_ops <= 0.700:
                reasons.append("opposing lineup is less dangerous")

        if team_obp is not None:
            expected_strikeouts += (0.315 - team_obp) * 2.0

        # Most important lineup-specific K tendency input
        if team_strikeouts is not None:
            expected_strikeouts += (team_strikeouts - 1200.0) / 220.0
            if team_strikeouts >= 1300:
                reasons.append("opposing lineup has high strikeout tendency")
            elif team_strikeouts <= 1100:
                reasons.append("opposing lineup does not strike out much")

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
        pitcher_name = pitcher.get("fullName")

        batting_team_offense = self._get_batting_team_offense_for_current_matchup(
            stats, pitcher_name
        )

        team_avg = self._safe_float((batting_team_offense or {}).get("avg"))
        team_ops = self._safe_float((batting_team_offense or {}).get("ops"))
        team_obp = self._safe_float((batting_team_offense or {}).get("obp"))
        team_slg = self._safe_float((batting_team_offense or {}).get("slg"))

        reasons = []
        expected_total_bases = 1.0

        if batter_avg is not None:
            expected_total_bases += (batter_avg - 0.245) * 2.8
            if batter_avg >= 0.275:
                reasons.append("strong contact profile")

        if batter_obp is not None:
            expected_total_bases += (batter_obp - 0.315) * 1.0

        if batter_ops is not None:
            expected_total_bases += (batter_ops - 0.720) * 1.4
            if batter_ops >= 0.800:
                reasons.append("good overall hitting profile")

        if batter_slg is not None:
            expected_total_bases += (batter_slg - 0.400) * 2.4
            if batter_slg >= 0.470:
                reasons.append("strong slugging profile")

        if team_avg is not None:
            expected_total_bases += (team_avg - 0.245) * 1.6

        if team_ops is not None:
            expected_total_bases += (team_ops - 0.720) * 0.9

        if team_obp is not None:
            expected_total_bases += (team_obp - 0.315) * 1.1

        if team_slg is not None:
            expected_total_bases += (team_slg - 0.400) * 1.7
            if team_slg >= 0.420:
                reasons.append("solid lineup power context")

        if pitcher_whip is not None:
            expected_total_bases += (pitcher_whip - 1.25) * 0.7
            if pitcher_whip >= 1.30:
                reasons.append("pitcher allows extra traffic")

        if pitcher_era is not None:
            expected_total_bases += (pitcher_era - 4.00) * 0.08

        handedness_edge = self._get_handedness_edge(batter_side, pitcher_hand)
        expected_total_bases += handedness_edge * 0.5
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
            ml_reason = "model favors the home team based on score, record, team offense, and pitching context"
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
            ml_reason = "model favors the away team based on score, record, team offense, and pitching context"
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

        score_component = 0.0
        if home_runs is not None and away_runs is not None:
            capped_diff = self._clamp(abs(score_diff), 0, 4)
            score_component = capped_diff * min(0.08 + (inning_number * 0.03), 0.25)

        if favorite == home_team:
            estimated_margin = max(0.5, (home_prob - 50) / 18 + score_component)
            spread_recommendation = (
                f"{home_team} -1.5"
                if estimated_margin >= 1.5
                else f"{away_team} +1.5"
            )
            spread_probability = max(55, min(80, home_prob))
        else:
            estimated_margin = max(0.5, (away_prob - 50) / 18 + score_component)
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
                "reason": "spread lean is based on model edge, current score state, and inning context",
            }
        )

        filtered_props = [prop for prop in props if prop["probability"] >= 65]
        filtered_props.sort(key=lambda item: item["valueScore"], reverse=True)
        return filtered_props

    def calculate_props(self, payload):
        if self._game_started(payload):
            return self.calculate_game_props(payload)
        return self.calculate_player_props(payload)
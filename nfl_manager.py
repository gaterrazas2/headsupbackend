import csv
import html
import json
import math
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class NFLManager:
    ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
    ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl"
    TEAM_STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_2025.csv"
    ABBR_TO_DATA = {"LAR": "LA", "WSH": "WAS"}
    _metrics_cache = None
    _metrics_cached_at = 0
    _power_rankings_cache = None
    _power_rankings_cached_at = 0

    def __init__(self, predictions_collection=None, archives_collection=None):
        self.predictions_collection = predictions_collection
        self.archives_collection = archives_collection

    def _archive_completed_week(self, events, season, season_type, week):
        if self.archives_collection is None or not events or not week:
            return
        games = []
        for event in events:
            competition = (event.get("competitions") or [{}])[0]
            competitors = {item.get("homeAway"): item for item in competition.get("competitors", [])}
            home, away = competitors.get("home", {}), competitors.get("away", {})
            home_team, away_team = home.get("team", {}), away.get("team", {})
            saved = self._saved_pregame_prediction(event.get("id")) or {}
            forecast = saved.get("prediction", {})
            venue = competition.get("venue", {})
            address = venue.get("address", {})
            games.append({
                "id": str(event.get("id")), "date": event.get("date"), "status": event.get("status", {}).get("type", {}).get("detail", "Final"), "gameState": "post",
                "homeScore": self._number(home.get("score")), "awayScore": self._number(away.get("score")),
                "home": self._team_summary(home_team), "away": self._team_summary(away_team),
                "projectedWinner": forecast.get("winner"), "homeWinProbability": forecast.get("homeWinProbability"), "awayWinProbability": forecast.get("awayWinProbability"),
                "spread": "Final", "venue": venue.get("fullName", ""), "location": ", ".join(filter(None, [address.get("city"), address.get("state")])),
            })
        self.archives_collection.update_one(
            {"season": season, "seasonType": season_type, "week": week},
            {"$set": {"season": season, "seasonType": season_type, "week": week, "games": games, "archivedAt": int(time.time())}},
            upsert=True,
        )

    def history(self, season, season_type=None, current_week=None):
        if self.archives_collection is None or not season:
            return []
        query = {"season": season}
        if season_type:
            query["seasonType"] = season_type
        if current_week:
            query["week"] = {"$lt": current_week}
        return list(self.archives_collection.find(query, {"_id": 0}).sort("week", -1))

    def _save_pregame_prediction(self, event_id, prediction, teams, player_projections=None):
        if self.predictions_collection is None or not event_id or prediction.get("source") != "pregame":
            return
        self.predictions_collection.update_one(
            {"gameId": str(event_id), "sport": "nfl", "phase": "pregame"},
            {"$setOnInsert": {
                "gameId": str(event_id), "sport": "nfl", "phase": "pregame",
                "awayTeam": teams["away"].get("name"), "homeTeam": teams["home"].get("name"),
                "prediction": prediction, "playerProjections": player_projections or [], "createdAt": int(time.time()),
            }},
            upsert=True,
        )
        if player_projections:
            self.predictions_collection.update_one(
                {"gameId": str(event_id), "sport": "nfl", "phase": "pregame", "$or": [{"playerProjections": {"$exists": False}}, {"playerProjections": []}]},
                {"$set": {"playerProjections": player_projections}},
            )

    def _saved_pregame_prediction(self, event_id):
        if self.predictions_collection is None:
            return None
        row = self.predictions_collection.find_one(
            {"gameId": str(event_id), "sport": "nfl", "phase": "pregame"},
            {"_id": 0, "prediction": 1, "playerProjections": 1},
        )
        return row if row else None

    def accuracy(self, season=None):
        if self.predictions_collection is None:
            return {"score": None, "wins": 0, "losses": 0, "games": {"wins": 0, "losses": 0}, "props": {"wins": 0, "losses": 0}}
        season = season or time.gmtime().tm_year
        rows = self.predictions_collection.find({"sport": "nfl", "season": season, "settled": True}, {"_id": 0, "grading": 1})
        game_wins = game_losses = prop_wins = prop_losses = 0
        for row in rows:
            grading = row.get("grading", {})
            if grading.get("gameCorrect") is True:
                game_wins += 1
            elif grading.get("gameCorrect") is False:
                game_losses += 1
            for prop in grading.get("props", []):
                if prop.get("win"):
                    prop_wins += 1
                else:
                    prop_losses += 1
        wins, losses = game_wins + prop_wins, game_losses + prop_losses
        total = wins + losses
        return {
            "score": round(wins / total * 100, 1) if total else None,
            "wins": wins, "losses": losses,
            "games": {"wins": game_wins, "losses": game_losses},
            "props": {"wins": prop_wins, "losses": prop_losses},
        }

    def _settle_prediction(self, event_id, summary, prediction, teams, player_comparisons):
        if self.predictions_collection is None:
            return
        saved = self._saved_pregame_prediction(event_id)
        if not saved or saved.get("settled"):
            return
        actual_winner = "Tie" if prediction.get("homeScore") == prediction.get("awayScore") else (
            teams["home"].get("name") if prediction.get("homeScore", 0) > prediction.get("awayScore", 0) else teams["away"].get("name")
        )
        forecast = saved.get("prediction", {})
        props = []
        relevant = {"QB": ["passingYards", "completions"], "RB": ["rushingYards", "receivingYards"], "WR": ["receptions", "receivingYards"]}
        for player in player_comparisons:
            for stat in relevant.get(player.get("position"), []):
                projected = player.get("projected", {}).get(stat)
                actual = player.get("actual", {}).get(stat)
                if projected is None or actual in (None, "—", ""):
                    continue
                actual_number = self._number(actual)
                props.append({
                    "playerId": player.get("id"), "player": player.get("name"), "position": player.get("position"),
                    "stat": stat, "projected": projected, "actual": actual_number, "win": actual_number >= self._number(projected),
                })
        season = (summary.get("header", {}).get("season") or {}).get("year") or time.gmtime().tm_year
        self.predictions_collection.update_one(
            {"gameId": str(event_id), "sport": "nfl", "phase": "pregame"},
            {"$set": {"season": season, "settled": True, "settledAt": int(time.time()), "grading": {
                "gameCorrect": None if actual_winner == "Tie" else forecast.get("winner") == actual_winner,
                "projectedWinner": forecast.get("winner"), "actualWinner": actual_winner, "props": props,
            }}},
        )

    def _skill_projection(self, player, opponent_abbreviation):
        position = (player.get("position") or "").upper()
        if position not in {"QB", "RB", "WR"}:
            return None
        try:
            data = self._json(f"{self.ESPN_WEB}/athletes/{player['id']}/stats?region=us&lang=en&contentorigin=espn")
        except Exception as error:
            print(f"Could not project {player.get('name')}: {error}")
            return None
        opponent = self._metric_for(opponent_abbreviation)
        projected = {}
        wanted = {"QB": {"passing"}, "RB": {"rushing", "receiving"}, "WR": {"receiving"}}[position]
        for category in data.get("categories", []):
            category_name = (category.get("name") or category.get("displayName") or "").lower()
            if category_name not in wanted:
                continue
            row = max(category.get("statistics", []), key=lambda item: item.get("season", {}).get("year", 0), default={})
            stats = dict(zip(category.get("names", []), row.get("stats", [])))
            games = self._number(stats.get("gamesPlayed")) or 1
            if position == "QB" and category_name == "passing":
                projected["passingYards"] = round(self._number(stats.get("passingYards")) / games * opponent.get("defensePassYpg", 220) / 220, 1)
                projected["completions"] = round(self._number(stats.get("completions") or stats.get("passingCompletions")) / games * opponent.get("defensePassYpg", 220) / 220, 1)
            elif category_name == "rushing":
                projected["rushingYards"] = round(self._number(stats.get("rushingYards")) / games * opponent.get("defenseRushYpg", 110) / 110, 1)
            elif category_name == "receiving":
                projected["receivingYards"] = round(self._number(stats.get("receivingYards")) / games * opponent.get("defensePassYpg", 220) / 220, 1)
                if position == "WR":
                    projected["receptions"] = round(self._number(stats.get("receptions")) / games * opponent.get("defensePassYpg", 220) / 220, 1)
        return {"id": str(player.get("id")), "name": player.get("name"), "team": player.get("team"), "position": position, "projected": projected}

    def _actual_skill_stats(self, summary, positions, injuries):
        comparisons = []
        for team_group in (summary.get("boxscore", {}).get("players") or []):
            team_abbreviation = team_group.get("team", {}).get("abbreviation")
            player_rows = {}
            for category in team_group.get("statistics", []):
                keys = category.get("keys") or category.get("names") or []
                for athlete_row in category.get("athletes", []):
                    athlete = athlete_row.get("athlete", {})
                    athlete_id = str(athlete.get("id"))
                    values = dict(zip(keys, athlete_row.get("stats", [])))
                    player_rows.setdefault(athlete_id, {"id": athlete_id, "name": athlete.get("displayName"), "team": team_abbreviation, "position": athlete.get("position", {}).get("abbreviation", ""), "actual": {}})["actual"].update(values)
            for athlete_id, row in player_rows.items():
                position = positions.get(athlete_id) or row.get("position", "")
                if position not in {"QB", "RB", "WR"}:
                    continue
                aliases = {
                    "passingYards": ["passingYards"], "completions": ["completions", "passingCompletions"],
                    "rushingYards": ["rushingYards"], "receivingYards": ["receivingYards"],
                    "receptions": ["receptions", "receivingReceptions"],
                }
                actual = {key: next((row["actual"][name] for name in names if name in row["actual"]), "—") for key, names in aliases.items()}
                row.update({"position": position, "actual": actual, "didNotFinish": injuries.get(athlete_id, {}).get("didNotFinish", False)})
                comparisons.append(row)
        return comparisons

    @staticmethod
    def _probability(value):
        return round(max(1.0, min(99.0, value * 100)), 1)

    @staticmethod
    def _spread_margin(details, home_abbreviation, away_abbreviation):
        match = re.search(r"([A-Z]{2,3})\s*([+-]?\d+(?:\.\d+)?)", details or "")
        if not match:
            return 0.0
        favorite, line = match.group(1), abs(float(match.group(2)))
        return line if favorite == home_abbreviation else -line if favorite == away_abbreviation else 0.0

    def _pregame_prediction(self, summary, teams, unavailable_by_team=None):
        unavailable_by_team = unavailable_by_team or {}
        competition = (summary.get("header", {}).get("competitions") or [{}])[0]
        pick = (summary.get("pickcenter") or summary.get("odds") or [{}])[0]
        home, away = teams["home"], teams["away"]
        home_metrics, away_metrics = home.get("statistics", {}), away.get("statistics", {})
        spread = pick.get("details") or ""
        market_margin = self._spread_margin(spread, home.get("abbreviation"), away.get("abbreviation"))
        market_probability = 1 / (1 + math.exp(-market_margin / 6.5)) if spread else 0.5
        predictor = summary.get("predictor") or {}
        espn_probability = self._number((predictor.get("homeTeam") or {}).get("gameProjection")) / 100 or 0.5
        epa_edge = home_metrics.get("adjustedEpaPerPlay", 0) - away_metrics.get("adjustedEpaPerPlay", 0)
        power_edge = ((away.get("powerRank") or 16.5) - (home.get("powerRank") or 16.5)) / 31
        injury_edge = (len(unavailable_by_team.get(away.get("abbreviation"), set())) - len(unavailable_by_team.get(home.get("abbreviation"), set()))) * 0.018
        model_logit = epa_edge * 5.5 + power_edge * 0.7 + injury_edge + 0.18
        model_probability = 1 / (1 + math.exp(-model_logit))
        home_probability = market_probability * 0.55 + espn_probability * 0.30 + model_probability * 0.15
        home_probability = max(0.01, min(0.99, home_probability))
        away_probability = 1 - home_probability
        projected_home_margin = round(math.log(home_probability / away_probability) * 6.5, 1)
        confidence = "High" if abs(home_probability - .5) >= .22 else "Medium" if abs(home_probability - .5) >= .10 else "Low"
        winner = home if home_probability >= .5 else away
        factors = [
            {"name": "Market spread", "value": spread or "Unavailable", "weight": "55%"},
            {"name": "ESPN matchup model", "value": f"{self._probability(espn_probability)}% {home['abbreviation']}", "weight": "30%"},
            {"name": "Efficiency model", "value": f"{self._probability(model_probability)}% {home['abbreviation']}", "weight": "15%"},
            {"name": "Injury adjustment", "value": f"{home['abbreviation']} {len(unavailable_by_team.get(home.get('abbreviation'), set()))} out · {away['abbreviation']} {len(unavailable_by_team.get(away.get('abbreviation'), set()))} out"},
        ]
        return {
            "source": "pregame",
            "updatedAt": int(time.time()),
            "winner": winner.get("name"),
            "homeWinProbability": self._probability(home_probability),
            "awayWinProbability": self._probability(away_probability),
            "projectedHomeMargin": projected_home_margin,
            "confidence": confidence,
            "factors": factors,
            "modelVersion": "NFL ensemble v1",
        }

    def game_probability(self, event_id):
        summary = self._json(f"{self.ESPN_SITE}/summary?event={event_id}")
        competition = (summary.get("header", {}).get("competitions") or [{}])[0]
        competitors = {item.get("homeAway"): item for item in competition.get("competitors", [])}
        state = competition.get("status", {}).get("type", {}).get("state", "pre")
        home_score = self._number(competitors.get("home", {}).get("score"))
        away_score = self._number(competitors.get("away", {}).get("score"))
        win_probability = summary.get("winprobability") or []
        if state == "in" and win_probability:
            latest = win_probability[-1]
            home_probability = self._number(latest.get("homeWinPercentage"))
            if home_probability > 1:
                home_probability /= 100
            probability = {
                "source": "live",
                "updatedAt": int(time.time()),
                "homeWinProbability": self._probability(home_probability),
                "awayWinProbability": self._probability(1 - home_probability),
                "confidence": "Live",
            }
        elif state == "post":
            home_won = home_score > away_score
            tied = home_score == away_score
            probability = {
                "source": "final",
                "updatedAt": int(time.time()),
                "homeWinProbability": 50.0 if tied else 100.0 if home_won else 0.0,
                "awayWinProbability": 50.0 if tied else 0.0 if home_won else 100.0,
                "confidence": "Final",
            }
        else:
            teams = {side: self._team_summary(competitors.get(side, {}).get("team", {})) for side in ("away", "home")}
            for team in teams.values():
                team["statistics"] = self._metric_for(team.get("abbreviation"))
                team["powerRank"] = None
            probability = self._pregame_prediction(summary, teams)
            probability["source"] = "pregame"
        probability["gameState"] = state
        probability["status"] = competition.get("status", {}).get("type", {}).get("detail")
        probability["homeScore"] = home_score
        probability["awayScore"] = away_score
        probability["winner"] = "Tie" if state == "post" and home_score == away_score else (
            competitors.get("home", {}).get("team", {}).get("displayName")
            if probability["homeWinProbability"] >= probability["awayWinProbability"]
            else competitors.get("away", {}).get("team", {}).get("displayName")
        )
        return probability

    def _json(self, url):
        request = Request(url, headers={"User-Agent": "LittleBrotherNFL/1.0", "Accept": "application/json"})
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _text(self, url):
        request = Request(url, headers={"User-Agent": "LittleBrotherNFL/1.0"})
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")

    def _team_metrics(self):
        if self._metrics_cache and time.time() - self._metrics_cached_at < 21600:
            return self._metrics_cache

        rows = [row for row in csv.DictReader(StringIO(self._text(self.TEAM_STATS_URL))) if row.get("season_type") == "REG"]
        offense = defaultdict(lambda: {"games": 0, "pass": 0.0, "rush": 0.0, "epa": 0.0, "plays": 0.0, "turnovers": 0.0, "special": 0.0})
        defense = defaultdict(lambda: {"games": 0, "pass": 0.0, "rush": 0.0, "epa": 0.0, "plays": 0.0, "takeaways": 0.0})
        for row in rows:
            team = row.get("team")
            opponent = row.get("opponent_team")
            passing = float(row.get("passing_yards") or 0)
            rushing = float(row.get("rushing_yards") or 0)
            plays = float(row.get("attempts") or 0) + float(row.get("sacks_suffered") or 0) + float(row.get("carries") or 0)
            epa = float(row.get("passing_epa") or 0) + float(row.get("rushing_epa") or 0)
            turnovers = float(row.get("passing_interceptions") or 0) + float(row.get("fumbles_lost_total") or 0)
            special = float(row.get("special_teams_tds") or 0) * 6 + float(row.get("fg_pct") or 0) / 100 + float(row.get("pt_net_yards") or 0) / 1000
            offense[team]["games"] += 1
            offense[team]["pass"] += passing
            offense[team]["rush"] += rushing
            offense[team]["epa"] += epa
            offense[team]["plays"] += plays
            offense[team]["turnovers"] += turnovers
            offense[team]["special"] += special
            defense[opponent]["games"] += 1
            defense[opponent]["pass"] += passing
            defense[opponent]["rush"] += rushing
            defense[opponent]["epa"] += epa
            defense[opponent]["plays"] += plays
            defense[opponent]["takeaways"] += turnovers

        metrics = {}
        for team in offense:
            games = max(offense[team]["games"], 1)
            def_games = max(defense[team]["games"], 1)
            metrics[team] = {
                "offensePassYpg": round(offense[team]["pass"] / games, 1),
                "offenseRushYpg": round(offense[team]["rush"] / games, 1),
                "offenseTotalYpg": round((offense[team]["pass"] + offense[team]["rush"]) / games, 1),
                "defensePassYpg": round(defense[team]["pass"] / def_games, 1),
                "defenseRushYpg": round(defense[team]["rush"] / def_games, 1),
                "defenseTotalYpg": round((defense[team]["pass"] + defense[team]["rush"]) / def_games, 1),
                "offenseEpaPerPlay": round(offense[team]["epa"] / max(offense[team]["plays"], 1), 3),
                "defenseEpaPerPlay": round(defense[team]["epa"] / max(defense[team]["plays"], 1), 3),
                "turnoverMarginPerGame": round((defense[team]["takeaways"] - offense[team]["turnovers"]) / games, 2),
                "specialTeamsScore": round(offense[team]["special"] / games, 2),
            }
            metrics[team]["netEpaPerPlay"] = round(metrics[team]["offenseEpaPerPlay"] - metrics[team]["defenseEpaPerPlay"], 3)

        for team in metrics:
            opponents = [row.get("opponent_team") for row in rows if row.get("team") == team]
            opponent_strength = sum(metrics.get(opponent, {}).get("netEpaPerPlay", 0) for opponent in opponents) / max(len(opponents), 1)
            metrics[team]["adjustedEpaPerPlay"] = round(metrics[team]["netEpaPerPlay"] + opponent_strength * 0.35, 3)

        rank_fields = {
            "offensePassRank": ("offensePassYpg", True),
            "offenseRushRank": ("offenseRushYpg", True),
            "offenseRank": ("offenseTotalYpg", True),
            "defensePassRank": ("defensePassYpg", False),
            "defenseRushRank": ("defenseRushYpg", False),
            "defenseRank": ("defenseTotalYpg", False),
            "epaRank": ("adjustedEpaPerPlay", True),
        }
        for rank_name, (field, descending) in rank_fields.items():
            ordered = sorted(metrics, key=lambda team: metrics[team][field], reverse=descending)
            for rank, team in enumerate(ordered, 1):
                metrics[team][rank_name] = rank

        self._metrics_cache = metrics
        self._metrics_cached_at = time.time()
        return metrics

    def _metric_for(self, abbreviation):
        key = self.ABBR_TO_DATA.get(abbreviation, abbreviation)
        return self._team_metrics().get(key, {})

    def _espn_power_rankings(self):
        if self._power_rankings_cache and time.time() - self._power_rankings_cached_at < 21600:
            return self._power_rankings_cache
        try:
            search = self._json(
                "https://site.web.api.espn.com/apis/search/v2?query=nfl%20power%20rankings&limit=20"
            )
            articles = next(
                (result.get("contents", []) for result in search.get("results", []) if result.get("type") == "article"),
                [],
            )
            candidates = [
                article for article in articles
                if "nfl power rankings" in (article.get("displayName") or "").lower()
                and "future power rankings" not in (article.get("displayName") or "").lower()
            ]
            latest = max(candidates, key=lambda article: article.get("date") or "")
            article = self._json(f"https://content.core.api.espn.com/v1/sports/news/{latest['id']}")["headlines"][0]
            rankings = {}
            pattern = r'<h2[^>]*>\s*(\d+)\.\s*<a[^>]*?/name/([^/\"]+)[^>]*>(.*?)</a>\s*</h2>'
            for rank, abbreviation, team_name in re.findall(pattern, article.get("story", ""), re.I | re.S):
                rankings[abbreviation.upper()] = {
                    "rank": int(rank),
                    "team": html.unescape(re.sub(r"<[^>]+>", "", team_name)).strip(),
                }
            if len(rankings) != 32:
                raise ValueError(f"Expected 32 ESPN power rankings, found {len(rankings)}")
            self._power_rankings_cache = {
                "rankings": rankings,
                "title": article.get("headline"),
                "published": article.get("published"),
                "link": latest.get("link", {}).get("web"),
            }
            self._power_rankings_cached_at = time.time()
        except Exception as error:
            print(f"ESPN power rankings error: {error}")
            if not self._power_rankings_cache:
                self._power_rankings_cache = {"rankings": {}, "title": None, "published": None, "link": None}
        return self._power_rankings_cache

    def weekly_matchups(self):
        scoreboard = self._json(f"{self.ESPN_SITE}/scoreboard?limit=50")
        season = scoreboard.get("season", {}).get("year")
        season_type = scoreboard.get("season", {}).get("type")
        week = scoreboard.get("week", {}).get("number")
        events = scoreboard.get("events", [])
        if events and all(
            event.get("status", {}).get("type", {}).get("completed") is True
            or event.get("status", {}).get("type", {}).get("state") == "post"
            for event in events
        ):
            self._archive_completed_week(events, season, season_type, week)
            next_week = int(week or 0) + 1
            scoreboard = self._json(
                f"{self.ESPN_SITE}/scoreboard?dates={season}&seasontype={season_type}&week={next_week}&limit=50"
            )
            if not scoreboard.get("events") and season_type in (1, 2):
                scoreboard = self._json(
                    f"{self.ESPN_SITE}/scoreboard?dates={season}&seasontype={season_type + 1}&week=1&limit=50"
                )
        matchups = []
        for event in scoreboard.get("events", []):
            competition = (event.get("competitions") or [{}])[0]
            competitors = {item.get("homeAway"): item for item in competition.get("competitors", [])}
            home = competitors.get("home", {})
            away = competitors.get("away", {})
            home_team = home.get("team", {})
            away_team = away.get("team", {})
            odds = (competition.get("odds") or [{}])[0]
            favorite_id = None
            if odds.get("homeTeamOdds", {}).get("favorite"):
                favorite_id = home_team.get("id")
            elif odds.get("awayTeamOdds", {}).get("favorite"):
                favorite_id = away_team.get("id")

            home_metrics = self._metric_for(home_team.get("abbreviation"))
            away_metrics = self._metric_for(away_team.get("abbreviation"))
            model_edge = home_metrics.get("adjustedEpaPerPlay", 0) - away_metrics.get("adjustedEpaPerPlay", 0)
            model_edge += (home_metrics.get("turnoverMarginPerGame", 0) - away_metrics.get("turnoverMarginPerGame", 0)) * 0.012
            model_edge += (home_metrics.get("specialTeamsScore", 0) - away_metrics.get("specialTeamsScore", 0)) * 0.004
            model_edge += 0.025
            market_edge = 0.035 if favorite_id == home_team.get("id") else -0.035 if favorite_id == away_team.get("id") else 0
            blended_edge = model_edge * 0.7 + market_edge * 0.3
            home_probability = 1 / (1 + math.exp(-blended_edge * 6))
            projected_winner = home_team.get("displayName") if home_probability >= .5 else away_team.get("displayName")

            venue = competition.get("venue", {})
            address = venue.get("address", {})
            game_state = event.get("status", {}).get("type", {}).get("state", "pre")
            matchup = {
                "id": event.get("id"),
                "name": event.get("name"),
                "date": event.get("date"),
                "status": event.get("status", {}).get("type", {}).get("detail", "Scheduled"),
                "gameState": game_state,
                "homeScore": self._number(home.get("score")),
                "awayScore": self._number(away.get("score")),
                "home": self._team_summary(home_team),
                "away": self._team_summary(away_team),
                "projectedWinner": projected_winner,
                "homeWinProbability": self._probability(home_probability),
                "awayWinProbability": self._probability(1 - home_probability),
                "spread": odds.get("details") or "Not available",
                "venue": venue.get("fullName", "Venue TBD"),
                "location": ", ".join(filter(None, [address.get("city"), address.get("state")])),
            }
            if game_state == "in":
                try:
                    live = self.game_probability(str(event.get("id")))
                    matchup.update({
                        "projectedWinner": live.get("winner", matchup["projectedWinner"]),
                        "homeWinProbability": live.get("homeWinProbability", matchup["homeWinProbability"]),
                        "awayWinProbability": live.get("awayWinProbability", matchup["awayWinProbability"]),
                        "homeScore": live.get("homeScore", matchup["homeScore"]),
                        "awayScore": live.get("awayScore", matchup["awayScore"]),
                        "status": live.get("status", matchup["status"]),
                    })
                except Exception as error:
                    print(f"Could not update live matchup {event.get('id')}: {error}")
            matchups.append(matchup)
        return {
            "season": scoreboard.get("season", {}).get("year"),
            "seasonType": scoreboard.get("season", {}).get("type"),
            "week": scoreboard.get("week", {}).get("number"),
            "matchups": matchups,
            "rankingsSeason": 2025,
            "accuracy": self.accuracy(scoreboard.get("season", {}).get("year")),
            "history": self.history(scoreboard.get("season", {}).get("year"), scoreboard.get("season", {}).get("type"), scoreboard.get("week", {}).get("number")),
        }

    def _team_summary(self, team):
        return {
            "id": team.get("id"),
            "name": team.get("displayName"),
            "abbreviation": team.get("abbreviation"),
            "logo": team.get("logo"),
            "color": team.get("color"),
        }

    def _depth_chart(self, team, unavailable_ids=None):
        data = self._json(f'{self.ESPN_SITE}/teams/{team["abbreviation"].lower()}/depthcharts')
        charts = data.get("depthchart", [])
        offense_chart = next((chart for chart in charts if "WR" in chart.get("name", "") or chart.get("name", "").endswith("O")), {})
        defense_chart = next((chart for chart in charts if chart.get("name", "").endswith("D")), {})
        return {
            "offense": self._starters(offense_chart, team, unavailable_ids),
            "defense": self._starters(defense_chart, team, unavailable_ids),
            "offenseScheme": offense_chart.get("name", "Offense"),
            "defenseScheme": defense_chart.get("name", "Defense"),
        }

    def _starters(self, chart, team, unavailable_ids=None):
        unavailable_ids = unavailable_ids or set()
        starters = []
        for position in (chart.get("positions") or {}).values():
            athletes = position.get("athletes") or []
            if not athletes:
                continue
            athlete = next(
                (candidate for candidate in athletes if str(candidate.get("id")) not in unavailable_ids),
                None,
            )
            if not athlete:
                continue
            abbreviation = position.get("position", {}).get("abbreviation", "")
            starters.append({
                "id": athlete.get("id"),
                "name": athlete.get("displayName"),
                "shortName": athlete.get("shortName"),
                "position": abbreviation,
                "shape": "circle",
                "headshot": athlete.get("headshot", {}).get("href"),
                "teamId": team.get("id"),
                "team": team.get("abbreviation"),
            })
        return starters[:12]

    def matchup_detail(self, event_id):
        summary = self._json(f"{self.ESPN_SITE}/summary?event={event_id}")
        competition = (summary.get("header", {}).get("competitions") or [{}])[0]
        competitors = {item.get("homeAway"): item for item in competition.get("competitors", [])}
        teams = {}
        for side in ("away", "home"):
            source = competitors.get(side, {}).get("team", {})
            teams[side] = self._team_summary(source)

        power_rankings = self._espn_power_rankings()
        for team in teams.values():
            ranking = power_rankings["rankings"].get(team.get("abbreviation"), {})
            team["powerRank"] = ranking.get("rank")

        injuries = {}
        unavailable_by_team = defaultdict(set)
        injury_groups = summary.get("injuries", {})
        if isinstance(injury_groups, dict):
            injury_groups = list(injury_groups.values())
        for group in injury_groups or []:
            for injury in group.get("injuries", []):
                athlete = injury.get("athlete", {})
                did_not_finish = (injury.get("status") or "").lower() == "out" or (injury.get("type", {}).get("abbreviation") or "").upper() == "O"
                injuries[str(athlete.get("id"))] = {
                    "status": injury.get("status"),
                    "type": injury.get("details", {}).get("type"),
                    "detail": injury.get("details", {}).get("detail"),
                    "didNotFinish": did_not_finish,
                }
                status = (injury.get("status") or "").lower()
                status_type = (injury.get("type", {}).get("abbreviation") or "").upper()
                if status in {"out", "injured reserve", "suspended"} or status_type in {"O", "IR", "SUSP"}:
                    team_abbreviation = group.get("team", {}).get("abbreviation")
                    unavailable_by_team[team_abbreviation].add(str(athlete.get("id")))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                side: executor.submit(self._depth_chart, team, unavailable_by_team.get(team.get("abbreviation"), set()))
                for side, team in teams.items()
            }
            depth = {side: future.result() for side, future in futures.items()}

        for side, team in teams.items():
            team["statistics"] = self._metric_for(team.get("abbreviation"))
            team["depthChart"] = depth[side]
            for unit in ("offense", "defense"):
                for player in team["depthChart"][unit]:
                    player["injury"] = injuries.get(str(player["id"]))

        pick = (summary.get("pickcenter") or [{}])[0]
        prediction = self._pregame_prediction(summary, teams, unavailable_by_team)
        saved_pregame = self._saved_pregame_prediction(event_id)
        pregame_prediction = saved_pregame.get("prediction") if saved_pregame else None
        live_state = competition.get("status", {}).get("type", {}).get("state", "pre")
        if live_state == "pre":
            projection_jobs = []
            with ThreadPoolExecutor(max_workers=8) as executor:
                for side, opponent_side in (("away", "home"), ("home", "away")):
                    for player in teams[side].get("depthChart", {}).get("offense", []):
                        if player.get("position") in {"QB", "RB", "WR"}:
                            projection_jobs.append(executor.submit(self._skill_projection, player, teams[opponent_side].get("abbreviation")))
                player_projections = [result for result in (job.result() for job in projection_jobs) if result and result.get("projected")]
            self._save_pregame_prediction(event_id, prediction, teams, player_projections)
            saved_pregame = self._saved_pregame_prediction(event_id)
            pregame_prediction = saved_pregame.get("prediction") if saved_pregame else prediction
        if live_state in {"in", "post"}:
            prediction = self.game_probability(event_id)
        if live_state == "post" and not pregame_prediction:
            pregame_prediction = self._pregame_prediction(summary, teams, unavailable_by_team)
            pregame_prediction["reconstructed"] = True
        positions = {
            str(player.get("id")): player.get("position")
            for team in teams.values()
            for player in team.get("depthChart", {}).get("offense", [])
        }
        player_comparisons = []
        if live_state == "post":
            actual_players = self._actual_skill_stats(summary, positions, injuries)
            saved_players = {str(item.get("id")): item for item in (saved_pregame or {}).get("playerProjections", [])}
            for actual in actual_players:
                forecast_player = saved_players.get(actual["id"], {})
                actual["projected"] = forecast_player.get("projected", {})
                player_comparisons.append(actual)
            self._settle_prediction(event_id, summary, prediction, teams, player_comparisons)
        articles = summary.get("news") or []
        if isinstance(articles, dict):
            articles = articles.get("articles", [])
        injury_terms = {
            "injury", "injured", "inactive", "questionable", "doubtful", "ruled out",
            "will not play", "won't play", "concussion", "reserve", "pup", "limited",
        }
        team_terms = {}
        player_terms = set()
        injured_player_terms = set()
        for side, team in teams.items():
            display_name = (team.get("name") or "").lower()
            words = display_name.split()
            team_terms[side] = {display_name, " ".join(words[:-1]), words[-1] if words else ""} - {""}
            for unit in ("offense", "defense"):
                for player in team.get("depthChart", {}).get(unit, []):
                    name = (player.get("name") or "").lower()
                    if name:
                        player_terms.add(name)
                        if player.get("injury"):
                            injured_player_terms.add(name)

        relevant_articles = []
        for article in articles:
            text = " ".join(filter(None, [article.get("headline"), article.get("description")])).lower()
            mentions_sides = [any(term in text for term in team_terms.get(side, set())) for side in ("away", "home")]
            mentions_player = any(name in text for name in player_terms)
            mentions_injured_player = any(name in text for name in injured_player_terms)
            injury_story = any(term in text for term in injury_terms) and (mentions_player or any(mentions_sides))
            matchup_story = all(mentions_sides)
            if injury_story or mentions_injured_player or matchup_story:
                relevant_articles.append(article)

        return {
            "id": event_id,
            "teams": teams,
            "odds": {
                "details": pick.get("details", "Not available"),
                "overUnder": pick.get("overUnder"),
            },
            "prediction": prediction,
            "pregamePrediction": pregame_prediction,
            "playerComparisons": player_comparisons,
            "gameState": live_state,
            "weather": (summary.get("gameInfo") or {}).get("weather") or summary.get("weather"),
            "articles": [
                {"headline": article.get("headline"), "description": article.get("description"), "link": article.get("links", {}).get("web", {}).get("href")}
                for article in relevant_articles[:6]
            ],
            "rankingsSeason": 2025,
            "accuracy": self.accuracy((summary.get("header", {}).get("season") or {}).get("year")),
            "powerRankings": {
                "title": power_rankings.get("title"),
                "published": power_rankings.get("published"),
                "link": power_rankings.get("link"),
            },
        }

    def _team_sacks_allowed(self, team_abbreviation):
        if not team_abbreviation:
            return "Not available"
        data = self._json(f"{self.ESPN_SITE}/teams/{team_abbreviation.lower()}/statistics?season=2025")
        for category in data.get("results", {}).get("stats", {}).get("categories", []):
            if category.get("name") == "passing":
                stat = next((item for item in category.get("stats", []) if item.get("name") == "sacks"), None)
                if stat:
                    return stat.get("displayValue") or stat.get("value") or "Not available"
        return "Not available"

    def _defensive_line_production(self, athlete_id):
        if not athlete_id:
            return {"sacks": 0.0, "tacklesForLoss": 0.0, "games": 0.0}
        try:
            data = self._json(f"{self.ESPN_WEB}/athletes/{athlete_id}/stats?region=us&lang=en&contentorigin=espn")
            defensive = next((category for category in data.get("categories", []) if (category.get("name") or "").lower() == "defensive"), {})
            row = max(defensive.get("statistics", []), key=lambda item: item.get("season", {}).get("year", 0), default={})
            stats = dict(zip(defensive.get("names", []), row.get("stats", [])))
            return {
                "sacks": self._number(stats.get("sacks")),
                "tacklesForLoss": self._number(stats.get("stuffs")),
                "games": self._number(stats.get("gamesPlayed")),
            }
        except Exception as error:
            print(f"Could not load line production for {athlete_id}: {error}")
            return {"sacks": 0.0, "tacklesForLoss": 0.0, "games": 0.0}

    def _matchup_production(self, athlete_id, category_name, stat_names):
        if not athlete_id:
            return {name: 0.0 for name in stat_names}
        try:
            data = self._json(f"{self.ESPN_WEB}/athletes/{athlete_id}/stats?region=us&lang=en&contentorigin=espn")
            category = next((item for item in data.get("categories", []) if (item.get("name") or "").lower() == category_name), {})
            row = max(category.get("statistics", []), key=lambda item: item.get("season", {}).get("year", 0), default={})
            stats = dict(zip(category.get("names", []), row.get("stats", [])))
            return {name: self._number(stats.get(name)) for name in stat_names}
        except Exception as error:
            print(f"Could not load matchup production for {athlete_id}: {error}")
            return {name: 0.0 for name in stat_names}

    def _receiver_corner_evaluation(self, receiver_id, defender_id, defense_abbreviation, receiver_position="WR"):
        receiver = self._matchup_production(receiver_id, "receiving", ["receptions", "receivingTargets", "receivingYards", "gamesPlayed"])
        defender = self._matchup_production(defender_id, "defensive", ["interceptions", "passesDefended", "gamesPlayed"])
        defense = self._metric_for(defense_abbreviation) if defense_abbreviation else {}
        receiver_games = receiver["gamesPlayed"] or 1
        receptions_per_game = receiver["receptions"] / receiver_games
        targets_per_game = receiver["receivingTargets"] / receiver_games
        yards_per_game = receiver["receivingYards"] / receiver_games
        baselines = {"WR": (4.5, 6.5, 55), "TE": (3.5, 5.0, 40), "RB": (2.5, 3.5, 25)}
        baseline_receptions, baseline_targets, baseline_yards = baselines.get(receiver_position, baselines["WR"])
        receiver_score = 50
        receiver_score += (receptions_per_game - baseline_receptions) * 4.0
        receiver_score += (targets_per_game - baseline_targets) * 1.8
        receiver_score += (yards_per_game - baseline_yards) * 0.10
        receiver_score -= defender["interceptions"] * 2.0 + defender["passesDefended"] * 0.5
        receiver_score += (self._number(defense.get("defensePassYpg")) - 220) * 0.12 if defense.get("defensePassYpg") else 0
        receiver_score += (self._number(defense.get("defensePassRank")) - 16.5) * 0.65 if defense.get("defensePassRank") else 0
        return {
            "receiverScore": round(max(1, min(99, receiver_score))),
            "receptionsPerGame": round(receptions_per_game, 1),
            "targetsPerGame": round(targets_per_game, 1),
            "yardsPerGame": round(yards_per_game, 1),
            "interceptions": defender["interceptions"],
            "passesDefended": defender["passesDefended"],
        }

    def player_detail(self, athlete_id, opponent_abbreviation=None, position=None, team_abbreviation=None, defender_name=None, defender_position=None, matchup_player_id=None):
        try:
            data = self._json(f"{self.ESPN_WEB}/athletes/{athlete_id}/stats?region=us&lang=en&contentorigin=espn")
        except HTTPError as error:
            if error.code != 404:
                raise
            data = {"categories": [], "teams": {}}
        athlete = self._json(f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/athletes/{athlete_id}?lang=en&region=us")
        categories = []
        projected = {}
        receiving_matchup = None
        line_matchup = None
        coverage_matchup = None
        opponent = self._metric_for(opponent_abbreviation) if opponent_abbreviation else {}
        position = (position or "").upper()
        defensive_line = position in {"DE", "LDE", "RDE", "LE", "RE", "DT", "LDT", "RDT", "NT", "DL"} or position.endswith(("DE", "DT"))
        offensive_line = position in {"LT", "LG", "C", "RG", "RT", "OL", "OT", "OG"}
        defensive_back = position in {"CB", "LCB", "RCB", "NB", "DB", "S", "FS", "SS"} or position.endswith(("CB", "S"))
        linebacker = position in {"LB", "ILB", "OLB", "MLB", "LILB", "RILB", "WLB", "SLB"} or position.endswith("LB")
        allowed_categories = {
            "QB": {"passing", "rushing"},
            "RB": {"rushing", "receiving"},
            "FB": {"rushing", "receiving"},
            "WR": {"receiving"},
            "TE": {"receiving"},
            "K": {"kicking"},
            "PK": {"kicking"},
            "P": {"punting"},
        }
        if position == "C":
            categories.append({
                "name": "Offensive Line",
                "season": 2025,
                "stats": [{"name": "Team Sacks Allowed", "value": self._team_sacks_allowed(team_abbreviation)}],
            })
        allowed = {"defensive"} if defensive_line or defensive_back or linebacker else allowed_categories.get(position, set())
        relevant_categories = [
            category for category in data.get("categories", [])
            if (category.get("name") or category.get("displayName") or "").lower() in allowed
        ]
        history_years = sorted({
            row.get("season", {}).get("year")
            for category in relevant_categories
            for row in category.get("statistics", [])
            if row.get("season", {}).get("year")
        }, reverse=True)[:3]
        latest_history_team = None
        for category in relevant_categories:
            rows = sorted(category.get("statistics", []), key=lambda item: item.get("season", {}).get("year", 0), reverse=True)
            for row_index, row in enumerate(row for row in rows if row.get("season", {}).get("year") in history_years):
                stats = dict(zip(category.get("names", []), row.get("stats", [])))
                useful = [
                    {"name": display, "value": value}
                    for name, display, value in zip(category.get("names", []), category.get("displayNames", []), row.get("stats", []))
                    if value not in ("0", "0.0", "--", None)
                    and not (defensive_line and name not in {"gamesPlayed", "totalTackles", "soloTackles", "assistTackles", "sacks", "stuffs", "forcedFumbles", "fumbleRecoveries"})
                ]
                games_played = self._number(stats.get("gamesPlayed"))
                category_name = (category.get("name") or category.get("displayName") or "").lower()
                per_game_stat = None
                if games_played:
                    if category_name == "passing" and position == "QB" and self._number(stats.get("passingYards")):
                        per_game_stat = {"name": "Passing Yards Per Game", "value": round(self._number(stats["passingYards"]) / games_played, 1)}
                    elif category_name == "rushing" and position in {"QB", "RB", "FB"} and self._number(stats.get("rushingYards")):
                        per_game_stat = {"name": "Rushing Yards Per Game", "value": round(self._number(stats["rushingYards"]) / games_played, 1)}
                    elif category_name == "receiving" and position in {"RB", "FB", "WR", "TE"} and self._number(stats.get("receivingYards")):
                        per_game_stat = {"name": "Receiving Yards Per Game", "value": round(self._number(stats["receivingYards"]) / games_played, 1)}
                if per_game_stat:
                    useful.insert(1 if useful and useful[0]["name"] == "Games Played" else 0, per_game_stat)
                if defensive_line:
                    for stat in useful:
                        if stat["name"].lower() == "stuffs":
                            stat["name"] = "Tackles for Loss (ESPN Stuffs)"
                team = data.get("teams", {}).get(row.get("teamSlug"), {})
                team_name = team.get("displayName") or row.get("teamSlug", "").replace("-", " ").title() or "Team unavailable"
                if row.get("season", {}).get("year") == (history_years[0] if history_years else None):
                    latest_history_team = latest_history_team or team
                categories.append({
                    "name": category.get("displayName"),
                    "season": row.get("season", {}).get("year"),
                    "team": team_name,
                    "stats": useful[:14],
                })
                if row_index == 0:
                    games = self._number(stats.get("gamesPlayed")) or 1
                    row_position = position or row.get("position")
                    if row_position == "QB" and stats.get("passingYards"):
                        projected["passingYards"] = round(self._number(stats["passingYards"]) / games * opponent.get("defensePassYpg", 220) / 220, 1)
                    if row_position == "RB" and stats.get("rushingYards"):
                        projected["rushingYards"] = round(self._number(stats["rushingYards"]) / games * opponent.get("defenseRushYpg", 110) / 110, 1)
                    if row_position in ("WR", "TE", "RB") and stats.get("receivingYards"):
                        projected["receivingYards"] = round(self._number(stats["receivingYards"]) / games * opponent.get("defensePassYpg", 220) / 220, 1)
                    if row_position == "WR" and stats.get("receptions"):
                        receptions_per_game = self._number(stats["receptions"]) / games
                        targets_per_game = self._number(stats.get("receivingTargets")) / games
                        receiving_yards_per_game = self._number(stats.get("receivingYards")) / games
                        opponent_factor = max(0.75, min(1.30, opponent.get("defensePassYpg", 220) / 220))
                        projected["receptions"] = round(receptions_per_game * opponent_factor, 1)
                        baseline_receptions = 4.5
                        baseline_yards = 55
                        matchup_score = 50
                        matchup_score += (receptions_per_game - baseline_receptions) * 4.0
                        matchup_score += (targets_per_game - baseline_receptions * 1.45) * 1.8
                        matchup_score += (receiving_yards_per_game - baseline_yards) * 0.10
                        matchup_score += (opponent.get("defensePassYpg", 220) - 220) * 0.12
                        matchup_score += (16.5 - opponent.get("defensePassRank", 16.5)) * 0.65
                        score = round(max(1, min(99, matchup_score)))
                        receiving_matchup = {
                            "defender": defender_name or f"{opponent_abbreviation or 'Opponent'} coverage unit",
                            "defenderPosition": defender_position,
                            "score": score,
                            "grade": "Great" if score >= 75 else "Good" if score >= 60 else "Average" if score >= 45 else "Difficult",
                            "opponentPassRank": opponent.get("defensePassRank"),
                            "opponentPassYpg": opponent.get("defensePassYpg"),
                            "receptionsPerGame": round(receptions_per_game, 1),
                            "targetsPerGame": round(targets_per_game, 1),
                            "receivingYardsPerGame": round(receiving_yards_per_game, 1),
                        }

        experience_years = athlete.get("experience", {}).get("years")
        is_rookie = experience_years is not None and experience_years <= 1
        previous_abbreviation = latest_history_team.get("abbreviation") if latest_history_team else None
        new_team = bool(previous_abbreviation and team_abbreviation and previous_abbreviation != team_abbreviation)
        categories.sort(key=lambda category: category.get("season") or 0, reverse=True)

        if position == "WR" and receiving_matchup and matchup_player_id:
            shared_matchup = self._receiver_corner_evaluation(athlete_id, matchup_player_id, opponent_abbreviation, position)
            receiver_score = shared_matchup["receiverScore"]
            corner_score = 100 - receiver_score
            defender_role = "Safety" if defender_position in {"S", "FS", "SS"} else "Corner"
            advantage = "Receiver advantage" if receiver_score >= 56 else f"{defender_role} advantage" if receiver_score <= 44 else "Even matchup"
            receiving_matchup.update({
                "score": receiver_score,
                "matchupScore": receiver_score,
                "receiverScore": receiver_score,
                "cornerScore": corner_score,
                "defenderScore": corner_score,
                "defenderRole": defender_role,
                "advantage": advantage,
                "grade": "Great" if receiver_score >= 75 else "Good" if receiver_score >= 60 else "Average" if receiver_score >= 45 else "Difficult",
                "receptionsPerGame": shared_matchup["receptionsPerGame"],
                "targetsPerGame": shared_matchup["targetsPerGame"],
                "receivingYardsPerGame": shared_matchup["yardsPerGame"],
            })

        if position in {"CB", "LCB", "RCB", "NB", "DB"} and matchup_player_id:
            receiver_position = (defender_position or "WR").upper()
            shared_matchup = self._receiver_corner_evaluation(matchup_player_id, athlete_id, team_abbreviation, receiver_position)
            receiver_score = shared_matchup["receiverScore"]
            corner_score = 100 - receiver_score
            defender_role = "Corner"
            advantage = "Receiver advantage" if receiver_score >= 56 else f"{defender_role} advantage" if receiver_score <= 44 else "Even matchup"
            coverage_matchup = {
                "receiver": defender_name or "Opposing receiver",
                "receiverPosition": defender_position,
                "score": receiver_score,
                "matchupScore": receiver_score,
                "receiverScore": receiver_score,
                "cornerScore": corner_score,
                "defenderScore": corner_score,
                "defenderRole": defender_role,
                "advantage": advantage,
                "grade": "Great" if corner_score >= 75 else "Good" if corner_score >= 60 else "Average" if corner_score >= 45 else "Difficult",
                "receiverReceptionsPerGame": shared_matchup["receptionsPerGame"],
                "receiverTargetsPerGame": shared_matchup["targetsPerGame"],
                "receiverYardsPerGame": shared_matchup["yardsPerGame"],
                "interceptions": shared_matchup["interceptions"],
                "passesDefended": shared_matchup["passesDefended"],
            }

        if offensive_line or defensive_line:
            if offensive_line:
                opposing_production = self._defensive_line_production(matchup_player_id)
                team_sacks = self._number(self._team_sacks_allowed(team_abbreviation))
                score = 65
                score -= opposing_production["sacks"] * 2.0
                score -= opposing_production["tacklesForLoss"] * 0.45
                score += (opponent.get("defensePassRank", 16.5) - 16.5) * 0.7
                score += (32 - team_sacks) * 0.35
                basis = {**opposing_production, "teamSacksAllowed": team_sacks, "opponentPassRank": opponent.get("defensePassRank")}
            else:
                own_production = self._defensive_line_production(athlete_id)
                opponent_sacks = self._number(self._team_sacks_allowed(opponent_abbreviation))
                score = 45
                score += own_production["sacks"] * 2.0
                score += own_production["tacklesForLoss"] * 0.45
                score += (opponent_sacks - 32) * 0.4
                basis = {**own_production, "opponentSacksAllowed": opponent_sacks}
            score = round(max(1, min(99, score)))
            line_matchup = {
                "opponent": defender_name or "Opposing lineman",
                "opponentPosition": defender_position,
                "score": score,
                "grade": "Great" if score >= 75 else "Good" if score >= 60 else "Average" if score >= 45 else "Difficult",
                "side": "offense" if offensive_line else "defense",
                "basis": basis,
            }

        return {
            "categories": categories,
            "projected": projected,
            "receivingMatchup": receiving_matchup,
            "coverageMatchup": coverage_matchup,
            "lineMatchup": line_matchup,
            "playerStatus": {
                "rookie": is_rookie,
                "newTeam": new_team,
                "previousTeam": latest_history_team.get("displayName") if new_team else None,
            },
            "newsLink": f"https://www.espn.com/nfl/player/news/_/id/{athlete_id}",
            "projectionNote": "Projection uses the player's latest season per-game production adjusted by the opponent's 2025 defense.",
        }

    def _number(self, value):
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

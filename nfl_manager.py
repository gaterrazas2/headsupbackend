import csv
import html
import json
import math
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class NFLManager:
    ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
    ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl"
    TEAM_STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"
    PLAYER_STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
    ABBR_TO_DATA = {"LAR": "LA", "WSH": "WAS"}
    _metrics_cache = {}
    _metrics_cached_at = {}
    _power_rankings_cache = None
    _power_rankings_cached_at = 0
    _power_rankings_2025_cache = None
    _sportsbook_cache = {}
    _player_stats_cache = {}
    _player_stats_cached_at = {}

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
        return list(self.archives_collection.find(query, {"_id": 0}).sort("week", 1))

    def _settle_completed_event(self, event):
        """Grade a final game from its box score without building full matchup detail."""
        event_id = str(event.get("id"))
        saved = self._saved_pregame_prediction(event_id) or {}
        if not saved or saved.get("settled"):
            return
        summary = self._json(f"{self.ESPN_SITE}/summary?event={event_id}")
        competition = (summary.get("header", {}).get("competitions") or [{}])[0]
        competitors = {item.get("homeAway"): item for item in competition.get("competitors", [])}
        teams = {
            side: self._team_summary(competitors.get(side, {}).get("team", {}))
            for side in ("away", "home")
        }
        prediction = {
            "source": "final",
            "awayScore": self._number(competitors.get("away", {}).get("score")),
            "homeScore": self._number(competitors.get("home", {}).get("score")),
        }
        saved_players = {str(item.get("id")): item for item in saved.get("playerProjections", [])}
        positions = {player_id: item.get("position") for player_id, item in saved_players.items()}
        comparisons = self._actual_skill_stats(summary, positions, {})
        for actual in comparisons:
            actual["projected"] = saved_players.get(actual["id"], {}).get("projected", {})
        self._settle_prediction(event_id, summary, prediction, teams, comparisons)

    def _backfill_completed_week_archives(self, season, season_type, current_week):
        """Persist any finished earlier week ESPN has already advanced past."""
        if self.archives_collection is None or not season or not season_type or not current_week:
            return
        existing_weeks = {
            row.get("week") for row in self.archives_collection.find(
                {"season": season, "seasonType": season_type, "week": {"$lt": current_week}},
                {"_id": 0, "week": 1},
            )
        }
        for previous_week in range(1, int(current_week)):
            if previous_week in existing_weeks:
                continue
            scoreboard = self._json(
                f"{self.ESPN_SITE}/scoreboard?dates={season}&seasontype={season_type}&week={previous_week}&limit=50"
            )
            events = scoreboard.get("events", [])
            if not events or not all(
                event.get("status", {}).get("type", {}).get("completed") is True
                or event.get("status", {}).get("type", {}).get("state") == "post"
                for event in events
            ):
                continue
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(self._settle_completed_event, event) for event in events]
                for event, future in zip(events, futures):
                    try:
                        future.result()
                    except Exception as error:
                        print(f"Could not grade archived matchup {event.get('id')}: {error}")
            self._archive_completed_week(events, season, season_type, previous_week)

    def snapshot_announced_rosters(self):
        """Save every upcoming forecast and automatically grade completed games."""
        scoreboard = self._json(f"{self.ESPN_SITE}/scoreboard?limit=50")
        season = scoreboard.get("season", {}).get("year") or time.gmtime().tm_year
        saved, skipped, settled, missed, errors = [], [], [], [], []
        for event in scoreboard.get("events", []):
            state = event.get("status", {}).get("type", {}).get("state")
            event_id = str(event.get("id"))
            existing = self._saved_pregame_prediction(event_id) or {}
            if state == "post":
                if not existing:
                    self.predictions_collection.update_one(
                        {"gameId": event_id, "sport": "nfl", "phase": "missed"},
                        {"$setOnInsert": {"gameId": event_id, "sport": "nfl", "phase": "missed", "season": season, "missed": True, "createdAt": int(time.time())}},
                        upsert=True,
                    )
                    missed.append(event_id)
                elif existing.get("settled"):
                    skipped.append(event_id)
                else:
                    try:
                        self.matchup_detail(event_id, include_sportsbook=False)
                        settled.append(event_id)
                    except Exception as error:
                        print(f"Could not automatically grade NFL game {event_id}: {error}")
                        errors.append(event_id)
                continue
            if state != "pre":
                continue
            if existing.get("playerProjections") and all(item.get("modelVersion") == 3 for item in existing["playerProjections"]):
                skipped.append(event_id)
                continue
            try:
                self.matchup_detail(event_id, include_sportsbook=False)
                saved.append(event_id)
            except Exception as error:
                print(f"Could not automatically snapshot NFL game {event_id}: {error}")
                errors.append(event_id)
        return {"saved": saved, "settled": settled, "missed": missed, "alreadySaved": skipped, "errors": errors}

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
                {"gameId": str(event_id), "sport": "nfl", "phase": "pregame", "$or": [{"playerProjections": {"$exists": False}}, {"playerProjections": []}, {"playerProjections.modelVersion": {"$ne": 3}}]},
                {"$set": {"playerProjections": player_projections}},
            )

    def _saved_pregame_prediction(self, event_id):
        if self.predictions_collection is None:
            return None
        row = self.predictions_collection.find_one(
            {"gameId": str(event_id), "sport": "nfl", "phase": "pregame"},
            {"_id": 0, "season": 1, "prediction": 1, "playerProjections": 1, "settled": 1, "grading": 1, "sportsbookTopProps": 1, "sportsbookTopPropsStatus": 1, "sportsbookTopPropsCheckedAt": 1, "sportsbookModelVersion": 1},
        )
        return row if row else None

    def _saved_final_detail(self, event_id):
        """Build a completed-game view entirely from MongoDB."""
        if self.archives_collection is None:
            return None
        saved = self._saved_pregame_prediction(event_id) or {}
        archive = self.archives_collection.find_one(
            {"games.id": str(event_id)}, {"_id": 0, "season": 1, "games.$": 1}
        )
        game = ((archive or {}).get("games") or [None])[0]
        if not game:
            return None
        grading = saved.get("grading", {}) if saved.get("settled") else {}
        # Older settled snapshots only retained graded yardage/reception props,
        # which discarded actual touchdown totals. Rebuild those snapshots once
        # from ESPN, then persist the complete comparisons for future requests.
        if "playerComparisons" not in grading:
            return None
        comparisons = grading.get("playerComparisons") or []
        home_score, away_score = game.get("homeScore", 0), game.get("awayScore", 0)
        return {
            "id": str(event_id), "teams": {"away": game.get("away", {}), "home": game.get("home", {})},
            "odds": {"details": "Final", "overUnder": None}, "gameState": "post",
            "prediction": {"source": "final", "awayScore": away_score, "homeScore": home_score, "status": game.get("status", "Final")},
            "pregamePrediction": saved.get("prediction"), "playerComparisons": comparisons,
            "topProps": [], "topPropsStatus": None, "articles": [],
            "rankingsSeason": 2025, "currentRankingsSeason": archive.get("season") or saved.get("season"),
            "accuracy": self.accuracy(archive.get("season") or saved.get("season")),
        }

    def accuracy(self, season=None):
        if self.predictions_collection is None:
            return {"score": None, "wins": 0, "losses": 0, "games": {"wins": 0, "losses": 0, "graded": 0, "missed": 0}, "props": {"wins": 0, "losses": 0}}
        season = season or time.gmtime().tm_year
        rows = self.predictions_collection.find({"sport": "nfl", "season": season, "settled": True}, {"_id": 0, "grading": 1})
        missed_games = self.predictions_collection.count_documents({"sport": "nfl", "season": season, "phase": "missed"})
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
            "games": {"wins": game_wins, "losses": game_losses, "graded": game_wins + game_losses, "missed": missed_games},
            "props": {"wins": prop_wins, "losses": prop_losses},
        }

    def _settle_prediction(self, event_id, summary, prediction, teams, player_comparisons):
        if self.predictions_collection is None:
            return
        saved = self._saved_pregame_prediction(event_id)
        if not saved:
            return
        if saved.get("settled"):
            if "playerComparisons" not in (saved.get("grading") or {}):
                self.predictions_collection.update_one(
                    {"gameId": str(event_id), "sport": "nfl", "phase": "pregame"},
                    {"$set": {"grading.playerComparisons": player_comparisons}},
                )
            return
        actual_winner = "Tie" if prediction.get("homeScore") == prediction.get("awayScore") else (
            teams["home"].get("name") if prediction.get("homeScore", 0) > prediction.get("awayScore", 0) else teams["away"].get("name")
        )
        forecast = saved.get("prediction", {})
        props = []
        relevant = {"QB": ["passingYards", "completions"], "RB": ["rushingYards", "receivingYards"], "WR": ["receptions", "receivingYards"], "TE": ["receptions", "receivingYards"]}
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
                "projectedWinner": forecast.get("winner"), "actualWinner": actual_winner,
                "props": props, "playerComparisons": player_comparisons,
            }}},
        )

    def _skill_projection(self, player, opponent_abbreviation, team_abbreviation=None):
        position = (player.get("position") or "").upper()
        if position not in {"QB", "RB", "WR", "TE"}:
            return None
        try:
            data = self._json(f"{self.ESPN_WEB}/athletes/{player['id']}/stats?region=us&lang=en&contentorigin=espn")
        except Exception as error:
            print(f"Could not project {player.get('name')}: {error}")
            return None
        opponent = self._metric_for(opponent_abbreviation)
        team_metrics = self._metric_for(team_abbreviation) if team_abbreviation else {}
        projected = {}
        matchup_edges = {}
        expected_touchdowns = 0.0
        wanted = {"QB": {"passing"}, "RB": {"rushing", "receiving"}, "WR": {"receiving"}, "TE": {"receiving"}}[position]
        for category in data.get("categories", []):
            category_name = (category.get("name") or category.get("displayName") or "").lower()
            if category_name not in wanted:
                continue
            row = max(category.get("statistics", []), key=lambda item: item.get("season", {}).get("year", 0), default={})
            stats = dict(zip(category.get("names", []), row.get("stats", [])))
            games = self._number(stats.get("gamesPlayed")) or 1
            if position == "QB" and category_name == "passing":
                multiplier = max(0.75, min(1.25, 0.55 * (self._number(opponent.get("defensePassYpg")) or 220) / 220 + 0.45 * (self._number(team_metrics.get("offensePassYpg")) or 220) / 220))
                projected["passingYards"] = round(self._number(stats.get("passingYards")) / games * multiplier, 1)
                projected["completions"] = round(self._number(stats.get("completions") or stats.get("passingCompletions")) / games * multiplier, 1)
                matchup_edges["passingYards"] = round((multiplier - 1) * 100, 1)
                expected_touchdowns += self._number(stats.get("passingTouchdowns")) / games * multiplier
            elif category_name == "rushing":
                multiplier = max(0.75, min(1.25, 0.55 * (self._number(opponent.get("defenseRushYpg")) or 110) / 110 + 0.45 * (self._number(team_metrics.get("offenseRushYpg")) or 110) / 110))
                projected["rushingYards"] = round(self._number(stats.get("rushingYards")) / games * multiplier, 1)
                matchup_edges["rushingYards"] = round((multiplier - 1) * 100, 1)
                expected_touchdowns += self._number(stats.get("rushingTouchdowns")) / games * multiplier
            elif category_name == "receiving":
                multiplier = max(0.75, min(1.25, 0.55 * (self._number(opponent.get("defensePassYpg")) or 220) / 220 + 0.45 * (self._number(team_metrics.get("offensePassYpg")) or 220) / 220))
                projected["receivingYards"] = round(self._number(stats.get("receivingYards")) / games * multiplier, 1)
                matchup_edges["receivingYards"] = round((multiplier - 1) * 100, 1)
                expected_touchdowns += self._number(stats.get("receivingTouchdowns")) / games * multiplier
                if position in {"WR", "TE"}:
                    projected["receptions"] = round(self._number(stats.get("receptions")) / games * multiplier, 1)
                    matchup_edges["receptions"] = matchup_edges["receivingYards"]
        touchdown_context = self._touchdown_context(player.get("name"), team_abbreviation, opponent_abbreviation, position)
        expected_touchdowns = self._adjust_touchdown_rate(expected_touchdowns, touchdown_context, player.get("injury"))
        projected["matchupEdges"] = matchup_edges
        projected["touchdownFactors"] = touchdown_context
        projected["touchdownProbability"] = round((1 - math.exp(-max(0, expected_touchdowns))) * 100, 1)
        return {"id": str(player.get("id")), "name": player.get("name"), "team": player.get("team"), "position": position, "projected": projected, "modelVersion": 3}

    def _top_prop_candidates(self, players):
        """Rank alternate player lines, allowing QB touchdowns only at 2+."""
        configurations = {
            "QB": {"passingYards": (25, 0.25, "passing yards", 199.5)},
            "RB": {"rushingYards": (10, 0.45, "rushing yards", 39.5), "receivingYards": (10, 0.55, "receiving yards", 19.5)},
            "WR": {"receptions": (1, 0.4, "receptions", 3.5), "receivingYards": (10, 0.45, "receiving yards", 39.5)},
            "TE": {"receptions": (1, 0.45, "receptions", 2.5), "receivingYards": (10, 0.5, "receiving yards", 29.5)},
        }
        candidates = []
        for player in players:
            position = player.get("position")
            projected = player.get("projected", {})
            common = {"playerId": player.get("id"), "player": player.get("name"), "team": player.get("team"), "position": position}
            matchup_edges = projected.get("matchupEdges", {})
            for stat, (step, variation, label, minimum_line) in configurations.get(position, {}).items():
                mean = self._number(projected.get(stat))
                if mean <= 0:
                    continue
                line = math.floor(mean / step) * step - 0.5
                if line < minimum_line:
                    continue
                deviation = max(step, mean * variation)
                probability = 0.5 * (1 + math.erf((mean - line) / (deviation * math.sqrt(2))))
                probability = round(probability * 100, 1)
                edge = self._number(matchup_edges.get(stat))
                value_score = round(max(1, min(99, probability + edge * 0.5)), 1)
                market = {"passingYards": "player_pass_yds", "rushingYards": "player_rush_yds", "receivingYards": "player_reception_yds", "receptions": "player_receptions"}[stat]
                candidates.append({**common, "prop": f"Over {line:g} {label}", "probability": probability, "matchupEdge": edge, "valueScore": value_score, "market": market, "stat": stat, "mean": mean, "variation": variation, "label": label})
            if position in {"RB", "WR", "TE"} and projected.get("touchdownProbability") is not None:
                probability = projected["touchdownProbability"]
                if 30 <= probability <= 75:
                    edge = max(matchup_edges.values(), default=0)
                    candidates.append({**common, "prop": "Anytime touchdown", "probability": probability, "matchupEdge": edge, "valueScore": round(probability + edge * 0.5, 1), "market": "player_anytime_td", "stat": "touchdown"})
            if position == "QB" and projected.get("touchdownProbability") is not None:
                one_plus_probability = min(0.999, max(0.0, self._number(projected["touchdownProbability"]) / 100))
                expected_touchdowns = -math.log(1 - one_plus_probability)
                two_plus_probability = 1 - math.exp(-expected_touchdowns) * (1 + expected_touchdowns)
                if two_plus_probability >= 0.5:
                    probability = round(two_plus_probability * 100, 1)
                    edge = self._number(matchup_edges.get("passingYards"))
                    candidates.append({**common, "prop": "2+ passing touchdowns", "probability": probability, "matchupEdge": edge, "valueScore": round(probability + edge * 0.5, 1), "market": "player_pass_tds", "stat": "passingTouchdowns", "mean": expected_touchdowns})
        return sorted(candidates, key=lambda item: item["valueScore"], reverse=True)

    def _player_weekly_stats(self, season=None):
        season = season or time.gmtime().tm_year
        if season in self._player_stats_cache and time.time() - self._player_stats_cached_at.get(season, 0) < 21600:
            return self._player_stats_cache[season]
        try:
            text = self._text(self.PLAYER_STATS_URL.format(season=season))
            rows = list(csv.DictReader(StringIO(text)))
        except Exception as error:
            print(f"Could not load {season} player usage stats: {error}")
            rows = []
        self._player_stats_cache[season] = rows
        self._player_stats_cached_at[season] = time.time()
        return rows

    def _touchdown_context(self, player_name, team_abbreviation, opponent_abbreviation, position):
        rows = self._player_weekly_stats()
        previous_rows = self._player_weekly_stats(time.gmtime().tm_year - 1)
        team_code = self.ABBR_TO_DATA.get(team_abbreviation, team_abbreviation)
        opponent_code = self.ABBR_TO_DATA.get(opponent_abbreviation, opponent_abbreviation)
        current_player_rows = [row for row in rows if row.get("team") == team_code and self._same_player(player_name, row.get("player_display_name"))]
        prior_player_rows = [row for row in previous_rows if self._same_player(player_name, row.get("player_display_name"))]
        current_player_rows.sort(key=lambda row: self._number(row.get("week")))
        prior_player_rows.sort(key=lambda row: self._number(row.get("week")))
        if not current_player_rows and not prior_player_rows:
            return {}
        touchdowns = lambda row: self._number(row.get("receiving_tds")) + self._number(row.get("rushing_tds"))
        position_baseline = {"RB": 0.35, "WR": 0.28, "TE": 0.22}.get(position, 0.25)
        prior_rate = sum(touchdowns(row) for row in prior_player_rows) / len(prior_player_rows) if prior_player_rows else position_baseline
        prior_weight = min(6, len(prior_player_rows)) if prior_player_rows else 4
        season_rate = (sum(touchdowns(row) for row in current_player_rows) + prior_rate * prior_weight) / (len(current_player_rows) + prior_weight)
        current_recent = current_player_rows[-3:]
        recent_source = (prior_player_rows[-max(0, 3 - len(current_recent)):] + current_recent)[-3:]
        recent_rate = (sum(touchdowns(row) for row in recent_source) + season_rate * 2) / (len(recent_source) + 2)
        recent_rows = recent_source
        targets_per_game = sum(self._number(row.get("targets")) for row in recent_rows) / len(recent_rows)
        carries_per_game = sum(self._number(row.get("carries")) for row in recent_rows) / len(recent_rows)
        target_share = sum(self._number(row.get("target_share")) for row in recent_rows) / len(recent_rows)
        air_yards_share = sum(self._number(row.get("air_yards_share")) for row in recent_rows) / len(recent_rows)

        current_opponent_rows = [row for row in rows if row.get("opponent_team") == opponent_code and row.get("position") == position]
        prior_opponent_rows = [row for row in previous_rows if row.get("opponent_team") == opponent_code and row.get("position") == position]
        current_opponent_games = len({row.get("game_id") for row in current_opponent_rows})
        prior_opponent_games = len({row.get("game_id") for row in prior_opponent_rows}) or 1
        prior_opponent_rate = sum(touchdowns(row) for row in prior_opponent_rows) / prior_opponent_games if prior_opponent_rows else position_baseline
        defense_prior_weight = min(4, prior_opponent_games)
        opponent_position_tds = (sum(touchdowns(row) for row in current_opponent_rows) + prior_opponent_rate * defense_prior_weight) / (current_opponent_games + defense_prior_weight)
        team_rows = [row for row in rows if row.get("team") == team_code and row.get("position") in {"RB", "WR", "TE"}]
        if not team_rows:
            team_rows = [row for row in previous_rows if row.get("team") == team_code and row.get("position") in {"RB", "WR", "TE"}]
        team_tds = sum(touchdowns(row) for row in team_rows) or 1
        player_td_share = (sum(touchdowns(row) for row in current_player_rows) if current_player_rows else sum(touchdowns(row) for row in prior_player_rows)) / team_tds
        current_quarterbacks = [row for row in rows if row.get("team") == team_code and row.get("position") == "QB"]
        prior_quarterbacks = [row for row in previous_rows if row.get("team") == team_code and row.get("position") == "QB"]
        current_team_games = len({row.get("game_id") for row in current_quarterbacks})
        prior_team_games = len({row.get("game_id") for row in prior_quarterbacks}) or 1
        prior_qb_rate = sum(self._number(row.get("passing_tds")) for row in prior_quarterbacks) / prior_team_games if prior_quarterbacks else 1.5
        qb_prior_weight = min(4, prior_team_games)
        qb_pass_tds_per_game = (sum(self._number(row.get("passing_tds")) for row in current_quarterbacks) + prior_qb_rate * qb_prior_weight) / (current_team_games + qb_prior_weight)
        return {
            "seasonTdRate": round(season_rate, 3), "recentTdRate": round(recent_rate, 3),
            "targetsPerGame": round(targets_per_game, 1), "carriesPerGame": round(carries_per_game, 1),
            "targetShare": round(target_share, 3), "airYardsShare": round(air_yards_share, 3),
            "opponentPositionTdsPerGame": round(opponent_position_tds, 3),
            "teamTdShare": round(player_td_share, 3), "qbPassTdsPerGame": round(qb_pass_tds_per_game, 2), "positionBaseline": position_baseline,
        }

    def _adjust_touchdown_rate(self, base_rate, context, injury=None):
        if not context:
            return base_rate
        season_rate = context.get("seasonTdRate", 0)
        recent_rate = context.get("recentTdRate", season_rate)
        blended_rate = base_rate * 0.35 + season_rate * 0.45 + recent_rate * 0.20
        position = "RB" if context.get("carriesPerGame", 0) >= 5 else "receiver"
        if position == "RB":
            usage_factor = max(0.8, min(1.2, (context.get("carriesPerGame", 0) + context.get("targetsPerGame", 0)) / 15))
            opponent_baseline = context.get("positionBaseline", 0.35)
            opportunities = context.get("carriesPerGame", 0) + context.get("targetsPerGame", 0)
            quarterback_weight = context.get("targetsPerGame", 0) / opportunities if opportunities else 0
        else:
            role_signal = context.get("targetShare", 0) * 0.65 + context.get("airYardsShare", 0) * 0.35
            usage_factor = max(0.8, min(1.2, role_signal / 0.18 if role_signal else 1))
            opponent_baseline = context.get("positionBaseline", 0.25)
            quarterback_weight = 1
        opponent_factor = max(0.8, min(1.25, context.get("opponentPositionTdsPerGame", opponent_baseline) / opponent_baseline))
        quarterback_quality = max(0.85, min(1.15, context.get("qbPassTdsPerGame", 1.5) / 1.5))
        quarterback_factor = 1 + (quarterback_quality - 1) * quarterback_weight
        competition_factor = max(0.9, min(1.1, 0.9 + context.get("teamTdShare", 0) * 0.4))
        injury_text = " ".join(str(value) for value in (injury or {}).values()).lower()
        availability_factor = 0.75 if any(word in injury_text for word in ("questionable", "limited", "doubtful")) else 1.0
        return max(0.01, min(0.9, blended_rate * usage_factor * opponent_factor * quarterback_factor * competition_factor * availability_factor))

    @staticmethod
    def _american_decimal(price):
        return 1 + (price / 100 if price > 0 else 100 / abs(price))

    @staticmethod
    def _same_player(left, right):
        normalize = lambda value: re.sub(r"[^a-z0-9 ]", "", (value or "").lower()).split()
        a, b = normalize(left), normalize(right)
        return bool(a and b and (a == b or (a[-1] == b[-1] and a[0][0] == b[0][0])))

    def _sportsbook_value_props(self, teams, candidates):
        api_key = os.environ.get("ODDS_API_KEY")
        if not api_key or not candidates:
            return [], "Sportsbook lines are not available yet."
        markets = sorted({candidate["market"] for candidate in candidates})
        cache_key = (teams["away"].get("name"), teams["home"].get("name"), tuple(markets))
        cached = self._sportsbook_cache.get(cache_key)
        if cached and time.time() - cached["time"] < 600:
            odds = cached["odds"]
        else:
            try:
                events_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?{urlencode({'apiKey': api_key})}"
                events = self._json(events_url, timeout=8)
                event = next((item for item in events if item.get("home_team") == teams["home"].get("name") and item.get("away_team") == teams["away"].get("name")), None)
                if not event:
                    return [], "Sportsbook lines are not available for this matchup yet."
                params = urlencode({"apiKey": api_key, "regions": "us", "markets": ",".join(markets), "oddsFormat": "american"})
                odds = self._json(f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event['id']}/odds?{params}", timeout=8)
                self._sportsbook_cache[cache_key] = {"time": time.time(), "odds": odds}
            except Exception as error:
                print(f"Could not load NFL player prop odds: {error}")
                return [], "Sportsbook lines could not be loaded right now."

        priced = []
        for candidate in candidates:
            best = None
            for bookmaker in odds.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != candidate["market"]:
                        continue
                    for outcome in market.get("outcomes", []):
                        described_player = outcome.get("description") or (outcome.get("name") if candidate["market"] == "player_anytime_td" else "")
                        if not self._same_player(candidate["player"], described_player):
                            continue
                        if candidate["market"] != "player_anytime_td" and outcome.get("name") != "Over":
                            continue
                        point = self._number(outcome.get("point"))
                        if candidate["market"] == "player_pass_tds" and point < 1.5:
                            continue
                        price = self._number(outcome.get("price"))
                        if not price:
                            continue
                        if candidate["stat"] == "touchdown":
                            probability = candidate["probability"] / 100
                            prop = "Anytime touchdown"
                        elif candidate["stat"] == "passingTouchdowns":
                            expected = candidate["mean"]
                            required = math.floor(point) + 1
                            probability = 1 - sum(math.exp(-expected) * expected ** count / math.factorial(count) for count in range(required))
                            prop = f"{required}+ passing touchdowns"
                        else:
                            deviation = max(1, candidate["mean"] * candidate["variation"])
                            probability = 0.5 * (1 + math.erf((candidate["mean"] - point) / (deviation * math.sqrt(2))))
                            prop = f"Over {point:g} {candidate['label']}"
                        expected_value = probability * self._american_decimal(price) - 1
                        offer = {**candidate, "prop": prop, "probability": round(probability * 100, 1), "sportsbook": bookmaker.get("title"), "odds": f"{int(price):+d}", "expectedValue": round(expected_value * 100, 1)}
                        if best is None or offer["expectedValue"] > best["expectedValue"]:
                            best = offer
            if best and best["expectedValue"] > 0:
                priced.append(best)
        priced.sort(key=lambda item: (item["expectedValue"], item["probability"]), reverse=True)
        selected, used_players = [], set()
        for offer in priced:
            if offer["playerId"] in used_players:
                continue
            selected.append(offer)
            used_players.add(offer["playerId"])
            if len(selected) == 3:
                break
        return selected, None if selected else "No positive-value sportsbook props are available for this matchup right now."

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
                if position not in {"QB", "RB", "WR", "TE"}:
                    continue
                aliases = {
                    "passingYards": ["passingYards"], "completions": ["completions", "passingCompletions"],
                    "rushingYards": ["rushingYards"], "receivingYards": ["receivingYards"],
                    "receptions": ["receptions", "receivingReceptions"],
                    "passingTouchdowns": ["passingTouchdowns"], "rushingTouchdowns": ["rushingTouchdowns"], "receivingTouchdowns": ["receivingTouchdowns"],
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

    def _json(self, url, timeout=20):
        request = Request(url, headers={"User-Agent": "LittleBrotherNFL/1.0", "Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _text(self, url):
        request = Request(url, headers={"User-Agent": "LittleBrotherNFL/1.0"})
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")

    def _team_metrics(self, season=2025):
        season = int(season or 2025)
        cached = self._metrics_cache.get(season)
        cached_at = self._metrics_cached_at.get(season, 0)
        if cached and (season == 2025 or time.time() - cached_at < 21600):
            return cached

        rows = [row for row in csv.DictReader(StringIO(self._text(self.TEAM_STATS_URL.format(season=season)))) if row.get("season_type") == "REG"]
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

        self._metrics_cache[season] = metrics
        self._metrics_cached_at[season] = time.time()
        return metrics

    def _metric_for(self, abbreviation, season=2025):
        key = self.ABBR_TO_DATA.get(abbreviation, abbreviation)
        return self._team_metrics(season).get(key, {})

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
            pattern = r'<h2[^>]*>\s*(\d+)\.\s*<a[^>]*?/name/([^/\"]+)[^>]*>(.*?)</a>.*?</h2>'
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

    def _espn_2025_power_rankings(self):
        if self._power_rankings_2025_cache:
            return self._power_rankings_2025_cache
        try:
            article = self._json("https://content.core.api.espn.com/v1/sports/news/47446909")["headlines"][0]
            rankings = {}
            pattern = r'<h2[^>]*>\s*(\d+)\.\s*<a[^>]*?/name/([^/\"]+)[^>]*>(.*?)</a>.*?</h2>'
            for rank, abbreviation, team_name in re.findall(pattern, article.get("story", ""), re.I | re.S):
                rankings[abbreviation.upper()] = {"rank": int(rank), "team": html.unescape(re.sub(r"<[^>]+>", "", team_name)).strip()}
            if len(rankings) != 32:
                raise ValueError(f"Expected 32 fixed 2025 ESPN rankings, found {len(rankings)}")
            self._power_rankings_2025_cache = {
                "rankings": rankings, "title": article.get("headline"), "published": article.get("published"),
                "link": "https://www.espn.com/nfl/story/_/id/47446909/nfl-week-18-power-rankings-poll-32-teams-2025-season-lessons",
            }
        except Exception as error:
            print(f"ESPN 2025 power rankings error: {error}")
            self._power_rankings_2025_cache = {"rankings": {}, "title": None, "published": None, "link": None}
        return self._power_rankings_2025_cache

    def weekly_matchups(self):
        scoreboard = self._json(f"{self.ESPN_SITE}/scoreboard?limit=50")
        season = scoreboard.get("season", {}).get("year")
        season_type = scoreboard.get("season", {}).get("type")
        week = scoreboard.get("week", {}).get("number")
        events = scoreboard.get("events", [])
        self._backfill_completed_week_archives(season, season_type, week)
        completed = [
            event for event in events
            if event.get("status", {}).get("type", {}).get("completed") is True
            or event.get("status", {}).get("type", {}).get("state") == "post"
        ]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(self._settle_completed_event, event) for event in completed]
            for event, future in zip(completed, futures):
                try:
                    future.result()
                except Exception as error:
                    print(f"Could not grade final matchup {event.get('id')}: {error}")
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
            calculated_home_probability = self._probability(home_probability)
            calculated_away_probability = self._probability(1 - home_probability)
            if game_state == "post":
                saved = self._saved_pregame_prediction(event.get("id")) or {}
                forecast = saved.get("prediction") or {}
                projected_winner = forecast.get("winner") or projected_winner
                home_probability = self._number(forecast.get("homeWinProbability")) or calculated_home_probability
                away_probability = self._number(forecast.get("awayWinProbability")) or calculated_away_probability
            else:
                home_probability = calculated_home_probability
                away_probability = calculated_away_probability
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
                "homeWinProbability": home_probability,
                "awayWinProbability": away_probability,
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

    def matchup_detail(self, event_id, include_sportsbook=True):
        saved_final = self._saved_final_detail(event_id)
        if saved_final:
            return saved_final
        summary = self._json(f"{self.ESPN_SITE}/summary?event={event_id}")
        competition = (summary.get("header", {}).get("competitions") or [{}])[0]
        competitors = {item.get("homeAway"): item for item in competition.get("competitors", [])}
        current_stats_season = (summary.get("header", {}).get("season") or {}).get("year") or time.gmtime().tm_year
        teams = {}
        for side in ("away", "home"):
            source = competitors.get(side, {}).get("team", {})
            teams[side] = self._team_summary(source)

        power_rankings = self._espn_power_rankings()
        power_rankings_2025 = self._espn_2025_power_rankings()
        for team in teams.values():
            ranking = power_rankings["rankings"].get(team.get("abbreviation"), {})
            fixed_ranking = power_rankings_2025["rankings"].get(team.get("abbreviation"), {})
            team["powerRank"] = ranking.get("rank")
            team["currentPowerRank"] = ranking.get("rank")
            team["powerRank2025"] = fixed_ranking.get("rank")

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
            team["statistics2025"] = self._metric_for(team.get("abbreviation"), 2025)
            team["currentStatistics"] = self._metric_for(team.get("abbreviation"), current_stats_season)
            team["statistics"] = team["currentStatistics"] or team["statistics2025"]
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
            saved_players = (saved_pregame or {}).get("playerProjections", [])
            projections_are_current = saved_players and all(item.get("modelVersion") == 3 for item in saved_players)
            if projections_are_current:
                player_projections = saved_players
            else:
                self._player_weekly_stats(current_stats_season)
                projection_jobs = []
                with ThreadPoolExecutor(max_workers=8) as executor:
                    for side, opponent_side in (("away", "home"), ("home", "away")):
                        for player in teams[side].get("depthChart", {}).get("offense", []):
                            if player.get("position") in {"QB", "RB", "WR", "TE"}:
                                projection_jobs.append(executor.submit(self._skill_projection, player, teams[opponent_side].get("abbreviation"), teams[side].get("abbreviation")))
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
        snapshot_players = (saved_pregame or {}).get("playerProjections", [])
        model_prop_candidates = self._top_prop_candidates(snapshot_players)
        saved_sportsbook_status = (saved_pregame or {}).get("sportsbookTopPropsStatus")
        saved_sportsbook_is_final = saved_sportsbook_status is None or saved_sportsbook_status.startswith("No positive-value")
        if live_state == "pre" and saved_pregame and saved_pregame.get("sportsbookTopPropsCheckedAt") and saved_sportsbook_is_final and saved_pregame.get("sportsbookModelVersion") == 2:
            top_props = saved_pregame.get("sportsbookTopProps", [])
            top_props_status = saved_pregame.get("sportsbookTopPropsStatus")
        elif live_state == "pre" and include_sportsbook:
            top_props, top_props_status = self._sportsbook_value_props(teams, model_prop_candidates)
            sportsbook_result_is_final = top_props_status is None or top_props_status.startswith("No positive-value")
            if self.predictions_collection is not None and saved_pregame and sportsbook_result_is_final:
                self.predictions_collection.update_one(
                    {"gameId": str(event_id), "sport": "nfl", "phase": "pregame"},
                    {"$set": {"sportsbookTopProps": top_props, "sportsbookTopPropsStatus": top_props_status, "sportsbookTopPropsCheckedAt": int(time.time()), "sportsbookModelVersion": 2}},
                )
        elif live_state == "pre":
            top_props, top_props_status = [], "Open this matchup on the website to check sportsbook value props."
        else:
            top_props, top_props_status = [], None
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
            "topProps": top_props,
            "topPropsStatus": top_props_status,
            "gameState": live_state,
            "weather": (summary.get("gameInfo") or {}).get("weather") or summary.get("weather"),
            "articles": [
                {"headline": article.get("headline"), "description": article.get("description"), "link": article.get("links", {}).get("web", {}).get("href")}
                for article in relevant_articles[:6]
            ],
            "rankingsSeason": 2025,
            "currentRankingsSeason": current_stats_season,
            "accuracy": self.accuracy((summary.get("header", {}).get("season") or {}).get("year")),
            "powerRankings": {
                "title": power_rankings.get("title"),
                "published": power_rankings.get("published"),
                "link": power_rankings.get("link"),
            },
            "powerRankings2025": {
                "title": power_rankings_2025.get("title"),
                "published": power_rankings_2025.get("published"),
                "link": power_rankings_2025.get("link"),
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
        expected_touchdowns = 0.0
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
                per_game_stats = []
                if games_played:
                    if category_name == "passing" and position == "QB" and self._number(stats.get("passingYards")):
                        per_game_stats.append({"name": "Passing Yards Per Game", "value": round(self._number(stats["passingYards"]) / games_played, 1)})
                    elif category_name == "rushing" and position in {"QB", "RB", "FB"}:
                        if self._number(stats.get("rushingYards")):
                            per_game_stats.append({"name": "Rushing Yards Per Game", "value": round(self._number(stats["rushingYards"]) / games_played, 1)})
                        if self._number(stats.get("rushingAttempts")):
                            per_game_stats.append({"name": "Rushing Attempts Per Game", "value": round(self._number(stats["rushingAttempts"]) / games_played, 1)})
                    elif category_name == "receiving" and position in {"RB", "FB", "WR", "TE"}:
                        if self._number(stats.get("receivingYards")):
                            per_game_stats.append({"name": "Receiving Yards Per Game", "value": round(self._number(stats["receivingYards"]) / games_played, 1)})
                        if self._number(stats.get("receptions")):
                            per_game_stats.append({"name": "Receptions Per Game", "value": round(self._number(stats["receptions"]) / games_played, 1)})
                if per_game_stats:
                    insertion_index = 1 if useful and useful[0]["name"] == "Games Played" else 0
                    useful[insertion_index:insertion_index] = per_game_stats
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
                        expected_touchdowns += self._number(stats.get("passingTouchdowns")) / games * opponent.get("defensePassYpg", 220) / 220
                    if row_position == "RB" and stats.get("rushingYards"):
                        projected["rushingYards"] = round(self._number(stats["rushingYards"]) / games * opponent.get("defenseRushYpg", 110) / 110, 1)
                        expected_touchdowns += self._number(stats.get("rushingTouchdowns")) / games * opponent.get("defenseRushYpg", 110) / 110
                    if row_position in ("WR", "TE", "RB") and stats.get("receivingYards"):
                        projected["receivingYards"] = round(self._number(stats["receivingYards"]) / games * opponent.get("defensePassYpg", 220) / 220, 1)
                        if row_position in {"WR", "RB", "TE"}:
                            expected_touchdowns += self._number(stats.get("receivingTouchdowns")) / games * opponent.get("defensePassYpg", 220) / 220
                    if row_position in {"WR", "TE"} and stats.get("receptions"):
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
        if position in {"RB", "WR", "TE"}:
            touchdown_context = self._touchdown_context(athlete.get("displayName") or athlete.get("fullName"), team_abbreviation, opponent_abbreviation, position)
            expected_touchdowns = self._adjust_touchdown_rate(expected_touchdowns, touchdown_context)
        if position in {"QB", "RB", "WR", "TE"}:
            projected["touchdownProbability"] = round((1 - math.exp(-max(0, expected_touchdowns))) * 100, 1)
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

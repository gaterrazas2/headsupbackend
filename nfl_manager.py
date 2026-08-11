import csv
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from urllib.request import Request, urlopen


class NFLManager:
    ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
    ESPN_WEB = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl"
    TEAM_STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_2025.csv"
    ABBR_TO_DATA = {"LAR": "LA", "WSH": "WAS"}
    _metrics_cache = None
    _metrics_cached_at = 0

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
        offense = defaultdict(lambda: {"games": 0, "pass": 0.0, "rush": 0.0})
        defense = defaultdict(lambda: {"games": 0, "pass": 0.0, "rush": 0.0})
        for row in rows:
            team = row.get("team")
            opponent = row.get("opponent_team")
            passing = float(row.get("passing_yards") or 0)
            rushing = float(row.get("rushing_yards") or 0)
            offense[team]["games"] += 1
            offense[team]["pass"] += passing
            offense[team]["rush"] += rushing
            defense[opponent]["games"] += 1
            defense[opponent]["pass"] += passing
            defense[opponent]["rush"] += rushing

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
            }

        rank_fields = {
            "offensePassRank": ("offensePassYpg", True),
            "offenseRushRank": ("offenseRushYpg", True),
            "offenseRank": ("offenseTotalYpg", True),
            "defensePassRank": ("defensePassYpg", False),
            "defenseRushRank": ("defenseRushYpg", False),
            "defenseRank": ("defenseTotalYpg", False),
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

    def weekly_matchups(self):
        scoreboard = self._json(f"{self.ESPN_SITE}/scoreboard?limit=50")
        season = scoreboard.get("season", {}).get("year")
        season_type = scoreboard.get("season", {}).get("type")
        week = scoreboard.get("week", {}).get("number")
        events = scoreboard.get("events", [])
        if events and all(
            (event.get("status", {}).get("type", {}).get("completed") is True)
            for event in events
        ):
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

            if favorite_id == home_team.get("id"):
                projected_winner = home_team.get("displayName")
            elif favorite_id == away_team.get("id"):
                projected_winner = away_team.get("displayName")
            else:
                home_metrics = self._metric_for(home_team.get("abbreviation"))
                away_metrics = self._metric_for(away_team.get("abbreviation"))
                home_score = home_metrics.get("offenseRank", 16) + home_metrics.get("defenseRank", 16) - 1
                away_score = away_metrics.get("offenseRank", 16) + away_metrics.get("defenseRank", 16)
                projected_winner = home_team.get("displayName") if home_score <= away_score else away_team.get("displayName")

            venue = competition.get("venue", {})
            address = venue.get("address", {})
            matchups.append({
                "id": event.get("id"),
                "name": event.get("name"),
                "date": event.get("date"),
                "status": event.get("status", {}).get("type", {}).get("detail", "Scheduled"),
                "home": self._team_summary(home_team),
                "away": self._team_summary(away_team),
                "projectedWinner": projected_winner,
                "spread": odds.get("details") or "Not available",
                "venue": venue.get("fullName", "Venue TBD"),
                "location": ", ".join(filter(None, [address.get("city"), address.get("state")])),
            })
        return {
            "season": scoreboard.get("season", {}).get("year"),
            "seasonType": scoreboard.get("season", {}).get("type"),
            "week": scoreboard.get("week", {}).get("number"),
            "matchups": matchups,
            "rankingsSeason": 2025,
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
                "shape": "triangle" if abbreviation == "C" else "circle",
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

        injuries = {}
        unavailable_by_team = defaultdict(set)
        injury_groups = summary.get("injuries", {})
        if isinstance(injury_groups, dict):
            injury_groups = list(injury_groups.values())
        for group in injury_groups or []:
            for injury in group.get("injuries", []):
                athlete = injury.get("athlete", {})
                injuries[str(athlete.get("id"))] = {
                    "status": injury.get("status"),
                    "type": injury.get("details", {}).get("type"),
                    "detail": injury.get("details", {}).get("detail"),
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
        articles = summary.get("news") or []
        if isinstance(articles, dict):
            articles = articles.get("articles", [])
        return {
            "id": event_id,
            "teams": teams,
            "odds": {
                "details": pick.get("details", "Not available"),
                "overUnder": pick.get("overUnder"),
                "homeMoneyline": pick.get("homeTeamOdds", {}).get("moneyLine"),
                "awayMoneyline": pick.get("awayTeamOdds", {}).get("moneyLine"),
            },
            "articles": [
                {"headline": article.get("headline"), "description": article.get("description"), "link": article.get("links", {}).get("web", {}).get("href")}
                for article in articles[:8]
            ],
            "rankingsSeason": 2025,
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

    def player_detail(self, athlete_id, opponent_abbreviation=None, position=None, team_abbreviation=None):
        data = self._json(f"{self.ESPN_WEB}/athletes/{athlete_id}/stats?region=us&lang=en&contentorigin=espn")
        categories = []
        projected = {}
        opponent = self._metric_for(opponent_abbreviation) if opponent_abbreviation else {}
        position = (position or "").upper()
        defensive_line = position in {"DE", "LDE", "RDE", "LE", "RE", "DT", "LDT", "RDT", "NT", "DL"} or position.endswith(("DE", "DT"))
        defensive_back = position in {"CB", "LCB", "RCB", "NB", "DB", "S", "FS", "SS"} or position.endswith(("CB", "S"))
        linebacker = position in {"LB", "ILB", "OLB", "MLB", "LILB", "RILB", "WLB", "SLB"} or position.endswith("LB")
        allowed_categories = {
            "QB": {"passing", "rushing"},
            "RB": {"rushing", "receiving"},
            "FB": {"rushing", "receiving"},
            "WR": {"receiving", "rushing"},
            "TE": {"receiving"},
            "K": {"kicking"},
            "PK": {"kicking"},
            "P": {"punting"},
        }
        if position == "C":
            categories.append({
                "name": "Pass Protection",
                "season": 2025,
                "stats": [
                    {"name": "Team Sacks Allowed", "value": self._team_sacks_allowed(team_abbreviation)},
                ],
            })
        allowed = {"defensive"} if defensive_line or defensive_back or linebacker else allowed_categories.get(position, set())
        for category in data.get("categories", []):
            category_name = (category.get("name") or category.get("displayName") or "").lower()
            if category_name not in allowed:
                continue
            rows = category.get("statistics", [])
            if not rows:
                continue
            row = max(rows, key=lambda item: item.get("season", {}).get("year", 0))
            stats = dict(zip(category.get("names", []), row.get("stats", [])))
            useful = [
                {"name": display, "value": value}
                for name, display, value in zip(category.get("names", []), category.get("displayNames", []), row.get("stats", []))
                if value not in ("0", "0.0", "--", None)
                and not (defensive_line and name not in {"gamesPlayed", "totalTackles", "soloTackles", "assistTackles", "sacks", "stuffs", "forcedFumbles", "fumbleRecoveries"})
            ]
            if defensive_line:
                for stat in useful:
                    if stat["name"].lower() == "stuffs":
                        stat["name"] = "Tackles for Loss (ESPN Stuffs)"
                useful.append({"name": "Pressures", "value": "Not published by ESPN"})
            categories.append({"name": category.get("displayName"), "season": row.get("season", {}).get("year"), "stats": useful[:14]})
            games = self._number(stats.get("gamesPlayed")) or 1
            row_position = position or row.get("position")
            if row_position == "QB" and stats.get("passingYards"):
                baseline = self._number(stats["passingYards"]) / games
                projected["passingYards"] = round(baseline * opponent.get("defensePassYpg", 220) / 220, 1)
            if row_position == "RB" and stats.get("rushingYards"):
                baseline = self._number(stats["rushingYards"]) / games
                projected["rushingYards"] = round(baseline * opponent.get("defenseRushYpg", 110) / 110, 1)
            if row_position in ("WR", "TE", "RB") and stats.get("receivingYards"):
                baseline = self._number(stats["receivingYards"]) / games
                projected["receivingYards"] = round(baseline * opponent.get("defensePassYpg", 220) / 220, 1)

        return {
            "categories": categories,
            "projected": projected,
            "newsLink": f"https://www.espn.com/nfl/player/news/_/id/{athlete_id}",
            "projectionNote": "Projection uses the player's latest season per-game production adjusted by the opponent's 2025 defense.",
        }

    def _number(self, value):
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

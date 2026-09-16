import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from bson.objectid import ObjectId
from bson.errors import InvalidId


class FantasyManager:
    LEAGUES = {
        "cs": {"leagueId": 1082498796},
        "discord": {"leagueId": 788168317},
        "ai-cheat": {"leagueId": 94545708},
        "league-68092989": {"leagueId": 68092989},
    }
    BENCH_SLOT = 20
    IR_SLOT = 21
    LINEUP_POSITION_NAMES = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K", 23: "FLEX"}
    PLAYER_POSITION_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}

    def __init__(self, recommendations_collection, nfl_manager=None):
        self.recommendations = recommendations_collection
        self.nfl = nfl_manager
        self.season = int(os.getenv("ESPN_FANTASY_SEASON", datetime.now().year))

    def configured(self):
        return bool(os.getenv("ESPN_S2") and os.getenv("ESPN_SWID"))

    def _headers(self, extra=None):
        headers = {
            "Cookie": f'espn_s2={os.environ["ESPN_S2"]}; SWID={os.environ["ESPN_SWID"]}',
            "Accept": "application/json",
            "User-Agent": "LittleBrotherFantasyManager/1.0",
        }
        headers.update(extra or {})
        return headers

    def _league_url(self, league_id, write=False):
        host = "lm-api-writes" if write else "lm-api-reads"
        return f"https://{host}.fantasy.espn.com/apis/v3/games/ffl/seasons/{self.season}/segments/0/leagues/{league_id}"

    def _request_json(self, url, method="GET", payload=None, headers=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(url, data=body, method=method, headers=self._headers(headers))
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ValueError(f"ESPN rejected the request ({error.code}): {detail[:300]}") from error

    def _owner_id(self):
        return os.environ["ESPN_SWID"].strip("{} ").lower()

    def _owned_team(self, league):
        owner_id = self._owner_id()
        for team in league.get("teams", []):
            owners = [team.get("primaryOwner"), *(team.get("owners") or [])]
            if any(str(owner or "").strip("{} ").lower() == owner_id for owner in owners):
                return team
        raise ValueError("No team in this league belongs to the connected ESPN account")

    @staticmethod
    def _team_name(team):
        return team.get("name") or f'{team.get("location", "")} {team.get("nickname", "")}'.strip() or "My Team"

    def league_options(self):
        options = []
        for key, config in self.LEAGUES.items():
            name = f'League {config["leagueId"]}'
            try:
                league = self._request_json(f'{self._league_url(config["leagueId"])}?view=mTeam')
                team = self._owned_team(league)
                name = self._team_name(team)
            except Exception as error:
                print(f'Could not load ESPN team name for {config["leagueId"]}: {error}')
            options.append({"key": key, "leagueId": config["leagueId"], "name": name})
        return options

    def _projection(self, player, week):
        projected = [
            stat.get("appliedTotal")
            for stat in player.get("stats", [])
            if stat.get("statSourceId") == 1 and stat.get("scoringPeriodId") == week
        ]
        if not projected:
            projected = [
                stat.get("appliedTotal")
                for stat in player.get("stats", [])
                if stat.get("statSourceId") == 1 and stat.get("seasonId") == self.season
            ]
        return round(float(projected[-1] or 0), 2) if projected else 0.0

    def _player(self, entry, week):
        player = (entry.get("playerPoolEntry") or {}).get("player", {})
        return {
            "id": player.get("id"),
            "name": player.get("fullName", "Unknown player"),
            "positionId": player.get("defaultPositionId"),
            "position": self.PLAYER_POSITION_NAMES.get(player.get("defaultPositionId"), "FLEX"),
            "proTeamId": player.get("proTeamId"),
            "eligibleSlots": player.get("eligibleSlots", []),
            "lineupSlotId": entry.get("lineupSlotId", self.BENCH_SLOT),
            "projectedPoints": self._projection(player, week),
            "injuryStatus": player.get("injuryStatus", "ACTIVE"),
        }

    def _apply_matchups(self, players, week):
        """Blend ESPN projections with the defense each player actually faces."""
        if not self.nfl:
            for player in players:
                player["matchupAdjustedPoints"] = player["projectedPoints"]
            return players
        try:
            scoreboard = self.nfl._json(f"{self.nfl.ESPN_SITE}/scoreboard?dates={self.season}&seasontype=2&week={week}&limit=50")
            opponents = {}
            for event in scoreboard.get("events", []):
                competition = (event.get("competitions") or [{}])[0]
                competitors = competition.get("competitors") or []
                if len(competitors) != 2:
                    continue
                left, right = competitors
                opponents[str(left.get("team", {}).get("id"))] = right.get("team", {})
                opponents[str(right.get("team", {}).get("id"))] = left.get("team", {})
            for player in players:
                opponent = opponents.get(str(player.get("proTeamId")))
                projection = player.get("projectedPoints", 0)
                factor, rank, matchup_type = 1.0, None, None
                if opponent:
                    metrics = self.nfl._metric_for(opponent.get("abbreviation"), self.season)
                    position = player.get("position")
                    if position in {"QB", "WR", "TE"}:
                        rank, matchup_type = metrics.get("defensePassRank"), "pass defense"
                    elif position == "RB":
                        rank, matchup_type = metrics.get("defenseRushRank"), "run defense"
                    elif position == "D/ST":
                        rank, matchup_type = metrics.get("offenseRank"), "opposing offense"
                    if rank:
                        factor = max(0.80, min(1.20, 1 + (rank - 16.5) * 0.012))
                injury = str(player.get("injuryStatus") or "").upper()
                injury_factor = 0.0 if injury in {"OUT", "INJURY_RESERVE", "SUSPENSION"} else 0.65 if injury == "DOUBTFUL" else 0.90 if injury == "QUESTIONABLE" else 1.0
                player["matchupAdjustedPoints"] = round(projection * factor * injury_factor, 2)
                player["matchup"] = {
                    "opponent": opponent.get("abbreviation") if opponent else "BYE/TBD",
                    "type": matchup_type,
                    "rank": rank,
                    "factor": round(factor, 3),
                }
            return players
        except Exception as error:
            print(f"Could not apply fantasy matchup adjustments: {error}")
            for player in players:
                player["matchupAdjustedPoints"] = player["projectedPoints"]
            return players

    def _best_lineup(self, players, slot_counts):
        slots = []
        for slot_text, count in slot_counts.items():
            slot = int(slot_text)
            if slot not in (self.BENCH_SLOT, self.IR_SLOT):
                slots.extend([slot] * int(count))

        slots.sort(key=lambda slot: sum(slot in player["eligibleSlots"] for player in players))
        best_score = -1
        best_assignments = []

        def search(index, used, score, assignments):
            nonlocal best_score, best_assignments
            if index == len(slots):
                if score > best_score:
                    best_score = score
                    best_assignments = list(assignments)
                return
            slot = slots[index]
            candidates = [
                player for player in players
                if player["id"] not in used and slot in player["eligibleSlots"]
            ]
            candidates.sort(key=lambda player: player["matchupAdjustedPoints"], reverse=True)
            for player in candidates:
                search(
                    index + 1,
                    used | {player["id"]},
                    score + player["matchupAdjustedPoints"],
                    [*assignments, (player, slot)],
                )

        search(0, set(), 0, [])
        return best_assignments

    def _free_agents(self, league_id, week):
        fantasy_filter = {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                "filterSlotIds": {"value": list(self.LINEUP_POSITION_NAMES)},
                "limit": 100,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            }
        }
        data = self._request_json(
            f"{self._league_url(league_id)}?view=kona_player_info",
            headers={"X-Fantasy-Filter": json.dumps(fantasy_filter)},
        )
        agents = []
        for entry in data.get("players", []):
            player = entry.get("player", {})
            agents.append({
                "id": player.get("id"),
                "name": player.get("fullName", "Unknown player"),
                "positionId": player.get("defaultPositionId"),
                "position": self.PLAYER_POSITION_NAMES.get(player.get("defaultPositionId"), "FLEX"),
                "proTeamId": player.get("proTeamId"),
                "projectedPoints": self._projection(player, week),
                "status": entry.get("status", "FREEAGENT"),
                "injuryStatus": player.get("injuryStatus", "ACTIVE"),
            })
        return self._apply_matchups(agents, week)

    def team_roster(self, league_key):
        config = self.LEAGUES.get(league_key)
        if not config:
            raise ValueError("Unknown fantasy league")
        if not self.configured():
            raise ValueError("ESPN connection is not configured")
        league = self._request_json(
            f'{self._league_url(config["leagueId"])}?view=mTeam&view=mRoster&view=mSettings'
        )
        team = self._owned_team(league)
        week = max(int(league.get("scoringPeriodId") or 0), 1)
        players = self._apply_matchups([self._player(entry, week) for entry in (team.get("roster") or {}).get("entries", [])], week)
        for player in players:
            slot = player["lineupSlotId"]
            player["lineupSlot"] = self.LINEUP_POSITION_NAMES.get(slot, "IR" if slot == self.IR_SLOT else "Bench")
        return {
            "leagueKey": league_key,
            "teamName": self._team_name(team),
            "week": int(league.get("scoringPeriodId") or 0),
            "starters": [player for player in players if player["lineupSlotId"] not in (self.BENCH_SLOT, self.IR_SLOT)],
            "bench": [player for player in players if player["lineupSlotId"] in (self.BENCH_SLOT, self.IR_SLOT)],
        }

    def build_plan(self, league_key):
        config = self.LEAGUES.get(league_key)
        if not config:
            raise ValueError("Unknown fantasy league")
        if not self.configured():
            raise ValueError("ESPN connection is not configured")

        league = self._request_json(
            f'{self._league_url(config["leagueId"])}?view=mTeam&view=mRoster&view=mSettings'
        )
        team = self._owned_team(league)
        week = max(int(league.get("scoringPeriodId") or 0), 1)
        entries = (team.get("roster") or {}).get("entries", [])
        players = self._apply_matchups([self._player(entry, week) for entry in entries], week)
        team_name = self._team_name(team)

        if not players:
            return {
                "leagueKey": league_key,
                "leagueName": team_name,
                "teamName": team_name,
                "teamId": team["id"],
                "week": int(league.get("scoringPeriodId") or 0),
                "status": "pre_draft",
                "message": "Your roster is empty. Recommendations will be available after the draft.",
                "lineupMoves": [],
                "addDropMoves": [],
                "roster": [],
            }

        if not any(player["projectedPoints"] > 0 for player in players):
            return {
                "leagueKey": league_key,
                "leagueName": team_name,
                "teamName": team_name,
                "teamId": team["id"],
                "week": week,
                "status": "no_projections",
                "message": "ESPN has not published projections for this week yet. No moves were generated.",
                "lineupMoves": [],
                "addDropMoves": [],
                "roster": players,
            }

        slot_counts = (league.get("settings", {}).get("rosterSettings", {}).get("lineupSlotCounts", {}))
        assignments = self._best_lineup(players, slot_counts)
        desired_slots = {player["id"]: slot for player, slot in assignments}
        raw_lineup_moves = []
        for player in players:
            desired = desired_slots.get(player["id"], self.BENCH_SLOT)
            if desired != player["lineupSlotId"] and player["lineupSlotId"] != self.IR_SLOT:
                raw_lineup_moves.append({
                    "playerId": player["id"],
                    "player": player["name"],
                    "fromSlotId": player["lineupSlotId"],
                    "toSlotId": desired,
                    "projectedPoints": player["projectedPoints"],
                    "matchupAdjustedPoints": player["matchupAdjustedPoints"],
                    "matchup": player.get("matchup"),
                })

        lineup_moves = []
        used_player_ids = set()
        for incoming in [move for move in raw_lineup_moves if move["toSlotId"] not in (self.BENCH_SLOT, self.IR_SLOT)]:
            if incoming["playerId"] in used_player_ids:
                continue
            outgoing = next((
                move for move in raw_lineup_moves
                if move["playerId"] not in used_player_ids
                and move["fromSlotId"] == incoming["toSlotId"]
                and move["toSlotId"] in (self.BENCH_SLOT, self.IR_SLOT)
            ), None)
            items = [incoming, *([outgoing] if outgoing else [])]
            used_player_ids.update(move["playerId"] for move in items)
            rejection_key = "lineup:" + ":".join(str(move["playerId"]) for move in sorted(items, key=lambda move: str(move["playerId"])))
            lineup_moves.append({
                "moveId": rejection_key, "rejectionKey": rejection_key, "decision": "pending",
                "startPlayer": incoming["player"], "benchPlayer": outgoing.get("player") if outgoing else None,
                "projectedPoints": incoming["projectedPoints"], "matchupAdjustedPoints": incoming["matchupAdjustedPoints"],
                "matchup": incoming.get("matchup"), "items": items,
            })

        add_drop_moves = []
        try:
            agents = self._free_agents(config["leagueId"], week)
            bench = [p for p in players if p["lineupSlotId"] == self.BENCH_SLOT]
            upgrades = []
            for agent in agents:
                same_position = [p for p in bench if p["positionId"] == agent["positionId"]]
                if not same_position:
                    continue
                drop = min(same_position, key=lambda player: player["matchupAdjustedPoints"])
                improvement = agent["matchupAdjustedPoints"] - drop["matchupAdjustedPoints"]
                if improvement >= 3:
                    upgrades.append((improvement, agent, drop))
            if upgrades:
                improvement, add, drop = max(upgrades, key=lambda item: item[0])
                add_drop_moves.append({
                    "moveId": f'adddrop:{add["id"]}:{drop["id"]}', "rejectionKey": f'adddrop:{add["id"]}:{drop["id"]}', "decision": "pending",
                    "addPlayerId": add["id"], "addPlayer": add["name"],
                    "dropPlayerId": drop["id"], "dropPlayer": drop["name"],
                    "position": add["position"], "status": add["status"],
                    "improvement": round(improvement, 2),
                    "projectedPoints": add["projectedPoints"], "matchupAdjustedPoints": add["matchupAdjustedPoints"],
                    "matchup": add.get("matchup"),
                })
        except Exception as error:
            print(f"Could not calculate free-agent upgrades: {error}")

        rejected = {
            row.get("rejectionKey") for row in self.recommendations.find(
                {"recordType": "move_rejection", "leagueKey": league_key}, {"_id": 0, "rejectionKey": 1}
            )
        }
        lineup_moves = [move for move in lineup_moves if move["rejectionKey"] not in rejected]
        add_drop_moves = [move for move in add_drop_moves if move["rejectionKey"] not in rejected]
        return {
            "leagueKey": league_key,
            "leagueName": team_name,
            "teamName": team_name,
            "teamId": team["id"],
            "week": week,
            "status": "ready",
            "message": "Review every move before approving.",
            "lineupMoves": lineup_moves,
            "addDropMoves": add_drop_moves,
            "roster": players,
        }

    @staticmethod
    def _position_value(players, position, count):
        values = sorted(
            (player.get("matchupAdjustedPoints", 0) for player in players if player.get("position") == position),
            reverse=True,
        )
        return sum(values[:count])

    def trade_suggestions(self, league_key):
        config = self.LEAGUES.get(league_key)
        if not config:
            raise ValueError("Unknown fantasy league")
        league = self._request_json(f'{self._league_url(config["leagueId"])}?view=mTeam&view=mRoster&view=mSettings')
        own_team = self._owned_team(league)
        week = max(int(league.get("scoringPeriodId") or 0), 1)
        slot_counts = league.get("settings", {}).get("rosterSettings", {}).get("lineupSlotCounts", {})
        position_counts = {"QB": int(slot_counts.get("0", 1)), "RB": int(slot_counts.get("2", 2)), "WR": int(slot_counts.get("4", 2)), "TE": int(slot_counts.get("6", 1))}

        rosters = {}
        for team in league.get("teams", []):
            roster = [self._player(entry, week) for entry in (team.get("roster") or {}).get("entries", [])]
            rosters[team.get("id")] = self._apply_matchups(roster, week)
        own_roster = rosters.get(own_team.get("id"), [])
        eligible = {"QB", "RB", "WR", "TE"}
        own_candidates = [player for player in own_roster if player.get("position") in eligible and player.get("matchupAdjustedPoints", 0) >= 4 and player.get("injuryStatus") not in {"OUT", "INJURY_RESERVE"}]
        rejected = {
            row.get("rejectionKey") for row in self.recommendations.find(
                {"recordType": "trade_rejection", "leagueKey": league_key}, {"_id": 0, "rejectionKey": 1}
            )
        }
        suggestions = []
        for other_team in league.get("teams", []):
            if other_team.get("id") == own_team.get("id"):
                continue
            other_roster = rosters.get(other_team.get("id"), [])
            other_candidates = [player for player in other_roster if player.get("position") in eligible and player.get("matchupAdjustedPoints", 0) >= 4 and player.get("injuryStatus") not in {"OUT", "INJURY_RESERVE"}]
            best_for_team = None
            for give in own_candidates:
                for receive in other_candidates:
                    if give["position"] == receive["position"]:
                        continue
                    give_value = give["matchupAdjustedPoints"]
                    receive_value = receive["matchupAdjustedPoints"]
                    value_ratio = receive_value / max(give_value, 0.1)
                    if not 0.88 <= value_ratio <= 1.12:
                        continue
                    own_before = sum(self._position_value(own_roster, pos, count) for pos, count in position_counts.items())
                    other_before = sum(self._position_value(other_roster, pos, count) for pos, count in position_counts.items())
                    own_after_roster = [player for player in own_roster if player["id"] != give["id"]] + [receive]
                    other_after_roster = [player for player in other_roster if player["id"] != receive["id"]] + [give]
                    own_gain = sum(self._position_value(own_after_roster, pos, count) for pos, count in position_counts.items()) - own_before
                    other_gain = sum(self._position_value(other_after_roster, pos, count) for pos, count in position_counts.items()) - other_before
                    if own_gain < 0.25 or other_gain < -0.75:
                        continue
                    rejection_key = f'trade:{give["id"]}:{receive["id"]}:{other_team.get("id")}'
                    if rejection_key in rejected:
                        continue
                    score = own_gain + other_gain - abs(1 - value_ratio) * 5
                    proposal = {
                        "moveId": rejection_key, "rejectionKey": rejection_key, "decision": "pending",
                        "targetTeamId": other_team.get("id"), "targetTeam": self._team_name(other_team),
                        "givePlayerId": give["id"], "givePlayer": give["name"], "givePosition": give["position"], "giveValue": give_value,
                        "receivePlayerId": receive["id"], "receivePlayer": receive["name"], "receivePosition": receive["position"], "receiveValue": receive_value,
                        "yourGain": round(own_gain, 2), "theirGain": round(other_gain, 2), "fairness": round(min(value_ratio, 1 / value_ratio) * 100),
                        "justification": f'{self._team_name(other_team)} gets {give["name"]} to strengthen {give["position"]}, while you fill a need at {receive["position"]}. The player values are within {abs(1 - value_ratio) * 100:.0f}% of each other and their modeled lineup impact is {other_gain:+.1f} points, so this is a balanced offer rather than a fleece.',
                    }
                    if best_for_team is None or score > best_for_team[0]:
                        best_for_team = (score, proposal)
            if best_for_team:
                suggestions.append(best_for_team)
        suggestions = [item[1] for item in sorted(suggestions, key=lambda item: item[0], reverse=True)[:5]]
        document = {
            "recordType": "trade_plan", "leagueKey": league_key, "teamId": own_team.get("id"),
            "teamName": self._team_name(own_team), "week": week, "trades": suggestions,
            "approvalStatus": "pending", "createdAt": datetime.now(timezone.utc),
        }
        result = self.recommendations.insert_one(document)
        return {**document, "id": str(result.inserted_id), "createdAt": document["createdAt"].isoformat()}

    def review_trade(self, plan_id, move_id, decision):
        plan = self._pending_plan(plan_id)
        if plan.get("recordType") != "trade_plan" or decision not in {"approve", "deny"}:
            raise ValueError("Invalid trade decision")
        trade = next((item for item in plan.get("trades", []) if item.get("moveId") == move_id), None)
        if not trade or trade.get("decision") != "pending":
            raise ValueError("This trade is missing or has already been reviewed")
        if decision == "approve":
            config = self.LEAGUES[plan["leagueKey"]]
            payload = {
                "isLeagueManager": False, "isActingAsTeamOwner": False,
                "teamId": plan["teamId"], "scoringPeriodId": plan["week"],
                "type": "TRADE_PROPOSAL", "executionType": "PROPOSE",
                "comment": trade["justification"],
                "items": [
                    {"playerId": trade["givePlayerId"], "type": "DROP", "fromTeamId": plan["teamId"], "toTeamId": trade["targetTeamId"]},
                    {"playerId": trade["receivePlayerId"], "type": "ADD", "fromTeamId": trade["targetTeamId"], "toTeamId": plan["teamId"]},
                ],
            }
            self._request_json(f'{self._league_url(config["leagueId"], write=True)}/transactions/', method="POST", payload=payload, headers={"Content-Type": "application/json", "X-Fantasy-Platform": "kona-PROD"})
        else:
            self.recommendations.update_one(
                {"recordType": "trade_rejection", "leagueKey": plan["leagueKey"], "rejectionKey": trade["rejectionKey"]},
                {"$set": {"recordType": "trade_rejection", "leagueKey": plan["leagueKey"], "rejectionKey": trade["rejectionKey"], "createdAt": datetime.now(timezone.utc)}}, upsert=True,
            )
        trade["decision"] = "approved" if decision == "approve" else "denied"
        remaining = [item for item in plan.get("trades", []) if item.get("moveId") != move_id and item.get("decision") == "pending"]
        status = "pending" if remaining else "reviewed"
        self.recommendations.update_one({"_id": plan["_id"]}, {"$set": {"trades": plan["trades"], "approvalStatus": status, "reviewedAt": datetime.now(timezone.utc)}})
        return {"status": trade["decision"], "moveId": move_id, "planStatus": status}

    def save_plan(self, plan):
        document = {
            **plan,
            "approvalStatus": "pending",
            "createdAt": datetime.now(timezone.utc),
        }
        result = self.recommendations.insert_one(document)
        return {**plan, "id": str(result.inserted_id), "approvalStatus": "pending"}

    def _pending_plan(self, plan_id):
        try:
            object_id = ObjectId(plan_id)
        except (InvalidId, TypeError) as error:
            raise ValueError("Invalid recommendation") from error
        plan = self.recommendations.find_one({"_id": object_id, "approvalStatus": "pending"})
        if not plan:
            raise ValueError("This recommendation is missing or has already been reviewed")
        return plan

    def deny_plan(self, plan_id):
        plan = self._pending_plan(plan_id)
        self.recommendations.update_one(
            {"_id": plan["_id"], "approvalStatus": "pending"},
            {"$set": {"approvalStatus": "denied", "reviewedAt": datetime.now(timezone.utc)}},
        )
        return {"status": "denied"}

    def _execute_lineup_items(self, plan, league_id, moves):
        if not moves:
            return
        payload = {
            "isLeagueManager": False,
            "teamId": plan["teamId"],
            "scoringPeriodId": plan["week"],
            "type": "ROSTER",
            "executionType": "EXECUTE",
            "items": [
                {
                    "playerId": move["playerId"],
                    "type": "LINEUP",
                    "fromLineupSlotId": move["fromSlotId"],
                    "toLineupSlotId": move["toSlotId"],
                }
                for move in moves
            ],
        }
        self._request_json(
            f"{self._league_url(league_id, write=True)}/transactions/",
            method="POST",
            payload=payload,
            headers={"Content-Type": "application/json", "X-Fantasy-Platform": "kona-PROD"},
        )

    def _execute_add_drop_moves(self, plan, league_id, moves):
        for move in moves:
            transaction_type = "WAIVER" if move.get("status") == "WAIVERS" else "FREEAGENT"
            payload = {
                "isLeagueManager": False,
                "teamId": plan["teamId"],
                "scoringPeriodId": plan["week"],
                "type": transaction_type,
                "executionType": "EXECUTE",
                "bidAmount": 0,
                "items": [
                    {"playerId": move["addPlayerId"], "type": "ADD", "toTeamId": plan["teamId"]},
                    {"playerId": move["dropPlayerId"], "type": "DROP", "fromTeamId": plan["teamId"]},
                ],
            }
            self._request_json(
                f"{self._league_url(league_id, write=True)}/transactions/",
                method="POST",
                payload=payload,
                headers={"Content-Type": "application/json", "X-Fantasy-Platform": "kona-PROD"},
            )

    def review_move(self, plan_id, move_id, decision):
        if decision not in {"approve", "deny"}:
            raise ValueError("Decision must be approve or deny")
        plan = self._pending_plan(plan_id)
        config = self.LEAGUES.get(plan.get("leagueKey"))
        if not config:
            raise ValueError("Unknown fantasy league")
        move_type = None
        move = None
        for field, kind in (("lineupMoves", "lineup"), ("addDropMoves", "adddrop")):
            move = next((item for item in plan.get(field, []) if item.get("moveId") == move_id), None)
            if move:
                move_type = kind
                break
        if not move or move.get("decision") != "pending":
            raise ValueError("This move is missing or has already been reviewed")

        if decision == "approve":
            if move_type == "lineup":
                self._execute_lineup_items(plan, config["leagueId"], move.get("items", []))
            else:
                self._execute_add_drop_moves(plan, config["leagueId"], [move])
        else:
            self.recommendations.update_one(
                {"recordType": "move_rejection", "leagueKey": plan["leagueKey"], "rejectionKey": move["rejectionKey"]},
                {"$set": {"recordType": "move_rejection", "leagueKey": plan["leagueKey"], "rejectionKey": move["rejectionKey"], "createdAt": datetime.now(timezone.utc)}},
                upsert=True,
            )

        move["decision"] = "approved" if decision == "approve" else "denied"
        remaining = [
            item for field in ("lineupMoves", "addDropMoves")
            for item in plan.get(field, []) if item.get("moveId") != move_id and item.get("decision") == "pending"
        ]
        status = "pending" if remaining else "reviewed"
        self.recommendations.update_one(
            {"_id": plan["_id"]},
            {"$set": {"lineupMoves": plan.get("lineupMoves", []), "addDropMoves": plan.get("addDropMoves", []), "approvalStatus": status, "reviewedAt": datetime.now(timezone.utc)}},
        )
        return {"status": move["decision"], "moveId": move_id, "planStatus": status}

    def approve_plan(self, plan_id):
        plan = self._pending_plan(plan_id)
        config = self.LEAGUES.get(plan.get("leagueKey"))
        if not config:
            raise ValueError("Unknown fantasy league")
        if plan.get("status") != "ready":
            raise ValueError("This league does not have actionable recommendations yet")
        if not plan.get("lineupMoves") and not plan.get("addDropMoves"):
            raise ValueError("There are no changes to execute")

        self._execute_add_drop_moves(plan, config["leagueId"], plan.get("addDropMoves", []))
        lineup_items = [item for move in plan.get("lineupMoves", []) for item in move.get("items", [])]
        self._execute_lineup_items(plan, config["leagueId"], lineup_items)
        self.recommendations.update_one(
            {"_id": plan["_id"], "approvalStatus": "pending"},
            {"$set": {"approvalStatus": "approved", "reviewedAt": datetime.now(timezone.utc)}},
        )
        return {"status": "approved"}

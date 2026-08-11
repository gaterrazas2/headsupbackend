import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from bson.objectid import ObjectId
from bson.errors import InvalidId


class FantasyManager:
    LEAGUES = {
        "cs": {"name": "CS League", "leagueId": 1082498796},
        "discord": {"name": "Discord League", "leagueId": 788168317},
    }
    BENCH_SLOT = 20
    IR_SLOT = 21
    LINEUP_POSITION_NAMES = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K"}
    PLAYER_POSITION_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
    POSITION_STARTER_SLOTS = {1: 0, 2: 2, 3: 4, 4: 6, 5: 17, 16: 16}

    def __init__(self, recommendations_collection):
        self.recommendations = recommendations_collection
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
            "eligibleSlots": player.get("eligibleSlots", []),
            "lineupSlotId": entry.get("lineupSlotId", self.BENCH_SLOT),
            "projectedPoints": self._projection(player, week),
            "injuryStatus": player.get("injuryStatus", "ACTIVE"),
        }

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
            candidates.sort(key=lambda player: player["projectedPoints"], reverse=True)
            for player in candidates:
                search(
                    index + 1,
                    used | {player["id"]},
                    score + player["projectedPoints"],
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
                "projectedPoints": self._projection(player, week),
                "status": entry.get("status", "FREEAGENT"),
            })
        return agents

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
        players = [self._player(entry, week) for entry in entries]
        team_name = team.get("name") or f'{team.get("location", "")} {team.get("nickname", "")}'.strip()

        if not players:
            return {
                "leagueKey": league_key,
                "leagueName": config["name"],
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
                "leagueName": config["name"],
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
        lineup_moves = []
        for player in players:
            desired = desired_slots.get(player["id"], self.BENCH_SLOT)
            if desired != player["lineupSlotId"] and player["lineupSlotId"] != self.IR_SLOT:
                lineup_moves.append({
                    "playerId": player["id"],
                    "player": player["name"],
                    "fromSlotId": player["lineupSlotId"],
                    "toSlotId": desired,
                    "projectedPoints": player["projectedPoints"],
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
                drop = min(same_position, key=lambda player: player["projectedPoints"])
                improvement = agent["projectedPoints"] - drop["projectedPoints"]
                if improvement >= 3:
                    upgrades.append((improvement, agent, drop))
            if upgrades:
                improvement, add, drop = max(upgrades, key=lambda item: item[0])
                add_drop_moves.append({
                    "addPlayerId": add["id"], "addPlayer": add["name"],
                    "dropPlayerId": drop["id"], "dropPlayer": drop["name"],
                    "position": add["position"], "status": add["status"],
                    "improvement": round(improvement, 2),
                })
        except Exception as error:
            print(f"Could not calculate free-agent upgrades: {error}")

        return {
            "leagueKey": league_key,
            "leagueName": config["name"],
            "teamName": team_name,
            "teamId": team["id"],
            "week": week,
            "status": "ready",
            "message": "Review every move before approving.",
            "lineupMoves": lineup_moves,
            "addDropMoves": add_drop_moves,
            "roster": players,
        }

    def draft_board(self, league_key):
        config = self.LEAGUES.get(league_key)
        if not config:
            raise ValueError("Unknown fantasy league")
        if not self.configured():
            raise ValueError("ESPN connection is not configured")

        league = self._request_json(
            f'{self._league_url(config["leagueId"])}?view=mTeam&view=mRoster&view=mSettings'
        )
        team = self._owned_team(league)
        entries = (team.get("roster") or {}).get("entries", [])
        roster_counts = {}
        for entry in entries:
            position_id = (entry.get("playerPoolEntry") or {}).get("player", {}).get("defaultPositionId")
            roster_counts[position_id] = roster_counts.get(position_id, 0) + 1

        scoring_items = league.get("settings", {}).get("scoringSettings", {}).get("scoringItems", [])
        reception_points = next(
            (float(item.get("points", 0)) for item in scoring_items if item.get("statId") == 53),
            0,
        )
        rank_type = "PPR" if reception_points >= 0.5 else "STANDARD"
        fantasy_filter = {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                "filterSlotIds": {"value": list(self.LINEUP_POSITION_NAMES)},
                "limit": 100,
                "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": rank_type},
            }
        }
        available = self._request_json(
            f'{self._league_url(config["leagueId"])}?view=kona_player_info',
            headers={"X-Fantasy-Filter": json.dumps(fantasy_filter)},
        ).get("players", [])

        slot_counts = league.get("settings", {}).get("rosterSettings", {}).get("lineupSlotCounts", {})
        recommendations = []
        for entry in available:
            player = entry.get("player", {})
            position_id = player.get("defaultPositionId")
            if position_id not in self.PLAYER_POSITION_NAMES:
                continue
            rank_data = (player.get("draftRanksByRankType") or {}).get(rank_type, {})
            rank = rank_data.get("rank")
            if not rank:
                continue
            starter_slot = self.POSITION_STARTER_SLOTS.get(position_id)
            starter_target = int(slot_counts.get(str(starter_slot), 0)) if starter_slot is not None else 0
            current_count = roster_counts.get(position_id, 0)
            needs_starter = position_id not in (5, 16) and current_count < starter_target
            ownership = player.get("ownership", {}) or {}
            season_projection = self._projection(player, 1)
            reason = "Best player available by ESPN draft rank"
            if needs_starter:
                reason = f'Fills a starting {self.PLAYER_POSITION_NAMES[position_id]} need'
            elif current_count:
                reason = f'Adds depth at {self.PLAYER_POSITION_NAMES[position_id]}'
            recommendations.append({
                "playerId": player.get("id"),
                "name": player.get("fullName", "Unknown player"),
                "position": self.PLAYER_POSITION_NAMES[position_id],
                "rank": int(rank),
                "adp": round(float(ownership.get("averageDraftPosition") or 0), 1),
                "auctionValue": rank_data.get("auctionValue"),
                "projectedPoints": season_projection,
                "injuryStatus": player.get("injuryStatus", "ACTIVE"),
                "reason": reason,
                "needsStarter": needs_starter,
                "recommendationScore": int(rank) - (8 if entries and needs_starter else 0),
            })

        recommendations.sort(key=lambda player: (player["recommendationScore"], player["rank"]))
        for player in recommendations:
            player.pop("recommendationScore", None)
        team_name = team.get("name") or f'{team.get("location", "")} {team.get("nickname", "")}'.strip()
        draft_settings = league.get("settings", {}).get("draftSettings", {})
        return {
            "leagueName": config["name"],
            "teamName": team_name,
            "rankType": rank_type,
            "draftType": draft_settings.get("type", "SNAKE"),
            "draftDate": draft_settings.get("date"),
            "rosterSize": len(entries),
            "players": recommendations[:30],
        }

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

    def _execute_lineup(self, plan, league_id):
        moves = plan.get("lineupMoves", [])
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

    def _execute_add_drop(self, plan, league_id):
        for move in plan.get("addDropMoves", []):
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

    def approve_plan(self, plan_id):
        plan = self._pending_plan(plan_id)
        config = self.LEAGUES.get(plan.get("leagueKey"))
        if not config:
            raise ValueError("Unknown fantasy league")
        if plan.get("status") != "ready":
            raise ValueError("This league does not have actionable recommendations yet")
        if not plan.get("lineupMoves") and not plan.get("addDropMoves"):
            raise ValueError("There are no changes to execute")

        self._execute_add_drop(plan, config["leagueId"])
        self._execute_lineup(plan, config["leagueId"])
        self.recommendations.update_one(
            {"_id": plan["_id"], "approvalStatus": "pending"},
            {"$set": {"approvalStatus": "approved", "reviewedAt": datetime.now(timezone.utc)}},
        )
        return {"status": "approved"}

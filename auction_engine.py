from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

FORBIDDEN_DEFAULT = {
    "Bijan Robinson", "Jayden Daniels", "Joe Burrow",
    "Jalen Hurts", "Puka Nacua", "Ja'Marr Chase",
}
TARGET_QBS = {"Trevor Lawrence", "Caleb Williams", "Kyler Murray"}


def base_position(position_rank: str) -> str:
    return re.sub(r"\d+$", "", str(position_rank))


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


@dataclass(frozen=True)
class Recommendation:
    action: str
    max_bid: int | None
    reason: str
    next_bid: int | None = None
    provisional: bool = False


def reserve_after_win(roster_size: int, players_owned: int, minimum_bid: int = 1) -> int:
    remaining_slots = max(roster_size - players_owned - 1, 0)
    return remaining_slots * minimum_bid


def hard_budget_ceiling(
    budget_remaining: int, roster_size: int, players_owned: int, minimum_bid: int = 1
) -> int:
    return max(0, budget_remaining - reserve_after_win(roster_size, players_owned, minimum_bid))


def recommend(
    player: dict | None,
    current_bid: int,
    budget_remaining: int,
    roster_size: int,
    players_owned: int,
    owned_positions: Iterable[str],
    forbidden: set[str],
    setup_confirmed: bool,
    minimum_bid: int = 1,
    top10_rb_available: int = 10,
) -> Recommendation:
    if not player:
        return Recommendation("CHECK SOURCE", None, "No matching source value is available; verify the PDF before setting a max.", provisional=not setup_confirmed)
    name = str(player["player"])
    if normalize(name) in {normalize(x) for x in forbidden}:
        return Recommendation("PASS", 0, "Hard do-not-draft rule.")
    source_value = int(player["value"])
    max_bid = source_value + (3 if name in TARGET_QBS else 0)
    budget_cap = hard_budget_ceiling(budget_remaining, roster_size, players_owned, minimum_bid)
    max_bid = min(max_bid, budget_cap)
    next_bid = int(current_bid) + 1
    position = base_position(player["position_rank"])
    strong_qbs = sum(1 for p in owned_positions if p == "QB")
    has_top10_rb = any(p == "TOP10_RB" for p in owned_positions)
    context = []
    if name in TARGET_QBS and strong_qbs < 2:
        context.append("priority QB target")
    elif position == "QB" and strong_qbs >= 2:
        context.append("two QBs already rostered, so urgency is lower")
    if position == "RB" and int(player["position_rank"][2:]) <= 10 and not has_top10_rb:
        context.append("fills the top-10 RB goal")
        if top10_rb_available <= 4:
            context.append("the eligible pool is thinning")
    provisional = not setup_confirmed
    if next_bid <= max_bid and next_bid <= budget_cap:
        reason = ", ".join(context) or "within the Ringer SuperFlex value"
        if provisional:
            reason += "; provisional until roster settings are confirmed"
        return Recommendation(f"BID ${next_bid}", max_bid, reason + ".", next_bid, provisional)
    reason = f"Next bid would exceed the ${max_bid} ceiling"
    if budget_cap < source_value:
        reason += f" after protecting a ${reserve_after_win(roster_size, players_owned, minimum_bid)} endgame reserve"
    return Recommendation(f"STOP AT ${max_bid}" if current_bid <= max_bid else "PASS", max_bid, reason + ".", provisional=provisional)

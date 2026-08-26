from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from io import StringIO
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from auction_engine import FORBIDDEN_DEFAULT, TARGET_QBS, base_position, normalize, recommend

st.set_page_config(page_title="SuperFlex Auction Proxy", page_icon="🏈", layout="wide")
SHEET_DEFAULT = "https://docs.google.com/spreadsheets/d/1pcopng1BexBWapsVGMBoYAjRQ6eZ-PbW_fDfUoeW4QE/edit?pli=1&gid=1862233416#gid=1862233416"
TAB_DEFAULT = "2025 draft"
APP_VERSION = "Draft Room 2.0 · 2026-08-26"
ROSTER_DEFAULTS = {"QB": 2, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "BENCH": 7}


@st.cache_data
def load_rankings():
    frame = pd.read_csv("data/ringer_superflex_2026.csv")
    frame["rank"] = pd.to_numeric(frame["rank"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["player", "position_rank", "rank", "value"]).copy()
    frame[["rank", "value"]] = frame[["rank", "value"]].astype(int)
    frame["base_position"] = frame["position_rank"].map(base_position)
    frame["key"] = frame["player"].map(normalize)
    return frame.sort_values("rank").reset_index(drop=True)


def sheet_id(url):
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", str(url))
    return match.group(1) if match else None


@st.cache_data(ttl=15, show_spinner=False)
def load_sheet(url, tab):
    sid = sheet_id(url)
    if not sid:
        raise ValueError("The Google Sheets URL is not valid.")
    if "gcp_service_account" in st.secrets:
        import gspread
        from google.oauth2.service_account import Credentials

        credentials = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        values = gspread.authorize(credentials).open_by_key(sid).worksheet(tab).get_all_values()
        if not values:
            return pd.DataFrame(), stamp()
        width = max(map(len, values))
        padded = [row + [""] * (width - len(row)) for row in values]
        return pd.DataFrame(padded[1:], columns=padded[0]), stamp()
    csv_url = f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&sheet={quote(tab)}"
    response = requests.get(csv_url, timeout=12)
    response.raise_for_status()
    if "text/html" in response.headers.get("content-type", ""):
        raise PermissionError("The sheet is not public. Add a service account in Streamlit secrets.")
    return pd.read_csv(StringIO(response.text)), stamp()


def safe_int(value, default=0):
    if value is None or pd.isna(value):
        return default
    try:
        return int(float(str(value).replace("$", "").replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_state():
    defaults = {
        "wins": [], "manual_drafted": [], "action_log": [], "budget": 200,
        "minimum_bid": 1, "roster_slots": ROSTER_DEFAULTS.copy(), "setup_confirmed": False,
        "forbidden": sorted(FORBIDDEN_DEFAULT), "sheet_url": SHEET_DEFAULT,
        "sheet_tab": TAB_DEFAULT, "drafted_columns": [], "drafted_columns_initialized": False,
        "last_decision": None, "last_sync": None, "auto_refresh": True, "refresh_seconds": 20,
        "rehearsal_mode": False,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("sheet_url_input", st.session_state.sheet_url)
    st.session_state.setdefault("sheet_tab_input", st.session_state.sheet_tab)


def roster_size():
    return sum(max(safe_int(value), 0) for value in st.session_state.roster_slots.values())


def budget_remaining():
    return safe_int(st.session_state.budget) - sum(safe_int(item.get("price")) for item in st.session_state.wins)


def add_log(action, player="", price=None, detail=""):
    st.session_state.action_log.append(
        {"time": stamp(), "action": action, "player": player, "price": price, "detail": detail}
    )


def add_win(player, position_rank, price, source="manual"):
    player = "" if pd.isna(player) else str(player).strip()
    price = safe_int(price)
    if not player or price < 1:
        return False
    if any(normalize(item.get("player", "")) == normalize(player) for item in st.session_state.wins):
        return False
    st.session_state.wins.append(
        {"player": player, "position_rank": str(position_rank), "price": price, "source": source}
    )
    st.session_state.manual_drafted = [name for name in st.session_state.manual_drafted if normalize(name) != normalize(player)]
    add_log("WON", player, price, source)
    return True


def mark_lost(player, detail="Commissioner entry"):
    if not any(normalize(name) == normalize(player) for name in st.session_state.manual_drafted):
        st.session_state.manual_drafted.append(player)
    add_log("LOST", player, None, detail)


def undo_last_result():
    for index in range(len(st.session_state.action_log) - 1, -1, -1):
        event = st.session_state.action_log[index]
        player = event.get("player", "")
        if event.get("action") == "WON":
            st.session_state.wins = [item for item in st.session_state.wins if normalize(item.get("player", "")) != normalize(player)]
            del st.session_state.action_log[index]
            return True
        if event.get("action") == "LOST":
            st.session_state.manual_drafted = [name for name in st.session_state.manual_drafted if normalize(name) != normalize(player)]
            del st.session_state.action_log[index]
            return True
    return False


def without_suffix(name):
    return re.sub(r"(?:jr|sr|ii|iii|iv)$", "", normalize(name))


def match_player_name(text, sentence=False, cutoff=.72):
    if pd.isna(text) or not str(text).strip():
        return None, 0.0
    text_key = normalize(text)
    rows = rankings[["player", "key"]]
    exact = rows[rows["key"] == text_key]
    if not exact.empty:
        return exact.iloc[0]["player"], 1.0
    suffix_key = without_suffix(text)
    suffix_matches = rows[rows["player"].map(without_suffix) == suffix_key]
    if len(suffix_matches) == 1:
        return suffix_matches.iloc[0]["player"], .99
    mentions = rows[rows["key"].map(lambda key: len(key) >= 5 and key in text_key)]
    if not mentions.empty:
        winner = mentions.sort_values("key", key=lambda column: column.str.len(), ascending=False).iloc[0]
        return winner["player"], .98
    words = re.sub(r"[^a-zA-Z0-9' -]", " ", str(text)).split()
    candidates = [str(text)]
    max_words = 4 if sentence else min(4, len(words))
    for size in range(max_words, 0, -1):
        candidates.extend(" ".join(words[i:i + size]) for i in range(len(words) - size + 1))
    best_name, best_score = None, 0.0
    for _, item in rows.iterrows():
        keys = {item["key"], without_suffix(item["player"])}
        for candidate in candidates:
            candidate_key = normalize(candidate)
            if candidate_key:
                score = max(SequenceMatcher(None, candidate_key, key).ratio() for key in keys)
                if score > best_score:
                    best_name, best_score = item["player"], score
    if len(words) == 1 and len(words[0]) >= 4:
        last_matches = rows[rows["player"].str.split().str[-1].map(normalize) == normalize(words[0])]
        if len(last_matches) == 1:
            return last_matches.iloc[0]["player"], .95
    return (best_name, best_score) if best_score >= cutoff else (None, best_score)


def recognized_player(value):
    if value is None or pd.isna(value):
        return None
    cell_key = normalize(value)
    if not cell_key:
        return None
    if cell_key in PLAYER_KEY_MAP:
        return PLAYER_KEY_MAP[cell_key]
    suffix_key = without_suffix(value)
    if suffix_key in PLAYER_SUFFIX_MAP:
        return PLAYER_SUFFIX_MAP[suffix_key]
    for player_key in PLAYER_KEYS_BY_LENGTH:
        if len(player_key) >= 6 and player_key in cell_key:
            return PLAYER_KEY_MAP[player_key]
    return None


def player_counts_by_column(frame):
    if frame.empty:
        return {}
    return {
        column: int(frame[column].dropna().astype(str).map(recognized_player).notna().sum())
        for column in frame.dropna(axis=1, how="all").columns
    }


def suggested_drafted_columns(frame, total_slots):
    upper_bound = max(total_slots + 2, 6)
    return [column for column, count in player_counts_by_column(frame).items() if 0 < count <= upper_bound]


def drafted_player_locations(frame, columns):
    locations = {}
    if frame.empty:
        return locations
    for column in [name for name in columns if name in frame.columns]:
        for row_index, value in frame[column].items():
            player = recognized_player(value)
            if player:
                locations.setdefault(normalize(player), []).append(
                    {"player": player, "column": str(column), "row": int(row_index) + 2, "cell": str(value)}
                )
    return locations


def owned_markers():
    markers = []
    for win in st.session_state.wins:
        position = base_position(win.get("position_rank", ""))
        markers.append(position)
        source = rankings[rankings["key"] == normalize(win.get("player", ""))]
        if position == "RB" and not source.empty:
            number = safe_int(re.sub(r"\D", "", str(source.iloc[0]["position_rank"])), 999)
            if number <= 10:
                markers.append("TOP10_RB")
    return markers


def recommendation_for(selected_name, current_bid, unavailable_keys):
    row = rankings.loc[rankings["player"] == selected_name].iloc[0].to_dict()
    numbers = pd.to_numeric(rankings["position_rank"].str.extract(r"(\d+)")[0], errors="coerce").fillna(999)
    eligible_top10 = rankings[
        (rankings["base_position"] == "RB") & numbers.le(10)
        & ~rankings["key"].isin({normalize(name) for name in st.session_state.forbidden})
        & ~rankings["key"].isin(unavailable_keys)
    ]
    result = recommend(
        row, safe_int(current_bid), budget_remaining(), roster_size(), len(st.session_state.wins),
        owned_markers(), set(st.session_state.forbidden), st.session_state.setup_confirmed,
        safe_int(st.session_state.minimum_bid, 1), len(eligible_top10),
    )
    return row, result


def question_intent(message):
    lowered = str(message).casefold()
    if any(term in lowered for term in ["available", "drafted", "taken", "still there"]):
        return "availability"
    if any(term in lowered for term in ["max", "ceiling", "stop at", "highest"]):
        return "maximum"
    return "bid"


def sheet_location_text(player, locations):
    hits = locations.get(normalize(player), [])
    if not hits:
        return "recorded as drafted"
    first = hits[0]
    return f"found in column **{first['column']}**, sheet row **{first['row']}**"


def recovery_payload():
    payload = {
        "version": 1, "exported_at": stamp(), "wins": st.session_state.wins,
        "manual_drafted": st.session_state.manual_drafted, "action_log": st.session_state.action_log,
        "budget": st.session_state.budget, "minimum_bid": st.session_state.minimum_bid,
        "roster_slots": st.session_state.roster_slots, "setup_confirmed": st.session_state.setup_confirmed,
        "forbidden": st.session_state.forbidden, "sheet_url": st.session_state.sheet_url,
        "sheet_tab": st.session_state.sheet_tab, "drafted_columns": st.session_state.drafted_columns,
    }
    return json.dumps(payload, indent=2)


def restore_payload(uploaded):
    try:
        payload = json.loads(uploaded.getvalue().decode("utf-8"))
        for key in ["wins", "manual_drafted", "action_log", "forbidden", "drafted_columns"]:
            if key in payload and isinstance(payload[key], list):
                st.session_state[key] = payload[key]
        for key in ["budget", "minimum_bid", "setup_confirmed", "sheet_url", "sheet_tab"]:
            if key in payload:
                st.session_state[key] = payload[key]
        if isinstance(payload.get("roster_slots"), dict):
            st.session_state.roster_slots = payload["roster_slots"]
        st.session_state.sheet_url_input = st.session_state.sheet_url
        st.session_state.sheet_tab_input = st.session_state.sheet_tab
        st.session_state.drafted_columns_initialized = bool(st.session_state.drafted_columns)
        load_sheet.clear()
        return True, "Draft state restored."
    except Exception as exc:
        return False, f"Could not restore that file: {exc}"


init_state()
rankings = load_rankings()
PLAYER_KEY_MAP = dict(zip(rankings["key"], rankings["player"]))
suffix_groups = {}
for ranked_name in rankings["player"]:
    suffix_groups.setdefault(without_suffix(ranked_name), []).append(ranked_name)
PLAYER_SUFFIX_MAP = {key: names[0] for key, names in suffix_groups.items() if len(names) == 1}
PLAYER_KEYS_BY_LENGTH = sorted(PLAYER_KEY_MAP, key=len, reverse=True)

with st.sidebar:
    st.header("Draft controls")
    st.session_state.rehearsal_mode = st.toggle("Rehearsal mode", value=st.session_state.rehearsal_mode)
    if st.button("Refresh Google Sheet now", type="primary", use_container_width=True):
        load_sheet.clear()
        st.session_state.last_sync = None
        st.rerun()
    st.session_state.auto_refresh = st.toggle("Auto-refresh", value=st.session_state.auto_refresh)
    st.session_state.refresh_seconds = st.slider(
        "Refresh interval", 10, 120, safe_int(st.session_state.refresh_seconds, 20), 5, format="%d sec"
    )
    if st.session_state.auto_refresh:
        st_autorefresh(interval=st.session_state.refresh_seconds * 1000, key="sheet_refresh")

    with st.expander("Admin setup", expanded=not st.session_state.setup_confirmed):
        with st.form("draft_source_form"):
            source_url = st.text_input("Google Sheet URL", key="sheet_url_input")
            source_tab = st.text_input("Tab name", key="sheet_tab_input")
            apply_source = st.form_submit_button("Apply draft source", use_container_width=True)
        if apply_source:
            if not sheet_id(source_url.strip()):
                st.error("Enter a valid Google Sheets URL.")
            elif not source_tab.strip():
                st.error("Enter the exact tab name.")
            else:
                st.session_state.sheet_url = source_url.strip()
                st.session_state.sheet_tab = source_tab.strip()
                st.session_state.drafted_columns = []
                st.session_state.drafted_columns_initialized = False
                load_sheet.clear()
                st.rerun()
        st.number_input("Starting budget", 1, 1000, key="budget")
        st.number_input("Minimum winning bid", 1, 20, key="minimum_bid")
        st.caption("Roster template")
        for position in ["QB", "RB", "WR", "TE", "FLEX", "BENCH"]:
            st.session_state.roster_slots[position] = st.number_input(
                position, 0, 20, safe_int(st.session_state.roster_slots.get(position)), key=f"slot_{position}"
            )
        st.session_state.setup_confirmed = st.checkbox("League and roster settings confirmed", value=st.session_state.setup_confirmed)
        avoid_text = st.text_area("Players to avoid — one per line", "\n".join(st.session_state.forbidden), height=150)
        st.session_state.forbidden = [line.strip() for line in avoid_text.splitlines() if line.strip()]

    with st.expander("Backup and recovery"):
        st.download_button("Download draft backup", recovery_payload(), "auction-draft-backup.json", "application/json", use_container_width=True)
        backup_file = st.file_uploader("Restore a backup", type=["json"])
        if backup_file is not None and st.button("Restore uploaded backup", use_container_width=True):
            restored, restore_message = restore_payload(backup_file)
            (st.success if restored else st.error)(restore_message)
            if restored:
                st.rerun()
        confirm_reset = st.checkbox("I understand this clears local wins and losses")
        if st.button("Reset local draft", disabled=not confirm_reset, use_container_width=True):
            st.session_state.wins = []
            st.session_state.manual_drafted = []
            st.session_state.action_log = []
            st.session_state.last_decision = None
            st.rerun()

sheet_df, sheet_error = pd.DataFrame(), None
try:
    sheet_df, fetched_at = load_sheet(st.session_state.sheet_url, st.session_state.sheet_tab)
    st.session_state.last_sync = fetched_at
except Exception as exc:
    sheet_error = exc

if not st.session_state.drafted_columns_initialized and not sheet_df.empty:
    st.session_state.drafted_columns = suggested_drafted_columns(sheet_df, roster_size())
    st.session_state.drafted_columns_initialized = True
st.session_state.drafted_columns = [column for column in st.session_state.drafted_columns if column in sheet_df.columns]
sheet_locations = drafted_player_locations(sheet_df, st.session_state.drafted_columns)
board_drafted_names = {hit["player"] for hits in sheet_locations.values() for hit in hits}
owned_names = {item.get("player", "") for item in st.session_state.wins}
known_drafted_names = board_drafted_names | owned_names | set(st.session_state.manual_drafted)
known_drafted_keys = {normalize(name) for name in known_drafted_names if name}

remaining = budget_remaining()
owned_count = len(st.session_state.wins)
unfilled = max(roster_size() - owned_count, 0)
reserve = unfilled * safe_int(st.session_state.minimum_bid, 1)
flexible = max(remaining - reserve, 0)
qbs_owned = sum(base_position(item.get("position_rank", "")) == "QB" for item in st.session_state.wins)
top10_rb_owned = "TOP10_RB" in owned_markers()
avoid_keys = {normalize(name) for name in st.session_state.forbidden}
available = rankings[~rankings["key"].isin(known_drafted_keys | avoid_keys)].copy()
budget_cap = max(0, remaining - max(unfilled - 1, 0) * safe_int(st.session_state.minimum_bid, 1))
available["maximum"] = available.apply(
    lambda item: min(int(item["value"]) + (3 if item["player"] in TARGET_QBS else 0), budget_cap), axis=1
)

st.title("SuperFlex Auction Proxy")
st.caption(f"{APP_VERSION} · Commissioner view · 12 teams · Half-PPR · $200 SuperFlex · exact $1 bid increments")
st.markdown(
    """<style>
    div.stButton > button {min-height: 3rem; font-weight: 700;}
    @media (max-width: 760px) {
      [data-testid="stMetricValue"] {font-size: 1.35rem;}
      div[data-testid="stHorizontalBlock"] {gap: .45rem;}
    }
    </style>""",
    unsafe_allow_html=True,
)
if st.session_state.rehearsal_mode:
    st.info("REHEARSAL MODE — results affect only this app session and backup file; the Google Sheet is read-only.")
if sheet_error:
    st.error(f"SHEET OFFLINE — manual tracking is active. {sheet_error}")
elif sheet_df.empty:
    st.warning(f"SHEET CONNECTED BUT EMPTY — tab: {st.session_state.sheet_tab}")
else:
    sync_time = datetime.fromisoformat(st.session_state.last_sync).astimezone().strftime("%I:%M:%S %p")
    st.success(f"LIVE — synced {sync_time} · tab: {st.session_state.sheet_tab} · {len(board_drafted_names)} drafted players recognized")

metrics = st.columns(6)
metrics[0].metric("Budget left", f"${remaining}")
metrics[1].metric("Spent", f"${safe_int(st.session_state.budget) - remaining}")
metrics[2].metric("Open spots", unfilled)
metrics[3].metric("Protected reserve", f"${reserve}")
metrics[4].metric("Spendable now", f"${flexible}")
metrics[5].metric("Drafted on board", len(board_drafted_names))
goal_left, goal_right = st.columns(2)
(goal_left.success if qbs_owned >= 2 else goal_left.warning)(f"Starting QB goal: {qbs_owned}/2")
(goal_right.success if top10_rb_owned else goal_right.warning)("Top-10 RB secured" if top10_rb_owned else "Top-10 eligible RB still needed")

readiness = {
    "Rankings loaded": not rankings.empty,
    "Sheet connected": sheet_error is None and not sheet_df.empty,
    "Draft columns selected": bool(st.session_state.drafted_columns),
    "Roster rules confirmed": bool(st.session_state.setup_confirmed),
    "Budget valid": remaining >= reserve,
}
failed = [label for label, ready in readiness.items() if not ready]
if failed:
    st.warning("READINESS CHECK — " + " · ".join(f"⚠ {item}" for item in failed))

st.divider()
left, right = st.columns([1.35, .85], gap="large")
with left:
    st.header("Live nomination")
    st.caption("Type a player or paste the commissioner's question. Misspellings and last names are supported.")
    with st.form("live_action_form", clear_on_submit=True):
        live_message = st.text_input("Player or question", placeholder="Trevor Lawerence is at $20 — should I bid?")
        current_bid_input = st.number_input("Current high bid", 0, 200, 0, 1)
        evaluate = st.form_submit_button("Get recommendation", type="primary", use_container_width=True)
    if evaluate:
        st.session_state.pop("decision_result_price", None)
        matched_name, confidence = match_player_name(live_message, sentence=True, cutoff=.70)
        amounts = [int(first or second) for first, second in re.findall(r"\$\s*(\d+)|\b(\d+)\s*dollars?\b", live_message, flags=re.I)]
        current_bid = amounts[-1] if amounts else safe_int(current_bid_input)
        intent = question_intent(live_message)
        if not matched_name:
            st.session_state.last_decision = {"kind": "error", "message": "No confident source match. Add the player's first or last name."}
        elif normalize(matched_name) in avoid_keys:
            source_row, result = recommendation_for(matched_name, current_bid, known_drafted_keys)
            st.session_state.last_decision = {
                "kind": "recommendation", "player": matched_name, "confidence": confidence,
                "current_bid": current_bid, "intent": intent, "action": "PASS", "forbidden": True,
                "max_bid": 0, "next_bid": None, "reason": "Hard do-not-draft rule.",
                "provisional": False, "rank": int(source_row["rank"]),
                "position_rank": str(source_row["position_rank"]), "source_value": int(source_row["value"]),
            }
        elif normalize(matched_name) in known_drafted_keys:
            st.session_state.last_decision = {
                "kind": "unavailable", "player": matched_name,
                "message": sheet_location_text(matched_name, sheet_locations), "confidence": confidence,
            }
        else:
            source_row, result = recommendation_for(matched_name, current_bid, known_drafted_keys)
            display_action = "AVAILABLE" if intent == "availability" else f"STOP AT ${result.max_bid}" if intent == "maximum" else result.action
            st.session_state.last_decision = {
                "kind": "recommendation", "player": matched_name, "confidence": confidence,
                "current_bid": current_bid, "intent": intent, "action": display_action,
                "max_bid": result.max_bid, "next_bid": result.next_bid, "reason": result.reason,
                "provisional": result.provisional, "rank": int(source_row["rank"]),
                "position_rank": str(source_row["position_rank"]), "source_value": int(source_row["value"]),
            }
    decision = st.session_state.last_decision
    if decision:
        if decision["kind"] == "error":
            st.error(decision["message"])
        elif decision["kind"] == "unavailable":
            st.error(f"# UNAVAILABLE — {decision['player']}")
            st.write(f"The player is already drafted: {decision['message']}.")
        else:
            action = decision["action"]
            color = "#1976D2" if action == "AVAILABLE" else "#16833B" if action.startswith("BID") else "#B45309" if action.startswith("STOP") else "#B42318"
            st.markdown(
                f'<div style="padding:24px;border-radius:14px;background:{color};color:white"><div style="font-size:2.35rem;font-weight:850">{action}</div><div style="font-size:1.15rem;margin-top:6px">{decision["player"]}</div></div>',
                unsafe_allow_html=True,
            )
            st.write(f"**Maximum ${decision['max_bid']}** · Source ${decision['source_value']} · #{decision['rank']} overall · {decision['position_rank']}")
            st.caption(decision["reason"])
            if decision["confidence"] < .98:
                st.info(f"Matched the entry to {decision['player']} ({decision['confidence']:.0%} confidence).")
            if decision["provisional"]:
                st.warning("PROVISIONAL — confirm the roster and minimum-bid settings in Admin setup.")
            st.subheader("Record the result")
            forbidden_decision = decision.get("forbidden", False)
            if not forbidden_decision:
                result_price = st.number_input("Winning price", 1, 200, safe_int(decision["next_bid"] or max(decision["current_bid"], 1), 1), 1, key="decision_result_price")
            won_col, lost_col, clear_col = st.columns(3)
            if won_col.button("WON", type="primary", disabled=forbidden_decision, use_container_width=True):
                if result_price > remaining:
                    st.error("That price exceeds the remaining budget.")
                else:
                    add_win(decision["player"], decision["position_rank"], result_price, "Draft Room")
                    st.session_state.last_decision = None
                    st.rerun()
            if lost_col.button("LOST", use_container_width=True):
                mark_lost(decision["player"])
                st.session_state.last_decision = None
                st.rerun()
            if clear_col.button("NEW NOMINATION", use_container_width=True):
                st.session_state.last_decision = None
                st.rerun()

with right:
    st.header("Best available")
    if available.empty:
        st.error("No ranked players are available. Check the selected draft columns below.")
    else:
        best_overall = available.iloc[0]
        target_qbs = available[available["player"].isin(TARGET_QBS)]
        rb_number = pd.to_numeric(available["position_rank"].str.extract(r"(\d+)")[0], errors="coerce")
        top_rbs = available[(available["base_position"] == "RB") & rb_number.le(10)]
        fit, reason = best_overall, "Highest-ranked available player"
        if qbs_owned < 2 and not target_qbs.empty:
            fit, reason = target_qbs.iloc[0], "Priority QB target"
        elif not top10_rb_owned and not top_rbs.empty:
            fit, reason = top_rbs.iloc[0], "Top-10 RB goal"
        st.metric("Best roster fit", fit["player"], f"#{int(fit['rank'])} · max ${int(fit['maximum'])}")
        st.caption(f"{fit['position_rank']} · {reason}")
        if fit["player"] != best_overall["player"]:
            st.write(f"**Best overall:** {best_overall['player']} · #{int(best_overall['rank'])} · max ${int(best_overall['maximum'])}")
        rows = []
        for position in ["QB", "RB", "WR", "TE"]:
            group = available[available["base_position"] == position]
            if not group.empty:
                item = group.iloc[0]
                rows.append({"Pos": position, "Player": item["player"], "Rank": int(item["rank"]), "Max": int(item["maximum"])})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, column_config={"Max": st.column_config.NumberColumn(format="$%d")})
        st.caption(f"{len(available)} source-ranked players remain available.")
        nomination_pool = rankings[
            rankings["key"].isin(avoid_keys) & ~rankings["key"].isin(known_drafted_keys)
        ]
        if not nomination_pool.empty:
            nomination = nomination_pool.iloc[0]
            st.warning(
                f"NOMINATE ONLY — {nomination['player']} (#{int(nomination['rank'])}) can drain opponent budget. "
                "Do not bid."
            )

st.divider()
undo_col, backup_col = st.columns([1, 2])
if undo_col.button("Undo last WON/LOST", disabled=not st.session_state.action_log, use_container_width=True):
    if undo_last_result():
        st.session_state.last_decision = None
        st.rerun()
backup_col.download_button("Download current draft backup", recovery_payload(), "auction-draft-backup.json", "application/json", use_container_width=True)

with st.expander(f"My roster — {owned_count}/{roster_size()} players · ${remaining} remaining"):
    if st.session_state.wins:
        st.dataframe(pd.DataFrame(st.session_state.wins), hide_index=True, use_container_width=True, column_config={"price": st.column_config.NumberColumn(format="$%d")})
    else:
        st.info("No purchases recorded yet.")
    with st.form("manual_purchase_form"):
        manual_entry = st.text_input("Add a player manually")
        manual_price = st.number_input("Price paid", 1, 200, 1, 1)
        manual_add = st.form_submit_button("Add purchase")
    if manual_add:
        manual_match, _ = match_player_name(manual_entry, cutoff=.70)
        if not manual_match:
            st.error("That player could not be matched to the source rankings.")
        elif normalize(manual_match) in avoid_keys:
            st.error(f"PASS — {manual_match} is on the hard do-not-draft list and cannot be added.")
        else:
            source = rankings[rankings["player"] == manual_match].iloc[0]
            if add_win(manual_match, source["position_rank"], manual_price, "Manual purchase"):
                st.rerun()

with st.expander("Draft monitor and column mapping"):
    if sheet_error:
        st.error(f"Live feed unavailable: {sheet_error}")
        st.info("Continue manually with LOST and use Refresh Google Sheet now when service returns.")
    elif sheet_df.empty:
        st.warning("The selected tab returned no rows.")
    else:
        counts = player_counts_by_column(sheet_df)
        st.multiselect(
            "Columns containing drafted team rosters", list(sheet_df.columns), key="drafted_columns",
            help="Only these columns determine availability. Exclude rankings, watch lists, and reference columns.",
        )
        selected_summary = ", ".join(f"{column} ({counts.get(column, 0)})" for column in st.session_state.drafted_columns)
        st.caption(selected_summary or "No roster columns selected.")
        st.dataframe(sheet_df, hide_index=True, use_container_width=True, height=350)
    st.subheader("Manual drafted-player fallback")
    with st.form("manual_lost_form"):
        lost_entry = st.text_input("Player drafted by another team")
        lost_submit = st.form_submit_button("Mark unavailable")
    if lost_submit:
        lost_match, _ = match_player_name(lost_entry, cutoff=.70)
        if lost_match:
            mark_lost(lost_match, "Manual fallback")
            st.rerun()
        else:
            st.error("That player could not be matched to the source rankings.")

with st.expander("Decision history"):
    if st.session_state.action_log:
        st.dataframe(pd.DataFrame(st.session_state.action_log[::-1]), hide_index=True, use_container_width=True)
    else:
        st.caption("No auction results recorded yet.")

with st.expander("Authoritative Ringer 2026 SuperFlex values"):
    source_view = rankings[["rank", "player", "team", "position_rank", "value"]].copy()
    source_view["status"] = source_view["player"].map(lambda name: "AVOID" if normalize(name) in avoid_keys else "TARGET +$3" if name in TARGET_QBS else "")
    st.dataframe(source_view, hide_index=True, use_container_width=True, height=500, column_config={"value": st.column_config.NumberColumn("Value", format="$%d")})

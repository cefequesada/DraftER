from __future__ import annotations
from difflib import SequenceMatcher
import re
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

@st.cache_data
def load_rankings():
    df = pd.read_csv("data/ringer_superflex_2026.csv")
    df["base_position"] = df["position_rank"].map(base_position)
    df["key"] = df["player"].map(normalize)
    return df

def sheet_id(url):
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None

@st.cache_data(ttl=15, show_spinner=False)
def load_sheet(url, tab):
    sid = sheet_id(url)
    if not sid:
        raise ValueError("The Google Sheets URL is not valid.")
    if "gcp_service_account" in st.secrets:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        values = gspread.authorize(creds).open_by_key(sid).worksheet(tab).get_all_values()
        if not values:
            return pd.DataFrame()
        width = max(map(len, values))
        padded = [row + [""] * (width - len(row)) for row in values]
        return pd.DataFrame(padded[1:], columns=padded[0])
    csv_url = f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&sheet={quote(tab)}"
    response = requests.get(csv_url, timeout=12)
    response.raise_for_status()
    if "text/html" in response.headers.get("content-type", ""):
        raise PermissionError("The sheet is not public. Add a service account in Streamlit secrets.")
    return pd.read_csv(StringIO(response.text))

def init_state():
    defaults = {"wins": [], "budget": 200, "roster_size": 16, "minimum_bid": 1, "setup_confirmed": False, "forbidden": sorted(FORBIDDEN_DEFAULT), "sheet_url": SHEET_DEFAULT, "sheet_tab": TAB_DEFAULT, "sheet_player_col": None}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("sheet_url_input", st.session_state.sheet_url)
    st.session_state.setdefault("sheet_tab_input", st.session_state.sheet_tab)

def safe_int(value, default=0):
    """Convert user/sheet values safely; blank editor rows arrive as NaN."""
    if pd.isna(value):
        return default
    try:
        return int(float(str(value).replace("$", "").replace(",", "").strip()))
    except (TypeError, ValueError):
        return default

def budget_remaining():
    return safe_int(st.session_state.budget) - sum(safe_int(x.get("price")) for x in st.session_state.wins)

def add_win(player, position_rank, price, source="manual"):
    clean_player = "" if pd.isna(player) else str(player).strip()
    clean_price = safe_int(price)
    if not clean_player or clean_price < 1:
        return False
    if not any(normalize(x.get("player", "")) == normalize(clean_player) for x in st.session_state.wins):
        st.session_state.wins.append({"player": clean_player, "position_rank": str(position_rank), "price": clean_price, "source": source})
        return True
    return False

def without_suffix(name):
    return re.sub(r"(?:jr|sr|ii|iii|iv)$", "", normalize(name))

def match_player_name(text, sentence=False, cutoff=.72):
    """Fuzzy-match a name and return (authoritative name, confidence)."""
    if pd.isna(text) or not str(text).strip():
        return None, 0.0
    text_key = normalize(text)
    rows = rankings[["player", "key"]].copy()

    exact = rows[rows["key"] == text_key]
    if not exact.empty:
        return exact.iloc[0]["player"], 1.0
    suffix_key = without_suffix(text)
    suffix_matches = rows[rows["player"].map(without_suffix) == suffix_key]
    if len(suffix_matches) == 1:
        return suffix_matches.iloc[0]["player"], .99

    mentions = rows[rows["key"].map(lambda key: len(key) >= 5 and key in text_key)]
    if not mentions.empty:
        winner = mentions.sort_values("key", key=lambda col: col.str.len(), ascending=False).iloc[0]
        return winner["player"], .98

    words = re.sub(r"[^a-zA-Z0-9' -]", " ", str(text)).split()
    candidates = [str(text)]
    max_words = 4 if sentence else min(4, len(words))
    for size in range(max_words, 0, -1):
        candidates.extend(" ".join(words[i:i + size]) for i in range(len(words) - size + 1))

    best_name, best_score = None, 0.0
    for _, item in rows.iterrows():
        player_keys = {item["key"], without_suffix(item["player"])}
        for candidate in candidates:
            candidate_key = normalize(candidate)
            if not candidate_key:
                continue
            score = max(SequenceMatcher(None, candidate_key, key).ratio() for key in player_keys)
            if score > best_score:
                best_name, best_score = item["player"], score

    # A unique last name is safe even when the first name is omitted.
    if len(words) == 1 and len(words[0]) >= 4:
        last_matches = rows[rows["player"].str.split().str[-1].map(normalize) == normalize(words[0])]
        if len(last_matches) == 1:
            return last_matches.iloc[0]["player"], .95
    return (best_name, best_score) if best_score >= cutoff else (None, best_score)

def find_player_in_message(message):
    return match_player_name(message, sentence=True, cutoff=.70)

def recommendation_for(selected_name, current_bid):
    row = rankings.loc[rankings["player"] == selected_name].iloc[0].to_dict()
    owned_markers = []
    for win in st.session_state.wins:
        pos = base_position(win["position_rank"])
        owned_markers.append(pos)
        source = rankings[rankings["key"] == normalize(win["player"])]
        if pos == "RB" and not source.empty and int(source.iloc[0]["position_rank"][2:]) <= 10:
            owned_markers.append("TOP10_RB")
    position_numbers = pd.to_numeric(
        rankings["position_rank"].str.extract(r"(\d+)")[0], errors="coerce"
    ).fillna(999)
    eligible_top10 = rankings[
        (rankings["base_position"] == "RB")
        & position_numbers.le(10)
        & ~rankings["key"].isin({normalize(x) for x in st.session_state.forbidden})
        & ~rankings["key"].isin({normalize(x["player"]) for x in st.session_state.wins})
    ]
    rec = recommend(
        row, current_bid, budget_remaining(), int(st.session_state.roster_size),
        len(st.session_state.wins), owned_markers, set(st.session_state.forbidden),
        st.session_state.setup_confirmed, int(st.session_state.minimum_bid),
        len(eligible_top10),
    )
    return row, rec

def recognized_player(value):
    """Fast spreadsheet matcher: exact, suffix-insensitive, or embedded name."""
    if pd.isna(value):
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
        if player_key in cell_key:
            return PLAYER_KEY_MAP[player_key]
    return None

def detect_player_column(frame):
    """Pick the sheet column containing the most recognized ranked players."""
    if frame.empty:
        return None
    scores = {}
    for column in frame.columns:
        scores[column] = frame[column].head(80).map(recognized_player).notna().sum()
    best = max(scores, key=scores.get) if scores else None
    return best if best is not None and scores[best] > 0 else None

@st.cache_data(show_spinner=False)
def all_drafted_players(frame):
    """Recognize drafted players anywhere on a row-style or owner-column draft board."""
    if frame.empty:
        return set()
    found = set()
    nonempty = frame.dropna(axis=1, how="all")
    for column in nonempty.columns:
        values = nonempty[column].dropna().astype(str)
        found.update(name for name in values.map(recognized_player) if name)
    return found

init_state()
rankings = load_rankings()
PLAYER_KEY_MAP = dict(zip(rankings["key"], rankings["player"]))
suffix_groups = {}
for player_name in rankings["player"]:
    suffix_groups.setdefault(without_suffix(player_name), []).append(player_name)
PLAYER_SUFFIX_MAP = {key: names[0] for key, names in suffix_groups.items() if len(names) == 1}
PLAYER_KEYS_BY_LENGTH = sorted(PLAYER_KEY_MAP, key=len, reverse=True)
st.title("SuperFlex Auction Proxy")
st.caption("12 teams · Half-PPR · $200 budget · Ringer 2026 SuperFlex values · $1 bid increments")

with st.sidebar:
    st.header("Live draft source")
    with st.form("draft_source_form"):
        source_url = st.text_input("Google Sheet URL", key="sheet_url_input")
        source_tab = st.text_input("Tab name", key="sheet_tab_input")
        apply_source = st.form_submit_button("Apply draft source", type="primary", use_container_width=True)
    if apply_source:
        clean_url = source_url.strip()
        clean_tab = source_tab.strip()
        if not sheet_id(clean_url):
            st.error("Enter a valid Google Sheets URL.")
        elif not clean_tab:
            st.error("Enter the exact tab name.")
        else:
            st.session_state.sheet_url = clean_url
            st.session_state.sheet_tab = clean_tab
            st.session_state.sheet_player_col = None
            load_sheet.clear()
            st.success(f"Draft source changed to: {clean_tab}")
            st.rerun()
    st.caption(f"Active tab: **{st.session_state.sheet_tab}**")
    refresh_seconds = st.slider("Refresh every", 10, 120, 20, 5, format="%d sec")
    if st.toggle("Auto-refresh", True):
        st_autorefresh(interval=refresh_seconds * 1000, key="sheet_refresh")
    st.header("Players to avoid")
    avoid_text = st.text_area("One per line", "\n".join(st.session_state.forbidden), height=180)
    st.session_state.forbidden = [x.strip() for x in avoid_text.splitlines() if x.strip()]
    st.header("League setup")
    st.session_state.budget = st.number_input("Starting budget", 1, 1000, st.session_state.budget)
    st.session_state.roster_size = st.number_input("Total roster slots", 1, 40, st.session_state.roster_size)
    st.session_state.minimum_bid = st.number_input("Minimum winning bid", 1, 20, st.session_state.minimum_bid)
    st.session_state.setup_confirmed = st.checkbox("Roster size and minimum bid confirmed", value=st.session_state.setup_confirmed)
    st.caption("Default roster size is a placeholder. Confirm it before full-budget optimization.")

remaining = budget_remaining()
owned_count = len(st.session_state.wins)
unfilled = max(int(st.session_state.roster_size) - owned_count, 0)
reserve = unfilled * int(st.session_state.minimum_bid)
spendable = max(remaining - reserve, 0)
m1, m2, m3, m4 = st.columns(4)
m1.metric("Budget left", f"${remaining}")
m2.metric("Roster spots left", unfilled)
m3.metric("Protected reserve", f"${reserve}")
m4.metric("Flexible dollars", f"${spendable}")

sheet_df = pd.DataFrame()
sheet_error = None
try:
    sheet_df = load_sheet(st.session_state.sheet_url, st.session_state.sheet_tab)
except Exception as exc:
    sheet_error = exc
board_drafted_names = all_drafted_players(sheet_df)
owned_names = {str(x.get("player", "")) for x in st.session_state.wins if x.get("player")}
known_drafted_names = board_drafted_names | owned_names
known_drafted_keys = {normalize(name) for name in known_drafted_names}

tab_bid, tab_roster, tab_sheet, tab_best, tab_values = st.tabs(["Live decision", "My roster", "Draft monitor", "Best available", "Source values"])
with tab_bid:
    st.subheader("Commissioner console")
    st.write("Ask in plain language during the auction. Include the current high bid when one exists.")
    with st.form("commissioner_console", clear_on_submit=True):
        commissioner_message = st.text_input(
            "Live auction message",
            placeholder="Trevor Lawrence is at $20. Should I bid?",
        )
        ask = st.form_submit_button("Get bid recommendation", type="primary", use_container_width=True)
    if ask:
        message_player, match_confidence = find_player_in_message(commissioner_message)
        amounts = re.findall(r"\$\s*(\d+)|\b(\d+)\s*dollars?\b", commissioner_message, flags=re.I)
        amount_values = [int(a or b) for a, b in amounts]
        message_bid = amount_values[-1] if amount_values else 0
        if not message_player:
            st.error("I could not match that player to the source rankings. Check the spelling or use the player picker below.")
        elif normalize(message_player) in known_drafted_keys:
            st.error(f"UNAVAILABLE — **{message_player}** is already drafted.")
            if message_player in board_drafted_names:
                st.write(f"The player appears on the **{st.session_state.sheet_tab}** draft sheet.")
            else:
                st.write("The player is already recorded on your roster.")
        else:
            message_row, message_rec = recommendation_for(message_player, message_bid)
            if match_confidence < .98:
                st.info(f"Matched your entry to **{message_player}** ({match_confidence:.0%} confidence).")
            st.markdown(f"### {message_rec.action}")
            st.write(f"**{message_player}** · Source value **\\${int(message_row['value'])}** · Maximum **\\${message_rec.max_bid}**")
            st.write(message_rec.reason)
            if not amount_values:
                st.caption("No current bid was included, so this treats the next legal bid as $1.")
            if message_rec.next_bid is not None:
                projected = remaining - message_rec.next_bid
                st.write(f"If won at that bid: **\\${projected} remaining** with **{max(unfilled - 1, 0)} roster spots left**.")
                if st.button(f"Record {message_player} won for ${message_rec.next_bid}", key="console_win"):
                    add_win(message_player, message_row["position_rank"], message_rec.next_bid, "commissioner console")
                    st.rerun()
            if message_rec.provisional:
                st.warning("PROVISIONAL — confirm roster size and minimum bid in League setup.")

    st.divider()
    st.subheader("Quick player picker")
    left, right = st.columns([1.1, .9])
    with left:
        quick_entry = st.text_input(
            "Player name",
            placeholder="Trevor Lawerence",
            help="Exact spelling is not required. The matched source player appears below.",
            key="quick_player_entry",
        )
        selected_name, quick_confidence = match_player_name(quick_entry, cutoff=.70)
        if quick_entry and selected_name:
            st.caption(f"Matched to **{selected_name}** · {quick_confidence:.0%} confidence")
        elif quick_entry:
            st.warning("No confident source match. Try adding the player's first or last name.")
        current_bid = st.number_input("Current bid", min_value=0, max_value=200, value=0, step=1)
        row = None
        rec = None
        selected_unavailable = bool(selected_name and normalize(selected_name) in known_drafted_keys)
        if selected_name and not selected_unavailable:
            row, rec = recommendation_for(selected_name, current_bid)
    with right:
        st.subheader("Call")
        if selected_unavailable:
            st.error(f"UNAVAILABLE — {selected_name} is already drafted.")
            st.caption(f"Found on the {st.session_state.sheet_tab} sheet or your recorded roster.")
        elif rec is None:
            st.info("Select a player to get the next legal bid and maximum price.")
        else:
            color = "#27AE60" if rec.action.startswith("BID") else "#E67E22" if rec.action.startswith("STOP") else "#C0392B"
            st.markdown(f'<div style="padding:22px;border-radius:14px;background:{color};color:white"><div style="font-size:2.1rem;font-weight:800">{rec.action}</div><div style="margin-top:8px">{rec.reason}</div></div>', unsafe_allow_html=True)
        if row and rec:
            st.write(f"**Source:** #{int(row['rank'])} overall · {row['position_rank']} · \\${int(row['value'])}")
            st.write(f"**Ceiling:** \\${rec.max_bid}" + (" (source + \\$3 target allowance)" if selected_name in TARGET_QBS else " (never above source)"))
        if rec and rec.provisional:
            st.warning("PROVISIONAL — confirm roster size and minimum bid.")
        if selected_name and rec and rec.next_bid is not None and st.button(f"Record win at ${rec.next_bid}", type="primary", use_container_width=True):
            add_win(selected_name, row["position_rank"], rec.next_bid)
            st.rerun()

with tab_roster:
    st.subheader("Players won")
    if st.session_state.wins:
        edited = st.data_editor(pd.DataFrame(st.session_state.wins), hide_index=True, use_container_width=True, num_rows="dynamic", column_config={"price": st.column_config.NumberColumn(min_value=1, step=1, format="$%d")}, key="roster_editor")
        if st.button("Apply roster edits"):
            cleaned = []
            for item in edited.to_dict("records"):
                player = "" if pd.isna(item.get("player")) else str(item.get("player", "")).strip()
                price = safe_int(item.get("price"))
                if player and price >= 1:
                    item["player"] = player
                    item["price"] = price
                    item["position_rank"] = "" if pd.isna(item.get("position_rank")) else str(item.get("position_rank", ""))
                    item["source"] = "manual" if pd.isna(item.get("source")) else str(item.get("source", "manual"))
                    cleaned.append(item)
            st.session_state.wins = cleaned
            st.rerun()
    else:
        st.info("No purchases recorded yet.")
    with st.expander("Add a purchase manually"):
        manual_name = st.text_input("Player name", key="manual_player")
        manual_match, manual_confidence = match_player_name(manual_name, cutoff=.70)
        found = rankings[rankings["player"] == manual_match] if manual_match else rankings.iloc[0:0]
        if manual_name and manual_match:
            st.caption(f"Matched to **{manual_match}** · {manual_confidence:.0%} confidence")
        manual_pos = found.iloc[0]["position_rank"] if not found.empty else st.selectbox("Position", ["QB", "RB", "WR", "TE", "K", "DST"])
        manual_price = st.number_input("Price paid", 1, 200, 1)
        if st.button("Add purchase"):
            add_win(manual_match or manual_name, str(manual_pos), manual_price)
            st.rerun()
    qbs = sum(base_position(x["position_rank"]) == "QB" for x in st.session_state.wins)
    top_rb = False
    for x in st.session_state.wins:
        source = rankings[rankings["key"] == normalize(x["player"])]
        top_rb |= bool(not source.empty and source.iloc[0]["base_position"] == "RB" and int(source.iloc[0]["position_rank"][2:]) <= 10)
    g1, g2 = st.columns(2)
    if qbs >= 2:
        g1.success(f"Strong QB goal: {qbs}/2")
    else:
        g1.warning(f"Strong QB goal: {qbs}/2")
    if top_rb:
        g2.success("Top-10 RB secured")
    else:
        g2.warning("Top-10 eligible RB still needed")

with tab_sheet:
    st.subheader(f"Google Sheet · {st.session_state.sheet_tab}")
    if sheet_error is None:
        st.success(f"Live feed connected · {len(sheet_df)} rows")
        st.dataframe(sheet_df, use_container_width=True, hide_index=True, height=420)
        if len(sheet_df.columns):
            detected_col = detect_player_column(sheet_df)
            saved_col = st.session_state.sheet_player_col
            default_col = saved_col if saved_col in sheet_df.columns else detected_col
            default_index = list(sheet_df.columns).index(default_col) if default_col in sheet_df.columns else 0
            st.session_state.sheet_player_col = st.selectbox(
                "My roster/owner column",
                list(sheet_df.columns),
                index=default_index,
                help="Choose your team column for roster importing. Best Available scans the entire draft board.",
            )
            board_count = len(all_drafted_players(sheet_df))
            my_count = sheet_df[st.session_state.sheet_player_col].map(recognized_player).notna().sum()
            st.caption(f"{board_count} drafted players recognized across the board · {my_count} in the selected roster column.")
            with st.expander("Import my purchases from this tab"):
                cols = ["—"] + list(sheet_df.columns)
                player_col = st.selectbox("Player column", cols)
                price_col = st.selectbox("Winning price column", cols)
                team_col = st.selectbox("Fantasy team/owner column", cols)
                my_team = st.text_input("My fantasy team/owner name")
                if st.button("Import matching rows", disabled="—" in (player_col, price_col, team_col) or not my_team):
                    picked = sheet_df[sheet_df[team_col].astype(str).str.strip().str.casefold() == my_team.strip().casefold()]
                    count = 0
                    for _, item in picked.iterrows():
                        source = rankings[rankings["key"] == normalize(item[player_col])]
                        if source.empty:
                            continue
                        price = safe_int(item[price_col])
                        if price > 0:
                            add_win(source.iloc[0]["player"], source.iloc[0]["position_rank"], price, "Google Sheet")
                            count += 1
                    st.success(f"Imported {count} recognized purchases.")
                    st.rerun()
    else:
        st.error(f"Live feed unavailable: {sheet_error}")
        st.info("For a private sheet, configure a read-only Google service account as described in README.md.")

with tab_best:
    st.subheader("Best available players")
    drafted_names = set(board_drafted_names)
    player_col = st.session_state.sheet_player_col
    drafted_names.update(x.get("player", "") for x in st.session_state.wins)
    unavailable_keys = {normalize(x) for x in drafted_names}
    avoid_keys = {normalize(x) for x in st.session_state.forbidden}
    available = rankings[~rankings["key"].isin(unavailable_keys | avoid_keys)].copy()
    available["max_bid"] = available.apply(
        lambda item: min(
            int(item["value"]) + (3 if item["player"] in TARGET_QBS else 0),
            max(0, remaining - max(unfilled - 1, 0) * int(st.session_state.minimum_bid)),
        ), axis=1,
    )
    available["affordable"] = available["max_bid"] >= int(st.session_state.minimum_bid)

    if sheet_error:
        st.warning("The live sheet is unavailable, so availability currently uses only purchases recorded in My roster.")
    elif sheet_df.empty:
        st.warning("No live draft rows were loaded. Availability currently uses only purchases recorded in My roster.")
    else:
        st.caption(f"Scanning all columns in **{st.session_state.sheet_tab}** · {len(drafted_names)} drafted players removed")

    if available.empty:
        st.error("No available ranked players remain after applying the draft sheet and avoid list.")
    else:
        best_overall = available.iloc[0]
        qbs_owned = sum(base_position(x.get("position_rank", "")) == "QB" for x in st.session_state.wins)
        has_top10_rb = False
        for win in st.session_state.wins:
            source = rankings[rankings["key"] == normalize(win.get("player", ""))]
            has_top10_rb |= bool(not source.empty and source.iloc[0]["base_position"] == "RB" and safe_int(source.iloc[0]["position_rank"][2:], 999) <= 10)

        roster_fit = best_overall
        fit_reason = "highest-ranked available player"
        eligible_target_qbs = available[(available["player"].isin(TARGET_QBS)) & available["affordable"]]
        eligible_top_rbs = available[(available["base_position"] == "RB") & pd.to_numeric(available["position_rank"].str.extract(r"(\d+)")[0], errors="coerce").le(10) & available["affordable"]]
        if qbs_owned < 2 and not eligible_target_qbs.empty:
            roster_fit = eligible_target_qbs.iloc[0]
            fit_reason = "priority target while you still need two strong QBs"
        elif not has_top10_rb and not eligible_top_rbs.empty:
            roster_fit = eligible_top_rbs.iloc[0]
            fit_reason = "best eligible option for the required top-10 RB goal"

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### Best overall")
            st.metric(best_overall["player"], f"${int(best_overall['value'])} source value", f"#{int(best_overall['rank'])} overall")
            st.caption(f"{best_overall['position_rank']} · Maximum \\${int(best_overall['max_bid'])}")
        with c2:
            st.markdown("#### Best roster fit")
            st.metric(roster_fit["player"], f"${int(roster_fit['max_bid'])} maximum", f"#{int(roster_fit['rank'])} overall")
            st.caption(f"{roster_fit['position_rank']} · {fit_reason.capitalize()}.")

        st.markdown("#### Best available by position")
        position_rows = []
        for position in ["QB", "RB", "WR", "TE"]:
            group = available[available["base_position"] == position]
            if not group.empty:
                item = group.iloc[0]
                position_rows.append({"Position": position, "Player": item["player"], "Overall rank": int(item["rank"]), "Source value": int(item["value"]), "Maximum bid": int(item["max_bid"])})
        st.dataframe(pd.DataFrame(position_rows), hide_index=True, use_container_width=True, column_config={"Source value": st.column_config.NumberColumn(format="$%d"), "Maximum bid": st.column_config.NumberColumn(format="$%d")})

        target_view = available[available["player"].isin(TARGET_QBS)][["player", "rank", "position_rank", "value", "max_bid"]]
        top_rb_view = eligible_top_rbs[["player", "rank", "position_rank", "value", "max_bid"]]
        d1, d2 = st.columns(2)
        with d1:
            st.markdown("#### Remaining target QBs")
            if not target_view.empty:
                st.dataframe(target_view, hide_index=True, use_container_width=True)
            else:
                st.success("All target QBs are drafted or unavailable.")
        with d2:
            st.markdown("#### Eligible top-10 RBs")
            if not top_rb_view.empty:
                st.dataframe(top_rb_view, hide_index=True, use_container_width=True)
            else:
                st.warning("No eligible top-10 RB remains in the source pool.")

with tab_values:
    st.subheader("Authoritative source values")
    st.caption("The Ringer's SuperFlex Fantasy Football Rankings · Updated August 3, 2026")
    view = rankings[["rank", "player", "team", "position_rank", "value"]].copy()
    view["status"] = view["player"].map(lambda x: "AVOID" if normalize(x) in {normalize(y) for y in st.session_state.forbidden} else "TARGET +$3" if x in TARGET_QBS else "")
    st.dataframe(view, hide_index=True, use_container_width=True, height=520, column_config={"value": st.column_config.NumberColumn("Value", format="$%d")})

from __future__ import annotations
from difflib import get_close_matches
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

def find_player_in_message(message):
    """Resolve a ranked player from a natural-language commissioner message."""
    message_key = normalize(message)
    exact_mentions = [name for name in rankings["player"] if normalize(name) in message_key]
    if exact_mentions:
        return max(exact_mentions, key=len)
    words = re.sub(r"[^a-zA-Z0-9' -]", " ", message).split()
    matches = get_close_matches(" ".join(words), rankings["player"].tolist(), n=1, cutoff=.72)
    if matches:
        return matches[0]
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            candidate = " ".join(words[i:i + size])
            matches = get_close_matches(candidate, rankings["player"].tolist(), n=1, cutoff=.86)
            if matches:
                return matches[0]
    return None

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
    """Return the authoritative player name represented by a sheet cell."""
    if pd.isna(value):
        return None
    cell_key = normalize(value)
    if not cell_key:
        return None
    exact = rankings[rankings["key"] == cell_key]
    if not exact.empty:
        return exact.iloc[0]["player"]
    contained = rankings[rankings["key"].map(lambda key: len(key) >= 5 and key in cell_key)]
    if not contained.empty:
        return contained.sort_values("key", key=lambda col: col.str.len(), ascending=False).iloc[0]["player"]
    return None

def detect_player_column(frame):
    """Pick the sheet column containing the most recognized ranked players."""
    if frame.empty:
        return None
    scores = {}
    for column in frame.columns:
        scores[column] = frame[column].head(300).map(recognized_player).notna().sum()
    best = max(scores, key=scores.get) if scores else None
    return best if best is not None and scores[best] > 0 else None

init_state()
rankings = load_rankings()
st.title("SuperFlex Auction Proxy")
st.caption("12 teams · Half-PPR · $200 budget · Ringer 2026 SuperFlex values · $1 bid increments")

with st.sidebar:
    st.header("Live draft source")
    st.session_state.sheet_url = st.text_input("Google Sheet URL", st.session_state.sheet_url)
    st.session_state.sheet_tab = st.text_input("Tab name", st.session_state.sheet_tab)
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
        message_player = find_player_in_message(commissioner_message)
        amounts = re.findall(r"\$\s*(\d+)|\b(\d+)\s*dollars?\b", commissioner_message, flags=re.I)
        amount_values = [int(a or b) for a, b in amounts]
        message_bid = amount_values[-1] if amount_values else 0
        if not message_player:
            st.error("I could not match that player to the source rankings. Check the spelling or use the player picker below.")
        else:
            message_row, message_rec = recommendation_for(message_player, message_bid)
            st.markdown(f"### {message_rec.action}")
            st.write(f"**{message_player}** · Source value **${int(message_row['value'])}** · Maximum **${message_rec.max_bid}**")
            st.write(message_rec.reason)
            if not amount_values:
                st.caption("No current bid was included, so this treats the next legal bid as $1.")
            if message_rec.next_bid is not None:
                projected = remaining - message_rec.next_bid
                st.write(f"If won at that bid: **${projected} remaining** with **{max(unfilled - 1, 0)} roster spots left**.")
                if st.button(f"Record {message_player} won for ${message_rec.next_bid}", key="console_win"):
                    add_win(message_player, message_row["position_rank"], message_rec.next_bid, "commissioner console")
                    st.rerun()
            if message_rec.provisional:
                st.warning("PROVISIONAL — confirm roster size and minimum bid in League setup.")

    st.divider()
    st.subheader("Quick player picker")
    left, right = st.columns([1.1, .9])
    with left:
        selected_name = st.selectbox(
            "Player",
            rankings["player"].tolist(),
            index=None,
            placeholder="Type a player name...",
            help="Start typing to search the 200 players in the authoritative source.",
        )
        current_bid = st.number_input("Current bid", min_value=0, max_value=200, value=0, step=1)
        row = None
        rec = None
        if selected_name:
            row, rec = recommendation_for(selected_name, current_bid)
    with right:
        st.subheader("Call")
        if rec is None:
            st.info("Select a player to get the next legal bid and maximum price.")
        else:
            color = "#27AE60" if rec.action.startswith("BID") else "#E67E22" if rec.action.startswith("STOP") else "#C0392B"
            st.markdown(f'<div style="padding:22px;border-radius:14px;background:{color};color:white"><div style="font-size:2.1rem;font-weight:800">{rec.action}</div><div style="margin-top:8px">{rec.reason}</div></div>', unsafe_allow_html=True)
        if row and rec:
            st.write(f"**Source:** #{int(row['rank'])} overall · {row['position_rank']} · ${int(row['value'])}")
            st.write(f"**Ceiling:** ${rec.max_bid}" + (" (source + $3 target allowance)" if selected_name in TARGET_QBS else " (never above source)"))
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
        found = rankings[rankings["key"] == normalize(manual_name)] if manual_name else rankings.iloc[0:0]
        manual_pos = found.iloc[0]["position_rank"] if not found.empty else st.selectbox("Position", ["QB", "RB", "WR", "TE", "K", "DST"])
        manual_price = st.number_input("Price paid", 1, 200, 1)
        if st.button("Add purchase"):
            add_win(manual_name, str(manual_pos), manual_price)
            st.rerun()
    qbs = sum(base_position(x["position_rank"]) == "QB" for x in st.session_state.wins)
    top_rb = False
    for x in st.session_state.wins:
        source = rankings[rankings["key"] == normalize(x["player"])]
        top_rb |= bool(not source.empty and source.iloc[0]["base_position"] == "RB" and int(source.iloc[0]["position_rank"][2:]) <= 10)
    g1, g2 = st.columns(2)
    g1.success(f"Strong QB goal: {qbs}/2") if qbs >= 2 else g1.warning(f"Strong QB goal: {qbs}/2")
    g2.success("Top-10 RB secured") if top_rb else g2.warning("Top-10 eligible RB still needed")

sheet_df = pd.DataFrame()
sheet_error = None
with tab_sheet:
    st.subheader(f"Google Sheet · {st.session_state.sheet_tab}")
    try:
        sheet_df = load_sheet(st.session_state.sheet_url, st.session_state.sheet_tab)
        st.success(f"Live feed connected · {len(sheet_df)} rows")
        st.dataframe(sheet_df, use_container_width=True, hide_index=True, height=420)
        if len(sheet_df.columns):
            detected_col = detect_player_column(sheet_df)
            saved_col = st.session_state.sheet_player_col
            default_col = saved_col if saved_col in sheet_df.columns else detected_col
            default_index = list(sheet_df.columns).index(default_col) if default_col in sheet_df.columns else 0
            st.session_state.sheet_player_col = st.selectbox(
                "Player column used to track drafted players",
                list(sheet_df.columns),
                index=default_index,
                help="Every recognized player in this column is removed from Best Available.",
            )
            recognized_count = sheet_df[st.session_state.sheet_player_col].map(recognized_player).notna().sum()
            st.caption(f"{recognized_count} drafted players recognized for availability tracking.")
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
    except Exception as exc:
        sheet_error = exc
        st.error(f"Live feed unavailable: {exc}")
        st.info("For a private sheet, configure a read-only Google service account as described in README.md.")

with tab_best:
    st.subheader("Best available players")
    drafted_names = set()
    player_col = st.session_state.sheet_player_col
    if not sheet_df.empty and player_col in sheet_df.columns:
        drafted_names = {name for name in sheet_df[player_col].map(recognized_player) if name}
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
    elif not player_col:
        st.warning("Choose the player column in Draft Monitor to remove drafted players automatically.")
    else:
        st.caption(f"Using **{player_col}** from **{st.session_state.sheet_tab}** · {len(drafted_names)} drafted players removed")

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
            st.caption(f"{best_overall['position_rank']} · Maximum ${int(best_overall['max_bid'])}")
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
            st.dataframe(target_view, hide_index=True, use_container_width=True) if not target_view.empty else st.success("All target QBs are drafted or unavailable.")
        with d2:
            st.markdown("#### Eligible top-10 RBs")
            st.dataframe(top_rb_view, hide_index=True, use_container_width=True) if not top_rb_view.empty else st.warning("No eligible top-10 RB remains in the source pool.")

with tab_values:
    st.subheader("Authoritative source values")
    st.caption("The Ringer's SuperFlex Fantasy Football Rankings · Updated August 3, 2026")
    view = rankings[["rank", "player", "team", "position_rank", "value"]].copy()
    view["status"] = view["player"].map(lambda x: "AVOID" if normalize(x) in {normalize(y) for y in st.session_state.forbidden} else "TARGET +$3" if x in TARGET_QBS else "")
    st.dataframe(view, hide_index=True, use_container_width=True, height=520, column_config={"value": st.column_config.NumberColumn("Value", format="$%d")})

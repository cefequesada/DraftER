from __future__ import annotations
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
    defaults = {"wins": [], "budget": 200, "roster_size": 16, "minimum_bid": 1, "setup_confirmed": False, "forbidden": sorted(FORBIDDEN_DEFAULT), "sheet_url": SHEET_DEFAULT, "sheet_tab": TAB_DEFAULT}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)

def budget_remaining():
    return int(st.session_state.budget) - sum(int(x["price"]) for x in st.session_state.wins)

def add_win(player, position_rank, price, source="manual"):
    if not any(normalize(x["player"]) == normalize(player) for x in st.session_state.wins):
        st.session_state.wins.append({"player": player, "position_rank": position_rank, "price": int(price), "source": source})

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

tab_bid, tab_roster, tab_sheet, tab_values = st.tabs(["Live decision", "My roster", "Draft monitor", "Source values"])
with tab_bid:
    left, right = st.columns([1.1, .9])
    with left:
        st.subheader("Nomination")
        query = st.text_input("Player", placeholder="Trevor Lawrence")
        matches = rankings[rankings["player"].str.contains(query, case=False, regex=False)] if query else rankings.iloc[0:0]
        choices = matches["player"].tolist()
        selected_name = st.selectbox("Source match", choices, index=0) if choices else None
        current_bid = st.number_input("Current bid", min_value=0, max_value=200, value=0, step=1)
        row = None
        owned_markers = []
        for win in st.session_state.wins:
            pos = base_position(win["position_rank"])
            owned_markers.append(pos)
            source = rankings[rankings["key"] == normalize(win["player"])]
            if pos == "RB" and not source.empty and int(source.iloc[0]["position_rank"][2:]) <= 10:
                owned_markers.append("TOP10_RB")
        if selected_name:
            row = rankings.loc[rankings["player"] == selected_name].iloc[0].to_dict()
            eligible_top10 = rankings[(rankings["base_position"] == "RB") & rankings["position_rank"].str.extract(r"(\\d+)")[0].astype(int).le(10) & ~rankings["key"].isin({normalize(x) for x in st.session_state.forbidden}) & ~rankings["key"].isin({normalize(x["player"]) for x in st.session_state.wins})]
            rec = recommend(row, current_bid, remaining, int(st.session_state.roster_size), owned_count, owned_markers, set(st.session_state.forbidden), st.session_state.setup_confirmed, int(st.session_state.minimum_bid), len(eligible_top10))
        else:
            rec = recommend(None, current_bid, remaining, int(st.session_state.roster_size), owned_count, owned_markers, set(st.session_state.forbidden), st.session_state.setup_confirmed, int(st.session_state.minimum_bid))
    with right:
        st.subheader("Call")
        color = "#27AE60" if rec.action.startswith("BID") else "#E67E22" if rec.action.startswith("STOP") else "#C0392B"
        st.markdown(f'<div style="padding:22px;border-radius:14px;background:{color};color:white"><div style="font-size:2.1rem;font-weight:800">{rec.action}</div><div style="margin-top:8px">{rec.reason}</div></div>', unsafe_allow_html=True)
        if row:
            st.write(f"**Source:** #{int(row['rank'])} overall · {row['position_rank']} · ${int(row['value'])}")
            st.write(f"**Ceiling:** ${rec.max_bid}" + (" (source + $3 target allowance)" if selected_name in TARGET_QBS else " (never above source)"))
        if rec.provisional:
            st.warning("PROVISIONAL — confirm roster size and minimum bid.")
        if selected_name and rec.next_bid is not None and st.button(f"Record win at ${rec.next_bid}", type="primary", use_container_width=True):
            add_win(selected_name, row["position_rank"], rec.next_bid)
            st.rerun()

with tab_roster:
    st.subheader("Players won")
    if st.session_state.wins:
        edited = st.data_editor(pd.DataFrame(st.session_state.wins), hide_index=True, use_container_width=True, num_rows="dynamic", column_config={"price": st.column_config.NumberColumn(min_value=1, step=1, format="$%d")}, key="roster_editor")
        if st.button("Apply roster edits"):
            st.session_state.wins = edited.to_dict("records")
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

with tab_sheet:
    st.subheader(f"Google Sheet · {st.session_state.sheet_tab}")
    try:
        sheet_df = load_sheet(st.session_state.sheet_url, st.session_state.sheet_tab)
        st.success(f"Live feed connected · {len(sheet_df)} rows")
        st.dataframe(sheet_df, use_container_width=True, hide_index=True, height=420)
        if len(sheet_df.columns):
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
                        price = int(float(re.sub(r"[^0-9.]", "", str(item[price_col])) or 0))
                        if price > 0:
                            add_win(source.iloc[0]["player"], source.iloc[0]["position_rank"], price, "Google Sheet")
                            count += 1
                    st.success(f"Imported {count} recognized purchases.")
                    st.rerun()
    except Exception as exc:
        st.error(f"Live feed unavailable: {exc}")
        st.info("For a private sheet, configure a read-only Google service account as described in README.md.")

with tab_values:
    st.subheader("Authoritative source values")
    st.caption("The Ringer's SuperFlex Fantasy Football Rankings · Updated August 3, 2026")
    view = rankings[["rank", "player", "team", "position_rank", "value"]].copy()
    view["status"] = view["player"].map(lambda x: "AVOID" if normalize(x) in {normalize(y) for y in st.session_state.forbidden} else "TARGET +$3" if x in TARGET_QBS else "")
    st.dataframe(view, hide_index=True, use_container_width=True, height=520, column_config={"value": st.column_config.NumberColumn("Value", format="$%d")})


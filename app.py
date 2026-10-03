import os
from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st

# ============================================================
# PAGE SETUP
# ============================================================

st.set_page_config(page_title="NIFTY Option OI Monitor", layout="wide")

BASE_URL = "https://api.upstox.com/v2"
NIFTY_KEY = "NSE_INDEX|Nifty 50"


# ============================================================
# TOKEN (Streamlit secrets -> env var -> sidebar input)
# ============================================================

def load_token():
    try:
        if "UPSTOX_ACCESS_TOKEN" in st.secrets:
            return st.secrets["UPSTOX_ACCESS_TOKEN"]
    except Exception:
        pass
    return os.getenv("UPSTOX_ACCESS_TOKEN", "")


with st.sidebar:
    st.header("Settings")
    token = load_token()
    if not token:
        token = st.text_input("Upstox access token", type="password")
    poll_seconds = st.slider("Refresh every (seconds)", 3, 60, 5)
    strikes_each_side = st.slider("Strikes each side of ATM", 3, 15, 5)
    move_threshold = st.number_input("Price move threshold (pts)", value=5.0, step=1.0)
    if st.button("Reset OI baseline"):
        st.session_state.pop("previous_oi", None)
        st.session_state.pop("previous_spot", None)

if not token:
    st.warning("Add UPSTOX_ACCESS_TOKEN in Streamlit secrets or paste it in the sidebar.")
    st.stop()

HEADERS = {"Accept": "application/json", "Authorization": f"Bearer {token}"}


# ============================================================
# API
# ============================================================

@st.cache_data(ttl=3600, show_spinner=False)
def get_nearest_expiry(_token: str) -> str:
    """Nearest upcoming NIFTY expiry as YYYY-MM-DD."""
    r = requests.get(
        f"{BASE_URL}/option/contract",
        headers=HEADERS,
        params={"instrument_key": NIFTY_KEY},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(data)
    today = date.today().isoformat()
    expiries = sorted({c["expiry"][:10] for c in data["data"] if c.get("expiry")})
    upcoming = [e for e in expiries if e >= today]
    if not upcoming:
        raise RuntimeError("No upcoming expiry found")
    return upcoming[0]


def get_option_chain(expiry: str):
    r = requests.get(
        f"{BASE_URL}/option/chain",
        headers=HEADERS,
        params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(data)
    return data["data"]


# ============================================================
# HELPERS
# ============================================================

def get_near_atm(chain, spot, n):
    strikes = sorted(row["strike_price"] for row in chain)
    atm = min(strikes, key=lambda x: abs(x - spot))
    i = strikes.index(atm)
    selected = set(strikes[max(0, i - n): min(len(strikes), i + n + 1)])
    return atm, [row for row in chain if row["strike_price"] in selected]


def calculate_signal(spot_change, call_oi_change, put_oi_change, threshold):
    if spot_change > threshold:
        if put_oi_change > 0 and call_oi_change < 0:
            return "BULLISH PRESSURE"
        if put_oi_change > 0:
            return "BULLISH / PUT BUILDUP"
        if call_oi_change > 0:
            return "WATCH CALL RESISTANCE"
        return "PRICE UP"
    if spot_change < -threshold:
        if call_oi_change > 0 and put_oi_change < 0:
            return "BEARISH PRESSURE"
        if call_oi_change > 0:
            return "BEARISH / CALL BUILDUP"
        if put_oi_change > 0:
            return "WATCH PUT SUPPORT"
        return "PRICE DOWN"
    return "NEUTRAL"


# ============================================================
# LIVE VIEW (auto-refreshing fragment)
# ============================================================

@st.fragment(run_every=poll_seconds)
def live_view():
    try:
        expiry = get_nearest_expiry(token)
        chain = get_option_chain(expiry)
    except Exception as e:
        st.error(f"API error: {e}")
        return

    if not chain:
        st.info("No option-chain data (market may be closed).")
        return

    spot = chain[0]["underlying_spot_price"]
    prev_spot = st.session_state.get("previous_spot")
    spot_change = 0.0 if prev_spot is None else spot - prev_spot
    st.session_state["previous_spot"] = spot

    atm, rows = get_near_atm(chain, spot, strikes_each_side)
    previous_oi = st.session_state.setdefault("previous_oi", {})

    table, tot_ce, tot_pe = [], 0, 0
    for row in sorted(rows, key=lambda x: x["strike_price"]):
        strike = row["strike_price"]
        call = row["call_options"]["market_data"]
        put = row["put_options"]["market_data"]

        c_oi, p_oi = call.get("oi", 0) or 0, put.get("oi", 0) or 0
        c_key, p_key = f"{strike}_CE", f"{strike}_PE"
        c_d = c_oi - previous_oi.get(c_key, c_oi)
        p_d = p_oi - previous_oi.get(p_key, p_oi)
        previous_oi[c_key], previous_oi[p_key] = c_oi, p_oi
        tot_ce += c_d
        tot_pe += p_d

        table.append({
            "STRIKE": f"{strike:.0f}" + (" ◀ ATM" if strike == atm else ""),
            "CE LTP": call.get("ltp", 0),
            "CE OI": c_oi,
            "CE ΔOI": c_d,
            "PE LTP": put.get("ltp", 0),
            "PE OI": p_oi,
            "PE ΔOI": p_d,
        })

    signal = calculate_signal(spot_change, tot_ce, tot_pe, move_threshold)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("NIFTY", f"{spot:,.2f}", f"{spot_change:+.2f}")
    c2.metric("Total CE ΔOI", f"{tot_ce:+,.0f}")
    c3.metric("Total PE ΔOI", f"{tot_pe:+,.0f}")
    c4.metric("Signal", signal)

    st.dataframe(
        pd.DataFrame(table),
        hide_index=True,
        use_container_width=True,
        column_config={
            "CE OI": st.column_config.NumberColumn(format="%d"),
            "PE OI": st.column_config.NumberColumn(format="%d"),
            "CE ΔOI": st.column_config.NumberColumn(format="%+d"),
            "PE ΔOI": st.column_config.NumberColumn(format="%+d"),
        },
    )
    st.caption(f"Expiry {expiry} • updated {datetime.now():%H:%M:%S} • ΔOI = change since previous refresh")


st.title("NIFTY Live Price + Option OI Monitor")
live_view()

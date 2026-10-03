import math
import os
import random
from datetime import datetime, date
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ============================================================
# CONFIG
# ============================================================
BASE_URL = "https://api.upstox.com/v2"
NIFTY_KEY = "NSE_INDEX|Nifty 50"
IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(page_title="NIFTY OI Monitor", page_icon="📈", layout="wide")


# ============================================================
# API
# ============================================================
def api_get(path, token, params):
    r = requests.get(
        f"{BASE_URL}{path}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        params=params,
        timeout=10,
    )
    if r.status_code == 401:
        raise PermissionError(
            "Upstox rejected the token (401). It expires daily (~3:30 AM IST), must be the "
            "ACCESS token (not API key/secret), and on Streamlit Cloud you must reboot the app "
            "after editing secrets. Also clear the sidebar token box if it holds an old token."
        )
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "success":
        raise RuntimeError(j)
    return j["data"]


@st.cache_data(ttl=3600, show_spinner=False)
def get_expiries(token):
    """Upcoming NIFTY expiries (YYYY-MM-DD), nearest first."""
    data = api_get("/option/contract", token, {"instrument_key": NIFTY_KEY})
    today = datetime.now(IST).date()
    out = set()
    for c in data:
        e = c.get("expiry")
        if e is None:
            continue
        if isinstance(e, (int, float)):
            e = datetime.fromtimestamp(e / 1000, IST).date().isoformat()
        e = str(e)[:10]
        if date.fromisoformat(e) >= today:
            out.add(e)
    return sorted(out)


def get_chain(token, expiry):
    return api_get("/option/chain", token, {"instrument_key": NIFTY_KEY, "expiry_date": expiry})


def get_index_quote(token):
    """NIFTY last price and net change vs previous close (still available after market close)."""
    data = api_get("/market-quote/quotes", token, {"instrument_key": NIFTY_KEY})
    q = next(iter(data.values()), {}) if isinstance(data, dict) else {}
    return {
        "last": q.get("last_price"),
        "net_change": q.get("net_change"),
        "prev_close": (q.get("ohlc") or {}).get("close"),
    }


# ============================================================
# SAMPLE DATA (Demo mode, same shape as the Upstox response)
# ============================================================
def _theo(k, spot):
    return max(4.0, 110 * math.exp(-abs(k - spot) / 160))


def _bidask(ltp, k, spot):
    """Synthetic bid/ask around LTP; spread is wider for strikes far from spot."""
    pct = random.uniform(0.003, 0.010) + min(abs(k - spot) / 8000, 0.04)
    half = max(ltp * pct, 0.05) / 2
    vol = int(random.uniform(0.5, 1.5) * 4_000_000 * math.exp(-abs(k - spot) / 250))  # busiest near ATM
    return {"bid_price": round(max(ltp - half, 0.05), 2), "ask_price": round(ltp + half, 2), "volume": vol}


def make_sample_chain():
    """Synthetic NIFTY chain. Spot random-walks and OI evolves on every refresh."""
    ss = st.session_state
    if "demo" not in ss:
        spot = 24850.0
        strikes = [24000 + 50 * i for i in range(36)]
        ce = {k: random.randint(60_000, 250_000) + (400_000 if k in (25000, 25200) else 0) for k in strikes}
        pe = {k: random.randint(60_000, 250_000) + (400_000 if k in (24700, 24500) else 0) for k in strikes}
        ss["demo"] = {
            "spot": spot, "drift": random.choice([-2, 2]), "ce": ce, "pe": pe,
            "prev_ce": {k: int(v * random.uniform(0.9, 1.1)) for k, v in ce.items()},
            "prev_pe": {k: int(v * random.uniform(0.9, 1.1)) for k, v in pe.items()},
            # previous-day close premiums so price change vs close can be computed
            "close_ce": {k: round((max(spot - k, 0) + _theo(k, spot)) * random.uniform(0.85, 1.15), 2) for k in strikes},
            "close_pe": {k: round((max(k - spot, 0) + _theo(k, spot)) * random.uniform(0.85, 1.15), 2) for k in strikes},
        }
    d = ss["demo"]
    if random.random() < 0.08:
        d["drift"] = -d["drift"]
    d["spot"] += random.gauss(d["drift"], 5)
    for k in d["ce"]:
        # uptrend -> more put writing; downtrend -> more call writing
        d["pe"][k] += int(random.gauss(2500 if d["drift"] > 0 else 500, 1500))
        d["ce"][k] += int(random.gauss(2500 if d["drift"] < 0 else 500, 1500))
        d["pe"][k], d["ce"][k] = max(d["pe"][k], 0), max(d["ce"][k], 0)

    spot, chain = d["spot"], []
    for k in d["ce"]:
        tv = _theo(k, spot)
        dc = min(max(0.5 + (spot - k) / 400, 0.02), 0.98)
        gm = round(0.0016 * math.exp(-(((spot - k) / 180) ** 2)) + 0.00005, 5)  # peaks at ATM
        chain.append({
            "strike_price": float(k),
            "underlying_spot_price": spot,
            "call_options": {
                "market_data": {"ltp": round(max(spot - k, 0) + tv, 2), "close_price": d["close_ce"][k],
                                "oi": d["ce"][k], "prev_oi": d["prev_ce"][k],
                                **_bidask(max(spot - k, 0) + tv, k, spot)},
                "option_greeks": {"iv": round(random.uniform(12, 17), 2), "delta": round(dc, 3), "gamma": gm},
            },
            "put_options": {
                "market_data": {"ltp": round(max(k - spot, 0) + tv, 2), "close_price": d["close_pe"][k],
                                "oi": d["pe"][k], "prev_oi": d["prev_pe"][k],
                                **_bidask(max(k - spot, 0) + tv, k, spot)},
                "option_greeks": {"iv": round(random.uniform(12, 17), 2), "delta": round(dc - 1, 3), "gamma": gm},
            },
        })
    return chain


# ============================================================
# DATA HELPERS
# ============================================================
def _spread(md, ltp):
    """Ask minus bid and that gap as % of LTP. NaN when the book is empty (e.g. market closed)."""
    bid, ask = md.get("bid_price") or 0, md.get("ask_price") or 0
    if bid <= 0 or ask <= 0 or ask < bid:
        return float("nan"), float("nan")
    diff = ask - bid
    return diff, (diff / ltp * 100 if ltp else float("nan"))


def build_df(chain):
    rows = []
    for it in chain:
        co, po = it.get("call_options", {}), it.get("put_options", {})
        cm, pm = co.get("market_data", {}), po.get("market_data", {})
        cg, pg = co.get("option_greeks", {}), po.get("option_greeks", {})
        ce_ltp, pe_ltp = cm.get("ltp") or 0, pm.get("ltp") or 0
        ce_close, pe_close = cm.get("close_price") or 0, pm.get("close_price") or 0
        ce_spr, ce_spr_pct = _spread(cm, ce_ltp)
        pe_spr, pe_spr_pct = _spread(pm, pe_ltp)
        rows.append(
            {
                "strike": it["strike_price"],
                "ce_ltp": ce_ltp,
                "ce_oi": cm.get("oi") or 0,
                "ce_day_doi": (cm.get("oi") or 0) - (cm.get("prev_oi") or 0),
                "ce_day_px": (ce_ltp - ce_close) if ce_close else 0,
                "ce_iv": cg.get("iv"),
                "ce_delta": cg.get("delta") or 0,
                "ce_gamma": cg.get("gamma") or 0,
                "ce_vol": cm.get("volume") or 0,
                "ce_spread": ce_spr,
                "ce_spr_pct": ce_spr_pct,
                "pe_ltp": pe_ltp,
                "pe_oi": pm.get("oi") or 0,
                "pe_day_doi": (pm.get("oi") or 0) - (pm.get("prev_oi") or 0),
                "pe_day_px": (pe_ltp - pe_close) if pe_close else 0,
                "pe_iv": pg.get("iv"),
                "pe_delta": pg.get("delta") or 0,
                "pe_gamma": pg.get("gamma") or 0,
                "pe_vol": pm.get("volume") or 0,
                "pe_spread": pe_spr,
                "pe_spr_pct": pe_spr_pct,
            }
        )
    return pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def near_atm(df, spot, n):
    atm = df.iloc[(df["strike"] - spot).abs().argmin()]["strike"]
    i = df.index[df["strike"] == atm][0]
    return atm, df.iloc[max(0, i - n): i + n + 1].copy()


def window_change(history, strikes, window):
    """Change in CE/PE OI over the last `window` refreshes, on a fixed set of strikes."""
    if len(history) < 2:
        return None
    base = history[-window - 1] if len(history) > window else history[0]
    cur = history[-1]
    ce = pe = 0
    for s in strikes:
        c_now, p_now = cur["oi"].get(s, (0, 0))
        c_old, p_old = base["oi"].get(s, (c_now, p_now))
        ce += c_now - c_old
        pe += p_now - p_old
    return {"ce": ce, "pe": pe, "spot": cur["spot"] - base["spot"]}


def strike_changes(view, history, window, basis_day):
    """Per-strike option price change and OI change for CE and PE.

    basis_day=True : price vs previous close, OI vs previous day OI (Upstox fields).
    basis_day=False: change over the last `window` refreshes (needs history).
    Returns the view with ce_px_chg, ce_oi_chg, pe_px_chg, pe_oi_chg columns.
    """
    v = view.copy()
    if basis_day or len(history) < 2:
        v["ce_px_chg"], v["pe_px_chg"] = v["ce_day_px"], v["pe_day_px"]
        v["ce_oi_chg"], v["pe_oi_chg"] = v["ce_day_doi"], v["pe_day_doi"]
        return v
    base = history[-window - 1] if len(history) > window else history[0]
    cur = history[-1]
    cp, pp, co, po = [], [], [], []
    for s in v["strike"]:
        c_px_now, p_px_now = cur.get("px", {}).get(s, (0, 0))
        c_px_old, p_px_old = base.get("px", {}).get(s, (c_px_now, p_px_now))
        c_oi_now, p_oi_now = cur["oi"].get(s, (0, 0))
        c_oi_old, p_oi_old = base["oi"].get(s, (c_oi_now, p_oi_now))
        cp.append(c_px_now - c_px_old)
        pp.append(p_px_now - p_px_old)
        co.append(c_oi_now - c_oi_old)
        po.append(p_oi_now - p_oi_old)
    v["ce_px_chg"], v["pe_px_chg"], v["ce_oi_chg"], v["pe_oi_chg"] = cp, pp, co, po
    return v


def fast_move_symbols(v, move_pts):
    """Rank strikes by how fast their premium should move, using delta and gamma (not displayed).

    For a spot move of `move_pts` in the option's favourable direction the expected premium
    change is |delta|*m + 0.5*gamma*m^2. Dividing by LTP gives the % speed. The fastest strikes
    (across both CE and PE in view) get 🚀🚀, the next tier 🚀. Strikes with a tiny delta or
    a near-zero premium are ignored so far-OTM lottery tickets do not dominate.
    """
    m = float(move_pts)
    scores = {}
    for side in ("ce", "pe"):
        s = []
        for d, g, p in zip(v[f"{side}_delta"], v[f"{side}_gamma"], v[f"{side}_ltp"]):
            if p is None or p < 1 or abs(d) < 0.15 or g <= 0:
                s.append(0.0)
            else:
                s.append((abs(d) * m + 0.5 * g * m * m) / p * 100)
        scores[side] = s
    allv = sorted([x for side in scores.values() for x in side if x > 0], reverse=True)
    if not allv:
        return ["" for _ in v["strike"]], ["" for _ in v["strike"]]
    n = len(allv)
    t_hi = allv[max(0, math.ceil(n * 0.15) - 1)]   # top ~15%
    t_mid = allv[max(0, math.ceil(n * 0.35) - 1)]  # next ~20%

    def sym(x):
        if x <= 0:
            return ""
        if x >= t_hi:
            return "🚀🚀"
        if x >= t_mid:
            return "🚀"
        return ""

    return [sym(x) for x in scores["ce"]], [sym(x) for x in scores["pe"]]


def fmt_indian(n):
    """Volume in Indian units: 1 L = 1,00,000 and 1 Cr = 1,00,00,000."""
    if n is None or pd.isna(n):
        return "-"
    if n >= 1e7:
        return f"{n / 1e7:.2f} Cr"
    if n >= 1e5:
        return f"{n / 1e5:.2f} L"
    return f"{n:,.0f}"


def spread_text(diff, pct, wide):
    """Plain-text spread: '₹ gap (% of price) Cheap/Costly'. '-' when there is no bid/ask."""
    if diff is None or pct is None or pd.isna(diff) or pd.isna(pct):
        return "-"
    return f"{diff:.2f} ({pct:.1f}%) {'Cheap' if pct <= wide else 'Costly'}"


def classify_buildup(px_chg, oi_chg):
    """Classic OI interpretation, applied to the option's own premium.

    Price up + OI up   -> Long Buildup
    Price down + OI up -> Short Buildup
    Price up + OI down -> Short Covering
    Price down + OI down -> Long Unwinding
    """
    if px_chg is None or oi_chg is None or px_chg == 0 or oi_chg == 0:
        return "-"
    if px_chg > 0 and oi_chg > 0:
        return "Long Buildup"
    if px_chg < 0 and oi_chg > 0:
        return "Short Buildup"
    if px_chg > 0 and oi_chg < 0:
        return "Short Covering"
    return "Long Unwinding"


BUILDUP_MEANING = {
    "CE": {
        "Long Buildup": "New call buyers",
        "Short Buildup": "New call sellers",
        "Short Covering": "Call sellers exiting",
        "Long Unwinding": "Call buyers exiting",
    },
    "PE": {
        "Long Buildup": "New put buyers",
        "Short Buildup": "New put sellers",
        "Short Covering": "Put sellers exiting",
        "Long Unwinding": "Put buyers exiting",
    },
}
BUILDUP_SEP = " - "


def buildup_text(side, label):
    """'Long Buildup - New call buyers' (side is 'CE' or 'PE'). '-' when there is no signal."""
    if label not in BUILDUP_MEANING[side]:
        return "-"
    return f"{label}{BUILDUP_SEP}{BUILDUP_MEANING[side][label]}"


BUILDUP_STYLE = {
    "Long Buildup": "background-color: rgba(0,170,80,0.35); font-weight: 600",
    "Short Buildup": "background-color: rgba(220,40,40,0.35); font-weight: 600",
    "Short Covering": "background-color: rgba(60,130,255,0.35); font-weight: 600",
    "Long Unwinding": "background-color: rgba(255,140,0,0.35); font-weight: 600",
}

# --- Market View: CE + PE buildup read together (Master Combination Table) ---
# (CE label, PE label) -> market view, exactly as in the table
MARKET_VIEW = {
    ("Long Buildup", "Short Buildup"): "Strong Bullish",                  # call buyers active + put writers active
    ("Short Covering", "Short Buildup"): "Strong Bullish / Fast Upside",  # call writers trapped + put writers supporting
    ("Long Buildup", "Long Unwinding"): "Bullish",                        # call buying + put buyers exiting
    ("Short Covering", "Long Unwinding"): "Bullish Relief",               # call sellers exiting + fear reducing
    ("Short Buildup", "Long Buildup"): "Strong Bearish",                  # call writers active + put buyers active
    ("Long Unwinding", "Long Buildup"): "Bearish",                        # call buyers exiting + put buyers active
}
# Each side's lean, used only for combinations that are not in the table above
CE_LEAN = {"Long Buildup": 1, "Short Covering": 1, "Short Buildup": -1, "Long Unwinding": -1}
PE_LEAN = {"Short Buildup": 1, "Long Unwinding": 1, "Long Buildup": -1, "Short Covering": -1}


def market_view(ce_label, pe_label):
    """Bullish / Bearish view for one strike from its CE and PE buildup labels."""
    if ce_label not in CE_LEAN or pe_label not in PE_LEAN:
        return "-"
    exact = MARKET_VIEW.get((ce_label, pe_label))
    if exact:
        return exact
    total = CE_LEAN[ce_label] + PE_LEAN[pe_label]
    if total > 0:
        return "Bullish"
    if total < 0:
        return "Bearish"
    return "Mixed - stay cautious"  # CE and PE point in opposite directions


def market_view_text(ce_label, pe_label):
    view = market_view(ce_label, pe_label)
    icon = "🟢" if "Bullish" in view else "🔴" if "Bearish" in view else "🟡" if view != "-" else ""
    return f"{icon} {view}".strip()


# ============================================================
# SIGNAL ENGINES
# ============================================================
def oi_pattern_signal(spot_change, call_oi_change, put_oi_change, thr):
    """ORIGINAL rule set from the first script (now measured over the window)."""
    if spot_change > thr:
        if put_oi_change > 0 and call_oi_change < 0:
            return "BULLISH PRESSURE"
        if put_oi_change > 0:
            return "BULLISH / PUT BUILDUP"
        if call_oi_change > 0:
            return "WATCH CALL RESISTANCE"
        return "PRICE UP"
    if spot_change < -thr:
        if call_oi_change > 0 and put_oi_change < 0:
            return "BEARISH PRESSURE"
        if call_oi_change > 0:
            return "BEARISH / CALL BUILDUP"
        if put_oi_change > 0:
            return "WATCH PUT SUPPORT"
        return "PRICE DOWN"
    return "NEUTRAL"


def calculate_score(chg, pcr, thr_points):
    """Score -3..+3 from spot move, OI build-up balance and PCR."""
    if chg is None:
        return "COLLECTING DATA", 0, ["Need at least 2 refreshes to compute change."]

    score, why = 0, []

    if chg["spot"] > thr_points:
        score += 1
        why.append(f"Spot up {chg['spot']:+.1f} pts over the window")
    elif chg["spot"] < -thr_points:
        score -= 1
        why.append(f"Spot down {chg['spot']:+.1f} pts over the window")
    else:
        why.append(f"Spot flat ({chg['spot']:+.1f} pts)")

    activity = abs(chg["ce"]) + abs(chg["pe"])
    if activity > 0:
        ratio = (chg["pe"] - chg["ce"]) / activity
        if ratio > 0.2:
            score += 1
            why.append("Put OI building faster than Call OI (put writing = support)")
        elif ratio < -0.2:
            score -= 1
            why.append("Call OI building faster than Put OI (call writing = resistance)")
        else:
            why.append("Call and Put OI changes are balanced")

    if pcr > 1.2:
        score += 1
        why.append(f"PCR {pcr:.2f} (> 1.2, supportive)")
    elif pcr < 0.8:
        score -= 1
        why.append(f"PCR {pcr:.2f} (< 0.8, weak)")
    else:
        why.append(f"PCR {pcr:.2f} (neutral)")

    label = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"
    return label, score, why


# ============================================================
# STRIKE IDEAS
# ============================================================
def strike_ideas(label, atm, spot, df, sl_pct, tgt_pct):
    atm_row = df[df["strike"] == atm].iloc[0]
    below = df[df["strike"] <= spot]
    above = df[df["strike"] >= spot]
    support = below.loc[below["pe_oi"].idxmax(), "strike"] if len(below) else atm
    resist = above.loc[above["ce_oi"].idxmax(), "strike"] if len(above) else atm

    ideas = []
    if label == "BULLISH":
        p = atm_row["ce_ltp"]
        ideas.append(("BUY", f"{atm:.0f} CE", p, p * (1 - sl_pct / 100), p * (1 + tgt_pct / 100),
                      "ATM call for a directional bullish view."))
        sp = df[df["strike"] == support].iloc[0]["pe_ltp"]
        ideas.append(("SELL", f"{support:.0f} PE", sp, None, None,
                      "Put at highest-OI support. Short options carry large risk; use a hedge/stop."))
    elif label == "BEARISH":
        p = atm_row["pe_ltp"]
        ideas.append(("BUY", f"{atm:.0f} PE", p, p * (1 - sl_pct / 100), p * (1 + tgt_pct / 100),
                      "ATM put for a directional bearish view."))
        sp = df[df["strike"] == resist].iloc[0]["ce_ltp"]
        ideas.append(("SELL", f"{resist:.0f} CE", sp, None, None,
                      "Call at highest-OI resistance. Short options carry large risk; use a hedge/stop."))
    return ideas, support, resist


# ============================================================
# SIDEBAR
# ============================================================
st.sidebar.header("Settings")

try:
    default_token = st.secrets.get("UPSTOX_ACCESS_TOKEN", "")
except Exception:
    default_token = ""
default_token = (default_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")).strip().strip("\"'").strip()

demo = st.sidebar.checkbox("Demo mode (sample data, no token)", value=not default_token)
token_in = st.sidebar.text_input("Upstox access token", value=default_token, type="password", disabled=demo)

# clean common paste mistakes: spaces, quotes, "Bearer " prefix
token = (token_in or "").strip().strip("\"'").strip()
if token.lower().startswith("bearer "):
    token = token[7:].strip()
if token and not demo:
    src = "secrets/env" if token == default_token else "sidebar box"
    st.sidebar.caption(f"Using token from {src}: length {len(token)}, ends with …{token[-4:]}")
    if len(token) < 100:
        st.sidebar.warning(
            "Upstox access tokens are long (200+ chars, JWT starting with 'eyJ'). "
            "This looks like an API key/secret instead."
        )

refresh_s = st.sidebar.slider("Refresh every (sec)", 3, 60, 5)
n_side = st.sidebar.slider("Strikes each side of ATM", 2, 15, 5)
window = st.sidebar.slider("Signal window (refreshes)", 2, 60, 12)
thr_pts = st.sidebar.number_input("Spot move threshold (pts)", 1.0, 100.0, 10.0)
sl_pct = st.sidebar.number_input("Stop-loss on bought premium (%)", 5, 90, 25)
tgt_pct = st.sidebar.number_input("Target on bought premium (%)", 5, 300, 50)
fast_pts = st.sidebar.slider("Fast-move test: NIFTY move (pts)", 5, 100, 20)
wide_pct = st.sidebar.slider("Costly spread above (% of premium)", 0.5, 10.0, 2.0, 0.5)

if st.sidebar.button("Reset history"):
    st.session_state.pop("history", None)
    st.session_state.pop("demo", None)

# ============================================================
# MAIN
# ============================================================
st.title("📈 NIFTY 50 Live Price + Option OI Monitor")

if not demo and not token:
    st.info("Enter your Upstox access token, or tick Demo mode, in the sidebar.")
    st.stop()

now = datetime.now(IST)
open_now = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
live = demo or open_now  # live = real-time mode; otherwise show last session's data

# Fast refresh while the market is open; slow check while closed (switches to live by itself at 9:15 IST)
st_autorefresh(interval=(refresh_s if live else 60) * 1000, key="auto")

# Buildup basis (only meaningful while live; closed market always uses day basis)
if live:
    basis_choice = st.sidebar.radio(
        "Buildup basis (chain table)",
        ["Day (vs prev close)", "Signal window (refreshes)"],
        index=0,
    )
    basis_day = basis_choice.startswith("Day")
else:
    basis_day = True

try:
    if demo:
        st.info("🧪 Demo mode: synthetic sample data, not real market prices.")
        expiry = st.sidebar.selectbox("Expiry", ["DEMO"], index=0)
        chain = make_sample_chain()
    else:
        expiries = get_expiries(token)
        if not expiries:
            st.error("No upcoming NIFTY expiries returned.")
            st.stop()
        expiry = st.sidebar.selectbox("Expiry", expiries, index=0)
        chain = get_chain(token, expiry)
except Exception as e:
    st.error(f"API error: {e}")
    st.stop()

if not chain:
    st.warning("No option-chain data returned.")
    st.stop()

quote = None
if not live:
    st.info(
        "🔒 Market closed (NSE Mon-Fri 9:15-15:30 IST). Showing the **last traded data** from the "
        "previous session. The signal uses full-day OI change and the day's price change. "
        "Live refresh starts automatically when the market opens (checked every 60 s). "
        "On exchange holidays the data will also be from the last session."
    )
    try:
        quote = get_index_quote(token)
    except Exception:
        quote = None  # price change just won't be shown

spot = chain[0]["underlying_spot_price"]
df = build_df(chain)
atm, view = near_atm(df, spot, n_side)

# --- history (kept per browser session) ---
hist = st.session_state.setdefault("history", [])
if live and (not hist or (now - hist[-1]["t"]).total_seconds() >= refresh_s * 0.8):
    hist.append({"t": now, "spot": spot,
                 "oi": {r.strike: (r.ce_oi, r.pe_oi) for r in df.itertuples()},
                 "px": {r.strike: (r.ce_ltp, r.pe_ltp) for r in df.itertuples()}})
    del hist[:-300]

if live:
    chg = window_change(hist, list(view["strike"]), window)
else:
    # closed market: use the full-day change reported by Upstox (OI vs previous day's OI)
    day_spot = (quote or {}).get("net_change")
    chg = {
        "ce": int(view["ce_day_doi"].sum()),
        "pe": int(view["pe_day_doi"].sum()),
        "spot": float(day_spot) if day_spot is not None else 0.0,
    }
scope = "window" if live else "day"
pcr = df["pe_oi"].sum() / df["ce_oi"].sum() if df["ce_oi"].sum() else 0
label, score, reasons = calculate_score(chg, pcr, thr_pts)
pattern = oi_pattern_signal(chg["spot"], chg["ce"], chg["pe"], thr_pts) if chg else "COLLECTING DATA"
ideas, support, resist = strike_ideas(label, atm, spot, df, sl_pct, tgt_pct)

# --- header metrics ---
if live:
    price_delta = spot - hist[-2]["spot"] if len(hist) > 1 else 0.0
elif quote and quote.get("net_change") is not None:
    price_delta = float(quote["net_change"])  # day change vs previous close
else:
    price_delta = None
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("NIFTY (last)" if not live else "NIFTY", f"{spot:,.2f}",
          f"{price_delta:+.2f}" if price_delta is not None else None)
c2.metric("ATM strike", f"{atm:.0f}")
c3.metric("PCR (full chain)", f"{pcr:.2f}")
c4.metric("Support (max PE OI)", f"{support:.0f}")
c5.metric("Resistance (max CE OI)", f"{resist:.0f}")
st.caption(
    f"Expiry {expiry}  |  Checked {now.strftime('%H:%M:%S')} IST  |  "
    + (f"Snapshots: {len(hist)}" if live else "Mode: last session (market closed)")
)

# --- signal ---
icon = {"BULLISH": "🟢", "BEARISH": "🔴"}.get(label, "🟡")
st.subheader(f"{icon} Signal: {label}  (score {score:+d})")
st.markdown(f"**OI pattern (original rules):** `{pattern}`")
if chg:
    m1, m2, m3 = st.columns(3)
    m1.metric(f"CE ΔOI ({scope})", f"{chg['ce']:+,.0f}")
    m2.metric(f"PE ΔOI ({scope})", f"{chg['pe']:+,.0f}")
    m3.metric(f"Spot move ({scope})", f"{chg['spot']:+.1f}")
for r in reasons:
    st.write(f"- {r}")

# --- strike ideas ---
st.subheader("🎯 Strike ideas")
if not ideas:
    st.info("No directional edge right now. Staying out is a valid position.")
else:
    for side, strike, prem, sl, tgt, note in ideas:
        with st.container(border=True):
            st.markdown(f"**{side} {strike}**  @ ~₹{prem:,.2f}")
            if sl is not None:
                st.write(f"Stop-loss ≈ ₹{sl:,.2f}  |  Target ≈ ₹{tgt:,.2f}")
            st.caption(note)
st.caption("⚠️ Rule-based idea from OI data only, not a recommendation. Test on paper first; options can expire worthless.")

# --- chain table ---
st.subheader("Option chain (near ATM)")
basis_txt = "vs previous close / previous-day OI" if basis_day else f"over the last {window} refreshes"
if not basis_day and len(hist) < 2:
    st.caption("Collecting snapshots... showing day basis until at least 2 refreshes are stored.")
    basis_txt = "vs previous close / previous-day OI"
st.caption(f"Buildup basis: {basis_txt}")

v = strike_changes(view, hist, window, basis_day)
v["pcr"] = [(p / c) if c else float("nan") for c, p in zip(v["ce_oi"], v["pe_oi"])]
ce_lab = [classify_buildup(px, oi) for px, oi in zip(v["ce_px_chg"], v["ce_oi_chg"])]
pe_lab = [classify_buildup(px, oi) for px, oi in zip(v["pe_px_chg"], v["pe_oi_chg"])]
v["ce_buildup"] = [buildup_text("CE", lab) for lab in ce_lab]
v["pe_buildup"] = [buildup_text("PE", lab) for lab in pe_lab]
v["mkt_view"] = [market_view_text(c, p) for c, p in zip(ce_lab, pe_lab)]
v["ce_fast"], v["pe_fast"] = fast_move_symbols(v, fast_pts)
v["ce_spread"] = [spread_text(d, p, wide_pct) for d, p in zip(v["ce_spread"], v["ce_spr_pct"])]
v["pe_spread"] = [spread_text(d, p, wide_pct) for d, p in zip(v["pe_spread"], v["pe_spr_pct"])]
v["ce_vol"] = [fmt_indian(x) for x in v["ce_vol"]]
v["pe_vol"] = [fmt_indian(x) for x in v["pe_vol"]]

table = v.rename(columns={
    "strike": "Strike", "pcr": "PCR", "mkt_view": "Market View",
    "ce_ltp": "CE LTP", "ce_oi": "CE OI", "ce_oi_chg": "CE ΔOI", "ce_px_chg": "CE Price Δ",
    "ce_buildup": "CE Buildup", "ce_fast": "CE Fast", "ce_spread": "CE Spread", "ce_vol": "CE Volume",
    "pe_ltp": "PE LTP", "pe_oi": "PE OI", "pe_oi_chg": "PE ΔOI", "pe_px_chg": "PE Price Δ",
    "pe_buildup": "PE Buildup", "pe_fast": "PE Fast", "pe_spread": "PE Spread", "pe_vol": "PE Volume",
})[["CE Fast", "CE Buildup", "CE ΔOI", "CE OI", "CE Volume", "CE Spread", "CE LTP",
    "Strike", "Market View", "PCR",
    "PE LTP", "PE Spread", "PE Volume", "PE OI", "PE ΔOI", "PE Buildup", "PE Fast"]]

fmt = {"Strike": "{:.0f}", "PCR": "{:.2f}", "CE LTP": "{:.2f}", "PE LTP": "{:.2f}",
       "CE OI": "{:,.0f}", "PE OI": "{:,.0f}",
       "CE ΔOI": "{:+,.0f}", "PE ΔOI": "{:+,.0f}"}


def style_row(r):
    out = []
    for col in r.index:
        s = "background-color: rgba(255,200,0,0.25)" if r["Strike"] == atm else ""
        if col in ("CE Buildup", "PE Buildup"):
            s = BUILDUP_STYLE.get(str(r[col]).split(BUILDUP_SEP)[0], s)
        elif col == "PCR" and pd.notna(r[col]):
            if r[col] >= 1.2:
                s = "background-color: rgba(0,170,80,0.25)"
            elif r[col] <= 0.8:
                s = "background-color: rgba(220,40,40,0.25)"
        elif col == "Market View":
            t = str(r[col])
            strong = 0.45 if "Strong" in t else 0.25
            if "Bullish" in t:
                s = f"background-color: rgba(0,170,80,{strong}); font-weight: 600"
            elif "Bearish" in t:
                s = f"background-color: rgba(220,40,40,{strong}); font-weight: 600"
            elif "Mixed" in t:
                s = "background-color: rgba(150,150,150,0.30); font-weight: 600"
        out.append(s)
    return out


st.dataframe(
    table.style.apply(style_row, axis=1).format(fmt, na_rep="-"),
    width="stretch",
    hide_index=True,
)
st.markdown("**How to read the Buildup columns**")
st.table(
    pd.DataFrame(
        [
            ["🟩 Long Buildup", "↑", "↑", "New call buyers", "New put buyers"],
            ["🟥 Short Buildup", "↓", "↑", "New call sellers", "New put sellers"],
            ["🟦 Short Covering", "↑", "↓", "Call sellers exiting", "Put sellers exiting"],
            ["🟧 Long Unwinding", "↓", "↓", "Call buyers exiting", "Put buyers exiting"],
        ],
        columns=["Label", "Option price", "OI", "CE (call) side", "PE (put) side"],
    ).set_index("Label")
)
st.markdown("**Market View: CE and PE read together, per strike**")
_mv = pd.DataFrame(
    [
        ["CE Long Buildup", "PE Short Buildup", "Call buyers active + Put writers active", "🟢 Strong Bullish"],
        ["CE Short Covering", "PE Short Buildup", "Call writers trapped + Put writers supporting", "🟢 Strong Bullish / Fast Upside"],
        ["CE Long Buildup", "PE Long Unwinding", "Call buying + Put buyers exiting", "🟢 Bullish"],
        ["CE Short Covering", "PE Long Unwinding", "Call sellers exiting + Fear reducing", "🟢 Bullish Relief"],
        ["CE Short Buildup", "PE Long Buildup", "Call writers active + Put buyers active", "🔴 Strong Bearish"],
        ["CE Long Unwinding", "PE Long Buildup", "Call buyers exiting + Put buyers active", "🔴 Bearish"],
    ],
    columns=["CE side", "PE side", "Combined meaning", "Market view"],
)
_mv.index = range(1, len(_mv) + 1)
st.table(_mv)
st.caption(
    "Other combinations: if CE and PE point in opposite directions the view is 🟡 Mixed - stay cautious; "
    "if both lean bearish but are not in the table (for example CE Short Buildup + PE Short Covering) it shows "
    "🔴 Bearish. Always read CE and PE together, not separately, and follow the stronger side. "
    "Strikes near ATM matter more than far-away strikes."
)
st.caption(
    "Buildup = new positions being opened. Covering / Unwinding = old positions being closed. "
    "Strike PCR = PE OI ÷ CE OI at that strike (green ≥ 1.2, red ≤ 0.8). "
    "Read each side on its own premium: CE Long Buildup is bullish, PE Long Buildup is bearish."
)
st.caption(
    f"🚀🚀 = fastest premium mover, 🚀 = fast (blank = slow), judged from delta + gamma for a "
    f"{fast_pts}-pt NIFTY move. CE 🚀 pays when NIFTY rises, PE 🚀 pays when NIFTY falls. "
    "Fast also means fast on the way down, so size and stop-loss accordingly."
)
st.caption(
    "Spread = best ask minus best bid. It is shown as the ₹ gap, then the same gap as a % of the "
    "option price in brackets (what you lose if you buy and sell straight away), then a word: "
    f"Cheap = {wide_pct:g}% or less, Costly = more than {wide_pct:g}%. "
    "Judge by the % and the word, not the ₹ number. '-' means no bid/ask quote (market closed)."
)
st.caption(
    "Volume = contracts traded today, shown in lakhs (L) and crores (Cr): "
    "1 L = 1,00,000 and 1 Cr = 1,00,00,000."
)

st.subheader("OI by strike")
st.bar_chart(view.set_index("strike")[["ce_oi", "pe_oi"]])

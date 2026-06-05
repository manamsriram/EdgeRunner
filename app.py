import hashlib
import sqlite3
from datetime import date, datetime

import streamlit as st


# ---- auth helpers (unchanged from original) ----

def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users
                 (username TEXT PRIMARY KEY,
                  password TEXT,
                  email TEXT,
                  full_name TEXT,
                  created_at DATETIME)""")
    c.execute("""CREATE TABLE IF NOT EXISTS queries
                 (username TEXT, query TEXT, response TEXT, timestamp DATETIME)""")
    conn.commit()
    conn.close()


def make_hash(password):
    return hashlib.sha256(str.encode(password)).hexdigest()


def check_credentials(username, password):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT password FROM users WHERE username=?", (username,))
    stored = c.fetchone()
    conn.close()
    return bool(stored and stored[0] == make_hash(password))


def username_exists(username):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE username=?", (username,))
    result = c.fetchone()
    conn.close()
    return result is not None


def email_exists(email):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE email=?", (email,))
    result = c.fetchone()
    conn.close()
    return result is not None


def save_query(username, query, response):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("INSERT INTO queries VALUES (?, ?, ?, ?)",
              (username, query, response, datetime.now()))
    conn.commit()
    conn.close()


def get_user_history(username):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute(
        "SELECT query, response, timestamp FROM queries WHERE username=? ORDER BY timestamp DESC",
        (username,),
    )
    history = c.fetchall()
    conn.close()
    return history


# ---- trading helpers ----

@st.cache_resource(hash_funcs={"builtins.module": lambda _: None})
def _trading_resources():
    """Load config, repo, and broker once per Streamlit session (cached across reruns).

    Re-raises on failure so @st.cache_resource does not cache the broken state.
    """
    from trader.config import load_config
    from trader.execution.broker import AlpacaBroker
    from trader.portfolio.sqlite_repo import SQLiteRepository

    cfg = load_config()
    repo = SQLiteRepository(cfg.portfolio_db_path)
    broker = AlpacaBroker(cfg)
    return cfg, repo, broker


def _render_approvals(cfg, repo, broker):
    st.subheader("Pending Trade Approvals")
    st.button("Refresh", key="approvals_refresh")  # click triggers rerun naturally

    from trader.execution.broker import client_order_id_for
    from trader.portfolio.repository import PROPOSAL_APPROVED, PROPOSAL_EXECUTED, PROPOSAL_PENDING, PROPOSAL_REJECTED

    pending = repo.list_pending_proposals()
    if not pending:
        st.info("No pending proposals.")
        return

    for row in pending:
        pid = row["id"]
        with st.expander(
            f"#{pid} {row['symbol']} {row['side'].upper()}  "
            f"${row['notional']:,.2f}  @${row['ref_price']:.2f}  —  {row['reason']}"
        ):
            st.write(f"**Created:** {row['created_at']}")
            col_approve, col_reject = st.columns(2)

            with col_approve:
                if st.button("Approve", key=f"approve_{pid}"):
                    # Guard: re-read status before acting to block double-clicks.
                    current = repo.list_pending_proposals()
                    ids = {r["id"] for r in current}
                    if pid not in ids:
                        st.warning("Already actioned.")
                        return
                    repo.set_proposal_status(pid, PROPOSAL_APPROVED)
                    try:
                        created_at = datetime.fromisoformat(str(row["created_at"]))
                        trade_date = created_at.date()
                        coid = client_order_id_for(trade_date, row["symbol"], row["side"], f"proposal-{pid}")
                        qty = None
                        notional = None
                        if row["side"] == "sell":
                            ref_price = row["ref_price"]
                            if not ref_price:
                                repo.set_proposal_status(pid, PROPOSAL_PENDING)
                                st.error("Cannot submit sell: ref_price is zero.")
                                return
                            qty = row["notional"] / ref_price
                        else:
                            notional = row["notional"]
                        order = broker.submit(
                            symbol=row["symbol"],
                            side=row["side"],
                            client_order_id=coid,
                            notional=notional,
                            qty=qty,
                        )
                        from trader.portfolio.repository import OrderRow
                        repo.record_order(OrderRow(
                            client_order_id=coid,
                            symbol=row["symbol"],
                            side=row["side"],
                            notional=row["notional"],
                            status="submitted",
                            broker_order_id=str(getattr(order, "id", "") or "") or None,
                        ))
                        repo.set_proposal_status(pid, PROPOSAL_EXECUTED)
                        st.success(f"Order submitted for {row['symbol']}.")
                    except Exception as exc:
                        repo.set_proposal_status(pid, PROPOSAL_PENDING)
                        st.error(f"Submit failed — rolled back: {exc}")
                    st.rerun()

            with col_reject:
                if st.button("Reject", key=f"reject_{pid}"):
                    repo.set_proposal_status(pid, PROPOSAL_REJECTED)
                    st.rerun()


def _render_portfolio(cfg, repo, broker):
    st.subheader("Live Positions")
    try:
        positions = broker.get_positions()
        if positions:
            rows = [
                {
                    "Symbol": p["symbol"],
                    "Qty": p["qty"],
                    "Avg Entry": p["avg_entry_price"],
                    "Market Value": p["market_value"],
                    "Unrealized P&L": p["unrealized_pl"],
                }
                for p in positions
            ]
            st.dataframe(rows, use_container_width=True)
        else:
            st.info("No open positions.")
    except Exception as exc:
        st.error(f"Could not fetch positions: {exc}")

    st.subheader("Recent Orders")
    orders = repo.get_orders()
    if orders:
        st.dataframe(
            [{k: v for k, v in o.items()} for o in orders[:50]],
            use_container_width=True,
        )
    else:
        st.info("No orders recorded yet.")

    st.subheader("Equity Curve")
    history = broker.get_portfolio_history()
    if history and history["equity"]:
        import pandas as _pd
        df = _pd.DataFrame(
            {"Equity ($)": history["equity"]},
            index=_pd.to_datetime(history["timestamp"]),
        )
        st.line_chart(df)
    else:
        st.info("No portfolio history yet — run the scheduler to populate.")


def _render_controls(cfg, repo, broker):
    from trader.risk.gate import KillSwitch

    ks = KillSwitch(cfg.kill_switch_path)
    engaged = ks.engaged()

    st.subheader("Kill Switch")
    status_color = "red" if engaged else "green"
    status_label = "ENGAGED" if engaged else "Disengaged"
    st.markdown(
        f"<span style='color:{status_color}; font-size:1.2em; font-weight:bold'>"
        f"Kill Switch: {status_label}</span>",
        unsafe_allow_html=True,
    )
    col_engage, col_disengage = st.columns(2)
    with col_engage:
        if st.button("Engage Kill Switch", disabled=engaged, type="primary"):
            ks.engage("dashboard")
            st.rerun()
    with col_disengage:
        if st.button("Disengage Kill Switch", disabled=not engaged):
            ks.disengage()
            st.rerun()

    st.subheader("Autonomy Mode")
    st.write(f"Current mode: **{cfg.autonomy}**")
    st.caption("To change autonomy mode, set `AUTONOMY=auto` or `AUTONOMY=manual` in `.env` and restart.")

    st.subheader("Run Log (last 20)")
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(cfg.portfolio_db_path)
        conn.row_factory = _sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, started_at, strategy, mode, note FROM runs ORDER BY id DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        if rows:
            st.dataframe([dict(r) for r in rows], use_container_width=True)
        else:
            st.info("No pipeline runs recorded yet.")
    except Exception as exc:
        st.info(f"Run log unavailable: {exc}")


# ---- app bootstrap ----

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

init_db()

# Sidebar auth (unchanged behaviour)
with st.sidebar:
    if not st.session_state.logged_in:
        st.header("Login/Register")
        action = st.radio("Choose action:", ["Login", "Register"])

        if action == "Register":
            st.subheader("Create New Account")
            with st.form("registration_form"):
                new_username = st.text_input("Username*")
                new_email = st.text_input("Email*")
                new_full_name = st.text_input("Full Name*")
                new_password = st.text_input("Password*", type="password")
                confirm_password = st.text_input("Confirm Password*", type="password")
                submit_button = st.form_submit_button("Register")

                if submit_button:
                    if not all([new_username, new_email, new_full_name, new_password, confirm_password]):
                        st.error("All fields are required!")
                    elif len(new_password) < 6:
                        st.error("Password must be at least 6 characters long!")
                    elif new_password != confirm_password:
                        st.error("Passwords do not match!")
                    elif username_exists(new_username):
                        st.error("Username already exists!")
                    elif email_exists(new_email):
                        st.error("Email already registered!")
                    elif "@" not in new_email:
                        st.error("Please enter a valid email address!")
                    else:
                        conn = sqlite3.connect("users.db")
                        c = conn.cursor()
                        c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                                  (new_username, make_hash(new_password),
                                   new_email, new_full_name, datetime.now()))
                        conn.commit()
                        conn.close()
                        st.success("Registration successful! Please login.")
        else:
            with st.form("login_form"):
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")
                login_button = st.form_submit_button("Login")
                if login_button:
                    if check_credentials(username, password):
                        st.session_state.logged_in = True
                        st.session_state.username = username
                        st.rerun()
                    else:
                        st.error("Invalid credentials")
    else:
        st.write(f"Logged in as **{st.session_state.username}**")
        if st.button("Logout"):
            st.session_state.logged_in = False
            st.rerun()

# Main content
if st.session_state.logged_in:
    st.title("Trading Agent Dashboard")

    try:
        cfg, repo, broker_or_err = _trading_resources()
        trading_available = True
    except Exception as _trading_exc:
        cfg = repo = broker_or_err = None
        trading_available = False
        _trading_err_msg = str(_trading_exc)
    else:
        _trading_err_msg = ""

    tab_analysis, tab_approvals, tab_portfolio, tab_controls = st.tabs(
        ["Analysis", "Approvals", "Portfolio", "Controls"]
    )

    with tab_analysis:
        st.subheader("Stock Analysis Bot")
        st.caption("Invest at your own risk. This bot gathers real-time stock information and analyzes it via LLM.")

        query = st.text_input("Input your investment related query:")
        col1, col2 = st.columns(2)
        with col1:
            enter = st.button("Enter")
        with col2:
            clear = st.button("Clear")

        if clear:
            st.markdown(" ")
        if enter and query:
            with st.spinner("Gathering information and analyzing…"):
                from tools.fetch_stock_info import Analyze_stock
                out = Analyze_stock(query)
                save_query(st.session_state.username, query, out)
            st.success("Done!")
            st.write(out)

        st.header("Your Query History")
        history = get_user_history(st.session_state.username)
        for q, response, timestamp in history:
            with st.expander(f"Query: {q[:50]}… ({timestamp})"):
                st.write("Query:", q)
                st.write("Response:", response)
                st.write("Time:", timestamp)

    with tab_approvals:
        if trading_available:
            _render_approvals(cfg, repo, broker_or_err)
        else:
            st.error(f"Trading unavailable — check .env: {_trading_err_msg}")

    with tab_portfolio:
        if trading_available:
            _render_portfolio(cfg, repo, broker_or_err)
        else:
            st.error(f"Trading unavailable — check .env: {_trading_err_msg}")

    with tab_controls:
        if trading_available:
            _render_controls(cfg, repo, broker_or_err)
        else:
            st.error(f"Trading unavailable — check .env: {_trading_err_msg}")

else:
    st.title("Welcome to Trading Agent Dashboard")
    st.write("Please login or register to continue.")

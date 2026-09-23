"""
Backtester Forex personnel — Streamlit + Backtrader
----------------------------------------------------
Colle ta stratégie (classe Backtrader) dans la zone de texte, choisis
la paire et la période, puis lance le backtest. Les résultats (courbe
d'équité, stats, trades) s'affichent directement dans l'app.

Lancer avec :
    streamlit run app.py
"""

import io
import contextlib
import datetime as dt

import backtrader as bt
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="Backtester Forex", layout="wide")

# ---------------------------------------------------------------------
# Stratégie d'exemple (utilisée si l'utilisateur ne colle pas la sienne)
# ---------------------------------------------------------------------
EXAMPLE_STRATEGY = '''\
class MyStrategy(bt.Strategy):
    params = dict(fast=10, slow=30)

    def __init__(self):
        fast_ma = bt.ind.SMA(period=self.p.fast)
        slow_ma = bt.ind.SMA(period=self.p.slow)
        self.crossover = bt.ind.CrossOver(fast_ma, slow_ma)

    def next(self):
        if not self.position:
            if self.crossover > 0:
                self.buy()
        elif self.crossover < 0:
            self.close()
'''

FOREX_PAIRS = {
    "EUR/USD": "EUR/USD",
    "GBP/USD": "GBP/USD",
    "USD/JPY": "USD/JPY",
    "USD/CHF": "USD/CHF",
    "AUD/USD": "AUD/USD",
    "USD/CAD": "USD/CAD",
    "NZD/USD": "NZD/USD",
    "XAU/USD (Or)": "XAU/USD",
    "XAG/USD (Argent)": "XAG/USD",
    "Autre (saisir le symbole Twelve Data)": None,
}

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"

# ---------------------------------------------------------------------
# Sidebar — paramètres
# ---------------------------------------------------------------------
st.sidebar.header("Paramètres du backtest")

api_key = st.sidebar.text_input(
    "Clé API Twelve Data",
    type="password",
    help="Gratuite sur twelvedata.com — nécessaire pour récupérer les données.",
)

pair_label = st.sidebar.selectbox("Paire", list(FOREX_PAIRS.keys()))
ticker = FOREX_PAIRS[pair_label]
if ticker is None:
    ticker = st.sidebar.text_input("Symbole Twelve Data (ex: EUR/USD)", "EUR/USD")

interval = st.sidebar.selectbox(
    "Timeframe",
    ["1day", "4h", "1h", "30min", "15min", "5min", "1min"],
    help="Le tier gratuit Twelve Data limite le nombre de requêtes/jour et l'historique disponible sur les timeframes courts.",
)

# Largeur de fenêtre sûre par timeframe : reste sous la limite de 5000 bougies
# par requête de Twelve Data, avec une marge de sécurité.
MAX_WINDOW_DAYS = {
    "1min": 3,
    "5min": 15,
    "15min": 45,
    "30min": 90,
    "1h": 180,
    "4h": 600,
    "1day": 1825,
}
window_days = MAX_WINDOW_DAYS[interval]
st.sidebar.info(f"⏱️ Pour ce timeframe, période testable : **{window_days} jours** par backtest (limite Twelve Data).")

offset_weeks = st.sidebar.number_input(
    "Reculer de combien de semaines par rapport à aujourd'hui ?",
    min_value=0, value=0, step=1,
    help="0 = période la plus récente possible. Augmente pour tester une fenêtre plus ancienne (même largeur de période, juste décalée dans le temps).",
)

today = dt.date.today()
end_date = today - dt.timedelta(weeks=offset_weeks)
start_date = end_date - dt.timedelta(days=window_days)

st.sidebar.caption(
    f"📅 Période testée : **{start_date:%d/%m/%Y}** → **{end_date:%d/%m/%Y}** "
    f"({window_days} jours, largeur fixe pour ce timeframe)."
)

initial_cash = st.sidebar.number_input("Capital initial ($)", value=10000, step=1000)
commission = st.sidebar.number_input(
    "Commission (%)", value=0.0, step=0.01, format="%.3f",
    help="Ex: 0.01 pour 0.01% par trade. Laisse à 0 si ton broker ne prend pas de commission séparée du spread.",
)
size_pct = st.sidebar.slider(
    "Taille de position (% du capital)", min_value=1, max_value=100, value=10,
    help="% du capital investi à chaque trade. Une taille fixe en unités peut faire rejeter les ordres sur des actifs chers comme l'or.",
)

# ---------------------------------------------------------------------
# Zone de code — la stratégie de l'utilisateur
# ---------------------------------------------------------------------
st.title("🧪 Backtester Forex")
st.caption("Colle ta stratégie Backtrader ci-dessous, puis lance le backtest.")

with st.expander("ℹ️ Comment écrire ta stratégie (format attendu)"):
    st.markdown(
        """
- Ta classe doit s'appeler **`MyStrategy`** et hériter de `bt.Strategy`.
- Utilise les méthodes standard de Backtrader : `__init__`, `next`, `self.buy()`, `self.sell()`, `self.close()`.
- `bt` (backtrader) est déjà importé, pas besoin de le réimporter.
- Tu peux définir des paramètres via `params = dict(...)`.
        """
    )

strategy_code = st.text_area(
    "Code de ta stratégie",
    value=EXAMPLE_STRATEGY,
    height=300,
)

run = st.button("🚀 Lancer le backtest", type="primary")

# ---------------------------------------------------------------------
# Exécution du backtest
# ---------------------------------------------------------------------
def load_strategy_class(code: str):
    """Exécute le code collé dans un namespace isolé et récupère MyStrategy."""
    namespace = {"bt": bt}
    exec(code, namespace)
    if "MyStrategy" not in namespace:
        raise ValueError("Aucune classe 'MyStrategy' trouvée dans le code collé.")
    return namespace["MyStrategy"]


def to_plain(obj):
    """Convertit récursivement les objets internes de Backtrader (AutoOrderedDict)
    en dict/list Python natifs, pour un affichage sûr dans Streamlit."""
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


def fetch_data(symbol: str, start: dt.date, end: dt.date, interval: str, api_key: str) -> pd.DataFrame:
    if not api_key:
        raise ValueError("Renseigne ta clé API Twelve Data dans la barre latérale.")

    params = {
        "symbol": symbol,
        "interval": interval,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "outputsize": 5000,
        "apikey": api_key,
        "order": "ASC",
        "timezone": "UTC",
    }
    resp = requests.get(TWELVEDATA_URL, params=params, timeout=30)
    data = resp.json()

    if isinstance(data, dict) and data.get("status") == "error":
        raise ValueError(f"Erreur API Twelve Data : {data.get('message', 'erreur inconnue')}")

    values = data.get("values") if isinstance(data, dict) else None
    if not values:
        raise ValueError(
            "Aucune donnée reçue. Vérifie le symbole, la période, "
            "ou ta clé API (limite de requêtes atteinte ?)."
        )

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    # Le forex n'a pas de vrai volume échangé centralisé ; Twelve Data n'en fournit pas non plus.
    df["volume"] = df.get("volume", 0)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df[["open", "high", "low", "close", "volume"]]


if run:
    try:
        with st.spinner("Récupération des données..."):
            data = fetch_data(ticker, start_date, end_date, interval, api_key)
            st.caption(
                f"📊 {len(data)} bougies récupérées, "
                f"du {data.index.min():%d/%m/%Y %H:%M} au {data.index.max():%d/%m/%Y %H:%M}."
            )

        with st.spinner("Chargement de la stratégie..."):
            StrategyClass = load_strategy_class(strategy_code)

        with st.spinner("Backtest en cours..."):
            cerebro = bt.Cerebro()
            cerebro.addstrategy(StrategyClass)
            feed = bt.feeds.PandasData(dataname=data, openinterest=-1)
            cerebro.adddata(feed)
            cerebro.broker.setcash(initial_cash)
            cerebro.broker.setcommission(commission=commission / 100)
            cerebro.addsizer(bt.sizers.PercentSizer, percents=size_pct)

            cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
            cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
            cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days)
            cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
            cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="time_return")

            start_value = cerebro.broker.getvalue()
            results = cerebro.run()
            end_value = cerebro.broker.getvalue()
            strat = results[0]

        # -----------------------------------------------------------
        # Résultats chiffrés
        # -----------------------------------------------------------
        st.success("Backtest terminé ✅")

        pnl = end_value - start_value
        pnl_pct = (pnl / start_value) * 100

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Valeur finale", f"${end_value:,.2f}", f"{pnl_pct:+.2f}%")
        m2.metric("P&L", f"${pnl:,.2f}")

        dd = strat.analyzers.drawdown.get_analysis()
        m3.metric("Drawdown max", f"{dd.get('max', {}).get('drawdown', 0):.2f}%")

        sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio")
        m4.metric("Sharpe ratio", f"{sharpe:.2f}" if sharpe else "N/A")

        trades = strat.analyzers.trades.get_analysis()
        total_trades = trades.get("total", {}).get("total", 0)
        won = trades.get("won", {}).get("total", 0)
        lost = trades.get("lost", {}).get("total", 0)
        win_rate = (won / total_trades * 100) if total_trades else 0

        t1, t2, t3 = st.columns(3)
        t1.metric("Nombre de trades", total_trades)
        t2.metric("Trades gagnants", won)
        t3.metric("Win rate", f"{win_rate:.1f}%")

        # -----------------------------------------------------------
        # Courbe d'équité
        # -----------------------------------------------------------
        time_return = strat.analyzers.time_return.get_analysis()
        if time_return:
            dates = list(time_return.keys())
            returns = list(time_return.values())
            equity = [initial_cash]
            for r in returns:
                equity.append(equity[-1] * (1 + r))
            equity = equity[1:]

            fig = go.Figure()
            fig.add_trace(go.Scatter(x=dates, y=equity, mode="lines", name="Équité"))
            fig.update_layout(
                title="Courbe d'équité",
                xaxis_title="Date",
                yaxis_title="Valeur du compte ($)",
                height=450,
            )
            st.plotly_chart(fig, use_container_width=True)

        with st.expander("Détails bruts des analyzers"):
            st.write("Trades:", to_plain(trades))
            st.write("Drawdown:", to_plain(dd))

    except Exception as e:
        st.error(f"Erreur pendant le backtest : {e}")
        st.exception(e)
else:
    st.info("Configure les paramètres dans la barre latérale, colle ta stratégie, puis clique sur **Lancer le backtest**.")

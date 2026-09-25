"""
Backtester Forex — Streamlit + Backtrader
------------------------------------------
Choisis une strategie predefinie, la paire et la periode, puis lance le
backtest. Les resultats (courbe d'equite, stats, trades) s'affichent
directement dans l'app.

Lancer avec :
    streamlit run app.py
"""

import datetime as dt

import backtrader as bt
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from sandbox import run_backtest_sandboxed

st.set_page_config(page_title="Backtester Forex", layout="wide")

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


# ===========================================================================
# STRATEGIES PREDEFINIES
# (plus de code arbitraire colle par l'utilisateur : securite pour une app
#  publique, ou n'importe qui pourrait sinon executer du code sur le serveur)
# ===========================================================================

class DonchianSwingStrategy(bt.Strategy):
    """Breakout Donchian + tendance EMA200 + filtre ATR + SL/TP fixes + breakeven.
    Pensee pour un timeframe 1h."""
    params = dict(
        ema_trend=200,
        donchian_period=20,
        atr_period=14,
        atr_avg_period=50,
        atr_mult=1.5,
        min_sl_distance=3.00,
        rr_ratio=1.5,
        breakeven_trigger=0.5,
        max_trades_per_day=5,
    )

    def __init__(self):
        self.ema_trend = bt.ind.EMA(period=self.p.ema_trend)
        self.atr = bt.ind.ATR(period=self.p.atr_period)
        self.atr_avg = bt.ind.SMA(self.atr, period=self.p.atr_avg_period)
        self.donchian_high = bt.ind.Highest(self.data.high(-1), period=self.p.donchian_period)
        self.donchian_low = bt.ind.Lowest(self.data.low(-1), period=self.p.donchian_period)

        self.trades_today = 0
        self.current_day = None
        self.entry_price = None
        self.sl = None
        self.tp = None
        self.sl_distance = None
        self.breakeven_armed = False
        self.trade_type = None

    def next(self):
        bar_date = self.data.datetime.date(0)
        if self.current_day != bar_date:
            self.current_day = bar_date
            self.trades_today = 0

        min_bars = max(self.p.ema_trend, self.p.donchian_period, self.p.atr_avg_period) + 5
        if len(self) < min_bars:
            return

        price = self.data.close[0]

        if self.position:
            if self.trade_type == "BUY":
                if not self.breakeven_armed and price >= self.entry_price + self.sl_distance * self.p.breakeven_trigger:
                    self.breakeven_armed = True
                    self.sl = self.entry_price
                if price >= self.tp:
                    self.close()
                elif self.data.low[0] <= self.sl:
                    self.close()
            elif self.trade_type == "SELL":
                if not self.breakeven_armed and price <= self.entry_price - self.sl_distance * self.p.breakeven_trigger:
                    self.breakeven_armed = True
                    self.sl = self.entry_price
                if price <= self.tp:
                    self.close()
                elif self.data.high[0] >= self.sl:
                    self.close()
            return

        if self.trades_today >= self.p.max_trades_per_day:
            return
        if self.atr[0] < self.atr_avg[0]:
            return

        trend_bullish = price > self.ema_trend[0]
        trend_bearish = price < self.ema_trend[0]
        breakout_up = price > self.donchian_high[0]
        breakout_down = price < self.donchian_low[0]

        sl_distance = max(self.atr[0] * self.p.atr_mult, self.p.min_sl_distance)

        if trend_bullish and breakout_up:
            self.entry_price = price
            self.sl_distance = sl_distance
            self.sl = price - sl_distance
            self.tp = price + sl_distance * self.p.rr_ratio
            self.breakeven_armed = False
            self.trade_type = "BUY"
            self.buy()
            self.trades_today += 1
        elif trend_bearish and breakout_down:
            self.entry_price = price
            self.sl_distance = sl_distance
            self.sl = price + sl_distance
            self.tp = price - sl_distance * self.p.rr_ratio
            self.breakeven_armed = False
            self.trade_type = "SELL"
            self.sell()
            self.trades_today += 1


class ScalpingMomentumStrategy(bt.Strategy):
    """Breakout Donchian court + EMA50 + momentum RSI + cooldown apres perte.
    Pensee pour un timeframe 5min."""
    params = dict(
        ema_trend=50,
        donchian_period=10,
        rsi_period=9,
        atr_period=14,
        atr_avg_period=30,
        sl_atr_mult=1.0,
        rr_ratio=1.3,
        min_sl_distance=1.5,
        breakeven_trigger=0.5,
        max_trades_per_day=20,
        cooldown_bars=6,
    )

    def __init__(self):
        self.ema_trend = bt.ind.EMA(period=self.p.ema_trend)
        self.rsi = bt.ind.RSI(period=self.p.rsi_period, safediv=True)
        self.atr = bt.ind.ATR(period=self.p.atr_period)
        self.atr_avg = bt.ind.SMA(self.atr, period=self.p.atr_avg_period)
        self.donchian_high = bt.ind.Highest(self.data.high(-1), period=self.p.donchian_period)
        self.donchian_low = bt.ind.Lowest(self.data.low(-1), period=self.p.donchian_period)

        self.trades_today = 0
        self.current_day = None
        self.bars_since_loss = 999
        self.entry_price = None
        self.sl = None
        self.tp = None
        self.sl_distance = None
        self.breakeven_armed = False
        self.trade_type = None

    def notify_trade(self, trade):
        if trade.isclosed:
            if trade.pnl < 0:
                self.bars_since_loss = 0
            self.entry_price = None
            self.sl = None
            self.tp = None
            self.trade_type = None
            self.breakeven_armed = False

    def next(self):
        bar_date = self.data.datetime.date(0)
        if self.current_day != bar_date:
            self.current_day = bar_date
            self.trades_today = 0

        self.bars_since_loss += 1

        min_bars = max(self.p.ema_trend, self.p.donchian_period, self.p.atr_avg_period) + 5
        if len(self) < min_bars:
            return

        price = self.data.close[0]

        if self.position:
            if self.trade_type == "BUY":
                if not self.breakeven_armed and price >= self.entry_price + self.sl_distance * self.p.breakeven_trigger:
                    self.breakeven_armed = True
                    self.sl = self.entry_price
                if price >= self.tp:
                    self.close()
                elif self.data.low[0] <= self.sl:
                    self.close()
            elif self.trade_type == "SELL":
                if not self.breakeven_armed and price <= self.entry_price - self.sl_distance * self.p.breakeven_trigger:
                    self.breakeven_armed = True
                    self.sl = self.entry_price
                if price <= self.tp:
                    self.close()
                elif self.data.high[0] >= self.sl:
                    self.close()
            return

        if self.trades_today >= self.p.max_trades_per_day:
            return
        if self.bars_since_loss < self.p.cooldown_bars:
            return
        if self.atr[0] < self.atr_avg[0]:
            return

        trend_bullish = price > self.ema_trend[0]
        trend_bearish = price < self.ema_trend[0]
        breakout_up = price > self.donchian_high[0]
        breakout_down = price < self.donchian_low[0]
        momentum_bullish = self.rsi[0] > 50
        momentum_bearish = self.rsi[0] < 50

        sl_distance = max(self.atr[0] * self.p.sl_atr_mult, self.p.min_sl_distance)

        if trend_bullish and breakout_up and momentum_bullish:
            self.entry_price = price
            self.sl_distance = sl_distance
            self.sl = price - sl_distance
            self.tp = price + sl_distance * self.p.rr_ratio
            self.breakeven_armed = False
            self.trade_type = "BUY"
            self.buy()
            self.trades_today += 1
        elif trend_bearish and breakout_down and momentum_bearish:
            self.entry_price = price
            self.sl_distance = sl_distance
            self.sl = price + sl_distance
            self.tp = price - sl_distance * self.p.rr_ratio
            self.breakeven_armed = False
            self.trade_type = "SELL"
            self.sell()
            self.trades_today += 1


class SmaCrossoverExampleStrategy(bt.Strategy):
    """Exemple pedagogique simple : croisement de deux moyennes mobiles.
    Non optimisee, sert de point de depart pour comprendre le fonctionnement
    de l'app."""
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


STRATEGIES = {
    "Breakout Donchian (Swing, 1h)": {
        "class": DonchianSwingStrategy,
        "description": (
            "Suit la tendance (EMA200) et entre sur une cassure du plus haut/bas "
            "des 20 dernieres bougies, filtre par la volatilite (ATR). SL/TP fixes "
            "avec passage au breakeven. Conçue pour un timeframe **1h**."
        ),
        "recommended_interval": "1h",
    },
    "Scalping Momentum (5min)": {
        "class": ScalpingMomentumStrategy,
        "description": (
            "Version plus rapide : EMA50, cassure sur 10 bougies, confirmee par le "
            "momentum RSI, avec une pause (cooldown) apres chaque perte pour eviter "
            "l'acharnement. Conçue pour un timeframe **5min**."
        ),
        "recommended_interval": "5min",
    },
    "Croisement de moyennes mobiles (exemple simple)": {
        "class": SmaCrossoverExampleStrategy,
        "description": (
            "Stratégie pédagogique basique (SMA 10/30) pour découvrir l'app. "
            "Non optimisée — sert de point de départ, pas de recommandation."
        ),
        "recommended_interval": "1h",
    },
}


# ---------------------------------------------------------------------
# Sidebar — parametres
# ---------------------------------------------------------------------
st.sidebar.header("Paramètres du backtest")

api_key = st.sidebar.text_input(
    "Clé API Twelve Data",
    type="password",
    help="Gratuite sur twelvedata.com — nécessaire pour récupérer les données. Ta clé n'est jamais stockée ni partagée.",
)

pair_label = st.sidebar.selectbox("Paire", list(FOREX_PAIRS.keys()))
ticker = FOREX_PAIRS[pair_label]
if ticker is None:
    ticker = st.sidebar.text_input("Symbole Twelve Data (ex: EUR/USD)", "EUR/USD")

interval = st.sidebar.selectbox(
    "Timeframe",
    ["1day", "4h", "1h", "30min", "15min", "5min", "1min"],
    index=2,
    help="Le tier gratuit Twelve Data limite le nombre de requêtes/jour et l'historique disponible sur les timeframes courts.",
)

MAX_WINDOW_DAYS = {
    "1min": 3, "5min": 15, "15min": 45, "30min": 90,
    "1h": 180, "4h": 600, "1day": 1825,
}
window_days = MAX_WINDOW_DAYS[interval]
st.sidebar.info(f"⏱️ Pour ce timeframe, période testable : **{window_days} jours** par backtest (limite Twelve Data).")

offset_weeks = st.sidebar.number_input(
    "Reculer de combien de semaines par rapport à aujourd'hui ?",
    min_value=0, value=0, step=1,
    help="0 = période la plus récente possible. Augmente pour tester une fenêtre plus ancienne.",
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
)

# ---------------------------------------------------------------------
# Choix de la stratégie (plus de code libre : securite pour usage public)
# ---------------------------------------------------------------------
st.title("🧪 Backtester Forex")
st.caption("Choisis une stratégie, configure les paramètres, puis lance le backtest.")

st.warning(
    "⚠️ **Avertissement** : ceci n'est pas un conseil financier. Les performances "
    "passées (même backtestées) ne garantissent en rien les résultats futurs. "
    "Ces stratégies ont été testées sur des données historiques et peuvent ne pas "
    "fonctionner en conditions réelles (spread, slippage, changement de marché). "
    "Utilise cet outil à des fins éducatives et teste toujours en démo avant le réel."
)

strategy_label = st.selectbox("Stratégie", list(STRATEGIES.keys()) + ["✏️ Mon propre code"])

custom_code = None
if strategy_label == "✏️ Mon propre code":
    st.caption(
        "Ton code tourne dans un environnement isolé (processus séparé, "
        "imports bloqués, timeout strict) — voir l'encart ci-dessous pour le détail."
    )
    with st.expander("ℹ️ Règles du bac à sable (pourquoi ton code peut être refusé)"):
        st.markdown(
            """
- Ta classe doit s'appeler **`MyStrategy`** et hériter de `bt.Strategy`.
- **Aucun `import`** n'est autorisé — `bt` (backtrader) est déjà fourni.
- Pas d'accès aux attributs commençant par `_` (bloque les tentatives de contournement).
- Fonctions interdites : `open`, `eval`, `exec`, `os`, `sys`, etc.
- Le code est exécuté dans un **processus isolé avec un timeout strict** — une boucle infinie sera automatiquement interrompue.
- Utilise les méthodes standard de Backtrader : `__init__`, `next`, `self.buy()`, `self.sell()`, `self.close()`.
            """
        )
    custom_code = st.text_area(
        "Colle ton code ici",
        value=(
            "class MyStrategy(bt.Strategy):\n"
            "    params = dict(fast=10, slow=30)\n\n"
            "    def __init__(self):\n"
            "        fast_ma = bt.ind.SMA(period=self.p.fast)\n"
            "        slow_ma = bt.ind.SMA(period=self.p.slow)\n"
            "        self.crossover = bt.ind.CrossOver(fast_ma, slow_ma)\n\n"
            "    def next(self):\n"
            "        if not self.position:\n"
            "            if self.crossover > 0:\n"
            "                self.buy()\n"
            "        elif self.crossover < 0:\n"
            "            self.close()\n"
        ),
        height=280,
    )
else:
    strategy_info = STRATEGIES[strategy_label]
    st.info(strategy_info["description"])

    if interval != strategy_info["recommended_interval"]:
        st.caption(
            f"💡 Cette stratégie a été validée sur le timeframe "
            f"**{strategy_info['recommended_interval']}** — tu utilises **{interval}**, "
            f"les résultats peuvent différer de ce qui a été testé."
        )

run = st.button("🚀 Lancer le backtest", type="primary")


# ---------------------------------------------------------------------
# Fonctions utilitaires
# ---------------------------------------------------------------------
def to_plain(obj):
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
    df["volume"] = df.get("volume", 0)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df[["open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------
# Execution du backtest
# ---------------------------------------------------------------------
if run:
    try:
        with st.spinner("Récupération des données..."):
            data = fetch_data(ticker, start_date, end_date, interval, api_key)
            st.caption(
                f"📊 {len(data)} bougies récupérées, "
                f"du {data.index.min():%d/%m/%Y %H:%M} au {data.index.max():%d/%m/%Y %H:%M}."
            )

        with st.spinner("Backtest en cours..."):
            if strategy_label == "✏️ Mon propre code":
                if not custom_code or not custom_code.strip():
                    raise ValueError("Colle ton code de stratégie avant de lancer le backtest.")
                result = run_backtest_sandboxed(custom_code, data, initial_cash, commission, size_pct)
                start_value = result["start_value"]
                end_value = result["end_value"]
                trades = result["trades"]
                dd = result["drawdown"]
                sharpe = result["sharpe"]
                time_return = result["time_return"]
            else:
                StrategyClass = strategy_info["class"]

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
                cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="time_return")

                start_value = cerebro.broker.getvalue()
                results = cerebro.run()
                end_value = cerebro.broker.getvalue()
                strat = results[0]

                trades = to_plain(strat.analyzers.trades.get_analysis())
                dd = to_plain(strat.analyzers.drawdown.get_analysis())
                sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio")
                time_return = dict(strat.analyzers.time_return.get_analysis())

        st.success("Backtest terminé ✅")

        pnl = end_value - start_value
        pnl_pct = (pnl / start_value) * 100

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Valeur finale", f"${end_value:,.2f}", f"{pnl_pct:+.2f}%")
        m2.metric("P&L", f"${pnl:,.2f}")
        m3.metric("Drawdown max", f"{dd.get('max', {}).get('drawdown', 0):.2f}%")
        m4.metric("Sharpe ratio", f"{sharpe:.2f}" if sharpe else "N/A")

        total_trades = trades.get("total", {}).get("total", 0)
        won = trades.get("won", {}).get("total", 0)
        win_rate = (won / total_trades * 100) if total_trades else 0

        t1, t2, t3 = st.columns(3)
        t1.metric("Nombre de trades", total_trades)
        t2.metric("Trades gagnants", won)
        t3.metric("Win rate", f"{win_rate:.1f}%")

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
            st.write("Trades:", trades)
            st.write("Drawdown:", dd)

    except Exception as e:
        st.error(f"Erreur pendant le backtest : {e}")
else:
    st.info("Configure les paramètres dans la barre latérale, choisis une stratégie, puis clique sur **Lancer le backtest**.")

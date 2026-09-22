# Backtester Forex — usage local

Application Streamlit pour backtester tes robots (stratégies) forex écrits en Python,
avec [Backtrader](https://www.backtrader.com/) comme moteur de backtest et les données
historiques de Yahoo Finance.

## Installation (une seule fois)

Ouvre un terminal dans ce dossier et lance :

```bash
python -m venv venv
source venv/bin/activate        # sur Windows : venv\Scripts\activate
pip install -r requirements.txt
```

## Lancer l'application

```bash
streamlit run app.py
```

Ça ouvre automatiquement l'app dans ton navigateur (en général sur `http://localhost:8501`).
Tant que tu ne fermes pas le terminal, elle reste accessible en local.

## Utilisation

1. Choisis la paire forex et la période dans la barre latérale.
2. Colle ta stratégie dans la zone de texte principale — elle doit être une classe
   `MyStrategy(bt.Strategy)` (voir l'exemple pré-rempli et le guide dans l'app).
3. Clique sur **Lancer le backtest**.
4. Les résultats (P&L, drawdown, Sharpe, win rate, courbe d'équité) s'affichent directement.

## Limites à connaître

- Les données Yahoo Finance en intraday (1h, 30m, 15m) ne remontent que sur les ~60
  derniers jours. Pour du long historique, utilise le timeframe journalier (`1d`).
- Les données Yahoo Finance forex sont de qualité correcte mais moins précises que les
  données tick de ton broker (pas de spread/slippage réaliste). Utile pour valider une
  logique de stratégie, moins pour un chiffrage précis de rentabilité.
- Si ton robot est déjà écrit pour MT5 (MQL) ou cTrader (C#), il faudra le retraduire
  en Python/Backtrader — la logique (indicateurs, conditions d'entrée/sortie) se transpose
  généralement assez directement.

## Si tu veux passer sur des données plus précises plus tard

Tu peux remplacer la fonction `fetch_data()` dans `app.py` par un appel à l'API de ton
broker (beaucoup, comme OANDA ou Dukascopy, exposent une API avec historique tick/minute),
sans changer le reste du code.

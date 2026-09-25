"""
Module de sandboxing pour l'execution de strategies utilisateur.
Trois couches de protection :
  1. Analyse statique du code (AST) : bloque imports, attributs prives,
     fonctions dangereuses.
  2. Environnement d'execution restreint (builtins limites).
  3. Isolation en processus separe avec timeout CPU strict.

AVERTISSEMENT HONNETE : ceci reduit fortement le risque mais ne constitue
pas une garantie de securite absolue equivalente a une isolation systeme
complete (conteneur/VM dediee). Adapte a un usage educatif/personnel sur
un hebergement gratuit partage, pas a un environnement a haute sensibilite.
"""

import ast
import multiprocessing
import resource

import backtrader as bt
import pandas as pd


# ---------------------------------------------------------------------
# Couche 1 : analyse statique du code (AST)
# ---------------------------------------------------------------------
ALLOWED_NODES = (
    ast.Module, ast.ClassDef, ast.FunctionDef, ast.arguments, ast.arg,
    ast.Load, ast.Store, ast.Del,
    ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return, ast.Pass,
    ast.Break, ast.Continue,
    ast.If, ast.For, ast.While, ast.Expr,
    ast.Call, ast.Attribute, ast.Name, ast.Constant,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.And, ast.Or, ast.Not, ast.Invert, ast.UAdd, ast.USub,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Subscript, ast.Slice,
    ast.keyword, ast.Starred,
    ast.IfExp, ast.comprehension, ast.ListComp, ast.DictComp,
    ast.SetComp, ast.GeneratorExp, ast.Lambda,
)

FORBIDDEN_NAMES = {
    "exec", "eval", "compile", "__import__", "open", "input",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "type", "super", "memoryview", "breakpoint", "help", "exit", "quit",
    "os", "sys", "subprocess", "socket", "requests", "shutil", "pathlib",
    "importlib", "ctypes", "pickle", "marshal", "builtins", "classmethod",
    "staticmethod", "property", "object",
}


def validate_strategy_code(code: str):
    """Leve une ValueError explicite si le code contient une construction
    non autorisee. Sinon, ne renvoie rien (code valide)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"Erreur de syntaxe dans ton code : {e}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError(
                "Les imports ne sont pas autorisés pour des raisons de sécurité. "
                "'bt' (backtrader) est déjà fourni, utilise-le directement."
            )
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ValueError("'global' et 'nonlocal' ne sont pas autorisés.")
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError(
                f"Construction Python non autorisée : {type(node).__name__}. "
                "Cette app n'autorise que la logique de trading standard "
                "(conditions, boucles, calculs, appels à self./bt.)."
            )
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"Utilisation interdite : '{node.id}'.")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(
                f"Accès à un attribut privé/spécial interdit : '.{node.attr}'. "
                "C'est bloqué pour empêcher le contournement du bac à sable."
            )

    has_strategy_class = any(
        isinstance(n, ast.ClassDef) and n.name == "MyStrategy"
        for n in ast.walk(tree)
    )
    if not has_strategy_class:
        raise ValueError("Ton code doit définir une classe nommée exactement 'MyStrategy'.")


# ---------------------------------------------------------------------
# Couche 2 : environnement d'execution restreint
# ---------------------------------------------------------------------
def _safe_builtins():
    return {
        "True": True, "False": False, "None": None,
        "abs": abs, "min": min, "max": max, "round": round, "len": len,
        "range": range, "enumerate": enumerate, "sum": sum, "sorted": sorted,
        "int": int, "float": float, "bool": bool, "str": str,
        "list": list, "dict": dict, "tuple": tuple, "set": set, "zip": zip,
        "isinstance": isinstance,
    }


def load_strategy_class(code: str):
    validate_strategy_code(code)
    namespace = {"__builtins__": _safe_builtins(), "bt": bt}
    exec(code, namespace)  # noqa: S102 -- code deja valide par l'AST ci-dessus
    return namespace["MyStrategy"]


# ---------------------------------------------------------------------
# Couche 3 : isolation en processus separe avec timeout CPU
# ---------------------------------------------------------------------
CPU_TIME_LIMIT_SECONDS = 20
WALL_CLOCK_TIMEOUT_SECONDS = 30


def _run_in_subprocess(code, data, cash, commission, size_pct, queue):
    try:
        try:
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (CPU_TIME_LIMIT_SECONDS, CPU_TIME_LIMIT_SECONDS),
            )
        except Exception:
            pass  # setrlimit indisponible sur certains systemes (ex: Windows) -- on continue quand meme

        StrategyClass = load_strategy_class(code)

        cerebro = bt.Cerebro()
        cerebro.addstrategy(StrategyClass)
        feed = bt.feeds.PandasData(dataname=data, openinterest=-1)
        cerebro.adddata(feed)
        cerebro.broker.setcash(cash)
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

        def to_plain(obj):
            if isinstance(obj, dict):
                return {k: to_plain(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [to_plain(v) for v in obj]
            return obj

        trades = to_plain(strat.analyzers.trades.get_analysis())
        dd = to_plain(strat.analyzers.drawdown.get_analysis())
        sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio")
        time_return = dict(strat.analyzers.time_return.get_analysis())

        queue.put({
            "ok": True,
            "start_value": start_value,
            "end_value": end_value,
            "trades": trades,
            "drawdown": dd,
            "sharpe": sharpe,
            "time_return": time_return,
        })
    except Exception as e:
        queue.put({"ok": False, "error": str(e)})


def run_backtest_sandboxed(code: str, data: pd.DataFrame, cash: float, commission: float, size_pct: float):
    """Valide et execute le code utilisateur dans un processus isole,
    avec timeout. Renvoie le dict de resultats ou leve une Exception claire."""
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_run_in_subprocess,
        args=(code, data, cash, commission, size_pct, queue),
    )
    process.start()
    process.join(timeout=WALL_CLOCK_TIMEOUT_SECONDS)

    if process.is_alive():
        process.terminate()
        process.join()
        raise TimeoutError(
            f"Ton code a dépassé la limite de {WALL_CLOCK_TIMEOUT_SECONDS}s "
            "(boucle infinie ou calcul trop lourd ?). Exécution interrompue."
        )

    if queue.empty():
        raise RuntimeError(
            "Le processus s'est arrêté sans résultat (dépassement mémoire ou "
            "crash). Vérifie que ton code ne consomme pas trop de ressources."
        )

    result = queue.get()
    if not result.get("ok"):
        raise ValueError(result.get("error", "Erreur inconnue pendant le backtest."))
    return result

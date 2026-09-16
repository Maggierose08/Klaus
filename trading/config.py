import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADING_DIR = os.path.dirname(os.path.abspath(__file__))

# Hardcoded to the paper-trading endpoint on purpose, with no setting to
# override it to the live endpoint (api.alpaca.markets) anywhere in this
# codebase. This is the actual safety boundary for "paper only, never live" -
# not a flag that could be flipped by mistake.
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")


def require_alpaca_credentials():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment. "
            "Use paper-trading keys from the Alpaca dashboard, not live keys."
        )


# Symbols the pipeline researches and (if a signal clears risk_manager) trades.
WATCHLIST = ["AAPL", "MSFT", "SPY"]

JOURNAL_PATH = os.path.join(TRADING_DIR, "journal.json")
SUMMARIES_DIR = os.path.join(TRADING_DIR, "summaries")

# Risk limits enforced by risk_manager.py. Conservative defaults for a paper
# account; tune as you get a feel for how the pipeline behaves.
MAX_POSITION_NOTIONAL_USD = 1000.0       # max $ in a single symbol per trade
MAX_DAILY_TRADES = 5                     # max approved trades per calendar day
MAX_PORTFOLIO_NOTIONAL_USD = 5000.0      # max total $ deployed across positions

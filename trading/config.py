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

# When set, journal entries and daily summaries are read/written to this GCS
# bucket instead of local disk. Required on Cloud Run: the pipeline job, the
# daily-summary job, and the web service each run in their own ephemeral
# container and don't share a filesystem, so local files wouldn't persist
# between runs or be visible to the web service. Left unset for local/laptop
# runs (Task Scheduler .bat files), which keep using local files as before.
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")

# Risk limits enforced by risk_manager.py. Conservative defaults for a paper
# account; tune as you get a feel for how the pipeline behaves.
MAX_POSITION_NOTIONAL_USD = 1000.0       # max $ in a single symbol per trade
MAX_DAILY_TRADES = 5                     # max approved trades per calendar day
MAX_PORTFOLIO_NOTIONAL_USD = 5000.0      # max total $ deployed across positions

# Code mode (Klaus editing this repo from the trading-web page). The actual
# Claude Code CLI run happens in a separate Cloud Run Job (trading-codemode),
# triggered by trading/codemode.py from the web service - see trading/DEPLOY.md.
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Maggierose08/Klaus")
GCP_PROJECT = os.environ.get("GCP_PROJECT")
GCP_REGION = os.environ.get("GCP_REGION", "us-east4")
CODEMODE_JOB_NAME = os.environ.get("CODEMODE_JOB_NAME", "trading-codemode")

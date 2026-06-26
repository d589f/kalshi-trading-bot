"""Configuration constants."""
import os

BINANCE_TRADES_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_DEPTH_WS = "wss://stream.binance.com:9443/ws/btcusdt@depth5@100ms"
POLY_RTDS_WS = "wss://ws-live-data.polymarket.com"
POLY_CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
BITCOIN_TAG_ID = "235"
HTTP_PORT = 8888
P_MODEL_KAPPA = 0.5
SIGMA_DEFAULT = 0.0005
P_MODEL_THRESHOLD = 0.60
DELTA_THRESHOLD = 20
TAU_MODE = "linear"          # "linear" or "sqrt"
SIGMA_TYPE = "realized5_pmin"
TRADE_SIDE = "NO"            # "NO", "YES", or "BOTH"
MAX_ENTRY_PRICE = 0.95
PAPER_STAKE = 10
PAPER_MIN_STAKE = 5
WINDOW_MINUTES = 5
OB_MAX_LEVELS = 20

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "live_data.db")

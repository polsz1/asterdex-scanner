#!/usr/bin/env python3
"""
AsterDex Volatility Scanner v3.3
==================================
Nowości vs v3.2:
  1. LEPSZY SCORING KIERUNKOWY — divergencje
     Klasyczne divergencje techniczne:
       Bull divergence:  cena spada LUB płaska, ale OI rośnie + RSI rośnie → akumulacja
       Bear divergence:  cena rośnie, ale OI spada + RSI spada → dystrybucja
       RSI/cena div:     cena robi nowe high, RSI nie potwierdza → osłabienie trendu
       Funding squeeze:  funding ekstremalny → kontrariańskie odwrócenie
     Wynik: osobna kolumna "div" + wpływ na bull/bear prob

  2. ANALIZA HISTORII CSV (--history)
     - Które tokeny najczęściej trafiają do TOP
     - Trend score'ów w czasie dla każdego tokena
     - Średni bull/bear % per token
     - Tabela "hall of fame" + tokeny z rosnącym trendem

Uruchomienie:
  python aster_scanner.py                    # normalny skan
  python aster_scanner.py --history          # analiza historii
  python aster_scanner.py --history --top 20 # TOP 20 z historii
  python aster_scanner.py --diag             # tylko diagnostyka
  python aster_scanner.py --vol-stats        # rozkład volume

Wymagania:
  pip install requests rich
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import requests
from rich.console import Console
from rich.table import Table
from rich import box
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich.panel import Panel

# ============================================================
# KONFIGURACJA
# ============================================================
BASE_URL           = "https://fapi.asterdex.com"
TOP_N              = 50
REQUEST_TIMEOUT    = 10
RATE_LIMIT_DELAY   = 0.10
MIN_VOLUME_24H_USD = 10_000
OI_CACHE_FILE      = "aster_oi_cache.json"
HISTORY_CSV        = "aster_history.csv"
ONLY_ASCII_SYMBOLS = True

DEFAULT_MIN_SPIKE  = Decimal("1.5")
DEFAULT_MIN_SCORE  = Decimal("20.0")

# Wagi score bazowego
WEIGHT_PRICE_CHANGE = Decimal("1.0")
WEIGHT_VOLATILITY   = Decimal("0.5")
WEIGHT_VOL_SPIKE    = Decimal("10.0")
WEIGHT_OI_CHANGE    = Decimal("2.0")
WEIGHT_RSI          = Decimal("0.3")
WEIGHT_FUNDING      = Decimal("15.0")
WEIGHT_LS_RATIO     = Decimal("5.0")
WEIGHT_LIQ_RATIO    = Decimal("3.0")

# Wagi bull/bear probability (suma = 100)
BULL_SIGNALS = {
    "price_up":          10,
    "oi_rising":         12,
    "funding_low":        8,
    "rsi_below_50":       8,
    "rsi_not_oversold":   4,
    "vol_spike":         10,
    "ema_bull_4h":       14,
    "price_4h_up":       12,
    "score_rising":       8,
    "btc_independent":    8,
    # divergencje (zastępują L/S ratio który nie działa)
    "bull_div_oi_rsi":    6,   # bull divergence OI+RSI
}
BEAR_SIGNALS = {
    "price_down":        10,
    "oi_rising":         12,
    "funding_high":       8,
    "rsi_above_50":       8,
    "rsi_not_overbought": 4,
    "vol_spike":         10,
    "ema_bear_4h":       14,
    "price_4h_down":     12,
    "score_rising":       8,
    "btc_independent":    8,
    "bear_div_oi_rsi":    6,   # bear divergence OI+RSI
}

ENDPOINTS_AVAILABLE = {
    "funding":  True,
    "ls_ratio": True,
    "liq":      True,
    "oi_now":   True,
}

EXCLUDE_TOP_MARKETS = {
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","TRXUSDT","AVAXUSDT","TONUSDT",
    "DOTUSDT","LINKUSDT","MATICUSDT","LTCUSDT","BCHUSDT",
    "SHIBUSDT","UNIUSDT","ATOMUSDT","XLMUSDT","NEARUSDT",
    "APTUSDT","ARBUSDT","OPUSDT","FILUSDT","ETCUSDT",
    "HBARUSDT","VETUSDT","ICPUSDT","IMXUSDT","INJUSDT",
    "RNDRUSDT","RENDERUSDT","FTMUSDT","TIAUSDT","SUIUSDT",
    "SEIUSDT","LDOUSDT","KASUSDT","WIFUSDT","PEPEUSDT",
    "BONKUSDT","FLOKIUSDT",
}
EXCLUDE_STOCKS = {
    "AAPLUSDT","TSLAUSDT","GOOGLUSDT","GOOGUSDT","MSFTUSDT",
    "AMZNUSDT","METAUSDT","NFLXUSDT","NVDAUSDT","AMDUSDT",
    "COINUSDT","MSTRUSDT","GMEUSDT","AMCUSDT","SPYUSDT",
    "QQQUSDT","TQQQUSDT","IWMUSDT","DIAUSDT","HOODUSDT",
    "BABAUSDT","JPMUSDT","BACUSDT","WMTUSDT","DISUSDT",
    "PYPLUSDT","SQUSDT","PLTRUSDT","SHOPUSDT","UBERUSDT",
    "LYFTUSDT","ABNBUSDT","SNAPUSDT","TWTRUSDT","SNOWUSDT",
    "CRWDUSDT","ZMUSDT","RBLXUSDT","NIOUSDT","RIVNUSDT",
    "LCIDUSDT","FUSDT","GMUSDT","BRKUSDT",
}
EXCLUDE_COMMODITIES = {
    "XAUUSDT","XAGUSDT","GOLDUSDT","SILVERUSDT",
    "WTIUSDT","BRENTUSDT","OILUSDT","USOILUSDT","UKOILUSDT",
    "NATGASUSDT","COPPERUSDT","PLATUSDT","PALLADUSDT",
    "CORNUSDT","WHEATUSDT","SOYBEANUSDT",
}
EXCLUDE_ALL = EXCLUDE_TOP_MARKETS | EXCLUDE_STOCKS | EXCLUDE_COMMODITIES

console = Console()


# ============================================================
# DIVERGENCJE
# ============================================================
@dataclass
class Divergence:
    """Wynik analizy divergencji dla jednego symbolu."""
    bull_div: bool  = False   # cena spada/płaska + OI rośnie + RSI rośnie = akumulacja
    bear_div: bool  = False   # cena rośnie + OI spada + RSI spada = dystrybucja
    rsi_price_div_bull: bool = False  # cena low niższe, RSI low wyższe = bull hidden div
    rsi_price_div_bear: bool = False  # cena high wyższe, RSI high niższe = bear hidden div
    funding_squeeze_bull: bool = False  # funding ekstremalnie ujemny → potencjalny short squeeze
    funding_squeeze_bear: bool = False  # funding ekstremalnie wysoki → potencjalny long squeeze
    label: str = "—"          # czytelna etykieta do tabeli

    def build_label(self) -> None:
        parts = []
        if self.bull_div:             parts.append("[bright_green]BULL-DIV[/]")
        if self.rsi_price_div_bull:   parts.append("[bright_green]RSI-BULL[/]")
        if self.funding_squeeze_bull: parts.append("[bright_green]SQ-BULL[/]")
        if self.bear_div:             parts.append("[bright_red]BEAR-DIV[/]")
        if self.rsi_price_div_bear:   parts.append("[bright_red]RSI-BEAR[/]")
        if self.funding_squeeze_bear: parts.append("[bright_red]SQ-BEAR[/]")
        self.label = " ".join(parts) if parts else "[bright_black]—[/]"

    @property
    def bull_score(self) -> int:
        return (int(self.bull_div) + int(self.rsi_price_div_bull) +
                int(self.funding_squeeze_bull))

    @property
    def bear_score(self) -> int:
        return (int(self.bear_div) + int(self.rsi_price_div_bear) +
                int(self.funding_squeeze_bear))


def detect_divergences(
    price_change_pct: Decimal,
    price_4h_change_pct: Decimal,
    oi_change_pct: Decimal,
    rsi_14: Decimal,
    rsi_prev: Decimal,          # RSI sprzed ~4h (z klines)
    funding_rate_pct: Decimal,
    has_oi_delta: bool,
    has_funding: bool,
    closes_1h: list,            # ostatnie 28 zamknięć 1h
) -> Divergence:
    """
    Wykryj klasyczne divergencje techniczne.

    Definicje:
    ──────────
    BULL divergence (akumulacja):
      - cena 24h spada LUB jest płaska (< +2%)
      - OI rośnie (nowe pozycje mimo spadku — ktoś kupuje)
      - RSI rośnie lub jest < 45 (potencjał odbicia)

    BEAR divergence (dystrybucja):
      - cena 24h rośnie (> +2%)
      - OI spada (pozycje zamykane mimo wzrostu — brak przekonania)
      - RSI spada lub jest > 60 (wykupienie bez siły)

    RSI/price bull hidden divergence:
      - w ostatnich 28 świeczkach 1h: cena zrobiła nowe minimum (low < poprzednie low)
      - ale RSI zrobiło wyższe minimum — siła kupujących mimo niższej ceny

    RSI/price bear hidden divergence:
      - cena zrobiła nowe maximum
      - ale RSI zrobiło niższe maximum — słabość kupujących

    Funding squeeze:
      - funding < -0.05% → zbyt dużo shortów → potencjalny short squeeze (bull)
      - funding > +0.15% → zbyt dużo longów → potencjalny long squeeze (bear)
    """
    div = Divergence()

    # BULL divergence
    if has_oi_delta:
        price_flat_or_down = price_change_pct < Decimal("2")
        oi_acc = oi_change_pct > Decimal("2")
        rsi_low = rsi_14 < Decimal("45")
        rsi_rising = rsi_14 > rsi_prev
        if price_flat_or_down and oi_acc and (rsi_low or rsi_rising):
            div.bull_div = True

    # BEAR divergence
    if has_oi_delta:
        price_rising = price_change_pct > Decimal("2")
        oi_dist = oi_change_pct < Decimal("-2")
        rsi_high = rsi_14 > Decimal("60")
        rsi_falling = rsi_14 < rsi_prev
        if price_rising and oi_dist and (rsi_high or rsi_falling):
            div.bear_div = True

    # RSI/price hidden divergences (z klines 1h)
    if len(closes_1h) >= 10:
        try:
            # Podziel na dwie połowy — porównaj low i high
            mid = len(closes_1h) // 2
            first_half  = closes_1h[:mid]
            second_half = closes_1h[mid:]
            first_low  = min(first_half)
            second_low = min(second_half)
            first_high = max(first_half)
            second_high= max(second_half)

            # Bull hidden div: nowe price low + RSI wyższe (rsi_prev = RSI z pierwszej połowy)
            if second_low < first_low * Decimal("0.99") and rsi_14 > rsi_prev + Decimal("3"):
                div.rsi_price_div_bull = True

            # Bear hidden div: nowe price high + RSI niższe
            if second_high > first_high * Decimal("1.01") and rsi_14 < rsi_prev - Decimal("3"):
                div.rsi_price_div_bear = True
        except Exception:
            pass

    # Funding squeeze
    if has_funding:
        if funding_rate_pct < Decimal("-0.05"):
            div.funding_squeeze_bull = True
        if funding_rate_pct > Decimal("0.15"):
            div.funding_squeeze_bear = True

    div.build_label()
    return div


# ============================================================
# DATACLASS
# ============================================================
@dataclass
class MarketStats:
    symbol: str
    last_price: Decimal           = Decimal("0")
    price_change_pct: Decimal     = Decimal("0")
    high_24h: Decimal             = Decimal("0")
    low_24h: Decimal              = Decimal("0")
    volume_24h_usd: Decimal       = Decimal("0")
    volume_last_1h_usd: Decimal   = Decimal("0")
    volume_avg_1h_usd: Decimal    = Decimal("0")
    volume_spike_ratio: Decimal   = Decimal("0")
    oi_now: Decimal               = Decimal("0")
    oi_change_pct: Decimal        = Decimal("0")
    volatility_range_pct: Decimal = Decimal("0")
    rsi_14: Decimal               = Decimal("50")
    rsi_prev: Decimal             = Decimal("50")   # RSI z ~połowy okna 1h
    funding_rate_pct: Decimal     = Decimal("0")
    long_short_ratio: Decimal     = Decimal("1")
    liq_ratio_pct: Decimal        = Decimal("0")
    setup: str                    = "NEUTRAL"
    score: Decimal                = Decimal("0")
    price_4h_change_pct: Decimal  = Decimal("0")
    ema_bull_4h: bool             = False
    btc_corr: Decimal             = Decimal("1")
    score_prev: Decimal           = Decimal("0")
    score_trend: Decimal          = Decimal("0")
    bull_prob: int                = 0
    bear_prob: int                = 0
    has_funding: bool             = False
    has_oi_delta: bool            = False
    closes_1h: list               = None   # surowe dane do divergencji
    div: Divergence               = None

    def __post_init__(self):
        if self.closes_1h is None:
            self.closes_1h = []
        if self.div is None:
            self.div = Divergence()

    def calc_rsi(self, closes: list) -> None:
        if len(closes) < 15:
            self.rsi_14 = Decimal("50"); return
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, Decimal("0")))
            losses.append(max(-d, Decimal("0")))
        # Pełny RSI na wszystkich świeczkach
        ag = sum(gains[:14]) / Decimal("14")
        al = sum(losses[:14]) / Decimal("14")
        for i in range(14, len(gains)):
            ag = (ag * Decimal("13") + gains[i]) / Decimal("14")
            al = (al * Decimal("13") + losses[i]) / Decimal("14")
        if al == 0:
            self.rsi_14 = Decimal("100")
        else:
            self.rsi_14 = Decimal("100") - (Decimal("100") / (Decimal("1") + ag/al))

        # RSI na pierwszej połowie okna (do divergencji)
        half = closes[:len(closes)//2]
        if len(half) >= 15:
            g2, l2 = [], []
            for i in range(1, len(half)):
                d = half[i] - half[i-1]
                g2.append(max(d, Decimal("0")))
                l2.append(max(-d, Decimal("0")))
            ag2 = sum(g2[:14]) / Decimal("14")
            al2 = sum(l2[:14]) / Decimal("14")
            for i in range(14, len(g2)):
                ag2 = (ag2 * Decimal("13") + g2[i]) / Decimal("14")
                al2 = (al2 * Decimal("13") + l2[i]) / Decimal("14")
            if al2 == 0:
                self.rsi_prev = Decimal("100")
            else:
                self.rsi_prev = Decimal("100") - (Decimal("100") / (Decimal("1") + ag2/al2))
        else:
            self.rsi_prev = self.rsi_14

    @staticmethod
    def _ema(prices: list, period: int) -> Decimal:
        if len(prices) < period:
            return prices[-1] if prices else Decimal("0")
        k = Decimal("2") / (Decimal(period) + Decimal("1"))
        v = sum(prices[:period]) / Decimal(period)
        for p in prices[period:]:
            v = p * k + v * (Decimal("1") - k)
        return v

    def determine_setup(self) -> None:
        price_up   = self.price_change_pct > 0
        price_down = self.price_change_pct < 0
        oi_ok      = (not self.has_oi_delta) or (self.oi_change_pct > 0)
        fund_low   = self.funding_rate_pct < Decimal("0.05")
        fund_high  = self.funding_rate_pct > Decimal("0.05")

        if price_up and oi_ok and fund_low and self.rsi_14 < Decimal("70"):
            self.setup = "LONG"
        elif price_down and oi_ok and fund_high and self.rsi_14 > Decimal("30"):
            self.setup = "SHORT"
        else:
            self.setup = "NEUTRAL"

    def calc_score(self) -> None:
        self.score = (
            abs(self.price_change_pct)       * WEIGHT_PRICE_CHANGE
            + self.volatility_range_pct      * WEIGHT_VOLATILITY
            + min(self.volume_spike_ratio, Decimal("10")) * WEIGHT_VOL_SPIKE
            + abs(self.oi_change_pct)        * WEIGHT_OI_CHANGE
            + abs(self.rsi_14 - Decimal("50")) * WEIGHT_RSI
            + abs(self.funding_rate_pct)     * WEIGHT_FUNDING
            + abs(self.long_short_ratio - Decimal("1")) * WEIGHT_LS_RATIO
            + self.liq_ratio_pct             * WEIGHT_LIQ_RATIO
        )

    def calc_direction_prob(self) -> None:
        """
        Heurystyczny scoring kierunkowy z uwzględnieniem divergencji.
        NIE jest to backtestowane prawdopodobieństwo statystyczne.
        """
        b = e = 0

        # Bazowe sygnały
        if self.price_change_pct > 0:   b += BULL_SIGNALS["price_up"]
        elif self.price_change_pct < 0: e += BEAR_SIGNALS["price_down"]

        if self.has_oi_delta:
            if self.oi_change_pct > 0:
                if self.price_change_pct >= 0: b += BULL_SIGNALS["oi_rising"]
                else:                          e += BEAR_SIGNALS["oi_rising"]

        if self.has_funding:
            if self.funding_rate_pct < Decimal("0.03"):   b += BULL_SIGNALS["funding_low"]
            elif self.funding_rate_pct > Decimal("0.05"): e += BEAR_SIGNALS["funding_high"]

        if self.rsi_14 < Decimal("50"):   b += BULL_SIGNALS["rsi_below_50"]
        else:                              e += BEAR_SIGNALS["rsi_above_50"]
        if self.rsi_14 > Decimal("30"):   b += BULL_SIGNALS["rsi_not_oversold"]
        if self.rsi_14 < Decimal("70"):   e += BEAR_SIGNALS["rsi_not_overbought"]

        if self.volume_spike_ratio >= Decimal("1.5"):
            if self.price_change_pct >= 0: b += BULL_SIGNALS["vol_spike"]
            else:                          e += BEAR_SIGNALS["vol_spike"]

        if self.ema_bull_4h: b += BULL_SIGNALS["ema_bull_4h"]
        else:                e += BEAR_SIGNALS["ema_bear_4h"]

        if self.price_4h_change_pct > 0:   b += BULL_SIGNALS["price_4h_up"]
        elif self.price_4h_change_pct < 0: e += BEAR_SIGNALS["price_4h_down"]

        if self.score_trend > 0:
            if b >= e: b += BULL_SIGNALS["score_rising"]
            else:      e += BEAR_SIGNALS["score_rising"]

        if self.btc_corr < Decimal("0.5"):
            if b >= e: b += BULL_SIGNALS["btc_independent"]
            else:      e += BEAR_SIGNALS["btc_independent"]

        # Divergencje — mogą odwrócić lub wzmocnić sygnał
        if self.div:
            # Bull divergence kontrariańsko wzmacnia bulla nawet gdy cena spada
            if self.div.bull_div:
                b += BULL_SIGNALS["bull_div_oi_rsi"]
            if self.div.rsi_price_div_bull:
                b += BULL_SIGNALS["bull_div_oi_rsi"] // 2
            if self.div.funding_squeeze_bull:
                b += BULL_SIGNALS["bull_div_oi_rsi"]

            if self.div.bear_div:
                e += BEAR_SIGNALS["bear_div_oi_rsi"]
            if self.div.rsi_price_div_bear:
                e += BEAR_SIGNALS["bear_div_oi_rsi"] // 2
            if self.div.funding_squeeze_bear:
                e += BEAR_SIGNALS["bear_div_oi_rsi"]

        total = sum(BULL_SIGNALS.values())
        self.bull_prob = min(int(round(b / total * 100)), 100)
        self.bear_prob = min(int(round(e / total * 100)), 100)


# ============================================================
# API
# ============================================================
def api_get(path: str, params: Optional[dict] = None, silent: bool = False):
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params or {}, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            if not silent: console.print(f"[yellow]429 ({path}) — pauza 5s[/]")
            time.sleep(5)
            r = requests.get(f"{BASE_URL}{path}", params=params or {}, timeout=REQUEST_TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        if not silent: console.print(f"[red]API err {path}: {str(e)[:60]}[/]")
        return None


# ============================================================
# DIAGNOSTYKA
# ============================================================
PROBE_SYMBOL = "WLFIUSDT"

def run_diagnostics() -> dict:
    probes = [
        ("klines_1h",      "/fapi/v1/klines",
         {"symbol": PROBE_SYMBOL, "interval": "1h", "limit": 3}),
        ("klines_4h",      "/fapi/v1/klines",
         {"symbol": PROBE_SYMBOL, "interval": "4h", "limit": 3}),
        ("openInterest",   "/fapi/v1/openInterest",
         {"symbol": PROBE_SYMBOL}),
        ("premiumIndex",   "/fapi/v1/premiumIndex",
         {"symbol": PROBE_SYMBOL}),
        ("fundingRate",    "/fapi/v1/fundingRate",
         {"symbol": PROBE_SYMBOL, "limit": 1}),
        ("globalLS",       "/fapi/v1/globalLongShortAccountRatio",
         {"symbol": PROBE_SYMBOL, "period": "1h", "limit": 1}),
        ("allForceOrders", "/fapi/v1/allForceOrders",
         {"symbol": PROBE_SYMBOL, "limit": 5}),
    ]
    results = {}
    for name, path, params in probes:
        data = api_get(path, params, silent=True)
        results[name] = data is not None and data != [] and data != {}
    return results


# ============================================================
# FETCH HELPERS
# ============================================================
def fetch_exchange_info() -> list:
    data = api_get("/fapi/v1/exchangeInfo")
    if not data: return []
    out = []
    for s in data.get("symbols", []):
        sym = s.get("symbol", "")
        if s.get("quoteAsset") != "USDT": continue
        if s.get("status") != "TRADING": continue
        ct = s.get("contractType", "")
        if ct and ct != "PERPETUAL": continue
        if sym in EXCLUDE_ALL: continue
        if ONLY_ASCII_SYMBOLS and not sym.isascii(): continue
        out.append(sym)
    return out


def fetch_24hr_tickers() -> dict:
    data = api_get("/fapi/v1/ticker/24hr")
    return {d["symbol"]: d for d in data} if isinstance(data, list) else {}


def fetch_klines(symbol: str, interval: str, limit: int) -> list:
    data = api_get("/fapi/v1/klines",
                   {"symbol": symbol, "interval": interval, "limit": limit}, silent=True)
    return data if isinstance(data, list) else []


def fetch_open_interest(symbol: str) -> Optional[Decimal]:
    data = api_get("/fapi/v1/openInterest", {"symbol": symbol}, silent=True)
    try:
        return Decimal(str(data.get("openInterest","0"))) if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_funding_rate(symbol: str) -> Optional[Decimal]:
    for path, params in [
        ("/fapi/v1/premiumIndex", {"symbol": symbol}),
        ("/fapi/v1/fundingRate",  {"symbol": symbol, "limit": 1}),
    ]:
        data = api_get(path, params, silent=True)
        if isinstance(data, dict) and "lastFundingRate" in data:
            try: return Decimal(str(data["lastFundingRate"]))
            except Exception: pass
        if isinstance(data, list) and data and "fundingRate" in data[0]:
            try: return Decimal(str(data[0]["fundingRate"]))
            except Exception: pass
    return None


def fetch_btc_closes(limit: int = 28) -> list:
    klines = fetch_klines("BTCUSDT", "1h", limit)
    try: return [Decimal(str(k[4])) for k in klines]
    except Exception: return []


def calc_correlation(a: list, b: list) -> Decimal:
    n = min(len(a), len(b))
    if n < 5: return Decimal("1")
    def pct(p):
        return [(p[i]-p[i-1])/p[i-1]*Decimal("100") for i in range(1,len(p))]
    da = pct(a[-n:]); db = pct(b[-n:])
    m = min(len(da), len(db))
    if m < 4: return Decimal("1")
    ma = sum(da[:m])/Decimal(m); mb = sum(db[:m])/Decimal(m)
    num  = sum((da[i]-ma)*(db[i]-mb) for i in range(m))
    dena = sum((x-ma)**2 for x in da[:m])**Decimal("0.5")
    denb = sum((x-mb)**2 for x in db[:m])**Decimal("0.5")
    if dena==0 or denb==0: return Decimal("1")
    return max(Decimal("-1"), min(Decimal("1"), num/(dena*denb)))


def volume_distribution(tickers: dict, symbols: list) -> dict:
    vols = [float(tickers.get(s,{}).get("quoteVolume",0)) for s in symbols]
    return {t: sum(1 for v in vols if v >= t)
            for t in [0, 1_000, 5_000, 10_000, 25_000, 50_000, 100_000]}


# ============================================================
# HISTORIA + OI CACHE
# ============================================================
def load_last_scores() -> dict:
    if not os.path.exists(HISTORY_CSV): return {}
    try:
        rows = list(csv.DictReader(open(HISTORY_CSV, encoding="utf-8")))
        if not rows: return {}
        last_ts = rows[-1].get("run_timestamp","")
        result = {}
        for row in reversed(rows):
            if row.get("run_timestamp") != last_ts: break
            try: result[row["symbol"]] = Decimal(str(row.get("score","0")))
            except Exception: pass
        return result
    except Exception:
        return {}


def load_oi_cache() -> dict:
    if not os.path.exists(OI_CACHE_FILE):
        return {"timestamp": 0, "oi": {}}
    try:
        with open(OI_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"timestamp": 0, "oi": {}}


def save_oi_cache(oi_data: dict):
    try:
        with open(OI_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": int(time.time()),
                       "oi": {s: str(v) for s,v in oi_data.items()}}, f, indent=2)
    except Exception as e:
        console.print(f"[red]save OI cache err: {e}[/]")


# ============================================================
# ANALIZA SYMBOLU
# ============================================================
def analyze_symbol(symbol: str, ticker_24h: dict, oi_cache: dict,
                   btc_closes: list, last_scores: dict,
                   min_volume: Decimal) -> Optional[MarketStats]:
    s = MarketStats(symbol=symbol)

    try:
        s.last_price          = Decimal(str(ticker_24h.get("lastPrice","0")))
        s.price_change_pct    = Decimal(str(ticker_24h.get("priceChangePercent","0")))
        s.high_24h            = Decimal(str(ticker_24h.get("highPrice","0")))
        s.low_24h             = Decimal(str(ticker_24h.get("lowPrice","0")))
        s.volume_24h_usd      = Decimal(str(ticker_24h.get("quoteVolume","0")))
        if s.low_24h > 0:
            s.volatility_range_pct = (s.high_24h-s.low_24h)/s.low_24h*Decimal("100")
    except Exception:
        return None

    if s.volume_24h_usd < min_volume:
        return None

    # 1h klines — vol spike + RSI + RSI_prev + BTC corr + surowe closes
    k1h = fetch_klines(symbol, "1h", 28)
    time.sleep(RATE_LIMIT_DELAY)
    if k1h and len(k1h) >= 15:
        try:
            qvols  = [Decimal(str(k[7])) for k in k1h]
            closes = [Decimal(str(k[4])) for k in k1h]
            s.closes_1h = closes
            s.volume_last_1h_usd = qvols[-1]
            s.volume_avg_1h_usd  = sum(qvols)/Decimal(len(qvols))
            if s.volume_avg_1h_usd > 0:
                s.volume_spike_ratio = s.volume_last_1h_usd/s.volume_avg_1h_usd
            s.calc_rsi(closes)   # ustawia rsi_14 i rsi_prev
            if len(btc_closes) >= 5:
                s.btc_corr = abs(calc_correlation(closes, btc_closes))
        except Exception:
            pass

    # 4h klines — EMA cross + 4h trend
    k4h = fetch_klines(symbol, "4h", 30)
    time.sleep(RATE_LIMIT_DELAY)
    if k4h and len(k4h) >= 5:
        try:
            c4h = [Decimal(str(k[4])) for k in k4h]
            s.ema_bull_4h = MarketStats._ema(c4h,9) > MarketStats._ema(c4h,21)
            if len(c4h) >= 5 and c4h[-5] > 0:
                s.price_4h_change_pct = (c4h[-1]-c4h[-5])/c4h[-5]*Decimal("100")
        except Exception:
            pass

    # Open Interest
    if ENDPOINTS_AVAILABLE.get("oi_now", True):
        oi = fetch_open_interest(symbol)
        time.sleep(RATE_LIMIT_DELAY)
        if oi is not None:
            s.oi_now = oi
            cached = oi_cache.get("oi",{}).get(symbol)
            if cached:
                try:
                    past = Decimal(cached)
                    if past > 0:
                        s.oi_change_pct = (s.oi_now-past)/past*Decimal("100")
                        s.has_oi_delta = True
                except Exception:
                    pass

    # Funding rate
    if ENDPOINTS_AVAILABLE.get("funding", True):
        fr = fetch_funding_rate(symbol)
        time.sleep(RATE_LIMIT_DELAY)
        if fr is not None:
            s.funding_rate_pct = fr * Decimal("100")
            s.has_funding = True

    # Divergencje
    s.div = detect_divergences(
        price_change_pct    = s.price_change_pct,
        price_4h_change_pct = s.price_4h_change_pct,
        oi_change_pct       = s.oi_change_pct,
        rsi_14              = s.rsi_14,
        rsi_prev            = s.rsi_prev,
        funding_rate_pct    = s.funding_rate_pct,
        has_oi_delta        = s.has_oi_delta,
        has_funding         = s.has_funding,
        closes_1h           = s.closes_1h,
    )

    # Score + trend + prob
    s.determine_setup()
    s.calc_score()
    s.score_prev  = last_scores.get(symbol, Decimal("0"))
    s.score_trend = s.score - s.score_prev
    s.calc_direction_prob()
    return s


# ============================================================
# LIGHTER.XYZ — cross-check dostępności par
# ============================================================
LIGHTER_BASE = "https://mainnet.zklighter.elliot.ai"
LIGHTER_CACHE_FILE = "lighter_markets_cache.json"
LIGHTER_CACHE_TTL  = 3600  # sekund — odśwież listę rynków co godzinę


def fetch_lighter_markets() -> dict[str, dict]:
    """
    Pobierz listę wszystkich rynków z Lighter.xyz.
    Używa cache (LIGHTER_CACHE_FILE) ważnego przez LIGHTER_CACHE_TTL sekund.
    Zwraca słownik: {base_symbol_upper: {market_index, symbol, base, status}}
    """
    # Sprawdź cache
    if os.path.exists(LIGHTER_CACHE_FILE):
        try:
            with open(LIGHTER_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            age = int(time.time()) - cached.get("timestamp", 0)
            if age < LIGHTER_CACHE_TTL and cached.get("markets"):
                console.print(f"  [bright_black]Lighter.xyz: cache ({age//60}min temu, "
                               f"{len(cached['markets'])} rynków)[/]")
                return cached["markets"]
        except Exception:
            pass

    console.print("  [bright_black]Lighter.xyz: pobieram listę rynków...[/]", end=" ")

    # Próbuj kilka możliwych endpointów
    endpoints = [
        f"{LIGHTER_BASE}/api/v1/orderBookDetails",
        f"{LIGHTER_BASE}/api/v1/orderBooks",
        f"{LIGHTER_BASE}/api/v1/markets",
    ]

    data = None
    used_url = ""
    for url in endpoints:
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                used_url = url
                break
            # 404 → spróbuj następny, inne błędy → loguj
            if r.status_code != 404:
                console.print(f"[yellow]HTTP {r.status_code} ({url.split('/')[-1]})[/]", end=" ")
        except Exception as e:
            console.print(f"[red]{str(e)[:40]}[/]", end=" ")
            continue

    if data is None:
        console.print("[red]brak odpowiedzi ze wszystkich endpointów[/]")
        return {}

    markets = {}

    # Format 1: {"order_book_details": [{symbol, market_index, ...}]}  ← Lighter mainnet
    order_books = data.get("order_book_details", [])
    # Format 2: {"order_books": [...]}
    if not order_books:
        order_books = data.get("order_books", [])
    # Format 3: {"markets": [...]}
    if not order_books:
        order_books = data.get("markets", [])
    # Format 4: lista bezpośrednio
    if not order_books and isinstance(data, list):
        order_books = data

    for ob in order_books:
        if not isinstance(ob, dict):
            continue
        # Różne nazwy pola symbolu w różnych wersjach API
        raw_sym = (ob.get("symbol")
                   or ob.get("market_symbol")
                   or ob.get("name")
                   or "")
        market_i = (ob.get("market_index")
                    or ob.get("id")
                    or ob.get("market_id")
                    or -1)
        status = ob.get("status", "active")
        if not raw_sym:
            continue
        # Wyciągnij base z "ETH-USDC", "WLFI/USDC", "WLFI-USD" itp.
        base = raw_sym.replace("/", "-").split("-")[0].upper().strip()
        if base:
            markets[base] = {
                "market_index": market_i,
                "symbol":       raw_sym,
                "base":         base,
                "status":       status,
            }

    # Zapisz cache
    try:
        with open(LIGHTER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": int(time.time()), "markets": markets}, f, indent=2)
    except Exception:
        pass

    if markets:
        console.print(f"[green]OK[/] — {len(markets)} rynków ({used_url.split('/')[-1]})")
    else:
        console.print(f"[yellow]odpowiedź OK ale 0 rynków — nieznany format. "
                      f"Klucze: {list(data.keys()) if isinstance(data, dict) else type(data)}[/]")
    return markets


def _aster_to_base(symbol: str) -> str:
    """Zamień symbol AsterDex na base token. WLFIUSDT → WLFI, 1000SHIBUSDT → 1000SHIB"""
    return symbol.removesuffix("USDT").removesuffix("BUSD").removesuffix("USDC").upper()


def render_lighter_crosscheck(top: list, lighter_markets: dict) -> None:
    """
    Wyświetl tabelę cross-checku: które tokeny z AsterDex TOP są też na Lighter.xyz.
    Dla tokenów dostępnych na Lighter pokazuje market_index i symbol.
    """
    if not lighter_markets:
        console.print("[yellow]  Lighter.xyz: brak danych o rynkach — pomiń cross-check.[/]")
        return

    found     = []
    not_found = []

    for s in top:
        base = _aster_to_base(s.symbol)
        if base in lighter_markets:
            found.append((s, lighter_markets[base]))
        else:
            not_found.append(s)

    console.print(f"[bold bright_black]━━━ CROSS-CHECK: Lighter.xyz ━━━[/]  "
                  f"[green]{len(found)} par dostępnych[/]  "
                  f"[bright_black]{len(not_found)} tylko na AsterDex[/]")

    if not found:
        console.print("  [bright_black]Żaden token z TOP nie jest dostępny na Lighter.xyz[/]")
        return

    t = Table(box=box.SIMPLE_HEAD, header_style="bold bright_black", expand=False)
    t.add_column("#",           style="bright_black", width=3)
    t.add_column("AsterDex",    style="white",        min_width=14)
    t.add_column("Lighter sym", style="cyan",         min_width=12)
    t.add_column("mkt idx",     justify="right",      style="bright_black")
    t.add_column("rank",        justify="right",      style="bright_black")
    t.add_column("score",       justify="right",      style="yellow")
    t.add_column("setup",       justify="center")
    t.add_column("bull%",       justify="right")
    t.add_column("bear%",       justify="right")
    t.add_column("divergencja", justify="left")

    for idx, (s, lm) in enumerate(found, 1):
        if s.setup == "LONG":    su_t = "[bold bright_green]LONG[/]"
        elif s.setup == "SHORT": su_t = "[bold bright_red]SHORT[/]"
        else:                    su_t = "[bright_black]—[/]"

        div_label = s.div.label if s.div else "[bright_black]—[/]"

        t.add_row(
            str(idx),
            s.symbol,
            lm["symbol"],
            str(lm["market_index"]),
            str(top.index(s) + 1),
            f"{s.score:.0f}",
            su_t,
            f"[bright_green]{s.bull_prob}%[/]",
            f"[bright_red]{s.bear_prob}%[/]",
            div_label,
        )
    console.print(t)

    # Tokeny których nie ma na Lighter
    if not_found:
        syms = "  ".join(f"[bright_black]{s.symbol}[/]" for s in not_found)
        console.print(f"  Tylko AsterDex: {syms}")
    console.print()


# ============================================================
# FILTRY
# ============================================================
def apply_filters(results: list, mode: str,
                  min_score: Decimal, min_spike: Decimal) -> list:
    out = []
    for s in results:
        if s.score < min_score: continue
        if s.volume_spike_ratio < min_spike: continue
        if mode == "long"  and s.setup != "LONG": continue
        if mode == "short" and s.setup != "SHORT": continue
        out.append(s)
    return out


# ============================================================
# RENDER — główna tabela skanera
# ============================================================
def prob_bar(pct: int, color: str, w: int = 7) -> str:
    filled = round(pct/100*w)
    return f"[{color}]{'█'*filled}{'░'*(w-filled)}[/] [bright_black]{pct}%[/]"


def render_scan_table(results: list, title: str) -> Table:
    has_funding = any(s.has_funding for s in results)
    has_oi_d    = any(s.has_oi_delta for s in results)
    has_div     = any(s.div and s.div.label != "[bright_black]—[/]" for s in results)

    t = Table(title=title, box=box.SIMPLE_HEAD,
              title_style="bold cyan", header_style="bold bright_black", expand=True)
    t.add_column("#",      style="bright_black", width=3)
    t.add_column("symbol", style="white", min_width=12)
    t.add_column("setup",  justify="center", width=7)
    t.add_column("price",  justify="right", style="white")
    t.add_column("24h%",   justify="right")
    t.add_column("4h%",    justify="right")
    t.add_column("spike",  justify="right")
    if has_oi_d:
        t.add_column("OI Δ%", justify="right")
    t.add_column("RSI",    justify="right")
    if has_funding:
        t.add_column("fund%",  justify="right")
    t.add_column("vol$M",  justify="right", style="bright_black")
    t.add_column("score",  justify="right", style="bold yellow")
    t.add_column("Δsc",    justify="right")
    t.add_column("🐂",     justify="left",  min_width=12)
    t.add_column("🐻",     justify="left",  min_width=12)
    if has_div:
        t.add_column("divergencja", justify="left", min_width=10)
    t.add_column("corr",   justify="right", style="bright_black")

    for i, s in enumerate(results, 1):
        pc = float(s.price_change_pct)
        p4 = float(s.price_4h_change_pct)
        sp = float(s.volume_spike_ratio)
        oi = float(s.oi_change_pct)
        rs = float(s.rsi_14)
        fr = float(s.funding_rate_pct)
        st = float(s.score_trend)
        cr = float(s.btc_corr)
        vm = float(s.volume_24h_usd / Decimal("1000000"))

        def pc_col(v, d=2):
            c = "bright_green" if v >= 0 else "bright_red"
            return f"[{c}]{v:+.{d}f}%[/]"

        if sp >= 3:    sp_t = f"[bold bright_red]{sp:.1f}x[/]"
        elif sp >= 2:  sp_t = f"[bold yellow]{sp:.1f}x[/]"
        elif sp >= 1.5:sp_t = f"[cyan]{sp:.1f}x[/]"
        else:          sp_t = f"[bright_black]{sp:.1f}x[/]"

        if rs >= 70:   rs_t = f"[bold bright_red]{rs:.0f}[/]"
        elif rs <= 30: rs_t = f"[bold bright_green]{rs:.0f}[/]"
        else:          rs_t = f"[bright_black]{rs:.0f}[/]"

        if s.setup == "LONG":    su_t = "[bold bright_green]LONG[/]"
        elif s.setup == "SHORT": su_t = "[bold bright_red]SHORT[/]"
        else:                    su_t = "[bright_black]—[/]"

        if st > 5:    st_t = f"[bold bright_green]▲{st:+.0f}[/]"
        elif st > 0:  st_t = f"[bright_green]▲{st:+.0f}[/]"
        elif st < -5: st_t = f"[bold bright_red]▼{st:.0f}[/]"
        elif st < 0:  st_t = f"[bright_red]▼{st:.0f}[/]"
        else:         st_t = "[bright_black]new[/]"

        cr_t = (f"[bold bright_green]{cr:.2f}[/]" if cr < 0.3
                else f"[yellow]{cr:.2f}[/]" if cr < 0.6
                else f"[bright_black]{cr:.2f}[/]")

        row = [str(i), s.symbol, su_t, f"{s.last_price}", pc_col(pc), pc_col(p4), sp_t]

        if has_oi_d:
            if abs(oi) >= 10:  oi_s = "bold bright_red" if oi>0 else "bold bright_magenta"
            elif abs(oi) >= 5: oi_s = "yellow"
            else:              oi_s = "bright_black"
            row.append(f"[{oi_s}]{oi:+.1f}%[/]" if s.has_oi_delta else "[bright_black]—[/]")

        row.append(rs_t)

        if has_funding:
            if fr > 0.15:    fr_t = f"[bold bright_red]{fr:+.3f}%[/]"
            elif fr > 0.05:  fr_t = f"[yellow]{fr:+.3f}%[/]"
            elif fr < -0.05: fr_t = f"[bold bright_green]{fr:+.3f}%[/]"
            else:            fr_t = f"[bright_black]{fr:+.3f}%[/]"
            row.append(fr_t if s.has_funding else "[bright_black]—[/]")

        row += [f"{vm:.3f}", f"{s.score:.0f}", st_t,
                prob_bar(s.bull_prob,"bright_green"),
                prob_bar(s.bear_prob,"bright_red")]

        if has_div:
            row.append(s.div.label if s.div else "[bright_black]—[/]")

        row.append(cr_t)
        t.add_row(*row)
    return t


# ============================================================
# HELPERS — regresja liniowa (slope) na liście floatów
# ============================================================
def linear_slope(values: list) -> float:
    n = len(values)
    if n < 2: return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    num = sum((xs[i]-mx) * (values[i]-my) for i in range(n))
    den = sum((x-mx)**2 for x in xs)
    return num / den if den != 0 else 0.0


def sparkline(values: list) -> str:
    blocks = ["▁","▂","▃","▄","▅","▆","▇","█"]
    mx = max(values) if max(values) > 0 else 1
    return "".join(blocks[min(7, int(v/mx*7))] for v in values)


# ============================================================
# RANKING SYGNAŁÓW Z HISTORII
# ============================================================
def _load_history_rows() -> tuple[list, list]:
    """Zwraca (rows, timestamps) lub ([], []) gdy brak pliku."""
    if not os.path.exists(HISTORY_CSV):
        return [], []
    try:
        with open(HISTORY_CSV, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        timestamps = list(dict.fromkeys(r["run_timestamp"] for r in rows))
        return rows, timestamps
    except Exception:
        return [], []


def _safe_int(val, default: int = 0) -> int:
    """Bezpieczne parsowanie int — puste stringi, None, Decimal repr → default."""
    try:
        v = str(val).strip()
        # Obsłuż "Decimal('50')" z starych CSV
        if v.startswith("Decimal("):
            v = v[9:-2]
        return int(float(v)) if v else default
    except Exception:
        return default

def _safe_float(val, default: float = 0.0) -> float:
    """Bezpieczne parsowanie float — puste stringi, None, Decimal repr → default."""
    try:
        v = str(val).strip()
        # Obsłuż "Decimal('50.82...')" z starych CSV gdzie zapisano obiekt Decimal
        if v.startswith("Decimal("):
            v = v[9:-2]
        return float(v) if v else default
    except Exception:
        return default


def _group_by_symbol(rows: list) -> dict:
    sym_data: dict[str, list] = defaultdict(list)
    for row in rows:
        sym = row.get("symbol","")
        if not sym:
            continue
        try:
            # Używamy _safe_* żeby puste pola ze starych wersji CSV nie psuły całego wiersza
            bull = _safe_int(row.get("bull_prob"), -1)   # -1 = brak danych
            bear = _safe_int(row.get("bear_prob"), -1)
            rsi  = _safe_float(row.get("rsi_14"), -1.0)  # -1 = brak danych
            sym_data[sym].append({
                "ts":           row.get("run_timestamp",""),
                "rank":         _safe_int(row.get("rank"), 99),
                "score":        _safe_float(row.get("score"), 0),
                "bull_prob":    bull,
                "bear_prob":    bear,
                "setup":        row.get("setup","NEUTRAL"),
                "price_chg":    _safe_float(row.get("price_change_pct_24h"), 0),
                "price_4h":     _safe_float(row.get("price_4h_change_pct"), 0),
                "oi_chg":       _safe_float(row.get("oi_change_pct"), 0),
                "rsi":          rsi,
                "funding":      _safe_float(row.get("funding_rate_pct"), 0),
                "spike":        _safe_float(row.get("volume_spike_ratio"), 0),
                "btc_corr":     _safe_float(row.get("btc_corr"), 1),
                "div_bull":     _safe_int(row.get("div_bull"), 0),
                "div_bear":     _safe_int(row.get("div_bear"), 0),
                "div_sq_bull":  _safe_int(row.get("div_squeeze_bull"), 0),
                "div_sq_bear":  _safe_int(row.get("div_squeeze_bear"), 0),
            })
        except Exception:
            continue
    return sym_data


def score_token_for_ranking(sym: str, data: list, n_runs: int) -> dict:
    """
    Oblicz 'reliability score' tokena na podstawie historii.

    Składniki:
      consistency   — jak często token trafia do TOP (% runów)
      avg_score     — średni score (aktywność)
      score_slope   — trend score (rośnie = rozgrzewa się)
      bull_clarity  — jak jednoznaczny jest sygnał bull (avg bull% - avg bear%)
      bear_clarity  — jak jednoznaczny jest sygnał bear (avg bear% - avg bull%)
      low_corr_bonus— bonus za niską korelację z BTC
      div_bonus     — bonus za historyczne divergencje
      spike_avg     — średni vol spike (ratio, cap 20x)

    Zabezpieczenia przed złymi danymi z poprzednich wersji CSV:
      - spike > 1000 → ignorowany (to był volume_24h_usd zamiast ratio)
      - bull_prob / bear_prob == 0 → dane z błędnego runu, ignorowane w avg
      - rsi < 0 lub > 100 → clamp do 50
    """
    scores = [d["score"]    for d in data]
    corrs  = [d["btc_corr"] for d in data]
    cnt    = len(data)

    # Sanity check: spike — jeśli > 500 to na pewno jest to volume w USD
    raw_spikes = [d["spike"] for d in data]
    clean_spikes = [s for s in raw_spikes if 0 < s <= 500]
    avg_spike = (sum(clean_spikes) / len(clean_spikes)) if clean_spikes else 1.0
    avg_spike = min(avg_spike, 20.0)

    # Sanity check: bull/bear prob
    # -1 = brak danych (stare CSV), 0 = naprawdę 0% (możliwe ale rzadkie)
    # Filtrujemy -1, a pary (0,0) też ignorujemy (błędne runy)
    valid_bulls = [d["bull_prob"] for d in data
                   if d["bull_prob"] >= 0 and d["bear_prob"] >= 0
                   and not (d["bull_prob"] == 0 and d["bear_prob"] == 0)]
    valid_bears = [d["bear_prob"] for d in data
                   if d["bull_prob"] >= 0 and d["bear_prob"] >= 0
                   and not (d["bull_prob"] == 0 and d["bear_prob"] == 0)]
    avg_bull = sum(valid_bulls) / len(valid_bulls) if valid_bulls else 50.0
    avg_bear = sum(valid_bears) / len(valid_bears) if valid_bears else 50.0

    # Sanity check: RSI — -1 = brak danych, zakres poza [0,100] = błąd
    valid_rsi = [max(0.0, min(100.0, d["rsi"])) for d in data if 0 <= d["rsi"] <= 100]
    avg_rsi = sum(valid_rsi) / len(valid_rsi) if valid_rsi else 50.0

    avg_score   = sum(scores) / cnt
    avg_corr    = sum(corrs)  / cnt
    consistency = cnt / n_runs * 100
    slope       = linear_slope(scores)

    bull_clarity = avg_bull - avg_bear   # > 0 = historycznie bullish
    bear_clarity = avg_bear - avg_bull   # > 0 = historycznie bearish

    div_bull_cnt = sum(d["div_bull"] + d["div_sq_bull"] for d in data)
    div_bear_cnt = sum(d["div_bear"] + d["div_sq_bear"] for d in data)

    # Reliability score — wszystkie składniki w podobnej skali (0-100)
    slope_norm = min(max(slope, 0), 50)

    # LONG reliability — bull_clarity MUSI być > 0
    if avg_bull <= avg_bear:
        long_reliability = 0.0
    else:
        long_reliability = (
            consistency    * 0.5
            + max(bull_clarity, 0) * 0.6
            + slope_norm   * 0.4
            + (1 - min(avg_corr, 1)) * 15
            + avg_spike    * 1.5
            + div_bull_cnt * 5
        )

    # SHORT reliability — bear_clarity MUSI być > 0
    if avg_bear <= avg_bull:
        short_reliability = 0.0
    else:
        short_reliability = (
            consistency    * 0.5
            + max(bear_clarity, 0) * 0.6
            + slope_norm   * 0.4
            + (1 - min(avg_corr, 1)) * 15
            + avg_spike    * 1.5
            + div_bear_cnt * 5
        )

    return {
        "sym":               sym,
        "cnt":               cnt,
        "consistency":       consistency,
        "avg_score":         avg_score,
        "avg_bull":          avg_bull,
        "avg_bear":          avg_bear,
        "avg_spike":         avg_spike,
        "avg_corr":          avg_corr,
        "avg_rsi":           avg_rsi,
        "slope":             slope,
        "bull_clarity":      bull_clarity,
        "bear_clarity":      bear_clarity,
        "long_reliability":  long_reliability,
        "short_reliability": short_reliability,
        "scores":            scores,
        "last":              data[-1],
        "div_bull_cnt":      div_bull_cnt,
        "div_bear_cnt":      div_bear_cnt,
    }


def render_signal_ranking(ranked: list, title: str, mode: str) -> Table:
    """Tabela rankingu sygnałów (mode='long' lub 'short')."""
    t = Table(title=title, box=box.SIMPLE_HEAD,
              title_style="bold cyan", header_style="bold bright_black", expand=False)
    t.add_column("#",          style="bright_black", width=3)
    t.add_column("symbol",     style="white", min_width=14)
    t.add_column("reliability",justify="right", style="bold yellow")
    t.add_column("consistency",justify="right")
    t.add_column("avg score",  justify="right")
    t.add_column("avg bull%",  justify="right")
    t.add_column("avg bear%",  justify="right")
    t.add_column("avg spike",  justify="right")
    t.add_column("avg corr",   justify="right")
    t.add_column("slope",      justify="right")
    t.add_column("hist",       justify="left",  min_width=8)
    t.add_column("ostatni setup", justify="center")

    for i, r in enumerate(ranked, 1):
        sl = r["scores"]
        sp = sparkline(sl)
        sl_t = f"[bright_black]{sp}[/] [bright_black]{sl[-1]:.0f}[/]"

        slope = r["slope"]
        if slope > 5:    slope_t = f"[bold bright_green]+{slope:.1f}[/]"
        elif slope > 0:  slope_t = f"[bright_green]+{slope:.1f}[/]"
        elif slope < -5: slope_t = f"[bold bright_red]{slope:.1f}[/]"
        else:            slope_t = f"[bright_red]{slope:.1f}[/]"

        corr = r["avg_corr"]
        corr_t = (f"[bold bright_green]{corr:.2f}[/]" if corr < 0.3
                  else f"[yellow]{corr:.2f}[/]" if corr < 0.6
                  else f"[bright_black]{corr:.2f}[/]")

        last_setup = r["last"]["setup"]
        if last_setup == "LONG":    su_t = "[bold bright_green]LONG[/]"
        elif last_setup == "SHORT": su_t = "[bold bright_red]SHORT[/]"
        else:                       su_t = "[bright_black]—[/]"

        rel_key = "long_reliability" if mode == "long" else "short_reliability"
        t.add_row(
            str(i), r["sym"],
            f"{r[rel_key]:.1f}",
            f"{r['consistency']:.0f}%",
            f"{r['avg_score']:.0f}",
            f"[bright_green]{r['avg_bull']:.0f}%[/]",
            f"[bright_red]{r['avg_bear']:.0f}%[/]",
            f"{r['avg_spike']:.1f}x",
            corr_t,
            slope_t,
            sl_t,
            su_t,
        )
    return t


def pick_top5(ranked_long: list, ranked_short: list, n_runs: int):
    """
    Wydrukuj TOP 5 LONG i TOP 5 SHORT z uzasadnieniem.
    Używa reliability score + dodatkowe warunki jakości.
    """
    def reasons_long(r: dict) -> list:
        out = []
        if r["consistency"] >= 80:
            out.append(f"był w TOP w {r['consistency']:.0f}% runów")
        if r["avg_bull"] >= 65:
            out.append(f"avg bull% {r['avg_bull']:.0f}% >> bear% {r['avg_bear']:.0f}%")
        elif r["avg_bull"] >= 55:
            out.append(f"avg bull% {r['avg_bull']:.0f}%")
        if r["slope"] > 3:
            out.append(f"score rośnie (slope +{r['slope']:.1f})")
        if r["avg_corr"] < 0.25:
            out.append(f"niezależny od BTC (corr {r['avg_corr']:.2f})")
        if 1.5 <= r["avg_spike"] <= 20:
            out.append(f"avg vol spike {r['avg_spike']:.1f}x")
        if r["div_bull_cnt"] > 0:
            out.append(f"bull divergencja w {r['div_bull_cnt']} runach")
        last = r["last"]
        last_rsi = max(0.0, min(100.0, last["rsi"]))
        # RSI < 5 lub > 95 przy małym rynku nocnym to prawdopodobnie błędne dane
        rsi_valid = 5 <= last_rsi <= 95
        if rsi_valid and last_rsi < 40:
            out.append(f"ostatni RSI {last_rsi:.0f} — wyprzedany (potencjał odbicia)")
        if last["setup"] == "LONG":
            out.append("ostatni setup: LONG")
        if not out:
            out.append(f"avg score {r['avg_score']:.0f}, consistency {r['consistency']:.0f}%")
        return out

    def reasons_short(r: dict) -> list:
        out = []
        if r["consistency"] >= 80:
            out.append(f"był w TOP w {r['consistency']:.0f}% runów")
        if r["avg_bear"] >= 55:
            out.append(f"avg bear% {r['avg_bear']:.0f}% >> bull% {r['avg_bull']:.0f}%")
        elif r["avg_bear"] >= 45:
            out.append(f"avg bear% {r['avg_bear']:.0f}%")
        if r["slope"] > 3:
            out.append(f"score rośnie (slope +{r['slope']:.1f})")
        if r["avg_corr"] < 0.25:
            out.append(f"niezależny od BTC (corr {r['avg_corr']:.2f})")
        if 1.5 <= r["avg_spike"] <= 20:
            out.append(f"avg vol spike {r['avg_spike']:.1f}x")
        if r["div_bear_cnt"] > 0:
            out.append(f"bear divergencja w {r['div_bear_cnt']} runach")
        last = r["last"]
        last_rsi = max(0.0, min(100.0, last["rsi"]))
        rsi_valid = 5 <= last_rsi <= 95
        if rsi_valid and last_rsi > 65:
            out.append(f"ostatni RSI {last_rsi:.0f} — wykupiony (ryzyko korekty)")
        if last["setup"] == "SHORT":
            out.append("ostatni setup: SHORT")
        if not out:
            out.append(f"avg score {r['avg_score']:.0f}, consistency {r['consistency']:.0f}%")
        return out

    console.print()
    console.print(Panel(
        "[bold bright_black]UWAGA:[/] Wytypowanie oparte na historycznych sygnałach z poprzednich runów.\n"
        "NIE jest to rekomendacja inwestycyjna. Każda pozycja wiąże się z ryzykiem straty.",
        box=box.ROUNDED, style="yellow"
    ))

    # ── TOP 5 LONG ────────────────────────────────────────────
    console.print()
    console.print("[bold bright_green]══ TOP 5 KANDYDATÓW DO LONGA ══[/]")
    if n_runs < 3:
        console.print(f"  [yellow]Mało runów ({n_runs}) — ranking będzie dokładniejszy po ≥5 runach.[/]")

    real_long = [r for r in ranked_long if r["long_reliability"] > 0]

    if not real_long:
        console.print("  [bright_black]Brak tokenów z historycznym sygnałem bullish "
                      "(potrzeba więcej runów).[/]")
    else:
        for i, r in enumerate(real_long[:5], 1):
            reasons = reasons_long(r)
            console.print(
                f"  [bold bright_green]{i}.[/] [bold white]{r['sym']}[/]  "
                f"reliability=[yellow]{r['long_reliability']:.1f}[/]  "
                f"avg bull%: [bright_green]{r['avg_bull']:.0f}%[/]  "
                f"avg bear%: [bright_red]{r['avg_bear']:.0f}%[/]  "
                f"consistency: [white]{r['consistency']:.0f}%[/]"
            )
            for reason in reasons:
                console.print(f"     [bright_black]→[/] {reason}")

    # ── TOP 5 SHORT ───────────────────────────────────────────
    console.print()
    console.print("[bold bright_red]══ TOP 5 KANDYDATÓW DO SHORTA ══[/]")

    # Filtruj tylko tokeny gdzie avg_bear > avg_bull (prawdziwy bear signal)
    real_short = [r for r in ranked_short if r["short_reliability"] > 0]

    if not real_short:
        console.print("  [bright_black]Brak tokenów z historycznym sygnałem bearish "
                      "(potrzeba więcej runów).[/]")
    else:
        for i, r in enumerate(real_short[:5], 1):
            last = r["last"]
            reasons = reasons_short(r)
            console.print(
                f"  [bold bright_red]{i}.[/] [bold white]{r['sym']}[/]  "
                f"reliability=[yellow]{r['short_reliability']:.1f}[/]  "
                f"avg bear%: [bright_red]{r['avg_bear']:.0f}%[/]  "
                f"avg bull%: [bright_green]{r['avg_bull']:.0f}%[/]  "
                f"consistency: [white]{r['consistency']:.0f}%[/]"
            )
            for reason in reasons:
                console.print(f"     [bright_black]→[/] {reason}")
    console.print()


# ============================================================
# ANALIZA HISTORII CSV
# ============================================================
def analyze_history(top_n: int = 20):
    """
    Pełna analiza aster_history.csv:
    1. Hall of Fame — najczęściej w TOP
    2. Rozgrzewające się — rosnący slope score
    3. Ranking sygnałów LONG / SHORT (reliability score)
    4. TOP 5 picks z uzasadnieniem
    """
    rows, timestamps = _load_history_rows()
    if not rows:
        console.print(f"[red]Brak pliku {HISTORY_CSV}. Uruchom najpierw normalny skan.[/]")
        return

    n_runs = len(timestamps)
    console.print()
    console.print(Panel(
        f"[bold cyan]Analiza historii AsterDex[/]\n"
        f"Plik: [white]{HISTORY_CSV}[/]  |  "
        f"Runów: [yellow]{n_runs}[/]  |  "
        f"Rekordów: [white]{len(rows)}[/]\n"
        f"Pierwszy: [bright_black]{timestamps[0][:19]}[/]  "
        f"Ostatni:  [bright_black]{timestamps[-1][:19]}[/]",
        box=box.ROUNDED, style="bright_black"
    ))

    if n_runs < 2:
        console.print("[yellow]Za mało runów — uruchom skaner kilka razy dla pełnej analizy.[/]")

    sym_data = _group_by_symbol(rows)

    # ── 1. Hall of Fame ───────────────────────────────────────
    appearances = sorted(
        {sym: len(data) for sym, data in sym_data.items()}.items(),
        key=lambda x: x[1], reverse=True
    )
    console.print()
    console.print("[bold bright_black]━━━ HALL OF FAME — najczęściej w TOP ━━━[/]")
    hof = Table(box=box.SIMPLE_HEAD, header_style="bold bright_black", expand=False)
    hof.add_column("#",         style="bright_black", width=3)
    hof.add_column("symbol",    style="white", min_width=14)
    hof.add_column("w TOP",     justify="right")
    hof.add_column("% runów",   justify="right")
    hof.add_column("avg score", justify="right", style="yellow")
    hof.add_column("avg bull%", justify="right")
    hof.add_column("avg bear%", justify="right")
    hof.add_column("avg rank",  justify="right", style="bright_black")
    hof.add_column("trend",     justify="center")

    for idx, (sym, cnt) in enumerate(appearances[:top_n], 1):
        data  = sym_data[sym]
        slope = linear_slope([d["score"] for d in data])
        avg_s = sum(d["score"]     for d in data) / len(data)
        avg_b = sum(d["bull_prob"] for d in data) // len(data)
        avg_e = sum(d["bear_prob"] for d in data) // len(data)
        avg_r = sum(d["rank"]      for d in data) / len(data)
        pct   = cnt / n_runs * 100

        if slope > 2:    tr = "[bold bright_green]▲▲[/]"
        elif slope > 0:  tr = "[bright_green]▲[/]"
        elif slope < -2: tr = "[bold bright_red]▼▼[/]"
        elif slope < 0:  tr = "[bright_red]▼[/]"
        else:            tr = "[bright_black]→[/]"

        hof.add_row(str(idx), sym, str(cnt), f"{pct:.0f}%",
                    f"{avg_s:.0f}",
                    f"[bright_green]{avg_b}%[/]",
                    f"[bright_red]{avg_e}%[/]",
                    f"{avg_r:.1f}", tr)
    console.print(hof)

    # ── 2. Rozgrzewające się ──────────────────────────────────
    if n_runs >= 2:
        console.print()
        console.print("[bold bright_black]━━━ ROZGRZEWAJĄCE SIĘ — rosnący slope score ━━━[/]")
        heating = []
        for sym, data in sym_data.items():
            if len(data) < 2: continue
            sl = linear_slope([d["score"] for d in data])
            if sl > 0:
                heating.append((sym, sl, [d["score"] for d in data], data[-1]))
        heating.sort(key=lambda x: x[1], reverse=True)

        if heating:
            ht = Table(box=box.SIMPLE_HEAD, header_style="bold bright_black", expand=False)
            ht.add_column("#",      style="bright_black", width=3)
            ht.add_column("symbol", style="white", min_width=14)
            ht.add_column("slope",  justify="right", style="yellow")
            ht.add_column("historia score", justify="left", min_width=18)
            ht.add_column("bull%",  justify="right")
            ht.add_column("bear%",  justify="right")
            ht.add_column("setup",  justify="center")
            for idx, (sym, sl, sc, last) in enumerate(heating[:top_n], 1):
                su = ("[bold bright_green]LONG[/]"  if last["setup"] == "LONG"
                      else "[bold bright_red]SHORT[/]" if last["setup"] == "SHORT"
                      else "[bright_black]—[/]")
                ht.add_row(str(idx), sym, f"+{sl:.1f}",
                           f"[bright_black]{sparkline(sc)}[/] [bright_black]{sc[-1]:.0f}[/]",
                           f"[bright_green]{last['bull_prob']}%[/]",
                           f"[bright_red]{last['bear_prob']}%[/]", su)
            console.print(ht)
        else:
            console.print("[bright_black]Potrzeba ≥2 runów.[/]")

    # ── 3. Ranking sygnałów LONG / SHORT ─────────────────────
    all_ranked = [
        score_token_for_ranking(sym, data, n_runs)
        for sym, data in sym_data.items()
        if len(data) >= 1
    ]
    ranked_long  = sorted(all_ranked, key=lambda r: r["long_reliability"],  reverse=True)
    ranked_short = sorted(all_ranked, key=lambda r: r["short_reliability"], reverse=True)

    console.print()
    console.print("[bold bright_black]━━━ RANKING SYGNAŁÓW — LONG (reliability) ━━━[/]")
    console.print("[bright_black]reliability = consistency × aktywność × niezależność od BTC × divergencje[/]")
    console.print(render_signal_ranking(ranked_long[:top_n], "Najlepsze historyczne sygnały LONG", "long"))

    console.print()
    console.print("[bold bright_black]━━━ RANKING SYGNAŁÓW — SHORT (reliability) ━━━[/]")
    console.print(render_signal_ranking(ranked_short[:top_n], "Najlepsze historyczne sygnały SHORT", "short"))

    # ── 4. TOP 5 PICKS z uzasadnieniem ───────────────────────
    pick_top5(ranked_long, ranked_short, n_runs)

    # ── 5. Podsumowanie ───────────────────────────────────────
    from collections import Counter
    all_scores = [d["score"] for data in sym_data.values() for d in data]
    console.print("[bold bright_black]━━━ PODSUMOWANIE ━━━[/]")
    console.print(f"  Symboli w historii: [white]{len(sym_data)}[/]  "
                  f"Avg score: [yellow]{sum(all_scores)/len(all_scores):.1f}[/]")
    top1 = Counter(r["symbol"] for r in rows if r.get("rank","") == "1").most_common(5)
    if top1:
        console.print("  Najczęściej #1: " +
                      "  ".join(f"[yellow]{s}[/][bright_black]×{c}[/]" for s,c in top1))
    console.print()


# ============================================================
# CSV
# ============================================================
HISTORY_HEADERS = [
    "run_timestamp","rank","symbol","setup",
    "last_price","price_change_pct_24h","price_4h_change_pct",
    "high_24h","low_24h","volatility_range_pct",
    "volume_24h_usd","volume_spike_ratio",
    "oi_now","oi_change_pct","rsi_14","funding_rate_pct",
    "long_short_ratio","liq_ratio_pct",
    "ema_bull_4h","btc_corr","score","score_prev","score_trend",
    "bull_prob","bear_prob",
    "div_bull","div_bear","div_rsi_bull","div_rsi_bear",
    "div_squeeze_bull","div_squeeze_bear",
]

def _write_rows(w, results, ts):
    for i, s in enumerate(results, 1):
        div = s.div or Divergence()
        # Konwertuj Decimal → float żeby CSV nie zapisywał "Decimal('50.82')"
        w.writerow([
            ts, i, s.symbol, s.setup,
            float(s.last_price), float(s.price_change_pct), float(s.price_4h_change_pct),
            float(s.high_24h), float(s.low_24h), float(s.volatility_range_pct),
            float(s.volume_24h_usd), float(s.volume_spike_ratio),
            float(s.oi_now), float(s.oi_change_pct), float(s.rsi_14), float(s.funding_rate_pct),
            float(s.long_short_ratio), float(s.liq_ratio_pct),
            int(s.ema_bull_4h), float(s.btc_corr),
            float(s.score), float(s.score_prev), float(s.score_trend),
            int(s.bull_prob), int(s.bear_prob),
            int(div.bull_div), int(div.bear_div),
            int(div.rsi_price_div_bull), int(div.rsi_price_div_bear),
            int(div.funding_squeeze_bull), int(div.funding_squeeze_bear),
        ])

def save_csv_snapshot(results, filename):
    with open(filename, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(HISTORY_HEADERS)
        _write_rows(w, results, datetime.now().isoformat())

def append_history(results):
    ts = datetime.now().isoformat()
    exists = os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists: w.writerow(HISTORY_HEADERS)
        _write_rows(w, results, ts)


# ============================================================
# CLI + MAIN
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(description="AsterDex Volatility Scanner v3.4")
    p.add_argument("--mode",       choices=["long","short","all"], default="all")
    p.add_argument("--min-score",  type=float, default=float(DEFAULT_MIN_SCORE))
    p.add_argument("--min-spike",  type=float, default=float(DEFAULT_MIN_SPIKE))
    p.add_argument("--top",        type=int,   default=TOP_N)
    p.add_argument("--min-volume", type=float, default=float(MIN_VOLUME_24H_USD))
    p.add_argument("--diag",       action="store_true")
    p.add_argument("--vol-stats",  action="store_true")
    p.add_argument("--history",    action="store_true",
                   help="Pokaż analizę historii CSV zamiast skanować")
    p.add_argument("--no-lighter",  action="store_true",
                   help="Pomiń cross-check z Lighter.xyz")
    p.add_argument("--picks",      action="store_true",
                   help="Pokaż tylko TOP 5 long/short picks z historii (szybko)")
    return p.parse_args()


def main():
    args       = parse_args()
    min_score  = Decimal(str(args.min_score))
    min_spike  = Decimal(str(args.min_spike))
    min_volume = Decimal(str(args.min_volume))
    mode       = args.mode
    top_n      = args.top

    console.print()
    console.print("[bold cyan]══════════════════════════════════════════════════════[/]")
    console.print("[bold cyan]          ASTERDEX VOLATILITY SCANNER  v3.4[/]")
    console.print("[bold cyan]══════════════════════════════════════════════════════[/]")

    # Tryb historii — bez API
    if args.history:
        analyze_history(top_n)
        return

    # Tryb szybkich picks z historii — bez skanowania
    if args.picks:
        rows, timestamps = _load_history_rows()
        if not rows:
            console.print(f"[red]Brak {HISTORY_CSV}. Uruchom najpierw normalny skan.[/]")
            return
        n_runs = len(timestamps)
        sym_data = _group_by_symbol(rows)
        all_ranked = [score_token_for_ranking(s, d, n_runs)
                      for s, d in sym_data.items()]
        ranked_long  = sorted(all_ranked, key=lambda r: r["long_reliability"],  reverse=True)
        ranked_short = sorted(all_ranked, key=lambda r: r["short_reliability"], reverse=True)
        console.print(f"  [bright_black]Historia: {n_runs} runów, {len(sym_data)} symboli[/]")
        pick_top5(ranked_long, ranked_short, n_runs)
        return

    console.print(f"  tryb: [bold]{mode.upper()}[/]  score>=[yellow]{min_score}[/]  "
                  f"spike>=[cyan]{min_spike}x[/]  vol>=[white]${min_volume:,.0f}[/]  "
                  f"top=[white]{top_n}[/]")
    console.print("  [bright_black]bull/bear%=heurystyczny scoring | div=divergencje tech.[/]")
    console.print()

    # Diagnostyka
    console.print("[bright_black]0/5[/] Diagnostyka endpointow...")
    diag = run_diagnostics()
    for name, ok in diag.items():
        s = "[bold bright_green]OK  [/]" if ok else "[bold bright_red]BRAK[/]"
        console.print(f"  {s}  {name}")
    console.print()

    ENDPOINTS_AVAILABLE["funding"]  = diag.get("premiumIndex",False) or diag.get("fundingRate",False)
    ENDPOINTS_AVAILABLE["ls_ratio"] = diag.get("globalLS",False)
    ENDPOINTS_AVAILABLE["liq"]      = diag.get("allForceOrders",False)
    ENDPOINTS_AVAILABLE["oi_now"]   = diag.get("openInterest",False)

    missing = [k for k,v in ENDPOINTS_AVAILABLE.items() if not v]
    if missing:
        console.print(f"  [yellow]Niedostepne: {', '.join(missing)}[/]")

    if args.diag: return

    # 1–2. Symbole + tickery
    console.print("[bright_black]1/5[/] Symbole...")
    symbols = fetch_exchange_info()
    if not symbols: console.print("[red]Brak symboli.[/]"); return
    console.print(f"     [green]v[/] {len(symbols)} par USDT")

    console.print("[bright_black]2/5[/] 24h tickery...")
    tickers = fetch_24hr_tickers()
    if not tickers: console.print("[red]Brak tickerow.[/]"); return
    passes = sum(1 for sym in symbols
                 if float(tickers.get(sym,{}).get("quoteVolume",0)) >= float(min_volume))
    console.print(f"     [green]v[/] {len(tickers)} tickerow  |  "
                  f"vol>=${min_volume:,.0f}: [white]{passes}[/] symboli")

    if args.vol_stats:
        dist = volume_distribution(tickers, symbols)
        console.print("[bright_black]Rozkład volume:[/]")
        for thr, cnt in dist.items():
            console.print(f"  ${thr:>7,.0f}: {'█'*min(cnt,40)} {cnt}")
        return

    # 3. Cache + historia
    console.print("[bright_black]3/5[/] BTC + cache OI + historia...")
    btc_closes  = fetch_btc_closes(28)
    oi_cache    = load_oi_cache()
    last_scores = load_last_scores()
    cache_age   = (int(time.time()) - oi_cache.get("timestamp",0)) / 3600
    console.print(f"     [green]v[/] BTC {len(btc_closes)} swiec  "
                  f"| OI cache: {len(oi_cache.get('oi',{}))} ({cache_age:.1f}h)  "
                  f"| historia: {len(last_scores)}")

    # 4. Analiza
    console.print(f"[bright_black]4/5[/] Analiza {passes} symboli...")
    results = []; errors = 0; skipped = 0; oi_snap = {}

    with Progress(TextColumn("     {task.description}"), BarColumn(bar_width=35),
                  TextColumn("{task.percentage:>3.0f}%"),
                  TextColumn("[bright_black]{task.completed}/{task.total}[/]"),
                  TimeElapsedColumn(), console=console) as progress:
        task = progress.add_task("[cyan]analiza...", total=len(symbols))
        for sym in symbols:
            ticker = tickers.get(sym)
            if not ticker: progress.advance(task); continue
            try:
                vol = Decimal(str(ticker.get("quoteVolume","0")))
                if vol < min_volume:
                    skipped += 1; progress.advance(task); continue
            except Exception: pass
            stats = analyze_symbol(sym, ticker, oi_cache, btc_closes,
                                   last_scores, min_volume)
            if stats:
                results.append(stats)
                if stats.oi_now > 0: oi_snap[sym] = stats.oi_now
            else:
                errors += 1
            progress.advance(task)

    console.print(f"     [green]v[/] {len(results)} z danymi  "
                  f"[bright_black]| {skipped} vol<${min_volume:,.0f}  "
                  f"| {errors} błędy[/]")
    if oi_snap:
        save_oi_cache(oi_snap)
        console.print(f"     [green]v[/] cache OI -> {OI_CACHE_FILE} ({len(oi_snap)})")

    # 5. Sort + filtry + tabela
    console.print(f"[bright_black]5/5[/] Sort + filtry...")
    results.sort(key=lambda s: s.score, reverse=True)
    filtered = apply_filters(results[:top_n*3], mode, min_score, min_spike)

    # Auto-fallback gdy wyników za mało (niska aktywność rynku, noc itp.)
    used_spike = min_spike
    used_score = min_score
    if len(filtered) < 5 and min_spike > Decimal("1.0"):
        used_spike = Decimal("1.0")
        filtered2 = apply_filters(results[:top_n*3], mode, min_score, used_spike)
        if len(filtered2) > len(filtered):
            console.print(f"  [yellow]Mało wyników przy spike>={min_spike}x ({len(filtered)})."
                          f" Obniżam do 1.0x → {len(filtered2)} wyników.[/]")
            filtered = filtered2
    if len(filtered) < 5 and min_score > Decimal("10"):
        used_score = Decimal("10.0")
        filtered3 = apply_filters(results[:top_n*3], mode, used_score, Decimal("1.0"))
        if len(filtered3) > len(filtered):
            console.print(f"  [yellow]Nadal mało — obniżam score do {used_score}"
                          f" → {len(filtered3)} wyników.[/]")
            filtered = filtered3
    top = filtered[:top_n]

    if not top:
        by_s = [s for s in results if s.score >= min_score]
        by_sp= [s for s in results if s.volume_spike_ratio >= min_spike]
        console.print(f"[yellow]Brak wyników. score>={min_score}: {len(by_s)}  "
                      f"spike>={min_spike}x: {len(by_sp)}[/]")
        if results:
            b = results[0]
            console.print(f"  Najwyższy: [yellow]{b.symbol}[/] "
                          f"score={b.score:.0f} spike={b.volume_spike_ratio:.1f}x")
        return

    console.print()
    subtitle = (f"TOP {len(top)} | {mode.upper()} | "
                f"score>={used_score} | spike>={used_spike}x")
    console.print(render_scan_table(top, f"AsterDex v3.4 — {subtitle}"))

    # ── Cross-check z Lighter.xyz ─────────────────────────────
    console.print()
    if not args.no_lighter:
        lighter_markets = fetch_lighter_markets()
        render_lighter_crosscheck(top, lighter_markets)
    else:
        console.print("[bright_black]  Cross-check Lighter.xyz pominięty (--no-lighter)[/]")
        console.print()

    # Legenda
    console.print()
    console.print("[bright_black]Divergencje:[/]  "
                  "[bright_green]BULL-DIV[/] cena↓+OI↑+RSI↑=akumulacja  "
                  "[bright_green]RSI-BULL[/] price nowe low, RSI wyższe  "
                  "[bright_green]SQ-BULL[/] funding<-0.05%=short squeeze")
    console.print("              "
                  "[bright_red]BEAR-DIV[/] cena↑+OI↓+RSI↓=dystrybucja  "
                  "[bright_red]RSI-BEAR[/] price nowe high, RSI niższe  "
                  "[bright_red]SQ-BEAR[/] funding>0.15%=long squeeze")
    console.print("[bright_black]Uruchom z --history żeby zobaczyć analizę poprzednich runów.[/]")
    console.print()

    # Zapis
    ts   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    snap = f"aster_scan_{ts}.csv"
    save_csv_snapshot(top, snap)
    append_history(top)
    console.print(f"[green]v[/] {snap}  |  {HISTORY_CSV}")

    # Statystyki
    t10 = top[:min(10,len(top))]; n = len(t10)
    divs = sum(1 for s in t10 if s.div and
               (s.div.bull_score > 0 or s.div.bear_score > 0))
    console.print()
    console.print(f"[bright_black]TOP 10:[/]  "
                  f"score avg [yellow]{sum(s.score for s in t10)/n:.0f}[/]  "
                  f"bull% [bright_green]{sum(s.bull_prob for s in t10)//n}%[/]  "
                  f"bear% [bright_red]{sum(s.bear_prob for s in t10)//n}%[/]  "
                  f"div wykryte [yellow]{divs}/{n}[/]  "
                  f"LONG [bright_green]{sum(1 for s in t10 if s.setup=='LONG')}[/]  "
                  f"SHORT [bright_red]{sum(1 for s in t10 if s.setup=='SHORT')}[/]")

    # ── AUTO PICKS z historii po każdym skanie ────────────────
    rows_h, ts_h = _load_history_rows()
    if rows_h and len(ts_h) >= 1:
        sym_data_h = _group_by_symbol(rows_h)
        all_ranked_h = [score_token_for_ranking(s, d, len(ts_h))
                        for s, d in sym_data_h.items()]
        ranked_long_h  = sorted(all_ranked_h, key=lambda r: r["long_reliability"],  reverse=True)
        ranked_short_h = sorted(all_ranked_h, key=lambda r: r["short_reliability"], reverse=True)
        pick_top5(ranked_long_h, ranked_short_h, len(ts_h))
    console.print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Przerwano.[/]"); sys.exit(0)
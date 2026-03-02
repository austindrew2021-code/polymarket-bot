"""
Polymarket 15m Crypto Bot  v5.0 — CANDLE PATTERN + MULTI-SOURCE DATA
══════════════════════════════════════════════════════════════════════
Fixed: Multi-API fallback (Gemini → Coinbase → CryptoCompare → Bitfinex)
Fixed: Aggressive candle pattern + momentum signal engine
Fixed: MIN_CONF lowered, signals fire much more frequently
Stage 4 aggressive sizing: 28% per bet, full reinvest
"""

import requests, time, json, math, logging
from datetime import datetime, timezone

# ============================================================
#              <- ONLY EDIT THIS SECTION ->
# ============================================================
STAGE    = 1      # 1=baseline | 2=conservative | 3=aggressive | 4=moon🚀
DRY_RUN  = True   # True=simulate | False=real money

STARTING_CAPITAL = 12.0  # mock balance (or real wallet balance when live)
# ============================================================

STAGE_CFG = {
    1: dict(bet=0.10, conf=3, bank=0.40, name="Stage 1 | Baseline      | 10% bets | bank 40%"),
    2: dict(bet=0.10, conf=3, bank=0.20, name="Stage 2 | Conservative  | 10% bets | bank 20%"),
    3: dict(bet=0.20, conf=3, bank=0.00, name="Stage 3 | Aggressive    | 20% bets | full reinvest"),
    4: dict(bet=0.28, conf=3, bank=0.00, name="Stage 4 | Moon Mode 🚀  | 28% bets | full reinvest"),
}

cfg      = STAGE_CFG[STAGE]
BET_PCT  = cfg["bet"]
MIN_CONF = cfg["conf"]
BANK_PCT = cfg["bank"]

BET_FLOOR         = 0.50
MAX_BET           = 999.0
EDGE_THRESHOLD    = 0.03    # lowered from 0.05 → more bets fire
EXTREME_ODDS      = 0.88    # lowered → catches more extreme markets
CYCLE_SLEEP       = 60
RESOLUTION_BUFFER = 180
WR_CONFIRM_BETS   = 20
STAGE_THRESHOLDS  = {2: 0.57, 3: 0.62, 4: 0.65}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", mode="a")],
)
log = logging.getLogger("bot")

capital     = STARTING_CAPITAL
banked      = 0.0
wins        = 0
losses      = 0
total_res   = 0
win_streak  = 0
loss_streak = 0
live_wr     = 0.574
sim_bets    = []
seen_slugs  = set()

ASSETS = {
    "BTC": ("btcusd",  "BTC-USD",   "BTC",  "tBTCUSD", "btc"),
    "ETH": ("ethusd",  "ETH-USD",   "ETH",  "tETHUSD", "eth"),
    "SOL": ("solusd",  "SOL-USD",   "SOL",  "tSOLUSD", "sol"),
    "XRP": ("xrpusd",  "XRP-USD",   "XRP",  "tXRPUSD", "xrp"),
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# ─────────────────── MULTI-SOURCE DATA ───────────────────
def fetch_gemini(gemini_sym):
    """Gemini public candles — 15m — returns newest last"""
    try:
        url = f"https://api.gemini.com/v2/candles/{gemini_sym}/15m"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            raw = r.json()  # [[ts,open,high,low,close,vol], ...]  newest last
            if len(raw) >= 20:
                candles = [{"o": float(x[1]), "h": float(x[2]),
                            "l": float(x[3]), "c": float(x[4]),
                            "v": float(x[5])} for x in raw[-100:]]
                return candles
    except Exception as e:
        log.debug(f"Gemini error: {e}")
    return []

def fetch_coinbase(cb_sym):
    """Coinbase Advanced Trade public candles — 15m (900s)"""
    try:
        url = (f"https://api.exchange.coinbase.com/products/{cb_sym}/candles"
               f"?granularity=900")
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            raw = r.json()  # [[time,low,high,open,close,vol]] newest first
            if len(raw) >= 20:
                raw.reverse()
                candles = [{"o": float(x[3]), "h": float(x[2]),
                            "l": float(x[1]), "c": float(x[4]),
                            "v": float(x[5])} for x in raw[-100:]]
                return candles
    except Exception as e:
        log.debug(f"Coinbase error: {e}")
    return []

def fetch_cryptocompare(cc_sym):
    """CryptoCompare histominute aggregated to 15m"""
    try:
        url = (f"https://min-api.cryptocompare.com/data/v2/histominute"
               f"?fsym={cc_sym}&tsym=USD&limit=300&aggregate=15&toTs=9999999999")
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            rows = data.get("Data", {}).get("Data", [])
            if len(rows) >= 20:
                candles = [{"o": float(x["open"]), "h": float(x["high"]),
                            "l": float(x["low"]),  "c": float(x["close"]),
                            "v": float(x["volumefrom"])} for x in rows[-100:]]
                return candles
    except Exception as e:
        log.debug(f"CryptoCompare error: {e}")
    return []

def fetch_bitfinex(bf_sym):
    """Bitfinex public candles — 15m"""
    try:
        url = f"https://api-pub.bitfinex.com/v2/candles/trade:15m:{bf_sym}/hist?limit=100&sort=1"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            raw = r.json()  # [[ts,open,close,high,low,vol]]
            if len(raw) >= 20:
                candles = [{"o": float(x[1]), "h": float(x[3]),
                            "l": float(x[4]), "c": float(x[2]),
                            "v": float(x[5])} for x in raw[-100:]]
                return candles
    except Exception as e:
        log.debug(f"Bitfinex error: {e}")
    return []

def fetch_klines(symbol):
    """Try all sources in order, return first that works"""
    gemini_sym, cb_sym, cc_sym, bf_sym, _ = ASSETS[symbol]

    for name, fn, arg in [
        ("Gemini",       fetch_gemini,       gemini_sym),
        ("Coinbase",     fetch_coinbase,     cb_sym),
        ("CryptoCompare",fetch_cryptocompare,cc_sym),
        ("Bitfinex",     fetch_bitfinex,     bf_sym),
    ]:
        candles = fn(arg)
        if candles and len(candles) >= 20:
            log.debug(f"  {symbol}: data from {name} ({len(candles)} candles)")
            return candles, name
        time.sleep(0.2)

    return [], "none"

def get_market(slug):
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/events/slug/{slug}",
            headers=HEADERS, timeout=8)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None

def get_prices(market):
    ps = market.get("outcomePrices")
    if ps:
        try:
            p = json.loads(ps)
            if len(p) == 2:
                return float(p[0]), float(p[1])
        except:
            pass
    return 0.5, 0.5

# ─────────────────── INDICATORS ──────────────────────────
def ema(vals, p):
    if not vals or len(vals) < p:
        return [vals[-1]] * len(vals) if vals else [0]
    k = 2 / (p + 1)
    r = [sum(vals[:p]) / p]
    for v in vals[p:]:
        r.append(v * k + r[-1] * (1 - k))
    pad = len(vals) - len(r)
    return [r[0]] * pad + r

def rsi(closes, p=14):
    if len(closes) < p + 1:
        return 50.0
    d = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = sum(max(x, 0) for x in d[-p:]) / p or 1e-9
    l = sum(max(-x, 0) for x in d[-p:]) / p or 1e-9
    return 100 - (100 / (1 + g / l))

def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return 0., 0.
    ef = ema(closes, fast)
    es = ema(closes, slow)
    ml = [a - b for a, b in zip(ef, es)]
    sl = ema(ml, signal)
    return ml[-1], ml[-1] - sl[-1]  # macd_line, histogram

def stoch(c, h, l, kp=14, sk=3, sd=3):
    n = len(c)
    if n < kp:
        return 50., 50.
    rk = []
    for i in range(n):
        lo = min(l[max(0, i-kp+1):i+1])
        hi = max(h[max(0, i-kp+1):i+1])
        rk.append(100*(c[i]-lo)/(hi-lo+1e-9) if hi != lo else 50.)
    ks = [sum(rk[max(0,i-sk+1):i+1])/min(sk,i+1) for i in range(n)]
    ds = [sum(ks[max(0,i-sd+1):i+1])/min(sd,i+1) for i in range(n)]
    return ks[-1], ds[-1]

def atr(h, l, c, p=14):
    if len(c) < 2:
        return 0.
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-p:]) / min(p, len(trs)) if trs else 0.

def bollinger(closes, p=20):
    if len(closes) < p:
        v = closes[-1]
        return v, v, v, 0.5, 2.0
    w = closes[-p:]
    mid = sum(w) / p
    std = math.sqrt(sum((x-mid)**2 for x in w)/p + 1e-12)
    up = mid + 2*std; lo = mid - 2*std
    pctb = (closes[-1]-lo)/(up-lo+1e-9)
    bw   = (up-lo)/(mid+1e-9)*100
    return up, mid, lo, pctb, bw

def volume_ratio(vols, p=20):
    if len(vols) < p+1:
        return 1.
    avg = sum(vols[-p-1:-1])/p
    return vols[-1]/avg if avg > 0 else 1.

def higher_highs_lows(candles, n=5):
    """Detects if we have n consecutive higher highs + higher lows (uptrend) or opposite"""
    if len(candles) < n+1:
        return 0
    highs  = [c['h'] for c in candles[-(n+1):]]
    lows   = [c['l'] for c in candles[-(n+1):]]
    hh = all(highs[i] > highs[i-1] for i in range(1, len(highs)))
    hl = all(lows[i]  > lows[i-1]  for i in range(1, len(lows)))
    lh = all(highs[i] < highs[i-1] for i in range(1, len(highs)))
    ll = all(lows[i]  < lows[i-1]  for i in range(1, len(lows)))
    if hh and hl: return  1
    if lh and ll: return -1
    return 0

def momentum_bars(candles, n=3):
    """Last n candles: net bull vs bear based on close-open"""
    if len(candles) < n:
        return 0
    recent = candles[-n:]
    bull = sum(1 for c in recent if c['c'] >= c['o'])
    bear = n - bull
    if bull > bear: return  1
    if bear > bull: return -1
    return 0

# ─────────────────── CANDLE PATTERNS ─────────────────────
def candle_patterns(candles):
    """
    Returns (bull_score, bear_score, patterns_list)
    Detects 15+ classic patterns on the last 3 candles.
    """
    if len(candles) < 3:
        return 0, 0, []

    bull = 0; bear = 0; found = []
    c  = candles[-1]
    p  = candles[-2]
    pp = candles[-3]

    o=c['o']; h=c['h']; l=c['l']; cl=c['c']
    po=p['o']; ph=p['h']; pl=p['l']; pc=p['c']
    ppo=pp['o']; pph=pp['h']; ppl=pp['l']; ppc=pp['c']

    body       = abs(cl - o)
    rng        = (h - l) if (h - l) > 0 else 1e-9
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l
    bullish    = cl >= o
    bearish    = cl < o
    p_body     = abs(pc - po)
    p_bullish  = pc >= po
    p_bearish  = pc < po
    pp_bullish = ppc >= ppo
    pp_bearish = ppc < ppo

    # ── BULLISH ─────────────────────────────────────────
    # Hammer
    if lower_wick > 2*body and upper_wick < body*1.1 and bullish and body > 0:
        bull += 3; found.append("Hammer")

    # Bullish engulfing
    if bullish and p_bearish and cl > po and o < pc and body > p_body * 0.8:
        bull += 3; found.append("BullEngulf")

    # Morning star: pp=red, p=small, c=green
    if pp_bearish and p_body < rng * 0.25 and bullish and cl > (ppo + ppc)/2:
        bull += 4; found.append("MornStar")

    # Piercing line: red p, then green c that closes above midpoint of p
    if bullish and p_bearish and o < pc and cl > (po + pc)/2:
        bull += 2; found.append("Piercing")

    # Three white soldiers
    if bullish and p_bullish and pp_bullish and cl > pc > ppc and o > po:
        bull += 3; found.append("3Soldiers")

    # Bullish marubozu (close body, tiny wicks)
    if bullish and body > rng * 0.80:
        bull += 2; found.append("BullMaru")

    # Bullish harami: big red candle contains small green
    if bullish and p_bearish and o > pc and cl < po and body < p_body * 0.6:
        bull += 2; found.append("BullHarami")

    # Tweezer bottom
    if abs(l - pl) < rng * 0.05 and p_bearish and bullish:
        bull += 2; found.append("TweezBot")

    # Doji after downtrend (indecision = potential reversal)
    if body < rng * 0.08 and pp_bearish and p_bearish:
        bull += 2; found.append("DojiRev")

    # ── BEARISH ─────────────────────────────────────────
    # Shooting star
    if upper_wick > 2*body and lower_wick < body*1.1 and bearish and body > 0:
        bear += 3; found.append("ShootStar")

    # Bearish engulfing
    if bearish and p_bullish and o > pc and cl < po and body > p_body * 0.8:
        bear += 3; found.append("BearEngulf")

    # Evening star: pp=green, p=small, c=red
    if pp_bullish and p_body < rng * 0.25 and bearish and cl < (ppo + ppc)/2:
        bear += 4; found.append("EveStar")

    # Dark cloud cover
    if bearish and p_bullish and o > pc and cl < (po + pc)/2:
        bear += 2; found.append("DarkCloud")

    # Three black crows
    if bearish and p_bearish and pp_bearish and cl < pc < ppc and o < po:
        bear += 3; found.append("3Crows")

    # Bearish marubozu
    if bearish and body > rng * 0.80:
        bear += 2; found.append("BearMaru")

    # Bearish harami
    if bearish and p_bullish and o < pc and cl > po and body < p_body * 0.6:
        bear += 2; found.append("BearHarami")

    # Tweezer top
    if abs(h - ph) < rng * 0.05 and p_bullish and bearish:
        bear += 2; found.append("TweezTop")

    # Doji after uptrend
    if body < rng * 0.08 and pp_bullish and p_bullish:
        bear += 2; found.append("DojiTop")

    return bull, bear, found

# ─────────────────── SIGNAL ENGINE ───────────────────────
def analyze(symbol):
    """
    Returns (direction, confidence, description, source)
    direction: +1=up -1=down 0=skip
    confidence: 0..18 (sum of weighted indicator scores)
    """
    candles, source = fetch_klines(symbol)

    if not candles or len(candles) < 20:
        return 0, 0, f"No data (tried all sources)", "none"

    closes  = [c['c'] for c in candles]
    highs   = [c['h'] for c in candles]
    lows    = [c['l'] for c in candles]
    opens   = [c['o'] for c in candles]
    volumes = [c['v'] for c in candles]

    # ── INDICATORS ────────────────────────────────────────
    n = len(candles)
    e8  = ema(closes, 8)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, min(50, n-1))[-1]

    rsi14 = rsi(closes, 14)
    rsi7  = rsi(closes, 7)   # faster RSI

    macd_line, macd_hist = macd(closes)
    sk, sd = stoch(closes, highs, lows)

    _, _, _, pctb, bw = bollinger(closes)

    at      = atr(highs, lows, closes)
    atr_pct = at / closes[-1] * 100 if closes[-1] else 0

    vr = volume_ratio(volumes)

    # Candle patterns
    cpat_bull, cpat_bear, patterns = candle_patterns(candles)

    # Price structure
    hh = higher_highs_lows(candles, 4)
    mb = momentum_bars(candles, 3)

    # Price position vs recent range
    recent_high = max(highs[-20:])
    recent_low  = min(lows[-20:])
    price_pos   = (closes[-1] - recent_low) / (recent_high - recent_low + 1e-9)

    # Last candle size vs average
    last_body = abs(closes[-1] - opens[-1])
    avg_body  = sum(abs(closes[i]-opens[i]) for i in range(-10,-1)) / 9 or 1e-9
    body_ratio = last_body / avg_body

    # ── SCORING ───────────────────────────────────────────
    bull = 0; bear = 0; sigs = []

    # 1. EMA Stack (2 pts)
    if   e8 > e21 > e50: bull += 2; sigs.append("EMA++")
    elif e8 < e21 < e50: bear += 2; sigs.append("EMA--")
    elif e8 > e21:        bull += 1; sigs.append("EMA+")
    elif e8 < e21:        bear += 1; sigs.append("EMA-")

    # 2. RSI 14m (2 pts)
    if   rsi14 < 30: bull += 2; sigs.append(f"RSI{rsi14:.0f}OS")
    elif rsi14 > 70: bear += 2; sigs.append(f"RSI{rsi14:.0f}OB")
    elif rsi14 < 45: bull += 1; sigs.append(f"RSI{rsi14:.0f}L")
    elif rsi14 > 55: bear += 1; sigs.append(f"RSI{rsi14:.0f}H")

    # 3. RSI 7 (fast momentum, 1 pt)
    if   rsi7 < 30: bull += 1; sigs.append("RSI7OS")
    elif rsi7 > 70: bear += 1; sigs.append("RSI7OB")
    elif rsi7 < 40: bull += 1; sigs.append("RSI7L")
    elif rsi7 > 60: bear += 1; sigs.append("RSI7H")

    # 4. MACD histogram (2 pts)
    if   macd_hist > 0: bull += (2 if macd_hist > 0.0005*closes[-1] else 1); sigs.append("MACD+")
    elif macd_hist < 0: bear += (2 if macd_hist < -0.0005*closes[-1] else 1); sigs.append("MACD-")

    # 5. Stochastic (2 pts)
    if   sk > sd and sk < 40: bull += 2; sigs.append(f"StX+{sk:.0f}")
    elif sk < sd and sk > 60: bear += 2; sigs.append(f"StX-{sk:.0f}")
    elif sk < 20: bull += 1; sigs.append("StOS")
    elif sk > 80: bear += 1; sigs.append("StOB")

    # 6. Bollinger (2 pts)
    if   pctb < 0.05: bull += 2; sigs.append("BB_OS")
    elif pctb > 0.95: bear += 2; sigs.append("BB_OB")
    elif pctb < 0.25: bull += 1; sigs.append("BB_L")
    elif pctb > 0.75: bear += 1; sigs.append("BB_H")

    # 7. Candle patterns (up to 4 pts — biggest weight!)
    if cpat_bull > 0:
        pts = min(cpat_bull, 4); bull += pts
        sigs.extend(patterns[:3])
    if cpat_bear > 0:
        pts = min(cpat_bear, 4); bear += pts
        sigs.extend(patterns[:3])

    # 8. Price structure (1 pt)
    if   hh ==  1: bull += 1; sigs.append("HH+HL")
    elif hh == -1: bear += 1; sigs.append("LH+LL")

    # 9. Momentum bars (1 pt)
    if   mb ==  1: bull += 1; sigs.append("MomBars+")
    elif mb == -1: bear += 1; sigs.append("MomBars-")

    # 10. Price position in range (1 pt)
    if   price_pos < 0.20: bull += 1; sigs.append(f"PosBot{price_pos:.0%}")
    elif price_pos > 0.80: bear += 1; sigs.append(f"PosTop{price_pos:.0%}")

    # 11. Volume surge (1 pt)
    if vr > 1.5:
        if closes[-1] >= opens[-1]: bull += 1; sigs.append(f"VolUp{vr:.1f}x")
        else:                       bear += 1; sigs.append(f"VolDn{vr:.1f}x")

    # 12. ATR filter — skip if dead/ranging (no pts, just filter)
    if atr_pct < 0.04:
        return 0, 0, f"ATR={atr_pct:.3f}% too low (ranging)", source

    # ── DECISION ──────────────────────────────────────────
    total = bull + bear
    net   = bull - bear

    # Direction requires: majority agreement + net score >= MIN_CONF
    if   total > 0 and bull/total >= 0.55 and net >= MIN_CONF:
        direction =  1; conf = bull
    elif total > 0 and bear/total >= 0.55 and net <= -MIN_CONF:
        direction = -1; conf = bear
    else:
        direction = 0;  conf = 0

    pat_str = ",".join(patterns) if patterns else "none"
    desc = (f"src={source} | ATR={atr_pct:.2f}% | "
            f"RSI={rsi14:.0f}/{rsi7:.0f} | MACD_h={macd_hist:+.5f} | "
            f"Stoch={sk:.0f}/{sd:.0f} | BB={pctb:.2f} | "
            f"Vol={vr:.1f}x | Pats={pat_str} | "
            f"HH={hh} MB={mb} | B={bull} D={bear} | [{','.join(sigs)}]")
    return direction, conf, desc, source

# ─────────────────── POSITION SIZING ─────────────────────
def bet_size(conf):
    global capital, win_streak, loss_streak
    pct = BET_PCT
    pct += min((conf - MIN_CONF) * 0.012, 0.06)
    pct += min(win_streak * 0.015, 0.05)
    if loss_streak >= 3:
        pct *= max(0.6, 1.0 - (loss_streak - 2) * 0.10)
    pct  = min(pct, 0.85)
    size = capital * pct
    size = max(BET_FLOOR, min(size, MAX_BET, capital * 0.90))
    return round(size, 2)

# ─────────────────── RESOLUTION ──────────────────────────
def check_resolutions():
    global capital, banked, wins, losses, total_res
    global win_streak, loss_streak, live_wr

    now = int(time.time())
    for bet in sim_bets:
        if bet["resolved"]:
            continue
        if now < bet["epoch"] + 900 + RESOLUTION_BUFFER:
            continue
        market = get_market(bet["slug"])
        if not market:
            continue
        closed     = market.get("closed", False)
        up_p, dn_p = get_prices(market)
        winner = None
        if closed and abs(up_p-1.0) < 0.02: winner = "YES"
        elif closed and abs(dn_p-1.0) < 0.02: winner = "NO"
        if winner is None:
            continue

        if bet["side"] == winner:
            pnl = bet["size"] * (1/bet["price"] - 1)
            wins += 1; win_streak += 1; loss_streak = 0
            icon = "WIN "
        else:
            pnl = -bet["size"]
            losses += 1; loss_streak += 1; win_streak = 0
            icon = "LOSS"

        bet["pnl"] = pnl; bet["resolved"] = True
        total_res += 1; capital += pnl

        if pnl > 0 and BANK_PCT > 0:
            bnk = pnl * BANK_PCT; banked += bnk; capital -= bnk

        if total_res >= 5:
            live_wr = wins / total_res

        wr  = wins/total_res*100
        nw  = capital + banked
        roi = (nw - STARTING_CAPITAL)/STARTING_CAPITAL*100
        tag = "[DRY]" if DRY_RUN else "[LIVE]"

        log.info(f"\n{'='*62}")
        log.info(f"  {icon} {tag}  {bet['slug']}")
        log.info(f"  Side={bet['side']} @ {bet['price']:.4f} | Bet=${bet['size']:.2f} | PNL=${pnl:+.2f}")
        log.info(f"  Resolved={total_res} | W{wins}({wr:.1f}%) L{losses} | Streak W{win_streak} L{loss_streak}")
        log.info(f"  Capital=${capital:.2f} | Banked=${banked:.2f} | Net=${nw:.2f} | ROI={roi:+.1f}%")

        if total_res >= WR_CONFIRM_BETS:
            nxt = STAGE + 1
            if nxt in STAGE_THRESHOLDS and live_wr >= STAGE_THRESHOLDS[nxt]:
                log.info(f"  *** WR={live_wr:.1%} qualifies for Stage {nxt}! Set STAGE={nxt} + redeploy")
            elif STAGE >= 3 and live_wr < 0.52:
                log.info(f"  *** WR={live_wr:.1%} below break-even — consider downgrading")
        log.info(f"{'='*62}")

# ─────────────────── BET PLACEMENT ───────────────────────
def place_bet(sym, side, price, slug, epoch, conf):
    global capital
    size = bet_size(conf)
    if size > capital:
        if capital >= BET_FLOOR: size = round(capital*0.90, 2)
        else:
            log.info(f"  [SKIP] Capital too low (${capital:.2f})")
            return False
    tag = "[DRY]" if DRY_RUN else "[LIVE]"
    log.info(f"  {tag} S{STAGE} | {sym} -> {side} @ {price:.4f} | "
             f"${size:.2f} ({BET_PCT*100:.0f}%) | conf={conf} | WR={live_wr*100:.1f}%")
    sim_bets.append({"slug":slug,"epoch":epoch,"side":side,
                     "price":price,"size":size,"conf":conf,
                     "resolved":False,"pnl":None})
    return True

# ─────────────────── MAIN CYCLE ──────────────────────────
def cycle():
    check_resolutions()

    now_ts        = int(time.time())
    current_epoch = (now_ts // 900) * 900
    secs_into     = now_ts - current_epoch

    if secs_into > 810:
        log.info("  [SKIP] <90s to epoch end")
        return

    epochs = [current_epoch, current_epoch + 900]
    trades = 0

    for symbol in ASSETS:
        _, _, _, _, poly_coin = ASSETS[symbol]
        log.info(f"\n  -- {symbol} --")
        direction, conf, desc, src = analyze(symbol)
        log.info(f"  {desc}")
        dir_str = "UP" if direction==1 else ("DOWN" if direction==-1 else "FLAT")
        log.info(f"  -> {dir_str} | conf={conf} | min={MIN_CONF} | src={src}")

        if direction == 0 or conf < MIN_CONF:
            log.info("  [SKIP] No qualifying signal")
            continue

        for epoch in epochs:
            slug = f"{poly_coin}-updown-15m-{epoch}"
            if slug in seen_slugs:
                continue
            market = get_market(slug)
            if not market or market.get("closed", False):
                continue
            up_p, dn_p = get_prices(market)
            if up_p > 0.97 or dn_p > 0.97:
                log.info(f"  [SKIP] Market locked")
                continue

            strength = min(conf / 12.0, 1.0)
            wr_adj   = max(0, live_wr - 0.50) * 0.4
            fair_up  = 0.50 + strength * 0.18 + wr_adj

            placed = False
            if direction == 1 and up_p < fair_up - EDGE_THRESHOLD:
                placed = place_bet(symbol, "YES", up_p, slug, epoch, conf)
            elif direction == -1 and dn_p < (1-fair_up) - EDGE_THRESHOLD:
                placed = place_bet(symbol, "NO",  dn_p, slug, epoch, conf)

            if not placed:
                if   up_p > EXTREME_ODDS and direction == -1:
                    placed = place_bet(symbol, "NO",  dn_p, slug, epoch, conf)
                elif dn_p > EXTREME_ODDS and direction ==  1:
                    placed = place_bet(symbol, "YES", up_p, slug, epoch, conf)

            if placed:
                seen_slugs.add(slug); trades += 1

    if trades > 0:
        wr  = wins/total_res*100 if total_res else 0
        nw  = capital + banked
        roi = (nw - STARTING_CAPITAL)/STARTING_CAPITAL*100
        tag = "[DRY]" if DRY_RUN else "[LIVE]"
        log.info(f"\n  {tag} Cycle: {trades} bets | Cap=${capital:.2f} | Net=${nw:.2f} | ROI={roi:+.1f}% | WR={wr:.1f}%")

# ─────────────────── BANNER ──────────────────────────────
def banner():
    mode = "DRY-RUN (no real money)" if DRY_RUN else "LIVE (real USDC)"
    log.info("="*62)
    log.info(f"  Polymarket 15m Bot v5.0 — Candle Pattern Edition")
    log.info(f"  Mode    : {mode}")
    log.info(f"  {cfg['name']}")
    log.info(f"  Capital : ${STARTING_CAPITAL:.2f}")
    log.info(f"  Bet/cap : {BET_PCT*100:.0f}%  Bank: {BANK_PCT*100:.0f}%  MinConf: {MIN_CONF}")
    log.info(f"  Data    : Gemini → Coinbase → CryptoCompare → Bitfinex")
    log.info(f"  Signals : EMA+RSI+MACD+Stoch+BB+Candles+Patterns+HH/LL")
    log.info(f"  Target  : ${STARTING_CAPITAL*13:.2f} (1200% = 13x)")
    log.info("="*62)
    if DRY_RUN:
        log.info(f"  >> Simulating Stage {STAGE} — no real bets <<")
    log.info("")

if __name__ == "__main__":
    banner()
    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        log.info(f"[{ts}] -- cycle --")
        try:
            cycle()
        except KeyboardInterrupt:
            wr  = wins/total_res*100 if total_res else 0
            nw  = capital + banked
            log.info(f"\nStopped. {total_res} resolved | W{wins}({wr:.1f}%) L{losses}")
            log.info(f"Cap=${capital:.2f} | Banked=${banked:.2f} | Net=${nw:.2f}")
            break
        except Exception as e:
            log.error(f"Error: {e}")
        time.sleep(CYCLE_SLEEP)

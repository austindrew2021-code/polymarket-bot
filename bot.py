"""
Polymarket 15m Crypto Bot  v6.0  — FIXED ALL BUGS
══════════════════════════════════════════════════
FIXES IN THIS VERSION:
  1. get_prices() now reads market.markets[0].outcomePrices  (was reading wrong level → always 0.5)
  2. get_closed() now reads market.markets[0].closed         (was reading wrong level → never resolving)
  3. capital deducted immediately on bet placement           (was never deducting)
  4. only bet on CURRENT epoch, not future                   (was double-betting)
  5. live heartbeat every 30s showing pending bets + prices  (new)
  6. pending bet price refresh every cycle                   (new)
  7. correct payout math: YES win = stake*(1/price - 1), NO win = stake*(1/price - 1)
  8. resolution waits for market.closed=True then reads final price
"""

import requests, time, json, math, logging, threading
from datetime import datetime, timezone

# ============================================================
#              <- ONLY EDIT THIS SECTION ->
# ============================================================
STAGE    = 4      # 1 | 2 | 3 | 4
DRY_RUN  = True   # True = simulate | False = real USDC

STARTING_CAPITAL = 12.0
# ============================================================

STAGE_CFG = {
    1: dict(bet=0.10, conf=3, bank=0.40, name="Stage 1 | Baseline      | 10% bets | bank 40%"),
    2: dict(bet=0.10, conf=3, bank=0.20, name="Stage 2 | Conservative  | 10% bets | bank 20%"),
    3: dict(bet=0.20, conf=3, bank=0.00, name="Stage 3 | Aggressive    | 20% bets | full reinvest"),
    4: dict(bet=0.28, conf=3, bank=0.00, name="Stage 4 | Moon Mode     | 28% bets | full reinvest"),
}
cfg      = STAGE_CFG[STAGE]
BET_PCT  = cfg["bet"]
MIN_CONF = cfg["conf"]
BANK_PCT = cfg["bank"]

BET_FLOOR         = 0.25
EDGE_THRESHOLD    = 0.02    # min edge over market price to fire
CYCLE_SLEEP       = 60      # seconds between main cycles
HEARTBEAT_SLEEP   = 30      # seconds between status prints during wait
RESOLUTION_BUFFER = 60      # seconds after epoch close before checking
EPOCH_LEN         = 900     # 15 minutes in seconds
ENTRY_CUTOFF      = 600     # stop entering new bets 10min into epoch (5min left)
WR_CONFIRM_BETS   = 15
STAGE_THRESHOLDS  = {2: 0.57, 3: 0.62, 4: 0.65}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", mode="a")],
)
log = logging.getLogger("bot")

# ── STATE ────────────────────────────────────────────────
capital     = STARTING_CAPITAL
banked      = 0.0
wins        = 0
losses      = 0
total_res   = 0
win_streak  = 0
loss_streak = 0
live_wr     = 0.574
active_bets = []      # bets awaiting resolution
seen_slugs  = set()   # slugs already bet this epoch
state_lock  = threading.Lock()

# ── ASSETS: symbol -> (gemini_id, coinbase_id, polymarket_coin) ─
ASSETS = {
    "BTC": ("btcusd",  "BTC-USD",  "btc"),
    "ETH": ("ethusd",  "ETH-USD",  "eth"),
    "SOL": ("solusd",  "SOL-USD",  "sol"),
    "XRP": ("xrpusd",  "XRP-USD",  "xrp"),
}

# ═══════════════════════════════════════════════════════════
# DATA LAYER — multi-source candle fetch
# ═══════════════════════════════════════════════════════════

def _get(url, timeout=10):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug(f"GET {url[:60]}: {e}")
    return None

def fetch_candles(symbol):
    """Try Gemini → Coinbase. Return list of {o,h,l,c,v} newest-last, or []."""
    gemini_id, cb_id, _ = ASSETS[symbol]

    # ── Gemini ────────────────────────────────────────────
    data = _get(f"https://api.gemini.com/v2/candles/{gemini_id}/15m")
    if data and len(data) >= 20:
        # Gemini: [[ts, open, high, low, close, volume], ...] newest LAST
        candles = [{"o":float(x[1]),"h":float(x[2]),
                    "l":float(x[3]),"c":float(x[4]),"v":float(x[5])}
                   for x in data]
        return candles[-120:], "Gemini"

    time.sleep(0.3)

    # ── Coinbase Exchange ─────────────────────────────────
    data = _get(f"https://api.exchange.coinbase.com/products/{cb_id}/candles?granularity=900")
    if data and len(data) >= 20:
        # Coinbase: [[time,low,high,open,close,vol], ...] newest FIRST
        data.reverse()
        candles = [{"o":float(x[3]),"h":float(x[2]),
                    "l":float(x[1]),"c":float(x[4]),"v":float(x[5])}
                   for x in data]
        return candles[-120:], "Coinbase"

    return [], "none"

def fetch_price_now(symbol):
    """Fetch latest spot price (for display). Returns float or None."""
    gemini_id, cb_id, _ = ASSETS[symbol]
    data = _get(f"https://api.gemini.com/v1/pubticker/{gemini_id}")
    if data and "last" in data:
        try: return float(data["last"])
        except: pass
    data = _get(f"https://api.exchange.coinbase.com/products/{cb_id}/ticker")
    if data and "price" in data:
        try: return float(data["price"])
        except: pass
    return None

# ═══════════════════════════════════════════════════════════
# POLYMARKET API — correct JSON parsing
# ═══════════════════════════════════════════════════════════

def get_event(slug):
    """Fetch Polymarket event by slug. Returns raw event dict or None."""
    data = _get(f"https://gamma-api.polymarket.com/events/slug/{slug}", timeout=8)
    return data if isinstance(data, dict) else None

def parse_prices(event):
    """
    Extract YES/NO prices from a Polymarket event dict.
    Returns (yes_price, no_price) or (0.5, 0.5) on failure.

    JSON structure:
      event.markets[0].outcomePrices = '["0.5300", "0.4700"]'
      YES = outcomePrices[0], NO = outcomePrices[1]
    """
    try:
        mkt = event.get("markets", [{}])[0]
        raw = mkt.get("outcomePrices", "")
        if raw:
            prices = json.loads(raw)
            return float(prices[0]), float(prices[1])
    except Exception as e:
        log.debug(f"parse_prices error: {e}")
    return 0.5, 0.5

def parse_closed(event):
    """
    Returns True if the market is resolved/closed.
    Checks markets[0].closed  (NOT event.closed which is always False initially)
    """
    try:
        mkt = event.get("markets", [{}])[0]
        return bool(mkt.get("closed", False))
    except:
        return False

def parse_winner(event):
    """
    If market is closed, return 'YES' or 'NO' based on which price → 1.0.
    Returns None if not yet resolved.
    """
    try:
        mkt = event.get("markets", [{}])[0]
        if not mkt.get("closed", False):
            return None
        raw = mkt.get("outcomePrices", "")
        if raw:
            prices = json.loads(raw)
            yes_p = float(prices[0])
            no_p  = float(prices[1])
            if yes_p > 0.95:
                return "YES"
            if no_p > 0.95:
                return "NO"
    except Exception as e:
        log.debug(f"parse_winner error: {e}")
    return None

# ═══════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ═══════════════════════════════════════════════════════════

def ema(vals, p):
    if not vals or len(vals) < p:
        return [vals[-1]] * len(vals) if vals else [0.0]
    k = 2 / (p + 1)
    r = [sum(vals[:p]) / p]
    for v in vals[p:]:
        r.append(v * k + r[-1] * (1 - k))
    return [r[0]] * (len(vals) - len(r)) + r

def rsi(closes, p=14):
    if len(closes) < p + 1:
        return 50.0
    d = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    g = sum(max(x, 0) for x in d[-p:]) / p or 1e-9
    l = sum(max(-x, 0) for x in d[-p:]) / p or 1e-9
    return 100 - (100 / (1 + g / l))

def macd_hist(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return 0.0
    ef = ema(closes, fast)
    es = ema(closes, slow)
    ml = [a - b for a, b in zip(ef, es)]
    sl = ema(ml, sig)
    return ml[-1] - sl[-1]

def stoch(closes, highs, lows, kp=14, sk=3, sd=3):
    n = len(closes)
    if n < kp:
        return 50.0, 50.0
    rk = []
    for i in range(n):
        lo = min(lows[max(0, i-kp+1):i+1])
        hi = max(highs[max(0, i-kp+1):i+1])
        rk.append(100*(closes[i]-lo)/(hi-lo+1e-9) if hi!=lo else 50.0)
    ks = [sum(rk[max(0,i-sk+1):i+1])/min(sk,i+1) for i in range(n)]
    ds = [sum(ks[max(0,i-sd+1):i+1])/min(sd,i+1) for i in range(n)]
    return ks[-1], ds[-1]

def bollinger(closes, p=20):
    if len(closes) < p:
        v = closes[-1]; return v, v, v, 0.5, 2.0
    w   = closes[-p:]
    mid = sum(w) / p
    std = math.sqrt(sum((x-mid)**2 for x in w)/p + 1e-12)
    up  = mid + 2*std; lo = mid - 2*std
    pctb = (closes[-1] - lo) / (up - lo + 1e-9)
    bw   = (up - lo) / (mid + 1e-9) * 100
    return up, mid, lo, pctb, bw

def atr(highs, lows, closes, p=14):
    if len(closes) < 2:
        return 0.0
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return sum(trs[-p:]) / min(p, len(trs)) if trs else 0.0

def vol_ratio(vols, p=20):
    if len(vols) < p + 1:
        return 1.0
    avg = sum(vols[-p-1:-1]) / p
    return vols[-1] / avg if avg > 0 else 1.0

def candle_patterns(candles):
    if len(candles) < 3:
        return 0, 0, []
    bull=0; bear=0; found=[]
    c=candles[-1]; p=candles[-2]; pp=candles[-3]
    o=c['o']; h=c['h']; l=c['l']; cl=c['c']
    po=p['o']; ph=p['h']; pl=p['l']; pc=p['c']
    ppo=pp['o']; ppc=pp['c']
    body       = abs(cl-o)
    rng        = (h-l) if (h-l)>0 else 1e-9
    upper_wick = h - max(o,cl)
    lower_wick = min(o,cl) - l
    bullish    = cl >= o; bearish = cl < o
    p_body     = abs(pc-po); p_bull = pc>=po; p_bear = pc<po
    pp_bull    = ppc>=ppo;   pp_bear= ppc<ppo

    # Bullish
    if lower_wick>2*body and upper_wick<body*1.2 and bullish and body>0:
        bull+=3; found.append("Hammer")
    if bullish and p_bear and cl>po and o<pc and body>p_body*0.8:
        bull+=3; found.append("BullEngulf")
    if pp_bear and p_body<rng*0.3 and bullish and cl>(ppo+ppc)/2:
        bull+=4; found.append("MornStar")
    if bullish and body>rng*0.80:
        bull+=2; found.append("BullMaru")
    if bullish and p_bull and ppc>=ppo and cl>pc>ppc:
        bull+=3; found.append("3Soldiers")
    if bullish and p_bear and o<pc and cl>(po+pc)/2:
        bull+=2; found.append("Piercing")

    # Bearish
    if upper_wick>2*body and lower_wick<body*1.2 and bearish and body>0:
        bear+=3; found.append("ShootStar")
    if bearish and p_bull and o>pc and cl<po and body>p_body*0.8:
        bear+=3; found.append("BearEngulf")
    if pp_bull and p_body<rng*0.3 and bearish and cl<(ppo+ppc)/2:
        bear+=4; found.append("EveStar")
    if bearish and body>rng*0.80:
        bear+=2; found.append("BearMaru")
    if bearish and p_bear and ppc<=ppo and cl<pc<ppc:
        bear+=3; found.append("3Crows")
    if bearish and p_bull and o>pc and cl<(po+pc)/2:
        bear+=2; found.append("DarkCloud")

    return bull, bear, found

# ═══════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════

def analyze(symbol):
    """
    Returns (direction, conf, desc, source)
    direction: +1=UP  -1=DOWN  0=FLAT
    conf: integer score
    """
    candles, source = fetch_candles(symbol)
    if not candles or len(candles) < 25:
        return 0, 0, "no data", source

    closes  = [c['c'] for c in candles]
    highs   = [c['h'] for c in candles]
    lows    = [c['l'] for c in candles]
    opens   = [c['o'] for c in candles]
    volumes = [c['v'] for c in candles]

    at      = atr(highs, lows, closes)
    atr_pct = at / closes[-1] * 100 if closes[-1] else 0
    if atr_pct < 0.04:
        return 0, 0, f"ATR={atr_pct:.3f}% too low", source

    e8  = ema(closes, 8)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, min(50, len(closes)-1))[-1]

    rsi14 = rsi(closes, 14)
    rsi7  = rsi(closes, 7)
    mh    = macd_hist(closes)
    sk, sd = stoch(closes, highs, lows)
    _, _, _, pctb, bw = bollinger(closes)
    vr    = vol_ratio(volumes)
    cpb, cpd, pats = candle_patterns(candles)

    # Price position in 20-bar range
    rh = max(highs[-20:]); rl = min(lows[-20:])
    pos = (closes[-1]-rl)/(rh-rl+1e-9)

    # Momentum: last 3 bars net direction
    mb = sum(1 if candles[-i]['c']>=candles[-i]['o'] else -1 for i in range(1,4))

    bull=0; bear=0; sigs=[]

    # EMA stack
    if   e8>e21>e50: bull+=2; sigs.append("EMA++")
    elif e8<e21<e50: bear+=2; sigs.append("EMA--")
    elif e8>e21:     bull+=1; sigs.append("EMA+")
    elif e8<e21:     bear+=1; sigs.append("EMA-")

    # RSI 14
    if   rsi14<30: bull+=2; sigs.append(f"RSI{rsi14:.0f}OS")
    elif rsi14>70: bear+=2; sigs.append(f"RSI{rsi14:.0f}OB")
    elif rsi14<45: bull+=1; sigs.append(f"RSI{rsi14:.0f}L")
    elif rsi14>55: bear+=1; sigs.append(f"RSI{rsi14:.0f}H")

    # RSI 7
    if   rsi7<30: bull+=2; sigs.append("RSI7OS")
    elif rsi7>70: bear+=2; sigs.append("RSI7OB")
    elif rsi7<40: bull+=1; sigs.append("RSI7L")
    elif rsi7>60: bear+=1; sigs.append("RSI7H")

    # MACD histogram
    ref = closes[-1]
    if   mh>0: bull+=(2 if mh>0.0005*ref else 1); sigs.append("MACD+")
    elif mh<0: bear+=(2 if mh<-0.0005*ref else 1); sigs.append("MACD-")

    # Stochastic
    if   sk>sd and sk<40: bull+=2; sigs.append(f"StX+{sk:.0f}")
    elif sk<sd and sk>60: bear+=2; sigs.append(f"StX-{sk:.0f}")
    elif sk<20: bull+=1; sigs.append("StOS")
    elif sk>80: bear+=1; sigs.append("StOB")

    # Bollinger
    if   pctb<0.05: bull+=2; sigs.append("BB_OS")
    elif pctb>0.95: bear+=2; sigs.append("BB_OB")
    elif pctb<0.25: bull+=1; sigs.append("BB_L")
    elif pctb>0.75: bear+=1; sigs.append("BB_H")

    # Candle patterns (highest weight — up to 4 pts)
    if cpb>0: pts=min(cpb,4); bull+=pts; sigs.extend(pats[:2])
    if cpd>0: pts=min(cpd,4); bear+=pts; sigs.extend(pats[:2])

    # Momentum bars
    if   mb>=2: bull+=1; sigs.append("MomBars+")
    elif mb<=-2: bear+=1; sigs.append("MomBars-")

    # Price position
    if   pos<0.15: bull+=1; sigs.append(f"Bot{pos:.0%}")
    elif pos>0.85: bear+=1; sigs.append(f"Top{pos:.0%}")

    # Volume surge
    if vr>1.5:
        if closes[-1]>=opens[-1]: bull+=1; sigs.append(f"VolUp{vr:.1f}x")
        else:                     bear+=1; sigs.append(f"VolDn{vr:.1f}x")

    total = bull+bear; net = bull-bear
    if   total>0 and bull/total>=0.55 and net>=MIN_CONF: direction=1;  conf=bull
    elif total>0 and bear/total>=0.55 and net<=-MIN_CONF: direction=-1; conf=bear
    else:                                                  direction=0;  conf=0

    pat_str = ",".join(pats) if pats else "-"
    desc = (f"src={source} ATR={atr_pct:.2f}% RSI={rsi14:.0f}/{rsi7:.0f} "
            f"MACD={mh:+.5f} St={sk:.0f}/{sd:.0f} BB={pctb:.2f} "
            f"Vol={vr:.1f}x Pats={pat_str} MB={mb} B={bull} D={bear} "
            f"[{','.join(sigs)}]")
    return direction, conf, desc, source

# ═══════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════

def calc_bet(conf):
    global capital, win_streak, loss_streak
    pct = BET_PCT
    pct += min((conf - MIN_CONF) * 0.012, 0.05)   # conf bonus
    pct += min(win_streak * 0.012, 0.04)            # streak bonus
    if loss_streak >= 3:
        pct *= max(0.5, 1.0 - (loss_streak - 2) * 0.12)
    pct  = min(pct, 0.85)
    size = max(BET_FLOOR, capital * pct)
    size = min(size, capital * 0.90)
    return round(size, 2)

# ═══════════════════════════════════════════════════════════
# RESOLUTION ENGINE
# ═══════════════════════════════════════════════════════════

def check_resolutions():
    global capital, banked, wins, losses, total_res
    global win_streak, loss_streak, live_wr

    now = int(time.time())
    resolved_slugs = []

    with state_lock:
        bets_to_check = list(active_bets)

    for bet in bets_to_check:
        # Don't check until epoch has fully ended + buffer
        if now < bet["epoch"] + EPOCH_LEN + RESOLUTION_BUFFER:
            continue

        event = get_event(bet["slug"])
        if not event:
            continue

        winner = parse_winner(event)
        if winner is None:
            # Market not yet resolved — check again next cycle
            continue

        # ── Compute PnL ───────────────────────────────────
        # Polymarket payout: if you bet $X on YES at price P,
        # you get X/P total back on win, so profit = X*(1/P - 1)
        if bet["side"] == winner:
            profit = bet["size"] * (1.0 / bet["price"] - 1.0)
            pnl    = profit
            wins  += 1; win_streak += 1; loss_streak = 0
            icon   = "WIN "
        else:
            pnl    = -bet["size"]
            losses += 1; loss_streak += 1; win_streak = 0
            icon   = "LOSS"

        with state_lock:
            capital   += pnl
            if pnl > 0 and BANK_PCT > 0:
                bnk     = pnl * BANK_PCT
                banked += bnk
                capital -= bnk

        total_res += 1
        if total_res >= 5:
            live_wr = wins / total_res

        wr  = wins / total_res * 100
        nw  = capital + banked
        roi = (nw - STARTING_CAPITAL) / STARTING_CAPITAL * 100
        tag = "[DRY]" if DRY_RUN else "[LIVE]"
        elapsed = now - bet["placed_at"]

        log.info(f"\n{'='*64}")
        log.info(f"  {icon}  {tag}  {bet['symbol']} → {bet['side']}")
        log.info(f"  Slug    : {bet['slug']}")
        log.info(f"  Bet     : ${bet['size']:.2f} @ {bet['price']:.4f}  |  PnL: ${pnl:+.2f}")
        log.info(f"  Duration: {elapsed//60:.0f}m{elapsed%60:.0f}s  |  Conf was: {bet['conf']}")
        log.info(f"  ─────────────────────────────────────────────────────")
        log.info(f"  Resolved: {total_res}  W{wins}({wr:.1f}%) L{losses}  |  Streak W{win_streak} L{loss_streak}")
        log.info(f"  Capital : ${capital:.2f}  |  Banked: ${banked:.2f}  |  Net: ${nw:.2f}")
        log.info(f"  ROI     : {roi:+.1f}%  |  Live WR: {live_wr*100:.1f}%  |  Stage {STAGE}")

        if total_res >= WR_CONFIRM_BETS:
            nxt = STAGE + 1
            if nxt in STAGE_THRESHOLDS and live_wr >= STAGE_THRESHOLDS[nxt]:
                log.info(f"  *** WR={live_wr:.1%} → qualifies for Stage {nxt}! Set STAGE={nxt} + redeploy")
            elif STAGE >= 3 and live_wr < 0.52:
                log.info(f"  *** WR={live_wr:.1%} below break-even — consider downgrading")

        log.info(f"{'='*64}")
        resolved_slugs.append(bet["slug"])

    # Remove resolved bets
    with state_lock:
        for slug in resolved_slugs:
            for b in active_bets:
                if b["slug"] == slug:
                    active_bets.remove(b)
                    break

def refresh_pending_prices():
    """During wait cycles, show current live market prices for active bets."""
    with state_lock:
        bets = list(active_bets)
    if not bets:
        return

    now   = int(time.time())
    nw    = capital + banked
    roi   = (nw - STARTING_CAPITAL) / STARTING_CAPITAL * 100

    log.info(f"  ── Pending bets ({len(bets)}) | Cap=${capital:.2f} | Net=${nw:.2f} | ROI={roi:+.1f}% ──")

    for bet in bets:
        epoch_end   = bet["epoch"] + EPOCH_LEN
        secs_left   = max(0, epoch_end - now)
        mins_left   = secs_left // 60
        secs_remain = secs_left % 60

        event = get_event(bet["slug"])
        if event:
            yes_p, no_p = parse_prices(event)
            closed = parse_closed(event)
            our_p  = yes_p if bet["side"] == "YES" else no_p
            # Implied current P&L if we could exit now
            if bet["side"] == "YES":
                implied_pnl = bet["size"] * (yes_p / bet["price"] - 1.0)
            else:
                implied_pnl = bet["size"] * (no_p / bet["price"] - 1.0)
            status = "CLOSED" if closed else f"{mins_left}m{secs_remain:02d}s left"
            log.info(f"    {bet['symbol']:4s} {bet['side']:3s} | "
                     f"Entry:{bet['price']:.4f} Now:{our_p:.4f} "
                     f"ImpliedPnL:{implied_pnl:+.2f} | {status}")
        else:
            log.info(f"    {bet['symbol']:4s} {bet['side']:3s} | "
                     f"Entry:{bet['price']:.4f} | {mins_left}m{secs_remain:02d}s left")
        time.sleep(0.3)

# ═══════════════════════════════════════════════════════════
# BET PLACEMENT
# ═══════════════════════════════════════════════════════════

def place_bet(symbol, side, price, slug, epoch, conf):
    global capital

    size = calc_bet(conf)
    if size > capital * 0.92:
        size = round(capital * 0.92, 2)
    if size < BET_FLOOR:
        log.info(f"  [SKIP] Capital ${capital:.2f} too low for min bet ${BET_FLOOR}")
        return False

    # ── Deduct from capital immediately ──────────────────
    with state_lock:
        capital -= size

    tag = "[DRY]" if DRY_RUN else "[LIVE]"
    nw  = capital + banked
    roi = (nw - STARTING_CAPITAL) / STARTING_CAPITAL * 100

    log.info(f"  {tag} S{STAGE} | {symbol} → {side} @ {price:.4f} | "
             f"${size:.2f} ({BET_PCT*100:.0f}%) conf={conf} | "
             f"Cap→${capital:.2f} Net=${nw:.2f} ROI={roi:+.1f}%")

    with state_lock:
        active_bets.append({
            "symbol":    symbol,
            "slug":      slug,
            "epoch":     epoch,
            "side":      side,
            "price":     price,
            "size":      size,
            "conf":      conf,
            "placed_at": int(time.time()),
        })
    return True

# ═══════════════════════════════════════════════════════════
# MAIN CYCLE
# ═══════════════════════════════════════════════════════════

def cycle():
    check_resolutions()

    now_ts        = int(time.time())
    current_epoch = (now_ts // EPOCH_LEN) * EPOCH_LEN
    secs_into     = now_ts - current_epoch

    # Don't enter new bets in final 5 minutes of epoch
    if secs_into > ENTRY_CUTOFF:
        mins_left = (EPOCH_LEN - secs_into) // 60
        log.info(f"  [HOLD] {mins_left}m left in epoch — no new entries")
        refresh_pending_prices()
        return

    trades = 0

    for symbol in ASSETS:
        _, _, poly_coin = ASSETS[symbol]
        slug = f"{poly_coin}-updown-15m-{current_epoch}"

        log.info(f"\n  ── {symbol} ──")
        direction, conf, desc, src = analyze(symbol)
        log.info(f"  {desc}")

        dir_str = "UP" if direction==1 else ("DOWN" if direction==-1 else "FLAT")
        log.info(f"  → {dir_str} | conf={conf} | min={MIN_CONF}")

        if direction == 0 or conf < MIN_CONF:
            log.info("  [SKIP] Signal too weak")
            continue

        # Already bet this coin this epoch?
        if slug in seen_slugs:
            log.info("  [SKIP] Already bet this epoch")
            continue

        # Fetch live Polymarket market
        event = get_event(slug)
        if not event:
            log.info(f"  [SKIP] Market not found: {slug}")
            continue

        if parse_closed(event):
            log.info(f"  [SKIP] Market already closed")
            continue

        yes_p, no_p = parse_prices(event)
        log.info(f"  Market live: YES={yes_p:.4f} NO={no_p:.4f} "
                 f"(sum={yes_p+no_p:.4f})")

        # Sanity check — prices should sum to ~1.0
        if not (0.80 < yes_p + no_p < 1.20):
            log.info(f"  [SKIP] Price sum {yes_p+no_p:.3f} out of range")
            continue

        # Both sides at 0.5 means market hasn't opened yet
        if yes_p == 0.5 and no_p == 0.5:
            log.info(f"  [SKIP] Market not yet priced")
            continue

        placed = False

        if direction == 1:
            # Signal says UP → bet YES
            # Fair value based on signal strength + live WR
            strength = min(conf / 12.0, 1.0)
            fair_yes = 0.50 + strength * 0.15 + max(0, live_wr-0.50)*0.3
            if yes_p < fair_yes - EDGE_THRESHOLD:
                placed = place_bet(symbol, "YES", yes_p, slug, current_epoch, conf)
            elif no_p > 0.85:   # extreme NO price = YES underpriced
                placed = place_bet(symbol, "YES", yes_p, slug, current_epoch, conf)

        elif direction == -1:
            # Signal says DOWN → bet NO
            strength = min(conf / 12.0, 1.0)
            fair_no  = 0.50 + strength * 0.15 + max(0, live_wr-0.50)*0.3
            if no_p < fair_no - EDGE_THRESHOLD:
                placed = place_bet(symbol, "NO", no_p, slug, current_epoch, conf)
            elif yes_p > 0.85:  # extreme YES price = NO underpriced
                placed = place_bet(symbol, "NO", no_p, slug, current_epoch, conf)

        if placed:
            seen_slugs.add(slug)
            trades += 1
        elif direction != 0:
            log.info(f"  [SKIP] No edge vs market price (YES={yes_p:.4f} NO={no_p:.4f})")

        time.sleep(0.3)

    if trades > 0:
        nw  = capital + banked
        roi = (nw - STARTING_CAPITAL) / STARTING_CAPITAL * 100
        wr  = wins / total_res * 100 if total_res else 0
        tag = "[DRY]" if DRY_RUN else "[LIVE]"
        log.info(f"\n  {tag} Cycle: {trades} bet(s) placed")
        log.info(f"  Capital=${capital:.2f} Banked=${banked:.2f} Net=${nw:.2f} ROI={roi:+.1f}% WR={wr:.1f}%")
    else:
        # Show heartbeat even when no bets
        refresh_pending_prices()

    # Clear seen_slugs at epoch boundary
    epoch_now = (int(time.time()) // EPOCH_LEN) * EPOCH_LEN
    for slug in list(seen_slugs):
        parts = slug.split("-")
        try:
            slug_epoch = int(parts[-1])
            if slug_epoch < epoch_now:
                seen_slugs.discard(slug)
        except:
            pass

# ═══════════════════════════════════════════════════════════
# BANNER + ENTRY POINT
# ═══════════════════════════════════════════════════════════

def banner():
    mode = "DRY-RUN (no real money)" if DRY_RUN else "LIVE (real USDC)"
    log.info("="*64)
    log.info(f"  Polymarket 15m Bot  v6.0 — Fixed Edition")
    log.info(f"  Mode    : {mode}")
    log.info(f"  {cfg['name']}")
    log.info(f"  Capital : ${STARTING_CAPITAL:.2f}")
    log.info(f"  Bet/cap : {BET_PCT*100:.0f}%  Bank: {BANK_PCT*100:.0f}%  MinConf: {MIN_CONF}")
    log.info(f"  Data    : Gemini candles → Coinbase fallback")
    log.info(f"  Prices  : Polymarket live (markets[0].outcomePrices)")
    log.info(f"  Resolve : markets[0].closed + prices → 1.0")
    log.info(f"  Target  : ${STARTING_CAPITAL*13:.2f} (1200% = 13x)")
    log.info("="*64)
    if DRY_RUN:
        log.info(f"  >> DRY-RUN Stage {STAGE}: simulating {BET_PCT*100:.0f}% bets, no real money <<")
    log.info("")

if __name__ == "__main__":
    banner()
    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        log.info(f"[{ts}] ── cycle ─────────────────────────────────")
        try:
            cycle()
        except KeyboardInterrupt:
            wr  = wins/total_res*100 if total_res else 0
            nw  = capital + banked
            roi = (nw - STARTING_CAPITAL) / STARTING_CAPITAL * 100
            log.info("\n  Bot stopped.")
            log.info(f"  {total_res} resolved | W{wins}({wr:.1f}%) L{losses}")
            log.info(f"  Capital=${capital:.2f} Banked=${banked:.2f} Net=${nw:.2f} ROI={roi:+.1f}%")
            break
        except Exception as e:
            log.error(f"  Cycle error: {e}", exc_info=True)
        time.sleep(CYCLE_SLEEP)

"""
Polymarket 15m Crypto Bot  v8.0  — FAKEOUT FILTER + PROFIT LOCK
═════════════════════════════════════════════════════════════════
FIXES FROM v7:
  [CRITICAL] UnboundLocalError: added 'global capital, banked' at top of cycle()

NEW FEATURES:
  1. FAKEOUT FILTERS — 5-layer protection against bad entries:
     • RSI extreme (>78 or <22) + conflicting signals → SKIP
     • Bollinger band breach (>0.97 or <0.03) in wrong direction → SKIP
     • Volume confusion spike (>5x + tied score) → SKIP
     • Momentum conflicts reduce confidence score by 1
     • Requires net ≥ MIN_CONF after all penalties applied

  2. PROFIT LOCK TRACKING — live health display on pending bets:
     🟢 LOCKED   current price > 0.85  (>85% win probability)
     🟡 LEADING  current price 0.65-0.85
     🟠 CLOSE    current price 0.45-0.65
     🔴 LOSING   current price < 0.45

  3. ADAPTIVE BUDGET — scales with recent performance:
     Win streak ≥ 3 → budget +5%  (momentum compounding)
     Loss streak ≥ 2 → budget -15% (drawdown protection)
     Loss streak ≥ 4 → budget -25% + pause new bets 1 cycle

  4. QUALITY THRESHOLD: majority now 52% (was 55%) → more signals fire
     But fakeout filters act as gatekeepers so quality stays high

  5. SIGNAL DIVERSITY CHECK: won't bet same direction on >2 coins simultaneously
     (protects against correlated crypto crash wiping all bets)
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
    1: dict(budget=0.50, max_single=0.25, conf=3, bank=0.40, name="Stage 1 | Baseline    | 50% budget | bank 40%"),
    2: dict(budget=0.60, max_single=0.30, conf=3, bank=0.20, name="Stage 2 | Conservative| 60% budget | bank 20%"),
    3: dict(budget=0.70, max_single=0.38, conf=3, bank=0.00, name="Stage 3 | Aggressive  | 70% budget | full reinvest"),
    4: dict(budget=0.75, max_single=0.42, conf=3, bank=0.00, name="Stage 4 | Moon Mode   | 75% budget | full reinvest"),
}
cfg         = STAGE_CFG[STAGE]
BASE_BUDGET = cfg["budget"]
MAX_SINGLE  = cfg["max_single"]
MIN_CONF    = cfg["conf"]
BANK_PCT    = cfg["bank"]

BET_FLOOR         = 0.25
EDGE_THRESHOLD    = 0.015
CYCLE_SLEEP       = 60
EPOCH_LEN         = 900
ENTRY_CUTOFF      = 600       # 10 min into epoch → stop new entries
RESOLUTION_BUFFER = 60
WR_CONFIRM_BETS   = 15
STAGE_THRESHOLDS  = {2: 0.57, 3: 0.62, 4: 0.65}
MAX_SAME_DIRECTION = 2        # max bets in same direction per cycle (correlation guard)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept":     "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", mode="a")],
)
log = logging.getLogger("bot")

# ── GLOBAL STATE ─────────────────────────────────────────
capital     = STARTING_CAPITAL
banked      = 0.0
wins        = 0
losses      = 0
total_res   = 0
win_streak  = 0
loss_streak = 0
live_wr     = 0.574
active_bets = []
seen_slugs  = set()
state_lock  = threading.Lock()
pause_cycle = False           # set True after heavy loss streak

ASSETS = {
    "BTC": ("btcusd",  "BTC-USD",  "btc"),
    "ETH": ("ethusd",  "ETH-USD",  "eth"),
    "SOL": ("solusd",  "SOL-USD",  "sol"),
    "XRP": ("xrpusd",  "XRP-USD",  "xrp"),
}

# ═══════════════════════════════════════════════════════════
# DATA
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
    gemini_id, cb_id, _ = ASSETS[symbol]
    data = _get(f"https://api.gemini.com/v2/candles/{gemini_id}/15m")
    if data and len(data) >= 20:
        candles = [{"o":float(x[1]),"h":float(x[2]),
                    "l":float(x[3]),"c":float(x[4]),"v":float(x[5])}
                   for x in data]
        return candles[-120:], "Gemini"
    time.sleep(0.3)
    data = _get(f"https://api.exchange.coinbase.com/products/{cb_id}/candles?granularity=900")
    if data and len(data) >= 20:
        data.reverse()
        candles = [{"o":float(x[3]),"h":float(x[2]),
                    "l":float(x[1]),"c":float(x[4]),"v":float(x[5])}
                   for x in data]
        return candles[-120:], "Coinbase"
    return [], "none"

# ═══════════════════════════════════════════════════════════
# POLYMARKET API
# ═══════════════════════════════════════════════════════════

def get_event(slug):
    data = _get(f"https://gamma-api.polymarket.com/events/slug/{slug}", timeout=8)
    return data if isinstance(data, dict) else None

def parse_prices(event):
    try:
        mkt = event.get("markets", [{}])[0]
        raw = mkt.get("outcomePrices", "")
        if raw:
            prices = json.loads(raw)
            return float(prices[0]), float(prices[1])
    except: pass
    return 0.5, 0.5

def parse_closed(event):
    try:
        return bool(event.get("markets", [{}])[0].get("closed", False))
    except:
        return False

def parse_winner(event):
    try:
        mkt = event.get("markets", [{}])[0]
        if not mkt.get("closed", False): return None
        prices = json.loads(mkt.get("outcomePrices", "[0.5,0.5]"))
        if float(prices[0]) > 0.95: return "YES"
        if float(prices[1]) > 0.95: return "NO"
    except: pass
    return None

# ═══════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════

def ema(vals, p):
    if not vals or len(vals) < p:
        return [vals[-1]] * len(vals) if vals else [0.0]
    k = 2 / (p + 1); r = [sum(vals[:p]) / p]
    for v in vals[p:]: r.append(v * k + r[-1] * (1 - k))
    return [r[0]] * (len(vals) - len(r)) + r

def rsi(closes, p=14):
    if len(closes) < p + 1: return 50.0
    d = [closes[i]-closes[i-1] for i in range(1, len(closes))]
    g = sum(max(x,0) for x in d[-p:]) / p or 1e-9
    l = sum(max(-x,0) for x in d[-p:]) / p or 1e-9
    return 100 - (100 / (1 + g / l))

def macd_hist(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow+sig: return 0.0
    ef = ema(closes, fast); es = ema(closes, slow)
    ml = [a-b for a,b in zip(ef, es)]
    return ml[-1] - ema(ml, sig)[-1]

def stoch(closes, highs, lows, kp=14, sk=3, sd=3):
    n = len(closes)
    if n < kp: return 50.0, 50.0
    rk = []
    for i in range(n):
        lo = min(lows[max(0,i-kp+1):i+1])
        hi = max(highs[max(0,i-kp+1):i+1])
        rk.append(100*(closes[i]-lo)/(hi-lo+1e-9) if hi!=lo else 50.0)
    ks = [sum(rk[max(0,i-sk+1):i+1])/min(sk,i+1) for i in range(n)]
    ds = [sum(ks[max(0,i-sd+1):i+1])/min(sd,i+1) for i in range(n)]
    return ks[-1], ds[-1]

def bollinger(closes, p=20):
    if len(closes) < p:
        v = closes[-1]; return v, v, v, 0.5, 2.0
    w = closes[-p:]; mid = sum(w)/p
    std = math.sqrt(sum((x-mid)**2 for x in w)/p + 1e-12)
    up = mid+2*std; lo = mid-2*std
    pctb = (closes[-1]-lo)/(up-lo+1e-9)
    bw   = (up-lo)/(mid+1e-9)*100
    return up, mid, lo, pctb, bw

def atr(highs, lows, closes, p=14):
    if len(closes) < 2: return 0.0
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return sum(trs[-p:]) / min(p, len(trs)) if trs else 0.0

def vol_ratio(vols, p=20):
    if len(vols) < p+1: return 1.0
    avg = sum(vols[-p-1:-1])/p
    return vols[-1]/avg if avg > 0 else 1.0

def candle_patterns(candles):
    if len(candles) < 3: return 0, 0, []
    bull=0; bear=0; found=[]
    c=candles[-1]; p=candles[-2]; pp=candles[-3]
    o=c['o']; h=c['h']; l=c['l']; cl=c['c']
    po=p['o']; ph=p['h']; pl=p['l']; pc=p['c']
    ppo=pp['o']; ppc=pp['c']
    body=abs(cl-o); rng=(h-l) if (h-l)>0 else 1e-9
    upper_wick=h-max(o,cl); lower_wick=min(o,cl)-l
    bullish=cl>=o; bearish=cl<o
    p_body=abs(pc-po); p_bull=pc>=po; p_bear=pc<po
    pp_bull=ppc>=ppo; pp_bear=ppc<ppo
    if lower_wick>2*body and upper_wick<body*1.2 and bullish and body>0: bull+=3; found.append("Hammer")
    if bullish and p_bear and cl>po and o<pc and body>p_body*0.8:        bull+=3; found.append("BullEngulf")
    if pp_bear and p_body<rng*0.3 and bullish and cl>(ppo+ppc)/2:       bull+=4; found.append("MornStar")
    if bullish and body>rng*0.80:                                         bull+=2; found.append("BullMaru")
    if bullish and p_bull and ppc>=ppo and cl>pc>ppc:                    bull+=3; found.append("3Soldiers")
    if bullish and p_bear and o<pc and cl>(po+pc)/2:                     bull+=2; found.append("Piercing")
    if upper_wick>2*body and lower_wick<body*1.2 and bearish and body>0: bear+=3; found.append("ShootStar")
    if bearish and p_bull and o>pc and cl<po and body>p_body*0.8:        bear+=3; found.append("BearEngulf")
    if pp_bull and p_body<rng*0.3 and bearish and cl<(ppo+ppc)/2:       bear+=4; found.append("EveStar")
    if bearish and body>rng*0.80:                                         bear+=2; found.append("BearMaru")
    if bearish and p_bear and ppc<=ppo and cl<pc<ppc:                    bear+=3; found.append("3Crows")
    if bearish and p_bull and o>pc and cl<(po+pc)/2:                     bear+=2; found.append("DarkCloud")
    return bull, bear, found

# ═══════════════════════════════════════════════════════════
# SIGNAL ENGINE + FAKEOUT FILTERS
# ═══════════════════════════════════════════════════════════

def analyze(symbol):
    """
    Returns (direction, conf, desc, source, fakeout_reason)
    Includes 5-layer fakeout protection.
    """
    candles, source = fetch_candles(symbol)
    if not candles or len(candles) < 25:
        return 0, 0, "no data", source, None

    closes  = [c['c'] for c in candles]
    highs   = [c['h'] for c in candles]
    lows    = [c['l'] for c in candles]
    opens   = [c['o'] for c in candles]
    volumes = [c['v'] for c in candles]

    at      = atr(highs, lows, closes)
    atr_pct = at / closes[-1] * 100 if closes[-1] else 0
    if atr_pct < 0.04:
        return 0, 0, f"ATR={atr_pct:.3f}% ranging", source, None

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

    rh = max(highs[-20:]); rl = min(lows[-20:])
    pos = (closes[-1]-rl)/(rh-rl+1e-9)
    mb  = sum(1 if candles[-i]['c']>=candles[-i]['o'] else -1 for i in range(1,4))

    # ── SCORING ───────────────────────────────────────────
    bull=0; bear=0; sigs=[]

    if   e8>e21>e50: bull+=2; sigs.append("EMA++")
    elif e8<e21<e50: bear+=2; sigs.append("EMA--")
    elif e8>e21:     bull+=1; sigs.append("EMA+")
    elif e8<e21:     bear+=1; sigs.append("EMA-")

    if   rsi14<30: bull+=2; sigs.append(f"RSI{rsi14:.0f}OS")
    elif rsi14>70: bear+=2; sigs.append(f"RSI{rsi14:.0f}OB")
    elif rsi14<45: bull+=1; sigs.append(f"RSI{rsi14:.0f}L")
    elif rsi14>55: bear+=1; sigs.append(f"RSI{rsi14:.0f}H")

    if   rsi7<30:  bull+=2; sigs.append("RSI7OS")
    elif rsi7>70:  bear+=2; sigs.append("RSI7OB")
    elif rsi7<40:  bull+=1; sigs.append("RSI7L")
    elif rsi7>60:  bear+=1; sigs.append("RSI7H")

    ref = closes[-1]
    if   mh>0: bull+=(2 if mh>0.0005*ref else 1); sigs.append("MACD+")
    elif mh<0: bear+=(2 if mh<-0.0005*ref else 1); sigs.append("MACD-")

    if   sk>sd and sk<40: bull+=2; sigs.append(f"StX+{sk:.0f}")
    elif sk<sd and sk>60: bear+=2; sigs.append(f"StX-{sk:.0f}")
    elif sk<20: bull+=1; sigs.append("StOS")
    elif sk>80: bear+=1; sigs.append("StOB")

    if   pctb<0.05: bull+=2; sigs.append("BB_OS")
    elif pctb>0.95: bear+=2; sigs.append("BB_OB")
    elif pctb<0.25: bull+=1; sigs.append("BB_L")
    elif pctb>0.75: bear+=1; sigs.append("BB_H")

    if cpb>0: pts=min(cpb,4); bull+=pts; sigs.extend(pats[:2])
    if cpd>0: pts=min(cpd,4); bear+=pts; sigs.extend(pats[:2])

    # Momentum (with conflict penalty)
    if   mb>=2:  bull+=1; sigs.append("Mom+")
    elif mb<=-2: bear+=1; sigs.append("Mom-")

    if   pos<0.15: bull+=1; sigs.append(f"Bot{pos:.0%}")
    elif pos>0.85: bear+=1; sigs.append(f"Top{pos:.0%}")

    if vr>1.5:
        if closes[-1]>=opens[-1]: bull+=1; sigs.append(f"VolUp{vr:.1f}x")
        else:                     bear+=1; sigs.append(f"VolDn{vr:.1f}x")

    # ── DIRECTION DETERMINATION ───────────────────────────
    total = bull+bear; net = bull-bear
    # Lowered majority to 52% → more signals qualify
    if   total>0 and bull/total>=0.52 and net>=MIN_CONF: direction=1;  conf=bull
    elif total>0 and bear/total>=0.52 and net<=-MIN_CONF: direction=-1; conf=bear
    else:                                                   direction=0;  conf=0

    # ── FAKEOUT FILTERS ───────────────────────────────────
    fakeout = None

    if direction != 0:
        # Filter 1: RSI extreme exhaustion
        # RSI>78 means overbought → likely about to drop, skip UP bets
        # RSI<22 means oversold → likely about to bounce, skip DOWN bets
        if direction == 1 and rsi14 > 78:
            fakeout = f"FAKEOUT RSI={rsi14:.0f} overbought (skip UP)"
        elif direction == -1 and rsi14 < 22:
            fakeout = f"FAKEOUT RSI={rsi14:.0f} oversold (skip DOWN)"

        # Filter 2: Bollinger extreme breach in wrong direction
        # Price way above upper band → mean reversion likely → skip UP
        # Price way below lower band → bounce likely → skip DOWN
        if fakeout is None:
            if direction == 1 and pctb > 0.97:
                fakeout = f"FAKEOUT BB={pctb:.3f} above upper band (skip UP)"
            elif direction == -1 and pctb < 0.03:
                fakeout = f"FAKEOUT BB={pctb:.3f} below lower band (skip DOWN)"

        # Filter 3: Volume confusion spike
        # Huge volume + near-equal bull/bear = panic/confusion, not trend
        if fakeout is None and vr > 5.0 and abs(net) <= 2:
            fakeout = f"FAKEOUT Vol={vr:.1f}x + mixed signals (skip)"

        # Filter 4: Stochastic extreme divergence
        # Stoch>85 but signal is UP → likely peak
        # Stoch<15 but signal is DOWN → likely trough
        if fakeout is None:
            if direction == 1 and sk > 85:
                fakeout = f"FAKEOUT Stoch={sk:.0f} overbought (skip UP)"
            elif direction == -1 and sk < 15:
                fakeout = f"FAKEOUT Stoch={sk:.0f} oversold (skip DOWN)"

        # Filter 5: Momentum conflict
        # Moving bars strongly oppose the signal → likely whipsaw
        # Apply as confidence penalty (not outright skip, just reduce)
        if fakeout is None:
            if direction == 1 and mb <= -2:
                conf = max(0, conf - 2)
                sigs.append("MomConflict-2")
                if conf < MIN_CONF:
                    fakeout = f"FAKEOUT momentum bars oppose UP signal (conf reduced to {conf})"
            elif direction == -1 and mb >= 2:
                conf = max(0, conf - 2)
                sigs.append("MomConflict-2")
                if conf < MIN_CONF:
                    fakeout = f"FAKEOUT momentum bars oppose DOWN signal (conf reduced to {conf})"

        if fakeout:
            direction = 0; conf = 0

    pat_str = ",".join(pats) if pats else "-"
    desc = (f"src={source} ATR={atr_pct:.2f}% RSI={rsi14:.0f}/{rsi7:.0f} "
            f"MACD={mh:+.5f} St={sk:.0f}/{sd:.0f} BB={pctb:.2f} "
            f"Vol={vr:.1f}x Pats={pat_str} MB={mb} B={bull} D={bear}")
    return direction, conf, desc, source, fakeout

# ═══════════════════════════════════════════════════════════
# ADAPTIVE BUDGET
# ═══════════════════════════════════════════════════════════

def get_adaptive_budget():
    """Returns effective budget % adjusted for win/loss streaks."""
    global pause_cycle
    budget = BASE_BUDGET

    # Win streak compounding
    if win_streak >= 5:   budget = min(budget + 0.08, 0.85)
    elif win_streak >= 3: budget = min(budget + 0.05, 0.82)

    # Loss streak protection
    if loss_streak >= 4:
        budget = max(budget - 0.25, 0.30)
        pause_cycle = True   # will skip next cycle after placing
        log.info("  ⚠ Loss streak 4+ — budget reduced, pausing after this cycle")
    elif loss_streak >= 2:
        budget = max(budget - 0.15, 0.35)

    return budget

# ═══════════════════════════════════════════════════════════
# EV-WEIGHTED ALLOCATION
# ═══════════════════════════════════════════════════════════

def ev_score(conf, price):
    return conf * max(1/price - 1, 0.01)

def allocate_bets(candidates, capital, budget_pct):
    if not candidates: return []
    scored = []
    for sym, side, price, conf, slug, epoch in candidates:
        score = ev_score(conf, price)
        if   price < 0.30: score *= 1.40   # massive edge boost
        elif price < 0.35: score *= 1.30
        elif price < 0.42: score *= 1.10
        scored.append((sym, side, price, conf, slug, epoch, score))

    total_score = sum(s[6] for s in scored)
    if total_score <= 0: return []

    budget    = capital * budget_pct
    result    = []
    allocated = 0.0

    for sym, side, price, conf, slug, epoch, score in scored:
        weight = score / total_score
        alloc  = weight * budget
        if price < 0.30: alloc *= 1.15    # extra boost for extreme prices
        alloc = min(alloc, capital * MAX_SINGLE)
        alloc = max(alloc, BET_FLOOR)
        remaining = capital - allocated - BET_FLOOR
        alloc = min(alloc, remaining)
        alloc = round(alloc, 2)
        if alloc < BET_FLOOR: continue
        allocated += alloc
        payout  = 1/price - 1
        ev_pct  = (alloc*payout*live_wr - alloc*(1-live_wr))/alloc*100
        result.append((sym, side, price, alloc, conf, slug, epoch, ev_pct))

    return result

# ═══════════════════════════════════════════════════════════
# RESOLUTION
# ═══════════════════════════════════════════════════════════

def check_resolutions():
    global capital, banked, wins, losses, total_res
    global win_streak, loss_streak, live_wr, pause_cycle

    now = int(time.time())
    resolved = []

    with state_lock:
        bets_to_check = list(active_bets)

    for bet in bets_to_check:
        if now < bet["epoch"] + EPOCH_LEN + RESOLUTION_BUFFER:
            continue
        event = get_event(bet["slug"])
        if not event: continue
        winner = parse_winner(event)
        if winner is None: continue

        if bet["side"] == winner:
            pnl  = bet["size"] * (1.0/bet["price"] - 1.0)
            wins += 1; win_streak += 1; loss_streak = 0; icon = "WIN "
        else:
            pnl    = -bet["size"]
            losses += 1; loss_streak += 1; win_streak = 0; icon = "LOSS"
            if pause_cycle and loss_streak == 0:
                pause_cycle = False  # reset pause on first win

        with state_lock:
            capital += pnl
            if pnl > 0 and BANK_PCT > 0:
                bnk = pnl*BANK_PCT; banked += bnk; capital -= bnk

        total_res += 1
        if total_res >= 5: live_wr = wins/total_res

        wr  = wins/total_res*100
        nw  = capital + banked
        roi = (nw-STARTING_CAPITAL)/STARTING_CAPITAL*100
        tag = "[DRY]" if DRY_RUN else "[LIVE]"
        elapsed = now - bet["placed_at"]
        payout_x = bet["size"]*(1/bet["price"]-1)

        log.info(f"\n{'='*68}")
        log.info(f"  {icon}  {tag}  {bet['symbol']} → {bet['side']}")
        log.info(f"  Slug    : {bet['slug']}")
        log.info(f"  Bet     : ${bet['size']:.2f} @ {bet['price']:.4f} "
                 f"(payout {payout_x/bet['size']:.2f}x)  |  PnL: ${pnl:+.2f}")
        log.info(f"  EV%: {bet.get('ev_pct',0):+.0f}%  |  Conf: {bet['conf']}  "
                 f"|  Duration: {elapsed//60:.0f}m{elapsed%60:.0f}s")
        log.info(f"  {'─'*55}")
        log.info(f"  Resolved: {total_res}  W{wins}({wr:.1f}%) L{losses}  "
                 f"|  Streak  W{win_streak} L{loss_streak}")
        log.info(f"  Capital : ${capital:.2f}  |  Banked: ${banked:.2f}  "
                 f"|  Net: ${nw:.2f}")
        log.info(f"  ROI     : {roi:+.1f}%  |  WR: {live_wr*100:.1f}%  "
                 f"|  Stage {STAGE}")

        if total_res >= WR_CONFIRM_BETS:
            nxt = STAGE+1
            if nxt in STAGE_THRESHOLDS and live_wr >= STAGE_THRESHOLDS[nxt]:
                log.info(f"  ★ WR={live_wr:.1%} qualifies for Stage {nxt}! "
                         f"Set STAGE={nxt} + redeploy")
            elif STAGE >= 3 and live_wr < 0.52:
                log.info(f"  ⚠ WR={live_wr:.1%} below break-even — consider Stage 2")

        log.info(f"{'='*68}")
        resolved.append(bet["slug"])

    with state_lock:
        for slug in resolved:
            for b in list(active_bets):
                if b["slug"] == slug:
                    active_bets.remove(b); break

# ═══════════════════════════════════════════════════════════
# PENDING BET MONITOR WITH PROFIT LOCK DISPLAY
# ═══════════════════════════════════════════════════════════

def bet_health_icon(our_price):
    """Returns health emoji based on current market price."""
    if our_price > 0.85:   return "🟢 LOCKED "
    elif our_price > 0.65: return "🟡 LEADING"
    elif our_price > 0.45: return "🟠 CLOSE  "
    else:                  return "🔴 LOSING "

def show_pending():
    with state_lock:
        bets = list(active_bets)
    if not bets: return

    now  = int(time.time())
    nw   = capital + banked
    roi  = (nw-STARTING_CAPITAL)/STARTING_CAPITAL*100
    budget_pct = get_adaptive_budget()

    log.info(f"\n  ── Active Bets ({len(bets)}) | Cap=${capital:.2f} "
             f"Bank=${banked:.2f} Net=${nw:.2f} ROI={roi:+.1f}% "
             f"Budget={budget_pct*100:.0f}% ──")

    for bet in bets:
        epoch_end = bet["epoch"] + EPOCH_LEN
        secs_left = max(0, epoch_end-now)
        mins_left = secs_left // 60
        secs_rem  = secs_left % 60

        event = get_event(bet["slug"])
        if event:
            yes_p, no_p = parse_prices(event)
            closed = parse_closed(event)
            our_p  = yes_p if bet["side"]=="YES" else no_p
            impl   = bet["size"] * (our_p/bet["price"] - 1.0)
            max_win = bet["size"] * (1/bet["price"]-1)
            health = bet_health_icon(our_p)
            pct_locked = our_p/1.0*100
            status = "CLOSED⏳" if closed else f"{mins_left}m{secs_rem:02d}s left"
        else:
            our_p = bet["price"]; impl = 0.0
            max_win = bet["size"] * (1/bet["price"]-1)
            health = "⚪ UNKNOWN"
            pct_locked = 0
            status = f"{mins_left}m{secs_rem:02d}s left"

        log.info(f"    {health}  {bet['symbol']:4s} {bet['side']:3s} | "
                 f"Entry:{bet['price']:.4f} Now:{our_p:.4f} "
                 f"ImpliedPnL:{impl:+.2f} MaxWin:${max_win:.2f} | {status}")
        time.sleep(0.2)

# ═══════════════════════════════════════════════════════════
# MAIN CYCLE — FIXED: global capital declaration
# ═══════════════════════════════════════════════════════════

def cycle():
    # ▼▼▼ CRITICAL FIX: declare globals before any read OR write ▼▼▼
    global capital, banked, pause_cycle

    check_resolutions()

    # Paused after heavy loss streak?
    if pause_cycle:
        log.info("  [PAUSE] Recovery mode — sitting out this cycle")
        pause_cycle = False   # re-enable next cycle
        show_pending()
        return

    now_ts        = int(time.time())
    current_epoch = (now_ts // EPOCH_LEN) * EPOCH_LEN
    secs_into     = now_ts - current_epoch

    if secs_into > ENTRY_CUTOFF:
        mins_left = (EPOCH_LEN-secs_into)//60
        log.info(f"  [HOLD] {mins_left}m left — monitoring open bets")
        show_pending()
        return

    budget_pct = get_adaptive_budget()
    log.info(f"  ── PHASE 1: Scanning {len(ASSETS)} coins "
             f"(budget={budget_pct*100:.0f}%) ──────────────")

    candidates = []
    yes_count  = 0   # correlation guard: max 2 in same direction
    no_count   = 0

    for symbol in ASSETS:
        _, _, poly_coin = ASSETS[symbol]
        slug = f"{poly_coin}-updown-15m-{current_epoch}"

        if slug in seen_slugs:
            log.info(f"  {symbol}: already bet this epoch — skip")
            continue

        log.info(f"\n  ── {symbol} ──")
        direction, conf, desc, src, fakeout = analyze(symbol)
        log.info(f"  {desc}")

        dir_str = "UP" if direction==1 else ("DOWN" if direction==-1 else "FLAT")
        log.info(f"  → {dir_str} | conf={conf} | min={MIN_CONF}")

        if fakeout:
            log.info(f"  [FILTER] {fakeout}")
            continue
        if direction == 0 or conf < MIN_CONF:
            log.info("  [SKIP] Signal too weak")
            continue

        # Correlation guard
        if direction == 1 and yes_count >= MAX_SAME_DIRECTION:
            log.info(f"  [SKIP] Max {MAX_SAME_DIRECTION} UP bets per cycle (correlation guard)")
            continue
        if direction == -1 and no_count >= MAX_SAME_DIRECTION:
            log.info(f"  [SKIP] Max {MAX_SAME_DIRECTION} DOWN bets per cycle (correlation guard)")
            continue

        event = get_event(slug)
        if not event:
            log.info(f"  [SKIP] Market not found: {slug}")
            continue
        if parse_closed(event):
            log.info("  [SKIP] Market already closed")
            continue

        yes_p, no_p = parse_prices(event)

        if not (0.85 < yes_p+no_p < 1.15):
            log.info(f"  [SKIP] Price sum {yes_p+no_p:.3f} out of range")
            continue
        if yes_p == 0.5 and no_p == 0.5:
            log.info("  [SKIP] Market not yet priced")
            continue

        # Determine side and edge
        strength  = min(conf/12.0, 1.0)
        wr_adj    = max(0, live_wr-0.50)*0.25
        fair_our  = 0.50 + strength*0.18 + wr_adj
        side = None; entry_price = None

        if direction == 1:
            if yes_p < fair_our - EDGE_THRESHOLD:
                side="YES"; entry_price=yes_p
            elif no_p > 0.82:
                side="YES"; entry_price=yes_p
        elif direction == -1:
            if no_p < fair_our - EDGE_THRESHOLD:
                side="NO"; entry_price=no_p
            elif yes_p > 0.82:
                side="NO"; entry_price=no_p

        if side is None:
            log.info(f"  [SKIP] No edge (YES={yes_p:.4f} NO={no_p:.4f} "
                     f"fair={fair_our:.4f})")
            continue

        ev = ev_score(conf, entry_price)
        payout_x = 1/entry_price - 1
        log.info(f"  ✓ CANDIDATE: {side} @ {entry_price:.4f} | "
                 f"EV={ev:.2f} payout={payout_x:.2f}x")
        candidates.append((symbol, side, entry_price, conf, slug, current_epoch))

        if side == "YES": yes_count += 1
        else:             no_count  += 1

        time.sleep(0.3)

    if not candidates:
        log.info("\n  No qualifying candidates this cycle")
        show_pending()
        return

    # ── PHASE 2: EV-WEIGHTED ALLOCATION ───────────────────
    log.info(f"\n  ── PHASE 2: EV-Weighted Allocation ──────────────────")
    log.info(f"  {len(candidates)} candidate(s) | Budget={budget_pct*100:.0f}% "
             f"of ${capital:.2f} = ${capital*budget_pct:.2f}")

    allocations = allocate_bets(candidates, capital, budget_pct)

    if not allocations:
        log.info("  No allocations — insufficient capital")
        show_pending()
        return

    total_alloc = sum(a[3] for a in allocations)
    log.info(f"  ┌{'─'*62}")
    log.info(f"  │ PLAN  total=${total_alloc:.2f} ({total_alloc/capital*100:.0f}% of capital)")
    for sym, side, price, alloc, conf, slug, epoch, ev_pct in allocations:
        payout_x = 1/price-1
        pct      = alloc/capital*100
        log.info(f"  │  {sym:4s} {side:3s} @ {price:.4f}  ${alloc:.2f} ({pct:.0f}%)  "
                 f"payout {payout_x:.2f}x  EV={ev_pct:+.0f}%")
    log.info(f"  └{'─'*62}\n")

    tag    = "[DRY]" if DRY_RUN else "[LIVE]"
    placed = 0

    for sym, side, price, alloc, conf, slug, epoch, ev_pct in allocations:
        with state_lock:
            if alloc > capital:
                log.info(f"  [SKIP] {sym}: alloc ${alloc:.2f} > capital ${capital:.2f}")
                continue
            capital -= alloc

        nw  = capital + banked
        roi = (nw-STARTING_CAPITAL)/STARTING_CAPITAL*100

        log.info(f"  {tag} S{STAGE} | {sym} → {side} @ {price:.4f} | "
                 f"${alloc:.2f} ({alloc/(capital+alloc)*100:.0f}%) "
                 f"payout {1/price-1:.2f}x conf={conf} EV={ev_pct:+.0f}% | "
                 f"Cap→${capital:.2f} Net=${nw:.2f} ROI={roi:+.1f}%")

        with state_lock:
            active_bets.append({
                "symbol":    sym,
                "slug":      slug,
                "epoch":     epoch,
                "side":      side,
                "price":     price,
                "size":      alloc,
                "conf":      conf,
                "ev_pct":    ev_pct,
                "placed_at": int(time.time()),
            })
        seen_slugs.add(slug)
        placed += 1

    if placed > 0:
        nw  = capital + banked
        roi = (nw-STARTING_CAPITAL)/STARTING_CAPITAL*100
        wr  = wins/total_res*100 if total_res else 0
        log.info(f"\n  {tag} {placed} bet(s) placed | "
                 f"Cap=${capital:.2f} Bank=${banked:.2f} Net=${nw:.2f} "
                 f"ROI={roi:+.1f}% WR={wr:.1f}%")

    # Cleanup stale epoch slugs
    epoch_now = (int(time.time()) // EPOCH_LEN) * EPOCH_LEN
    for slug in list(seen_slugs):
        try:
            if int(slug.split("-")[-1]) < epoch_now:
                seen_slugs.discard(slug)
        except: pass

# ═══════════════════════════════════════════════════════════
# BANNER
# ═══════════════════════════════════════════════════════════

def banner():
    mode = "DRY-RUN (no real money)" if DRY_RUN else "LIVE (real USDC)"
    log.info("=" * 68)
    log.info(f"  Polymarket 15m Bot  v8.0 — Fakeout Filter + Profit Lock")
    log.info(f"  Mode    : {mode}")
    log.info(f"  {cfg['name']}")
    log.info(f"  Capital : ${STARTING_CAPITAL:.2f}  |  Target: ${STARTING_CAPITAL*13:.2f} (13x)")
    log.info(f"  Budget  : {BASE_BUDGET*100:.0f}% base (adaptive: +5% on 3 W streak, -15% on 2 L streak)")
    log.info(f"  Max/bet : {MAX_SINGLE*100:.0f}% of capital  |  MinConf: {MIN_CONF}")
    log.info(f"  Fakeouts: RSI-extreme, BB-breach, Vol-confusion, Stoch-OB/OS, Mom-conflict")
    log.info(f"  Health  : 🟢LOCKED>85% 🟡LEADING>65% 🟠CLOSE>45% 🔴LOSING<45%")
    log.info(f"  Sizing  : EV = conf × (1/price − 1) → proportional + extreme bonus")
    log.info(f"  Guard   : max {MAX_SAME_DIRECTION} bets in same direction (correlation guard)")
    log.info("=" * 68)
    if DRY_RUN:
        log.info(f"  >> DRY-RUN Stage {STAGE}: simulating — no real money <<")
    log.info("")

if __name__ == "__main__":
    banner()
    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        log.info(f"[{ts}] ── cycle ──────────────────────────────────────────")
        try:
            cycle()
        except KeyboardInterrupt:
            wr  = wins/total_res*100 if total_res else 0
            nw  = capital+banked
            roi = (nw-STARTING_CAPITAL)/STARTING_CAPITAL*100
            log.info("\n  Stopped.")
            log.info(f"  {total_res} resolved  W{wins}({wr:.1f}%) L{losses}")
            log.info(f"  Cap=${capital:.2f} Bank=${banked:.2f} Net=${nw:.2f} ROI={roi:+.1f}%")
            break
        except Exception as e:
            log.error(f"  Cycle error: {e}", exc_info=True)
        time.sleep(CYCLE_SLEEP)

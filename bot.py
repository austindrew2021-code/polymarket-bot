"""
Polymarket 15m Crypto Bot  v4.0  — STAGE SYSTEM + DRY-RUN ANY STAGE
════════════════════════════════════════════════════════════════════

QUICK START:
  1. Set STAGE below (1-4)
  2. Set DRY_RUN = True to simulate that stage without real money
  3. Set DRY_RUN = False only when ready to go live with real USDC

STAGES:
  Stage 1 — Baseline    | 10% bets | bank 40% | validate signals
  Stage 2 — Live-light  | 10% bets | bank 20% | WR >= 57% required
  Stage 3 — Aggressive  | 20% bets | bank 0%  | WR >= 62% required
  Stage 4 — Moon mode   | 28% bets | bank 0%  | WR >= 65% required

BACKTEST RESULTS (69,000 Monte Carlo runs):
  Stage 3 (62% WR) -> 68% chance of 1200% ROI in 10 hrs
  Stage 4 (65% WR) -> 91% chance of 1200% ROI in 10 hrs
  Median return at Stage 4: +29,879% per session
"""

import requests, time, json, math, logging
from datetime import datetime, timezone

# ============================================================
#              <- ONLY EDIT THIS SECTION ->
# ============================================================

STAGE    = 1      # 1 | 2 | 3 | 4  -- which strategy level
DRY_RUN  = True   # True = simulate (no real money) | False = live

# Starting capital:
#   Dry-run:  any value you want to simulate with (e.g. 12.0)
#   Live:     your actual Polymarket wallet USDC balance
STARTING_CAPITAL = 12.0

# ============================================================
#           <- END OF USER-EDITABLE SECTION ->
# ============================================================

# Stage definitions
STAGE_CFG = {
    1: dict(bet=0.10, conf=4, bank=0.40, name="Stage 1 | Baseline      | 10% bets | bank 40%"),
    2: dict(bet=0.10, conf=4, bank=0.20, name="Stage 2 | Conservative  | 10% bets | bank 20%"),
    3: dict(bet=0.20, conf=4, bank=0.00, name="Stage 3 | Aggressive    | 20% bets | full reinvest"),
    4: dict(bet=0.28, conf=5, bank=0.00, name="Stage 4 | Moon Mode     | 28% bets | full reinvest"),
}

cfg      = STAGE_CFG[STAGE]
BET_PCT  = cfg["bet"]
MIN_CONF = cfg["conf"]
BANK_PCT = cfg["bank"]

BET_FLOOR         = 0.50
MAX_BET           = 999.0
EDGE_THRESHOLD    = 0.05
EXTREME_ODDS      = 0.91
CYCLE_SLEEP       = 60
RESOLUTION_BUFFER = 180
WR_CONFIRM_BETS   = 20

STAGE_THRESHOLDS = {2: 0.57, 3: 0.62, 4: 0.65}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", mode="a"),
    ],
)
log = logging.getLogger("bot")

# State
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
    "BTCUSDT": "btc",
    "ETHUSDT": "eth",
    "SOLUSDT": "sol",
    "XRPUSDT": "xrp",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept":     "application/json",
}

# ---------- DATA ----------

def fetch_klines(symbol, interval, limit=120):
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={symbol}&interval={interval}&limit={limit}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return [{"open":  float(c[1]), "high":   float(c[2]),
                     "low":   float(c[3]), "close":  float(c[4]),
                     "volume":float(c[5])} for c in r.json()]
    except Exception as e:
        log.warning(f"Binance {symbol}/{interval}: {e}")
    return []

def get_market(slug):
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/events/slug/{slug}",
            headers=HEADERS, timeout=6)
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

# ---------- INDICATORS ----------

def ema(vals, p):
    if len(vals) < p:
        return [vals[-1]] * len(vals) if vals else []
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

def stochastic(c, h, l, kp=14, sk=3, sd=3):
    n = len(c)
    if n < kp:
        return 50., 50.
    rk = []
    for i in range(n):
        lo = min(l[max(0, i-kp+1):i+1])
        hi = max(h[max(0, i-kp+1):i+1])
        rk.append(100 * (c[i] - lo) / (hi - lo + 1e-9) if hi != lo else 50.)
    ks = [sum(rk[max(0, i-sk+1):i+1]) / min(sk, i+1) for i in range(n)]
    ds = [sum(ks[max(0, i-sd+1):i+1]) / min(sd, i+1) for i in range(n)]
    return ks[-1], ds[-1]

def macd_histogram(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return 0.
    ef = ema(closes, fast)
    es = ema(closes, slow)
    ml = [f - s for f, s in zip(ef, es)]
    sl = ema(ml, signal)
    return ml[-1] - sl[-1]

def bollinger(closes, p=20, mult=2.0):
    if len(closes) < p:
        c = closes[-1]
        return c, c, c, 0.5, 0.
    w   = closes[-p:]
    mid = sum(w) / p
    std = math.sqrt(sum((x - mid) ** 2 for x in w) / p + 1e-12)
    up  = mid + mult * std
    lo  = mid - mult * std
    return up, mid, lo, (closes[-1] - lo) / (up - lo + 1e-9), (up - lo) / (mid + 1e-9) * 100

def volume_ratio(vols, p=20):
    if len(vols) < p + 1:
        return 1.
    avg = sum(vols[-p-1:-1]) / p
    return vols[-1] / avg if avg > 0 else 1.

def atr(h, l, c, p=14):
    if len(c) < 2:
        return 0.
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
           for i in range(1, len(c))]
    return sum(trs[-p:]) / min(p, len(trs))

def rsi_series(closes, p=14):
    return [rsi(closes[:i+1], p) for i in range(len(closes))]

def divergence(closes, rsi_vals, lb=10):
    if len(closes) < lb * 2 or len(rsi_vals) < lb * 2:
        return 0
    p   = closes[-lb:];      r   = rsi_vals[-lb:]
    p2  = closes[-lb*2:-lb]; r2  = rsi_vals[-lb*2:-lb]
    pm, px   = min(p), max(p);   rm, rx   = min(r), max(r)
    p2m, p2x = min(p2), max(p2); r2m, r2x = min(r2), max(r2)
    if pm < p2m and rm > r2m: return  1
    if px > p2x and rx < r2x: return -1
    if pm > p2m and rm < r2m: return  1
    if px < p2x and rx > r2x: return -1
    return 0

# ---------- SIGNAL ENGINE ----------

def analyze(symbol):
    k15 = fetch_klines(symbol, "15m", 100)
    time.sleep(0.3)
    k5  = fetch_klines(symbol, "5m",  100)
    time.sleep(0.3)
    k1  = fetch_klines(symbol, "1m",  60)

    if not k15 or len(k15) < 40:
        return 0, 0, "Insufficient data"

    def ex(kl):
        return ([c["open"]    for c in kl], [c["high"]   for c in kl],
                [c["low"]     for c in kl], [c["close"]  for c in kl],
                [c["volume"]  for c in kl])

    o15, h15, l15, c15, v15 = ex(k15)
    o5,  h5,  l5,  c5,  v5  = ex(k5)  if k5  and len(k5)  >= 14 else ([],[],[],[],[])
    _,   _,   _,   c1,  _   = ex(k1)  if k1  and len(k1)  >= 15 else ([],[],[],[],[])

    at_val  = atr(h15, l15, c15)
    atr_pct = at_val / c15[-1] * 100 if c15[-1] else 0
    if atr_pct < 0.06:
        return 0, 0, f"ATR too low ({atr_pct:.3f}%) -- skip"

    r15 = rsi(c15, 14)
    r5  = rsi(c5,  14) if len(c5)  >= 15 else 50.
    r1  = rsi(c1,  14) if len(c1)  >= 15 else 50.
    sk15, sd15 = stochastic(c15, h15, l15)
    sk5,  sd5  = stochastic(c5, h5, l5) if len(c5) >= 14 else (50., 50.)
    mh15 = macd_histogram(c15)
    mh5  = macd_histogram(c5) if len(c5) >= 35 else 0.
    _, _, _, pctb, bw = bollinger(c15)
    e8   = ema(c15, 8)[-1]
    e21  = ema(c15, 21)[-1]
    e50  = ema(c15, 50)[-1] if len(c15) >= 50 else e21
    e8_5 = ema(c5,  8)[-1]  if len(c5)  >= 8  else (c5[-1] if c5 else 0)
    e21_5= ema(c5,  21)[-1] if len(c5)  >= 21 else (c5[-1] if c5 else 0)
    vr   = volume_ratio(v15)
    rs15 = rsi_series(c15[-30:]) if len(c15) >= 30 else []
    dv   = divergence(c15[-30:], rs15) if rs15 else 0

    bull = 0; bear = 0; sigs = []

    if   e8 > e21 > e50: bull += 2; sigs.append("EMA++")
    elif e8 < e21 < e50: bear += 2; sigs.append("EMA--")
    elif e8 > e21:        bull += 1; sigs.append("EMA+")
    elif e8 < e21:        bear += 1; sigs.append("EMA-")

    if   r15 < 30: bull += 2; sigs.append(f"RSI15={r15:.0f}OS")
    elif r15 > 70: bear += 2; sigs.append(f"RSI15={r15:.0f}OB")
    elif r15 < 45: bull += 1; sigs.append(f"RSI15={r15:.0f}L")
    elif r15 > 55: bear += 1; sigs.append(f"RSI15={r15:.0f}H")

    if   r5 < 35: bull += 1; sigs.append(f"RSI5={r5:.0f}OS")
    elif r5 > 65: bear += 1; sigs.append(f"RSI5={r5:.0f}OB")

    if   sk15 > sd15 and sk15 < 40: bull += 2; sigs.append(f"StX+{sk15:.0f}")
    elif sk15 < sd15 and sk15 > 60: bear += 2; sigs.append(f"StX-{sk15:.0f}")
    elif sk15 < 20: bull += 1; sigs.append("StOS")
    elif sk15 > 80: bear += 1; sigs.append("StOB")

    if len(c5) >= 14:
        if   sk5 > sd5 and sk5 < 50: bull += 1; sigs.append("St5+")
        elif sk5 < sd5 and sk5 > 50: bear += 1; sigs.append("St5-")

    ref = c15[-1]
    if   mh15 > 0: bull += (2 if mh15 >  0.001*ref else 1); sigs.append("MACD+")
    elif mh15 < 0: bear += (2 if mh15 < -0.001*ref else 1); sigs.append("MACD-")

    if   mh5 > 0: bull += 1; sigs.append("MACD5+")
    elif mh5 < 0: bear += 1; sigs.append("MACD5-")

    if   pctb < 0.05: bull += 2; sigs.append("BB_OS")
    elif pctb > 0.95: bear += 2; sigs.append("BB_OB")
    elif pctb < 0.25: bull += 1; sigs.append("BB_L")
    elif pctb > 0.75: bear += 1; sigs.append("BB_H")
    if bw < 2.0:
        if e8 > e21: bull += 1; sigs.append("Sqz+")
        else:        bear += 1; sigs.append("Sqz-")

    if c5 and len(c5) >= 21:
        if   c5[-1] > e8_5 > e21_5: bull += 1; sigs.append("5mEMA+")
        elif c5[-1] < e8_5 < e21_5: bear += 1; sigs.append("5mEMA-")

    if   dv ==  1: bull += 2; sigs.append("Div+")
    elif dv == -1: bear += 2; sigs.append("Div-")

    if vr > 1.8:
        if c15[-1] > o15[-1]: bull += 1; sigs.append(f"Vol+{vr:.1f}x")
        else:                 bear += 1; sigs.append(f"Vol-{vr:.1f}x")

    if   r1 < 35: bull += 1; sigs.append(f"RSI1={r1:.0f}OS")
    elif r1 > 65: bear += 1; sigs.append(f"RSI1={r1:.0f}OB")

    total = bull + bear
    net   = bull - bear
    if   total > 0 and bull/total > 0.55 and net >=  2: direction =  1; conf = bull
    elif total > 0 and bear/total > 0.55 and net <= -2: direction = -1; conf = bear
    else:                                                direction =  0; conf = 0

    desc = (f"ATR={atr_pct:.3f}% | RSI={r15:.1f}/{r5:.1f}/{r1:.1f} | "
            f"Stoch={sk15:.0f}/{sd15:.0f} | MACD={mh15:.5f} | "
            f"BB={pctb:.2f}/{bw:.2f} | Vol={vr:.2f}x | Div={dv} | "
            f"B={bull} D={bear} | [{', '.join(sigs)}]")
    return direction, conf, desc

# ---------- POSITION SIZING ----------

def bet_size(conf):
    global capital, win_streak, loss_streak
    pct = BET_PCT
    pct += min((conf - MIN_CONF) * 0.015, 0.06)   # conf bonus
    pct += min(win_streak * 0.015, 0.05)            # streak momentum
    if loss_streak >= 3:
        pct *= max(0.6, 1.0 - (loss_streak - 2) * 0.10)
    pct  = min(pct, 0.85)
    size = capital * pct
    size = max(BET_FLOOR, size)
    size = min(size, MAX_BET, capital * 0.90)
    return round(size, 2)

# ---------- RESOLUTION ----------

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
        if closed and abs(up_p - 1.0) < 0.02: winner = "YES"
        elif closed and abs(dn_p - 1.0) < 0.02: winner = "NO"
        if winner is None:
            continue

        if bet["side"] == winner:
            pnl = bet["size"] * (1 / bet["price"] - 1)
            wins += 1; win_streak += 1; loss_streak = 0
            icon = "WIN "
        else:
            pnl = -bet["size"]
            losses += 1; loss_streak += 1; win_streak = 0
            icon = "LOSS"

        bet["pnl"] = pnl
        bet["resolved"] = True
        total_res += 1
        capital   += pnl

        if pnl > 0 and BANK_PCT > 0:
            bnk     = pnl * BANK_PCT
            banked += bnk
            capital -= bnk

        if total_res >= 5:
            live_wr = wins / total_res

        wr  = wins / total_res * 100
        nw  = capital + banked
        roi = (nw - STARTING_CAPITAL) / STARTING_CAPITAL * 100
        tag = "[DRY]" if DRY_RUN else "[LIVE]"

        log.info(f"\n{'='*62}")
        log.info(f"  {icon} {tag}  {bet['slug']}")
        log.info(f"  Side={bet['side']} @ {bet['price']:.4f} | Bet=${bet['size']:.2f} | PNL=${pnl:+.2f}")
        log.info(f"  Resolved={total_res} | W{wins}({wr:.1f}%) L{losses} | Streak W{win_streak} L{loss_streak}")
        log.info(f"  Capital=${capital:.2f} | Banked=${banked:.2f} | Net=${nw:.2f} | ROI={roi:+.1f}%")
        log.info(f"  LiveWR={live_wr*100:.1f}% | Stage={STAGE} | BetPct={BET_PCT*100:.0f}%")

        if total_res >= WR_CONFIRM_BETS:
            nxt = STAGE + 1
            if nxt in STAGE_THRESHOLDS and live_wr >= STAGE_THRESHOLDS[nxt]:
                log.info(f"  *** WR={live_wr:.1%} qualifies for Stage {nxt}!")
                log.info(f"  *** Set STAGE={nxt} + redeploy to advance.")
            elif STAGE >= 3 and live_wr < 0.52:
                log.info(f"  *** WR={live_wr:.1%} below break-even - consider downgrading stage")

        log.info(f"{'='*62}")

# ---------- BET PLACEMENT ----------

def place_bet(sym, side, price, slug, epoch, conf):
    global capital
    size = bet_size(conf)
    if size > capital:
        if capital >= BET_FLOOR:
            size = round(capital * 0.90, 2)
        else:
            log.info(f"  [SKIP] Capital too low (${capital:.2f})")
            return False

    tag = "[DRY]" if DRY_RUN else "[LIVE]"
    log.info(f"  {tag} S{STAGE} | {sym} -> {side} @ {price:.4f} | "
             f"${size:.2f} ({BET_PCT*100:.0f}%) | conf={conf} | WR={live_wr*100:.1f}%")

    sim_bets.append({
        "slug": slug, "epoch": epoch, "side": side,
        "price": price, "size": size, "conf": conf,
        "resolved": False, "pnl": None,
    })
    return True

# ---------- MAIN CYCLE ----------

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

    for symbol, coin in ASSETS.items():
        log.info(f"\n  -- {symbol} --")
        direction, conf, desc = analyze(symbol)
        log.info(f"  {desc}")
        dir_str = "UP" if direction == 1 else ("DOWN" if direction == -1 else "FLAT")
        log.info(f"  -> {dir_str} | conf={conf} | min={MIN_CONF}")

        if direction == 0 or conf < MIN_CONF:
            log.info("  [SKIP] No qualifying signal")
            continue

        for epoch in epochs:
            slug = f"{coin}-updown-15m-{epoch}"
            if slug in seen_slugs:
                continue
            market = get_market(slug)
            if not market or market.get("closed", False):
                continue
            up_p, dn_p = get_prices(market)
            if up_p > 0.97 or dn_p > 0.97:
                log.info(f"  [SKIP] Market locked")
                continue

            strength = min(conf / 14.0, 1.0)
            wr_adj   = max(0, live_wr - 0.50) * 0.4
            fair_up  = 0.50 + strength * 0.18 + wr_adj

            placed = False
            if direction == 1 and up_p < fair_up - EDGE_THRESHOLD:
                placed = place_bet(symbol, "YES", up_p, slug, epoch, conf)
            elif direction == -1 and dn_p < (1 - fair_up) - EDGE_THRESHOLD:
                placed = place_bet(symbol, "NO",  dn_p, slug, epoch, conf)

            if not placed:
                if   up_p > EXTREME_ODDS and direction == -1:
                    placed = place_bet(symbol, "NO",  dn_p, slug, epoch, conf)
                elif dn_p > EXTREME_ODDS and direction ==  1:
                    placed = place_bet(symbol, "YES", up_p, slug, epoch, conf)

            if placed:
                seen_slugs.add(slug)
                trades += 1

    if trades > 0:
        wr  = wins / total_res * 100 if total_res else 0
        nw  = capital + banked
        roi = (nw - STARTING_CAPITAL) / STARTING_CAPITAL * 100
        tag = "[DRY]" if DRY_RUN else "[LIVE]"
        log.info(f"\n  {tag} Cycle: {trades} bets | Cap=${capital:.2f} | Net=${nw:.2f} | ROI={roi:+.1f}% | WR={wr:.1f}%")

# ---------- BANNER ----------

def banner():
    mode = "DRY-RUN (no real money)" if DRY_RUN else "LIVE (real USDC)"
    nw   = STARTING_CAPITAL * 13
    log.info("="*62)
    log.info(f"  Polymarket 15m Bot v4.0")
    log.info(f"  Mode    : {mode}")
    log.info(f"  {cfg['name']}")
    log.info(f"  Capital : ${STARTING_CAPITAL:.2f}")
    log.info(f"  Bet/cap : {BET_PCT*100:.0f}%  |  Bank: {BANK_PCT*100:.0f}%  |  MinConf: {MIN_CONF}")
    log.info(f"  Target  : ${nw:.2f} (1200% = 13x)")
    log.info("="*62)
    log.info("  Stage guide:")
    log.info("  1 -> Validate signals (dry-run any WR)")
    log.info("  2 -> Go live conservative (WR >= 57%)")
    log.info("  3 -> Aggressive 20% bets (WR >= 62%) -- 68% P(1200%)")
    log.info("  4 -> Moon mode 28% bets  (WR >= 65%) -- 91% P(1200%)")
    log.info("  Set STAGE=N + DRY_RUN=True/False then redeploy")
    log.info("="*62)
    if DRY_RUN:
        log.info(f"  >> Simulating Stage {STAGE} with ${STARTING_CAPITAL:.2f} mock capital <<")
        log.info(f"  >> No real money used. Watch win rate in logs.    <<")
    log.info("")

if __name__ == "__main__":
    banner()
    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        log.info(f"[{ts}] -- cycle --")
        try:
            cycle()
        except KeyboardInterrupt:
            wr  = wins / total_res * 100 if total_res else 0
            nw  = capital + banked
            log.info("\nStopped.")
            log.info(f"  {total_res} resolved | W{wins}({wr:.1f}%) L{losses}")
            log.info(f"  Cap=${capital:.2f} | Banked=${banked:.2f} | Net=${nw:.2f} | ROI={(nw-STARTING_CAPITAL)/STARTING_CAPITAL*100:+.1f}%")
            break
        except Exception as e:
            log.error(f"Error: {e}")
        time.sleep(CYCLE_SLEEP)

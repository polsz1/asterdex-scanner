#!/usr/bin/env python3
"""
generate_report.py v2
Czyta aster_history.csv i generuje docs/index.html z:
  - Pelna tabela tokenow z ostatniego runu
  - TOP 5 LONG / SHORT picks z historii
  - Cross-check Lighter.xyz
  - Auto-odswiezanie co 60 sekund
  - Responsywny design (telefon + desktop)
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

HISTORY_CSV        = "aster_history.csv"
LIGHTER_CACHE_FILE = "lighter_markets_cache.json"
OUTPUT_DIR         = "docs"
OUTPUT_HTML        = os.path.join(OUTPUT_DIR, "index.html")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def safe_float(val, default=0.0):
    try:
        v = str(val).strip()
        if v.startswith("Decimal("): v = v[9:-2]
        return float(v) if v else default
    except Exception: return default

def safe_int(val, default=0):
    try:
        v = str(val).strip()
        if v.startswith("Decimal("): v = v[9:-2]
        return int(float(v)) if v else default
    except Exception: return default


def load_all_runs(path):
    if not os.path.exists(path): return [], [], ""
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows: return [], [], ""
    last_ts = rows[-1]["run_timestamp"]
    return rows, [r for r in rows if r["run_timestamp"] == last_ts], last_ts

def load_lighter():
    if not os.path.exists(LIGHTER_CACHE_FILE): return {}
    try:
        with open(LIGHTER_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f).get("markets", {})
    except Exception: return {}

def base(sym): return sym.removesuffix("USDT").removesuffix("BUSD").removesuffix("USDC").upper()

def linear_slope(vals):
    n = len(vals)
    if n < 2: return 0.0
    xs = list(range(n)); mx = sum(xs)/n; my = sum(vals)/n
    num = sum((xs[i]-mx)*(vals[i]-my) for i in range(n))
    den = sum((x-mx)**2 for x in xs)
    return num/den if den else 0.0

def pct(v, d=2):
    c = "#4ade80" if v >= 0 else "#f87171"
    return f'<span style="color:{c}">{v:+.{d}f}%</span>'

def spike_s(v):
    if v >= 3:    c,w = "#f87171","bold"
    elif v >= 2:  c,w = "#facc15","bold"
    elif v >= 1.5:c,w = "#67e8f9","normal"
    else:         c,w = "#64748b","normal"
    return f'<span style="color:{c};font-weight:{w}">{v:.1f}x</span>'

def rsi_s(v):
    c = "#f87171" if v>=70 else "#4ade80" if v<=30 else "#94a3b8"
    return f'<span style="color:{c}">{v:.0f}</span>'

def score_s(v):
    c = "#f59e0b" if v>=150 else "#facc15" if v>=80 else "#fb923c" if v>=40 else "#94a3b8"
    return f'<span style="color:{c};font-weight:bold">{v:.0f}</span>'

def prob_bar(pct_v, color):
    p = max(0,min(100,pct_v))
    bc = "#4ade80" if color=="bull" else "#f87171"
    return (f'<div class="bw"><div class="bf" style="width:{p}%;background:{bc}"></div></div>'
            f'<span class="bp">{p}%</span>')

def setup_b(s):
    if s=="LONG":  return '<span class="badge long">LONG</span>'
    if s=="SHORT": return '<span class="badge short">SHORT</span>'
    return '<span class="badge neu">—</span>'

def corr_s(v):
    c = "#4ade80" if v<0.3 else "#facc15" if v<0.6 else "#64748b"
    return f'<span style="color:{c}">{v:.2f}</span>'

def oi_s(v, has):
    if not has: return '<span style="color:#334155">—</span>'
    c = ("#f87171" if v>0 else "#c084fc") if abs(v)>=10 else "#facc15" if abs(v)>=5 else "#64748b"
    return f'<span style="color:{c}">{v:+.1f}%</span>'

def div_s(row):
    p = []
    if safe_int(row.get("div_bull")):          p.append('<span class="db">BULL-DIV</span>')
    if safe_int(row.get("div_squeeze_bull")):   p.append('<span class="db">SQ-BULL</span>')
    if safe_int(row.get("div_bear")):           p.append('<span class="dr">BEAR-DIV</span>')
    if safe_int(row.get("div_squeeze_bear")):   p.append('<span class="dr">SQ-BEAR</span>')
    return " ".join(p) or '<span style="color:#334155">—</span>'

def trend_s(v):
    if v>5:   return f'<span style="color:#4ade80;font-weight:bold">▲{v:+.0f}</span>'
    if v>0:   return f'<span style="color:#4ade80">▲{v:+.0f}</span>'
    if v<-5:  return f'<span style="color:#f87171;font-weight:bold">▼{v:.0f}</span>'
    if v<0:   return f'<span style="color:#f87171">▼{v:.0f}</span>'
    return '<span style="color:#334155">new</span>'


def compute_picks(all_rows, n_runs):
    sd = defaultdict(list)
    for row in all_rows:
        sym = row.get("symbol","")
        if not sym: continue
        b = safe_int(row.get("bull_prob"),-1)
        e = safe_int(row.get("bear_prob"),-1)
        sd[sym].append({
            "score": safe_float(row.get("score")),
            "bull_prob": b, "bear_prob": e,
            "rsi": safe_float(row.get("rsi_14"),-1),
            "setup": row.get("setup","NEUTRAL"),
            "spike": safe_float(row.get("volume_spike_ratio")),
            "btc_corr": safe_float(row.get("btc_corr"),1),
            "div_bull": safe_int(row.get("div_bull")),
            "div_bear": safe_int(row.get("div_bear")),
            "div_sq_bull": safe_int(row.get("div_squeeze_bull")),
            "div_sq_bear": safe_int(row.get("div_squeeze_bear")),
        })
    ranked = []
    for sym, data in sd.items():
        cnt = len(data)
        scores = [d["score"] for d in data]
        spk = [d["spike"] for d in data if 0 < d["spike"] <= 500]
        avg_spk = min(sum(spk)/len(spk) if spk else 1.0, 20.0)
        vb = [d["bull_prob"] for d in data if d["bull_prob"]>=0 and d["bear_prob"]>=0 and not (d["bull_prob"]==0 and d["bear_prob"]==0)]
        ve = [d["bear_prob"] for d in data if d["bull_prob"]>=0 and d["bear_prob"]>=0 and not (d["bull_prob"]==0 and d["bear_prob"]==0)]
        ab = sum(vb)/len(vb) if vb else 50.0
        ae = sum(ve)/len(ve) if ve else 50.0
        ac = sum(d["btc_corr"] for d in data)/cnt
        cons = cnt/n_runs*100
        sl = linear_slope(scores)
        sn = min(max(sl,0),50)
        bc = ab - ae; ec = ae - ab
        dbc = sum(d["div_bull"]+d["div_sq_bull"] for d in data)
        dec = sum(d["div_bear"]+d["div_sq_bear"] for d in data)
        lr = 0.0 if ab<=ae else cons*0.5+max(bc,0)*0.6+sn*0.4+(1-min(ac,1))*15+avg_spk*1.5+dbc*5
        sr = 0.0 if ae<=ab else cons*0.5+max(ec,0)*0.6+sn*0.4+(1-min(ac,1))*15+avg_spk*1.5+dec*5
        ranked.append({"sym":sym,"cnt":cnt,"cons":cons,"ab":ab,"ae":ae,"spk":avg_spk,"ac":ac,"sl":sl,"lr":lr,"sr":sr,"last":data[-1],"dbc":dbc,"dec":dec})
    lp = sorted([r for r in ranked if r["lr"]>0], key=lambda x:x["lr"], reverse=True)[:5]
    sp = sorted([r for r in ranked if r["sr"]>0], key=lambda x:x["sr"], reverse=True)[:5]
    return lp, sp


def pick_card(r, mode, idx):
    cls = "long" if mode=="long" else "short"
    rel = r["lr"] if mode=="long" else r["sr"]
    ab=r["ab"]; ae=r["ae"]; sl=r["sl"]
    last = r["last"]
    lr = max(0.0,min(100.0,last["rsi"])); rv = 5<=lr<=95
    reasons = []
    if r["cons"]>=70: reasons.append(f'był w TOP w {r["cons"]:.0f}% runów')
    if mode=="long" and ab>=65: reasons.append(f'avg bull% {ab:.0f}% &gt;&gt; bear% {ae:.0f}%')
    if mode=="short" and ae>=50: reasons.append(f'avg bear% {ae:.0f}% &gt;&gt; bull% {ab:.0f}%')
    if sl>3: reasons.append(f'score rośnie (slope {sl:+.1f})')
    if r["ac"]<0.25: reasons.append(f'niezależny od BTC (corr {r["ac"]:.2f})')
    if 1.5<=r["spk"]<=20: reasons.append(f'avg vol spike {r["spk"]:.1f}x')
    if rv and mode=="long"  and lr<40: reasons.append(f'RSI {lr:.0f} — wyprzedany')
    if rv and mode=="short" and lr>65: reasons.append(f'RSI {lr:.0f} — wykupiony')
    if last["setup"]=="LONG"  and mode=="long":  reasons.append("ostatni setup: LONG")
    if last["setup"]=="SHORT" and mode=="short": reasons.append("ostatni setup: SHORT")
    if not reasons: reasons.append(f'consistency {r["cons"]:.0f}%')
    ri = "".join(f"<li>{x}</li>" for x in reasons)
    return f"""<div class="pick-card {cls}">
      <div class="ph"><span class="pr">#{idx}</span><span class="ps">{r["sym"]}</span><span class="prel">rel {rel:.0f}</span></div>
      <div class="pb-row"><span class="pbl">🐂</span>{prob_bar(int(ab),"bull")}<span class="pbl ml">🐻</span>{prob_bar(int(ae),"bear")}</div>
      <ul class="prlist">{ri}</ul>
    </div>"""


def build_tokens(last_rows, lm):
    out = ""
    for row in last_rows:
        sym=row.get("symbol",""); b=base(sym); ol=b in lm; m=lm.get(b,{})
        pc_v=safe_float(row.get("price_change_pct_24h"))
        p4_v=safe_float(row.get("price_4h_change_pct"))
        sp_v=safe_float(row.get("volume_spike_ratio"))
        oi_v=safe_float(row.get("oi_change_pct"))
        rs_v=safe_float(row.get("rsi_14"),50)
        fr_v=safe_float(row.get("funding_rate_pct"))
        st_v=safe_float(row.get("score_trend"))
        cr_v=safe_float(row.get("btc_corr"),1)
        sc_v=safe_float(row.get("score"))
        vm_v=safe_float(row.get("volume_24h_usd"))/1_000_000
        bp_v=safe_int(row.get("bull_prob"))
        ep_v=safe_int(row.get("bear_prob"))
        pr_v=safe_float(row.get("last_price"))
        has_oi=row.get("oi_change_pct","") not in ("","0","0.0")
        mi = m.get("market_index","")
        lt = f'<span class="lt" title="Lighter #{mi}">L</span>' if ol else ""
        frc = "#f87171" if fr_v>0.1 else "#4ade80" if fr_v<-0.05 else "#64748b"
        out += f"""<tr>
<td class="dim">{row.get("rank","")}</td>
<td class="sym">{sym}{lt}</td>
<td>{setup_b(row.get("setup",""))}</td>
<td class="num">{pr_v:.6g}</td>
<td>{pct(pc_v)}</td><td>{pct(p4_v)}</td>
<td>{spike_s(sp_v)}</td>
<td>{oi_s(oi_v,has_oi)}</td>
<td>{rsi_s(rs_v)}</td>
<td><span style="color:{frc}">{fr_v:+.3f}%</span></td>
<td class="dim">{vm_v:.3f}</td>
<td>{score_s(sc_v)}</td>
<td>{trend_s(st_v)}</td>
<td class="bc">{prob_bar(bp_v,"bull")}</td>
<td class="bc">{prob_bar(ep_v,"bear")}</td>
<td>{div_s(row)}</td>
<td>{corr_s(cr_v)}</td>
</tr>\n"""
    return out or '<tr><td colspan="17" class="dim" style="text-align:center;padding:20px">Brak danych</td></tr>'


def build_lighter_tbl(last_rows, lm):
    if not lm: return '<p class="dim">Lighter.xyz: brak cache</p>'
    found = [(r,lm[base(r["symbol"])]) for r in last_rows if base(r["symbol"]) in lm]
    if not found: return '<p class="dim">Żaden token z TOP nie jest na Lighter.xyz</p>'
    rows = ""
    for row,m in found:
        rows += f"""<tr>
<td class="sym">{row.get("symbol","")}</td>
<td class="dim">{m["symbol"]}</td><td class="dim">{m["market_index"]}</td>
<td>{setup_b(row.get("setup",""))}</td>
<td>{score_s(safe_float(row.get("score")))}</td>
<td class="bc">{prob_bar(safe_int(row.get("bull_prob")),"bull")}</td>
<td class="bc">{prob_bar(safe_int(row.get("bear_prob")),"bear")}</td>
<td>{div_s(row)}</td>
</tr>\n"""
    nf = [r["symbol"] for r in last_rows if base(r["symbol"]) not in lm]
    nf_html = f'<p class="dim" style="font-size:11px;margin-top:6px">Tylko AsterDex: {" · ".join(nf)}</p>' if nf else ""
    return f"""<div class="tw"><table class="data-table"><thead><tr>
<th>AsterDex</th><th>Lighter</th><th>Mkt#</th><th>Setup</th>
<th>Score</th><th>🐂 Bull</th><th>🐻 Bear</th><th>Div</th>
</tr></thead><tbody>{rows}</tbody></table>{nf_html}</div>"""


def generate(all_rows, last_rows, last_ts, lm):
    n_runs = len(set(r["run_timestamp"] for r in all_rows)) if all_rows else 0
    try: ts = datetime.fromisoformat(last_ts).strftime("%Y-%m-%d %H:%M UTC")
    except: ts = last_ts[:19] if last_ts else "—"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lp, sp = compute_picks(all_rows, n_runs) if all_rows and n_runs>0 else ([],[])
    nt = len(last_rows)
    longs  = sum(1 for r in last_rows if r.get("setup")=="LONG")
    shorts = sum(1 for r in last_rows if r.get("setup")=="SHORT")
    ab_avg = int(sum(safe_int(r.get("bull_prob")) for r in last_rows)/nt) if last_rows else 0

    warn = f'<p class="few-runs">⚠ Tylko {n_runs} runów w historii — picks dokładniejsze po ≥10 runach</p>' if 0<n_runs<10 else ""

    long_picks_html = warn + ("".join(pick_card(r,"long",i+1) for i,r in enumerate(lp)) if lp else '<p class="dim">Brak kandydatów LONG</p>')
    short_picks_html = ("".join(pick_card(r,"short",i+1) for i,r in enumerate(sp)) if sp else '<p class="dim">Brak kandydatów SHORT</p>')

    CSS = """
:root{--bg:#0b1120;--bg2:#0f172a;--bg3:#1e293b;--bd:#1e2d40;--tx:#e2e8f0;--dim:#64748b;--cy:#67e8f9;--gr:#4ade80;--rd:#f87171;--yw:#facc15}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Courier New',monospace;font-size:13px}
a{color:var(--cy);text-decoration:none}
.header{background:var(--bg2);border-bottom:1px solid var(--bd);padding:14px 20px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{font-size:17px;font-weight:bold;color:var(--cy);letter-spacing:2px;white-space:nowrap}
.logo span{color:var(--yw)}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.pill{background:var(--bg3);border:1px solid var(--bd);border-radius:20px;padding:2px 9px;font-size:11px;color:var(--dim)}
.pill b{color:var(--tx)}
.pill.g b{color:var(--gr)}.pill.r b{color:var(--rd)}.pill.y b{color:var(--yw)}
.container{max-width:1600px;margin:0 auto;padding:16px}
.st{font-size:11px;color:var(--dim);letter-spacing:1px;text-transform:uppercase;margin:20px 0 8px;padding-bottom:5px;border-bottom:1px solid var(--bd)}
.tw{overflow-x:auto;border-radius:8px;border:1px solid var(--bd)}
.data-table{border-collapse:collapse;width:100%;min-width:860px}
.data-table th{background:var(--bg3);color:var(--dim);padding:7px 9px;text-align:left;font-weight:normal;font-size:11px;white-space:nowrap;border-bottom:1px solid var(--bd)}
.data-table td{padding:6px 9px;border-bottom:1px solid #0f1929;white-space:nowrap;vertical-align:middle}
.data-table tr:last-child td{border-bottom:none}
.data-table tr:hover td{background:#111d30}
.sym{font-weight:bold;color:var(--tx)}.num{color:var(--tx)}.dim{color:var(--dim)}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:bold}
.badge.long{background:#052e16;color:var(--gr);border:1px solid #166534}
.badge.short{background:#2d0a0a;color:var(--rd);border:1px solid #7f1d1d}
.badge.neu{background:transparent;color:var(--dim);border:1px solid var(--bd)}
.bc{min-width:105px}
.bw{display:inline-block;width:65px;height:7px;background:var(--bg3);border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:4px}
.bf{height:100%;border-radius:4px}
.bp{color:var(--dim);font-size:11px;vertical-align:middle}
.db{color:var(--gr);font-size:11px}.dr{color:var(--rd);font-size:11px}
.lt{display:inline-block;background:#0c1f3f;color:#38bdf8;border:1px solid #1e40af;border-radius:3px;padding:0 3px;font-size:10px;vertical-align:middle;margin-left:3px}
.picks-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:8px}
@media(max-width:700px){.picks-grid{grid-template-columns:1fr}}
.pt{font-size:13px;font-weight:bold;margin-bottom:10px;padding:7px 11px;border-radius:6px}
.pt.l{background:#052e16;color:var(--gr);border:1px solid #166534}
.pt.s{background:#2d0a0a;color:var(--rd);border:1px solid #7f1d1d}
.pick-card{background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:11px 13px;margin-bottom:9px}
.pick-card.long{border-left:3px solid var(--gr)}.pick-card.short{border-left:3px solid var(--rd)}
.ph{display:flex;align-items:baseline;gap:7px;margin-bottom:7px}
.pr{color:var(--dim);font-size:12px}.ps{font-weight:bold;font-size:14px;color:var(--tx)}.prel{color:var(--yw);font-size:11px;margin-left:auto}
.pb-row{display:flex;align-items:center;gap:4px;margin-bottom:7px;flex-wrap:wrap}
.pbl{color:var(--dim);font-size:12px}.ml{margin-left:10px}
.prlist{list-style:none;padding:0}
.prlist li{color:var(--dim);font-size:11px;padding:1px 0}
.prlist li::before{content:"→ ";color:var(--bd)}
.few-runs{color:var(--yw);font-size:11px;margin-bottom:9px;padding:5px 9px;background:#2d2500;border-radius:4px;border:1px solid #854d0e}
.legend{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:9px 13px;margin-top:7px;font-size:11px;color:var(--dim);line-height:2;display:flex;flex-wrap:wrap;gap:4px 18px}
.legend b{color:var(--tx)}
.footer{margin-top:28px;padding:14px;border-top:1px solid var(--bd);color:var(--dim);font-size:11px;text-align:center;line-height:2}
.rb{height:2px;background:linear-gradient(90deg,var(--cy),transparent);animation:sh 60s linear infinite;transform-origin:left}
@keyframes sh{from{transform:scaleX(1)}to{transform:scaleX(0)}}
"""

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AsterDex Scanner</title>
<meta http-equiv="refresh" content="60">
<style>{CSS}</style>
</head>
<body>
<div class="rb"></div>
<header class="header">
  <div class="logo">⚡ ASTER<span>DEX</span> SCANNER</div>
  <div class="pills">
    <div class="pill">Run: <b>{ts}</b></div>
    <div class="pill">Strona: <b>{now}</b></div>
    <div class="pill y">Runów: <b>{n_runs}</b></div>
    <div class="pill">Tokenów: <b>{nt}</b></div>
    <div class="pill g">LONG: <b>{longs}</b></div>
    <div class="pill r">SHORT: <b>{shorts}</b></div>
    <div class="pill">avg bull%: <b>{ab_avg}%</b></div>
  </div>
</header>
<div class="container">
  <div class="st">📊 Ostatni skan</div>
  <div class="tw"><table class="data-table">
    <thead><tr><th>#</th><th>Symbol</th><th>Setup</th><th>Price</th>
    <th>24h%</th><th>4h%</th><th>Spike</th><th>OI Δ%</th>
    <th>RSI</th><th>Fund%</th><th>Vol$M</th>
    <th>Score</th><th>Δsc</th><th>🐂 Bull</th><th>🐻 Bear</th>
    <th>Div</th><th>BTCcorr</th></tr></thead>
    <tbody>{build_tokens(last_rows, lm)}</tbody>
  </table></div>
  <div class="legend">
    <span><b>Score</b>=aktywność</span>
    <span><b>L</b>=dostępny na Lighter.xyz</span>
    <span><b>BULL-DIV</b> cena↓+OI↑+RSI↑=akumulacja</span>
    <span><b>SQ-BULL</b> funding&lt;-0.05%=short squeeze</span>
    <span><b>BEAR-DIV</b> cena↑+OI↓+RSI↓=dystrybucja</span>
    <span><b>SQ-BEAR</b> funding&gt;0.15%=long squeeze</span>
    <span><b>BTC corr</b> 🟢&lt;0.3 niezależny 🟡0.3-0.6 szary&gt;0.6</span>
    <span><b>bull/bear%</b>=heurystyczny scoring, NIE statystyczne prawd.</span>
  </div>

  <div class="st">🎯 Picks z historii</div>
  <div class="picks-grid">
    <div>
      <div class="pt l">🐂 TOP 5 KANDYDATÓW DO LONGA</div>
      {long_picks_html}
    </div>
    <div>
      <div class="pt s">🐻 TOP 5 KANDYDATÓW DO SHORTA</div>
      {short_picks_html}
    </div>
  </div>

  <div class="st">🔗 Cross-check Lighter.xyz ({len(lm)} rynków)</div>
  {build_lighter_tbl(last_rows, lm)}
</div>
<footer class="footer">
  <div>Dane z AsterDex · Odświeżane co godzinę przez GitHub Actions · Auto-refresh 60s</div>
  <div style="color:#1e293b">NIE jest to rekomendacja inwestycyjna. Handel kryptowalutami wiąże się z ryzykiem straty.</div>
</footer>
</body></html>"""


def main():
    all_rows, last_rows, last_ts = load_all_runs(HISTORY_CSV)
    lm = load_lighter()
    html = generate(all_rows, last_rows, last_ts, lm)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    n_runs = len(set(r["run_timestamp"] for r in all_rows)) if all_rows else 0
    print(f"✓ Raport HTML → {OUTPUT_HTML} ({len(last_rows)} tokenów, {n_runs} runów, {len(lm)} Lighter)")

if __name__ == "__main__":
    main()

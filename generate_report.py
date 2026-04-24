#!/usr/bin/env python3
"""
generate_report.py
Czyta ostatni run z aster_history.csv i generuje docs/index.html
Strona jest serwowana przez GitHub Pages z repo.
"""

import csv
import os
from datetime import datetime, timezone

HISTORY_CSV = "aster_history.csv"
OUTPUT_DIR  = "docs"
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "index.html")

os.makedirs(OUTPUT_DIR, exist_ok=True)


def load_last_run(path: str) -> tuple[list[dict], str]:
    """Wczytaj wiersze z ostatniego runu."""
    if not os.path.exists(path):
        return [], ""
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    if not rows:
        return [], ""
    last_ts = rows[-1]["run_timestamp"]
    return [r for r in rows if r["run_timestamp"] == last_ts], last_ts


def safe_float(val, default=0.0) -> float:
    try:
        v = str(val).strip()
        if v.startswith("Decimal("):
            v = v[9:-2]
        return float(v) if v else default
    except Exception:
        return default


def fmt_pct(val: str, decimals=2) -> str:
    v = safe_float(val)
    color = "#4ade80" if v >= 0 else "#f87171"
    return f'<span style="color:{color}">{v:+.{decimals}f}%</span>'


def fmt_score(val: str) -> str:
    v = safe_float(val)
    if v >= 100: color = "#facc15"
    elif v >= 50: color = "#fb923c"
    else: color = "#e2e8f0"
    return f'<span style="color:{color};font-weight:bold">{v:.0f}</span>'


def fmt_spike(val: str) -> str:
    v = safe_float(val)
    if v >= 3:   color = "#f87171"; weight = "bold"
    elif v >= 2: color = "#facc15"; weight = "bold"
    elif v >= 1.5: color = "#67e8f9"; weight = "normal"
    else:        color = "#94a3b8"; weight = "normal"
    return f'<span style="color:{color};font-weight:{weight}">{v:.1f}x</span>'


def fmt_rsi(val: str) -> str:
    v = safe_float(val, 50)
    if v >= 70:   color = "#f87171"
    elif v <= 30: color = "#4ade80"
    else:         color = "#94a3b8"
    return f'<span style="color:{color}">{v:.0f}</span>'


def fmt_prob_bar(pct_str: str, color: str) -> str:
    pct = int(safe_float(pct_str, 0))
    pct = max(0, min(100, pct))
    width = pct
    bg = "#1e293b"
    bar_color = "#4ade80" if color == "bull" else "#f87171"
    return (
        f'<div style="display:flex;align-items:center;gap:6px">'
        f'<div style="width:80px;height:10px;background:{bg};border-radius:4px;overflow:hidden">'
        f'<div style="width:{width}%;height:100%;background:{bar_color};border-radius:4px"></div>'
        f'</div>'
        f'<span style="color:#94a3b8;font-size:11px">{pct}%</span>'
        f'</div>'
    )


def fmt_setup(val: str) -> str:
    if val == "LONG":
        return '<span style="color:#4ade80;font-weight:bold">LONG</span>'
    elif val == "SHORT":
        return '<span style="color:#f87171;font-weight:bold">SHORT</span>'
    return '<span style="color:#475569">—</span>'


def fmt_corr(val: str) -> str:
    v = safe_float(val, 1)
    if v < 0.3:   color = "#4ade80"
    elif v < 0.6: color = "#facc15"
    else:         color = "#94a3b8"
    return f'<span style="color:{color}">{v:.2f}</span>'


def fmt_div(val_bull, val_bear, val_sq_bull, val_sq_bear) -> str:
    labels = []
    if int(safe_float(val_bull)):    labels.append('<span style="color:#4ade80">BULL-DIV</span>')
    if int(safe_float(val_sq_bull)): labels.append('<span style="color:#4ade80">SQ-BULL</span>')
    if int(safe_float(val_bear)):    labels.append('<span style="color:#f87171">BEAR-DIV</span>')
    if int(safe_float(val_sq_bear)): labels.append('<span style="color:#f87171">SQ-BEAR</span>')
    return " ".join(labels) if labels else '<span style="color:#334155">—</span>'


def fmt_oi(val: str, has_delta: bool) -> str:
    if not has_delta:
        return '<span style="color:#334155">—</span>'
    v = safe_float(val)
    if abs(v) >= 10:   color = "#f87171" if v > 0 else "#c084fc"
    elif abs(v) >= 5:  color = "#facc15"
    else:              color = "#94a3b8"
    return f'<span style="color:{color}">{v:+.1f}%</span>'


def generate_html(rows: list[dict], last_ts: str, n_runs: int) -> str:
    try:
        dt = datetime.fromisoformat(last_ts)
        ts_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        ts_display = last_ts[:19]

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Nagłówki tabeli
    header_cells = [
        "#", "Symbol", "Setup", "Price", "24h%", "4h%",
        "Spike", "OI Δ%", "RSI", "Fund%", "Vol$M",
        "Score", "Δscore", "🐂 Bull", "🐻 Bear", "Div", "BTC corr"
    ]
    headers_html = "".join(f"<th>{h}</th>" for h in header_cells)

    rows_html = ""
    for row in rows:
        rank = row.get("rank","")
        sym  = row.get("symbol","")
        has_delta = row.get("oi_change_pct","") not in ("", "0", "0.0")

        # Score trend
        sc_trend = safe_float(row.get("score_trend","0"))
        if sc_trend > 5:    sc_t = f'<span style="color:#4ade80;font-weight:bold">▲{sc_trend:+.0f}</span>'
        elif sc_trend > 0:  sc_t = f'<span style="color:#4ade80">▲{sc_trend:+.0f}</span>'
        elif sc_trend < -5: sc_t = f'<span style="color:#f87171;font-weight:bold">▼{sc_trend:.0f}</span>'
        elif sc_trend < 0:  sc_t = f'<span style="color:#f87171">▼{sc_trend:.0f}</span>'
        else:               sc_t = '<span style="color:#475569">new</span>'

        vol_m = safe_float(row.get("volume_24h_usd","0")) / 1_000_000

        cells = [
            f'<td style="color:#475569">{rank}</td>',
            f'<td style="font-weight:bold;color:#e2e8f0">{sym}</td>',
            f'<td>{fmt_setup(row.get("setup",""))}</td>',
            f'<td style="color:#e2e8f0">{safe_float(row.get("last_price","0")):.6g}</td>',
            f'<td>{fmt_pct(row.get("price_change_pct_24h","0"))}</td>',
            f'<td>{fmt_pct(row.get("price_4h_change_pct","0"))}</td>',
            f'<td>{fmt_spike(row.get("volume_spike_ratio","0"))}</td>',
            f'<td>{fmt_oi(row.get("oi_change_pct","0"), has_delta)}</td>',
            f'<td>{fmt_rsi(row.get("rsi_14","50"))}</td>',
            f'<td style="color:#94a3b8">{safe_float(row.get("funding_rate_pct","0")):+.3f}%</td>',
            f'<td style="color:#475569">{vol_m:.3f}</td>',
            f'<td>{fmt_score(row.get("score","0"))}</td>',
            f'<td>{sc_t}</td>',
            f'<td>{fmt_prob_bar(row.get("bull_prob","0"), "bull")}</td>',
            f'<td>{fmt_prob_bar(row.get("bear_prob","0"), "bear")}</td>',
            f'<td>{fmt_div(row.get("div_bull","0"), row.get("div_bear","0"), row.get("div_squeeze_bull","0"), row.get("div_squeeze_bear","0"))}</td>',
            f'<td>{fmt_corr(row.get("btc_corr","1"))}</td>',
        ]
        rows_html += f"<tr>{''.join(cells)}</tr>\n"

    html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AsterDex Scanner</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0f172a;
    color: #e2e8f0;
    font-family: 'Courier New', monospace;
    font-size: 13px;
    padding: 20px;
  }}
  h1 {{
    color: #67e8f9;
    font-size: 20px;
    margin-bottom: 4px;
    letter-spacing: 2px;
  }}
  .meta {{
    color: #475569;
    font-size: 11px;
    margin-bottom: 16px;
  }}
  .meta span {{ color: #94a3b8; }}
  .legend {{
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 16px;
    font-size: 11px;
    color: #64748b;
    line-height: 1.8;
  }}
  .legend b {{ color: #94a3b8; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    min-width: 1100px;
  }}
  th {{
    background: #1e293b;
    color: #64748b;
    padding: 8px 10px;
    text-align: left;
    font-weight: normal;
    border-bottom: 1px solid #334155;
    white-space: nowrap;
  }}
  td {{
    padding: 7px 10px;
    border-bottom: 1px solid #1e293b;
    white-space: nowrap;
  }}
  tr:hover td {{ background: #1e293b; }}
  .footer {{
    margin-top: 20px;
    color: #334155;
    font-size: 11px;
  }}
  .badge {{
    display: inline-block;
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 4px;
    padding: 2px 8px;
    margin-right: 8px;
    color: #94a3b8;
  }}
</style>
</head>
<body>
<h1>⚡ ASTERDEX VOLATILITY SCANNER</h1>
<div class="meta">
  Ostatni run: <span>{ts_display}</span> &nbsp;|&nbsp;
  Strona odświeżona: <span>{now_utc}</span> &nbsp;|&nbsp;
  Runów w historii: <span>{n_runs}</span> &nbsp;|&nbsp;
  Tokenów w tym runie: <span>{len(rows)}</span>
</div>

<div class="legend">
  <b>Score</b> = aktywność tokena (vol spike + OI + cena + RSI + funding) &nbsp;|&nbsp;
  <b>🐂 Bull% / 🐻 Bear%</b> = heurystyczny scoring kierunkowy (NIE statystyczne prawdopodobieństwo) &nbsp;|&nbsp;
  <b>BTC corr</b> = korelacja z BTC (zielony = niezależny) &nbsp;|&nbsp;
  <b>BULL-DIV</b> = cena↓ + OI↑ + RSI↑ (akumulacja) &nbsp;|&nbsp;
  <b>SQ-BULL</b> = funding &lt; -0.05% (short squeeze risk) &nbsp;|&nbsp;
  <b>BEAR-DIV</b> = cena↑ + OI↓ + RSI↓ (dystrybucja)
</div>

<div class="table-wrap">
<table>
  <thead><tr>{headers_html}</tr></thead>
  <tbody>
{rows_html}
  </tbody>
</table>
</div>

<div class="footer">
  <p>Dane z AsterDex (fapi.asterdex.com) &nbsp;|&nbsp; Odświeżane automatycznie co godzinę przez GitHub Actions</p>
  <p style="margin-top:6px;color:#1e293b">NIE jest to rekomendacja inwestycyjna. Handel kryptowalutami wiąże się z ryzykiem straty kapitału.</p>
</div>
</body>
</html>"""
    return html


def main():
    rows, last_ts = load_last_run(HISTORY_CSV)
    if not rows:
        print(f"Brak danych w {HISTORY_CSV} — generuję pustą stronę.")
        rows = []
        last_ts = "brak danych"

    # Policz liczbę unikalnych runów
    all_rows = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, encoding="utf-8") as f:
            all_rows = list(csv.DictReader(f))
    n_runs = len(set(r["run_timestamp"] for r in all_rows))

    html = generate_html(rows, last_ts, n_runs)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✓ Raport HTML → {OUTPUT_HTML} ({len(rows)} tokenów, {n_runs} runów w historii)")


if __name__ == "__main__":
    main()

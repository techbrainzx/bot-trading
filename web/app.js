"use strict";

/* ============================================================ utilidades */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TZ = -new Date().getTimezoneOffset() * 60; // gráficos em hora local

const S = {
  status: null, clockOffset: 0, symbol: null, tf: "1h", tab: "decisions",
  logsAfter: 0, lastDecisionSig: "", settings: null, liveCheck: null, busy: {},
};

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "X-Token": window.API_TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* sem corpo */ }
  if (!res.ok) throw new Error((data && data.detail) || `Erro ${res.status}`);
  return data;
}

const nfCache = {};
const nf = (d) => (nfCache[d] ||= new Intl.NumberFormat("pt-PT", { minimumFractionDigits: d, maximumFractionDigits: d }));
const sign = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
const fmtMoney = (v, d = 2) => (v == null || isNaN(v) ? "—" : (v < 0 ? "−" : "") + nf(d).format(Math.abs(v)));
const fmtSigned = (v, d = 2) => (v == null || isNaN(v) ? "—" : sign(v) + nf(d).format(Math.abs(v)));
const fmtPct = (v, d = 2) => (v == null || isNaN(v) ? "—" : sign(v) + nf(d).format(Math.abs(v)) + "%");
const priceDec = (p) => { p = Math.abs(p || 0); return p >= 100 ? 2 : p >= 1 ? 4 : p >= 0.01 ? 5 : 7; };
const fmtPrice = (p) => (p == null ? "—" : nf(priceDec(p)).format(p));
const fmtQty = (q) => (q == null ? "—" : nf(q >= 100 ? 2 : q >= 1 ? 4 : 6).format(q));
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (id, extra = "") => `<svg class="i ${extra}"><use href="#i-${id}"/></svg>`;
const base = (sym) => (sym || "").split("/")[0];

function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "agora mesmo";
  if (s < 3600) return `há ${Math.floor(s / 60)} min`;
  if (s < 86400) return `há ${Math.floor(s / 3600)} h`;
  return `há ${Math.floor(s / 86400)} d`;
}
const fmtTime = (iso) => new Date(iso).toLocaleString("pt-PT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
function fmtDuration(h) {
  if (h == null) return "—";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${nf(1).format(h)} h`;
  return `${nf(1).format(h / 24)} d`;
}
const REASONS = { stop_loss: "Stop-loss", trailing_stop: "Trailing stop", take_profit: "Take-profit", sinal_ia: "Sinal da IA", manual: "Manual" };
const ACTION_PT = { BUY: "COMPRAR", SELL: "VENDER", HOLD: "ESPERAR", BUY_LIMIT: "COMPRA NO RECUO", BUY_STOP: "COMPRA NO ROMPIMENTO", SKIP: "SEM SETUP" };
const PENDING_PT = { BUY_LIMIT: "Comprar no recuo", BUY_STOP: "Comprar no rompimento" };
const PROFILE_PT = { conservador: "Conservador", equilibrado: "Equilibrado", agressivo: "Agressivo", personalizado: "Personalizado" };
const TREND_PT = { strong_up: "alta forte", up: "alta", sideways: "lateral", down: "baixa", strong_down: "baixa forte" };
const Q = () => S.status?.quote || "USDC";
const isBusy = (a) => /analis|pesquis|radar|notícias|recolher|gestora|escolher|ler not/i.test(a || "");

function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

/* ============================================================ toasts & confirmação */
function toast(msg, kind = "info", ms = 4500) {
  const ic = kind === "ok" ? "check" : kind === "err" ? "alert" : "zap";
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `${icon(ic)}<div>${esc(msg)}</div>`;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}

function confirmDialog({ title, text, ok = "Confirmar", danger = false }) {
  return new Promise((resolve) => {
    const m = $("#confirmModal");
    $("#confirmTitle").textContent = title;
    $("#confirmText").innerHTML = text;
    const okBtn = $("#confirmOk");
    okBtn.textContent = ok;
    okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    m.hidden = false;
    okBtn.focus();
    const done = (v) => { m.hidden = true; okBtn.onclick = null; $("#confirmCancel").onclick = null; resolve(v); };
    okBtn.onclick = () => done(true);
    $("#confirmCancel").onclick = () => done(false);
    m.onclick = (e) => { if (e.target === m) done(false); };
  });
}

async function withBusy(btn, fn) {
  if (!btn || btn.disabled) return;
  const html = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>${btn.textContent.trim()}`;
  try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = html; }
}

/* ============================================================ tema */
function getTheme() { try { return localStorage.getItem("theme") || "dark"; } catch { return "dark"; } }
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch { /* sem storage */ }
  $("#btnTheme").innerHTML = icon(t === "dark" ? "sun" : "moon");
  applyChartTheme();
}

/* ============================================================ gráficos */
let priceChart, candleSeries, volSeries, equityChart, equitySeries;
let priceLines = [];
let lastCandles = [];

function chartOptions() {
  return {
    autoSize: true,
    layout: { background: { type: "solid", color: cssVar("--surface-1") }, textColor: cssVar("--text-muted"), fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: cssVar("--grid") }, horzLines: { color: cssVar("--grid") } },
    rightPriceScale: { borderColor: cssVar("--border") },
    timeScale: { borderColor: cssVar("--border"), timeVisible: true, secondsVisible: false, rightOffset: 4 },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: cssVar("--border-strong"), labelBackgroundColor: cssVar("--surface-3") },
      horzLine: { color: cssVar("--border-strong"), labelBackgroundColor: cssVar("--surface-3") },
    },
    localization: { locale: "pt-PT", priceFormatter: (p) => fmtPrice(p) },
  };
}

function initCharts() {
  if (!window.LightweightCharts) {
    $("#priceChart").innerHTML = `<div class="chart-empty">Não foi possível carregar os gráficos (sem internet?).</div>`;
    return;
  }
  priceChart = LightweightCharts.createChart($("#priceChart"), chartOptions());
  candleSeries = priceChart.addCandlestickSeries({ borderVisible: false });
  volSeries = priceChart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  priceChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.2 } });
  priceChart.subscribeCrosshairMove((p) => renderLegend(p && p.time ? p.seriesData.get(candleSeries) : null));

  equityChart = LightweightCharts.createChart($("#equityChart"), {
    ...chartOptions(),
    localization: { locale: "pt-PT", priceFormatter: (p) => fmtMoney(p) },
  });
  equitySeries = equityChart.addBaselineSeries({ baseValue: { type: "price", price: 0 }, lineWidth: 2, priceLineVisible: false });
  equityChart.subscribeCrosshairMove((p) => {
    const d = p && p.time ? p.seriesData.get(equitySeries) : null;
    renderEquityLegend(d);
  });
  applyChartTheme();
}

function applyChartTheme() {
  if (!priceChart) return;
  priceChart.applyOptions(chartOptions());
  equityChart.applyOptions({ ...chartOptions(), localization: { locale: "pt-PT", priceFormatter: (p) => fmtMoney(p) } });
  const up = cssVar("--up"), down = cssVar("--down");
  candleSeries.applyOptions({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down });
  equitySeries.applyOptions({
    topLineColor: up, topFillColor1: hexA(up, 0.28), topFillColor2: hexA(up, 0.02),
    bottomLineColor: down, bottomFillColor1: hexA(down, 0.02), bottomFillColor2: hexA(down, 0.28),
  });
  if (lastCandles.length) setVolume(lastCandles);
}

function hexA(hex, a) {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function setVolume(candles) {
  const up = hexA(cssVar("--up"), 0.35), down = hexA(cssVar("--down"), 0.35);
  volSeries.setData(candles.map((c) => ({ time: c.time + TZ, value: c.volume, color: c.close >= c.open ? up : down })));
}

async function refreshCandles(reset = false) {
  if (!candleSeries || !S.symbol) return;
  const sym = S.symbol, tf = S.tf;
  try {
    const d = await api(`/api/candles?symbol=${encodeURIComponent(sym)}&tf=${tf}`);
    if (sym !== S.symbol || tf !== S.tf) return;
    lastCandles = d.candles;
    const dec = priceDec(d.candles.at(-1)?.close);
    candleSeries.applyOptions({ priceFormat: { type: "price", precision: dec, minMove: 1 / 10 ** dec } });
    candleSeries.setData(d.candles.map((c) => ({ time: c.time + TZ, open: c.open, high: c.high, low: c.low, close: c.close })));
    setVolume(d.candles);
    const up = cssVar("--up"), down = cssVar("--down");
    candleSeries.setMarkers(d.markers.map((m) => ({
      time: m.time + TZ,
      position: m.kind === "buy" ? "belowBar" : "aboveBar",
      shape: m.kind === "buy" ? "arrowUp" : "arrowDown",
      color: m.kind === "buy" ? cssVar("--accent") : (m.pnl >= 0 ? up : down),
      text: m.kind === "buy" ? "Compra" : `Venda ${fmtSigned(m.pnl)}`,
    })));
    priceLines.forEach((l) => candleSeries.removePriceLine(l));
    priceLines = [];
    if (d.lines) {
      const L = LightweightCharts.LineStyle;
      if (d.lines.entry) priceLines.push(candleSeries.createPriceLine({ price: d.lines.entry, color: cssVar("--accent"), lineWidth: 1, lineStyle: L.Solid, title: "Entrada" }));
      if (d.lines.pending) priceLines.push(candleSeries.createPriceLine({ price: d.lines.pending, color: cssVar("--accent"), lineWidth: 2, lineStyle: L.Dotted, title: d.lines.pending_type === "BUY_LIMIT" ? "Compra (recuo)" : "Compra (rompimento)" }));
      priceLines.push(candleSeries.createPriceLine({ price: d.lines.stop, color: down, lineWidth: 1, lineStyle: L.Dashed, title: "Stop" }));
      if (d.lines.take_profit) priceLines.push(candleSeries.createPriceLine({ price: d.lines.take_profit, color: up, lineWidth: 1, lineStyle: L.Dashed, title: "Alvo" }));
    }
    if (reset) priceChart.timeScale().fitContent();
    renderLegend(null);
  } catch (e) {
    console.warn(e);
  }
}

function renderLegend(bar) {
  const c = bar || (lastCandles.length ? { ...lastCandles.at(-1) } : null);
  if (!c) { $("#chartLegend").innerHTML = ""; return; }
  const ch = c.open ? (c.close / c.open - 1) * 100 : 0;
  $("#chartLegend").innerHTML =
    `<span class="lg-title">${esc(S.symbol)} · ${S.tf}</span>` +
    `<span>A <b>${fmtPrice(c.open)}</b></span><span>M <b>${fmtPrice(c.high)}</b></span>` +
    `<span>m <b>${fmtPrice(c.low)}</b></span><span>F <b>${fmtPrice(c.close)}</b></span>` +
    `<span class="${cls(ch)}">${fmtPct(ch)}</span>`;
}

/* ============================================================ estado principal */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    S.clockOffset = s.server_ms - Date.now();
    S.status = s;
    renderStatus(s);
  } catch (e) {
    const pill = $("#statusPill");
    pill.className = "status-pill offline";
    $("#statusText").textContent = "Sem ligação ao bot";
  }
}

function renderStatus(s) {
  document.body.dataset.mode = s.mode_key;
  $$(".mode-btn").forEach((b) => {
    const on = b.dataset.mode === s.mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on);
  });
  $$(".q").forEach((el) => (el.textContent = s.quote));

  // estado + botões (só redesenha quando muda, para não "comer" cliques)
  const pill = $("#statusPill");
  pill.className = "status-pill" + (s.error ? " error" : s.running ? " running" : "");
  $("#statusText").textContent = s.error ? "Erro" : s.running ? "A correr" : "Parado";
  pill.title = s.error || s.activity || "";
  const btn = $("#btnStartStop");
  const btnKey = `${s.running}|${s.mode}`;
  if (!btn.disabled && btn.dataset.key !== btnKey) {
    btn.dataset.key = btnKey;
    btn.className = s.running ? "btn btn-stop" : s.mode === "live" ? "btn btn-danger" : "btn btn-primary";
    btn.innerHTML = s.running ? `${icon("stop", "i-fill")}<span>Parar bot</span>` : `${icon("play", "i-fill")}<span>Iniciar bot</span>`;
  }
  const act = $("#activity");
  act.textContent = s.running ? s.activity : "Bot parado";
  act.classList.toggle("busy", s.running && isBusy(s.activity));

  // banner
  const risk = s.risk_mode === "fixed" ? `risco ${fmtMoney(s.risk_fixed)} ${s.quote} por trade` : `risco ${nf(2).format(s.risk_pct)}% por trade`;
  const radar = s.scanner.enabled ? `radar ${s.scanner.universe} moedas` : "radar desligado";
  const extra = ` <span class="sep">·</span> perfil ${PROFILE_PT[s.profile] || s.profile} <span class="sep">·</span> ${radar} <span class="sep">·</span> ${risk}`;
  const banner = $("#modeBanner");
  let html;
  if (s.mode_key === "paper") {
    html = `${icon("flask")}<span><b>MODO TESTE</b> <span class="sep">·</span> dinheiro fictício com preços reais${extra}</span>`;
  } else if (s.mode_key === "testnet") {
    html = `${icon("shield")}<span><b>MODO REAL · TESTNET</b> <span class="sep">·</span> dinheiro fictício na Binance Testnet <span class="sep">·</span> capital ${fmtMoney(s.capital_limit, 0)} ${s.quote}${extra}</span>`;
  } else {
    const bal = s.exchange_usdt != null ? ` <span class="sep">·</span> saldo livre ${fmtMoney(s.exchange_usdt)} ${s.quote}` : "";
    html = `${icon("alert")}<span><b>MODO REAL</b> <span class="sep">·</span> DINHEIRO REAL <span class="sep">·</span> capital máximo ${fmtMoney(s.capital_limit, 0)} ${s.quote}${bal}${extra}</span>`;
  }
  if (banner.dataset.html !== html) { banner.dataset.html = html; banner.innerHTML = html; }

  // alertas
  const alert = $("#alertBox");
  const alertKey = s.halted ? `h|${s.halt_reason}` : s.error ? `e|${s.error}` : "";
  if (alert.dataset.key !== alertKey) {
    alert.dataset.key = alertKey;
    if (s.halted) {
      alert.hidden = false;
      alert.innerHTML = `${icon("alert")}<span><b>Novas compras bloqueadas:</b> ${esc(s.halt_reason)}.</span><button class="btn btn-sm btn-secondary" id="btnResume">Desbloquear</button>`;
      $("#btnResume").onclick = resumeTrading;
    } else if (s.error) {
      alert.hidden = false;
      alert.innerHTML = `${icon("alert")}<span>${esc(s.error)}</span>`;
    } else {
      alert.hidden = true;
    }
  }

  // KPIs
  $("#kEquity").innerHTML = `${fmtMoney(s.equity)}<small>${s.quote}</small>`;
  $("#kEquitySub").textContent = `Livre ${fmtMoney(s.cash)} · inicial ${fmtMoney(s.start_cash, 0)}`;
  $("#kPnl").innerHTML = `<span class="${cls(s.pnl)}">${fmtSigned(s.pnl)}</span><small>${s.quote}</small>`;
  $("#kPnlSub").innerHTML = `<span class="${cls(s.pnl_pct)}">${fmtPct(s.pnl_pct)}</span> desde o início`;
  $("#kToday").innerHTML = `<span class="${cls(s.day_pnl_pct)}">${fmtPct(s.day_pnl_pct)}</span>`;
  $("#kTodaySub").textContent = "desde as 00:00 UTC";
  $("#kDD").innerHTML = `<span class="${s.drawdown_pct < -0.005 ? "down" : ""}">${fmtPct(s.drawdown_pct)}</span>`;
  $("#kDDSub").textContent = `bloqueio a −${nf(0).format(s.max_drawdown_pct)}%`;
  const st = s.stats;
  $("#kTrades").textContent = st.count;
  $("#kTradesSub").textContent = st.count
    ? `${nf(0).format(st.win_rate)}% acerto · fator lucro ${st.profit_factor == null ? "∞" : nf(2).format(st.profit_factor)}`
    : "ainda sem trades";
  renderCountdown();

  renderFocus(s);
  renderSymbolTabs(s);
  renderPositions(s);
  renderPending(s);
  renderAI(s);

  const u = Object.entries(s.usage_today || {});
  $("#usage").textContent = u.length
    ? "OpenAI hoje: " + u.map(([m, v]) => `${m} ${v.calls} chamadas (${nf(0).format((v.input + v.output) / 1000)}k tokens)`).join(" · ")
    : "OpenAI hoje: sem chamadas";

  const sig = Object.values(s.decisions).map((d) => d && d.time).join("|") + s.stats.count + "|" + s.pending.length + "|" + s.positions.length;
  if (sig !== S.lastDecisionSig) {
    const first = !S.lastDecisionSig;
    S.lastDecisionSig = sig;
    if (!first) {
      refreshCandles();
      if (S.tab === "decisions") refreshDecisions();
      if (S.tab === "trades") refreshTrades();
      if (S.tab === "radar") refreshRadar();
    }
  }
}

const REGIME_PT = { risk_on: "Mercado favorável", neutral: "Mercado misto", risk_off: "Mercado desfavorável" };
const STANCE_PT = { aggressive: "ofensiva", normal: "normal", defensive: "defensiva" };

function renderFocus(s) {
  const f = s.focus, r = s.regime;
  const key = JSON.stringify([f && f.time, r && r.state, s.auto_select, s.running]);
  const box = $("#focusBox");
  if (box.dataset.key === key) {
    const ago = $(".focus-foot .ago", box);
    if (ago && f) ago.textContent = `Atualizado ${timeAgo(f.time)}`;
    return;
  }
  box.dataset.key = key;
  $("#regimeBadge").innerHTML = r ? `<span class="regime ${r.state}" title="${r.breadth_above_ema50_pct}% das moedas acima da média de 50 no timeframe maior">${REGIME_PT[r.state]}</span>` : "";
  const intro = s.auto_select
    ? `Modo automático: em cada análise o bot examina as ${s.scanner.universe} moedas mais negociadas e escolhe sozinho as melhores ${s.scanner.top} para a IA analisar a fundo.`
    : "Modo manual: a IA analisa os teus pares fixos e as melhores do radar.";
  if (!f) {
    box.innerHTML = `<div class="focus-view">${esc(intro)}</div><div class="focus-foot">Ainda sem análise: inicia o bot ou clica em "Analisar agora".</div>`;
    return;
  }
  const metrics = r ? `<div class="focus-metrics"><span><b>${r.breadth_up_pct}%</b> das moedas a subir</span>
      <span>BTC: <b>${TREND_PT[r.btc_trend] || "—"}</b></span><span>média 24h <b class="${cls(r.avg_change_24h_pct)}">${fmtPct(r.avg_change_24h_pct)}</b></span></div>` : "";
  const view = f.view ? `<div class="focus-view">${esc(f.view)}${f.stance ? ` <span class="muted">(postura ${STANCE_PT[f.stance] || f.stance})</span>` : ""}</div>` : "";
  const picks = f.picks.length
    ? f.picks.map((p, i) => `<div class="pick" data-go="${esc(p.symbol)}">
        <span class="n">${i + 1}</span>
        <div><div class="name">${esc(base(p.symbol))}${p.setup ? `<span>${esc(p.setup)}</span>` : ""}</div><div class="why">${esc(p.reason)}</div></div>
        <span class="sc">${p.score ?? ""}</span></div>`).join("")
    : `<div class="empty" style="padding:10px"><b>Nenhuma oportunidade boa agora</b>O bot espera pela próxima análise sem gastar IA nas outras moedas.</div>`;
  const avoid = f.avoid && f.avoid.length
    ? `<div class="focus-avoid">Evitadas: ${f.avoid.map((a) => `<b>${esc(base(a.symbol))}</b> (${esc(a.reason)})`).join("; ")}</div>` : "";
  const src = f.source === "ia" ? "escolha da IA gestora" : f.source === "radar" ? "escolha pelo radar" : "pares fixos";
  box.innerHTML = metrics + view + picks + avoid + `<div class="focus-foot"><span class="ago">Atualizado ${timeAgo(f.time)}</span> · ${src}</div>`;
  $$("[data-go]", box).forEach((el) => (el.onclick = () => selectSymbol(el.dataset.go)));
}

function renderCountdown() {
  const s = S.status;
  if (!s) return;
  const el = $("#kNext"), sub = $("#kNextSub");
  sub.textContent = `a cada ${s.timeframe} · ${PROFILE_PT[s.profile] || s.profile}`;
  if (!s.running) { el.innerHTML = `<span class="muted">parado</span>`; return; }
  if (isBusy(s.activity)) { el.innerHTML = `<span style="color:var(--accent)">a analisar…</span>`; return; }
  if (!s.next_decision_ms) { el.textContent = "—"; return; }
  const left = Math.max(0, s.next_decision_ms - (Date.now() + S.clockOffset));
  const m = Math.floor(left / 60000), sec = Math.floor((left % 60000) / 1000);
  el.textContent = left > 3600000 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}:${String(sec).padStart(2, "0")}`;
}

function selectSymbol(sym) {
  S.symbol = sym;
  if (S.status) renderSymbolTabs(S.status);
  refreshCandles(true);
}

function renderSymbolTabs(s) {
  const syms = [...(s.tab_symbols || s.symbols)];
  if (S.symbol && !syms.includes(S.symbol)) syms.push(S.symbol);
  if (!S.symbol) { S.symbol = syms[0]; refreshCandles(true); }
  const host = $("#symbolTabs");
  if (host.dataset.sig !== syms.join()) {
    host.dataset.sig = syms.join();
    host.innerHTML = syms.map((x) => `<button class="sym-tab" data-sym="${esc(x)}"><span class="sym"></span><span class="px"></span></button>`).join("");
    $$(".sym-tab", host).forEach((b) => (b.onclick = () => selectSymbol(b.dataset.sym)));
  }
  const open = new Set(s.positions.map((p) => p.symbol));
  const pend = new Set(s.pending.map((p) => p.symbol));
  $$(".sym-tab", host).forEach((b) => {
    const x = b.dataset.sym, t = s.tickers[x] || {};
    b.classList.toggle("active", x === S.symbol);
    const dot = open.has(x) ? '<span class="pos-dot" title="Posição aberta"></span>' : pend.has(x) ? '<span class="pos-dot" style="background:transparent;border:1.5px solid var(--accent)" title="Ordem pendente"></span>' : "";
    const symHtml = `${esc(base(x))}${dot}`;
    if ($(".sym", b).innerHTML !== symHtml) $(".sym", b).innerHTML = symHtml;
    $(".px", b).innerHTML = t.last != null ? `${fmtPrice(t.last)} <span class="${cls(t.change_pct)}">${fmtPct(t.change_pct)}</span>` : "—";
  });
}

function positionRange(p) {
  const lo = Math.min(p.stop, p.price), hi = Math.max(p.take_profit || p.price, p.price);
  const span = hi - lo || 1;
  const at = (v) => Math.min(100, Math.max(0, ((v - lo) / span) * 100));
  return { e: at(p.entry_price), c: at(p.price) };
}

function renderPositions(s) {
  $("#posCount").textContent = `${s.positions.length}/${s.max_positions}`;
  $("#btnCloseAll").hidden = s.positions.length < 2;
  const host = $("#positions");
  const key = s.positions.map((p) => `${p.symbol}|${p.stop}|${p.take_profit}`).join(",") + s.quote;
  if (host.dataset.key !== key) {
    host.dataset.key = key;
    if (!s.positions.length) {
      host.innerHTML = `<div class="empty"><b>Sem posições abertas</b>O bot compra quando a IA encontra uma oportunidade que passa as regras de risco.</div>`;
      return;
    }
    host.innerHTML = s.positions.map((p) => `<div class="position" data-sym="${esc(p.symbol)}">
      <div class="position-top"><span class="position-sym">${esc(p.symbol)}</span><span class="position-pnl"></span></div>
      <div class="position-meta"><span class="pm-left"></span><span class="pm-r"></span></div>
      <div class="range" title="Posição do preço entre o stop e o alvo">
        <div class="range-fill"></div><span class="range-mark" title="Entrada"></span><span class="range-cur" title="Preço atual"></span>
      </div>
      <div class="range-labels"><span class="stop">Stop ${fmtPrice(p.stop)}</span><span>Entrada ${fmtPrice(p.entry_price)}</span><span class="tp">Alvo ${fmtPrice(p.take_profit)}</span></div>
      <div class="position-actions"><span class="pm-age"></span>
        <button class="btn btn-sm btn-danger-ghost" data-close="${esc(p.symbol)}">Fechar</button></div>
    </div>`).join("");
    $$("[data-close]", host).forEach((b) => (b.onclick = () => closePosition(b.dataset.close, b)));
  }
  for (const p of s.positions) {
    const el = host.querySelector(`.position[data-sym="${CSS.escape(p.symbol)}"]`);
    if (!el) continue;
    const { e, c } = positionRange(p);
    const pnl = $(".position-pnl", el);
    pnl.className = `position-pnl ${cls(p.pnl)}`;
    pnl.innerHTML = `${fmtSigned(p.pnl)} ${s.quote}<small>${fmtPct(p.pnl_pct)}</small>`;
    $(".pm-left", el).textContent = `${fmtQty(p.qty)} ${base(p.symbol)} · ${fmtMoney(p.value)} ${s.quote}`;
    $(".pm-r", el).textContent = p.r != null ? fmtSigned(p.r) + "R" : "";
    const fill = $(".range-fill", el);
    fill.className = `range-fill ${p.price >= p.entry_price ? "up" : "down"}`;
    fill.style.left = `${Math.min(e, c)}%`; fill.style.width = `${Math.abs(c - e)}%`;
    $(".range-mark", el).style.left = `${e}%`;
    $(".range-cur", el).style.left = `${c}%`;
    $(".pm-age", el).textContent = `Aberta ${timeAgo(p.opened_at)}`;
  }
}

function renderPending(s) {
  const box = $("#pendingBox"), host = $("#pendingList");
  box.hidden = !s.pending.length;
  $("#pendCount").textContent = s.pending.length ? `(${s.pending.length})` : "";
  const key = s.pending.map((o) => `${o.symbol}|${o.trigger}|${o.expires}`).join(",");
  if (host.dataset.key !== key) {
    host.dataset.key = key;
    host.innerHTML = s.pending.map((o) => `<div class="pending" data-sym="${esc(o.symbol)}">
      <div class="pending-top"><span><b>${esc(o.symbol)}</b> <span class="badge ${o.type}">${PENDING_PT[o.type] || o.type}</span></span>
        <button class="btn btn-sm btn-secondary" data-cancel="${esc(o.symbol)}">Cancelar</button></div>
      <div class="pending-meta"><span>Disparo <b>${fmtPrice(o.trigger)}</b></span><span class="pd-dist"></span>
        <span>Stop ${fmtPrice(o.stop)}</span><span>Alvo ${fmtPrice(o.take_profit)}</span>
        <span>${icon("clock")} até ${esc(fmtTime(o.expires))}</span></div>
    </div>`).join("");
    $$("[data-cancel]", host).forEach((b) => (b.onclick = () => cancelPending(b.dataset.cancel, b)));
  }
  for (const o of s.pending) {
    const el = host.querySelector(`.pending[data-sym="${CSS.escape(o.symbol)}"] .pd-dist`);
    if (el && o.price) el.textContent = `atual ${fmtPrice(o.price)} (${fmtPct((o.trigger / o.price - 1) * 100)})`;
  }
}

function renderAI(s) {
  const host = $("#aiList");
  const items = Object.entries(s.decisions);
  const key = items.map(([sym, d]) => `${sym}|${d.time}`).join(",");
  if (host.dataset.key === key) {
    $$(".ai-item .when", host).forEach((el) => (el.textContent = timeAgo(el.dataset.time)));
    return;
  }
  host.dataset.key = key;
  if (!items.length) {
    host.innerHTML = `<div class="empty"><b>Ainda sem análises</b>Inicia o bot ou clica em "Analisar agora".</div>`;
    return;
  }
  const openSet = new Set($$(".ai-text.open", host).map((e) => e.dataset.sym));
  host.innerHTML = items.map(([sym, d]) => `
    <div class="ai-item ${d.action === "SKIP" ? "skip" : ""}">
      <div class="ai-top"><span class="sym" data-go="${esc(sym)}" style="cursor:pointer">${esc(base(sym))}</span><span class="badge ${d.action}">${ACTION_PT[d.action] || d.action}</span>
        <span class="when" data-time="${esc(d.time)}" title="${esc(fmtTime(d.time))}">${timeAgo(d.time)}</span></div>
      ${d.setup ? `<div class="ai-setup">Setup: ${esc(d.setup)}</div>` : ""}
      ${d.action !== "SKIP" ? `<div class="conf"><span>Confiança</span><div class="conf-bar"><span style="width:${Math.round(d.confidence * 100)}%"></span></div><span>${Math.round(d.confidence * 100)}%</span></div>` : ""}
      <div class="ai-text ${openSet.has(sym) ? "open" : ""}" data-sym="${esc(sym)}" title="Clica para ver tudo">${esc(d.reasoning || "—")}</div>
      <div class="ai-outcome">${icon("chevron")}<span>${esc(d.outcome || "")}</span></div>
    </div>`).join("");
  $$(".ai-text", host).forEach((el) => (el.onclick = () => el.classList.toggle("open")));
  $$("[data-go]", host).forEach((el) => (el.onclick = () => selectSymbol(el.dataset.go)));
}

/* ============================================================ separadores */
function setTab(tab) {
  S.tab = tab;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  $$(".tab-panel").forEach((p) => (p.hidden = p.id !== `panel-${tab}`));
  if (tab === "decisions") refreshDecisions();
  if (tab === "radar") refreshRadar();
  if (tab === "news") refreshNews();
  if (tab === "trades") refreshTrades();
  if (tab === "equity") refreshEquity();
  if (tab === "logs") refreshLogs(true);
}

async function refreshRadar() {
  try {
    const r = await api("/api/scan");
    const info = $("#radarInfo");
    if (!r.time) {
      info.textContent = "O radar corre em cada análise do bot. Clica em \"Atualizar radar\" para ver agora.";
      $("#radarTable").innerHTML = `<div class="empty"><b>Radar ainda sem dados</b>Analisa tecnicamente as ${S.status?.scanner.universe || 25} moedas mais negociadas em ${Q()} e escolhe as melhores para a IA.</div>`;
      return;
    }
    info.textContent = `${r.results.length} de ${r.universe} moedas · atualizado ${timeAgo(new Date(r.time * 1000).toISOString())} · as de pontuação mais alta vão à IA`;
    $("#radarTable").innerHTML = `<table><thead><tr><th>#</th><th>Moeda</th><th></th><th>Pontuação</th><th>Setup detetado</th><th>Tendência maior</th>
      <th class="r">Preço</th><th class="r">24h</th><th class="r">vs BTC</th><th class="r">Volume 24h</th></tr></thead><tbody>` +
      r.results.map((x, i) => `<tr class="clickable ${x.picked ? "picked" : ""}" data-sym="${esc(x.symbol)}" title="${esc(x.pick_reason || "")}">
        <td class="muted">${i + 1}</td><td><b>${esc(base(x.symbol))}</b></td>
        <td>${x.picked ? `<span class="pick-tag">${icon("check")}Escolhida</span>` : ""}</td>
        <td><div class="score"><div class="score-bar"><span style="width:${x.score}%"></span></div><b>${x.score}</b></div></td>
        <td>${x.setup ? `${esc(x.setup.label)}${x.setup.order !== "BUY" ? ` <span class="muted small">(${x.setup.order === "BUY_LIMIT" ? "recuo" : "rompimento"} ${fmtPrice(x.setup.entry)})</span>` : ""}` : '<span class="muted">—</span>'}</td>
        <td><span class="trend-tag ${/up/.test(x.higher_trend) ? "up" : /down/.test(x.higher_trend) ? "down" : ""}">${TREND_PT[x.higher_trend] || "—"}</span></td>
        <td class="r">${fmtPrice(x.price)}</td><td class="r ${cls(x.change_24h_pct)}">${fmtPct(x.change_24h_pct)}</td>
        <td class="r ${cls(x.rel_strength_vs_btc)}">${fmtPct(x.rel_strength_vs_btc)}</td>
        <td class="r">${x.volume_24h ? nf(1).format(x.volume_24h / 1e6) + " M" : "—"}</td></tr>`).join("") + "</tbody></table>";
    $$("#radarTable tr[data-sym]").forEach((tr) => (tr.onclick = () => { selectSymbol(tr.dataset.sym); window.scrollTo({ top: 0, behavior: "smooth" }); }));
  } catch (e) { console.warn(e); }
}

async function scanNow() {
  const btn = $("#btnScan");
  try {
    await withBusy(btn, () => api("/api/scan", { method: "POST" }));
    refreshRadar();
  } catch (e) { toast(e.message, "err"); }
}

async function refreshNews() {
  if (!$("#newsList").children.length) {
    $("#newsList").innerHTML = `<div class="empty"><span class="spinner" style="display:inline-block;margin-bottom:6px"></span><br>A recolher notícias de ${13} sites…</div>`;
  }
  try {
    const n = await api("/api/news");
    $("#newsList").innerHTML = n.headlines.length ? n.headlines.map((h) => `
      <a class="news-item" href="${esc(h.link)}" target="_blank" rel="noopener">
        <span class="age">${h.age_h < 1 ? Math.round(h.age_h * 60) + " min" : nf(0).format(h.age_h) + " h"}</span>
        <span><div class="title">${esc(h.title)}</div><div class="src">${esc(h.source)}</div></span></a>`).join("")
      : `<div class="empty">Sem manchetes (verifica a ligação à internet).</div>`;
    const g = n.global;
    $("#globalBox").innerHTML = g ? `
      <div>Capitalização total<b>${nf(0).format(g.total_market_cap_usd_bn)} mil M$</b></div>
      <div>Variação 24h<b class="${cls(g.market_cap_change_24h_pct)}">${fmtPct(g.market_cap_change_24h_pct)}</b></div>
      <div>Dominância BTC<b>${nf(1).format(g.btc_dominance_pct)}%</b></div>
      <div>Dominância ETH<b>${nf(1).format(g.eth_dominance_pct)}%</b></div>` : `<div>Indisponível</div>`;
    $("#eventsList").innerHTML = n.events.length ? n.events.map((e) => `
      <div class="event"><span class="when">${new Date(e.time_utc.replace(" ", "T") + ":00Z").toLocaleString("pt-PT", { weekday: "short", hour: "2-digit", minute: "2-digit" })}</span>
        <span><span class="imp-${e.impact}">${e.country}</span> ${esc(e.event)}${e.forecast ? ` <span class="muted">(prev. ${esc(e.forecast)})</span>` : ""}</span></div>`).join("")
      : `<div class="muted small">Nenhum evento de alto impacto nas próximas 48h.</div>`;
    $("#sourcesList").innerHTML = Object.entries(n.sources).map(([k, v]) => `<span class="${String(v).startsWith("ok") ? "" : "bad"}" title="${esc(v)}">${esc(k)}</span>`).join("");
  } catch (e) { console.warn(e); }
}

async function refreshDecisions() {
  try {
    const { decisions } = await api("/api/decisions?limit=80");
    const host = $("#panel-decisions");
    if (!decisions.length) {
      host.innerHTML = `<div class="empty"><b>Ainda sem decisões</b>Cada análise da IA aparece aqui com o raciocínio completo.</div>`;
      return;
    }
    const open = new Set($$(".dec.open", host).map((e) => e.dataset.key));
    host.innerHTML = decisions.map((d) => {
      const key = `${d.time}|${d.symbol}`;
      const levels = d.action === "BUY" && d.stop_loss
        ? `<span>Stop ${fmtPrice(d.stop_loss)}</span><span>Alvo ${fmtPrice(d.take_profit)}</span>` : "";
      return `<div class="dec ${open.has(key) ? "open" : ""}" data-key="${esc(key)}">
        <div class="dec-row">
          <span class="t">${esc(fmtTime(d.time))}</span>
          <b>${esc(d.symbol)}</b>
          <span class="badge ${d.action}">${ACTION_PT[d.action] || d.action}</span>
          <span class="cf num">conf. ${Math.round((d.confidence || 0) * 100)}%</span>
          <span class="p">${fmtPrice(d.price)}</span>
          <span class="o" title="${esc(d.outcome)}">${esc(d.outcome)}</span>
          <svg class="i chev"><use href="#i-chevron"/></svg>
        </div>
        <div class="dec-detail">
          <div class="full"><h4>Decisão</h4><p>${esc(d.reasoning)}</p></div>
          <div><h4>A favor</h4><p>${esc(d.bull_case)}</p></div>
          <div><h4>Contra</h4><p>${esc(d.bear_case)}</p></div>
          <div class="full"><h4>O que muda a ideia</h4><p>${esc(d.invalidation)}</p></div>
          ${d.news ? `<div class="full"><h4>Notícias consideradas</h4><p>${esc(d.news)}</p></div>` : ""}
          <div class="full dec-meta"><span>Modelo ${esc(d.model)} (${esc(d.votes)})</span><span>Regime: ${esc(d.market_regime)}</span>${levels}
            <span>${nf(0).format((d.input_tokens || 0) + (d.output_tokens || 0))} tokens</span></div>
        </div>
      </div>`;
    }).join("");
    $$(".dec-row", host).forEach((r) => (r.onclick = () => r.parentElement.classList.toggle("open")));
  } catch (e) { console.warn(e); }
}

async function refreshTrades() {
  try {
    const { trades } = await api("/api/trades");
    const host = $("#panel-trades");
    if (!trades.length) {
      host.innerHTML = `<div class="empty"><b>Ainda sem trades fechados</b>Quando o bot fechar uma posição, o resultado aparece aqui.</div>`;
      return;
    }
    host.innerHTML = `<table><thead><tr>
        <th>Fecho</th><th>Par</th><th class="r">Entrada</th><th class="r">Saída</th><th class="r">Duração</th>
        <th class="r">Resultado</th><th class="r">%</th><th class="r">R</th><th>Motivo</th></tr></thead><tbody>` +
      trades.map((t) => `<tr>
        <td>${esc(fmtTime(t.closed_at))}</td><td><b>${esc(t.symbol)}</b></td>
        <td class="r">${fmtPrice(t.entry_price)}</td><td class="r">${fmtPrice(t.exit_price)}</td>
        <td class="r">${fmtDuration(t.hours)}</td>
        <td class="r ${cls(t.pnl)}">${fmtSigned(t.pnl)} ${Q()}</td><td class="r ${cls(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</td>
        <td class="r ${cls(t.r_multiple)}">${fmtSigned(t.r_multiple)}</td>
        <td><span class="tag">${esc(REASONS[t.reason] || t.reason)}</span></td></tr>`).join("") + "</tbody></table>";
  } catch (e) { console.warn(e); }
}

let equityData = [];
async function refreshEquity() {
  if (!equitySeries) return;
  try {
    const { points, start } = await api("/api/equity");
    const s = S.status;
    const now = Math.floor((Date.now() + S.clockOffset) / 1000);
    const byTime = new Map(points.map(([t, v]) => [Math.floor(t / 1000) + TZ, v]));
    if (s) byTime.set(now + TZ, s.equity);
    equityData = [...byTime.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
    equitySeries.applyOptions({ baseValue: { type: "price", price: start } });
    equitySeries.setData(equityData);
    equityChart.timeScale().fitContent();
    const vals = equityData.map((p) => p.value);
    const peak = Math.max(...vals, start), low = Math.min(...vals, start);
    $("#equityHead").innerHTML = `
      <div>Capital inicial<b>${fmtMoney(start)} ${Q()}</b></div>
      <div>Atual<b class="${cls((s?.equity ?? start) - start)}">${fmtMoney(s?.equity ?? start)} ${Q()}</b></div>
      <div>Máximo<b>${fmtMoney(peak)}</b></div><div>Mínimo<b>${fmtMoney(low)}</b></div>`;
    renderEquityLegend(null);
    if (equityData.length < 3) {
      $("#equityLegend").innerHTML = `<span>A curva vai-se formando enquanto o bot corre (um ponto a cada 5 minutos).</span>`;
    }
  } catch (e) { console.warn(e); }
}

function renderEquityLegend(d) {
  const p = d || equityData.at(-1);
  if (!p) { $("#equityLegend").innerHTML = ""; return; }
  const when = new Date((p.time - TZ) * 1000).toLocaleString("pt-PT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  $("#equityLegend").innerHTML = `<span class="lg-title">Capital</span><span><b>${fmtMoney(p.value)} ${Q()}</b></span><span>${when}</span>`;
}

async function refreshLogs(reset = false) {
  try {
    if (reset) { S.logsAfter = 0; $("#logs").innerHTML = ""; }
    const { lines } = await api(`/api/logs?after=${S.logsAfter}`);
    if (!lines.length) {
      if (!$("#logs").children.length) $("#logs").innerHTML = `<div class="empty">Sem registos ainda.</div>`;
      return;
    }
    const host = $("#logs");
    if (host.querySelector(".empty")) host.innerHTML = "";
    const panel = $("#panel-logs");
    const atBottom = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 30;
    host.insertAdjacentHTML("beforeend", lines.map((l) => {
      const t = new Date(l.t * 1000).toLocaleTimeString("pt-PT");
      const lvl = { INFO: "INFO", WARNING: "AVISO", ERROR: "ERRO" }[l.level] || l.level;
      return `<div class="log-line ${l.level}"><span class="lt">${t}</span><span class="ll">${lvl}</span><span class="lm">${esc(l.msg)}</span></div>`;
    }).join(""));
    S.logsAfter = lines.at(-1).id;
    while (host.children.length > 800) host.firstChild.remove();
    if (atBottom || reset) panel.scrollTop = panel.scrollHeight;
  } catch (e) { console.warn(e); }
}

/* ============================================================ ações */
async function startStop() {
  const s = S.status;
  if (!s) return;
  const btn = $("#btnStartStop");
  if (s.running) {
    await withBusy(btn, () => api("/api/bot/stop", { method: "POST" }));
    toast("Bot parado. As posições abertas mantêm-se, mas deixam de ser vigiadas.", "info");
  } else {
    if (s.mode === "live") {
      const ok = await confirmDialog({
        title: s.mode_key === "testnet" ? "Iniciar na Testnet?" : "Iniciar com dinheiro real?",
        text: s.mode_key === "testnet"
          ? `O bot vai enviar ordens para a Binance Testnet (dinheiro fictício), usando até <b>${fmtMoney(s.capital_limit, 0)} ${s.quote}</b>.`
          : `O bot vai comprar e vender na tua conta Binance com até <b>${fmtMoney(s.capital_limit, 0)} ${s.quote}</b> de dinheiro real.`,
        ok: "Iniciar", danger: s.mode_key === "live",
      });
      if (!ok) return;
    }
    try {
      await withBusy(btn, () => api("/api/bot/start", { method: "POST" }));
      toast("Bot iniciado. A primeira análise começa dentro de segundos.", "ok");
    } catch (e) { toast(e.message, "err", 8000); }
  }
  refreshStatus();
}

async function analyzeNow() {
  const btn = $("#btnAnalyze");
  try {
    await withBusy(btn, () => api("/api/bot/analyze", { method: "POST" }));
    toast(`Análise iniciada: cerca de 10–20 s por par.`, "info");
    setTimeout(refreshStatus, 800);
  } catch (e) { toast(e.message, "err", 8000); }
}

async function closePosition(symbol, btn) {
  const ok = await confirmDialog({ title: `Fechar ${symbol}?`, text: "A posição é vendida a preço de mercado agora.", ok: "Fechar posição", danger: true });
  if (!ok) return;
  try {
    const r = await withBusy(btn, () => api("/api/positions/close", { method: "POST", body: { symbol } }));
    const pnl = r.trade?.pnl;
    toast(`${symbol} fechada${pnl != null ? ` (${fmtSigned(pnl)} ${Q()})` : ""}.`, "ok");
  } catch (e) { toast(e.message, "err", 8000); }
  refreshStatus(); refreshCandles();
}

async function cancelPending(symbol, btn) {
  const ok = await confirmDialog({ title: `Cancelar a ordem de ${symbol}?`, text: "A ordem pendente é removida e o bot deixa de a vigiar.", ok: "Cancelar ordem" });
  if (!ok) return;
  try {
    await withBusy(btn, () => api("/api/pending/cancel", { method: "POST", body: { symbol } }));
    toast(`Ordem de ${symbol} cancelada.`, "ok");
  } catch (e) { toast(e.message, "err"); }
  refreshStatus(); refreshCandles();
}

async function closeAll() {
  const ok = await confirmDialog({ title: "Fechar todas as posições?", text: "Todas as posições são vendidas a preço de mercado agora.", ok: "Fechar tudo", danger: true });
  if (!ok) return;
  try {
    await withBusy($("#btnCloseAll"), () => api("/api/positions/close-all", { method: "POST" }));
    toast("Todas as posições foram fechadas.", "ok");
  } catch (e) { toast(e.message, "err", 8000); }
  refreshStatus();
}

async function resumeTrading() {
  const ok = await confirmDialog({ title: "Desbloquear novas compras?", text: "O limite de queda foi atingido. Confirma que queres que o bot volte a abrir posições.", ok: "Desbloquear" });
  if (!ok) return;
  try { await api("/api/resume", { method: "POST" }); toast("Compras desbloqueadas.", "ok"); } catch (e) { toast(e.message, "err"); }
  refreshStatus();
}

/* ============================================================ modos */
async function onModeClick(mode) {
  const s = S.status;
  if (!s || mode === s.mode) return;
  if (mode === "paper") {
    if (s.running) {
      const ok = await confirmDialog({ title: "Mudar para o Modo Teste?", text: "O bot em Modo Real vai ser parado. As posições reais abertas ficam na tua conta Binance sem vigilância até voltares ao Modo Real.", ok: "Parar e mudar" });
      if (!ok) return;
    }
    try { await api("/api/mode", { method: "POST", body: { mode: "paper" } }); toast("Modo Teste ativo.", "ok"); }
    catch (e) { toast(e.message, "err"); }
    S.symbol = null; S.lastDecisionSig = "";
    await refreshStatus(); refreshCandles(true); setTab(S.tab);
    return;
  }
  openLiveModal();
}

function openLiveModal() {
  const s = S.status;
  S.liveCheck = null;
  $("#liveTestnet").checked = !!s.use_testnet;
  $("#liveCapital").value = Math.round(s.capital_limit);
  $("#liveApiKey").value = ""; $("#liveApiSecret").value = "";
  $("#liveAccept1").checked = false; $("#liveAccept2").checked = false; $("#liveConfirm").value = "";
  $("#liveChecks").innerHTML = "";
  updateLiveKeyView();
  $("#liveModal").hidden = false;
  validateLive();
  if (savedKeyFor($("#liveTestnet").checked)) verifyLive(true);
}

function savedKeyFor(testnet) { return testnet ? S.status.keys.testnet : S.status.keys.binance; }

function updateLiveKeyView() {
  const saved = savedKeyFor($("#liveTestnet").checked);
  const box = $("#liveSavedKey");
  box.hidden = !saved;
  $("#liveKeyInputs").hidden = !!saved;
  if (saved) {
    box.innerHTML = `<span>${icon("lock")} Chave guardada <span class="mono">${esc(saved)}</span></span><button class="btn btn-sm btn-secondary" id="btnChangeKey">Alterar</button>`;
    $("#btnChangeKey").onclick = () => { box.hidden = true; $("#liveKeyInputs").hidden = false; $("#liveApiKey").focus(); };
  }
}

async function verifyLive(silent = false) {
  const testnet = $("#liveTestnet").checked;
  const body = { testnet, api_key: $("#liveApiKey").value.trim(), api_secret: $("#liveApiSecret").value.trim() };
  const usingInputs = !$("#liveKeyInputs").hidden;
  if (usingInputs && (!body.api_key || !body.api_secret)) {
    if (!silent) toast("Preenche a API Key e a Secret Key.", "err");
    return;
  }
  if (!usingInputs) { body.api_key = ""; body.api_secret = ""; }
  const btn = $("#btnLiveVerify");
  $("#liveChecks").innerHTML = `<div class="check-item muted"><span class="spinner"></span>A verificar ligação à Binance…</div>`;
  try {
    const r = await withBusy(btn, () => api("/api/keys/binance", { method: "POST", body }));
    S.liveCheck = r;
    const items = [];
    if (r.usdt_free != null) {
      items.push(["ok", `Ligação OK${r.testnet ? " (Testnet)" : ""}`]);
      items.push(["ok", `Saldo livre: ${fmtMoney(r.usdt_free)} ${r.quote || Q()}`]);
      if (!r.testnet) {
        items.push([r.withdrawals_enabled ? "bad" : "ok", r.withdrawals_enabled ? "Levantamentos ATIVADOS nesta chave" : "Levantamentos desativados"]);
        items.push([r.spot_trading_enabled ? "ok" : "bad", r.spot_trading_enabled ? "Trading spot ativo" : "Trading spot desativado"]);
      }
    }
    r.problems.filter((p) => r.usdt_free == null || !/LEVANTAMENTOS|spot/i.test(p)).forEach((p) => items.push(["bad", p]));
    if (r.withdrawals_enabled) items.push(["bad", "Desativa 'Enable Withdrawals' na Binance e verifica de novo."]);
    $("#liveChecks").innerHTML = items.map(([k, t]) => `<div class="check-item ${k}">${icon(k === "ok" ? "check" : "alert")}<span>${esc(t)}</span></div>`).join("");
    if (r.saved) { await refreshStatus(); updateLiveKeyView(); }
  } catch (e) {
    S.liveCheck = null;
    $("#liveChecks").innerHTML = `<div class="check-item bad">${icon("alert")}<span>${esc(e.message)}</span></div>`;
  }
  validateLive();
}

function validateLive() {
  const c = S.liveCheck, testnet = $("#liveTestnet").checked;
  const capital = parseFloat($("#liveCapital").value);
  let hint = "";
  if (!c || !c.ok) hint = "Verifica primeiro a ligação à Binance.";
  else if (!(capital >= 10)) hint = `O capital mínimo é 10 ${Q()}.`;
  else if (!testnet && capital > c.usdt_free) hint = `O capital é maior do que o saldo livre (${fmtMoney(c.usdt_free)} ${Q()}).`;
  else if (!$("#liveAccept1").checked || !$("#liveAccept2").checked) hint = "Confirma que compreendes os riscos.";
  else if ($("#liveConfirm").value.trim().toUpperCase() !== "REAL") hint = "Escreve REAL para confirmar.";
  $("#liveHint").textContent = hint;
  const btn = $("#btnLiveActivate");
  btn.disabled = !!hint;
  btn.textContent = testnet ? "Ativar Modo Real (Testnet)" : "Ativar Modo Real";
}

async function activateLive() {
  const body = {
    mode: "live", testnet: $("#liveTestnet").checked, capital: parseFloat($("#liveCapital").value),
    confirm: $("#liveConfirm").value, accept: true,
  };
  try {
    await withBusy($("#btnLiveActivate"), () => api("/api/mode", { method: "POST", body }));
    $("#liveModal").hidden = true;
    toast(body.testnet ? "Modo Real (Testnet) ativo. Clica em Iniciar bot quando quiseres." : "Modo Real ativo. Clica em Iniciar bot quando quiseres.", "ok", 7000);
    S.symbol = null; S.lastDecisionSig = "";
    await refreshStatus(); refreshCandles(true); setTab(S.tab);
  } catch (e) { toast(e.message, "err", 9000); }
}

/* ============================================================ definições */
const PROFILE_INFO = {
  conservador: "Poucos trades, só setups muito claros. Análise a cada hora.",
  equilibrado: "Bons setups e ordens pendentes. Análise a cada hora.",
  agressivo: "Mais trades, risco controlado por trade. Análise a cada 15 min (mais custo de IA).",
};

const SETTINGS_LAYOUT = [
  { title: "Escolha das moedas", fields: [
    { key: "AUTO_SELECT", type: "toggle", label: "Escolha automática (recomendado)",
      help: "Em cada análise o bot examina as moedas mais negociadas, a IA gestora escolhe as melhores para aquele momento e a lista vai mudando sozinha." },
    { key: "SCAN_UNIVERSE", type: "num", label: "Moedas examinadas", step: 5 },
    { key: "SCAN_TOP", type: "num", label: "Moedas analisadas a fundo por ciclo", step: 1, profile: true },
    { key: "SCAN_MIN_SCORE", type: "num", label: "Pontuação técnica mínima (0-100)", step: 5 },
  ] },
  { title: "Mercado", fields: [
    { key: "QUOTE", type: "seg", label: "Moeda de negociação", options: [["USDC", "USDC"], ["EUR", "EUR"], ["USDT", "USDT"]],
      help: "Na União Europeia a Binance não permite USDT: usa USDC (≈ 1 dólar, muitos pares) ou EUR (poucos pares)." },
    { key: "ASSETS", type: "chips", label: "Pares fixos (sempre acompanhados)", showIf: (d) => !d.AUTO_SELECT,
      help: "Até 8. O radar junta as melhores oportunidades das outras moedas." },
    { key: "PRIMARY_TIMEFRAME", type: "seg", label: "Frequência de análise", profile: true, options: [["15m", "15 min"], ["1h", "1 hora"], ["4h", "4 horas"]],
      help: "A IA analisa no fecho de cada vela. Mais frequente = mais oportunidades e mais custo." },
  ] },
  { title: "Radar (modo manual)", showIf: (d) => !d.AUTO_SELECT, fields: [
    { key: "SCANNER_ENABLED", type: "toggle", label: "Juntar as melhores do radar aos pares fixos", help: "Analisa tecnicamente as moedas mais negociadas (sem custo de IA) e envia as melhores à IA." },
    { key: "ONLY_WITH_SETUP", type: "toggle", label: "Poupar IA quando não há setup", help: "Pares fixos sem nenhum setup técnico não vão à IA. As posições abertas são sempre revistas." },
  ] },
  { title: "Inteligência artificial", fields: [
    { key: "DECISION_MODEL", type: "select", label: "Modelo de decisão" },
    { key: "REASONING_EFFORT", type: "seg", label: "Profundidade de raciocínio", options: [["low", "Baixa"], ["medium", "Média"], ["high", "Alta"]] },
    { key: "DECISION_VOTES", type: "seg", label: "Opiniões independentes", options: [[1, "1"], [3, "3"], [5, "5"]], help: "Com 3 ou 5 a IA analisa várias vezes e o bot só age se a maioria concordar (custo multiplicado)." },
    { key: "NEWS_ENABLED", type: "toggle", label: "Resumo de notícias com pesquisa web", help: "As manchetes de 13 sites e o calendário económico são sempre usados (grátis). Isto acrescenta a pesquisa da IA." },
    { key: "NEWS_REFRESH_MINUTES", type: "num", label: "Atualizar resumo a cada", unit: "min", step: 15 },
    { key: "AI_DAILY_CALL_LIMIT", type: "num", label: "Limite diário de análises da IA", step: 50,
      help: "Protege o teu saldo da OpenAI. 0 = sem limite. Ao atingir, o bot continua a proteger as posições com stops e alvos." },
  ] },
  { title: "Gestão de risco", fields: [
    { key: "RISK_MODE", type: "seg", label: "Como definir o risco", options: [["percent", "% do capital"], ["fixed", "Valor fixo"]] },
    { key: "RISK_PER_TRADE_PCT", type: "num", label: "Risco por trade", unit: "%", step: 0.1, profile: true, showIf: (d) => d.RISK_MODE === "percent",
      help: "Quanto do capital se perde se o stop-loss for atingido." },
    { key: "RISK_FIXED_AMOUNT", type: "num", label: "Risco por trade", unit: "Q", step: 0.1, showIf: (d) => d.RISK_MODE === "fixed",
      help: "Quanto perdes se o stop for atingido (ex.: 0,50). No alvo ganhas normalmente 1,5 a 3 vezes isto." },
    { key: "MAX_POSITION_PCT", type: "num", label: "Tamanho máximo por posição", unit: "%", step: 5 },
    { key: "MAX_OPEN_POSITIONS", type: "num", label: "Posições em simultâneo", step: 1, profile: true },
    { key: "MIN_CONFIDENCE", type: "num", label: "Confiança mínima para comprar", step: 0.01, profile: true, help: "Entre 0,5 e 0,95. Mais baixo = mais trades." },
    { key: "MIN_RISK_REWARD", type: "num", label: "Alvo mínimo (x o risco)", step: 0.1, profile: true },
    { key: "PENDING_ORDER_CANDLES", type: "num", label: "Validade das ordens pendentes", unit: "velas", step: 1 },
    { key: "MAX_DAILY_LOSS_PCT", type: "num", label: "Perda diária máxima", unit: "%", step: 0.5, help: "Ao atingir, o bot não abre mais posições nesse dia." },
    { key: "MAX_DRAWDOWN_PCT", type: "num", label: "Queda máxima desde o pico", unit: "%", step: 1, help: "Ao atingir, o bot bloqueia compras até desbloqueares." },
    { key: "COOLDOWN_AFTER_LOSS_MINUTES", type: "num", label: "Pausa num par após perda", unit: "min", step: 30 },
  ] },
  { title: "Proteção de lucros", fields: [
    { key: "BREAKEVEN_AT_R", type: "num", label: "Stop para a entrada ao ganhar", unit: "R", step: 0.25, help: "R = valor arriscado. 1R = quando o lucro iguala o risco. 0 desliga." },
    { key: "TRAIL_START_R", type: "num", label: "Ativar trailing stop a partir de", unit: "R", step: 0.25 },
    { key: "TRAIL_ATR_MULT", type: "num", label: "Distância do trailing stop", unit: "ATR", step: 0.25 },
  ] },
  { title: "Capital", fields: [
    { key: "PAPER_START_BALANCE", type: "num", label: "Saldo inicial do Modo Teste", unit: "Q", step: 10, help: "Aplica-se quando recomeças o Modo Teste (em baixo). Usa o mesmo valor que pensas usar no Modo Real." },
    { key: "LIVE_CAPITAL_USDT", type: "num", label: "Capital máximo do Modo Real", unit: "Q", step: 10 },
  ] },
];

async function openSettings() {
  try {
    S.settings = await api("/api/settings");
  } catch (e) { toast(e.message, "err"); return; }
  S.draft = JSON.parse(JSON.stringify(S.settings.values));
  paintSettings();
  $("#settingsNote").textContent = "";
  $("#drawerOverlay").hidden = false;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}

function closeSettings() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawerOverlay").hidden = true;
}

function renderSettings() { S.draft = JSON.parse(JSON.stringify(S.settings.values)); paintSettings(); }

function estimateText(d) {
  const tfMin = { "15m": 15, "1h": 60, "4h": 240 }[d.PRIMARY_TIMEFRAME] || 60;
  const perCycle = d.AUTO_SELECT ? Number(d.SCAN_TOP) + 1 : d.ASSETS.length + (d.SCANNER_ENABLED ? Number(d.SCAN_TOP) : 0);
  const perHour = (perCycle * 60 / tfMin) * Number(d.DECISION_VOTES || 1);
  return `Até ~${nf(0).format(perHour)} análises da IA por hora (menos quando não há setups técnicos), mais o resumo de notícias.`;
}

function paintSettings() {
  const { options: o } = S.settings;
  const draft = S.draft;
  const q = draft.QUOTE || Q();
  const field = (f) => {
    if (f.showIf && !f.showIf(draft)) return "";
    const val = draft[f.key];
    const unit = f.unit === "Q" ? q : f.unit;
    const help = f.help ? `<div class="help">${esc(f.help)}</div>` : "";
    if (f.type === "chips") {
      return `<div class="field"><span class="field-label">${f.label}</span><div class="chips" data-key="${f.key}">` +
        o.assets.map((a) => `<button class="chip ${val.includes(a) ? "on" : ""}" data-asset="${a}">${a}</button>`).join("") + `</div>${help}</div>`;
    }
    if (f.type === "seg") {
      return `<div class="field"><span class="field-label">${f.label}</span><div class="seg" data-key="${f.key}" data-profile="${f.profile ? 1 : 0}">` +
        f.options.map(([ov, ol]) => `<button data-val="${ov}" class="${String(val) === String(ov) ? "active" : ""}">${ol}</button>`).join("") + `</div>${help}</div>`;
    }
    if (f.type === "select") {
      return `<div class="field"><label for="f-${f.key}">${f.label}</label><select class="input" id="f-${f.key}" data-key="${f.key}">` +
        o.models.map((m) => `<option value="${m.value}" ${m.value === val ? "selected" : ""}>${esc(m.label)}</option>`).join("") + `</select>${help}</div>`;
    }
    if (f.type === "toggle") {
      return `<div class="field-inline"><div class="field-text"><span class="field-label">${f.label}</span>${help}</div>
        <label class="switch"><input type="checkbox" data-key="${f.key}" ${val ? "checked" : ""}><span></span></label></div>`;
    }
    const [lo, hi] = o.bounds[f.key] || [];
    return `<div class="field-inline"><div class="field-text"><label class="field-label" for="f-${f.key}">${f.label}</label>${help}</div>
      <div class="input-suffix" style="width:140px"><input class="input" id="f-${f.key}" type="number" data-key="${f.key}" data-profile="${f.profile ? 1 : 0}" value="${val}" step="${f.step || 1}" ${lo != null ? `min="${lo}" max="${hi}"` : ""}>${unit ? `<span>${unit}</span>` : ""}</div></div>`;
  };
  const s = S.status;
  const profileCards = ["conservador", "equilibrado", "agressivo"].map((p) => `
    <button class="profile-card ${draft.PROFILE === p ? "on" : ""}" data-profile-pick="${p}"><b>${PROFILE_PT[p]}</b><span>${PROFILE_INFO[p]}</span></button>`).join("");
  $("#settingsBody").innerHTML = `
    <div class="set-section"><h3>Estilo de trading</h3>
      <div class="profiles">${profileCards}</div>
      <div class="help">${draft.PROFILE === "personalizado" ? "Perfil personalizado (alteraste valores de um perfil)." : "Escolher um perfil ajusta a frequência, a confiança mínima, o alvo mínimo, o risco e o radar. Podes afinar depois."}</div>
      <div class="estimate" id="estimate">${estimateText(draft)}</div>
    </div>` +
    SETTINGS_LAYOUT.filter((sec) => !sec.showIf || sec.showIf(draft)).map((sec) => `<div class="set-section"><h3>${sec.title}</h3>${sec.fields.map(field).join("")}</div>`).join("") + `
    <div class="set-section"><h3>Chaves API</h3>
      <div class="key-row"><span>OpenAI</span><span class="mono">${esc(s.keys.openai || "não configurada")}</span></div>
      <div class="field"><label for="openaiKey">Nova chave da OpenAI</label>
        <div class="row-gap"><input class="input mono" id="openaiKey" type="password" placeholder="sk-..." autocomplete="off"><button class="btn btn-secondary" id="btnSaveOpenai">Guardar</button></div>
        <div class="help">É testada antes de ser guardada. Fica só no ficheiro .env deste PC.</div></div>
      <div class="key-row"><span>Binance</span><span class="mono">${esc(s.keys.binance || "não configurada")}</span></div>
      <div class="key-row"><span>Binance Testnet</span><span class="mono">${esc(s.keys.testnet || "não configurada")}</span></div>
      <div class="help">As chaves da Binance configuram-se ao ativar o Modo Real.</div>
    </div>
    <div class="set-section"><h3>Zona de perigo</h3>
      <div class="danger-zone">
        <div class="field-inline" style="margin:0"><div class="field-text"><span class="field-label">Recomeçar o Modo Teste</span>
          <div class="help">Apaga posições e histórico do Modo Teste e recomeça com o saldo inicial definido acima.</div></div>
          <button class="btn btn-sm btn-danger-ghost" id="btnResetTest" ${s.mode_key !== "paper" ? "disabled title='Só no Modo Teste'" : ""}>Recomeçar</button></div>
      </div>
    </div>`;

  const body = $("#settingsBody");
  const touched = (isProfileField) => {
    if (isProfileField && draft.PROFILE !== "personalizado") { draft.PROFILE = "personalizado"; paintSettings(); }
    $("#estimate").textContent = estimateText(draft);
    markDirty();
  };
  $$("[data-profile-pick]", body).forEach((b) => (b.onclick = () => {
    const name = b.dataset.profilePick;
    Object.assign(draft, S.settings.options.profiles[name], { PROFILE: name });
    paintSettings(); markDirty();
  }));
  $$(".chips .chip", body).forEach((c) => (c.onclick = () => {
    const list = draft.ASSETS;
    const i = list.indexOf(c.dataset.asset);
    if (i >= 0) list.splice(i, 1); else if (list.length < 8) list.push(c.dataset.asset);
    c.classList.toggle("on", list.includes(c.dataset.asset));
    touched(false);
  }));
  $$(".seg[data-key]", body).forEach((seg) => $$("button", seg).forEach((b) => (b.onclick = () => {
    $$("button", seg).forEach((x) => x.classList.toggle("active", x === b));
    const raw = b.dataset.val;
    draft[seg.dataset.key] = isNaN(Number(raw)) ? raw : Number(raw);
    if (seg.dataset.key === "RISK_MODE" || seg.dataset.key === "QUOTE") { paintSettings(); markDirty(); return; }
    touched(seg.dataset.profile === "1");
  })));
  $$("select[data-key]", body).forEach((el) => (el.onchange = () => { draft[el.dataset.key] = el.value; touched(false); }));
  $$("input[type=checkbox][data-key]", body).forEach((el) => (el.onchange = () => {
    draft[el.dataset.key] = el.checked;
    if (el.dataset.key === "AUTO_SELECT") { paintSettings(); markDirty(); return; }
    touched(false);
  }));
  $$("input[type=number][data-key]", body).forEach((el) => {
    el.oninput = () => { draft[el.dataset.key] = parseFloat(el.value); $("#estimate").textContent = estimateText(draft); markDirty(); };
    el.onchange = () => touched(el.dataset.profile === "1");
  });
  $("#btnSaveOpenai").onclick = saveOpenaiKey;
  $("#btnResetTest").onclick = resetTest;
}

function markDirty() {
  $("#settingsNote").textContent = S.status?.running ? "Alterações aplicadas a partir da próxima análise." : "Alterações por guardar.";
}

async function saveSettings() {
  try {
    await withBusy($("#btnSaveSettings"), () => api("/api/settings", { method: "POST", body: S.draft }));
    toast("Definições guardadas.", "ok");
    closeSettings();
    S.symbol = null;
    await refreshStatus();
    refreshCandles(true);
  } catch (e) { toast(e.message, "err", 8000); }
}

async function saveOpenaiKey() {
  const key = $("#openaiKey").value.trim();
  if (!key) return;
  try {
    await withBusy($("#btnSaveOpenai"), () => api("/api/keys/openai", { method: "POST", body: { key } }));
    toast("Chave da OpenAI verificada e guardada.", "ok");
    await refreshStatus();
    renderSettings();
  } catch (e) { toast(e.message, "err", 8000); }
}

async function resetTest() {
  const bal = S.draft.PAPER_START_BALANCE;
  const ok = await confirmDialog({ title: "Recomeçar o Modo Teste?", text: `Apaga posições e histórico do Modo Teste e recomeça com <b>${fmtMoney(bal, 0)} ${Q()}</b> fictícios.`, ok: "Recomeçar", danger: true });
  if (!ok) return;
  try {
    await api("/api/reset-test", { method: "POST", body: { balance: bal } });
    toast("Modo Teste recomeçado.", "ok");
    closeSettings(); S.lastDecisionSig = "";
    await refreshStatus(); refreshCandles(true); setTab(S.tab);
  } catch (e) { toast(e.message, "err"); }
}

/* ============================================================ arranque */
function bind() {
  $$(".mode-btn").forEach((b) => (b.onclick = () => onModeClick(b.dataset.mode)));
  $("#btnStartStop").onclick = startStop;
  $("#btnAnalyze").onclick = analyzeNow;
  $("#btnCloseAll").onclick = closeAll;
  $("#btnScan").onclick = scanNow;
  $("#btnSettings").onclick = openSettings;
  $("#btnTheme").onclick = () => setTheme(getTheme() === "dark" ? "light" : "dark");
  $$("[data-close-drawer]").forEach((b) => (b.onclick = closeSettings));
  $("#drawerOverlay").onclick = closeSettings;
  $("#btnSaveSettings").onclick = saveSettings;
  $$(".tab").forEach((t) => (t.onclick = () => setTab(t.dataset.tab)));

  $("#tfTabs").innerHTML = ["15m", "1h", "4h", "1d"].map((tf) => `<button data-tf="${tf}">${tf}</button>`).join("");
  $$("#tfTabs button").forEach((b) => (b.onclick = () => {
    S.tf = b.dataset.tf;
    $$("#tfTabs button").forEach((x) => x.classList.toggle("active", x === b));
    refreshCandles(true);
  }));

  $$("[data-close-modal]").forEach((b) => (b.onclick = () => ($("#liveModal").hidden = true)));
  $("#liveTestnet").onchange = () => { S.liveCheck = null; $("#liveChecks").innerHTML = ""; updateLiveKeyView(); validateLive(); if (savedKeyFor($("#liveTestnet").checked)) verifyLive(true); };
  $("#btnLiveVerify").onclick = () => verifyLive(false);
  ["#liveCapital", "#liveAccept1", "#liveAccept2", "#liveConfirm"].forEach((id) => ($(id).oninput = validateLive));
  ["#liveAccept1", "#liveAccept2"].forEach((id) => ($(id).onchange = validateLive));
  $("#btnLiveActivate").onclick = activateLive;

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#confirmModal").hidden) $("#confirmCancel").click();
    else if (!$("#liveModal").hidden) $("#liveModal").hidden = true;
    else if ($("#drawer").classList.contains("open")) closeSettings();
  });
}

async function main() {
  setTheme(getTheme());
  bind();
  initCharts();
  await refreshStatus();
  S.tf = S.status?.timeframe || "1h";
  $$("#tfTabs button").forEach((x) => x.classList.toggle("active", x.dataset.tf === S.tf));
  refreshCandles(true);
  setTab("decisions");

  setInterval(refreshStatus, 2500);
  setInterval(renderCountdown, 1000);
  setInterval(() => refreshCandles(), 15000);
  setInterval(() => { if (S.tab === "logs") refreshLogs(); }, 2500);
  setInterval(() => {
    if (S.tab === "decisions") refreshDecisions();
    else if (S.tab === "trades") refreshTrades();
    else if (S.tab === "equity") refreshEquity();
    else if (S.tab === "radar") refreshRadar();
    else if (S.tab === "news") refreshNews();
  }, 20000);

}

main();

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
const REASONS = { stop_loss: "Stop-loss", trailing_stop: "Trailing stop", take_profit: "Take-profit", sinal_ia: "Sinal da IA", manual: "Manual",
  fim_de_semana: "Fecho de sexta", corretora: "Na corretora", antes_resultados: "Antes dos resultados", sem_progresso: "Sem progresso",
  parcial: "Parcial (1R)", alvo_parcial: "Alvo (parcial)", reequilibrio: "Reequilíbrio", abaixo_media_200: "Abaixo da MM200" };
const ACTION_PT = { BUY: "COMPRAR", SELL: "VENDER", HOLD: "ESPERAR", BUY_LIMIT: "COMPRA NO RECUO", BUY_STOP: "COMPRA NO ROMPIMENTO", SKIP: "SEM SETUP",
  SHORT: "VENDA SHORT", SHORT_LIMIT: "SHORT NO REPIQUE", SHORT_STOP: "SHORT NA QUEBRA", CLOSE: "FECHAR" };
const PENDING_PT = { BUY_LIMIT: "Comprar no recuo", BUY_STOP: "Comprar no rompimento", SHORT_LIMIT: "Vender no repique", SHORT_STOP: "Vender na quebra" };
const ENTRY_ACTIONS = ["BUY", "BUY_LIMIT", "BUY_STOP", "SHORT", "SHORT_LIMIT", "SHORT_STOP"];
const MARKET_PT = { crypto: "Cripto", stocks: "Ações & ETFs", cfd: "CFDs (ouro, forex, índices)" };
const BROKER_PT = { crypto: "Binance", stocks: "Trading 212", cfd: "cTrader" };
const broker = () => BROKER_PT[S.status?.market] || "Binance";
const sideBadge = (side) => (side === "short" ? '<span class="side-badge short" title="Venda a descoberto: ganha se o preço descer">VENDA</span>'
  : '<span class="side-badge long" title="Compra: ganha se o preço subir">COMPRA</span>');
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
    const MK = {  // compra/venda normais + venda a descoberto (short) e recompra (cover)
      buy: ["belowBar", "arrowUp", () => cssVar("--accent"), () => "Compra"],
      sell: ["aboveBar", "arrowDown", (m) => (m.pnl >= 0 ? up : down), (m) => `Venda ${fmtSigned(m.pnl)}`],
      short: ["aboveBar", "arrowDown", () => cssVar("--accent"), () => "Venda (short)"],
      cover: ["belowBar", "arrowUp", (m) => (m.pnl >= 0 ? up : down), (m) => `Fecho ${fmtSigned(m.pnl)}`],
    };
    candleSeries.setMarkers(d.markers.map((m) => {
      const [position, shape, color, text] = MK[m.kind] || MK.buy;
      return { time: m.time + TZ, position, shape, color: color(m), text: text(m) };
    }));
    priceLines.forEach((l) => candleSeries.removePriceLine(l));
    priceLines = [];
    if (d.lines) {
      const L = LightweightCharts.LineStyle;
      if (d.lines.entry) priceLines.push(candleSeries.createPriceLine({ price: d.lines.entry, color: cssVar("--accent"), lineWidth: 1, lineStyle: L.Solid, title: d.lines.side === "short" ? "Entrada (venda)" : "Entrada" }));
      const PEND_TITLE = { BUY_LIMIT: "Compra (recuo)", BUY_STOP: "Compra (rompimento)", SHORT_LIMIT: "Venda (repique)", SHORT_STOP: "Venda (quebra)" };
      if (d.lines.pending) priceLines.push(candleSeries.createPriceLine({ price: d.lines.pending, color: cssVar("--accent"), lineWidth: 2, lineStyle: L.Dotted, title: PEND_TITLE[d.lines.pending_type] || "Pendente" }));
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

function renderTfTabs(s) {
  const tfs = s.chart_tfs || ["15m", "1h", "4h", "1d"];
  const host = $("#tfTabs");
  if (host.dataset.sig === tfs.join()) return;
  host.dataset.sig = tfs.join();
  if (!tfs.includes(S.tf)) S.tf = "1h";
  host.innerHTML = tfs.map((tf) => `<button data-tf="${tf}" class="${tf === S.tf ? "active" : ""}">${tf}</button>`).join("");
  $$("button", host).forEach((b) => (b.onclick = () => {
    S.tf = b.dataset.tf;
    $$("button", host).forEach((x) => x.classList.toggle("active", x === b));
    refreshCandles(true);
  }));
}

function modeKind(s) {  // paper | testnet | live (para cores e textos, em qualquer mercado)
  return s.mode === "paper" ? "paper" : /testnet|demo/.test(s.mode_key) ? "testnet" : "live";
}

function renderStatus(s) {
  document.body.dataset.mode = modeKind(s);
  document.body.dataset.market = s.market;
  $$(".market-btn").forEach((b) => {
    const on = b.dataset.market === s.market;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on);
  });
  renderTfTabs(s);
  $$(".mode-btn[data-mode]").forEach((b) => {
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
  $("#brandSub").textContent = { stocks: "Trading 212 · Yahoo Finance · OpenAI", cfd: "cTrader · Yahoo Finance · OpenAI" }[s.market] || "Binance · OpenAI";
  const act = $("#activity");
  act.textContent = s.running ? s.activity : "Bot parado";
  act.classList.toggle("busy", s.running && isBusy(s.activity));

  // banner
  const risk = s.risk_mode === "fixed" ? `risco ${fmtMoney(s.risk_fixed)} ${s.quote} por trade` : `risco ${nf(2).format(s.risk_pct)}% por trade`;
  const stocks = s.market === "stocks", cfd = s.market === "cfd";
  const trend = stocks && s.strategy === "tendencia";
  const radar = trend ? `tendência de ETFs (sem IA)` : s.auto_select ? `escolha automática entre ${s.scanner.universe} ${stocks ? "ativos" : cfd ? "instrumentos" : "moedas"}` : "pares fixos";
  const exch = s.market !== "crypto" && s.exchanges.length ? ` <span class="sep">·</span> ` + exchangesSummary(s.exchanges, cfd) : "";
  const shortTxt = cfd ? ` <span class="sep">·</span> ${s.allow_short ? "compra e venda a descoberto" : "só compras"}` : "";
  const extra = ` <span class="sep">·</span> ${trend ? "" : `perfil ${PROFILE_PT[s.profile] || s.profile} <span class="sep">·</span> `}${radar}${shortTxt}${trend ? "" : ` <span class="sep">·</span> ${risk}`}${exch}`;
  const mk = stocks ? "AÇÕES & ETFs · " : cfd ? "CFDs · " : "CRIPTO · ";
  const demoName = { stocks: "conta demo da Trading 212", cfd: "conta demo do cTrader" }[s.market] || "Binance Testnet";
  const banner = $("#modeBanner");
  let html;
  if (modeKind(s) === "paper") {
    html = `${icon("flask")}<span><b>${mk}MODO TESTE</b> <span class="sep">·</span> dinheiro fictício com preços reais${extra}</span>`;
  } else if (modeKind(s) === "testnet") {
    html = `${icon("shield")}<span><b>${mk}MODO REAL · DEMO</b> <span class="sep">·</span> dinheiro fictício na ${demoName} <span class="sep">·</span> capital ${fmtMoney(s.capital_limit, 0)} ${s.quote}${extra}</span>`;
  } else {
    const bal = s.exchange_usdt != null ? ` <span class="sep">·</span> saldo livre ${fmtMoney(s.exchange_usdt)} ${s.quote}` : "";
    html = `${icon("alert")}<span><b>${mk}MODO REAL</b> <span class="sep">·</span> DINHEIRO REAL ${cfd ? "no" : "na"} ${BROKER_PT[s.market]} <span class="sep">·</span> capital máximo ${fmtMoney(s.capital_limit, 0)} ${s.quote}${bal}${extra}</span>`;
  }
  if (banner.dataset.html !== html) { banner.dataset.html = html; banner.innerHTML = html; }

  // alertas
  const alert = $("#alertBox");
  const alertKey = s.halted ? `h|${s.halt_reason}` : s.error ? `e|${s.error}` : "";
  if (alert.dataset.key !== alertKey) {
    alert.dataset.key = alertKey;
    if (s.halted) {
      alert.hidden = false;
      alert.innerHTML = `${icon("alert")}<span><b>Novas entradas bloqueadas:</b> ${esc(s.halt_reason)}.</span><button class="btn btn-sm btn-secondary" id="btnResume">Desbloquear</button>`;
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

function renderTrendCard(s) {
  const box = $("#focusBox"), t = s.trend;
  const key = JSON.stringify([t && t.time, s.running]);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  $("#regimeBadge").innerHTML = "";
  const intro = `<div class="focus-view">Estratégia de tendência: fica com os ${esc(String(Object.keys((t && t.targets) || {}).length || 3))} ETFs com melhor momentum (1, 3, 6 e 12 meses) que estão acima da média de 200 dias; o resto fica em "dinheiro" (XEON). Reequilibra ${t && t.due === false ? "no início de cada período" : "quando é devido"}, sem IA.</div>`;
  if (!t || !t.scores) {
    box.innerHTML = intro + `<div class="focus-foot">Ainda sem avaliação: inicia o bot ou clica em "Analisar agora" (com a bolsa aberta).</div>`;
    return;
  }
  const targets = t.targets || {};
  const rows = t.scores.map((r) => `<tr class="trend-row ${targets[r.symbol] ? "target" : ""}" data-go="${esc(r.symbol)}" style="cursor:pointer">
      <td><b>${esc(r.symbol.replace(".DE", ""))}</b></td>
      <td class="r ${cls(r.momentum)}">${fmtPct(r.momentum, 1)}</td>
      <td class="r ${cls(r.dist_sma_pct)}">${fmtPct(r.dist_sma_pct, 1)}</td>
      <td class="r">${targets[r.symbol] ? `<span class="w-tag">${Math.round(targets[r.symbol] * 100)}%</span>` : r.eligible ? '<span class="muted">elegível</span>' : '<span class="muted">—</span>'}</td></tr>`).join("");
  box.innerHTML = intro + `<table class="trend-table"><thead><tr><th>ETF</th><th class="r">Momentum</th><th class="r">vs MM200</th><th class="r">Alvo</th></tr></thead><tbody>${rows}</tbody></table>`
    + (t.notes && t.notes.length ? `<div class="focus-avoid">${esc(t.notes.join("; "))}</div>` : "")
    + `<div class="focus-foot"><span class="ago">Avaliado ${timeAgo(t.time)}</span></div>`;
  $$("[data-go]", box).forEach((el) => (el.onclick = () => selectSymbol(el.dataset.go)));
}

function renderFocus(s) {
  if (s.market === "stocks" && s.strategy === "tendencia") return renderTrendCard(s);
  const f = s.focus, r = s.regime;
  const key = JSON.stringify([f && f.time, r && r.state, s.auto_select, s.running, s.market]);
  const box = $("#focusBox");
  if (box.dataset.key === key) {
    const ago = $(".focus-foot .ago", box);
    if (ago && f) ago.textContent = `Atualizado ${timeAgo(f.time)}`;
    return;
  }
  box.dataset.key = key;
  $("#regimeBadge").innerHTML = r ? `<span class="regime ${r.state}" title="${r.breadth_above_ema50_pct}% das moedas acima da média de 50 no timeframe maior">${REGIME_PT[r.state]}</span>` : "";
  const intro = s.auto_select
    ? `Modo automático: em cada análise o bot examina ${s.market === "stocks" ? "as ações e ETFs mais negociados com a bolsa aberta" : s.market === "cfd" ? "o ouro, a prata, o forex, os índices e o petróleo com o mercado aberto, a subir e a descer," : `as ${s.scanner.universe} moedas mais negociadas`} e escolhe sozinho os melhores ${s.scanner.top} para a IA analisar a fundo.`
    : "Modo manual: a IA analisa os teus pares fixos e as melhores do radar.";
  if (!f) {
    box.innerHTML = `<div class="focus-view">${esc(intro)}</div><div class="focus-foot">Ainda sem análise: inicia o bot ou clica em "Analisar agora".</div>`;
    return;
  }
  const metrics = r ? `<div class="focus-metrics"><span><b>${r.breadth_up_pct}%</b> ${s.market === "crypto" ? "das moedas" : "dos ativos"} a subir</span>
      <span>${esc(r.benchmark || s.benchmark_label)}: <b>${TREND_PT[r.benchmark_trend || r.btc_trend] || "—"}</b></span><span>média 24h <b class="${cls(r.avg_change_24h_pct)}">${fmtPct(r.avg_change_24h_pct)}</b></span></div>` : "";
  const view = f.view ? `<div class="focus-view">${esc(f.view)}${f.stance ? ` <span class="muted">(postura ${STANCE_PT[f.stance] || f.stance})</span>` : ""}</div>` : "";
  const picks = f.picks.length
    ? f.picks.map((p, i) => `<div class="pick" data-go="${esc(p.symbol)}">
        <span class="n">${i + 1}</span>
        <div><div class="name">${esc(base(p.symbol))}${p.setup ? `<span>${esc(p.setup)}</span>` : ""}</div><div class="why">${esc(p.reason)}</div></div>
        <span class="sc">${p.score ?? ""}</span></div>`).join("")
    : `<div class="empty" style="padding:10px"><b>Nenhuma oportunidade boa agora</b>O bot espera pela próxima análise sem gastar IA.</div>`;
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
  sub.textContent = s.market === "stocks" && s.strategy === "tendencia" ? "tendência de ETFs · sem IA"
    : `a cada ${s.timeframe} · ${PROFILE_PT[s.profile] || s.profile}`;
  if (s.running && s.market !== "crypto" && /fechad/i.test(s.activity)) { el.innerHTML = `<span class="muted">${s.market === "cfd" ? "mercados fechados" : "bolsas fechadas"}</span>`; return; }
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

function positionRange(p) {  // 0% = stop, 100% = alvo (funciona para compras e vendas a descoberto)
  const far = p.take_profit || (p.side === "short" ? Math.min(p.price, p.entry_price) : Math.max(p.price, p.entry_price));
  const span = far - p.stop || 1;
  const at = (v) => Math.min(100, Math.max(0, ((v - p.stop) / span) * 100));
  return { e: at(p.entry_price), c: at(p.price) };
}

function renderPositions(s) {
  $("#posCount").textContent = `${s.positions.length}/${s.max_positions}`;
  $("#btnCloseAll").hidden = s.positions.length < 2;
  const host = $("#positions");
  const key = s.positions.map((p) => `${p.symbol}|${p.side}|${p.stop}|${p.take_profit}|${p.partial_done}|${p.let_run}`).join(",") + s.quote;
  if (host.dataset.key !== key) {
    host.dataset.key = key;
    if (!s.positions.length) {
      host.innerHTML = `<div class="empty"><b>Sem posições abertas</b>O bot ${s.market === "cfd" ? "abre posições (compra ou venda)" : "compra"} quando a IA encontra uma oportunidade que passa as regras de risco.</div>`;
      return;
    }
    host.innerHTML = s.positions.map((p) => {
      const trendPos = p.setup === "tendencia" || !(p.stop > 0);
      const badges = [p.partial_done ? `<span class="mini-badge good">metade ${s.market === "cfd" ? "fechada" : "vendida"}</span>` : "",
                      p.let_run ? '<span class="mini-badge good">a deixar correr</span>' : "",
                      p.rules_note ? `<span class="mini-badge">${esc(p.rules_note)}</span>` : ""].join("");
      return `<div class="position" data-sym="${esc(p.symbol)}">
      <div class="position-top"><span class="position-sym">${esc(p.symbol)}${s.market === "cfd" ? sideBadge(p.side) : ""}</span><span class="position-pnl"></span></div>
      <div class="position-meta"><span class="pm-left"></span><span class="pm-r"></span></div>
      ${trendPos ? `<div class="pos-note">Estratégia de tendência: mantém enquanto estiver entre os melhores e acima da média de 200 dias. Entrada ${fmtPrice(p.entry_price)}.</div>` : `
      <div class="range" title="Posição do preço entre o stop e o alvo">
        <div class="range-fill"></div><span class="range-mark" title="Entrada"></span><span class="range-cur" title="Preço atual"></span>
      </div>
      <div class="range-labels"><span class="stop">Stop ${fmtPrice(p.stop)}</span><span>Entrada ${fmtPrice(p.entry_price)}</span><span class="tp">${p.take_profit ? `Alvo ${fmtPrice(p.take_profit)}` : "Sem alvo (a correr)"}</span></div>
      ${badges ? `<div class="chip-row">${badges}</div>` : ""}`}
      <div class="position-actions"><span class="pm-age"></span>
        <button class="btn btn-sm btn-danger-ghost" data-close="${esc(p.symbol)}">Fechar</button></div>
    </div>`;
    }).join("");
    $$("[data-close]", host).forEach((b) => (b.onclick = () => closePosition(b.dataset.close, b)));
  }
  for (const p of s.positions) {
    const el = host.querySelector(`.position[data-sym="${CSS.escape(p.symbol)}"]`);
    if (!el) continue;
    const pnl = $(".position-pnl", el);
    pnl.className = `position-pnl ${cls(p.pnl)}`;
    pnl.innerHTML = `${fmtSigned(p.pnl)} ${s.quote}<small>${fmtPct(p.pnl_pct)}</small>`;
    $(".pm-left", el).textContent = p.margin != null
      ? `${fmtQty(p.qty)} un. · exposição ${fmtMoney(p.notional, 0)} ${s.quote} · margem ${fmtMoney(p.margin)}`
      : `${fmtQty(p.qty)} ${base(p.symbol)} · ${fmtMoney(p.value)} ${s.quote}`;
    $(".pm-r", el).textContent = p.r != null ? fmtSigned(p.r) + "R" : "";
    const fill = $(".range-fill", el);
    if (fill && p.stop > 0) {
      const { e, c } = positionRange(p);
      fill.className = `range-fill ${(p.side === "short" ? p.price <= p.entry_price : p.price >= p.entry_price) ? "up" : "down"}`;
      fill.style.left = `${Math.min(e, c)}%`; fill.style.width = `${Math.abs(c - e)}%`;
      $(".range-mark", el).style.left = `${e}%`;
      $(".range-cur", el).style.left = `${c}%`;
    }
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
      const what = { stocks: "Analisa as ações e ETFs com a bolsa aberta", cfd: "Analisa o ouro, a prata, o forex, os índices e o petróleo (a subir e a descer)" }[S.status?.market]
        || `Analisa as ${S.status?.scanner.universe || 25} moedas mais negociadas em ${Q()}`;
      $("#radarTable").innerHTML = `<div class="empty"><b>Radar ainda sem dados</b>${what} e escolhe as melhores para a IA.</div>`;
      return;
    }
    info.textContent = `${r.results.length} de ${r.universe} ${{ stocks: "ativos (bolsa aberta)", cfd: "instrumentos (mercado aberto)" }[S.status?.market] || "moedas"} · atualizado ${timeAgo(new Date(r.time * 1000).toISOString())} · as de pontuação mais alta vão à IA`;
    const ORDER_TXT = { BUY_LIMIT: "recuo", BUY_STOP: "rompimento", SHORT_LIMIT: "repique", SHORT_STOP: "quebra", SHORT: "venda" };
    $("#radarTable").innerHTML = `<table><thead><tr><th>#</th><th>${S.status?.market === "crypto" ? "Moeda" : "Ativo"}</th><th></th><th>Pontuação</th><th>Setup detetado</th><th>Tendência maior</th>
      <th class="r">Preço</th><th class="r">24h</th><th class="r">vs ${esc(S.status?.benchmark_label || "BTC")}</th><th class="r">Volume</th></tr></thead><tbody>` +
      r.results.map((x, i) => `<tr class="clickable ${x.picked ? "picked" : ""}" data-sym="${esc(x.symbol)}" title="${esc(x.pick_reason || "")}">
        <td class="muted">${i + 1}</td><td><b>${esc(base(x.symbol))}</b>${x.mine ? ' <span class="star" title="Na tua lista">★</span>' : ""}</td>
        <td>${x.picked ? `<span class="pick-tag">${icon("check")}Escolhida</span>` : ""}</td>
        <td><div class="score"><div class="score-bar"><span style="width:${x.score}%"></span></div><b>${x.score}</b></div></td>
        <td>${x.setup ? `${/^SHORT/.test(x.setup.order) ? sideBadge("short") + " " : ""}${esc(x.setup.label)}${x.setup.order !== "BUY" && x.setup.order !== "SHORT" ? ` <span class="muted small">(${ORDER_TXT[x.setup.order] || ""} ${fmtPrice(x.setup.entry)})</span>` : ""}` : '<span class="muted">—</span>'}</td>
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
    $("#newsList").innerHTML = `<div class="empty"><span class="spinner" style="display:inline-block;margin-bottom:6px"></span><br>A recolher notícias…</div>`;
  }
  try {
    const n = await api("/api/news");
    $("#newsList").innerHTML = n.headlines.length ? n.headlines.map((h) => `
      <a class="news-item" href="${esc(h.link)}" target="_blank" rel="noopener">
        <span class="age">${h.age_h < 1 ? Math.round(h.age_h * 60) + " min" : nf(0).format(h.age_h) + " h"}</span>
        <span><div class="title">${esc(h.title)}</div><div class="src">${esc(h.source)}</div></span></a>`).join("")
      : `<div class="empty">Sem manchetes (verifica a ligação à internet).</div>`;
    const g = n.global, v = n.vix;
    if (n.market !== "crypto") {
      const rows = g ? Object.entries(g).filter(([k]) => !(v && k === "vix")).map(([k, x]) => `<div>${esc(x.label)}<b>${nf(k === "eurusd" ? 4 : x.last >= 100 ? 1 : 2).format(x.last)} <span class="${cls(x.change_1d_pct)}">${fmtPct(x.change_1d_pct)}</span></b></div>`).join("") : "";
      $("#globalBox").innerHTML = (v ? `<div>VIX (medo das bolsas)<b>${nf(1).format(v.vix)} · ${esc(v.label)}</b></div>` : "") + (rows || `<div>Indisponível</div>`);
    } else
    $("#globalBox").innerHTML = (v ? `<div>VIX (medo das bolsas)<b>${nf(1).format(v.vix)} · ${esc(v.label)}</b></div><div>VIX 5 dias<b class="${cls(-v.vix_5d_change_pct)}">${fmtPct(v.vix_5d_change_pct, 1)}</b></div>` : "") + (g ? `
      <div>Capitalização total<b>${nf(0).format(g.total_market_cap_usd_bn)} mil M$</b></div>
      <div>Variação 24h<b class="${cls(g.market_cap_change_24h_pct)}">${fmtPct(g.market_cap_change_24h_pct)}</b></div>
      <div>Dominância BTC<b>${nf(1).format(g.btc_dominance_pct)}%</b></div>
      <div>Dominância ETH<b>${nf(1).format(g.eth_dominance_pct)}%</b></div>` : `<div>Indisponível</div>`);
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
      const levels = ENTRY_ACTIONS.includes(d.action) && d.stop_loss
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
        <td>${esc(fmtTime(t.closed_at))}</td><td><b>${esc(t.symbol)}</b>${t.side === "short" ? sideBadge("short") : ""}</td>
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
        title: /testnet|demo/.test(s.mode_key) ? "Iniciar na conta demo?" : "Iniciar com dinheiro real?",
        text: /testnet|demo/.test(s.mode_key)
          ? `O bot vai enviar ordens para a ${{ stocks: "conta demo da Trading 212", cfd: "conta demo do cTrader" }[s.market] || "Binance Testnet"} (dinheiro fictício), usando até <b>${fmtMoney(s.capital_limit, 0)} ${s.quote}</b>.`
          : `O bot vai ${s.market === "cfd" ? "abrir e fechar CFDs (compra e venda)" : "comprar e vender"} na tua conta ${BROKER_PT[s.market]} com até <b>${fmtMoney(s.capital_limit, 0)} ${s.quote}</b> de dinheiro real.`,
        ok: "Iniciar", danger: /live/.test(s.mode_key),
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
  const ok = await confirmDialog({ title: `Fechar ${symbol}?`, text: "A posição é fechada a preço de mercado agora.", ok: "Fechar posição", danger: true });
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
  const ok = await confirmDialog({ title: "Fechar todas as posições?", text: "Todas as posições são fechadas a preço de mercado agora.", ok: "Fechar tudo", danger: true });
  if (!ok) return;
  try {
    await withBusy($("#btnCloseAll"), () => api("/api/positions/close-all", { method: "POST" }));
    toast("Todas as posições foram fechadas.", "ok");
  } catch (e) { toast(e.message, "err", 8000); }
  refreshStatus();
}

async function resumeTrading() {
  const ok = await confirmDialog({ title: "Desbloquear novas entradas?", text: "O limite de queda foi atingido. Confirma que queres que o bot volte a abrir posições.", ok: "Desbloquear" });
  if (!ok) return;
  try { await api("/api/resume", { method: "POST" }); toast("Entradas desbloqueadas.", "ok"); } catch (e) { toast(e.message, "err"); }
  refreshStatus();
}

function exchangesSummary(list, cfd = false) {
  const open = list.filter((e) => e.open), closed = list.filter((e) => !e.open);
  const tip = (arr) => esc(arr.map((e) => `${e.name} ${e.hours}`).join("\n"));
  const g = cfd ? "o" : "a";  // CFDs: "Forex aberto"; bolsas: "Xetra aberta"
  const parts = open.map((e) => `<span class="exch open" title="${esc(e.name)} ${esc(e.hours)}">${esc(e.short || e.name)} abert${g}</span>`);
  if (closed.length) {
    const label = open.length ? `${closed.length} fechad${g}${closed.length > 1 ? "s" : ""}` : closed.length > 1 ? (cfd ? "mercados fechados" : `todas as ${closed.length} bolsas fechadas`) : `${esc(closed[0].short || closed[0].name)} fechad${g}`;
    parts.push(`<span class="exch" title="${tip(closed)}">${label}</span>`);
  }
  return parts.join(" ");
}

/* ============================================================ gerir ativos */
const TYPE_PT = { EQUITY: "Ação", ETF: "ETF/ETC", CRYPTO: "Cripto", CFD: "CFD" };

async function openAssets() {
  $("#assetsModal").hidden = false;
  $("#assetQuery").value = "";
  $("#assetResults").innerHTML = "";
  const market = S.status?.market;
  $("#assetsSubtitle").textContent = {
    stocks: "Ações e ETFs que o bot analisa. Qualquer ativo passa pelas mesmas verificações antes de entrar.",
    cfd: "Ouro, prata, forex, índices e petróleo (CFDs na cTrader). Cada instrumento é verificado antes de entrar.",
  }[market] || `Criptomoedas que o bot analisa (pares em ${Q()} na Binance).`;
  $("#assetQuery").placeholder = { stocks: "Nome ou ticker: Rheinmetall, ASML, ouro, EDP, NVDA...", cfd: "Ouro, XAUUSD, EURUSD, DAX, Nasdaq, petróleo..." }[market] || "Moeda: PAXG, PEPE, TAO...";
  $("#assetQueryHelp").textContent = {
    stocks: "Pesquisa no Yahoo Finance. Aceita ações americanas e europeias (Xetra, Paris, Amesterdão, Lisboa, Madrid, Milão, Zurique, Copenhaga...) e ETFs/ETCs europeus. ETFs americanos (SPY, QQQ) não são permitidos a particulares na UE.",
    cfd: "Pesquisa no catálogo e, com a conta cTrader ligada, em todos os instrumentos da tua corretora. O Modo Teste usa preços do Yahoo (futuros e índices), que podem diferir uns pontos dos da corretora.",
  }[market] || "Procura entre todos os pares da Binance. O radar já vê sozinho as mais negociadas; aqui juntas as que queres acompanhar sempre.";
  setTimeout(() => $("#assetQuery").focus(), 50);
  await loadAssets();
}

function closeAssets() { $("#assetsModal").hidden = true; S.lastDecisionSig = ""; refreshStatus(); }

async function loadAssets() {
  try {
    const a = await api("/api/assets");
    S.assets = a;
    renderAssetsMine(a);
    renderAssetsCatalog(a);
  } catch (e) { toast(e.message, "err"); }
}

function renderAssetsMine(a) {
  $("#assetMineCount").textContent = `(${a.mine.length}/${a.max})`;
  $("#assetMine").innerHTML = a.mine.length ? a.mine.map((m) => `<span class="asset-chip" title="${esc([m.name, m.exchange, m.note].filter(Boolean).join(" · "))}">
      <b>${esc(m.symbol)}</b>${m.name && m.name !== m.symbol ? `<span class="nm">${esc(m.name)}</span>` : ""}
      <button data-remove="${esc(m.symbol)}" title="Remover">${icon("x")}</button></span>`).join("")
    : `<span class="muted small">Ainda não tens ativos na tua lista.</span>`;
  $$("[data-remove]", $("#assetMine")).forEach((b) => (b.onclick = () => removeAsset(b.dataset.remove)));
}

function renderAssetsCatalog(a) {
  const host = $("#assetCatalog");
  if (a.market === "stocks" || a.market === "cfd") {
    const cfd = a.market === "cfd";
    host.innerHTML = `<h3>Catálogo do radar <span class="count">(${a.universe_size} ${cfd ? "instrumentos" : "ativos"})</span></h3>
      <div class="help">${cfd ? "Categorias que o radar examina automaticamente, à procura de compras e de vendas a descoberto." : `Categorias que o radar examina automaticamente. Em cada análise escolhe as ${a.scan_top} mais negociadas entre as bolsas abertas nesse momento.`}</div>
      <div class="cat-list">${a.categories.map((c) => `<label class="cat-item"><input type="checkbox" data-cat="${c.key}" ${c.enabled ? "checked" : ""}>
        <span>${esc(c.label)} (${c.symbols.length})<span class="ex">${esc(c.symbols.slice(0, 6).join(", "))}${c.symbols.length > 6 ? "…" : ""}</span></span></label>`).join("")}</div>
      ${cfd ? `<div class="help" style="margin-top:10px">${a.broker ? "Conta cTrader ligada: podes adicionar qualquer instrumento da tua corretora (fora do catálogo só funciona no Modo Real)." : "Liga a conta cTrader (Modo Real) para adicionares também instrumentos da tua corretora que não estão no catálogo."}</div>`
        : `<label class="cat-item" style="margin-top:12px"><input type="checkbox" id="assetDiscovery" ${a.discovery ? "checked" : ""}>
        <span>Descobertas do dia<span class="ex">Junta as ações americanas grandes (mais de 5 mil milhões de dólares) mais ativas e a subir no dia${a.discoveries.length ? `: ${esc(a.discoveries.slice(0, 8).join(", "))}` : ""}.</span></span></label>`}`;
    $$("[data-cat]", host).forEach((cb) => (cb.onchange = saveCategories));
    if ($("#assetDiscovery")) $("#assetDiscovery").onchange = saveCategories;
  } else {
    host.innerHTML = `<h3>Sugestões</h3>` + a.catalog.map((c) => `<div class="help" style="margin-top:8px">${esc(c.label)}</div>
      <div class="quick-add">${c.symbols.map((sym) => `<button class="chip ${a.mine.some((m) => m.symbol === sym) ? "on" : ""}" data-quick="${sym}">${sym}</button>`).join("")}</div>`).join("")
      + `<div class="help" style="margin-top:10px">O ouro "XAU/USD" é um CFD (produto alavancado) que a Binance não tem e que a API da Trading 212 não permite. O PAXG e o XAUT valem cada um 1 onça de ouro físico e negoceiam 24/7.</div>`;
    $$("[data-quick]", host).forEach((b) => (b.onclick = () => (b.classList.contains("on") ? removeAsset(b.dataset.quick) : addAsset(b.dataset.quick, b))));
  }
}

let assetSearchTimer = null;
function onAssetQuery() {
  clearTimeout(assetSearchTimer);
  const q = $("#assetQuery").value.trim();
  if (q.length < 2) { $("#assetResults").innerHTML = ""; return; }
  assetSearchTimer = setTimeout(async () => {
    $("#assetResults").innerHTML = `<div class="check-item muted"><span class="spinner"></span>A procurar…</div>`;
    try {
      const { results } = await api(`/api/assets/search?q=${encodeURIComponent(q)}`);
      if ($("#assetQuery").value.trim() !== q) return;
      $("#assetResults").innerHTML = results.length ? results.map((r) => `<div class="asset-row">
          <div><span class="sym">${esc(r.symbol)}</span><span class="nm">${r.name && r.name !== r.symbol ? esc(r.name) : ""}</span>
            <div class="meta">${esc(TYPE_PT[r.type] || r.type || "")} · ${esc(r.exchange || "")}${r.volume_24h ? ` · volume 24h ${nf(1).format(r.volume_24h / 1e6)} M` : ""}
              ${r.why ? ` · <span class="${r.ok ? "warn" : "bad"}">${esc(r.why)}</span>` : ""}</div></div>
          <button class="btn btn-sm ${r.in_list ? "btn-secondary" : "btn-primary"}" data-add="${esc(r.symbol)}" ${!r.ok || r.in_list ? "disabled" : ""}>${r.in_list ? "Na lista" : "Adicionar"}</button>
        </div>`).join("") : `<div class="muted small">Nada encontrado para "${esc(q)}".</div>`;
      $$("[data-add]", $("#assetResults")).forEach((b) => (b.onclick = () => addAsset(b.dataset.add, b)));
    } catch (e) { $("#assetResults").innerHTML = `<div class="check-item bad">${icon("alert")}<span>${esc(e.message)}</span></div>`; }
  }, 350);
}

async function addAsset(symbol, btn) {
  try {
    const r = await withBusy(btn, () => api("/api/assets/add", { method: "POST", body: { symbol } }));
    if (!r) return;
    if (!r.ok) { toast(`${symbol}: ${r.problems.join("; ")}`, "err", 9000); return; }
    toast(`${r.symbol} adicionado${r.name && r.name !== r.symbol ? ` (${r.name})` : ""}.${r.warnings.length ? " Atenção: " + r.warnings.join("; ") + "." : ""}`, r.warnings.length ? "info" : "ok", r.warnings.length ? 9000 : 4500);
    if (btn && btn.dataset.add) { btn.textContent = "Na lista"; btn.disabled = true; btn.className = "btn btn-sm btn-secondary"; }
    await loadAssets();
  } catch (e) { toast(e.message, "err", 8000); }
}

async function removeAsset(symbol) {
  try {
    await api("/api/assets/remove", { method: "POST", body: { symbol } });
    toast(`${symbol} removido da tua lista.`, "ok");
    $$("[data-add]", $("#assetResults")).filter((b) => b.dataset.add === symbol)
      .forEach((b) => { b.textContent = "Adicionar"; b.disabled = false; b.className = "btn btn-sm btn-primary"; });
    await loadAssets();
  } catch (e) { toast(e.message, "err"); }
}

async function saveCategories() {
  const categories = $$("[data-cat]").filter((c) => c.checked).map((c) => c.dataset.cat);
  try {
    const r = await api("/api/assets/categories", { method: "POST", body: { categories, discovery: $("#assetDiscovery") ? $("#assetDiscovery").checked : null } });
    $("#assetsNote").textContent = `O radar vê agora ${r.universe_size} ativos do catálogo.`;
    await loadAssets();
  } catch (e) { toast(e.message, "err"); await loadAssets(); }
}

/* ============================================================ mercados */
async function onMarketClick(market) {
  const s = S.status;
  if (!s || market === s.market) return;
  if (s.running) {
    const ok = await confirmDialog({ title: `Mudar para ${MARKET_PT[market]}?`,
      text: "O bot vai ser parado. Cada mercado tem a sua carteira e histórico; o novo mercado começa em Modo Teste.", ok: "Parar e mudar" });
    if (!ok) return;
  }
  try {
    await api("/api/market", { method: "POST", body: { market } });
    toast(`Mercado: ${MARKET_PT[market]} (Modo Teste).`, "ok");
  } catch (e) { toast(e.message, "err"); return; }
  S.symbol = null; S.lastDecisionSig = "";
  ["#focusBox", "#aiList", "#positions", "#pendingList", "#symbolTabs"].forEach((id) => { $(id).dataset.key = "-"; $(id).dataset.sig = "-"; });
  await refreshStatus(); refreshCandles(true); setTab(S.tab);
}

/* ============================================================ modos */
async function onModeClick(mode) {
  const s = S.status;
  if (!s || mode === s.mode) return;
  if (mode === "paper") {
    if (s.running) {
      const ok = await confirmDialog({ title: "Mudar para o Modo Teste?", text: `O bot em Modo Real vai ser parado. As posições reais abertas ficam na tua conta ${broker()} sem vigilância do bot até voltares ao Modo Real${s.market === "cfd" ? " (o stop-loss fica na corretora)" : ""}.`, ok: "Parar e mudar" });
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

function liveBrokerTexts() {
  const stocks = S.status.market === "stocks", cfd = S.status.market === "cfd";
  $("#liveIpBox").hidden = cfd;
  $("#liveCtrader").hidden = !cfd;
  $("#liveAccept1").nextElementSibling.textContent = cfd
    ? "Compreendo que os CFDs usam alavancagem, que posso perder parte ou todo este capital e que nenhum resultado é garantido."
    : "Compreendo que posso perder parte ou todo este capital e que nenhum resultado é garantido.";
  $("#liveAccept2").nextElementSibling.textContent = cfd
    ? "Compreendo que o stop-loss fica na corretora, mas o resto da gestão (trailing, fecho parcial, fecho de sexta) só funciona com a aplicação aberta e o PC ligado."
    : "Compreendo que os stops só são vigiados enquanto a aplicação estiver aberta e o PC ligado.";
  if (cfd) {
    $("#liveSubtitle").textContent = "O bot vai abrir e fechar CFDs (ouro, forex, índices, petróleo; compra e venda a descoberto) na tua conta cTrader.";
    $("#liveKeysTitle").textContent = "Ligação à cTrader";
    $("#liveKeysHelp").innerHTML = "A cTrader liga-se por autorização (OAuth): <b>nunca</b> escreves aqui a palavra-passe da corretora, e a API não permite levantamentos. Precisas de uma aplicação gratuita no portal da cTrader (uma vez só).";
    $("#liveTestnetLabel").innerHTML = "<b>Usar uma conta demo cTrader</b>: ordens a sério com dinheiro fictício, para testar primeiro (recomendado).";
    return;
  }
  $("#liveSubtitle").textContent = stocks ? "O bot vai comprar e vender ações e ETFs com o teu dinheiro na Trading 212." : "O bot vai comprar e vender com o teu dinheiro na Binance.";
  $("#liveKeysTitle").textContent = stocks ? "Chaves API da Trading 212" : "Chaves API da Binance";
  $("#liveSecretLabel").textContent = stocks ? "API Secret" : "Secret Key";
  $("#liveKeysHelp").innerHTML = stocks
    ? `Na app Trading 212: Definições › API (Beta) › Gerar chave, com permissão para <b>ver a conta</b> e <b>colocar ordens</b>. Só funciona em contas <b>Invest</b>. A conta tem de estar em <b>${esc(S.status.quote)}</b>.`
    : `Cria as chaves em Binance › Gestão de API. Ativa só <b>Leitura</b> e <b>Trading spot</b>. <b>Nunca</b> ativas levantamentos. O bot negoceia em <b>${esc(S.status.quote)}</b> (na UE a Binance não permite USDT).`;
  $("#liveTestnetLabel").innerHTML = stocks
    ? "<b>Usar a conta demo da Trading 212</b>: ordens a sério com dinheiro fictício, para testar primeiro (gera a chave com a conta demo selecionada na app)."
    : "<b>Usar a Binance Testnet</b>: ordens reais com dinheiro fictício, para testar primeiro. As chaves são criadas em testnet.binance.vision.";
  $("#liveIpHelp").innerHTML = stocks
    ? "Opcional na Trading 212: se restringires a chave a IPs, usa este."
    : "Na Binance: <b>Gestão de API › Editar restrições › Restrict access to trusted IPs only</b> › cola este IP › Confirmar. "
      + "É obrigatório para poderes ativar <b>Enable Spot &amp; Margin Trading</b>. O 127.0.0.1 da janela não serve: é este o IP que a Binance vê. "
      + "Se o IP da tua internet mudar (por exemplo, ao reiniciar o router), tens de o atualizar lá.";
}

async function loadPublicIp() {
  $("#liveIp").textContent = "a obter…";
  try {
    const { ip } = await api("/api/public-ip");
    $("#liveIp").textContent = ip || "não foi possível obter (vê em whatismyip.com)";
    S.publicIp = ip;
  } catch { $("#liveIp").textContent = "não foi possível obter (vê em whatismyip.com)"; }
}

async function copyIp() {
  if (!S.publicIp) return;
  try {
    await navigator.clipboard.writeText(S.publicIp);
    toast(`IP ${S.publicIp} copiado.`, "ok");
  } catch {
    const r = document.createRange(); r.selectNodeContents($("#liveIp"));
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
    toast("Seleciona e copia o IP com Ctrl+C.", "info");
  }
}

function openLiveModal() {
  const s = S.status;
  S.liveCheck = null;
  liveBrokerTexts();
  if (s.market === "cfd") loadCtrader(); else loadPublicIp();
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

function savedKeyFor(testnet) {
  const k = S.status.keys;
  if (S.status.market === "cfd") return testnet ? k.ctrader_demo : k.ctrader_live;
  if (S.status.market === "stocks") return testnet ? k.t212_demo : k.t212;
  return testnet ? k.testnet : k.binance;
}

function updateLiveKeyView() {
  if (S.status.market === "cfd") { $("#liveSavedKey").hidden = true; $("#liveKeyInputs").hidden = true; return; }
  const saved = savedKeyFor($("#liveTestnet").checked);
  const box = $("#liveSavedKey");
  box.hidden = !saved;
  $("#liveKeyInputs").hidden = !!saved;
  if (saved) {
    box.innerHTML = `<span>${icon("lock")} Chave guardada <span class="mono">${esc(saved)}</span></span><button class="btn btn-sm btn-secondary" id="btnChangeKey">Alterar</button>`;
    $("#btnChangeKey").onclick = () => { box.hidden = true; $("#liveKeyInputs").hidden = false; $("#liveApiKey").focus(); };
  }
}

/* ---------------------------------------------------------------- cTrader (CFDs) */
async function loadCtrader() {
  let st;
  try { st = await api("/api/ctrader/status"); } catch (e) { toast(e.message, "err"); return; }
  S.ct = st;
  $("#ctRedirect").textContent = st.redirect_uri;
  $("#ctPortal").href = st.portal;
  $("#ctAppSaved").hidden = !st.app;
  $("#ctAppInputs").hidden = !!st.app;
  if (st.app) {
    $("#ctAppSaved").innerHTML = `<span>${icon("lock")} Aplicação guardada <span class="mono">${esc(st.client_id || "")}</span></span><button class="btn btn-sm btn-secondary" id="btnCtChangeApp" type="button">Alterar</button>`;
    $("#btnCtChangeApp").onclick = () => { $("#ctAppSaved").hidden = true; $("#ctAppInputs").hidden = false; $("#ctClientId").focus(); };
  }
  $("#btnCtConnect").disabled = !st.app;
  $("#btnCtConnect").innerHTML = `${icon("link")}${st.token ? "Autorizar de novo" : "Ligar ao cTrader"}`;
  $("#ctTokenState").innerHTML = st.token
    ? `<span class="up">${icon("check")} Conta cTrader autorizada</span>${st.token_expires ? ` · renova-se sozinha (atual até ${new Date(st.token_expires * 1000).toLocaleDateString("pt-PT")})` : ""}`
    : st.app ? "Carrega em \"Ligar ao cTrader\" e autoriza no browser que vai abrir." : "Guarda primeiro a aplicação (Client ID e Secret).";
  $("#ctAccountBox").hidden = !st.token;
  if (st.token) await loadCtAccounts();
}

async function loadCtAccounts(refresh = false) {
  const sel = $("#ctAccount"), demo = $("#liveTestnet").checked;
  sel.innerHTML = `<option value="">A carregar as contas…</option>`;
  try {
    const r = await api(`/api/ctrader/accounts${refresh ? "?refresh=1" : ""}`);
    const list = r.accounts.filter((a) => a.is_live !== demo);
    const cur = demo ? r.demo_account : r.live_account;
    sel.innerHTML = list.length
      ? `<option value="">Escolhe a conta ${demo ? "demo" : "real"}…</option>` + list.map((a) =>
        `<option value="${a.id}" ${a.id === cur ? "selected" : ""}>${esc(a.broker || "cTrader")} · ${esc(String(a.login ?? a.id))} (${a.is_live ? "real" : "demo"})</option>`).join("")
      : `<option value="">Nenhuma conta ${demo ? "demo" : "real"} autorizada: autoriza de novo e seleciona-a</option>`;
  } catch (e) { sel.innerHTML = `<option value="">${esc(e.message)}</option>`; }
}

async function saveCtApp() {
  const body = { client_id: $("#ctClientId").value.trim(), client_secret: $("#ctClientSecret").value.trim() };
  if (!body.client_id || !body.client_secret) { toast("Preenche o Client ID e o Secret.", "err"); return; }
  try {
    const r = await withBusy($("#btnCtApp"), () => api("/api/ctrader/app", { method: "POST", body }));
    if (!r) return;
    $("#ctClientId").value = ""; $("#ctClientSecret").value = "";
    toast("Aplicação cTrader verificada e guardada.", "ok");
    await loadCtrader();
  } catch (e) { toast(e.message, "err", 9000); }
}

async function ctConnect() {
  try {
    const r = await withBusy($("#btnCtConnect"), () => api("/api/ctrader/connect", { method: "POST" }));
    if (!r) return;
    $("#ctTokenState").innerHTML = `<span class="spinner" style="display:inline-block;vertical-align:-2px"></span> À espera da autorização no browser… Se não abriu, <a href="${esc(r.url)}" target="_blank" rel="noopener">abre este link</a>.`;
    toast("Abriu o browser: entra com o teu cTrader ID, escolhe as contas e carrega em \"Allow access\". Depois volta aqui.", "info", 10000);
    const before = S.ct?.token_expires, t0 = Date.now();
    clearInterval(S.ctPoll);
    S.ctPoll = setInterval(async () => {
      if (Date.now() - t0 > 5 * 60 * 1000 || $("#liveModal").hidden) { clearInterval(S.ctPoll); return; }
      try {
        const st = await api("/api/ctrader/status");
        if (st.token && st.token_expires !== before) {
          clearInterval(S.ctPoll);
          toast("Conta cTrader autorizada. Escolhe agora a conta que o bot vai usar.", "ok", 7000);
          await refreshStatus();
          await loadCtrader();
        }
      } catch { /* tenta outra vez */ }
    }, 3000);
  } catch (e) { toast(e.message, "err", 8000); }
}

async function saveCtToken() {
  const body = { access_token: $("#ctAccess").value.trim(), refresh_token: $("#ctRefresh").value.trim() };
  try {
    const r = await withBusy($("#btnCtToken"), () => api("/api/ctrader/token", { method: "POST", body }));
    if (!r) return;
    $("#ctAccess").value = ""; $("#ctRefresh").value = ""; $("#ctManual").open = false;
    toast(`Tokens guardados: ${r.accounts.length} conta(s) cTrader encontradas.`, "ok");
    await refreshStatus();
    await loadCtrader();
  } catch (e) { toast(e.message, "err", 9000); }
}

async function verifyCtrader(pick = null) {
  const btn = $("#btnLiveVerify");
  $("#liveChecks").innerHTML = `<div class="check-item muted"><span class="spinner"></span>A verificar a conta cTrader…</div>`;
  try {
    const testnet = $("#liveTestnet").checked;
    const r = await withBusy(btn, () => (pick
      ? api("/api/ctrader/account", { method: "POST", body: { testnet, account_id: pick } })
      : api("/api/keys/ctrader", { method: "POST", body: { testnet } })));
    if (!r) return;
    S.liveCheck = r;
    const items = [];
    if (r.usdt_free != null) {
      items.push(["ok", `Ligado à ${r.broker || "corretora"} · conta ${r.login ?? r.account_id} (${r.testnet ? "demo" : "real"}) em ${r.account_currency || r.quote}`]);
      items.push(["ok", `Saldo: ${fmtMoney(r.usdt_free)} ${r.quote}${r.leverage ? ` · alavancagem máxima 1:${nf(0).format(r.leverage)}` : ""}`]);
      const names = { XAUUSD: "Ouro", EURUSD: "EUR/USD", US500: "S&P 500" };
      Object.entries(r.symbols || {}).forEach(([k, v]) => items.push(v ? ["ok", `${names[k] || k}: ${v}`] : ["info", `${names[k] || k}: não encontrado nesta corretora (o bot usa os outros instrumentos)`]));
      if (r.usdt_free < 300) items.push(["info", "Com menos de ~300 de saldo, o ouro (mínimo 1 onça) e alguns índices podem não caber no risco por trade: o bot salta esses e usa forex, que permite posições mais pequenas."]);
    }
    r.problems.forEach((t) => items.push(["bad", t]));
    $("#liveChecks").innerHTML = items.map(([k, t]) => `<div class="check-item ${k}">${icon(k === "ok" ? "check" : k === "info" ? "chevron" : "alert")}<span>${esc(t)}</span></div>`).join("");
    if (pick) await refreshStatus();
  } catch (e) {
    S.liveCheck = null;
    $("#liveChecks").innerHTML = `<div class="check-item bad">${icon("alert")}<span>${esc(e.message)}</span></div>`;
  }
  validateLive();
}

async function copyRedirect() {
  const uri = $("#ctRedirect").textContent;
  try { await navigator.clipboard.writeText(uri); toast("Redirect URI copiado.", "ok"); }
  catch {
    const r = document.createRange(); r.selectNodeContents($("#ctRedirect"));
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
    toast("Seleciona e copia com Ctrl+C.", "info");
  }
}

async function verifyLive(silent = false) {
  if ($("#btnLiveVerify").disabled) return;  // já há uma verificação a decorrer
  if (S.status.market === "cfd") return verifyCtrader();
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
    const r = await withBusy(btn, () => api(S.status.market === "stocks" ? "/api/keys/t212" : "/api/keys/binance", { method: "POST", body }));
    S.liveCheck = r;
    const items = [];
    if (r.usdt_free != null) {
      items.push(["ok", `Ligação OK${r.testnet ? " (demo)" : ""}${r.account_currency ? ` · conta em ${r.account_currency}` : ""}`]);
      items.push(["ok", `Saldo livre: ${fmtMoney(r.usdt_free)} ${r.quote || Q()}`]);
      if (!r.testnet && S.status.market !== "stocks") {
        items.push([r.withdrawals_enabled ? "bad" : "ok", r.withdrawals_enabled ? "Levantamentos ATIVADOS nesta chave" : "Levantamentos desativados"]);
        items.push([r.spot_trading_enabled ? "ok" : "bad", r.spot_trading_enabled ? "Trading spot ativo" : "Trading spot desativado"]);
      }
    }
    r.problems.filter((p) => r.usdt_free == null || !/LEVANTAMENTOS|spot/i.test(p)).forEach((p) => items.push(["bad", p]));
    if (r.withdrawals_enabled) items.push(["bad", "Desativa 'Enable Withdrawals' na Binance e verifica de novo."]);
    if (r.spot_trading_enabled === false) items.push(["bad", "Na Binance (Gestão de API › Editar restrições) escolhe 'Restrict access to trusted IPs only', "
      + `escreve o IP público da tua internet${r.public_ip ? ` (${r.public_ip})` : ""} e só depois ativa 'Enable Spot & Margin Trading'. `
      + "O 127.0.0.1 é só o endereço desta janela: não é o IP que a Binance vê."]);
    if (r.public_ip) { S.publicIp = r.public_ip; $("#liveIp").textContent = r.public_ip; }
    const quote = r.quote || Q();
    const capital = parseFloat($("#liveCapital").value) || 0;
    if (r.usdt_free != null && !r.testnet && S.status.market !== "stocks" && r.usdt_free < Math.max(10, capital)) {
      const fmtBal = (b) => Object.entries(b || {}).map(([c, v]) => `${nf(v >= 100 ? 2 : 6).format(v)} ${c}`).join(", ");
      const spotTxt = fmtBal(r.spot_balances);
      const fundTxt = fmtBal(r.funding_balances);
      items.push(["bad", `O bot negoceia em ${quote} e tens ${fmtMoney(r.usdt_free)} ${quote} livres na carteira Spot.`]);
      items.push(["info", `Carteira Spot: ${spotTxt || "vazia"}.`]);
      if (fundTxt) items.push(["info", `Carteira de Financiamento: ${fundTxt}. O bot só usa a Spot: na Binance vai a Carteira › Transferir e passa o dinheiro de Financiamento para Spot.`]);
      if (quote === "USDC") {
        items.push(["info", "Para teres USDC: na Binance vai a Negociar › Converter, escolhe de EUR (ou USDT/outra moeda) para USDC e confirma, com a carteira Spot selecionada. Se ainda não tens dinheiro na Binance, deposita EUR primeiro (Comprar cripto › Depositar)."]);
        if ((r.spot_balances || {}).EUR) items.push(["info", "Também podes pôr o bot a negociar em EUR (Definições › Mercado › Moeda de negociação), mas há muito menos moedas com par em euros."]);
      }
      items.push(["info", `Depois carrega em "Verificar ligação" outra vez. O capital do bot (passo 2) não pode ser maior do que o saldo em ${quote}.`]);
    }
    if (r.usdt_free == null && S.status.market !== "stocks") {
      const ip = r.public_ip || S.publicIp || "o IP acima";
      [
        `Se a chave tem restrição de IP, o IP ${ip} tem de estar na lista de IPs de confiança dessa chave (e carregar em Confirmar na Binance; às vezes demora 1-2 minutos).`,
        testnet ? "A opção Testnet está marcada: só funcionam chaves criadas em testnet.binance.vision, não as da Binance normal."
                : "A opção Testnet está desmarcada: só funcionam chaves da Binance normal, não as da testnet.binance.vision.",
        "A Secret Key só aparece uma vez, quando crias a chave. Se não a copiaste inteira, apaga a chave e cria outra.",
        "Cria a chave do tipo 'System generated' (HMAC), não 'Self-generated' (Ed25519/RSA).",
        "Confirma que 'Enable Reading' está ativo nessa chave.",
      ].forEach((t) => items.push(["info", t]));
      if (r.error_code) items.push(["info", `Código de erro da Binance: ${r.error_code}.`]);
    }
    $("#liveChecks").innerHTML = items.map(([k, t]) => `<div class="check-item ${k}">${icon(k === "ok" ? "check" : k === "info" ? "chevron" : "alert")}<span>${esc(t)}</span></div>`).join("");
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
  if (!c || !c.ok) hint = S.status.market === "cfd" ? "Liga e escolhe primeiro a conta cTrader (passo 1)." : `Verifica primeiro a ligação à ${broker()}.`;
  else if (!(capital >= 10)) hint = `O capital mínimo é 10 ${Q()}.`;
  else if (!testnet && c.usdt_free < 10) hint = S.status.market === "crypto" ? `Não tens ${Q()} livres na carteira Spot (${fmtMoney(c.usdt_free)}). Vê as instruções no passo 1.` : `A conta não tem saldo suficiente (${fmtMoney(c.usdt_free)} ${Q()}).`;
  else if (!testnet && capital > c.usdt_free) hint = `O capital é maior do que o saldo livre (${fmtMoney(c.usdt_free)} ${Q()}). Baixa o valor no passo 2.`;
  else if (!$("#liveAccept1").checked || !$("#liveAccept2").checked) hint = "Confirma que compreendes os riscos.";
  else if ($("#liveConfirm").value.trim().toUpperCase() !== "REAL") hint = "Escreve REAL para confirmar.";
  $("#liveHint").textContent = hint;
  const btn = $("#btnLiveActivate");
  btn.disabled = !!hint;
  btn.textContent = testnet ? "Ativar Modo Real (demo)" : "Ativar Modo Real";
}

async function activateLive() {
  const body = {
    mode: "live", testnet: $("#liveTestnet").checked, capital: parseFloat($("#liveCapital").value),
    confirm: $("#liveConfirm").value, accept: true,
  };
  try {
    await withBusy($("#btnLiveActivate"), () => api("/api/mode", { method: "POST", body }));
    $("#liveModal").hidden = true;
    toast(body.testnet ? "Modo Real (demo) ativo. Clica em Iniciar bot quando quiseres." : "Modo Real ativo. Clica em Iniciar bot quando quiseres.", "ok", 7000);
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
  { title: "Escolha dos ativos", showIf: (d) => !(S.settings.market === "stocks" && d.STOCK_STRATEGY === "tendencia"), fields: [
    { key: "AUTO_SELECT", type: "toggle", label: "Escolha automática (recomendado)",
      help: "Em cada análise o bot examina os ativos mais negociados, a IA gestora escolhe os melhores para aquele momento e a lista vai mudando sozinha." },
    { key: "SCAN_UNIVERSE", type: "num", label: "Ativos examinados", step: 5 },
    { key: "SCAN_TOP", type: "num", label: "Ativos analisados a fundo por ciclo", step: 1, profile: true },
    { key: "SCAN_MIN_SCORE", type: "num", label: "Pontuação técnica mínima (0-100)", step: 5 },
  ] },
  { title: "Ações e ETFs", showIf: () => S.settings.market === "stocks", fields: [
    { key: "STOCK_STRATEGY", type: "seg", label: "Estratégia", options: [["ativo", "IA ativa (trading)"], ["tendencia", "Tendência de ETFs (longo prazo)"]],
      help: "IA ativa: como em cripto (radar, IA, ordens pendentes). Tendência: rotação de ETFs por momentum com filtro da média de 200 dias; sem IA, poucos trades, a abordagem com mais evidência histórica." },
    { key: "STOCK_CURRENCY", type: "seg", label: "Moeda da conta", options: [["EUR", "EUR"], ["USD", "USD"]], help: "Tem de ser igual à moeda principal da tua conta Trading 212." },
    { key: "TREND_TOP_N", type: "num", label: "ETFs em carteira", step: 1, showIf: (d) => d.STOCK_STRATEGY === "tendencia" },
    { key: "TREND_REBALANCE", type: "seg", label: "Reequilíbrio", options: [["monthly", "Mensal"], ["weekly", "Semanal"]], showIf: (d) => d.STOCK_STRATEGY === "tendencia" },
    { key: "EARNINGS_BLACKOUT_DAYS", type: "num", label: "Sem entradas antes dos resultados", unit: "dias", step: 1, showIf: (d) => d.STOCK_STRATEGY !== "tendencia" },
    { key: "EXIT_BEFORE_EARNINGS", type: "toggle", label: "Fechar posições antes dos resultados", showIf: (d) => d.STOCK_STRATEGY !== "tendencia",
      help: "Os resultados trimestrais podem fazer o preço saltar 10% durante a noite, por cima de qualquer stop." },
    { key: "_assets", type: "assets-button", label: "Ações e ETFs analisados", showIf: (d) => d.STOCK_STRATEGY !== "tendencia",
      help: "Catálogo por categorias, descobertas do dia e os teus próprios ativos (qualquer ação ou ETF, com pesquisa)." },
  ] },
  { title: "CFDs (cTrader)", showIf: () => S.settings.market === "cfd", fields: [
    { key: "CFD_ALLOW_SHORT", type: "toggle", label: "Vender a descoberto (apostar na descida)",
      help: "O bot também abre vendas quando a tendência é de queda (ganha se o preço descer). Desligado, só compra." },
    { key: "CFD_CLOSE_BEFORE_WEEKEND", type: "toggle", label: "Fechar tudo antes do fim de semana",
      help: "Fecha as posições à sexta às 20:30 UTC e não abre novas depois das 19:00: evita os saltos de preço na abertura de segunda-feira." },
    { key: "CFD_CURRENCY", type: "seg", label: "Moeda da conta", options: [["EUR", "EUR"], ["USD", "USD"]],
      help: "No Modo Real passa a ser automaticamente a moeda da tua conta cTrader." },
    { key: "_assets", type: "assets-button", label: "Instrumentos analisados",
      help: "Ouro, prata, forex, índices e petróleo por categorias, mais os teus próprios instrumentos." },
  ] },
  { title: "Mercado", fields: [
    { key: "QUOTE", type: "seg", label: "Moeda de negociação", showIf: () => S.settings.market === "crypto", options: [["USDC", "USDC"], ["EUR", "EUR"], ["USDT", "USDT"]],
      help: "Na União Europeia a Binance não permite USDT: usa USDC (≈ 1 dólar, muitos pares) ou EUR (poucos pares)." },
    { key: "_assets", type: "assets-button", label: "Os teus ativos", showIf: () => S.settings.market === "crypto",
      help: "Adiciona as criptomoedas que queres acompanhar sempre (ex.: PAXG = ouro). O radar junta-lhes as mais negociadas." },
    { key: "PRIMARY_TIMEFRAME", type: "seg", label: "Frequência de análise", profile: true, options: [["15m", "15 min"], ["1h", "1 hora"], ["4h", "4 horas"]],
      stocksOptions: [["15m", "15 min"], ["1h", "1 hora"]],
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
    { key: "MIN_CONFIDENCE", type: "num", label: "Confiança mínima para entrar", step: 0.01, profile: true, help: "Entre 0,5 e 0,95. Mais baixo = mais trades." },
    { key: "MIN_RISK_REWARD", type: "num", label: "Alvo mínimo (x o risco)", step: 0.1, profile: true },
    { key: "PENDING_ORDER_CANDLES", type: "num", label: "Validade das ordens pendentes", unit: "velas", step: 1 },
    { key: "MAX_DAILY_LOSS_PCT", type: "num", label: "Perda diária máxima", unit: "%", step: 0.5, help: "Ao atingir, o bot não abre mais posições nesse dia." },
    { key: "MAX_DRAWDOWN_PCT", type: "num", label: "Queda máxima desde o pico", unit: "%", step: 1, help: "Ao atingir, o bot bloqueia novas entradas até desbloqueares." },
    { key: "COOLDOWN_AFTER_LOSS_MINUTES", type: "num", label: "Pausa num par após perda", unit: "min", step: 30 },
  ] },
  { title: "Técnicas dinâmicas de saída e entrada", fields: [
    { key: "PARTIAL_TP_R", type: "num", label: "Fechar parte ao ganhar", unit: "R", step: 0.25, help: "R = valor arriscado. Ao ganhar isto, fecha uma parte e põe o stop no preço de entrada." },
    { key: "PARTIAL_TP_PCT", type: "num", label: "Parte a fechar", unit: "%", step: 10, help: "0 desliga o fecho parcial." },
    { key: "TIME_STOP_CANDLES", type: "num", label: "Fechar trades parados ao fim de", unit: "velas", step: 4, help: "Liberta capital de trades que não andam (0 desliga)." },
    { key: "LOSS_STREAK_REDUCE", type: "num", label: "Reduzir risco após perdas seguidas", step: 1, help: "Depois de N perdas seguidas arrisca metade até voltar a ganhar (0 desliga)." },
    { key: "EVENT_BLACKOUT_BEFORE_MIN", type: "num", label: "Pausa antes de eventos macro", unit: "min", step: 15, help: "Sem novas entradas perto de anúncios da Fed, inflação, emprego..." },
    { key: "EVENT_BLACKOUT_AFTER_MIN", type: "num", label: "Pausa depois de eventos macro", unit: "min", step: 15 },
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
  if (S.settings.market === "stocks" && d.STOCK_STRATEGY === "tendencia") return "Estratégia de tendência: não usa IA (custo zero em OpenAI). Poucos trades por mês.";
  const tfMin = { "15m": 15, "1h": 60, "4h": 240 }[d.PRIMARY_TIMEFRAME] || 60;
  const mine = { stocks: d.STOCK_ASSETS, cfd: d.CFD_ASSETS }[S.settings.market] || d.ASSETS || [];
  const perCycle = d.AUTO_SELECT ? Number(d.SCAN_TOP) + 1 : mine.length + (d.SCANNER_ENABLED ? Number(d.SCAN_TOP) : 0);
  const perHour = (perCycle * 60 / tfMin) * Number(d.DECISION_VOTES || 1);
  return `Até ~${nf(0).format(perHour)} análises da IA por hora (menos quando não há setups técnicos), mais o resumo de notícias.`;
}

function paintSettings() {
  const { options: o } = S.settings;
  const draft = S.draft;
  const q = { stocks: draft.STOCK_CURRENCY, cfd: draft.CFD_CURRENCY }[S.settings.market] || draft.QUOTE || Q();
  const field = (f) => {
    if (f.showIf && !f.showIf(draft)) return "";
    const val = draft[f.key];
    const unit = f.unit === "Q" ? q : f.unit;
    const help = f.help ? `<div class="help">${esc(f.help)}</div>` : "";
    if (f.type === "chips") {
      return `<div class="field"><span class="field-label">${f.label}</span><div class="chips" data-key="${f.key}">` +
        o.assets.map((a) => `<button class="chip ${val.includes(a) ? "on" : ""}" data-asset="${a}">${a}</button>`).join("") + `</div>${help}</div>`;
    }
    if (f.type === "assets-button") {
      return `<div class="field-inline"><div class="field-text"><span class="field-label">${f.label}</span>${help}</div>
        <button class="btn btn-sm btn-secondary" type="button" data-open-assets>Gerir ativos</button></div>`;
    }
    if (f.type === "textarea") {
      return `<div class="field"><label class="field-label" for="f-${f.key}">${f.label}</label>
        <textarea class="input" id="f-${f.key}" data-key="${f.key}" data-list="1" spellcheck="false">${esc((val || []).join(", "))}</textarea>${help}</div>`;
    }
    if (f.type === "chips-stocks") {
      return `<div class="field"><span class="field-label">${f.label}</span><div class="chips" data-key="${f.key}">` +
        (draft.STOCK_UNIVERSE || []).map((a) => `<button class="chip ${val.includes(a) ? "on" : ""}" data-asset="${a}">${a}</button>`).join("") + `</div>${help}</div>`;
    }
    if (f.type === "seg") {
      const opts = S.settings.market === "stocks" && f.stocksOptions ? f.stocksOptions : f.options;
      return `<div class="field"><span class="field-label">${f.label}</span><div class="seg" data-key="${f.key}" data-profile="${f.profile ? 1 : 0}">` +
        opts.map(([ov, ol]) => `<button data-val="${ov}" class="${String(val) === String(ov) ? "active" : ""}">${ol}</button>`).join("") + `</div>${help}</div>`;
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
      <div class="key-row"><span>Trading 212</span><span class="mono">${esc(s.keys.t212 || s.keys.t212_demo || "não configurada")}</span></div>
      <div class="key-row"><span>cTrader</span><span class="mono">${esc(s.ctrader.token ? "autorizado" : s.ctrader.app ? "aplicação guardada, falta autorizar" : "não configurado")}</span></div>
      <div class="help">As chaves das corretoras configuram-se ao ativar o Modo Real de cada mercado.</div>
    </div>
    <div class="set-section"><h3>Zona de perigo</h3>
      <div class="danger-zone">
        <div class="field-inline" style="margin:0"><div class="field-text"><span class="field-label">Recomeçar o Modo Teste</span>
          <div class="help">Apaga posições e histórico do Modo Teste e recomeça com o saldo inicial definido acima.</div></div>
          <button class="btn btn-sm btn-danger-ghost" id="btnResetTest" ${s.mode !== "paper" ? "disabled title='Só no Modo Teste'" : ""}>Recomeçar</button></div>
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
  $$("textarea[data-list]", body).forEach((el) => (el.oninput = () => {
    draft[el.dataset.key] = el.value.split(/[\s,;]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
    markDirty();
  }));
  $$(".chips .chip", body).forEach((c) => (c.onclick = () => {
    const list = draft[c.parentElement.dataset.key];
    const i = list.indexOf(c.dataset.asset);
    if (i >= 0) list.splice(i, 1); else if (list.length < 8) list.push(c.dataset.asset);
    c.classList.toggle("on", list.includes(c.dataset.asset));
    touched(false);
  }));
  $$(".seg[data-key]", body).forEach((seg) => $$("button", seg).forEach((b) => (b.onclick = () => {
    $$("button", seg).forEach((x) => x.classList.toggle("active", x === b));
    const raw = b.dataset.val;
    draft[seg.dataset.key] = isNaN(Number(raw)) ? raw : Number(raw);
    if (["RISK_MODE", "QUOTE", "STOCK_STRATEGY", "CFD_CURRENCY"].includes(seg.dataset.key)) { paintSettings(); markDirty(); return; }
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
  $$("[data-open-assets]", body).forEach((b) => (b.onclick = () => { closeSettings(); openAssets(); }));
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
  $$(".mode-btn[data-mode]").forEach((b) => (b.onclick = () => onModeClick(b.dataset.mode)));
  $$(".market-btn").forEach((b) => (b.onclick = () => onMarketClick(b.dataset.market)));
  $("#btnStartStop").onclick = startStop;
  $("#btnAnalyze").onclick = analyzeNow;
  $("#btnCloseAll").onclick = closeAll;
  $("#btnScan").onclick = scanNow;
  $("#btnAssets").onclick = openAssets;
  $("#btnAssets2").onclick = openAssets;
  $("#assetQuery").oninput = onAssetQuery;
  $$("[data-close-assets]").forEach((b) => (b.onclick = closeAssets));
  $("#btnSettings").onclick = openSettings;
  $("#btnTheme").onclick = () => setTheme(getTheme() === "dark" ? "light" : "dark");
  $$("[data-close-drawer]").forEach((b) => (b.onclick = closeSettings));
  $("#drawerOverlay").onclick = closeSettings;
  $("#btnSaveSettings").onclick = saveSettings;
  $$(".tab").forEach((t) => (t.onclick = () => setTab(t.dataset.tab)));



  $$("[data-close-modal]").forEach((b) => (b.onclick = () => ($("#liveModal").hidden = true)));
  $("#liveTestnet").onchange = () => {
    S.liveCheck = null; $("#liveChecks").innerHTML = ""; updateLiveKeyView(); validateLive();
    if (S.status.market === "cfd" && S.ct?.token) loadCtAccounts();
    if (savedKeyFor($("#liveTestnet").checked)) verifyLive(true);
  };
  $("#btnCopyRedirect").onclick = copyRedirect;
  $("#btnCtApp").onclick = saveCtApp;
  $("#btnCtConnect").onclick = ctConnect;
  $("#btnCtToken").onclick = saveCtToken;
  $("#btnCtAccounts").onclick = () => loadCtAccounts(true);
  $("#ctAccount").onchange = () => { const id = Number($("#ctAccount").value); if (id) verifyCtrader(id); };
  $("#btnLiveVerify").onclick = () => verifyLive(false);
  $("#btnCopyIp").onclick = copyIp;
  ["#liveCapital", "#liveAccept1", "#liveAccept2", "#liveConfirm"].forEach((id) => ($(id).oninput = validateLive));
  ["#liveAccept1", "#liveAccept2"].forEach((id) => ($(id).onchange = validateLive));
  $("#btnLiveActivate").onclick = activateLive;

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#confirmModal").hidden) $("#confirmCancel").click();
    else if (!$("#liveModal").hidden) $("#liveModal").hidden = true;
    else if (!$("#assetsModal").hidden) closeAssets();
    else if ($("#drawer").classList.contains("open")) closeSettings();
  });
}

async function main() {
  setTheme(getTheme());
  bind();
  initCharts();
  await refreshStatus();
  S.tf = S.status?.timeframe || "1h";
  if (S.status) { $("#tfTabs").dataset.sig = ""; renderTfTabs(S.status); }
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

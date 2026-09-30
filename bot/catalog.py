"""Catálogo de ativos (só dados, sem dependências: pode ser importado por config.py)."""


# ------------------------------------------------------------------ catálogo de ações e ETFs (Yahoo Finance)
STOCK_CATALOG = {
    "big_tech": ("Gigantes tecnológicas", ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "ORCL", "NFLX"]),
    "semis": ("Semicondutores", ["AMD", "MU", "INTC", "QCOM", "ARM", "TSM", "AMAT", "LRCX", "ADI", "TXN", "ASML.AS", "IFX.DE"]),
    "software": ("Software e cloud", ["CRM", "ADBE", "NOW", "PLTR", "CRWD", "PANW", "SNOW", "NET", "DDOG", "SHOP", "APP", "ANET", "SAP.DE"]),
    "fintech": ("Fintech e cripto-ações", ["COIN", "HOOD", "PYPL", "SOFI", "MSTR", "V", "MA"]),
    "finance": ("Bancos e seguros", ["JPM", "BAC", "GS", "MS", "ALV.DE", "SAN.MC"]),
    "health": ("Saúde", ["LLY", "UNH", "JNJ", "MRK", "ABBV", "NVO"]),
    "consumer": ("Consumo", ["COST", "WMT", "HD", "KO", "PEP", "MCD", "NKE", "DIS", "UBER", "SBUX", "BKNG", "SPOT", "MC.PA", "ITX.MC"]),
    "industry": ("Energia, indústria e defesa", ["XOM", "CVX", "CAT", "GE", "BA", "LMT", "RTX", "VRT", "RHM.DE", "AIR.PA", "SIE.DE", "TTE.PA", "SU.PA"]),
    "portugal": ("Portugal (Euronext Lisboa)", ["EDP.LS", "GALP.LS", "JMT.LS", "BCP.LS"]),
    "etf_index": ("ETFs de índices (UCITS)", ["SXR8.DE", "SXRV.DE", "EUNL.DE", "VWCE.DE", "IS3N.DE", "EXSA.DE", "SXRT.DE", "DBXD.DE", "IUSN.DE"]),
    "etf_sector": ("ETFs setoriais (UCITS)", ["QDVE.DE", "EXV1.DE", "G2X.DE"]),
    "metals": ("Ouro e prata (ETCs físicos)", ["4GLD.DE", "PPFB.DE", "XAD6.DE"]),
}
DEFAULT_STOCK_CATEGORIES = ["big_tech", "semis", "software", "fintech", "finance", "health", "consumer", "industry",
                            "portugal", "etf_index", "etf_sector", "metals"]

CRYPTO_CATALOG = {
    "top": ("Principais", ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "AVAX", "SUI"]),
    "gold": ("Ouro tokenizado (1 token = 1 onça)", ["PAXG", "XAUT"]),
}
CRYPTO_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "BNB": "BNB", "ADA": "Cardano",
                "DOGE": "Dogecoin", "LINK": "Chainlink", "AVAX": "Avalanche", "SUI": "Sui",
                "PAXG": "Pax Gold (1 onça de ouro)", "XAUT": "Tether Gold (1 onça de ouro)"}

# nomes do catálogo (para a pesquisa local e para a interface)
CATALOG_NAMES = {
    "4GLD.DE": "Xetra-Gold (ouro físico, EUR)", "PPFB.DE": "iShares Physical Gold (ouro físico, EUR)",
    "XAD6.DE": "Xtrackers Physical Silver (prata física, EUR)", "G2X.DE": "VanEck Gold Miners (mineiras de ouro)",
    "SXR8.DE": "iShares Core S&P 500", "SXRV.DE": "iShares Nasdaq 100", "EUNL.DE": "iShares Core MSCI World",
    "VWCE.DE": "Vanguard FTSE All-World", "IS3N.DE": "iShares Core MSCI Emerging Markets", "EXSA.DE": "iShares STOXX Europe 600",
    "SXRT.DE": "iShares Core EURO STOXX 50", "DBXD.DE": "Xtrackers DAX", "IUSN.DE": "iShares MSCI World Small Cap",
    "QDVE.DE": "iShares S&P 500 Information Technology", "EXV1.DE": "iShares STOXX Europe 600 Banks",
    "EDP.LS": "EDP", "GALP.LS": "Galp Energia", "JMT.LS": "Jerónimo Martins", "BCP.LS": "Millennium BCP",
    "ASML.AS": "ASML", "SAP.DE": "SAP", "SIE.DE": "Siemens", "IFX.DE": "Infineon", "RHM.DE": "Rheinmetall",
    "ALV.DE": "Allianz", "MC.PA": "LVMH", "AIR.PA": "Airbus", "TTE.PA": "TotalEnergies", "SU.PA": "Schneider Electric",
    "ITX.MC": "Inditex (Zara)", "SAN.MC": "Banco Santander",
}
ALIASES = {
    "ouro": ["4GLD.DE", "PPFB.DE", "G2X.DE"], "gold": ["4GLD.DE", "PPFB.DE", "G2X.DE"], "xau": ["4GLD.DE", "PPFB.DE"],
    "prata": ["XAD6.DE"], "silver": ["XAD6.DE"], "xag": ["XAD6.DE"], "s&p": ["SXR8.DE", "QDVE.DE"],
    "sp500": ["SXR8.DE"], "nasdaq": ["SXRV.DE"], "mundo": ["VWCE.DE", "EUNL.DE"], "world": ["VWCE.DE", "EUNL.DE"],
    "emergentes": ["IS3N.DE"], "emerging": ["IS3N.DE"], "dax": ["DBXD.DE"], "europa": ["EXSA.DE", "SXRT.DE"],
    "europe": ["EXSA.DE", "SXRT.DE"], "tecnologia": ["QDVE.DE", "SXRV.DE"], "mineiras": ["G2X.DE"],
    "small": ["IUSN.DE"], "bancos": ["EXV1.DE", "SAN.MC"], "portugal": ["EDP.LS", "GALP.LS", "JMT.LS", "BCP.LS"],
    "zara": ["ITX.MC"], "defesa": ["RHM.DE", "LMT", "RTX", "AIR.PA"],
}
GOLD_FOREX_HINT = ("XAU/USD é ouro negociado como CFD: está no mercado \"CFDs\" (botão no topo, corretora cTrader). "
                   "Aqui nas ações usa um ETC de ouro físico (4GLD.DE ou PPFB.DE) ou, em cripto, o PAXG/XAUT.")
SILVER_FOREX_HINT = "XAG/USD é prata como CFD: está no mercado \"CFDs\" (cTrader). Aqui usa o ETC de prata física XAD6.DE."

US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "BTS", "PCX", "NAS", "NYS"}
OTC_EXCHANGES = {"PNK", "OTC", "OQB", "OQX", "OBB"}
SECONDARY_EU = {"F", "HM", "DU", "MU", "SG", "BE"}  # Frankfurt floor, Hamburgo...: preferir a Xetra (.DE)


def categories_universe(enabled: list, catalog: dict | None = None) -> list:
    catalog = STOCK_CATALOG if catalog is None else catalog
    out = []
    for key in enabled:
        if key in catalog:
            out.extend(catalog[key][1])
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ CFDs (cTrader): ouro, prata, forex, índices, petróleo
# yahoo: série usada no Modo Teste (futuros/índices/forex do Yahoo Finance, grátis)
# quote: moeda em que o preço está cotado | margin: margem para clientes particulares na UE (ESMA)
# min/step: quantidade mínima e passo (em unidades: onças, barris, moeda base, contratos do índice)
# spread: spread típico (em preço) usado no Modo Teste | aliases: nomes usados pelas corretoras cTrader
# ccys: moedas cujos eventos macro afetam o instrumento (pausa antes de Fed, BCE...)
CFD_SPECS = {
    "XAUUSD": {"name": "Ouro (XAU/USD)", "yahoo": "GC=F", "quote": "USD", "margin": 0.05, "min": 1, "step": 1,
               "spread": 0.35, "aliases": ["XAUUSD", "GOLD", "XAUUSDSPOT"], "ccys": ["USD"], "group": "metals",
               "news": ["gold", "bullion", "XAU"]},
    "XAGUSD": {"name": "Prata (XAG/USD)", "yahoo": "SI=F", "quote": "USD", "margin": 0.10, "min": 50, "step": 50,
               "spread": 0.03, "aliases": ["XAGUSD", "SILVER"], "ccys": ["USD"], "group": "metals",
               "news": ["silver", "XAG"]},
    "EURUSD": {"name": "Euro / Dólar", "yahoo": "EURUSD=X", "quote": "USD", "margin": 1 / 30, "min": 1000, "step": 1000,
               "spread": 0.00008, "aliases": ["EURUSD"], "ccys": ["USD", "EUR"], "group": "fx_major",
               "news": ["EUR/USD", "euro", "ECB", "Lagarde"]},
    "GBPUSD": {"name": "Libra / Dólar", "yahoo": "GBPUSD=X", "quote": "USD", "margin": 1 / 30, "min": 1000, "step": 1000,
               "spread": 0.00012, "aliases": ["GBPUSD"], "ccys": ["USD", "GBP"], "group": "fx_major",
               "news": ["GBP/USD", "sterling", "pound", "Bank of England", "BoE"]},
    "USDJPY": {"name": "Dólar / Iene", "yahoo": "JPY=X", "quote": "JPY", "margin": 1 / 30, "min": 1000, "step": 1000,
               "spread": 0.012, "aliases": ["USDJPY"], "ccys": ["USD", "JPY"], "group": "fx_major",
               "news": ["USD/JPY", "yen", "Bank of Japan", "BoJ"]},
    "USDCHF": {"name": "Dólar / Franco suíço", "yahoo": "CHF=X", "quote": "CHF", "margin": 1 / 30, "min": 1000, "step": 1000,
               "spread": 0.00012, "aliases": ["USDCHF"], "ccys": ["USD", "CHF"], "group": "fx_major",
               "news": ["USD/CHF", "Swiss franc", "SNB"]},
    "USDCAD": {"name": "Dólar / Dólar canadiano", "yahoo": "CAD=X", "quote": "CAD", "margin": 1 / 30, "min": 1000, "step": 1000,
               "spread": 0.00014, "aliases": ["USDCAD"], "ccys": ["USD", "CAD"], "group": "fx_major",
               "news": ["USD/CAD", "Canadian dollar", "loonie", "Bank of Canada"]},
    "EURGBP": {"name": "Euro / Libra", "yahoo": "EURGBP=X", "quote": "GBP", "margin": 1 / 30, "min": 1000, "step": 1000,
               "spread": 0.00012, "aliases": ["EURGBP"], "ccys": ["EUR", "GBP"], "group": "fx_major",
               "news": ["EUR/GBP", "ECB", "Bank of England"]},
    "EURJPY": {"name": "Euro / Iene", "yahoo": "EURJPY=X", "quote": "JPY", "margin": 1 / 30, "min": 1000, "step": 1000,
               "spread": 0.02, "aliases": ["EURJPY"], "ccys": ["EUR", "JPY"], "group": "fx_major",
               "news": ["EUR/JPY", "yen", "ECB", "BoJ"]},
    "AUDUSD": {"name": "Dólar australiano / Dólar", "yahoo": "AUDUSD=X", "quote": "USD", "margin": 0.05, "min": 1000,
               "step": 1000, "spread": 0.0001, "aliases": ["AUDUSD"], "ccys": ["USD", "AUD"], "group": "fx_minor",
               "news": ["AUD/USD", "Aussie", "RBA", "Reserve Bank of Australia"]},
    "NZDUSD": {"name": "Dólar neozelandês / Dólar", "yahoo": "NZDUSD=X", "quote": "USD", "margin": 0.05, "min": 1000,
               "step": 1000, "spread": 0.00014, "aliases": ["NZDUSD"], "ccys": ["USD", "NZD"], "group": "fx_minor",
               "news": ["NZD/USD", "Kiwi", "RBNZ"]},
    "US500": {"name": "S&P 500", "yahoo": "ES=F", "quote": "USD", "margin": 0.05, "min": 0.1, "step": 0.1, "spread": 0.5,
              "aliases": ["US500", "SPX500", "USA500", "SP500", "US500CASH", "SPX", "US500INDEX"], "ccys": ["USD"],
              "group": "indices", "news": ["S&P 500", "S&P", "Wall Street", "US stocks"]},
    "NAS100": {"name": "Nasdaq 100", "yahoo": "NQ=F", "quote": "USD", "margin": 0.05, "min": 0.1, "step": 0.1, "spread": 1.5,
               "aliases": ["NAS100", "USTEC", "US100", "NDX100", "USTECH", "NASDAQ100", "NAS100CASH", "NDX"],
               "ccys": ["USD"], "group": "indices", "news": ["Nasdaq", "tech stocks", "Nasdaq 100"]},
    "US30": {"name": "Dow Jones 30", "yahoo": "YM=F", "quote": "USD", "margin": 0.05, "min": 0.1, "step": 0.1, "spread": 2.5,
             "aliases": ["US30", "DJ30", "WS30", "US30CASH", "DOW30", "DJI"], "ccys": ["USD"], "group": "indices",
             "news": ["Dow Jones", "Dow", "Wall Street"]},
    "GER40": {"name": "DAX 40 (Alemanha)", "yahoo": "^GDAXI", "quote": "EUR", "margin": 0.05, "min": 0.1, "step": 0.1,
              "spread": 1.2, "aliases": ["GER40", "DE40", "DAX40", "GER30", "DE30", "GERMANY40", "GER40CASH", "DAX"],
              "ccys": ["EUR"], "group": "indices", "news": ["DAX", "German stocks", "Germany"]},
    "UK100": {"name": "FTSE 100 (Reino Unido)", "yahoo": "^FTSE", "quote": "GBP", "margin": 0.05, "min": 0.1, "step": 0.1,
              "spread": 1.0, "aliases": ["UK100", "FTSE100", "UK100CASH", "FTSE"], "ccys": ["GBP"], "group": "indices",
              "news": ["FTSE", "UK stocks", "London stocks"]},
    "EU50": {"name": "Euro Stoxx 50", "yahoo": "^STOXX50E", "quote": "EUR", "margin": 0.05, "min": 0.1, "step": 0.1,
             "spread": 1.5, "aliases": ["EU50", "EUSTX50", "STOXX50", "EUROSTOXX50", "ESTX50", "EUSTOXX50"],
             "ccys": ["EUR"], "group": "indices", "news": ["Euro Stoxx", "European stocks"]},
    "FRA40": {"name": "CAC 40 (França)", "yahoo": "^FCHI", "quote": "EUR", "margin": 0.05, "min": 0.1, "step": 0.1,
              "spread": 1.2, "aliases": ["FRA40", "F40", "CAC40", "FR40", "FRANCE40"], "ccys": ["EUR"], "group": "indices",
              "news": ["CAC 40", "French stocks", "France"]},
    "JP225": {"name": "Nikkei 225 (Japão)", "yahoo": "^N225", "quote": "JPY", "margin": 0.05, "min": 1, "step": 1,
              "spread": 10, "aliases": ["JP225", "JPN225", "NIKKEI225", "JAP225", "NIKKEI"], "ccys": ["JPY", "USD"],
              "group": "indices", "news": ["Nikkei", "Japanese stocks", "Japan"]},
    "XTIUSD": {"name": "Petróleo WTI", "yahoo": "CL=F", "quote": "USD", "margin": 0.10, "min": 10, "step": 10,
               "spread": 0.03, "aliases": ["XTIUSD", "USOIL", "WTI", "SPOTCRUDE", "CRUDE", "OILWTI", "XTI", "WTIUSD",
                                           "USCRUDE", "CRUDEOIL"], "ccys": ["USD"], "group": "energy",
               "news": ["oil", "crude", "WTI", "OPEC"]},
    "XBRUSD": {"name": "Petróleo Brent", "yahoo": "BZ=F", "quote": "USD", "margin": 0.10, "min": 10, "step": 10,
               "spread": 0.03, "aliases": ["XBRUSD", "UKOIL", "BRENT", "SPOTBRENT", "BRN", "XBR", "BRENTUSD", "BRENTOIL"],
               "ccys": ["USD"], "group": "energy", "news": ["Brent", "oil", "crude", "OPEC"]},
    "XNGUSD": {"name": "Gás natural", "yahoo": "NG=F", "quote": "USD", "margin": 0.10, "min": 100, "step": 100,
               "spread": 0.004, "aliases": ["XNGUSD", "NATGAS", "NGAS", "USNATGAS", "NATURALGAS"], "ccys": ["USD"],
               "group": "energy", "news": ["natural gas", "LNG"]},
}
CFD_CATALOG = {
    "metals": ("Ouro e prata", ["XAUUSD", "XAGUSD"]),
    "fx_major": ("Forex (pares principais)", ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "EURGBP", "EURJPY"]),
    "fx_minor": ("Forex (outros pares)", ["AUDUSD", "NZDUSD"]),
    "indices": ("Índices de bolsa", ["US500", "NAS100", "US30", "GER40", "UK100", "EU50", "FRA40", "JP225"]),
    "energy": ("Petróleo e gás", ["XTIUSD", "XBRUSD", "XNGUSD"]),
}
DEFAULT_CFD_CATEGORIES = ["metals", "fx_major", "indices", "energy"]
CFD_ALIASES = {
    "ouro": ["XAUUSD"], "gold": ["XAUUSD"], "xau": ["XAUUSD"], "prata": ["XAGUSD"], "silver": ["XAGUSD"],
    "xag": ["XAGUSD"], "euro": ["EURUSD", "EURGBP", "EURJPY"], "libra": ["GBPUSD", "EURGBP"], "iene": ["USDJPY", "EURJPY"],
    "yen": ["USDJPY", "EURJPY"], "dolar": ["EURUSD", "USDJPY", "GBPUSD"], "s&p": ["US500"], "sp500": ["US500"],
    "nasdaq": ["NAS100"], "dow": ["US30"], "dax": ["GER40"], "alemanha": ["GER40"], "ftse": ["UK100"],
    "cac": ["FRA40"], "franca": ["FRA40"], "nikkei": ["JP225"], "japao": ["JP225"], "stoxx": ["EU50"],
    "petroleo": ["XTIUSD", "XBRUSD"], "oil": ["XTIUSD", "XBRUSD"], "wti": ["XTIUSD"], "brent": ["XBRUSD"],
    "gas": ["XNGUSD"], "indices": ["US500", "NAS100", "GER40"], "forex": ["EURUSD", "GBPUSD", "USDJPY"],
}
CFD_GROUP_NAMES = {"metals": "Metais", "fx_major": "Forex", "fx_minor": "Forex", "indices": "Índices", "energy": "Energia"}



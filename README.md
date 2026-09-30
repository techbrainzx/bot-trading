# Trading Bot IA

Aplicação de trading automático em três mercados: criptomoedas (Binance), ações e ETFs (Trading 212) e CFDs de ouro, prata, forex, índices e petróleo (cTrader, com compra **e venda a descoberto**). A IA da OpenAI decide **comprar / vender / esperar** e um gestor de risco com regras fixas, que a IA não consegue ultrapassar, controla tudo o que é executado.

> **Aviso:** nenhum bot, com ou sem IA, prevê o mercado de forma fiável. O trading tem risco real de perda. Começa sempre pelo **Modo Teste** e usa só dinheiro que possas perder.

## Abrir a aplicação

Duplo clique em **`Trading Bot IA.bat`**, ou:

```bash
python app.py
```

Abre uma janela própria. Para abrir no browser em vez disso: `python app.py --browser`.

Na primeira vez numa máquina nova: `pip install -r requirements.txt`.

## Mercados e modos

No topo escolhes o **mercado** (Cripto, Ações & ETFs ou CFDs) e o **modo** (Teste ou Real). Cada combinação tem a sua carteira e o seu histórico.

| | Cripto | Ações & ETFs | CFDs |
|---|---|---|---|
| Corretora (Modo Real) | Binance (USDC; a UE não permite USDT) | Trading 212 (conta Invest, em EUR) | Qualquer corretora com cTrader (IC Markets, Pepperstone, FxPro...) |
| Conta de teste da corretora | Binance Testnet | Conta demo da Trading 212 | Conta demo cTrader |
| Dados | Binance | Yahoo Finance (grátis) | Modo Teste: Yahoo (futuros/índices/forex); Modo Real: a própria cTrader |
| Horário | 24/7 | Só com a bolsa aberta (Nova Iorque 14:30-21:00, Xetra 08:00-16:30, hora de Lisboa) | Domingo 23h a sexta 22h (hora de Lisboa), quase 24h por dia |
| O que negoceia | ~40 criptomoedas mais líquidas | ~100 ações e ETFs em 12 categorias | Ouro (XAU/USD), prata, 9 pares de forex, 8 índices, petróleo e gás |
| Direção | Só compras | Só compras | Compra e venda a descoberto |

**Ativar o Modo Real**: clica em "Modo Real" e segue os 3 passos:
1. Ligação à corretora:
   - **Binance:** a aplicação recusa chaves com levantamentos ativados;
   - **Trading 212:** gera a chave na app (Definições › API) com permissão para colocar ordens;
   - **cTrader:** cria uma aplicação grátis em openapi.ctrader.com com o Redirect URI que a janela mostra, cola o Client ID e o Secret, carrega em "Ligar ao cTrader", autoriza no browser e escolhe a conta. Nunca escreves a palavra-passe da corretora e a API não permite levantamentos.
2. Capital máximo que o bot pode usar.
3. Confirmação escrita "REAL".

Recomendo começar com a opção de conta demo.

## Ações & ETFs: duas estratégias

1. **IA ativa (trading):** o mesmo sistema de cripto (radar, IA gestora, análise a fundo e ordens pendentes), com regras próprias da bolsa:
   - não entra nos 2 dias antes dos **resultados trimestrais** e fecha posições na véspera, porque o preço pode saltar durante a noite por cima de qualquer stop;
   - usa o **VIX** como medidor de medo das bolsas;
   - considera fundamentais (P/E, alvo dos analistas, short interest) e notícias de 11 fontes financeiras e de cada empresa;
   - só negoceia com a bolsa aberta e deteta feriados.
2. **Tendência de ETFs (longo prazo, sem IA):** a abordagem com mais evidência académica:
   - compra os 3 ETFs com melhor momentum a 1, 3, 6 e 12 meses (Moskowitz, Ooi & Pedersen, 2012), só se estiverem acima da média de 200 dias (Faber, 2007);
   - o resto fica num ETF de "dinheiro" (XEON);
   - reequilibra 1 vez por mês e vende logo um ETF que perca a média de 200 dias;
   - custo de IA zero e poucos trades.

## CFDs (cTrader): ouro, forex, índices e petróleo

- **XAU/USD e companhia:** os CFDs seguem o preço do ouro, das moedas, dos índices e do petróleo sem comprares o ativo. Ficas só com a **margem** presa (ex.: 5% no ouro e nos índices, 3,3% no forex principal, 10% no petróleo: limites da ESMA para particulares).
- **Compra e venda a descoberto:** o bot aposta na subida em tendências de alta e na descida em tendências de baixa. Os setups de venda são os mesmos das compras, detetados no gráfico "espelhado". Podes desligar as vendas nas definições.
- **O risco não depende da alavancagem:** cada trade é dimensionado pelo stop (ex.: arriscar 1% ou um valor fixo). A alavancagem só reduz o dinheiro preso.
- **Stop-loss na própria corretora:** cada posição é aberta com stop na cTrader, por isso fica protegida mesmo com o PC desligado. O bot leva para lá o stop sempre que o aperta (break-even, trailing, estrutural). Se a corretora fechar uma posição, o bot regista o resultado real.
- **Custos reais:** spread, comissão e financiamento noturno (swap, 3 noites à quarta-feira). No Modo Teste são simulados.
- **Fim de semana:** fecha tudo à sexta às 20:30 UTC e não abre posições novas depois das 19:00 UTC (evita os saltos de segunda-feira; desligável).
- **Eventos macro por moeda:** a pausa antes da Fed/BCE/emprego/inflação aplica-se às moedas de cada instrumento (EUR/USD pára no BCE e na Fed; o DAX no BCE).
- **Tamanhos mínimos:** o ouro tem mínimo de 1 onça (cerca de 3 700 € de exposição e 185 € de margem). Com contas pequenas, o bot salta o que não cabe no risco por trade e usa forex (mínimo de 1000 unidades).
- **Notícias:** 11 fontes de forex, matérias-primas e macro (FXStreet, investingLive/ForexLive, Investing.com, OilPrice, WSJ...) e ainda o dólar (DXY), os juros a 10 anos, o VIX, o S&P 500, o ouro e o petróleo em tempo real.

> Os CFDs são produtos complexos e alavancados. Pelos avisos obrigatórios das corretoras na UE, a maioria dos clientes particulares perde dinheiro com eles. Testa primeiro no Modo Teste e depois numa conta demo.

## Técnicas dinâmicas de compra e venda (todos os mercados)

- **Fecho parcial:** ao ganhar 1R fecha metade e põe o stop no preço de entrada (o trade já não pode dar prejuízo).
- **Trailing stop adaptado ao regime:** mais largo em tendência forte (ADX ≥ 30) e mais apertado em mercado lateral.
- **Stop estrutural:** sobe para baixo do último fundo mais alto (numa venda, desce para cima do último topo mais baixo).
- **Deixar correr:** num rompimento forte, ao chegar ao alvo não vende tudo; retira o alvo e usa um trailing apertado.
- **Stop por tempo:** fecha trades parados ao fim de 24 velas sem progresso.
- **Risco dinâmico:**
  - metade do risco depois de 3 perdas seguidas;
  - menos risco em setups que andam a perder e mais nos que ganham;
  - menos risco em mercado desfavorável.
- **Pausa em eventos macro:** sem novas entradas 45 min antes e 30 min depois de anúncios da Fed, inflação, emprego, etc.

Tudo isto é ajustável em Definições › Técnicas dinâmicas.

## O painel

- **Topo:** modo atual, estado do bot, *Iniciar/Parar*, *Analisar agora*, tema claro/escuro e definições.
- **Indicadores:** capital, resultado total, resultado de hoje, queda desde o pico, trades e taxa de acerto, e contagem para a próxima análise.
- **Gráfico:** velas de cada par com volume, marcadores de compra e venda e, na posição aberta, as linhas de entrada, stop e alvo.
- **Posições abertas:** resultado atual, barra stop → entrada → alvo e botão *Fechar*.
- **Análise da IA:** a última decisão de cada par, com confiança e motivo.
- **Separadores:**
  - *Decisões da IA*: raciocínio completo, argumentos a favor e contra, e notícias consideradas;
  - *Histórico de trades*;
  - *Evolução do capital*;
  - *Registo*: log em tempo real.
- **Definições:** pares, frequência, modelo de IA, regras de risco, proteção de lucros, capital, chave da OpenAI e recomeçar o Modo Teste.

## Como o bot decide

A cada fecho de vela (1h por defeito; 15 min no perfil Agressivo), num **modo totalmente automático**:

1. **Radar:** análise técnica completa das ~40 moedas mais negociadas (sem custo de IA). Para cada uma:
   - setups (pullback, rompimento, momentum, ressalto, compressão), cada um com um plano de entrada, stop e alvo;
   - tendência em 2 timeframes e momentum a 24h e 7 dias;
   - força face ao BTC, volatilidade, divergências RSI e padrões de velas.
2. **Clima do mercado:** percentagem de moedas em alta e tendência do BTC (favorável / misto / desfavorável). Em mercado desfavorável o bot exige mais confiança para comprar.
3. **IA gestora** (`gpt-5.4-mini`, rápida e barata): compara as 12 melhores com as notícias de cada uma, o clima e o histórico de resultados. Escolhe as 3-4 melhores **para aquele momento** e explica porquê, e também porque evita outras. A lista muda sozinha de ciclo para ciclo.
4. **Análise a fundo** (`gpt-5.5`) de cada moeda escolhida e das posições abertas:
   - 4 timeframes, livro de ordens, funding, open interest, rácio long/short e fluxo comprador/vendedor;
   - notícias de 13 sites, calendário económico e resumo da IA com pesquisa web.

   Decide entre:
   - **comprar já**;
   - **comprar no recuo**, uma ordem pendente;
   - **comprar no rompimento**, uma ordem pendente;
   - **vender**;
   - **esperar**.
5. **Gestor de risco** (regras fixas):
   - risco por trade em % ou **valor fixo**;
   - stop obrigatório e retorno/risco mínimo depois de comissões;
   - mínimos da Binance respeitados;
   - limite diário de perda e bloqueio se o capital cair demasiado desde o pico;
   - **limite diário de chamadas à IA**.
6. **Aprendizagem:** cada trade fica registado com o tipo de setup. Os setups que andam a perder descem de prioridade e os que ganham sobem, e a IA vê esse histórico.
7. **Proteção automática** a cada 20 s:
   - stop-loss e take-profit;
   - stop no preço de entrada ao ganhar 1R;
   - trailing stop;
   - disparo e expiração das ordens pendentes.

Queres escolher tu as moedas? Desliga "Escolha automática" nas definições e escolhe os pares fixos.

## Perfis

| Perfil | Análise | Confiança mín. | Alvo mín. | Radar → IA | Risco |
|---|---|---|---|---|---|
| Conservador | 1 h | 0,65 | 2,0x | 2 | 0,75% |
| Equilibrado | 1 h | 0,58 | 1,5x | 3 | 1% |
| Agressivo | 15 min | 0,52 | 1,3x | 4 | 1,5% |

Mais ativo = mais trades **e** mais custo de IA. A estimativa de análises por hora aparece nas definições.

## Moeda (importante na UE)

Na União Europeia (MiCA) a Binance **não permite pares com USDT**. O bot usa **USDC** por defeito (≈ 1 dólar, muitos pares) ou EUR (poucos pares). Para o Modo Real precisas de ter saldo nessa moeda na Binance.

## Importante

- **Os stops são vigiados pela aplicação.** Se a fechares ou desligares o PC, as posições abertas ficam sem proteção. Para 24/7 usa um PC sempre ligado ou um servidor.
- Os custos da OpenAI aparecem no rodapé (tokens usados hoje). Define um limite de gastos em https://platform.openai.com/settings/organization/limits.
- As chaves ficam só no ficheiro `.env` deste PC. Nunca o partilhes.

## Linha de comandos (opcional)

```bash
python main.py backtest --symbol SOL/USDT --days 30 --step 4
```

Testa a estratégia em dados históricos.

```bash
python main.py status
```

Mostra o estado do bot no terminal.

## Estrutura

```
app.py             aplicação (servidor local + janela)
web/               interface (HTML, CSS, JS)
config.py          valores por defeito; alterações da interface em data/settings.json
bot/controller.py  thread única que executa o bot e os comandos da interface
bot/engine.py      ciclo: dados -> IA -> risco -> execução
bot/brain.py       prompt e decisão da IA
bot/news.py        pesquisa de notícias
bot/risk.py        regras de risco, stops e trailing
bot/trader.py      posições e contabilidade
bot/broker.py      execução simulada (cripto/ações e CFDs) e Binance, verificação de chaves
bot/t212.py        corretora Trading 212 (ações e ETFs)
bot/ctrader.py     corretora cTrader (CFDs): ligação WebSocket/JSON, OAuth, ordens com stop na corretora
bot/market.py      dados de cripto (Binance)
bot/stockdata.py   dados de ações (Yahoo Finance), horários das bolsas, câmbio
bot/cfddata.py     dados de CFDs (Yahoo no Modo Teste, cTrader no Modo Real)
bot/catalog.py     catálogos de ações, cripto e CFDs
bot/assets.py      pesquisa e validação dos ativos que adicionas
bot/analysis.py    indicadores e contexto para a IA
bot/backtest.py    backtest
```

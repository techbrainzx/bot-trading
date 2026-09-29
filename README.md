# Trading Bot IA

Aplicação de trading automático de criptomoedas (Binance spot, só compras, sem alavancagem). A IA da OpenAI decide **comprar / vender / esperar** e um gestor de risco com regras fixas, que a IA não consegue ultrapassar, controla tudo o que é executado.

> **Aviso:** nenhum bot, com ou sem IA, prevê o mercado de forma fiável. O trading tem risco real de perda. Começa sempre pelo **Modo Teste** e usa só dinheiro que possas perder.

## Abrir a aplicação

Duplo clique em **`Trading Bot IA.bat`**, ou:

```bash
python app.py
```

Abre uma janela própria. Para abrir no browser em vez disso: `python app.py --browser`.

Na primeira vez numa máquina nova: `pip install -r requirements.txt`.

## Os dois modos

| | Modo Teste | Modo Real |
|---|---|---|
| Dinheiro | fictício (10 000 USDT por defeito) | **real**, na tua conta Binance |
| Preços | reais, em tempo real | reais |
| Ordens | simuladas, nada é enviado à Binance | enviadas à Binance |
| Carteira e histórico | próprios | próprios (separados do Teste) |

**Ativar o Modo Real**: clica em "Modo Real" e segue os 3 passos.
1. Chaves da API da Binance. A aplicação verifica a ligação, o saldo e as permissões, e **recusa chaves com levantamentos ativados**. Há também a opção *Testnet* para testar ordens reais com dinheiro fictício.
2. Capital máximo que o bot pode usar. Nunca usa mais do que isto.
3. Confirmação: aceitar os riscos e escrever `REAL`.

Ao iniciar o bot no Modo Real é pedida mais uma confirmação.

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
bot/broker.py      execução (simulada ou Binance) e verificação de chaves
bot/market.py      dados de mercado
bot/analysis.py    indicadores e contexto para a IA
bot/backtest.py    backtest
```

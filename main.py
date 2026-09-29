"""
Linha de comandos (alternativa à interface gráfica em app.py).

  python main.py run          -> corre o bot continuamente (no modo escolhido na interface)
  python main.py once         -> faz um único ciclo de análise/decisão agora
  python main.py status       -> mostra capital, posições e estatísticas
  python main.py backtest     -> testa a estratégia em dados históricos
  python main.py close-all    -> fecha todas as posições a mercado
"""
import argparse
import logging
import sys

import config
from bot.journal import setup_logging

log = logging.getLogger("bot")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    p = argparse.ArgumentParser(description="Trading Bot IA (linha de comandos)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="corre o bot continuamente")
    sub.add_parser("once", help="um único ciclo de decisão agora")
    sub.add_parser("status", help="capital, posições e estatísticas")
    sub.add_parser("close-all", help="fecha todas as posições")
    bt = sub.add_parser("backtest", help="testa a estratégia em dados históricos")
    bt.add_argument("--symbol", default=config.SYMBOLS[0] if config.SYMBOLS else f"BTC/{config.QUOTE}")
    bt.add_argument("--days", type=int, default=14)
    bt.add_argument("--step", type=int, default=4, help="decide a cada N velas (menos chamadas à IA)")
    bt.add_argument("--model", default=config.DECISION_MODEL)
    bt.add_argument("--effort", default=config.REASONING_EFFORT)
    bt.add_argument("--votes", type=int, default=1)
    bt.add_argument("--yes", action="store_true", help="não pedir confirmação")
    args = p.parse_args()

    setup_logging(config.LOGS_DIR)

    if args.cmd == "backtest":
        from bot.backtest import run_backtest
        run_backtest(args.symbol, args.days, args.step, args.model, args.effort, args.votes, args.yes)
        return

    from bot.engine import ConfigError, Engine
    try:
        engine = Engine()
    except ConfigError as e:
        raise SystemExit(str(e))

    if args.cmd == "status":
        print(engine.status_text())
    elif args.cmd == "close-all":
        engine.close_all()
        print(engine.status_text())
    elif args.cmd == "once":
        engine.run_once()
        print("\n" + engine.status_text())
    elif args.cmd == "run":
        try:
            engine.run_forever()
        except KeyboardInterrupt:
            log.info("Bot parado pelo utilizador. As posições abertas mantêm-se, mas os stops "
                     "só são vigiados com o bot a correr.")


if __name__ == "__main__":
    main()

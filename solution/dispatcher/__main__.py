"""CLI entrypoint for the Durable Campaign Dispatcher."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from .clock import ProviderClock
from .config import load_config
from .dispatch import DispatchLoop
from .output import write_final_state_csv
from .provider import TelephonyClient
from .reconcile import Reconciler
from .store import StateStore
from .webhook import create_webhook_app

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FIXTURES = os.path.abspath(os.path.join(HERE, "..", "..", "assignment-b-workflow", "fixtures", "campaigns.json"))


class ColoredFormatter(logging.Formatter):
    """Clean, high-readability ANSI color formatter for console output."""

    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",       # Cyan
        logging.INFO: "\033[32m",        # Green
        logging.WARNING: "\033[33m",     # Amber/Yellow
        logging.ERROR: "\033[31m",       # Red
        logging.CRITICAL: "\033[1;31m",  # Bold Red
    }
    RESET = "\033[0m"
    DIM = "\033[90m"
    CYAN = "\033[36m"
    BLUE = "\033[94m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_YELLOW = "\033[93m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        time_str = f"{self.DIM}{self.formatTime(record, '%Y-%m-%d %H:%M:%S')}{self.RESET}"
        level_str = f"{color}{record.levelname:<7}{self.RESET}"
        name_str = f"{self.CYAN}{record.name}{self.RESET}"
        msg = record.getMessage()

        # Context-aware highlighting for telephony domain events
        if "Carrier accepted" in msg or "recorded result" in msg:
            if "status=completed" in msg or "disp=interested" in msg:
                msg = f"{self.BRIGHT_GREEN}{msg}{self.RESET}"
            elif "status=busy" in msg or "status=no_answer" in msg:
                msg = f"{self.BRIGHT_YELLOW}{msg}{self.RESET}"
            else:
                msg = f"{self.BRIGHT_GREEN}{msg}{self.RESET}"
        elif "Dialing" in msg or "Reserved" in msg:
            msg = f"{self.BLUE}{msg}{self.RESET}"
        elif "503" in msg or "504" in msg or "releasing slot" in msg:
            msg = f"{self.BRIGHT_YELLOW}{msg}{self.RESET}"
        elif "HARD-KILLING" in msg or "Failed" in msg or "violation" in msg.lower() or "error" in msg.lower():
            msg = f"{self.BRIGHT_RED}{msg}{self.RESET}"

        formatted = f"{time_str} [{level_str}] {name_str}: {msg}"
        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"
        return formatted


def setup_logging(level_name: str = "INFO"):
    if sys.platform == "win32":
        os.system("")  # Enable ANSI terminal escape sequences on Windows
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(ColoredFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)


def parse_args():
    parser = argparse.ArgumentParser(description="Agentic Universe - Durable Campaign Dispatcher")
    parser.add_argument("--provider-url", default="http://127.0.0.1:8099", help="Telephony provider base URL")
    parser.add_argument("--db", default="dispatcher.db", help="Path to SQLite database file")
    parser.add_argument("--out", default="final_state.csv", help="Path for output final_state.csv")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind webhook server")
    parser.add_argument("--port", type=int, default=8200, help="Port to bind webhook server")
    parser.add_argument("--fixtures", default=DEFAULT_FIXTURES, help="Path to campaigns.json fixtures")
    parser.add_argument("--reset", action="store_true", help="Force wipe local database before starting")
    parser.add_argument("--linger", type=float, default=3.0, help="Seconds to linger after completion")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    return parser.parse_args()


async def run_dispatcher(args):
    setup_logging(args.log_level)
    logger = logging.getLogger("dispatcher")

    logger.info("Loading configuration and fixtures from: %s", args.fixtures)
    config, leads = load_config(args.fixtures)

    logger.info("Connecting to telephony provider at: %s", args.provider_url)
    try:
        clock = await ProviderClock.from_provider_async(args.provider_url)
    except Exception as exc:
        logger.error("Failed to connect to provider clock at %s: %s", args.provider_url, exc)
        sys.exit(1)

    logger.info("Provider clock synced: sim_now=%s, speed=%.1fx, real_epoch=%.2f",
                clock.now().isoformat(), clock.speed, clock.real_epoch)

    store = StateStore(args.db)
    wiped = store.check_fingerprint_or_reset(clock.real_epoch, force_reset=args.reset)
    if wiped:
        logger.warning("Provider real_epoch changed or --reset passed: database wiped clean")
    store.upsert_leads(leads)

    # Check if already completed (e.g. restart on finished DB)
    if store.is_all_finished():
        logger.info("All leads already in final state. Writing %s and exiting.", args.out)
        write_final_state_csv(store, config, args.out)
        return

    wake_event = asyncio.Event()
    callback_url = f"http://{args.host}:{args.port}/webhooks/telephony"
    provider = TelephonyClient(args.provider_url, callback_url=callback_url)

    # Webhook server
    app = create_webhook_app(store, config, wake_event)
    uvi_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(uvi_config)
    server_task = asyncio.create_task(server.serve())

    # Give server a moment to bind
    await asyncio.sleep(0.2)

    try:
        reconciler = Reconciler(store, provider, clock, config, wake_event)
        # Startup reconciliation
        await reconciler.run_startup_recovery()
        reconcile_task = asyncio.create_task(reconciler.run_periodic_loop(poll_interval_real_seconds=1.5))

        # Main dispatch loop
        dispatch = DispatchLoop(store, provider, clock, config, wake_event)
        await dispatch.run()

        # Stop periodic reconciler
        reconcile_task.cancel()
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass

        # Write final_state.csv
        logger.info("Writing output to %s...", args.out)
        write_final_state_csv(store, config, args.out)

        if args.linger > 0:
            logger.info("Lingering %.1f seconds for late webhooks...", args.linger)
            await asyncio.sleep(args.linger)

    finally:
        server.should_exit = True
        await server_task
        await provider.close()
        logger.info("Dispatcher shutdown cleanly")


def main():
    args = parse_args()
    asyncio.run(run_dispatcher(args))


if __name__ == "__main__":
    main()


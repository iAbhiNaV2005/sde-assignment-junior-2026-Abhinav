"""Chaos harness: runs mock telephony and dispatcher with random hard kills (kill -9 equivalent)."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import subprocess
import sys
import time

import httpx

class ColoredFormatter(logging.Formatter):
    """Clean, high-readability ANSI color formatter for chaos runner."""

    LEVEL_COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"
    DIM = "\033[90m"
    CYAN = "\033[36m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_YELLOW = "\033[93m"
    BOLD = "\033[1m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        time_str = f"{self.DIM}{self.formatTime(record, '%Y-%m-%d %H:%M:%S')}{self.RESET}"
        level_str = f"{color}{record.levelname:<7}{self.RESET}"
        name_str = f"{self.CYAN}{record.name}{self.RESET}"
        msg = record.getMessage()

        if "SUCCESS" in msg or "100% exact match" in msg:
            msg = f"{self.BOLD}{self.BRIGHT_GREEN}{msg}{self.RESET}"
        elif "HARD-KILLING" in msg or "terminated" in msg:
            msg = f"{self.BOLD}{self.BRIGHT_RED}{msg}{self.RESET}"
        elif "Restarting" in msg or "Kill " in msg or "Sleeping" in msg:
            msg = f"{self.BRIGHT_YELLOW}{msg}{self.RESET}"
        elif "CARRIER ADMIN REPORT" in msg or "===" in msg:
            msg = f"{self.BOLD}{self.CYAN}{msg}{self.RESET}"

        formatted = f"{time_str} [{level_str}] {name_str}: {msg}"
        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"
        return formatted


def setup_logging():
    if sys.platform == "win32":
        os.system("")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColoredFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


setup_logging()
logger = logging.getLogger("chaos")

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKFLOW_DIR = os.path.join(ROOT_DIR, "assignment-b-workflow")
SOLUTION_DIR = os.path.join(ROOT_DIR, "solution")


def parse_args():
    parser = argparse.ArgumentParser(description="Chaos test harness for Durable Campaign Dispatcher")
    parser.add_argument("--seed", type=int, default=123, help="RNG seed for mock carrier")
    parser.add_argument("--speed", type=float, default=1200.0, help="Simulated clock speed multiplier")
    parser.add_argument("--failure-scale", type=float, default=1.0, help="Carrier failure multiplier")
    parser.add_argument("--kills", type=int, default=4, help="Number of times to hard-kill dispatcher")
    parser.add_argument("--mock-port", type=int, default=8099, help="Mock provider port")
    parser.add_argument("--dispatcher-port", type=int, default=8200, help="Dispatcher port")
    return parser.parse_args()


def wait_for_url(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=1.0) as client:
                r = client.get(url)
                if r.status_code == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def run_chaos_test(args):
    db_file = os.path.join(SOLUTION_DIR, "chaos_test.db")
    csv_file = os.path.join(SOLUTION_DIR, "chaos_final_state.csv")

    for f in (db_file, f"{db_file}-wal", f"{db_file}-shm", csv_file):
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    mock_url = f"http://127.0.0.1:{args.mock_port}"
    clock_url = f"{mock_url}/v1/clock"

    logger.info("Starting mock telephony (seed=%d, speed=%.1fx, failure_scale=%.1f)...",
                args.seed, args.speed, args.failure_scale)

    mock_cmd = [
        sys.executable, "-m", "mock_telephony",
        "--port", str(args.mock_port),
        "--speed", str(args.speed),
        "--seed", str(args.seed),
        "--failure-scale", str(args.failure_scale),
    ]
    mock_proc = subprocess.Popen(mock_cmd, cwd=WORKFLOW_DIR)

    try:
        if not wait_for_url(clock_url, timeout=15.0):
            logger.error("Mock telephony failed to start at %s", clock_url)
            return False

        logger.info("Mock telephony ready at %s", mock_url)

        disp_cmd = [
            sys.executable, "-m", "dispatcher",
            "--provider-url", mock_url,
            "--port", str(args.dispatcher_port),
            "--db", db_file,
            "--out", csv_file,
            "--linger", "2.0",
        ]

        # Launch dispatcher
        logger.info("Starting dispatcher process...")
        disp_proc = subprocess.Popen(disp_cmd, cwd=SOLUTION_DIR)

        # Perform hard kills mid-run
        for k in range(1, args.kills + 1):
            sleep_sec = random.uniform(2.5, 5.0)
            logger.info("[Kill %d/%d] Sleeping %.1fs before next kill...", k, args.kills, sleep_sec)
            time.sleep(sleep_sec)

            if disp_proc.poll() is not None:
                logger.info("Dispatcher already exited with code %d before kill %d", disp_proc.returncode, k)
                break

            logger.warning("[Kill %d/%d] HARD-KILLING dispatcher process (PID=%d)...", k, args.kills, disp_proc.pid)
            disp_proc.kill()
            disp_proc.wait()
            logger.info("[Kill %d/%d] Dispatcher process terminated.", k, args.kills)

            time.sleep(0.8)
            logger.info("[Kill %d/%d] Restarting dispatcher from persistent state...", k, args.kills)
            disp_proc = subprocess.Popen(disp_cmd, cwd=SOLUTION_DIR)

        # Wait for dispatcher to complete normally (allowing sufficient time for the weekend gap)
        logger.info("Waiting for dispatcher to finish all campaigns...")
        disp_ret = disp_proc.wait(timeout=500.0)
        logger.info("Dispatcher exited with code %d", disp_ret)

        if disp_ret != 0:
            logger.error("Dispatcher exited with non-zero code %d", disp_ret)
            return False

        # Evaluate against carrier admin endpoints
        with httpx.Client(timeout=10.0) as client:
            report_resp = client.get(f"{mock_url}/admin/report")
            report = report_resp.json()

            leads_resp = client.get(f"{mock_url}/admin/leads")
            carrier_leads = leads_resp.json()

        logger.info("========== CARRIER ADMIN REPORT ==========")
        logger.info("Total calls placed: %d", report.get("calls_total", 0))
        logger.info("Calls by status: %s", report.get("calls_by_status", {}))
        logger.info("Violation count: %d", report.get("violation_count", 0))
        logger.info("Violations by type: %s", report.get("violations_by_type", {}))
        logger.info("Max concurrency seen: %s", report.get("max_concurrency_seen", {}))
        logger.info("Callback lateness: %s", report.get("callback_lateness_minutes", {}))
        logger.info("Webhooks: %s", report.get("webhooks", {}))
        logger.info("==========================================")

        # Verify output CSV exists
        if not os.path.exists(csv_file):
            logger.error("Output CSV %s was not created!", csv_file)
            return False

        csv_leads = {}
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                csv_leads[(row["campaign_id"], row["lead_id"])] = row

        logger.info("Read %d leads from %s. Verifying against %d carrier leads...",
                    len(csv_leads), csv_file, len(carrier_leads))

        mismatches = []
        for cl in carrier_leads:
            key = (cl["campaign_id"], cl["lead_id"])
            if key not in csv_leads:
                mismatches.append(f"Missing lead in CSV: {key}")
                continue

            my = csv_leads[key]
            if my["final_state"] != cl["expected_state"]:
                mismatches.append(f"{key}: state mismatch (mine='{my['final_state']}', carrier='{cl['expected_state']}')")
            if int(my["attempts"]) != int(cl["attempts"]):
                mismatches.append(f"{key}: attempts mismatch (mine={my['attempts']}, carrier={cl['attempts']})")
            if (my["last_status"] or "") != (cl["last_status"] or ""):
                mismatches.append(f"{key}: status mismatch (mine='{my['last_status']}', carrier='{cl['last_status']}')")
            if (my["last_disposition"] or "") != (cl["last_disposition"] or ""):
                mismatches.append(f"{key}: disp mismatch (mine='{my['last_disposition']}', carrier='{cl['last_disposition']}')")

        if mismatches:
            logger.error("Found %d lead verification mismatches!", len(mismatches))
            for m in mismatches[:15]:
                logger.error("  %s", m)
            return False

        if report.get("violation_count", 0) > 0:
            logger.error("Test failed: %d violations recorded by carrier!", report["violation_count"])
            for v in report.get("violations", []):
                logger.error("  Violation: %s", v)
            return False

        logger.info("SUCCESS: 0 violations, and 100%% exact match across all %d leads after %d kills!",
                    len(carrier_leads), args.kills)
        return True

    finally:
        mock_proc.kill()
        mock_proc.wait()
        logger.info("Mock telephony shut down.")


if __name__ == "__main__":
    args = parse_args()
    success = run_chaos_test(args)
    sys.exit(0 if success else 1)

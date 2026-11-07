"""Daily refresh for OSS Pulse.

Runs the ETL once, then waits 24 hours and runs it again, forever,
until stopped with Ctrl+C. The web app also refreshes on open when
the last run is over 24 hours old (see app.py), so the scheduler is
the background equivalent for a deployed server. A failed run never
wipes existing data: it is logged and the next run is still attempted.

Usage:
    python scheduler.py
"""
import time
import traceback

from oss_pulse.etl import read_repos_file, run

INTERVAL_SECONDS = 24 * 60 * 60  # daily


def main():
    repos = read_repos_file("repos.txt")
    while True:
        try:
            print("Running daily OSS Pulse refresh...")
            run(repos, "data/oss_pulse.db", allow_fallback=True)
        except Exception:  # noqa: BLE001 - keep the scheduler alive, log the failure
            traceback.print_exc()
        print("Next refresh in 24 hours. Press Ctrl+C to stop.")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

"""
main.py
-------
Command-line sync (no window). Uses the same settings.json and saved password as the app,
so sign in once in the app first ("Start TNB Sync.bat").

  python main.py                       sync the account selected in the app
  python main.py --account 210000000002
  python main.py --fill-gaps           also re-download incomplete days inside the history
  python main.py --folder D:\\copy      write to another folder (handy for testing on a copy)
  python main.py --list                show the accounts on your myTNB login
"""

from __future__ import annotations

import argparse
import logging
import sys

import config
import credentials
import sync
from excel_store import ExcelLockedError
from tnb_data import ApiError
from tnb_portal import LoginError, PortalError, TnbPortal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download myTNB 30-minute usage into Excel.")
    parser.add_argument("--account", help="myTNB account number (default: the one selected in the app)")
    parser.add_argument("--fill-gaps", action="store_true", help="re-download incomplete days in the history")
    parser.add_argument("--folder", help="Excel folder to use instead of the one in settings")
    parser.add_argument("--list", action="store_true", help="list accounts and exit")
    parser.add_argument("--attach", metavar="HOST:PORT",
                        help="use an already-open, logged-in Chrome (started with --remote-debugging-port)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(config.LOG_FILE, encoding="utf-8")])
    for noisy in ("selenium", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("tnb").info

    settings = config.load_settings()
    password = credentials.get_password(settings.get("email", ""))
    if not password and not args.attach:
        log("No saved password -- trying the browser's saved myTNB session.")

    def portal_factory(s: dict, log_fn):
        return TnbPortal(config.BROWSER_PROFILE_DIR, s.get("browser", "chrome"), log=log_fn, attach=args.attach)

    try:
        settings = sync.migrate_if_needed(settings, log)
        config.save_settings(settings)
        if args.list:
            accounts, settings, _ = sync.refresh_accounts(settings, password, log=log, portal_factory=portal_factory)
            config.save_settings(settings)
            for acc in accounts:
                log(f"  {acc['ca_no']}  {acc['name']}")
            return 0

        ca_no = args.account or settings.get("selected_account")
        if not ca_no:
            log("FAILED: no account selected yet. Sign in once in the app, or use --list / --account.")
            return 2
        result, settings = sync.sync_account(settings, ca_no, password, fill_gaps=args.fill_gaps,
                                             folder=args.folder, log=log,
                                             portal_factory=portal_factory)
        config.save_settings(settings)
    except LoginError as exc:
        log(f"FAILED: {exc} Open 'Start TNB Sync.bat' and sign in once.")
        return 2
    except (PortalError, ApiError, ExcelLockedError, ValueError) as exc:
        log(f"FAILED: {exc}")
        return 1

    log(f"Done. {result.rows_added} new reading(s) -> {result.file}")
    log(f"Data up to: {result.latest or '-'}   calls: {result.calls}")
    if result.gaps:
        log(f"{len(result.gaps)} incomplete day(s) in the history (run with --fill-gaps to retry): "
            + ", ".join(result.gaps[:15]) + (" ..." if len(result.gaps) > 15 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

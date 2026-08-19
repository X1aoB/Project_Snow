"""Dedicated least-privilege feedback receipt mail worker."""

from __future__ import annotations

import signal
import threading

from .config import PublicSettings
from .feedback_mailer import FeedbackMailer
from .public_store import PublicStore, PublicStoreUnavailable


def main() -> None:
    settings = PublicSettings.from_environment()
    store = PublicStore(settings.database_url)
    mailer = FeedbackMailer(settings, store)
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped.is_set():
        try:
            mailer.run_once(limit=10)
        except PublicStoreUnavailable:
            pass
        stopped.wait(30)


if __name__ == "__main__":
    main()

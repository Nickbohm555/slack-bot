from __future__ import annotations

from api_service.slack_server import run as run_slack_bot


def run() -> None:
    run_slack_bot()


if __name__ == "__main__":
    run()

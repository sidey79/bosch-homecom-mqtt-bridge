"""Command line entry point. The bridge itself follows in a later step (see PLAN.md)."""
import argparse
import asyncio
import sys

from . import __version__
from .config import ConfigError, load_config
from .logging_setup import setup_logging


def _login() -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    setup_logging(config.log_level, [config.mqtt_password or ""])
    # Imported here so that --version and the help work without the cloud dependencies.
    from .auth.login import login

    return asyncio.run(login(config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bosch-homecom-mqtt-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"bosch-homecom-mqtt-bridge {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser(
        "login",
        help="log in via browser and store the refresh token in BOSCH_AUTH_PATH",
        description="Interactive browser login. Prints a login URL, reads the code or redirect URL "
        "from the terminal and stores the refresh token in BOSCH_AUTH_PATH.",
    )
    args = parser.parse_args(argv)
    if args.command == "login":
        # No raw tracebacks: they may carry request data. Only the exception type is shown.
        try:
            return _login()
        except KeyboardInterrupt:
            print("Vorgang abgebrochen.", file=sys.stderr)
            return 1
        except Exception as error:
            print(f"Unerwarteter Fehler ({type(error).__name__}).", file=sys.stderr)
            return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

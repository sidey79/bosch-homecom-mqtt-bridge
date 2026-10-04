"""Command line entry point. The bridge itself follows in the next step (see PLAN.md)."""
import argparse

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="bosch-homecom-mqtt-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"bosch-homecom-mqtt-bridge {__version__}")
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()

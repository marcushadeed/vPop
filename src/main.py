"""Entry point for the vpop CLI."""

from data_gathering.android_messages.sync import sync


def main():
    """Entry point for the vpop CLI."""
    sync()


if __name__ == "__main__":
    main()

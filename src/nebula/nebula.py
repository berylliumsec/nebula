import sys

from .initial_logic import MainApplication


def main():
    """Entry point for the nebula command."""
    app = MainApplication(sys.argv)
    app.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

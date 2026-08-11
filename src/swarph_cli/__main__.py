"""Allow hook commands to invoke the installed Swarph package directly."""

from .main import main


if __name__ == "__main__":
    raise SystemExit(main())

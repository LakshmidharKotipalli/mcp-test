"""Entry point: python main.py --url <url> --goal "<goal>"  |  python main.py --suite <file>"""
import sys

from src.cli import main

if __name__ == "__main__":
    sys.exit(main())

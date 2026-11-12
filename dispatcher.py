"""Convenience runner for the Durable Campaign Dispatcher from repository root."""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from solution.dispatcher.__main__ import main

if __name__ == "__main__":
    main()


#!/usr/bin/env python3
"""CLI spelling for :mod:`execution_access_diagnose`."""

import sys

sys.dont_write_bytecode = True

from execution_access_diagnose import main


if __name__ == "__main__":
    raise SystemExit(main())

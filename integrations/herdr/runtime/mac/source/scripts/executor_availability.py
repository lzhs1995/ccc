#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in availability_contract."""
from availability_contract import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(main())

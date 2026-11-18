#!/usr/bin/env python3
"""Compatibility shim for the original engine module location."""

from runpod_ops.engine import *  # noqa: F401,F403

if __name__ == "__main__":
    import runpod_ops.engine as _engine

    _engine.main()

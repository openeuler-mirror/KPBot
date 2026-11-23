#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deprecated alias -- use apply_eplb.py (same interface, adds --mode check)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply_eplb  # noqa: E402

if __name__ == '__main__':
    apply_eplb.main()

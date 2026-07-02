"""Make server.py importable from the tests package."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

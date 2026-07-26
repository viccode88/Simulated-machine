import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EVENT_LOG_STDOUT", "0")
os.environ.setdefault("LAB_MODE", "true")

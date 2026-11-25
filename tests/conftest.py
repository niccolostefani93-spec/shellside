"""Every test runs against a throwaway DATA_HOME.

Set here, before any test module imports server.config — which resolves
DATA_HOME and seeds it at import — and set unconditionally, overriding
whatever the shell had: a test run must never read or write anyone's real
~/.config/shellwalk, nor migrate a checkout's old state into it.
"""
import os
import sys
import tempfile

os.environ["SHELLWALK_HOME"] = tempfile.mkdtemp(prefix="shellwalk-test-home-")
os.environ.pop("SHELLWALK_BIND_HOST", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

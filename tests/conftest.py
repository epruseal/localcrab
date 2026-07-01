"""Ensure the tests directory is importable so sibling helper modules
(e.g. ``_vec_helpers``) can be imported by test modules regardless of pytest's
import mode."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

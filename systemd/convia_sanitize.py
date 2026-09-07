#!/usr/bin/env python3
"""Point d entree du sanitizer des conversations IA. Voir /opt/convia-sanitize/."""
import runpy, sys
sys.path.insert(0, "/opt/convia-sanitize")
runpy.run_path("/opt/convia-sanitize/runner.py", run_name="__main__")

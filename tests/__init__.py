"""Marks tests/ as a package so helper modules import as ``tests.*`` under
both ``python -m pytest`` and the installed ``pytest`` console-script entry
point (the latter does not put the repo root on sys.path, so a namespace
package would not resolve). See tests/_trial_audio.py."""

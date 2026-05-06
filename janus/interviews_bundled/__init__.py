# Marker so setuptools treats janus/interviews_bundled/ as a sub-package
# and ships the bundled .md question files via
# [tool.setuptools.package-data]. The interview loader
# (janus/interviews.py:maybe_install_bundled) reads from this directory
# by path on first launch; nothing imports from this module.

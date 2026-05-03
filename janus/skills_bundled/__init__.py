# Marker so setuptools treats janus/skills_bundled/ as a sub-package and
# ships the bundled .md files via [tool.setuptools.package-data]. The
# catalog loader (janus/skill_catalog.py) reads from this directory by
# path; nothing imports from this module.

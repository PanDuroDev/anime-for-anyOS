from setuptools import setup
from Cython.Build import cythonize

setup(
    name="anime_cli",
    ext_modules=cythonize("anime_cli.py", compiler_directives={"language_level": "3"}),
)

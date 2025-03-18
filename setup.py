#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import io
from setuptools import setup, find_packages

# Package meta-data
NAME = 'nebula-ai'
DESCRIPTION = 'AI-Powered Ethical Hacking Assistant'
URL = 'https://github.com/berylliumsec/nebula'
EMAIL = 'david@berylliumsec.com'
AUTHOR = 'David I'
REQUIRES_PYTHON = '>=3.11,<3.12'
VERSION = '2.0.0b8'  # Read from pyproject.toml

# What packages are required for this module to be executed?
with open('requirements.txt') as f:
    REQUIRED = f.read().splitlines()

# Import the README and use it as the long-description.
try:
    with io.open(os.path.join(os.path.abspath(os.path.dirname(__file__)), 'README.md'), encoding='utf-8') as f:
        long_description = '\n' + f.read()
except FileNotFoundError:
    long_description = DESCRIPTION

# Where the magic happens:
setup(
    name=NAME,
    version=VERSION,
    description=DESCRIPTION,
    long_description=long_description,
    long_description_content_type='text/markdown',
    author=AUTHOR,
    author_email=EMAIL,
    python_requires=REQUIRES_PYTHON,
    url=URL,
    packages=find_packages(where='src'),
    package_dir={'': 'src'},
    install_requires=REQUIRED,
    include_package_data=True,
    license='BSD',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.11',
    ],
    entry_points={
        'console_scripts': [
            'nebula=nebula.nebula:main',
        ],
    },
    package_data={
        'nebula': [
            'images/*',
            'Images_readme/*',
            'command_search_index/*',
            'config/*',
        ],
    },
)
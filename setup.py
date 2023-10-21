from setuptools import find_packages, setup

setup(
    name="nebula-ai",
    version="1.0.9b24",
    description="AI-Powered Ethical Hacking Assistant",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="David I",
    author_email="david@berylliumsec.com",
    url="https://github.com/berylliumsec/nebula",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Programming Language :: Python :: 3",
    ],
    license="BSD",
    keywords="AI, ethical hacking, nmap, zap, crackmapexec",
    # Explicitly define where the packages are
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "nebula": [
            "images/*",
            "indexdir/*",
            "indexdir_auto/*",
            "nmap_flags",
            "crackmap_flags",
            "nuclei_flags",
            "zap_flags",
            "suggestions",
        ]
    },
    install_requires=[
        "argparse",
        "typing",
        "termcolor",
        "torch",
        "tqdm",
        "transformers",
        "whoosh",
        "pyspellchecker",
        "psutil",
        "nvidia-ml-py3",
        "prompt-toolkit",
    ],
    entry_points={
        "console_scripts": ["nebula = nebula.nebula:main_func"],
    },
    python_requires=">=3.10",
)

"""
Setup script for Sentinel Home application.
"""

from setuptools import setup, find_packages

setup(
    name="sentinel",
    version="1.0.0",
    description="Motion detection application with text-to-speech alerts",
    author="Luke Biller",
    author_email="your.email@example.com",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        "opencv-python>=4.5.0",
        "numpy>=1.20.0",
        "pyttsx3>=2.90",
        "PySide6>=6.0.0",
    ],
    entry_points={
        "console_scripts": [
            "sentinel=main:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Multimedia :: Video",
        "Topic :: Multimedia :: Sound/Audio :: Speech",
    ],
    python_requires=">=3.6",
)

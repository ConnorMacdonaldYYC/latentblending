from setuptools import find_packages, setup

# Read requirements.txt and store its contents in a list
with open("requirements.txt") as f:
    required = f.read().splitlines()

setup(
    name="latentblending",
    version="0.3",
    url="https://github.com/lunarring/latentblending",
    description="Butter-smooth video transitions",
    long_description=open("README.md").read(),
    install_requires=required,
    include_package_data=False,
)

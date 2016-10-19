#!/bin/sh
set -e
set -x

# Install the system packages needed for building the PyInstaller based binary
apk -U add --virtual temp python-dev py-pip alpine-sdk python py-setuptools

# Install python dependencies
pip install --upgrade pip

pip install urllib3==1.17
pip install ConcurrentLogHandler==0.9.1
pip install docker-py==1.7.2
pip install docopt==0.6.2
pip install netaddr==0.7.18
pip install prettytable==0.7.2
pip install PyInstaller==3.1.1
pip install PyYAML==3.11
pip install requests==2.9.1

pip install -r https://raw.githubusercontent.com/projectcalico/libcalico/v0.14.0/build-requirements.txt
pip install git+https://github.com/projectcalico/libcalico.git@v0.14.0
pip install simplejson 

# Produce a binary - outputs to /dist/policy_agent
pyinstaller /code/policy_agent.py -ayF

# Cleanup everything that was installed now that we have a self contained binary
apk del temp && rm -rf /var/cache/apk/*
rm -rf /usr/lib/python2.7

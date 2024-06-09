import subprocess
import sys

try:
    __import__('webvtt')
except:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-U', 'webvtt-py'])

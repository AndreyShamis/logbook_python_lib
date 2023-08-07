import multiprocessing
import os
import json
import re
import socket
import string
import time
import xml.etree.ElementTree as ET
from glob import glob
from typing import Dict, List, Optional, Tuple, Union

try:
    import psutil
except ImportError:
    pass

JOB_NAME = 'JOB_NAME'
BUILD_ID = 'BUILD_ID'


def list_folder_in_folder(path: str) -> List[str]:
    return [x for x in os.listdir(path) if os.path.isdir(os.path.join(path, x))]


def get_cpu_load() -> float:
    try:
        return psutil.cpu_percent()
    except Exception:
        pass

    proc_number = multiprocessing.cpu_count()
    _value = float(os.getloadavg()[0] * proc_number)
    if _value > 100:
        _value = 100
    return _value

def get_environ(key: str, default: str = '') -> str:
    return os.environ.get(key, default)


def get_cpu_load_avg(avg_counter: int = 10) -> float:
    summary = 0
    if avg_counter < 1:
        avg_counter = 1
    for x in range(0, avg_counter):
        summary += get_cpu_load()
        time.sleep(0.001)
    return round(summary / avg_counter, 2)


def get_hostname() -> str:
    ret = ''
    try:
        ret = socket.gethostname()
    except Exception:
        try:
            ret = f'{os.uname()[1]}'
        except Exception:
            pass
    return ret.strip()


def is_jenkins() -> bool:
    return os.environ.get(BUILD_ID, '') != ''

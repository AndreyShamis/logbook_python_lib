import json
import os
import platform
import random
import re
import socket
import string
import threading
import time
import urllib.request
from threading import Thread
from typing import List, Dict, Optional
import logging
import psutil
from logbook.log_book_post_encoder import LogBookPostEncoder
from logbook.logbook_api import LogbookAPI
from logbook.logbook_cycle import LogBookCycle
from logbook.functions import (get_cpu_load_avg, get_environ, get_hostname, is_jenkins)

from platform import node


TOKEN_LEN = 30
MAX_TOKEN_LEN = 150
DEBUG_LOGBOOK_DOMAIN = "127.0.0.1:8080"

URL_PATTERN = "http://{}/upload/new_cli"
SUITE_EXECUTION_URL_PATTERN = "http://{}/upload/create_suite_execution"
SE_CLOSE_URL_PATTERN = "http://{}/suites/close/{}"
MIN_TOKEN_LEN = 20
UPLOAD_TIMEOUT = 60
SUITE_CREATE_TIMEOUT = 60
SUITE_CLOSE_TIMEOUT = 60
MAX_UPLOADS_SAME_TIME = 10   # Max uploads in same time
WAIT_TIME_BEFORE_START_UPLOAD = 10   # Time to wait for MAX_UPLOADS_SAME_TIME exit
MAX_UPLOAD_SIZE: int = 15  # max file size in megabytes


class LogBookUploader(object):
    """
    Logbook Uploader object
    """
    # url = LOGBOOK_URL   # DEBUG_LOGBOOK_URL
    _DEF_FILE = 'debug/autoserv.DEBUG'

    def __init__(self, setup_name: Optional[str] = None, cycle_name: str = '', token: Optional[str] = None,
                 url: Optional[str] = None, domain: str = LogbookAPI.domain, *args, **dargs):
        self.domain = domain
        if url is not None:
            self.url = url
        else:
            self.url = URL_PATTERN.format(self.domain)
        self.suite_execution_url: str = SUITE_EXECUTION_URL_PATTERN.format(self.domain)
        self._threads: List[Thread] = []
        self._token = None
        self._user = None
        self._setup_name = None
        self._cycle_name = None
        self._build_name = None
        self._test_metadata = None
        self._cycle_metadata = ''
        self.progress: int = 0
        self._pre_post_aw_sem = threading.Semaphore()
        self.test_count: int = 0
        self.set_token(token)
        self.set_cycle_name(cycle_name)
        self.set_setup_name(setup_name)
        self.timeout_count: int = 0
        self.failures_count: int = 0
        self.uploads_success: int = 0
        self._suite_execution_id: int = 0
        self._is_commit: bool = False
        self.exit: bool = False
        async_thread: Thread = threading.Thread(target=self.threads_keeper)
        async_thread.setName("threads_keeper")
        async_thread.start()
        keep_alive: Thread = threading.Thread(target=self.keep_alive_keeper)
        keep_alive.setName("keep_alive")
        keep_alive.start()

    def keep_alive_keeper(self):
        counter = 0
        # delay start
        time.sleep(1)
        while not self.exit:
            try:
                # wait for some time but very small to increase counter
                time.sleep(1)
                if counter == 500:
                    self.keep_alive_thread()
            except Exception as ex:
                logging.exception(ex)
            counter += 1
            if counter > 10:
                counter = 0

    def keep_alive_thread(self) -> bool:
        """
        Uploads log file to logbook site
        """
        se = {}
        se['token'] = self.get_token()
        se['setup_name'] = self.get_setup_name()
        try:
            headers = {'Content-Type': 'application/json'}
            lbk_api = LogbookAPI()
            try:
                self.progress += 1
                r = lbk_api.urlopen_lbk(
                    # url=self.suite_execution_url,
                    url='cycle/api/token/keep_alive',
                    method='POST',
                    data=se,
                    headers=headers,
                    verbose=False,
                    sleep_on_error=0,
                    timeout=SUITE_CREATE_TIMEOUT
                )
                logging.debug(f'Increase cycleTokenKeepAlive result:{r}')
            except (socket.timeout, Exception) as ex:
                if hasattr(ex, 'args') and len(ex.args) and 'Name or service not known' in ex.args[0]:
                    return True
                logging.error(f"[keep_alive_thread]: {ex}")
            finally:
                self.progress -= 1
            return True
        except Exception as ex:
            logging.exception(f'{ex}')
        return False

    def current_count(self) -> int:
        return len(self._threads)

    def _get_lbk_cpu_load(self):
        val = 100
        try:
            api = LogbookAPI()
            ret = api.urlopen_lbk('api/cpu_load', 'GET')
            val = round(100 * float(ret['CPU'][0])/16, 2)
        except Exception as ex:
            logging.exception(ex)
        return float(val)

    def threads_keeper(self):
        while not self.exit:
            try:
                time.sleep(0.001)
                for counter in range(0, len(self._threads)):
                    for _t in self._threads:
                        if not _t.is_alive() and _t.ident is not None and _t.ident > 0:
                            _t.join(0)
                            self._threads.remove(_t)
                            break
                        if not _t.is_alive() and _t.ident is None:
                            if self.get_progress() <= 3:
                                _t.start()
                            break

                        if self.get_progress() > 10:
                            break
            except Exception as ex:
                logging.exception(ex)

    def get_threads_uploads(self) -> int:
        with self._pre_post_aw_sem:
            return len(self._threads)

    def get_progress(self) -> int:
        with self._pre_post_aw_sem:
            return self.progress

    def is_commit(self) -> bool:
        return self._is_commit

    def set_as_commit(self) -> None:
        """
        Set true if execution from CI commit
        """
        self._is_commit = True

    def get_suite_execution_id(self) -> int:
        return self._suite_execution_id

    def _get_post_fields(self, suite: Optional[Suite] = None, test: Optional[TestCaseBase] = None,
                         fcmu_errors: List = []) -> List:
        if test is None:
            test = TestCaseBase()

        fields = [
            ('return_urls_only', 'true'),
            ('token', self.get_token()),
            ('setup', self.get_setup_name()),
            ('user', self.get_user()),
            ('tests_count', self.test_count),
            ('build', self.get_build_name()),
            ('test_metadata', self.get_test_metadata()),
            ('cycle_metadata', self.get_cycle_metadata()),
            ('cycle', self.get_cycle_name()),
            ('test_name', test.get_test_name()),
            ('fcmu_errors', fcmu_errors),
            ('control_file', test.get_control_path()),
            ('test_weight', test.get_weight()),
            ('test_timeout', test.get_timeout()),
            ('test_exit_code', test.get_test_result()),  # 0
            ('suite_execution_id', self.get_suite_execution_id())
        ]
        try:
            fields.append(('test_result', test.get_test_case_result().get_result_str()))  # PASSED
        except Exception:
            pass
        return fields

    def save(self):
        """
        Save LogBookUploader into LogBookCycle object
        :return: file name
        """
        cycle = LogBookCycle()
        cycle.token = self.get_token()
        cycle.cycle_name = self.get_cycle_name()
        cycle.setup_name = self.get_setup_name()
        cycle.test_count = self.test_count
        cycle.build_name = self.get_build_name()
        return cycle.save()

    @classmethod
    def load(cls: "LogBookUploader", file_name: str) -> "LogBookUploader":
        """
        Load LogBookUploader from LogBookCycle object
        """
        cycle = LogBookCycle()
        cycle.load(file_name)
        new_cls = cls(setup_name=cycle.setup_name, cycle_name=cycle.cycle_name, token=cycle.token)
        # new_cls.set_token(cycle.token)
        # new_cls.set_cycle_name(cycle.cycle_name)
        # new_cls.set_setup_name(cycle.setup_name)
        new_cls.set_build_name(cycle.build_name)
        new_cls.test_count = cycle.test_count
        return new_cls

    def reset(self) -> None:
        self.set_token(LogBookUploader.get_random_string(TOKEN_LEN))

    def set_user(self, new_user: str) -> "LogBookUploader":
        if new_user is None:
            new_user = ""
        self._user = str(new_user)
        return self

    def get_user(self) -> str:
        if self._user is None:
            self.set_user("")
        return self._user

    def set_setup_name(self, new_setup_name: str) -> "LogBookUploader":
        if new_setup_name is None:
            new_setup_name = ""
        self._setup_name = str(new_setup_name)
        return self

    def get_setup_name(self) -> str:
        return self._setup_name

    def set_token(self, token: Optional[str] = None) -> "LogBookUploader":
        if token is None:
            self._token = LogBookUploader.get_random_string(TOKEN_LEN)
        else:
            self._token = token
        return self

    def get_token(self) -> str:
        return self._token

    def reset_cycle_metadata(self) -> None:
        """
        Reset cycle metadata
        """
        self.set_cycle_metadata('')

    def set_cycle_metadata(self, metadata: str) -> None:
        """
        Set cycle metadata
        """
        self._cycle_metadata = metadata

    def add_cycle_metadata(self, key: str, value: str) -> None:
        """
        Add cycle metadata
        """
        if len(self._cycle_metadata) == 0:
            self._cycle_metadata = f'{key}::{value};;'
        else:
            self._cycle_metadata = f'{self._cycle_metadata};;{key}::{value};;'
        self._cycle_metadata = self._cycle_metadata.replace(';;;;', ';;')

    def get_cycle_metadata(self) -> str:
        if self._cycle_metadata is None:
            return ''
        return self._cycle_metadata

    def set_test_metadata(self, metadata: str) -> None:
        """
        Set test metadata
        """
        self._test_metadata = metadata

    def get_test_metadata(self) -> str:
        if self._test_metadata is None:
            return ''
        return self._test_metadata

    def get_build_name(self) -> str:
        if self._build_name is None:
            self.set_build_name("")
        return self._build_name

    def set_build_name(self, build_name: str) -> None:
        if build_name is None:
            build_name = ""
        self._build_name = build_name

    def get_cycle_name(self) -> str:
        return self._cycle_name

    def set_cycle_name(self, cycle_name: str) -> "LogBookUploader":
        self._cycle_name = str(cycle_name)
        return self

    def close_suite_execution(self, suite: Suite, suite_execution: Dict, print_response: bool = False,
                              verbose: bool = True, *args, **dargs) -> bool:
        """
        Uploads log file to logbook site
        """
        if self._suite_execution_id > 0:
            se = {}
            try:
                files = []
                # se = suite_execution.copy()
                headers = {'Content-Type': 'application/json'}
                proxy_handler = urllib.request.ProxyHandler({})
                opener = urllib.request.build_opener(proxy_handler)
                data = json.dumps(se)
                data = str(data)    # Convert to String
                data = data.encode('utf-8')     # Convert string to byte
                _url: str = SE_CLOSE_URL_PATTERN.format(self.domain, self._suite_execution_id)
                the_request = urllib.request.Request(url=_url, data=data, headers=headers)
                try:
                    self.progress += 1
                    response = opener.open(the_request, timeout=SUITE_CLOSE_TIMEOUT)
                    readed = ''
                    if response.code == 200:
                        try:
                            readed = LogBookUploader._response_to_one_line(response)
                            self._suite_execution_id = suite.logbook_end_handler(readed)
                            logging.info('LogBook Suite Tests http://{dom}/cycle/suiteid/{sid} \t'
                                                  ' Suite Info http://{dom}/suites/cycle/{sid} '.format(
                                dom=LogbookAPI.domain, sid=self._suite_execution_id))
                        except Exception as ex:
                            logging.error(ex)
                    if print_response:
                        logging.error(readed)
                except (socket.timeout, Exception) as ex:
                    if hasattr(ex, 'args') and len(ex.args) and 'Name or service not known' in ex.args[0]:
                        return True
                    logging.error(f"[close_suite_execution]: {ex}")
                finally:
                    self.progress -= 1
                return True
            except Exception as ex:
                if verbose:
                    logging.error(f'Failed is suiteExecution closer {ex}')
                    logging.exception(ex)
                    logging.debug(f'{se}')
        return False

    def create_suite_execution(self, suite: Suite, suite_execution: Dict, print_response: bool = False,
                               verbose: bool = True, **dargs) -> bool:
        """
        Uploads log file to logbook site
        """
        se = {}
        try:
            se = suite_execution.copy()
            se['components'] = {}
            se['suite_dict'] = suite.get_json_dict()
            se['description'] = ''
            se['package_mode'] = 'regular_mode'
            se['build_flavor'] = BUILD_TYPE
            if os.environ.get('target_flavor', ''):
                plat_flavor = os.environ['target_flavor']
                plat = suite.get_platform_str().lower()
                if '_' in plat_flavor and plat in plat_flavor:
                    _lst = plat_flavor.split('_')
                    _lst.remove(plat)
                    plat_flavor = '_'.join(_lst)
                se['platform_flavor'] = plat_flavor

            if suite.get_package_mode():
                se['package_mode'] = suite.get_package_mode()
            se['product_version'] = ''
            try:
                if node().startswith('epm') and 'farm' in node() and os.environ.get('MINION_ID', '').strip().isdigit():
                    se['hostname'] = f"{get_hostname()}_{os.environ.get('MINION_ID', '').strip()}"
                else:
                    se['hostname'] = get_hostname()
            except Exception:
                se['hostname'] = get_hostname()
            try:
                se['host_uptime'] = int(psutil.boot_time())
                mem = psutil.virtual_memory()
                se['host_memory_total'] = int(mem.total/1024/1042)
                se['host_memory_free'] = int(mem.free/1024/1042)

            except Exception:
                pass
            try:
                se['host_cpu_usage'] = get_cpu_load_avg()
                se['host_system'] = platform.system()
                se['host_release'] = platform.release()
                se['host_version'] = platform.version()
                se['host_python_version'] = platform.python_version()
                se['host_user'] = os.getlogin()
                se['host_cpu_count'] = os.cpu_count()
            except Exception:
                pass

            try:
                se['product_version'] = suite_execution['prod_v']
                del se['prod_v']
            except Exception:
                pass
            try:
                project_list_ready = []
                project: str = get_environ('GERRIT_PROJECT', os.environ.get('GITLAB_PROJECT', ''))
                if project.strip():
                    se['GERRIT_PROJECT'] = project.strip()
            except Exception:
                pass
            try:
                clusters_list_ready = []
                clusters: str = get_environ('CLUSTER', '')
                if clusters:
                    clusters_list: List = clusters.split(',')
                    if clusters_list:
                        for x in clusters_list:
                            if x.strip() not in clusters_list_ready:
                                clusters_list_ready.append(x.strip())
                se['clusters'] = clusters_list_ready
            except Exception:
                pass
            try:
                se['description'] = suite_execution['desc']
                del se['desc']
            except Exception:
                pass
            try:
                se['components'] = suite_execution['_components']
                del se['_components']
            except Exception:
                pass

            gb: str = get_environ('GERRIT_BRANCH', os.environ.get('GITLAB_BRANCH', ''))
            mr: str = get_environ('MANIFEST_REVISION')
            if gb and len(gb) > 3:
                se['GERRIT_BRANCH'] = gb
            elif mr and len(mr) > 3:
                se['GERRIT_BRANCH'] = mr
            se['is_jenkins'] = is_jenkins()
            if is_jenkins():
                try:
                    gpsn: str = get_environ('GERRIT_PATCHSET_NUMBER', os.environ.get('GITLAB_HASH', ''))
                    gcn: str = get_environ('GERRIT_CHANGE_NUMBER', os.environ.get('GITLAB_MR_IID', ''))
                    gci: str = get_environ('GERRIT_CHANGE_ID', os.environ.get('GITLAB_HASH', ''))
                    if gcn != '' and int(gcn) > 0 and gci != '' and gpsn != '' and gb != '':
                        se['PRE_COMMIT'] = '{}_{}_{}_'.format(gb, gci, gcn, gpsn)
                except:
                    pass

            headers = {'Content-Type': 'application/json'}
            lbk_api = LogbookAPI()
            try:
                self.progress += 1
                r = lbk_api.urlopen_lbk(
                    # url=self.suite_execution_url,
                    url='upload/create_suite_execution',
                    method='POST',
                    data=se,
                    headers=headers,
                    verbose=False,
                    sleep_on_error=0,
                    timeout=SUITE_CREATE_TIMEOUT
                )
                filters = []
                if 'FILTERS' in r:
                    filters = r['FILTERS']
                    suite.set_filters(filters)
                if 'SUITE_EXECUTION_ID' in r:
                    sid = r['SUITE_EXECUTION_ID']
                    self._suite_execution_id = suite.set_suite_execution_id(sid)
                    logging.warning(f"LogBook Suite Execution http://{LogbookAPI.domain}/suites/show/{sid} for "
                                   f"{se['summary']}. Filters {len(filters)}")
            except (socket.timeout, Exception) as ex:
                if hasattr(ex, 'args') and len(ex.args) and 'Name or service not known' in ex.args[0]:
                    return True
                logging.error(f"[create_suite_execution]: {ex}")
            finally:
                self.progress -= 1
            return True
        except Exception as ex:
            if logging:
                logging.error(f'Failed is suiteExecution creation {ex}')
                logging.exception(ex)
                logging.debug(f'{se}')
        return False

    def find_fcmu_errors(self, fpath: str) -> List:
        ret = []
        try:
            # ] fcmu: FCMU-A error       GEM0[6] | 1.mc 1.m.c
            pattern = re.compile(".*\[\s*(\d+\.\d+)\s*\] (fcmu: FCMU-(A|B) error\s*(\w*)\[(\d+)\].*)")
            a_found = b_found = False
            current_fcmu = {}
            current_fcmu['block'] = ''
            current_fcmu['fault'] = ''
            current_fcmu['a_value'] = ''
            current_fcmu['b_value'] = ''
            current_fcmu['a_time'] = ''
            current_fcmu['b_time'] = ''
            current_fcmu_src = current_fcmu.copy()
            try:
                for i, line in enumerate(open(fpath)):
                    for match in re.finditer(pattern, line):
                        try:
                            data = match.groups()
                            if data[2] == 'A':
                                if a_found and not b_found:
                                    ret.append(current_fcmu)
                                    current_fcmu = current_fcmu_src.copy()
                                a_found = True
                                current_fcmu['block'] = data[3]
                                current_fcmu['fault'] = data[4]
                                current_fcmu['a_value'] = data[1]
                                current_fcmu['a_time'] = data[0]
                            if data[2] == 'B':
                                b_found = True
                                if not a_found:
                                    current_fcmu['block'] = data[3]
                                    current_fcmu['fault'] = data[4]
                                current_fcmu['b_value'] = data[1]
                                current_fcmu['b_time'] = data[0]
                            if a_found and b_found:
                                ret.append(current_fcmu)
                                current_fcmu = current_fcmu_src.copy()
                                a_found = b_found = False
                        except Exception as ex:
                            logging.error(ex)
            except Exception as ex:
                logging.error(ex)
        except Exception as ex:
            logging.error(ex)
        return ret

    def post(self, logs_path: str, print_response: bool = False, verbose: bool =False, **dargs) -> bool:
        """
        Uploads log file to logbook site
        """
        with self._pre_post_aw_sem:
            self.progress += 1
            self.test_count += 1

        if not os.path.isdir(logs_path):
            logging.error(f"Provided path not exist : [{logs_path}].")
            return False
        upload_file = os.path.join(logs_path, LogBookUploader._DEF_FILE)
        if not os.path.exists(upload_file):
            logging.error(f"File for upload not found : [{upload_file}].")
            return False
        files = [('file', os.path.basename(upload_file), upload_file)]

        test: TestCaseBase = dargs.get('test', None)
        suite: Suite = dargs.get('suite', None)
        fcmu_errors = self.find_fcmu_errors(upload_file)
        fields = self._get_post_fields(suite, test, fcmu_errors)

        try:
            content_type, body = LogBookPostEncoder().encode(fields, files)
            headers = {'Content-Type': content_type}
            # For disable proxy
            proxy_handler = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(proxy_handler)
            the_request = urllib.request.Request(url=self.url, data=body, headers=headers)
            response = opener.open(the_request, timeout=UPLOAD_TIMEOUT)
            # response = urllib2.urlopen(the_request, timeout=UPLOAD_TIMEOUT)
            readed = ''
            if response.code == 200:
                try:
                    readed = LogBookUploader._response_to_one_line(response)
                    if test is not None:
                        test.logbook_end_handler(readed)
                    if self.test_count % 20 == 0:
                        self.reset_cycle_metadata()
                except Exception as ex:
                    logging.exception(ex)
            if print_response:
                if test is not None and 'Link to detailed' in readed:
                    logging.error(f'{readed} (for test:[{test.get_testname()}] [{suite.get_short_name()}])')
                else:
                    logging.error(f'{readed}')

                LogBookUploader._print_response(response, f"File {self.test_count}")
            if verbose:
                logging.info(f"File {upload_file} uploaded.")
            self.uploads_success += 1
        except socket.timeout as ex:
            self.timeout_count += 1
            self.failures_count += 1
            logging.error(f'[LogBookUploader:POST] timeout found... Number of timeouts {self.timeout_count}')
        except Exception as ex:
            logging.error(ex)
            self.failures_count += 1
        finally:
            with self._pre_post_aw_sem:
                self.progress -= 1
        return True

    def post_async(self, logs_path: str, print_response: bool = False, verbose: bool = False, *args, **dargs) -> bool:
        log_size_m = 0
        try:
            try:
                tmp_path = os.path.join(logs_path, 'debug/autoserv.DEBUG')
                try:
                    log_size = os.path.getsize(tmp_path)
                except FileNotFoundError as e:
                    return False
                log_size_m = round(log_size / 1024 / 1024, 3)
                if log_size_m > MAX_UPLOAD_SIZE:
                    logging.warning(f'Skip lbk upload for {logs_path} due size {log_size_m} > {MAX_UPLOAD_SIZE}Mb')
                    return False
            except Exception as ex:
                logging.exception(ex)
            if verbose:
                logging.info(f"Uploading file {logs_path} in thread.")
            async_thread = threading.Thread(target=self.post, args=(logs_path, print_response, verbose), kwargs=dargs)
            async_thread.setName(f"{self.test_count + 1}")
            self._threads.append(async_thread)
            if log_size_m > 5:
                time.sleep(15)
        except Exception as ex:
            logging.exception(str(ex))
            return False
        return True

    def wait_for_close(self, wait_time: int = 20, verbose: bool = True) -> bool:
        res = True
        if self.get_progress() > 0:
            time.sleep(0.1)
        if verbose and len(self._threads) != 1 and self.get_progress() != 1:
            logging.info(f"Threads opened {len(self._threads)}. Progress={self.get_progress()}.")
            if self.get_progress() > 0:
                time.sleep(0.05)
        start_wait = time.time()
        while (self.get_progress() or self.current_count()) and start_wait + wait_time + 10 > time.time():
            if self.get_progress() > 0:
                time.sleep(0.1)
                loop_counter = 0
                while start_wait + wait_time > time.time() and self.get_progress() > 0:
                    time.sleep(0.2)
                    loop_counter += 1
                    if verbose and self.get_progress() > 0:
                        time.sleep(0.1)
                        if self.get_progress():
                            if loop_counter % 5 == 0:
                                _st = round((start_wait + wait_time) - time.time(), 1)
                                logging.info(f"{self.get_progress()} thread in progress, wait for {_st} seconds.")
                            time.sleep(1)
            else:
                time.sleep(0.1)
        self.exit = True
        if start_wait + wait_time < time.time() and self.get_progress() > 0:
            logging.error(f"Timeout on wait, in progress {self.get_progress()} uploads.")
            res = False
        try:
            for th in self._threads:
                try:
                    if th.is_alive():
                        th.join(0)
                except Exception as ex:
                    logging.exception(ex)
        except Exception as ex:
            logging.exception(ex)
        return res

    @staticmethod
    def _print_response(response, r_name=''):
        if len(r_name) > 0:
            r_name = f"{r_name}: "
        try:
            for line in response:
                tmp_line = line.replace('\n', ' ').replace('\r', '')
                print((f"{r_name}{tmp_line}"))
        except Exception:
            pass

    @staticmethod
    def _response_to_one_line(response):
        ret = ''
        try:
            for line in response:
                try:
                    if isinstance(line, bytes):
                        line = line.decode('utf8')
                except:
                    pass
                tmp_line = line.replace('\n', ' ').replace('\r', '')
                ret = f"{ret}{tmp_line}"
        except Exception:
            pass
        return ret

    @staticmethod
    def get_random_string(length: int = 50, silent: bool = True) -> str:
        """
        Generate random string
        :param length: Length of string
        :param silent: ignore errors
        :return:
        """
        if length < MIN_TOKEN_LEN:
            if silent:
                length = 30
            else:
                raise Exception(f"The min str len should be {MIN_TOKEN_LEN}, but {length} provided")
        if length > MAX_TOKEN_LEN:
            if silent:
                length = 31
            else:
                raise Exception(f"The max str len should be {MAX_TOKEN_LEN}, but {length} provided")
        return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(length))

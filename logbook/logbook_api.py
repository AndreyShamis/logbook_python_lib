import json
import ssl
import ast
import time
import urllib.request
import threading
import os
import logging
from typing import List, Dict, Optional


LOGBOOK_ERROR = 34


class AutomationException(Exception):
    def __init__(self, message: str = '', exit_code: int = 0, ex: Exception = None):
        self.message = message
        self.exit_code = exit_code
        self.ex = ex

    def __str__(self):
        return f'[{self.exit_code} - {self.message}]'


class LogbookException(AutomationException):
    def __init__(self, message='', exit_code=LOGBOOK_ERROR, ex=None):
        super(LogbookException, self).__init__(message, exit_code, ex)


class LogbookAPI(object):
    domain = ''
    THREADS: List[threading.Thread] = []

    def __init__(self, domain: str, verbose: bool = False):
        self._logbook_url: str = f'http://{domain}'
        LogbookAPI.domain = domain
        self.verbose: bool = verbose
        self.ex_msg = 'Failed for request {req}, with response: {res}, with exit code: {ex_code}'

    def get_lbk_url(self) -> str:
        return self._logbook_url

    def urlopen_lbk(self, url: str, method: str, data: Optional[Dict], headers: Optional[Dict], verbose: bool = True, sleep_on_error: int = 30, timeout: int = 120) -> Dict:
        try:
            expired_timeout = timeout
            url = f'{self.get_lbk_url()}/{url}'
            try:
                if headers:
                    _r = urllib.request.Request(url=url, headers=headers)
                else:
                    _r = urllib.request.Request(url=url)
            except Exception as ex:
                raise LogbookException(self.ex_msg.format(req=url, res=ex, ex_code=''))

            _r.get_method = lambda: method
            msg = f"{_r.get_method()} \t logbook url : {url} \t {data}"
            if verbose:
                logging.info(msg)
            gcontext = ssl._create_unverified_context()
            try:
                if data:
                    data = json.dumps(data)  # type: str
                    data = data.encode("utf-8")  # type: bytes
                    response = urllib.request.urlopen(_r, data=data, context=gcontext, timeout=expired_timeout)
                else:
                    response = urllib.request.urlopen(_r, context=gcontext, timeout=expired_timeout)
            except Exception as ex:
                raise LogbookException(self.ex_msg.format(req=url, res=ex, ex_code=''))

            res_data = response.read()
            content = json.loads(res_data)
        except Exception as ex:
            if verbose:
                logging.exception(ex)
            else:
                logging.error(ex)
            time.sleep(sleep_on_error)
            raise LogbookException(self.ex_msg.format(req=url, res='', ex_code=ex))
        return content

    @staticmethod
    def convert_data(data):
        # type: (str or dict) -> dict
        new_data = dict()
        try:
            new_data = ast.literal_eval(data)
        except:
            try:
                new_data = json.loads(data)
            except:
                return data
        return new_data

    def get_execution_from_lbk(self, execution_key: str) -> dict:
        exe_data = None
        try:
            lbk_url = f"cycle/te/{execution_key}"
            exe_data = self.urlopen_lbk(lbk_url, 'GET')
            exe_data = self.convert_data(exe_data)
        except Exception as ex:
            logging.error(f'[LBK] Error at get from logbook, {ex}')
        return exe_data

    @staticmethod
    def get_fields_for_exec(execution_key, set_url):
        fields = dict()
        fields['test_execution_key'] = str(execution_key)
        fields['test_set_url'] = str(set_url)
        return fields

    def update_test(self, test_id: int, test_key: str, exec_key: str, verbose: bool = True) -> dict:
        lbk_url = f"test/update/{test_id}"
        headers = {'Content-Type': 'application/json'}
        res_data = None
        req_data = self.get_fields_for_tests(test_key, exec_key)
        try:
            res_data = self.urlopen_lbk(lbk_url, 'POST', data=req_data, headers=headers, verbose=verbose)
        except Exception as ex:
            logging.error(f'[LBK] Error at update test, {ex}')
        return res_data

    def get_suite_info(self, uuid_list: List[str], verbose: bool = False, timeout: int = 5) -> dict:
        data = None
        lbk_url = 'api/suite_eta_runtime'
        headers = {'Content-Type': 'application/json'}
        req_data = {'uuid_list': uuid_list}
        try:
            data = self.urlopen_lbk(lbk_url, 'POST', data=req_data, headers=headers, verbose=verbose, sleep_on_error=0,
                                    timeout=timeout)
        except Exception as ex:
            logging.error(f'[LBK] get_suite_info, {ex}')
        verbose and logging.debug(f'{data}')
        return data

    def close_cycle(self, cycle_id: int, verbose: bool = False, timeout: int = 5) -> dict:
        data = None
        lbk_url = f'cycle/{cycle_id}'
        headers = {'Content-Type': 'application/json'}
        try:
            data = self.urlopen_lbk(lbk_url, 'GET', headers=headers, verbose=verbose, sleep_on_error=0, timeout=timeout)
        except Exception as ex:
            logging.error(f'[LBK] close_cycle, {ex}')
        verbose and logging.debug(f'{data}')
        return data

    def report_applied_filter_async(self, filter_id: int, test_name: str, control_path: str, suite_exec_id: int,
                                    token: str, verbose: bool = True, timeout: int = 10):
        try:
            new_thread = threading.Thread(
                target=self.report_applied_filter,
                args=(filter_id, test_name, control_path, suite_exec_id, token, verbose, timeout,), daemon=True
            )
            new_thread.start()
            LogbookAPI.THREADS.append(new_thread)
            time.sleep(0.2)
        except Exception as ex:
            logging.error(ex)

    def report_applied_filter(self, filter_id: int, test_name: str, control_path: str, suite_exec_id: int, token: str,
                              verbose: bool = False, timeout: int = 3) -> dict:
        data = None
        try:
            lbk_url = 'filters_apply/new_cli'
            headers = {'Content-Type': 'application/json'}
            req_data = {
                'filter_id': filter_id,
                'name': test_name,
                'path': control_path,
                'suite_execution_id': suite_exec_id,
                'token': token,
                'branch': os.environ.get('MANIFEST_REVISION', ''),
                'job_name': os.environ.get('JOB_NAME', ''),
                'build_url': os.environ.get('BUILD_URL', ''),
            }
            data = self.urlopen_lbk(lbk_url, 'POST', data=req_data, headers=headers, verbose=verbose, sleep_on_error=0,
                                    timeout=timeout)
        except Exception as ex:
            logging.error(f'[LBK] report_applied_filter, {ex}')
            logging.exception(ex)
        verbose and logging.debug(f'{data}')
        return data

    @staticmethod
    def get_fields_for_tests(test_key, exec_key):
        fields = dict()
        fields['test_key'] = str(test_key)
        fields['test_execution_key'] = str(exec_key)
        return fields

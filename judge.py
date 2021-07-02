import shutil
import subprocess
from typing import Union, List

import requests

import constants
from extensions import file_extensions
from language import Language
from task_info import TaskInfo, TestCase
from test_case_result import TestCaseResult
from threads_manager import threads_manager
from verdict import Verdict


# @cachetools.cached(cache=cachetools.TTLCache(maxsize=10, ttl=60))
def get_task_info(task_id: str) -> TaskInfo:
    if constants.DEBUG:
        json = {"grader": False, "memory_limit": 256,
                "test_cases": [{"input": "1 1\n", "output": "Quadrant I\n", "subtask": 1, "test_case": 1},
                               {"input": "-50 -33\n", "output": "Quadrant III\n", "subtask": 1, "test_case": 2},
                               {"input": "94 -87\n", "output": "Quadrant IV\n", "subtask": 1, "test_case": 3},
                               {"input": "-100 100\n", "output": "Quadrant II\n", "subtask": 1, "test_case": 4},
                               {"input": "0 -24\n", "output": "None\n", "subtask": 1, "test_case": 5},
                               {"input": "66 0\n", "output": "None\n", "subtask": 1, "test_case": 6},
                               {"input": "0 0\n", "output": "None\n", "subtask": 1, "test_case": 7}], "time_limit": 1.0}
    else:
        print(f'{constants.FRONTEND_URL}/task/{task_id}/info')
        response = requests.get(f'{constants.FRONTEND_URL}/task/{task_id}/info',
                                headers={'X-Auth-Token': constants.CONFIG.get('secret_key')})
        response.raise_for_status()
        json = response.json()

    return TaskInfo(float(json['time_limit']),
                    int(json['memory_limit']),
                    json['grader'],
                    json.get('grader_source_code'),
                    json.get('grader_language'),
                    [TestCase(tc['subtask'],
                              tc['test_case'],
                              tc['input'],
                              tc.get('output')) for tc in json['test_cases']])


def judge(code: str, submission_id: str, task_id: str, language: Language, thread_id: int) -> None:
    # print(thread_id)
    threads_manager.add_thread(thread_id)
    verdict = _judge_impl(code, task_id, language, thread_id)

    # cleanup sandbox
    cleanup_proc = subprocess.run(['isolate', '-b', str(thread_id), '--cleanup'],
                                  text=True,
                                  stdout=subprocess.PIPE)
    if cleanup_proc.returncode != 0:
        verdict = Verdict.SE
    threads_manager.remove_thread(thread_id)
    if constants.DEBUG:
        print(verdict)
    else:
        report_url = f'{constants.FRONTEND_URL}/submission/{submission_id}/report'
        if type(verdict) is Verdict:
            response = requests.post(report_url,
                                     json={'verdict': verdict},
                                     headers={'X-Auth-Token': constants.CONFIG.get('secret_key')})
        else:
            response = requests.post(report_url,
                                     json={'test_case_results': [{'subtask': v.subtask,
                                                                  'test_case': v.test_case,
                                                                  'verdict': v.verdict,
                                                                  'score': v.score,
                                                                  'time_used': v.time_used,
                                                                  'memory_used': v.memory_used}
                                                                 for v in verdict]},
                                     headers={'X-Auth-Token': constants.CONFIG.get('secret_key')})
        response.raise_for_status()


def _judge_impl(code: str, task_id: str, language: Language, thread_id: int) -> Union[Verdict, List[TestCaseResult]]:
    code_filename = f'code{thread_id}.{file_extensions[language]}'
    code_path = f'run/{code_filename}'
    executable_filename = f'code{thread_id}'
    executable_path = f'run/{executable_filename}'
    metadata_path = f'run/metadata{thread_id}.txt'

    with open(code_path, 'w') as f:
        f.write(code)  # write to run\codeX.xxx

    # initialises isolate sandbox
    init_proc = subprocess.run(['isolate', '-b', str(thread_id), '--init'],
                               text=True,
                               stdout=subprocess.PIPE)
    if init_proc.returncode != 0:
        return Verdict.SE
    sandbox_path = f'{init_proc.stdout.strip()}/box'

    compile_args = []
    running_args = []

    # assume no grader for now
    if language == Language.cpp or language == Language.c:
        compile_args = ['g++' if language == Language.cpp else 'gcc',
                        '-O2', '-o', executable_path, code_path]

    elif language == Language.ocaml:
        compile_args = ['ocamlopt', '-S', '-o', executable_path, code_path]

    elif language == Language.pas:
        compile_args = ['fpc', '-O2', '-Sg', '-v0', '-XS', code_path, f'-o{executable_path}']

    elif language == Language.kt:
        executable_filename += '.jar'
        executable_path += '.jar'
        compile_args = ['kotlinc', code_path, '-include-runtime', '-d', executable_path]
        running_args = ['java', '-jar', executable_filename]

    elif language == Language.py:
        running_args = [f'/usr/bin/python3.9', code_filename]

    if compile_args:
        compile_proc = subprocess.run(compile_args, text=True, stderr=subprocess.PIPE)
        print(compile_proc.stderr)
        if compile_proc.returncode != 0:
            return Verdict.CE

        shutil.copy(executable_path, sandbox_path)  # copies executable to sandbox
        if not running_args:
            running_args = [executable_filename]
    else:
        shutil.copy(code_path, sandbox_path)  # copies code to sandbox

    task_info = get_task_info(task_id)
    test_case_results = []
    for test_case in task_info.test_cases:
        run_proc = subprocess.run(['isolate',
                                   '-M', metadata_path,  # metadata
                                   '-b', str(thread_id),  # sandbox id
                                   '-t', str(task_info.time_limit),
                                   '-w', str(task_info.time_limit + 1),  # wall time to prevent sleeping programs
                                   '-m', str(task_info.memory_limit * 1024),  # in kilobytes
                                   '--stderr-to-stdout',
                                   '--silent',  # tells isolate to be silent
                                   '--run'] + running_args,
                                  input=test_case.input,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  text=True)

        metadata = {}
        with open(metadata_path) as f:
            for line in f.readlines():
                line = line.strip()
                if line:
                    a, b = line.split(':')
                    metadata[a] = b

        verdict = Verdict.AC
        if run_proc.stdout.strip() != test_case.output.strip():
            verdict = Verdict.WA
        if 'status' in metadata:
            status = metadata['status']
            if status == 'RE' or status == 'SG' or status == 'XX':
                verdict = Verdict.RE
            elif status == 'TO':
                verdict = Verdict.TLE
            else:
                verdict = Verdict.SE

        test_case_results.append(
            TestCaseResult(subtask=test_case.subtask,
                           test_case=test_case.test_case,
                           verdict=verdict,
                           score=100. if verdict == Verdict.AC else 0.,
                           time_used=float(metadata['time']),
                           memory_used=int(metadata['max-rss']) / 1024))

    return test_case_results

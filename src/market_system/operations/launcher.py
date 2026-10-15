"""Local process supervisor: cooperative stop, bounded wait, whole-tree cleanup."""
import ctypes
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


class WindowsJob:
    def __init__(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                        ('LimitFlags',w.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
                        ('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n,ctypes.c_uint64) for n in ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
                                                     'ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),
                        ('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p,w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None,None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle,int(process._handle)):
            process.kill()
            process.wait()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if getattr(self,'handle',None):
            self.api.CloseHandle(self.handle)
            self.handle = None


def supervise(commands, home, seconds=30, *, stop_request=None):
    from market_system.paths import local_path
    folder = local_path(home/'.artifacts'/('launcher-'+uuid.uuid4().hex))
    folder.mkdir(parents=True)
    gate,stop = folder/'ready',folder/'stop'
    environment = dict(os.environ, MARKET_HOME=str(home), MARKET_LAUNCH_GATE=str(gate), MARKET_STOP_FILE=str(stop),PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8')
    processes = []
    windows = WindowsJob() if os.name=='nt' else None
    previous = {}
    stopping = False
    def request(*_):
        nonlocal stopping
        stopping = True
    for signum in (signal.SIGINT,signal.SIGTERM):
        previous[signum] = signal.signal(signum,request)
    result = 0
    try:
        for component,command in commands:
            process = subprocess.Popen(command,cwd=home,env=environment,stdin=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                                       start_new_session=os.name!='nt')
            processes.append((component,process))
            if windows:
                windows.assign(process)
            logging.info('child_started component=%s pid=%s',component,process.pid)
        # Children cannot create descendants before being attached to the Windows job.
        gate.touch()
        while not stopping and not (stop_request and stop_request()):
            ended = [(name,p) for name,p in processes if p.poll() is not None]
            if ended:
                result = ended[0][1].returncode or 1
                logging.error('child_exited component=%s code=%s',ended[0][0],ended[0][1].returncode)
                print(f'Компонент {ended[0][0]} завершился. Система останавливается; подробности в logs/.', flush=True)
                break
            time.sleep(0.2)
    finally:
        stop.touch()
        deadline = time.monotonic()+seconds
        while any(p.poll() is None for _,p in processes) and time.monotonic()<deadline:
            time.sleep(0.1)
        if windows:
            windows.close()  # also kills any descendant left after its parent exited
        else:
            for _,process in processes:
                try:
                    os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:
                    pass
            time.sleep(0.1)
            for _,process in processes:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for name,process in processes:
            process.wait(timeout=10)
            logging.info('child_stopped component=%s pid=%s code=%s',name,process.pid,process.returncode)
        for signum,handler in previous.items():
            signal.signal(signum,handler)
    return result


def launch(config):
    from market_system.jobs.worker import worker_lock
    if config.values['bot_enabled'] or config.values['delivery_enabled']:
        config.telegram()  # fail before starting any child if secrets are absent
    commands = [(name,[sys.executable,'-m','market_system.operations','--config',str(config.config_file),name])
                for name in ['web','worker']+(['bot'] if config.values['bot_enabled'] else [])]
    with worker_lock(config.home/'state/launcher'):
        url = f"http://{config.values['host']}:{config.values['port']}"
        link = f"\033]8;;{url}\033\\{url}\033]8;;\033\\" if sys.stdout.isatty() else url
        bot_suffix = ' и телеграмм-бота' if config.values['bot_enabled'] else ''
        print(f"Запуск веб-системы {link}{bot_suffix}", flush=True)
        external_stop = os.environ.get('MARKET_STOP_FILE')
        return supervise(commands,config.home,config.values['shutdown_seconds'],
                         stop_request=(lambda:Path(external_stop).exists()) if external_stop else None)

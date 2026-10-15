"""Bounded JSON logs, separate file per process; no URLs, texts or exception payloads."""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re


class JSONFormatter(logging.Formatter):
    def __init__(self, component):
        super().__init__()
        self.component = component

    def format(self, record):
        message = record.getMessage()
        for key,value in os.environ.items():
            if value and any(word in key.upper() for word in ('TOKEN','SECRET','PASSWORD','PROXY','API_HASH')):
                message = message.replace(value,'[redacted]')
        message = re.sub(r'https?://\S+', '[url]', message)
        message = re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{20,}\b', '[token]', message)
        result = {'time':datetime.now(timezone.utc).isoformat(),'level':record.levelname,
                  'component':self.component,'pid':os.getpid(),'logger':record.name,'message':message[:2000]}
        if record.exc_info:
            result['exception_type'] = record.exc_info[0].__name__
        return json.dumps(result,ensure_ascii=False)


def configure(config, component):
    handler = RotatingFileHandler(config.home/'logs'/f'{component}.jsonl',
                                 maxBytes=config.values['log_bytes'], backupCount=config.values['log_backups'],encoding='utf-8')
    handler.setFormatter(JSONFormatter(component))
    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ('uvicorn','uvicorn.error','uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Access query strings may contain private filters; use lifecycle/error logs instead.
    logging.getLogger('uvicorn.access').disabled = True

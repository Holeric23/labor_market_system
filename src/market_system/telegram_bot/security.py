import logging


def install_redaction(settings):
    secrets=[value for value in (settings.token,settings.event_secret,settings.telegram_proxy_url) if value]
    class Redact(logging.Filter):
        def filter(self,record):
            message=record.getMessage()
            for value in secrets:
                message=message.replace(value,'[redacted]')
            record.msg,record.args=message,()
            if record.exc_info:
                record.msg+=' ['+record.exc_info[0].__name__+']'
                record.exc_info=None
                record.exc_text=None
            return True
    for handler in logging.getLogger().handlers:
        handler.addFilter(Redact())

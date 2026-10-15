"""One configuration for CLI, launcher and future services."""
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Operations:
    home: Path
    config_file: Path
    values: dict
    paths: dict

    @classmethod
    def load(cls, filename=None):
        from market_system.paths import PROJECT_ROOT, local_path
        filename = local_path(filename or os.environ.get('MARKET_OPERATIONS_CONFIG', 'config/operations.json'))
        values = json.loads(filename.read_text(encoding='utf-8'))
        required = {'mode','runtime','web_state','bot_state','bot_env','host','port','allowed_hosts',
                    'bot_enabled','delivery_enabled','log_bytes','log_backups','shutdown_seconds','source_freshness_hours'}
        if set(values) != required:
            raise ValueError('Unexpected operations configuration keys')
        if values['mode'] not in ('local','linux') or values['host'] != '127.0.0.1':
            raise ValueError('Local listeners must bind 127.0.0.1; use an HTTPS reverse proxy')
        for key, low, high in [('port',1,65535),('log_bytes',1024,100*1024*1024),('log_backups',1,20),
                               ('shutdown_seconds',1,300),('source_freshness_hours',1,87600)]:
            if type(values[key]) is not int or not low <= values[key] <= high:
                raise ValueError('Invalid '+key)
        for key in ('bot_enabled','delivery_enabled'):
            if type(values[key]) is not bool:
                raise ValueError('Invalid '+key)
        hosts = values['allowed_hosts']
        if not isinstance(hosts,list) or not hosts or any(not isinstance(h,str) or not h or '*' in h or ',' in h for h in hosts):
            raise ValueError('Explicit allowed_hosts required; wildcards forbidden')
        if values['mode']=='linux' and not any(h not in ('127.0.0.1','localhost','[::1]') for h in hosts):
            raise ValueError('Linux mode requires the real public domain in allowed_hosts')
        runtime = local_path(values['runtime'])
        os.environ['MARKET_RUNTIME_CONFIG'] = str(runtime)
        from market_system.runtime import load_config
        paths = load_config(runtime)
        for key in ('web_state','bot_state','bot_env'):
            paths[key] = local_path(values[key])
        dbs = [paths[k] for k in ('database','web_state','bot_state','annotation_database')]
        if len(set(dbs)) != len(dbs):
            raise ValueError('Catalog, web, bot and annotation databases must differ')
        for key in ('model','skill_lexicon','occupation_lexicon','dataset'):
            if not paths[key].exists():
                raise ValueError('Required local artifact missing: '+key)
        # A restore records aliases separately, keeping immutable DB specifications intact.
        relocation = PROJECT_ROOT / 'config/relocation.json'
        if relocation.exists():
            origins = json.loads(relocation.read_text(encoding='utf-8'))['origins']
            if not isinstance(origins,list) or any(not isinstance(p,str) or 'python_research_project' in p.casefold() for p in origins):
                raise ValueError('Invalid relocation origins')
            os.environ['MARKET_PATH_ALIASES'] = json.dumps({p:str(PROJECT_ROOT) for p in origins})
        os.environ['MARKET_OPERATIONS_CONFIG'] = str(filename)
        os.environ['MARKET_WEB_CATALOG'] = str(paths['database'])
        os.environ['MARKET_WEB_STATE'] = str(paths['web_state'])
        os.environ['MARKET_WEB_HOSTS'] = ','.join(hosts)
        os.environ['MARKET_WEB_SECURE_COOKIE'] = '1' if values['mode']=='linux' else '0'
        return cls(PROJECT_ROOT, filename, values, paths)

    def prepare(self):
        if os.name != 'nt':
            os.umask(0o077)
        from market_system.runtime import enable_guard
        enable_guard()
        for folder in ('data','state','state/secrets','logs','backups','.artifacts/tmp'):
            (self.home/folder).mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            os.chmod(self.home/'state',0o700)
            os.chmod(self.home/'state/secrets',0o700)
            os.chmod(self.home/'backups',0o700)

    def telegram(self):
        from market_system.telegram_bot.config import Settings
        settings = Settings.load(self.paths['bot_env'])
        if settings.repository_backend != 'sqlite' or settings.events_enabled:
            raise ValueError('Regular operation requires SQLite without the retired event receiver')
        if settings.research_db_path != self.paths['database'] or settings.bot_db_path != self.paths['bot_state']:
            raise ValueError('Telegram paths must match the common configuration')
        if os.name != 'nt' and self.paths['bot_env'].stat().st_mode & 0o077:
            raise ValueError('Telegram secret file must be private (chmod 600)')
        return settings

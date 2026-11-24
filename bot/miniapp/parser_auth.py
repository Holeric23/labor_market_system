"""Talk to the parser login endpoint over the private Docker network."""
import json
import os
import urllib.error
import urllib.request

from database.parser_auth import shared_proof


def request(action, actor, value=''):
    try:
        proof = shared_proof(os.environ.get('DATABASE_URL',''))
        body = json.dumps({'action':action,'actor':actor,'value':value},ensure_ascii=False).encode('utf-8')
        call = urllib.request.Request('http://parser:8091/internal/parser-login',data=body,
            headers={'Content-Type':'application/json','X-Parser-Auth':proof},method='POST')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(call,timeout=50) as response:
            result = json.loads(response.read(8192).decode('utf-8'))
        if not isinstance(result,dict) or type(result.get('ok')) is not bool:
            raise ValueError()
        return result
    except (ValueError,UnicodeError,OSError,urllib.error.URLError):
        return {'ok':False,'error':'Не удаётся связаться с парсером. Проверьте контейнер парсера.'}

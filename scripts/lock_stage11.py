"""Reproduce hashed platform locks from saved pip resolution reports, never resolve implicitly."""
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]


def canonical(name):
    return re.sub(r'[-_.]+','-',name).lower()


def main():
    vendor = ROOT/'vendor'
    vendor.mkdir(exist_ok=True)
    wheel = ROOT/'.artifacts/linux-wheels/pyaes-1.6.1-py3-none-any.whl'
    shutil.copyfile(wheel,vendor/wheel.name)
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()
    evidence = {'python':'3.12','windows_install_verified':False,'linux_execution_verified':False,'platforms':{}}
    for platform in ('windows','linux'):
        report = json.loads((ROOT/f'.artifacts/stage11-lock-{platform}.json').read_text(encoding='utf-8'))
        lines = ['# CPython 3.12 / '+platform+' x86_64; generated from pip report.',
                 '# Linux: wheel availability checked, execution NOT verified.' if platform=='linux' else '# Windows runtime lock.',
                 '--only-binary=:all:','--find-links vendor','--require-hashes']
        packages = []
        for item in sorted(report['install'],key=lambda r:canonical(r['metadata']['name'])):
            name = canonical(item['metadata']['name'])
            version = item['metadata']['version']
            checksum = item['download_info']['archive_info']['hashes']['sha256']
            if name=='pyaes':
                checksum = wheel_hash
            lines.append(f'{name}=={version} --hash=sha256:{checksum}')
            packages.append({'name':name,'version':version,'sha256':checksum})
        filename = ROOT/f'requirements.{platform}.lock.txt'
        filename.write_text('\n'.join(lines)+'\n',encoding='utf-8')
        evidence['platforms'][platform] = {'packages':len(packages),'lock_sha256':hashlib.sha256(filename.read_bytes()).hexdigest()}
    (ROOT/'docs/STAGE11_LOCKS.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print(json.dumps(evidence,indent=2))


if __name__=='__main__':
    main()

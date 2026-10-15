"""Local ADMIN CLI. Every experiment requires an explicit finite budget."""
import argparse
import json
from market_system.paths import DATABASE_PATH
from market_system.runtime import enable_guard, load_config
from market_system.review.store import Actor
from .store import Models


def main(argv=None):
    enable_guard()
    parser = argparse.ArgumentParser(description='Model registry and bounded research')
    parser.add_argument('--db',default=str(DATABASE_PATH))
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    register = sub.add_parser('register')
    register.add_argument('--config',help='Local runtime-format JSON; defaults to current runtime config')
    register.add_argument('--adapter',default='ensemble-v1')
    sub.add_parser('list')
    sub.add_parser('show').add_argument('version')
    for name in ('approve','retire','designate'):
        sub.add_parser(name).add_argument('version')
    sub.add_parser('activate').add_argument('result_set_id')
    sub.add_parser('results')
    history = sub.add_parser('import-history')
    history.add_argument('directory',help='Read-only historical research/results directory')
    experiments = sub.add_parser('experiments')
    experiments.add_argument('id',nargs='?')
    experiment = sub.add_parser('experiment',help='Fixed model evaluation, no training or automatic selection')
    experiment.add_argument('version')
    experiment.add_argument('--ids',required=True)
    experiment.add_argument('--max-documents',required=True,type=int)
    experiment.add_argument('--seconds',required=True,type=int)
    experiment.add_argument('--cohort',required=True,choices=['development','challenge','test'])
    experiment.add_argument('--purpose',choices=['diagnostic','selection'],default='diagnostic')
    args = parser.parse_args(argv)
    models = Models(args.db)
    actor = Actor('local-admin','ADMIN')
    try:
        models.initialize()
        if args.command=='init':
            result = 'initialized; no models loaded or training started'
        elif args.command=='register':
            result = models.register(actor,load_config(args.config) if args.config else None,args.adapter)
        elif args.command=='list':
            result = models.list(actor)
        elif args.command=='show':
            result = models.get(args.version,actor)
        elif args.command in ('approve','retire'):
            result = models.status(args.version,'approved' if args.command=='approve' else 'retired',actor)
        elif args.command=='designate':
            result = models.designate(args.version,actor)
        elif args.command=='activate':
            result = models.publish(args.result_set_id,actor)
        elif args.command=='results':
            result = models.results(actor)
        elif args.command=='experiments':
            result = models.experiments(actor,args.id)
        elif args.command=='import-history':
            from .experiments import import_history
            result = import_history(models,args.directory,actor)
        else:
            from .experiments import run
            result = run(models,args.version,[int(v) for v in args.ids.split(',')],args.max_documents,
                         args.seconds,actor,args.cohort,args.purpose)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,RuntimeError,OSError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__':
    main()

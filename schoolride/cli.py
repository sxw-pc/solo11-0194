import argparse,json
from pathlib import Path
from .store import connect,import_file,report,audit
from . import __file__ as module_file
IMPORTS={'roster-import':'roster','route-import':'route','handoff-import':'handoff'}
DEMO=[('roster-import','roster-ok.json'),('route-import','route-ok.json'),('handoff-import','handoff-ok.json'),('roster-import','roster-bad.json'),('route-import','route-bad.json'),('handoff-import','handoff-bad.json'),('roster-import','roster-change.json'),('route-import','route-change.json'),('handoff-import','handoff-change.json')]
def main(argv=None):
 p=argparse.ArgumentParser();p.add_argument('command',choices=[*IMPORTS,'demo','report','audit']);p.add_argument('--db',default='state.sqlite');p.add_argument('--file')
 a=p.parse_args(argv);db=connect(a.db)
 if a.command in IMPORTS:
  if not a.file:p.error('--file required')
  result=import_file(db,IMPORTS[a.command],a.file)
 elif a.command=='demo':
  result={'steps':[],'imported_module':str(Path(module_file).resolve())}
  for cmd,name in DEMO:
   info=import_file(db,IMPORTS[cmd],Path(__file__).resolve().parents[1]/'fixtures'/name)
   result['steps'].append({'file':name,**info,'decisions':len(report(db)['decisions'])})
  result['report']=report(db);result['audit']=audit(db)
 elif a.command=='report':result=report(db)
 else:result=audit(db)
 print(json.dumps(result,ensure_ascii=False,indent=2))
 return 0 if a.command!='audit' or result['ok'] else 2

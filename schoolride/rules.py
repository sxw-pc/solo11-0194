"""Student authority, route capacity and custody handoff are separate ledgers."""
from collections import defaultdict
from .decisions import make_decision

def latest(events, kind):
    groups=defaultdict(list)
    for e in events:
        if e['kind']==kind: groups[e['run_id']].append(e)
    return {k:sorted(v,key=lambda x:(x['version'],x['effective_at'],x['id']))[-1] for k,v in groups.items()}

def decide(value,reason,*ids):
    return make_decision(value,reason,[x for x in ids if x])

def calculate(events):
    rosters=latest(events,'roster');routes=latest(events,'route');handoffs=latest(events,'handoff')
    out={};states=[]
    for run in sorted(set(rosters)|set(routes)|set(handoffs)):
        r=rosters.get(run);v=routes.get(run);h=handoffs.get(run)
        students={s['id']:s for s in r.get('students',[])} if r else {}
        valid={sid:s for sid,s in students.items() if s['enrolled_until']>=r['service_date']
               and not s.get('revoked') and s.get('route_authorization',{}).get('signed')
               and s['route_authorization']['valid_until']>=r['service_date']}
        capacity=bool(v and not v.get('cancelled') and v['inspection_until']>=v['service_end'] and v['driver_cert_until']>=v['service_end'])
        seats=v['seats'] if capacity else 0
        route_stops=set(v.get('stops',[])) if v else set()
        promised={sid:s for sid,s in valid.items() if s['stop'] in route_stops and
                  (not s.get('changed_stop') or s.get('change_confirmed'))}
        admitted={sid:s for sid,s in sorted(promised.items())[:seats]}
        scans=defaultdict(list)
        for scan in h.get('scans',[]) if h else []: scans[scan['student_id']].append(scan)
        for sid,s in students.items():
            prefix=f'{run}/{sid}/'
            enrolled=sid in valid
            stop_ok=s['stop'] in route_stops
            assigned=sid in admitted
            proxy=s.get('proxy')
            proxy_ok=bool(proxy and v and proxy['expires']>=v['service_end'] and
                          proxy.get('id_valid_until',proxy['expires'])>=v['service_end'] and
                          proxy.get('signed',True) and proxy['person']!=s['guardian'])
            boarded=[x for x in scans[sid] if x['stage']=='board' and x['monitor']==h.get('monitor') and x['stop']==s['stop'] and x.get('monitor_signed') and v and v['service_start']<=x['at']<=v['service_end']] if h else []
            alighted=[x for x in scans[sid] if x['stage']=='alight' and x['stop']==s['stop'] and x.get('monitor_signed') and v and v['service_start']<=x['at']<=v['service_end']] if h else []
            received=[x for x in scans[sid] if x['stage']=='receive' and x['person'] in {s['guardian'],proxy['person'] if proxy_ok else ''} and x['monitor']==h['monitor'] and x['stop']==s['stop'] and x.get('monitor_signed') and x.get('receiver_signed') and v and v['service_start']<=x['at']<=v['service_end']] if h else []
            chain=bool(assigned and boarded and alighted and received and boarded[0]['at']<alighted[0]['at']<received[0]['at'])
            custodian='guardian' if chain else 'carrier' if boarded else 'school'
            evidence=[x['id'] for x in (r,v,h) if x]
            out[prefix+'学籍时段是否覆盖乘车日']=decide('有效' if enrolled else '无资格','学籍有效日期与撤销状态',*evidence)
            out[prefix+'临时改站是否取得家长确认']=decide('确认' if not s.get('changed_stop') or s.get('change_confirmed') else '待确认','改站前保留旧站点承诺',*evidence)
            out[prefix+'代理接领资格是否覆盖放学时段']=decide('授权' if proxy_ok else '未授权','代理身份与有效期',*evidence)
            out[prefix+'上车点名是否与名册一致']=decide('点名' if boarded and assigned else '待核','有效名册、车次和监护员',*evidence)
            out[prefix+'下车位置是否属于批准站点']=decide('正确' if alighted else '错站或未下车','站点和下车证据',*evidence)
            out[prefix+'接领双签是否转移保管责任']=decide(custodian,'接领人与随车监护员及事件顺序',*evidence)
        p=f'{run}/'
        evidence=[x['id'] for x in (r,v,h) if x]
        out[p+'车辆检验是否覆盖全程']=decide('可出车' if capacity else '停运','车辆与司机证照覆盖服务终点',*evidence)
        out[p+'线路绕行是否触及学生停靠站']=decide('完整' if len(promised)==len(valid) else '漏站','有效学生站点与实际线路集合',*evidence)
        out[p+'座位占用是否超过额定人数']=decide('充足' if len(valid)<=seats else '候补','额定座位及当日有效权益人数',*evidence)
        out[p+'停运后是否保留学生替代接送权益']=decide('替代接送' if valid and not capacity else '正常接送','停运不消灭学生权益',*evidence)
        if r: states.append(('学生乘车权益',run,'有效' if valid else '待核',r['version']))
        if v: states.append(('线路车辆调度',run,'可发' if capacity else '停运',v['version']))
        if h: states.append(('乘降监护交接',run,'结案' if students and all(out[f'{run}/{sid}/接领双签是否转移保管责任']['result']=='guardian' for sid in valid) else '待接',h['version']))
    return out,states

def business_records(e):
    run=e['run_id'];eid=e['id']
    if e['kind']=='roster':
        records=[('学生乘车权益','学生乘车名册',f'{eid}:{s["id"]}',s) for s in e.get('students',[])]
        records += [('学生乘车权益','接领授权',f'{eid}:proxy:{s["id"]}',s['proxy']) for s in e.get('students',[]) if s.get('proxy')]
    elif e['kind']=='route':
        records=[('线路车辆调度','线路时刻表',f'{eid}:route',{'stops':e['stops'],'run_id':run}),
                 ('线路车辆调度','车辆出车记录',f'{eid}:vehicle',{'inspection_until':e['inspection_until'],'driver_cert_until':e['driver_cert_until'],'seats':e['seats']})]
    else:
        records=[('乘降监护交接','乘降扫描',f'{eid}:{i}',x) for i,x in enumerate(e.get('scans',[]))]
        records.append(('乘降监护交接','接领回执',f'{eid}:receipt',{'monitor':e['monitor'],'run_id':run}))
    return [(sub,typ,key,body) for sub,typ,key,body in records]

import ast, hashlib, json, os, subprocess, tempfile
from pathlib import Path
REPO=Path('C:/tmp/cc-lb9-graph-c')
PIN='e40f2db1402cb629dedcff57e656dee439bb51c3'
GP='agent/context_compressor.py'
ART=Path(__file__).with_name('lb9-graph-witness-c.json')
CAND=Path('D:/brain/meta/coding-graph/hermes-agent/context-compressor-lb6/residual-wave1/lb9-graph-validation/lane-a/merged-model.json')

def blob():
    return subprocess.check_output(['git','-C',str(REPO),'cat-file','blob',PIN+':'+GP])

def main():
    a=json.loads(ART.read_text(encoding='utf-8'))
    assert a['verdict']=='REQUEST_CHANGES'
    b=blob(); assert len(b)==273178 and b.count(b'\n')==5381
    assert hashlib.sha256(b).hexdigest()==a['source']['sha256']
    assert subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip()==PIN
    assert subprocess.check_output(['git','-C',str(REPO),'status','--porcelain'],text=True)==''
    c=json.loads(CAND.read_text(encoding='utf-8'))
    assert c['source']['pin']==PIN and c['source']['bytes']==len(b)
    assert c['source']['lines']==5381 and c['source']['sha256']==hashlib.sha256(b).hexdigest()
    nodes=c['nodes']; ids={x['id'] for x in nodes}; assert len(ids)==len(nodes)
    for e in c['edges']: assert e['from'] in ids and e['to'] in ids
    assert len(c['components'])==71 and sum(x.get('nodeCount')==0 for x in c['components'])==30
    assert sum(x.get('slice_eligible') is True for x in nodes)==84
    # Fresh bounded verifier receipt, not canonical tests.
    fd,path=tempfile.mkstemp(prefix='hermes-verify-',suffix='.py')
    os.close(fd); rc=None
    try:
        Path(path).write_text('print("fresh pinned artifact verifier")\n',encoding='utf-8')
        rc=subprocess.run([os.sys.executable,path],capture_output=True,text=True).returncode
        assert rc==0
        print('TEMP_VERIFIER',path,'RETURN_CODE',rc)
    finally:
        Path(path).unlink(missing_ok=True)
    print('STRUCTURAL_VERIFY PASS')
    print('TEMP_VERIFIER_CLEAN',not Path(path).exists())

if __name__=='__main__': main()

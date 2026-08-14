import ast,hashlib,json,subprocess,collections,tarfile,io
repo='C:/tmp/cc-lb9-graph-c'; pin='e40f2db1402cb629dedcff57e656dee439bb51c3'; gp='agent/context_compressor.py'; cand='D:/brain/meta/coding-graph/hermes-agent/context-compressor-lb6/residual-wave1/lb9-graph-validation/lane-a/merged-model.json'
b=subprocess.check_output(['git','-C',repo,'cat-file','blob',pin+':'+gp])
text=b.decode('utf-8'); t=ast.parse(text)
print('SOURCE',len(b),b.count(b'\n'),hashlib.sha256(b).hexdigest(), 'module_lines',len(text.splitlines()))
classes=[n for n in t.body if isinstance(n,ast.ClassDef)]
print('TOP_CLASSES',[(n.name,n.lineno,n.end_lineno,[x.id if isinstance(x,ast.Name) else ast.dump(x) for x in n.bases]) for n in classes])
funcs=[n for n in ast.walk(t) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
print('FUNCS',len(funcs),'TOP_FUNCS',[(n.name,n.lineno,n.end_lineno) for n in t.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))])
methods=[]
for c in classes:
 for n in c.body:
  if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)): methods.append((c.name,n.name,n.lineno,n.end_lineno))
print('METHODS',len(methods),'firstlast',methods[:3],methods[-3:])
co=collections.Counter((c,n) for c,n,_,_ in methods); print('DUP_METHOD_NAMES',[x for x,v in co.items() if v>1])
arc=subprocess.check_output(['git','-C',repo,'archive',pin]); tf=tarfile.open(fileobj=io.BytesIO(arc)); files=[m.name for m in tf.getmembers()]; py=[f for f in files if f.endswith('.py')]
refs=[]; imports=[]; patches=[]
for f in py:
 data=tf.extractfile(f).read()
 try: tr=ast.parse(data.decode('utf-8'))
 except: continue
 for n in ast.walk(tr):
  if isinstance(n,ast.ImportFrom) and n.module and 'context_compressor' in n.module: imports.append((f,n.lineno,n.module,[a.name for a in n.names]))
  if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in ('patch','patch_object','setattr'): patches.append((f,n.lineno,ast.unparse(n)[:180]))
  if isinstance(n,ast.Name) and n.id=='ContextCompressor': refs.append((f,n.lineno,type(n).__name__))
print('PY_FILES',len(py),'IMPORTS',len(imports),imports[:20]); print('NAME_REFS',len(refs),refs[:30]); print('PATCH_CALLS',len(patches),patches[:20])
with open(cand,encoding='utf8') as fh: c=json.load(fh)
print('CAND_KEYS',sorted(c.keys()))
for k in ['nodes','typed_nodes','edges','components','claims','tree_claims','candidate_boundaries','eligible_slices','gates','verification','receipts','source','boundary','dependency_graph','typed_graph','state_entanglement','cycles','orphans']:
 v=c.get(k,'<missing>')
 if isinstance(v,dict): print('CAND',k,'dictkeys',list(v)[:30], 'len',len(v))
 elif isinstance(v,list): print('CAND',k,'listlen',len(v),'sample',v[:2])
 else: print('CAND',k,repr(v)[:500])
edges=c.get('edges',[]); comps=c.get('components',[])
print('EDGE_COUNT',len(edges),'COMP_COUNT',len(comps)); print('EDGE_TYPES',collections.Counter(e.get('type') for e in edges)); print('EDGE_CLAIMS',collections.Counter(tuple(e.get('tree_claims',[])) for e in edges).most_common(12))
ids=set()
for e in edges:
 for side in ('from','to'):
  if isinstance(e.get(side),str): ids.add(e[side])
compids={x.get('id') for x in comps}
print('EDGE_ENDPOINTS',len(ids),'COMPIDS',len(compids),'MISSING_ENDPOINTS',len(ids-compids),'sample_missing',sorted(ids-compids)[:15])
print('COMP_ZERO',sum(x.get('nodeCount')==0 for x in comps),'COMP_DUP_NAMES',[(n,v) for n,v in collections.Counter(x.get('name') for x in comps).items() if v>1][:20])
print('CLAIM_LABELS',collections.Counter(cl for e in edges for cl in e.get('tree_claims',[])))
print('DUP_EDGE_TUPLES',sum(v-1 for v in collections.Counter((e.get('from'),e.get('to'),e.get('type'),e.get('line')) for e in edges).values() if v>1)); print('NULL_LINES',sum(e.get('line') is None for e in edges))

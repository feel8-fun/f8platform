'use strict';
const views = new Set(['extensions','environments','tools','processes','tasks']);
function locationView() { const value=new URL(location.href).searchParams.get('view');return views.has(value) ? value : 'extensions'; }
let view = locationView();
let navigationRevision = 0;
let loadController = null;
let inspectedJob = null;
let detailRevision = 0;
let pollingJob = false;
let pollingTaskLog = false;
let pollingTasks = false;
let managementJobs = [];
let tasksSignature = '';
let statesSignature = '[]';
let toolStatesSignature = '[]';
let processStatesSignature = '[]';
let cachedStorage = null;
let inspectedResource = null;
let updatingApplications = false;
let backgroundReload = null;
let reloadPending = false;
let reloadStorage = false;
const content = document.querySelector('#content');
const detail = document.querySelector('#detail');
const error = document.querySelector('#error');
const taskPanel = document.querySelector('#tasks');
function node(tag, text, cls) { const el = document.createElement(tag); if(text !== undefined) el.textContent = text; if(cls) el.className = cls; return el; }
function fail(reason) { error.hidden = false; error.textContent = reason.message || String(reason); }
async function api(path, method='GET', body, signal) {
  const response = await fetch('/api/'+path, {method, signal, credentials:'same-origin', headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
  if(!response.ok) { let message = await response.text(); try { const obj = JSON.parse(message); message = obj.message || obj.detail || message; } catch {} throw new Error(response.status === 401 ? 'Open this page from the Launcher tray or use f8platform open to sign in.' : message); }
  return response.status === 204 ? null : response.json();
}
function action(parent, label, run) { const btn = node('button',label); btn.onclick = async () => { btn.disabled = true; error.hidden = true; const revision=navigationRevision; try { await run(); } catch(reason) { if(revision===navigationRevision) fail(reason); } finally { btn.disabled = false; } }; parent.append(btn); return btn; }
function card(title, description) { const el = node('article',undefined,'card'); el.append(node('h2',title)); if(description) el.append(node('p',description,'meta')); return el; }
function badge(parent,text) { parent.append(node('span',text,'badge '+text)); }
function showDetail(title,value,jobId=null) { inspectedResource=null;inspectedJob=jobId;detailRevision++;detail.hidden=false; detail.replaceChildren(node('h2',title),node('pre',typeof value === 'string' ? value : JSON.stringify(value,null,2))); detail.scrollIntoView({behavior:'smooth',block:'nearest'}); }
async function inspect(title,path,jobId=null,format=value=>value) {
  const navigation=navigationRevision;
  const revision=++detailRevision;
  inspectedJob=null;inspectedResource=null;
  const value=await api(path);
  if(navigation===navigationRevision && revision===detailRevision) {
    showDetail(title,format(value),jobId);
    if(!jobId) inspectedResource={title,path,format};
  }
}
function taskTitle(job) { return `${job.request.action.replaceAll('-', ' ')} · ${job.request.extensionId || job.request.environmentId || 'packages'}`; }
function taskRows(parent,jobs) {
  for(const job of jobs) {
    const row=node('article',undefined,'row task-row');const summary=node('div');
    summary.append(node('strong',taskTitle(job)));badge(summary,job.state);
    if(job.detail)summary.append(node('p',job.detail,'meta'));
    row.append(summary);const buttons=node('div',undefined,'actions');row.append(buttons);
    action(buttons,'Task details / logs',()=>inspect(taskTitle(job),`management-jobs/${job.jobId}/logs`,null,logs=>logs.log || job.detail));
    if(['queued','running'].includes(job.state) && job.cancellable && !job.cancelRequested)
      action(buttons,'Cancel task',async()=>{await api(`management-jobs/${job.jobId}/cancel`,'POST');await pollTasks();});
    parent.append(row);
  }
}
function renderTaskPanel() {
  taskPanel.hidden=!managementJobs.length || view==='tasks';
  const active=managementJobs.filter(job=>['queued','running'].includes(job.state)).sort((a,b)=>a.createdAt-b.createdAt);
  const recent=managementJobs.filter(job=>!['queued','running'].includes(job.state)).slice(0,3);
  taskPanel.replaceChildren(node('h2',`Maintenance tasks · ${active.length} active`));
  taskRows(taskPanel,[...active,...recent]);
}
async function tasks(content) {content.append(node('h1','Tasks'),node('p','Installation and environment maintenance run one at a time. Other pages remain available.','meta'));taskRows(content,managementJobs);}
async function extensions(content,signal) {
  const [items,sources,apps,startup] = await Promise.all([api('extensions','GET',undefined,signal),api('source-applications','GET',undefined,signal),api('applications','GET',undefined,signal),api('startup','GET',undefined,signal)]);
  updatingApplications=items.some(item=>item.applicationOperation?.state==='running');
  content.append(node('h1','Extensions'));
  const grid=node('div',undefined,'grid'); content.append(grid);
  for(const item of items) {
    const el=card(item.name,item.description); grid.append(el);
    const local=item.sourceCheckout && !item.releaseSha256;
    badge(el,local && item.state === 'installed' ? 'Registered locally' : local && item.state === 'disabled' ? 'Disabled locally' : item.state);
    if(item.running) badge(el,'running');
    el.append(node('p',`v${item.version} · ${item.serviceClasses.length} services · ${item.toolIds.length} tools · ${item.skillIds.length} skills`,'meta'));
    if(item.sourceCheckout) {
      el.append(node('p','Development source checkout','meta'));
      if(!item.releaseSha256) el.append(node('p','Uses local source and build outputs; no release package imported.','meta'));
      if(item.sourcePath) { const location=node('details');location.append(node('summary','Source location'),node('code',item.sourcePath));el.append(location); }
    }
    if(item.releaseSha256) {
      el.append(node('p',`Release package · v${item.version}`,'meta'));
      const verification=node('details');verification.append(node('summary','Package verification'),node('p','Archive SHA-256','meta'),node('code',item.releaseSha256));el.append(verification);
    } else if(!item.sourceCheckout) el.append(node('p','Local package · no verified release archive','meta'));
    if(item.preinstalled) el.append(node('p',item.sourceCheckout ? 'Included in the development workspace preset.' : 'Included with this distribution.','meta'));
    if(item.detail) el.append(node('p',item.detail,'meta'));
    if(item.applicationOperation?.state === 'running') badge(el,'stopping');
    if(item.applicationOperation?.state === 'failed') el.append(node('p',item.applicationOperation.detail,'meta'));
    if(item.application) {
      const label=node('label','Start with Launcher');const input=node('input');input.type='checkbox';input.checked=startup.applications.includes(item.extensionId);label.prepend(input);el.append(label);
      input.onchange=async()=>{input.disabled=true;try{const enabled=new Set(startup.applications);if(input.checked)enabled.add(item.extensionId);else enabled.delete(item.extensionId);const saved=await api('startup','PUT',{applications:[...enabled]});startup.applications=saved.applications;}catch(reason){input.checked=!input.checked;fail(reason);}finally{input.disabled=false;}};
    }
    const buttons=node('div',undefined,'actions'); el.append(buttons);
    const source=sources.find(x=>x.extensionId === item.extensionId);
    if(source && (source.managed || source.state !== 'running')) action(buttons,source.state === 'running' ? 'Stop source' : 'Start source', async()=>{await api(`source-applications/${item.extensionId}/${source.state === 'running' ? 'stop' : 'start'}`,'POST'); await load();});
    const installed=apps.find(x=>x.manifest.extensionId === item.extensionId && x.selected);
    if(installed) {
      action(buttons,installed.state === 'running' ? 'Stop' : 'Start',async()=>{await api(`applications/${item.extensionId}/${installed.state === 'running' ? 'stop' : 'start'}`,'POST');await load();});
      if(installed.manifest.webAssets && item.running) action(buttons,'Open',()=>window.open(installed.endpoints.find(x=>x.name===installed.manifest.health.endpoint).url,'_blank','noopener'));
    }
    if(source?.state === 'running') {
      const def=source.endpoints.find(x=>x.name==='http') || source.endpoints[0];
      if(def) action(buttons,'Open',()=>window.open(def.url,'_blank','noopener'));
    }
    if(!item.application || item.releaseSha256) {
      if(item.state === 'available' || item.state === 'failed') action(buttons,'Install',async()=>{await api(`extensions/${item.extensionId}/install`,'POST');await load();});
      if(item.state === 'installing') action(buttons,'Cancel installation',async()=>{await api(`extensions/${item.extensionId}/cancel`,'POST');await load();});
      if(['installed','disabled'].includes(item.state)) {
        action(buttons,item.state === 'installed' ? 'Disable' : 'Enable',async()=>{await api(`extensions/${item.extensionId}/enabled`,'PUT',{enabled:item.state!=='installed'});await load();});
        action(buttons,'Uninstall',async()=>{await api(`extensions/${item.extensionId}`,'DELETE');await load();});
      }
    }
    action(buttons,'Details',()=>inspect(item.name,`extensions/${item.extensionId}/detail`));
    if(item.application) action(buttons,'Logs',()=>inspect(item.name+' logs',`application-logs/${item.extensionId}`));
  }
  if(!items.length) content.append(node('p','No extensions installed. Add a release package below.','empty'));
  const form=node('form'); form.append(node('h3','Add extension package'));
  const url=node('input');url.type='url';url.required=true;url.placeholder='HTTPS package URL';url.setAttribute('aria-label','Package URL');
  const hash=node('input');hash.required=true;hash.pattern='[a-fA-F0-9]{64}';hash.placeholder='SHA-256';hash.setAttribute('aria-label','Package SHA-256');form.append(url,hash);
  const submit=node('button','Add package');submit.type='submit';form.append(submit);
  form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try {await api('extensions/import','POST',{url:url.value.trim(),sha256:hash.value.trim().toLowerCase()});await load();}catch(reason){fail(reason);}finally{submit.disabled=false;}}; content.append(form);
}
const bytes = value => value == null ? 'Unavailable' : `${(value / 1024 / 1024).toFixed(1)} MiB`;
function storageSummary(storage) {
  const summary=card('Storage and package cache',`Storage: ${storage.path} · cache: ${storage.cachePath}`);
  summary.append(node('p',`Total allocated file blocks: ${bytes(storage.totalUsage.allocatedBytes)} · file data (hardlinks counted once): ${bytes(storage.totalUsage.uniqueFileBytes)}`));
  summary.append(node('p',`Environment files: ${bytes(storage.environmentUsage.uniqueFileBytes)} · cached file data: ${bytes(storage.cacheUsage.uniqueFileBytes)}`));
  summary.append(node('p','The package cache is retained so packages can be reused by future installations.','meta'));
  summary.append(node('p','Allocated blocks may be unavailable on Windows. Shared copy-on-write extents are not measured. Hardlinks used elsewhere retain their file data.','meta'));
  if(storage.unusedEnvironments.length){
    summary.append(node('h3',`Unused environment directories (${storage.unusedEnvironments.length})`));
    for(const unused of storage.unusedEnvironments)summary.append(node('p',`${unused.environmentId} · ${bytes(unused.usage.uniqueFileBytes)}`,'meta'));
    action(summary,'Release unused environment files',async()=>{await api('environments/unused/clean','POST');await load();});
  }
  if(storage.usageUpdatedAt)summary.append(node('p',`Storage measured ${new Date(storage.usageUpdatedAt*1000).toLocaleTimeString()}`,'meta'));
  action(summary,'Refresh storage usage',()=>load({refreshStorage:true}));
  return summary;
}
async function environments(content,signal,background=false,refreshStorage=false) {
  const items=await api('environments','GET',undefined,signal);
  content.append(node('h1','Runtime Environments'),node('p','Extensions declare environments; Pixi installs them.','meta'));
  const usage=node('section');content.append(usage);
  if(cachedStorage)usage.append(storageSummary(cachedStorage));
  if(!background || !cachedStorage || refreshStorage) {
    const pending=node('p',cachedStorage ? 'Updating storage usage…' : 'Measuring storage usage…','meta');pending.setAttribute('role','status');usage.append(pending);
    void api(`environments/storage${refreshStorage ? '?refresh=true' : ''}`,'GET',undefined,signal).then(storage=>{
      if(signal.aborted)return;
      cachedStorage=storage;usage.replaceChildren(storageSummary(storage));
    }).catch(reason=>{
      if(signal.aborted)return;
      pending.textContent=`Storage inspection failed: ${reason.message || reason}`;pending.setAttribute('role','alert');
    });
  }
  const grid=node('div',undefined,'grid');content.append(grid);
  for(const item of items) {
    const el=card(item.name || item.environmentId,item.detail);grid.append(el);badge(el,item.state);
    el.append(node('p',`Extension: ${item.extensionIds.join(', ') || 'none'}`,'meta'),node('p',`Services: ${item.serviceClasses.join(', ') || 'none'}`,'meta'));
    const buttons=node('div',undefined,'actions');el.append(buttons);
    action(buttons,'Packages and details',()=>inspect(item.name,`environments/${item.environmentId}/detail`));
    if(item.runtimeKind!=='bundled')action(buttons,item.state==='preparing' ? 'Cancel' : 'Verify / prepare',async()=>{await api(`environments/${item.environmentId}/${item.state==='preparing' ? 'cancel' : 'prepare'}`,'POST');await load();});
    if(item.canRemove)action(buttons,'Release files',async()=>{await api(`environments/${item.environmentId}`,'DELETE');await load();});
  }
}
async function tools(content,signal) {
  const [items,jobs]=await Promise.all([api('extension-tools','GET',undefined,signal),api('tool-jobs','GET',undefined,signal)]); content.append(node('h1','Tools'));
  for(const id of [...new Set(items.map(x=>x.extensionId))]) {
    content.append(node('h2',id));const grid=node('div',undefined,'grid');content.append(grid);
    for(const tool of items.filter(x=>x.extensionId===id)) {
      const el=card(tool.name,tool.description);grid.append(el);action(el,'Open tool',()=>toolForm(tool));
    }
  }
  content.append(node('h2','Tool jobs'));
  for(const job of jobs.slice(0,30)) {const row=node('div',undefined,'row');row.append(node('span',`${job.extensionId} / ${job.toolId} · ${job.status}`));action(row,'Result / logs',()=>inspect(job.toolId,`tool-jobs/${job.jobId}`,job.jobId));if(['queued','running'].includes(job.status))action(row,'Stop',async()=>{await api(`tool-jobs/${job.jobId}/cancel`,'POST');await load();});content.append(row);}
  if(!items.length)content.append(node('p','Install and enable an extension with tools to get started.','empty'));
}
function toolForm(tool) {
  inspectedJob=null;inspectedResource=null;detailRevision++;detail.hidden=false;detail.replaceChildren(node('h2',tool.name));const form=node('form');detail.append(form);const controls=new Map();
  for(const field of tool.fields) {
    const label=node('label',field.label || field.name);let input;
    if(field.choices?.length){input=node('select');for(const option of field.choices || []){const el=node('option',String(option));el.value=String(option);input.append(el);}}
    else {input=node('input');input.type=field.kind === 'boolean' ? 'checkbox' : (field.kind === 'number' || field.kind === 'integer' ? 'number':'text');}
    if(input.type==='checkbox')input.checked=Boolean(field.default);else if(field.default !== null && field.default !== undefined)input.value=String(field.default);
    input.required=Boolean(field.required);label.append(input);form.append(label);controls.set(field.name,{field,input});
  }
  let confirm;if(tool.requiresConfirmation){const label=node('label','Confirm execution');confirm=node('input');confirm.type='checkbox';confirm.required=true;label.prepend(confirm);form.append(label);}
  const submit=node('button','Run');submit.type='submit';form.append(submit);
  form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;const revision=navigationRevision;try {const argumentsObject={};for(const [name,{field,input}]of controls){argumentsObject[name]=field.kind==='boolean' ? input.checked : ['number','integer'].includes(field.kind)?Number(input.value):input.value;}const job=await api(`extension-tools/${tool.extensionId}/${tool.toolId}/run`,'POST',{arguments:argumentsObject,confirm:Boolean(confirm?.checked)});if(revision!==navigationRevision)return;showDetail(tool.name,job,job.jobId);await load();}catch(reason){if(revision===navigationRevision)fail(reason);}finally{submit.disabled=false;}};
}
async function processes(content,signal) {
  content.append(node('h1','Service processes'));const items=await api('service-processes','GET',undefined,signal);
  for(const item of items){const row=node('div',undefined,'row');row.append(node('span',`${item.serviceId} · ${item.serviceClass} · ${item.running?'running':'stopped'}`));if(item.running)action(row,'Stop',async()=>{await api(`service-processes/${item.serviceId}/stop`,'POST');await load();});content.append(row);}
  action(content,'View logs',()=>inspect('Service logs','service-processes/logs',null,logs=>logs.map(x=>`${x.serviceId}: ${x.line}`).join('\n')));
}
async function load({background=false,refreshStorage=false}={}) {
  loadController?.abort();
  const controller=new AbortController();
  loadController=controller;
  const requestedView=view;
  const next=document.createDocumentFragment();
  error.hidden=true;
  try {
    if(requestedView==='extensions')await extensions(next,controller.signal);
    else if(requestedView==='environments')await environments(next,controller.signal,background,refreshStorage);
    else if(requestedView==='tools')await tools(next,controller.signal);
    else if(requestedView==='tasks')await tasks(next);
    else await processes(next,controller.signal);
    if(loadController===controller && !controller.signal.aborted && view===requestedView) {
      if(background) {
        const oldForm=content.querySelector('form');const newForm=next.querySelector('form');
        if(oldForm && newForm)newForm.replaceWith(oldForm);
      }
      content.replaceChildren(next);
      if(!background)document.querySelector('#refresh-status').textContent=`Updated ${new Date().toLocaleTimeString()}`;
    }
  } catch(reason) {
    if(loadController===controller && !controller.signal.aborted) fail(reason);
  }
}
function navigate(next,push=true) {
  view=next;navigationRevision++;detailRevision++;inspectedJob=null;inspectedResource=null;detail.hidden=true;content.replaceChildren();
  if(push){const url=new URL(location.href);url.searchParams.set('view',view);history.pushState(null,'',url);}
  for(const b of document.querySelectorAll('[data-view]'))b.classList.toggle('active',b.dataset.view===view);
  renderTaskPanel();
  void load();
}
for(const button of document.querySelectorAll('[data-view]'))button.onclick=()=>navigate(button.dataset.view);
window.addEventListener('popstate',()=>navigate(locationView(),false));
async function refresh() {
  const revision=navigationRevision;
  document.querySelector('#refresh-status').textContent='Refreshing…';
  try {
    await pollTasks(false);
    if(revision!==navigationRevision)return;
    await load({refreshStorage:true});
    if(revision===navigationRevision && inspectedResource)await inspect(inspectedResource.title,inspectedResource.path,null,inspectedResource.format);
  }catch(reason){if(revision===navigationRevision)fail(reason);}
}
document.querySelector('#refresh').onclick=()=>void refresh();
navigate(view,false);

function requestBackgroundReload(completed=false) {
  reloadPending=true;reloadStorage=reloadStorage || completed;
  if(backgroundReload)return;
  backgroundReload=(async()=>{
    let requestedNavigation=navigationRevision;
    try {
      while(reloadPending) {
        reloadPending=false;const updateStorage=reloadStorage;reloadStorage=false;
        const navigation=navigationRevision;const revision=detailRevision;const resource=inspectedResource;
        requestedNavigation=navigation;
        await load({background:true,refreshStorage:updateStorage});
        if(updateStorage && resource && navigation===navigationRevision && revision===detailRevision)
          await inspect(resource.title,resource.path,null,resource.format);
      }
    }catch(reason){if(requestedNavigation===navigationRevision)fail(reason);}
    finally{backgroundReload=null;}
  })();
}

async function pollTasks(refreshView=true) {
  if(pollingTasks)return;
  pollingTasks=true;
  const navigation=navigationRevision;
  try {
    const jobs=await api('management-jobs');
    const signature=JSON.stringify(jobs);
    const states=JSON.stringify(jobs.map(job=>[job.jobId,job.state]));
    const changed=statesSignature!==states;
    const completed=jobs.some(job=>!['queued','running'].includes(job.state) &&
      !managementJobs.some(old=>old.jobId===job.jobId && !['queued','running'].includes(old.state)));
    managementJobs=jobs;statesSignature=states;
    if(signature!==tasksSignature){tasksSignature=signature;renderTaskPanel();if(view==='tasks' && refreshView)requestBackgroundReload(completed);}
    if(updatingApplications) {
      const statuses=await api('extensions');
      updatingApplications=statuses.some(item=>item.applicationOperation?.state==='running');
      if(!updatingApplications && refreshView)requestBackgroundReload(true);
    }
    if(refreshView && (changed || updatingApplications) && view!=='tasks') {
      requestBackgroundReload(completed);
    }
    if(view==='tools' && refreshView) {
      const jobs=await api('tool-jobs');const next=JSON.stringify(jobs.map(job=>[job.jobId,job.status]));
      if(next!==toolStatesSignature){toolStatesSignature=next;requestBackgroundReload();}
    }
    if(view==='processes' && refreshView) {
      const processes=await api('service-processes');const next=JSON.stringify(processes);
      if(next!==processStatesSignature){processStatesSignature=next;requestBackgroundReload();}
    }
  } catch(reason) { if(navigation===navigationRevision)fail(reason); }
  finally { pollingTasks=false; }
}
void pollTasks();
setInterval(()=>void pollTasks(),1000);

setInterval(async()=>{
  const resource=inspectedResource;
  if(!resource || detail.hidden || pollingTaskLog)return;
  const active=managementJobs.some(job=>resource.path===`management-jobs/${job.jobId}/logs` && ['queued','running'].includes(job.state));
  if(!active)return;
  const navigation=navigationRevision;const revision=detailRevision;pollingTaskLog=true;
  try {
    const value=await api(resource.path);
    if(navigation!==navigationRevision || revision!==detailRevision || inspectedResource!==resource || detail.hidden)return;
    const result=detail.querySelector('pre');
    if(result)result.textContent=resource.format(value);
  }catch(reason){if(navigation===navigationRevision && revision===detailRevision)fail(reason);}
  finally{pollingTaskLog=false;}
},1000);

setInterval(async () => {
  if (view!=='tools' || !inspectedJob || detail.hidden || pollingJob) return;
  const jobId=inspectedJob;
  const revision=detailRevision;
  pollingJob=true;
  try {
    const job = await api(`tool-jobs/${jobId}`);
    if(view!=='tools' || inspectedJob!==jobId || revision!==detailRevision || detail.hidden) return;
    const result = detail.querySelector('pre');
    if (result) result.textContent = JSON.stringify(job, null, 2);
    if (!['queued','running'].includes(job.status)) inspectedJob = null;
  } catch (reason) {
    if(view==='tools' && inspectedJob===jobId && revision===detailRevision) { inspectedJob = null; fail(reason); }
  } finally { pollingJob=false; }
}, 1000);

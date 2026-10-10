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
let taskPanelClosed = false;
let taskPanelOpen = false;
let toolHistoryClosed = false;
let toolHistoryOpen = false;
const dismissedTasks = new Set((localStorage.getItem('f8-maintenance-dismissed') || '').split('\n').filter(Boolean));
let legacyTaskDismissals = dismissedTasks.size > 0;
const dismissedTools = new Set((localStorage.getItem('f8-tool-dismissed') || '').split('\n').filter(Boolean));
const activeJob = state => ['queued','running'].includes(state);
function dismissRecords(key, dismissed, ids) {
  for(const id of ids) dismissed.add(id);
  localStorage.setItem(key, [...dismissed].join('\n'));
}
function closeDetail() { detailRevision++;inspectedJob=null;inspectedResource=null;detail.hidden=true; }
function detailCloseButton() { action(detail,'Close details',closeDetail); }
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
const actionIcons = {
  start: ['m8 5 11 7-11 7Z'], stop: ['M6 6h12v12H6Z'],
  restart: ['M3 11a9 9 0 1 1 2.4 7', 'M3 4v7h7'],
  open: ['M15 3h6v6', 'm10 14 11-11', 'M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5'],
  install: ['M12 3v12', 'm7 10 5 5 5-5', 'M5 16v4h14v-4'],
  cancel: ['m6 6 12 12', 'M18 6 6 18'],
  enable: ['m5 12 4 4L19 6'], disable: ['M4 4 20 20', 'M9 3h6l6 6v6l-6 6H9l-6-6V9Z'],
  uninstall: ['M3 6h18', 'M9 6V3h6v3', 'm5 6 1 15h12l1-15', 'M10 10v7', 'M14 10v7'],
  details: ['M12 11v6', 'M12 7h.01', 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0'],
  logs: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z', 'M14 2v6h6', 'M8 13h8', 'M8 17h6'],
};
function iconAction(parent, label, iconName, run, disabled=false) {
  const button=action(parent,label,run);button.className='icon-action';button.title=label;
  button.setAttribute('aria-label',label);button.disabled=disabled;
  const icon=document.createElementNS('http://www.w3.org/2000/svg','svg');
  icon.setAttribute('viewBox','0 0 24 24');icon.setAttribute('fill','none');icon.setAttribute('stroke','currentColor');
  icon.setAttribute('stroke-width','1.8');icon.setAttribute('stroke-linecap','round');icon.setAttribute('stroke-linejoin','round');icon.setAttribute('aria-hidden','true');
  for(const data of actionIcons[iconName]) {const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',data);icon.append(path);}
  button.replaceChildren(icon);return button;
}
async function applicationAction(extensionId, source, command, buttons) {
  for(const button of buttons.querySelectorAll('button'))button.disabled=true;
  try {
    const result=await api(`${source?'source-applications':'applications'}/${encodeURIComponent(extensionId)}/${command}`,'POST');
    if(result.jobId) {managementJobs=[result,...managementJobs.filter(job=>job.jobId!==result.jobId)];renderTaskPanel();}
    await load();
  } catch(reason) {for(const button of buttons.querySelectorAll('button'))button.disabled=false;throw reason;}
}
function card(title, description) { const el = node('article',undefined,'card'); el.append(node('h2',title)); if(description) el.append(node('p',description,'meta')); return el; }
function badge(parent,text) { parent.append(node('span',text,'badge '+text)); }
function showDetail(title,value,jobId=null) { inspectedResource=null;inspectedJob=jobId;detailRevision++;detail.hidden=false; detail.replaceChildren(node('h2',title),node('pre',typeof value === 'string' ? value : JSON.stringify(value,null,2))); detailCloseButton();detail.scrollIntoView({behavior:'smooth',block:'nearest'}); }
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
async function clearTaskRecords(ids) {
  const remaining=await api('management-jobs/clear-completed','POST',{jobIds:ids});
  for(const id of ids)dismissedTasks.add(id);
  managementJobs=remaining;
  closeDetail();renderTaskPanel();
  if(view==='tasks')await load();
}
function taskRows(parent,jobs) {
  for(const job of jobs) {
    const row=node('article',undefined,'row task-row');const summary=node('div');
    summary.append(node('strong',taskTitle(job)));badge(summary,job.state);
    summary.append(node('time',new Date(job.createdAt*1000).toLocaleString(),'meta'));
    if(job.startedAt != null)summary.append(node('small',` · ${Math.max(0,(job.finishedAt ?? Date.now()/1000)-job.startedAt).toFixed(1)}s`,'meta'));
    row.append(summary);const buttons=node('div',undefined,'actions');row.append(buttons);
    action(buttons,'Task details / logs',()=>inspect(taskTitle(job),`management-jobs/${job.jobId}/logs`,null,logs=>logs.log || job.detail));
    if(['queued','running'].includes(job.state) && job.cancellable && !job.cancelRequested)
      action(buttons,'Cancel task',async()=>{await api(`management-jobs/${job.jobId}/cancel`,'POST');await pollTasks();});
    if(!activeJob(job.state))action(buttons,'Dismiss',()=>clearTaskRecords([job.jobId]));
    parent.append(row);
  }
}
function renderTaskPanel() {
  const visible=managementJobs.filter(job=>activeJob(job.state) || !dismissedTasks.has(job.jobId));
  const active=visible.filter(job=>activeJob(job.state));
  taskPanel.hidden=!visible.length || view==='tasks';
  taskPanel.replaceChildren();
  if(taskPanelClosed) {action(taskPanel,`Show maintenance tasks · ${active.length} active`,()=>{taskPanelClosed=false;renderTaskPanel();});return;}
  const heading=node('div',undefined,'history-toolbar');taskPanel.append(heading);
  heading.append(node('h2',`Maintenance tasks · ${active.length} active`));
  clearTasksButton(heading,visible);
  action(heading,'Close tasks',()=>{taskPanelClosed=true;renderTaskPanel();});
  const records=node('details');records.open=taskPanelOpen;records.ontoggle=()=>{taskPanelOpen=records.open;};
  records.append(node('summary',`Task records · ${visible.length}`));
  const rows=node('div',undefined,'history-records');taskRows(rows,visible);records.append(rows);taskPanel.append(records);
}
function clearTasksButton(parent,jobs) {
  const completed=jobs.filter(job=>!activeJob(job.state));
  const button=action(parent,'Clear completed',()=>clearTaskRecords(completed.map(job=>job.jobId)));
  button.disabled=!completed.length;
}
async function tasks(content) {
  content.append(node('h1','Tasks'));
  const visible=managementJobs.filter(job=>activeJob(job.state) || !dismissedTasks.has(job.jobId));
  clearTasksButton(content,visible);taskRows(content,visible);
  if(!visible.length)content.append(node('p','No task records.','empty'));
}
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
    const buttons=node('div',undefined,'actions extension-actions'); el.append(buttons);
    const activeApplicationJob=managementJobs.find(job=>job.request.extensionId===item.extensionId && activeJob(job.state) &&
      ['start-source','start-application','restart-source','restart-application'].includes(job.request.action));
    const applicationBusy=Boolean(activeApplicationJob) || item.applicationOperation?.state==='running';
    if(activeApplicationJob)badge(el,activeApplicationJob.request.action.startsWith('restart')?'restarting':activeApplicationJob.state);
    const source=sources.find(x=>x.extensionId === item.extensionId);
    if(source && (source.managed || source.state !== 'running')) {
      iconAction(buttons,source.state==='running'?'Stop source':'Start source',source.state==='running'?'stop':'start',
        ()=>applicationAction(item.extensionId,true,source.state==='running'?'stop':'start',buttons),applicationBusy);
      if(source.state==='running')iconAction(buttons,'Restart source','restart',()=>applicationAction(item.extensionId,true,'restart',buttons),applicationBusy);
    }
    const installed=apps.find(x=>x.manifest.extensionId === item.extensionId && x.selected);
    if(installed) {
      iconAction(buttons,installed.state==='running'?'Stop':'Start',installed.state==='running'?'stop':'start',
        ()=>applicationAction(item.extensionId,false,installed.state==='running'?'stop':'start',buttons),applicationBusy);
      if(installed.state==='running')iconAction(buttons,'Restart','restart',()=>applicationAction(item.extensionId,false,'restart',buttons),applicationBusy);
      if(installed.manifest.webAssets && installed.state==='running')iconAction(buttons,'Open','open',()=>window.open(installed.endpoints.find(x=>x.name===installed.manifest.health.endpoint).url,'_blank','noopener'));
    }
    if(source?.state === 'running') {
      const def=source.endpoints.find(x=>x.name==='http') || source.endpoints[0];
      if(def)iconAction(buttons,'Open','open',()=>window.open(def.url,'_blank','noopener'));
    }
    if(!item.application || item.releaseSha256) {
      if(item.state === 'available' || item.state === 'failed')iconAction(buttons,'Install','install',async()=>{await api(`extensions/${item.extensionId}/install`,'POST');await load();});
      if(item.state === 'installing')iconAction(buttons,'Cancel installation','cancel',async()=>{await api(`extensions/${item.extensionId}/cancel`,'POST');await load();});
      if(['installed','disabled'].includes(item.state)) {
        iconAction(buttons,item.state === 'installed' ? 'Disable' : 'Enable',item.state==='installed'?'disable':'enable',async()=>{await api(`extensions/${item.extensionId}/enabled`,'PUT',{enabled:item.state!=='installed'});await load();},applicationBusy);
        iconAction(buttons,'Uninstall','uninstall',async()=>{await api(`extensions/${item.extensionId}`,'DELETE');await load();},applicationBusy);
      }
    }
    iconAction(buttons,'Details','details',()=>inspect(item.name,`extensions/${item.extensionId}/detail`));
    if(item.application)iconAction(buttons,'Logs','logs',()=>inspect(item.name+' logs',`application-logs/${item.extensionId}`));
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
  const visible=jobs.filter(job=>activeJob(job.status) || !dismissedTools.has(job.jobId));
  const history=node('section',undefined,'tool-history');content.append(history);
  if(toolHistoryClosed)action(history,'Show tool history',()=>{toolHistoryClosed=false;void load();});
  else {
    const heading=node('div',undefined,'history-toolbar');history.append(heading);heading.append(node('h2','Tool jobs'));
    const completed=visible.filter(job=>!activeJob(job.status));
    const clear=action(heading,'Clear completed',()=>{dismissRecords('f8-tool-dismissed',dismissedTools,completed.map(job=>job.jobId));closeDetail();void load();});clear.disabled=!completed.length;
    action(heading,'Close history',()=>{toolHistoryClosed=true;void load();});
    const records=node('details');records.open=toolHistoryOpen;records.ontoggle=()=>{toolHistoryOpen=records.open;};records.append(node('summary',`Task records · ${visible.length}`));history.append(records);
    const rows=node('div',undefined,'history-records');records.append(rows);
    for(const job of visible) {
      const row=node('div',undefined,'row');row.append(node('strong',`${job.extensionId} / ${job.toolId}`));badge(row,job.status);
      if(job.createdAt)row.append(node('time',new Date(job.createdAt).toLocaleString(),'meta'));
      action(row,'Result / logs',()=>inspect(job.toolId,`tool-jobs/${job.jobId}`,job.jobId));
      if(activeJob(job.status))action(row,'Stop',async()=>{await api(`tool-jobs/${job.jobId}/cancel`,'POST');await load();});
      else action(row,'Dismiss',()=>{dismissRecords('f8-tool-dismissed',dismissedTools,[job.jobId]);void load();});
      rows.append(row);
    }
  }
  if(!items.length)content.append(node('p','Install and enable an extension with tools to get started.','empty'));
}
function toolForm(tool) {
  inspectedJob=null;inspectedResource=null;detailRevision++;detail.hidden=false;detail.replaceChildren(node('h2',tool.name));detailCloseButton();const form=node('form');detail.append(form);const controls=new Map();
  for(const field of tool.fields) {
    const label=node('label',field.label || field.name);let input;
    if(field.choices?.length){input=node('select');for(const option of field.choices || []){const el=node('option',String(option));el.value=String(option);input.append(el);}}
    else {input=node('input');input.type=field.kind === 'boolean' ? 'checkbox' : (field.kind === 'number' || field.kind === 'integer' ? 'number':'text');}
    if(input.type==='checkbox')input.checked=Boolean(field.default);else if(field.default !== null && field.default !== undefined)input.value=String(field.default);
    input.required=Boolean(field.required);label.append(input);form.append(label);controls.set(field.name,{field,input});
  }
  let confirm;if(tool.requiresConfirmation){const label=node('label','Confirm execution');confirm=node('input');confirm.type='checkbox';confirm.required=true;label.prepend(confirm);form.append(label);}
  const submit=node('button','Run');submit.type='submit';form.append(submit);
  form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;const revision=navigationRevision;try {const argumentsObject={};for(const [name,{field,input}]of controls){argumentsObject[name]=field.kind==='boolean' ? input.checked : ['number','integer'].includes(field.kind)?Number(input.value):input.value;}const job=await api(`extension-tools/${tool.extensionId}/${tool.toolId}/run`,'POST',{arguments:argumentsObject,confirm:Boolean(confirm?.checked)});if(revision!==navigationRevision)return;closeDetail();toolHistoryClosed=false;await load();}catch(reason){if(revision===navigationRevision)fail(reason);}finally{submit.disabled=false;}};
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
    let jobs=await api('management-jobs');
    if(legacyTaskDismissals) {
      const ids=jobs.filter(job=>!activeJob(job.state) && dismissedTasks.has(job.jobId)).map(job=>job.jobId);
      if(ids.length)jobs=await api('management-jobs/clear-completed','POST',{jobIds:ids});
      localStorage.removeItem('f8-maintenance-dismissed');dismissedTasks.clear();legacyTaskDismissals=false;
    }
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

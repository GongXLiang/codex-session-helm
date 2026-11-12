"use strict";
const labels = {planned:"待派发",running:"执行中",reported:"待审查",accepted:"✓ 已验收",needs_revision:"待修订",waiting_input:"待输入",waiting_approval:"待审批",failed:"执行失败",paused:"已暂停",detached:"已移交",needs_recheck:"待复核"};
const byId = id => document.getElementById(id);
const el = (tag, text, className) => { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node; };
let latest=null, source=null, loading=false, dirty=false, renderedSequence=-1;
let readError="", collectorError="";
const expanded=new Set();
const projectCache=new Map(), pendingChanges=new Map(), latencySamples=[];
let connectedAt=0,lastReadAt=0;
function evidenceLink(task,kind,label){const a=el("a",label);a.href="/evidence?"+new URLSearchParams({project:task.project,workflow:task.workflow,task:task.id,kind});a.target="_blank";a.rel="noopener";return a;}
function showError(){const message=readError||collectorError;byId("error").hidden=!message;byId("error").textContent=message;}
function dependencyGraph(tasks){
  const ns="http://www.w3.org/2000/svg", svg=document.createElementNS(ns,"svg"), wrapper=el("div",undefined,"dependency-graph");
  const ranks=new Map(), positions=new Map(), rows=new Map(), index=new Map(tasks.map(t=>[t.id,t]));
  function rank(task){if(!ranks.has(task.id))ranks.set(task.id,task.dependencies.length?Math.max(...task.dependencies.map(d=>rank(index.get(d.id))))+1:0);return ranks.get(task.id);}
  for(const task of tasks){const layer=rank(task),row=rows.get(layer)||0;positions.set(task.id,{x:layer*160+8,y:row*64+8});rows.set(layer,row+1);}
  const width=(Math.max(...ranks.values())+1)*160,height=Math.max(...rows.values())*64;
  svg.setAttribute("viewBox",`0 0 ${width} ${height}`);svg.setAttribute("role","img");svg.setAttribute("aria-label","任务依赖图；只有前置任务验收后才可派发后续任务");
  function shape(tag,attrs,text){const node=document.createElementNS(ns,tag);for(const [key,value]of Object.entries(attrs))node.setAttribute(key,String(value));if(text)node.textContent=text;svg.append(node);return node;}
  for(const task of tasks){const to=positions.get(task.id);for(const dep of task.dependencies){const from=positions.get(dep.id),end=to.x-3,y=to.y+22;shape("path",{d:`M${from.x+128},${from.y+22} L${end},${y} m-5,-4 l5,4 l-5,4`,class:"edge"});}}
  for(const task of tasks){const p=positions.get(task.id),passed=task.effective_status==="accepted";shape("rect",{x:p.x,y:p.y,width:128,height:44,rx:8,class:passed?"node accepted":"node"});shape("text",{x:p.x+8,y:p.y+18,class:passed?"passed":""},`${task.id} · v${task.revision}`);shape("text",{x:p.x+8,y:p.y+34,class:passed?"state passed":"state"},labels[task.effective_status]);}
  wrapper.append(svg);return wrapper;
}
function date(value){return value?new Date(value).toLocaleString():"尚未核实";}
function connection(online){const node=byId("connection");node.textContent=online?"● 自动更新已连接":"○ 连接中断，正在重连";node.dataset.online=String(online);}
function render(data){
  latest=data; renderedSequence=data.sequence;
  const selection=byId("project-filter").value;
  const filter=byId("project-filter");filter.replaceChildren(new Option("全部项目",""));
  data.projects.forEach(p=>filter.add(new Option(p.title,p.id)));filter.value=selection;
  const tasks=data.tasks.filter(t=>!selection||t.project===selection);
  const reviews=data.reviews.filter(r=>!selection||r.project===selection);
  const decisionTasks=new Set((data.decisions||[]).filter(d=>!selection||d.project===selection).map(d=>`${d.project}/${d.workflow}/${d.task}`));
  tasks.filter(t=>["waiting_input","waiting_approval"].includes(t.effective_status)).forEach(t=>decisionTasks.add(`${t.project}/${t.workflow}/${t.id}`));
  const metrics=[[decisionTasks.size,"待输入 / 审批"],[reviews.length,"待审查 / 审查中"],[tasks.filter(t=>t.status==="running").length,"已派发执行"],[`${tasks.filter(t=>t.effective_status==="accepted").length} / ${tasks.length}`,"有效验收"]];
  byId("metrics").replaceChildren(...metrics.map(([count,label])=>{const n=el("div",undefined,"metric");n.append(el("strong",String(count)),el("span",label));return n;}));
  const projectNodes=[];
  for(const project of data.projects.filter(p=>!selection||p.id===selection)){
    const own=tasks.filter(t=>t.project===project.id), fingerprint=JSON.stringify([project,own]), cached=projectCache.get(project.id);
    if(cached&&cached.fingerprint===fingerprint){projectNodes.push(cached.node);continue;}
    const section=el("article",undefined,"project");section.dataset.project=project.id;section.dataset.renderCount=String((cached?cached.count:0)+1);
    const head=el("div",undefined,"project-head");head.append(el("h2",project.title),el("span",`${own.filter(t=>t.effective_status==="accepted").length}/${own.length} 已验收`,"count"));
    section.append(head,el("p",project.goal,"project-goal"));
    if(own.some(t=>t.dependencies.length))section.append(dependencyGraph(own));
    for(const task of own){
      const row=el("div",undefined,"task");row.dataset.task=task.id;row.dataset.status=task.effective_status;
      const heading=el("div",undefined,"task-head");heading.append(el("span",`${task.id} · ${task.title}`,"task-title"),el("span",labels[task.effective_status]||task.effective_status,`badge ${task.effective_status}`));row.append(heading);
      row.append(el("p",`第 ${task.revision} 版 · 最近进展 ${date(task.updated_at)}`,"task-meta"));
      if(task.dependencies.length)row.append(el("p","前置："+task.dependencies.map(d=>`${d.id} 第 ${d.version} 版`).join("、"),"dependency"));
      if(task.note)row.append(el("p",task.note,"task-note"));
      const detail=el("details"), key=`${task.project}/${task.workflow}/${task.id}`;detail.open=expanded.has(key);detail.addEventListener("toggle",()=>detail.open?expanded.add(key):expanded.delete(key));detail.append(el("summary","查看运行与证据入口"));
      detail.append(el("p",`运行观察：${task.run_state}；核实时间：${date(task.observed_at)}`));
      detail.append(el("p",`工作目录：${project.root}`));
      if(task.thread){const p=el("p",`执行对话：${task.thread} `),button=el("button","复制会话 ID");button.type="button";button.addEventListener("click",async()=>{try{await navigator.clipboard.writeText(task.thread);button.textContent="已复制";}catch{button.textContent="请选中上方 ID 复制";}});p.append(button);detail.append(p);}
      if(task.report)detail.append(evidenceLink(task,"report","打开报告"));
      if(task.review)detail.append(evidenceLink(task,"review","打开独立验收记录"));
      if(task.ready_reason&&task.status==="planned")detail.append(el("p","派发条件尚未满足；由主管核对依赖、并发与写入范围。"));
      row.append(detail);section.append(row);
    }
    projectNodes.push(section);
    projectCache.set(project.id,{fingerprint,node:section,count:Number(section.dataset.renderCount)});
  }
  byId("projects").replaceChildren(...(projectNodes.length?projectNodes:[el("p","尚未登记项目。","empty")]));
  byId("reviews").replaceChildren(...(reviews.length?reviews.map((r,i)=>{const n=el("div",undefined,"queue-item"), p=data.projects.find(p=>p.id===r.project);n.append(el("strong",`${i+1}. ${p?p.title:r.project} · ${r.task}`),el("p",`${r.state==="claimed"?"主管审查中":"已收到，等待审查"} · 第 ${r.revision} 版`),el("p",`已等待 ${Math.floor(r.wait_seconds/60)} 分钟 · 可解锁 ${r.unlocks} 项`),evidenceLink({...r,id:r.task},"report","查看待审报告"));return n;}):[el("p","没有等待审查的报告。","empty")]));
  const attention=tasks.filter(t=>["waiting_input","waiting_approval","failed","needs_revision","needs_recheck"].includes(t.effective_status));
  const attentionNodes=attention.map(t=>{const n=el("div",undefined,"queue-item");n.append(el("strong",`${t.project} · ${t.id} · ${labels[t.effective_status]}`),el("p",t.note||"主管需核对后续处理。"));return n;});
  for(const d of (data.decisions||[]).filter(d=>!selection||d.project===selection)){const n=el("div",undefined,"queue-item decision");n.dataset.decision=d.id;n.append(el("strong",`${d.project} · ${d.task} · 待决定`),el("p",d.question),el("p","影响："+d.impact));const options=el("ol");JSON.parse(d.options).forEach(o=>options.append(el("li",o)));n.append(options,el("p","请在主管对话中作决定；看板不提交批准。"));attentionNodes.unshift(n);}
  for(const change of data.recent_changes.filter(c=>c.kind==="event.rejected"&&(!selection||c.project===selection)).slice(0,3)){const info=JSON.parse(change.payload),n=el("div",undefined,"queue-item");n.append(el("strong",`${change.project} · 最近拒收记录`),el("p",`${info.file}：${info.reason}`));attentionNodes.push(n);}
  byId("attention").replaceChildren(...(attentionNodes.length?attentionNodes:[el("p","当前没有输入、修订或复核请求。","empty")]));
  const changed=data.recent_changes[0];byId("freshness").textContent="账本最近变化："+date(changed&&changed.created_at);
  byId("sequence").textContent=`数据序号 ${data.sequence} · 页面读取 ${date(data.generated_at)}`;
  byId("focus").textContent=data.focus?`主管当前上下文：${data.projects.find(p=>p.id===data.focus.project)?.title||data.focus.project}`:"主管未选定项目；下一次审查前需加载对应项目上下文。";
  const applied=[...pendingChanges].filter(([seq])=>seq<=data.sequence);
  applied.forEach(([seq])=>pendingChanges.delete(seq));
  if(applied.length&&!document.hidden)requestAnimationFrame(()=>requestAnimationFrame(()=>{
    for(const [seq,event]of applied)latencySamples.push({sequence:seq,project:event.project,ingested_at:event.created_at,rendered_at:new Date().toISOString(),latency_ms:Date.now()-Date.parse(event.created_at),replay:Date.parse(event.created_at)<connectedAt});
    if(latencySamples.length>200)latencySamples.splice(0,latencySamples.length-200);
    byId("latency-samples").textContent=JSON.stringify(latencySamples);
    const live=latencySamples.filter(s=>!s.replay);if(live.length)byId("latency").textContent=`本页实测：最近更新 ${live.at(-1).latency_ms} ms · ${live.length} 个变化样本`;
  }));
}
async function refresh(){
  if(loading){dirty=true;return;}
  loading=true;
  try{do{dirty=false;const response=await fetch("/api/snapshot",{cache:"no-store"});if(!response.ok)throw new Error("无法读取任务账本");const data=await response.json();if(data.sequence>=renderedSequence)render(data);lastReadAt=Date.now();readError="";showError();}while(dirty);}
  catch(error){readError=error.message;showError();}
  finally{loading=false;}
}
async function connect(){
  await refresh();
  source=new EventSource(`/events?after=${Math.max(0,renderedSequence)}`);
  source.addEventListener("open",()=>{connectedAt=Date.now();connection(true);refresh();});
  source.addEventListener("change",event=>{const value=JSON.parse(event.data);pendingChanges.set(value.sequence,value);refresh();});
  source.addEventListener("reset",()=>{renderedSequence=-1;refresh();});
  source.addEventListener("heartbeat",event=>{const state=JSON.parse(event.data);connection(true);collectorError=state.collector_error?"事件采集暂时失败，显示最后已保存状态："+state.collector_error:"";showError();if(readError||(latest?.reviews.length&&Date.now()-lastReadAt>=15000))refresh();});
  source.addEventListener("error",()=>connection(false));
}
byId("project-filter").addEventListener("change",()=>{if(latest)render(latest);});
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
connect();
